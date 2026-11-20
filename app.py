import contextlib
import io
import os
import random
import sqlite3
import time
import uuid
from importlib import import_module
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    flask_mail = import_module("flask_mail")
    Mail = flask_mail.Mail
    Message = flask_mail.Message
except ImportError:
    class Mail:
        def __init__(self, app=None):
            self.app = app

        def send(self, message):
            raise ImportError(
                "The 'flask-mail' package is not installed. Install it with 'pip install flask-mail'."
            )

    class Message:
        def __init__(self, subject="", sender=None, recipients=None, body=""):
            self.subject = subject
            self.sender = sender
            self.recipients = recipients or []
            self.body = body

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    template_folder=str(BASE_DIR / "templates"),
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-development-only-change-me")

# Flask-Mail configuration with explicit default sender
mail_user = os.getenv("MAIL_USERNAME", "").strip()
mail_pass = os.getenv("MAIL_PASSWORD", "").strip()

app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com").strip()
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() in {"1", "true", "yes"}
app.config["MAIL_USERNAME"] = mail_user
app.config["MAIL_PASSWORD"] = mail_pass
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", mail_user).strip() or None

mail = Mail(app)
otp_store = {}

try:
    litellm = import_module("litellm")
    completion = litellm.completion
except ImportError:
    litellm = None

    def completion(*args, **kwargs):
        raise ImportError(
            "The 'litellm' package is not installed. Install it with 'pip install litellm'."
        )

for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
    value = os.getenv(key)
    if value:
        os.environ[key] = value

DB_NAME = str(BASE_DIR / "bats_ai.db")


@contextlib.contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT,
                user_email TEXT,
                owner_key TEXT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        """)
        cursor.execute("PRAGMA table_info(sessions)")
        session_cols = {row[1] for row in cursor.fetchall()}
        if "owner_key" not in session_cols:
            cursor.execute("ALTER TABLE sessions ADD COLUMN owner_key TEXT")

        cursor.execute("PRAGMA table_info(messages)")
        msg_cols = {row[1] for row in cursor.fetchall()}
        if "created_at" not in msg_cols:
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            )
        conn.commit()


init_db()


def get_current_owner():
    """Return (owner_key, user_email, display_name) for signed-in users or persistent guests."""
    user_email = session.get("user_email")
    if user_email:
        return user_email, user_email, session.get("user_name", "Bats User")

    guest_id = session.get("guest_id")
    if not guest_id:
        guest_id = f"guest-{uuid.uuid4().hex}"
        session["guest_id"] = guest_id
    return guest_id, None, "Guest User"


def fetch_recent_cross_session_memory(owner_key, current_session_id, limit_messages=12):
    """Fetch brief context from the user's other recent sessions so the AI remembers past chats."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.title, m.role, m.content
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE (s.owner_key = ? OR s.user_email = ?)
              AND s.session_id != ?
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (owner_key, owner_key, current_session_id, limit_messages),
        )
        rows = cursor.fetchall()

    if not rows:
        return ""

    rows.reverse()
    snippets = []
    for title, role, content in rows:
        clean_text = " ".join((content or "").split())
        if len(clean_text) > 280:
            clean_text = clean_text[:280] + "..."
        snippets.append(f"[{title}] {role}: {clean_text}")

    return (
        "PREVIOUS CONVERSATION MEMORY (refer to this if the user asks about earlier chats or "
        "past context, without forcing it when irrelevant):\n" + "\n".join(snippets)
    )


MODEL_MODES = {
    "auto": {
        "primary": "groq/openai/gpt-oss-20b",
        "fallbacks": ["gemini/gemini-2.5-flash", "openrouter/deepseek/deepseek-chat"],
    },
    "light": {
        "primary": "gemini/gemini-2.5-flash",
        "fallbacks": ["groq/openai/gpt-oss-20b"],
    },
    "fast": {
        "primary": "groq/openai/gpt-oss-20b",
        "fallbacks": ["gemini/gemini-2.5-flash"],
    },
    "smart": {
        "primary": "openrouter/deepseek/deepseek-chat",
        "fallbacks": ["gemini/gemini-2.5-flash", "groq/openai/gpt-oss-20b"],
    },
}


def search_web(query, limit=6):
    """Query a self-hosted SearXNG instance; returns normalized public results."""
    base_url = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080").rstrip("/")
    response = requests.get(
        f"{base_url}/search",
        params={"q": query, "format": "json", "language": "auto"},
        headers={"User-Agent": "BatsAI/1.0"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    for item in payload.get("results", [])[:limit]:
        url = item.get("url")
        title = item.get("title") or url or "Search result"
        content = item.get("content") or ""
        if url and url.startswith(("https://", "http://")):
            results.append({"title": title[:300], "url": url, "snippet": content[:1000]})
    return results


def _extract_model_name(response):
    if isinstance(response, dict):
        return response.get("model")
    return getattr(response, "model", None)


def _extract_message_content(response):
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content:
                return content
    choices = getattr(response, "choices", None) or []
    if choices:
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if content:
            return content
    return ""


@app.route("/")
def home():
    get_current_owner()
    return render_template("index.html")


@app.route("/api/signin", methods=["POST"])
def signin():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, password_hash FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()

        if not row or not check_password_hash(row[1], password):
            return jsonify({"error": "Invalid email or password."}), 401

        user_name = row[0]

        guest_id = session.get("guest_id")
        if guest_id:
            cursor.execute(
                """
                UPDATE sessions
                SET user_email = ?, username = ?, owner_key = ?
                WHERE owner_key = ? AND user_email IS NULL
                """,
                (email, user_name, email, guest_id),
            )
            conn.commit()

    session["user_email"] = email
    session["user_name"] = user_name

    return jsonify(
        {"message": "Signed in successfully!", "user": {"email": email, "name": user_name}}
    )


@app.route("/api/send-signup-otp", methods=["POST"])
def send_signup_otp():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    name = data.get("name", "").strip()
    password = data.get("password", "").strip()

    if not email or not name or not password:
        return jsonify({"error": "Name, email, and password are required."}), 400

    sender_email = (
        app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME") or ""
    ).strip()
    mail_password = (app.config.get("MAIL_PASSWORD") or "").strip()

    if (
        not sender_email
        or sender_email == "your-email@gmail.com"
        or not mail_password
        or mail_password == "your-app-password"
    ):
        return jsonify(
            {
                "error": (
                    "Email sender is not configured on the server. Set MAIL_USERNAME and "
                    "MAIL_PASSWORD (Gmail App Password) in your .env file and restart."
                )
            }
        ), 500

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            return jsonify(
                {"error": "An account with this email already exists. Please sign in."}
            ), 400

    otp_code = f"{random.randint(100000, 999999)}"
    otp_store[email] = {
        "otp": otp_code,
        "name": name,
        "password_hash": generate_password_hash(password),
        "expires_at": time.time() + 300,
    }

    try:
        msg = Message(
            subject="Your Bats AI Verification Code",
            sender=sender_email,
            recipients=[email],
        )
        msg.body = (
            f"Hello {name},\n\nYour 6-digit account creation code is: {otp_code}\n\n"
            "This code is valid for 5 minutes."
        )
        mail.send(msg)
        return jsonify({"message": "OTP sent successfully to your email."})
    except Exception as e:
        app.logger.exception("Failed to send OTP email to %s", email)
        return jsonify({"error": f"Failed to send email: {str(e)}"}), 500


@app.route("/api/verify-signup-otp", methods=["POST"])
def verify_signup_otp():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    otp = data.get("otp", "").strip()

    record = otp_store.get(email)
    if not record or time.time() > record["expires_at"]:
        return jsonify({"error": "Invalid or expired OTP code."}), 400

    if record["otp"] != otp:
        return jsonify({"error": "Incorrect 6-digit OTP code."}), 400

    user_name = record["name"]
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
            (email, user_name, record["password_hash"]),
        )
        guest_id = session.get("guest_id")
        if guest_id:
            cursor.execute(
                """
                UPDATE sessions
                SET user_email = ?, username = ?, owner_key = ?
                WHERE owner_key = ? AND user_email IS NULL
                """,
                (email, user_name, email, guest_id),
            )
        conn.commit()

    del otp_store[email]

    session["user_email"] = email
    session["user_name"] = user_name

    return jsonify(
        {"message": "Account created successfully!", "user": {"email": email, "name": user_name}}
    )


@app.route("/api/me", methods=["GET"])
def get_me():
    email = session.get("user_email")
    if not email:
        return jsonify({"authenticated": False}), 401
    return jsonify(
        {
            "authenticated": True,
            "user": {
                "email": email,
                "name": session.get("user_name", "Bats User"),
            },
        }
    )


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully."})


ALLOWED_UPLOAD_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".pdf",
    ".docx",
    ".xlsx",
    ".xlsm",
    ".pptx",
}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def extract_uploaded_file(upload):
    """Extract readable text from common office/document formats."""
    filename = Path(upload.filename or "uploaded_file").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(
            "Unsupported file type. Upload PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, or JSON."
        )
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("File is too large. Maximum upload size is 12 MB.")
    if not raw:
        raise ValueError("The uploaded file is empty.")

    if suffix in {".txt", ".md", ".json", ".csv"}:
        return raw.decode("utf-8-sig", errors="replace")
    if suffix == ".pdf":
        try:
            PdfReader = __import__("pypdf", fromlist=["PdfReader"]).PdfReader
        except ImportError as exc:
            raise ValueError(
                "PDF support requires 'pypdf'. Install it with: pip install pypdf"
            ) from exc

        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        try:
            Document = __import__("docx", fromlist=["Document"]).Document
        except ImportError as exc:
            raise ValueError(
                "DOCX support requires 'python-docx'. Install it with: pip install python-docx"
            ) from exc

        doc = Document(io.BytesIO(raw))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    if suffix in {".xlsx", ".xlsm"}:
        try:
            load_workbook = __import__("openpyxl", fromlist=["load_workbook"]).load_workbook
        except ImportError as exc:
            raise ValueError(
                "Excel support requires 'openpyxl'. Install it with: pip install openpyxl"
            ) from exc

        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        parts = []
        for sheet in workbook.worksheets:
            parts.append(f"SHEET: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                if any(values):
                    parts.append(" | ".join(values))
        return "\n".join(parts)
    if suffix == ".pptx":
        try:
            Presentation = __import__("pptx", fromlist=["Presentation"]).Presentation
        except ImportError as exc:
            raise ValueError(
                "PPTX support requires 'python-pptx'. Install it with: pip install python-pptx"
            ) from exc

        deck = Presentation(io.BytesIO(raw))
        parts = []
        for number, slide in enumerate(deck.slides, start=1):
            parts.append(f"SLIDE {number}")
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text.strip():
                    parts.append(shape.text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    return ""


@app.route("/chat", methods=["POST"])
def chat():
    owner_key, user_email, username = get_current_owner()

    if request.content_type and request.content_type.startswith("multipart/form-data"):
        data = request.form
        upload = request.files.get("file")
    else:
        data = request.get_json(silent=True) or {}
        upload = None
    user_message = (data.get("message") or "").strip()
    selected_mode = data.get("mode", "auto")
    session_id = data.get("session_id", "default_session")

    file_context = ""
    safe_filename = "uploaded_file"
    if upload and getattr(upload, "filename", None):
        safe_filename = Path(upload.filename).name or "uploaded_file"
        try:
            file_context = extract_uploaded_file(upload).strip()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Error extracting uploaded file '%s'", safe_filename)
            return jsonify(
                {"error": f"Could not read '{safe_filename}': {exc}"}
            ), 400

        if not file_context:
            return jsonify(
                {
                    "error": (
                        f"No readable text was found in '{safe_filename}'. "
                        "Scanned PDFs or image-only files require OCR before uploading."
                    )
                }
            ), 400

    if not user_message and not file_context:
        return jsonify({"error": "Type a message or attach a readable file."}), 400

    raw_title_source = user_message or f"File: {safe_filename}"

    if file_context:
        prompt_prefix = (
            user_message + "\n\n"
            if user_message
            else f"Please read and analyze '{safe_filename}'."
        )
        user_message = f"{prompt_prefix}[FILE: {safe_filename}]\n{file_context[:100000]}"

    web_results = []
    web_search_enabled = str(data.get("web_search", "false")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if web_search_enabled:
        try:
            web_results = search_web(user_message[:500])
        except requests.RequestException as exc:
            return jsonify(
                {
                    "error": (
                        "Live web search is unavailable. Check that SearXNG is running and "
                        f"SEARXNG_URL is correct. Details: {exc}"
                    )
                }
            ), 503
        except (ValueError, KeyError) as exc:
            return jsonify({"error": f"SearXNG returned an invalid response: {exc}"}), 502

        if web_results:
            source_text = "\n\n".join(
                f"[{i}] {item['title']}\nURL: {item['url']}\nSnippet: {item['snippet']}"
                for i, item in enumerate(web_results, start=1)
            )
            user_message += (
                "\n\nLIVE WEB SEARCH RESULTS (use these as evidence, distinguish snippets from "
                "verified full-page content, do not invent details, and cite sources using their "
                "URLs):\n" + source_text
            )
        else:
            user_message += (
                "\n\nLive web search returned no results. Tell the user that no results were "
                "found; do not pretend to have searched successfully."
            )

    system_prompt = (
        "You are Bats AI, a helpful, advanced AI assistant. "
        "CRITICAL RULES TO FOLLOW:\n"
        "1. Whenever writing math, physics, engineering, or science equations, ALWAYS use LaTeX ($...$ for inline, $$...$$ for display) AND use real Greek letters and symbols (such as α, β, γ, δ, ε, θ, λ, μ, ν, π, ρ, σ, τ, φ, ψ, ω, Δ, Λ, Σ, Ω, ≈, ≠, ≤, ≥, ∞) rather than writing words like 'alpha' or 'beta'.\n"
        "2. Ensure your answers are complete, detailed, well-structured, and explain steps. Strictly use clear headings, paragraphs, and bullet points.\n"
        "3. When a user uploads a document, spreadsheet, presentation, or text file, base your answer on its extracted contents. State clearly if the file does not contain the requested information. Do not invent missing file contents.\n"
        "4. When live web search results are supplied, use them carefully, cite the supplied URLs as Markdown links, and distinguish search snippets from facts verified by opening a page.\n"
        "5. When providing an image URL, format it as a Markdown image `![description](https://...)` so the interface displays the image directly.\n"
        "6. Do not use tables in your answers.\n"
        "7. Remember the full conversation history in this thread and any previous conversation memory provided below so you can recall user preferences, names, and earlier topics."
    )

    cross_session_memory = fetch_recent_cross_session_memory(owner_key, session_id)
    if cross_session_memory:
        system_prompt += "\n\n" + cross_session_memory

    litellm_messages = [{"role": "system", "content": system_prompt}]

    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT session_id FROM sessions WHERE session_id = ?", (session_id,))
        if not cursor.fetchone():
            title = (
                raw_title_source[:36] + "..."
                if len(raw_title_source) > 36
                else raw_title_source
            )
            cursor.execute(
                """
                INSERT INTO sessions (session_id, username, user_email, owner_key, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, username, user_email, owner_key, title),
            )

        cursor.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 40
            ) ORDER BY id ASC
            """,
            (session_id,),
        )
        for role, content in cursor.fetchall():
            litellm_messages.append({"role": role, "content": content})

        litellm_messages.append({"role": "user", "content": user_message})

        cursor.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, "user", user_message),
        )
        conn.commit()

    config = MODEL_MODES.get(selected_mode, MODEL_MODES["auto"])
    primary_model = config["primary"]
    fallback_models = config["fallbacks"]

    max_retries = 3
    retry_delay = 4.0
    response = None
    last_exception = None

    for attempt in range(max_retries):
        try:
            response = completion(
                model=primary_model,
                messages=litellm_messages,
                fallbacks=fallback_models,
                max_tokens=4000,
            )
            break
        except Exception as e:
            last_exception = e
            if "429" in str(e) or "rate_limit" in str(e).lower():
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                break

    if not response:
        return jsonify(
            {"error": f"All fallback providers failed or rate limit exceeded: {str(last_exception)}"}
        ), 500

    assistant_reply = _extract_message_content(response)
    if not assistant_reply:
        return jsonify({"error": "Model returned an empty response"}), 500

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, "assistant", assistant_reply),
        )
        conn.commit()

    return jsonify(
        {
            "reply": assistant_reply,
            "provider_used": _extract_model_name(response) or primary_model,
            "mode_used": selected_mode,
            "history_saved": True,
            "web_search_used": web_search_enabled,
            "sources": web_results,
        }
    )


@app.route("/sessions/<email_or_username>", methods=["GET"])
def get_user_sessions(email_or_username):
    owner_key, user_email, _ = get_current_owner()
    lookup_keys = list({k for k in (owner_key, user_email, email_or_username) if k})
    placeholders = ", ".join("?" for _ in lookup_keys)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT session_id, title, created_at
            FROM sessions
            WHERE owner_key IN ({placeholders})
               OR user_email IN ({placeholders})
               OR username IN ({placeholders})
            ORDER BY created_at DESC
            """,
            lookup_keys * 3,
        )
        rows = cursor.fetchall()

    sessions = [{"session_id": r[0], "title": r[1], "created_at": r[2]} for r in rows]
    return jsonify(sessions)


@app.route("/history/<session_id>", methods=["GET"])
def get_session_history(session_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
        )
        rows = cursor.fetchall()

    messages = [{"role": r[0], "content": r[1]} for r in rows]
    return jsonify(messages)


@app.route("/session/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return jsonify({"message": "Session deleted successfully"}), 200
        except Exception as e:
            return jsonify({"error": f"Failed to delete session: {str(e)}"}), 500


@app.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Endpoint or resource not found."}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error occurred."}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("Unhandled error in Bats AI")
    return jsonify({"error": f"An unexpected error occurred: {str(error)}"}), 500


if __name__ == "__main__":
    print("BASE_DIR:", BASE_DIR)
    print("STATIC_FOLDER:", app.static_folder)
    print("TEMPLATE_FOLDER:", app.template_folder)
    app.run(debug=True, port=5000, use_reloader=False)
