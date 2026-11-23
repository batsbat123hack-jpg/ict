import base64
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from importlib import import_module
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    template_folder=str(BASE_DIR / "templates"),
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-development-only-change-me")

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
            CREATE TABLE IF NOT EXISTS oauth_accounts (
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                user_email TEXT NOT NULL UNIQUE,
                PRIMARY KEY (provider, provider_user_id),
                FOREIGN KEY (user_email) REFERENCES users (email)
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

MODEL_PROVIDER_API_KEYS = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def has_provider_credentials(model):
    provider = model.partition("/")[0]
    key_name = MODEL_PROVIDER_API_KEYS.get(provider)
    return bool(key_name and os.getenv(key_name, "").strip())


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


def should_auto_search_web(query):
    """Search when a question depends on current information or explicitly asks for sources."""
    current_info = re.search(
        r"\b(latest|current(?:ly)?|today|tonight|yesterday|tomorrow|"
        r"this\s+(?:week|month|year|morning)|right\s+now|as\s+of|"
        r"recent(?:ly)?|newest|breaking|live)\b",
        query,
        re.IGNORECASE,
    )
    live_topics = re.search(
        r"\b(weather|forecast|headlines|news|stock prices?|share prices?|"
        r"exchange rates?|bitcoin prices?|live scores?|sports scores?|"
        r"traffic|flight status|opening hours)\b",
        query,
        re.IGNORECASE,
    )
    source_request = re.search(
        r"\b(?:search|look\s+up|find|check)\b.{0,50}\b"
        r"(?:web|online|internet|sources?|citations?)\b",
        query,
        re.IGNORECASE,
    )
    return bool(current_info or live_topics or source_request)


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


@app.route("/api/generate-image", methods=["POST"])
def generate_image():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request body is required."}), 400

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify({"error": "Describe the image you want to create."}), 400
    prompt = prompt.strip()
    if len(prompt) > 4000:
        return jsonify({"error": "Image prompts must be 4,000 characters or fewer."}), 400

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "Image generation requires GEMINI_API_KEY to be configured."}), 503

    model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1").strip()
    model = model or "gemini-nano-banana-2.1"
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": "2026-05-20",
            },
            json={"model": model, "input": prompt},
            timeout=180,
        )
    except requests.RequestException:
        app.logger.exception("Gemini image generation request failed")
        return jsonify({"error": "Could not reach the image generation service. Try again later."}), 502

    try:
        payload = response.json()
    except ValueError:
        app.logger.error("Gemini image generation returned invalid JSON (HTTP %s)", response.status_code)
        return jsonify({"error": "The image generation service returned an invalid response."}), 502

    if not response.ok:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        detail = error.get("message") if isinstance(error, dict) else None
        detail = detail or f"Image generation failed (HTTP {response.status_code})."
        return jsonify({"error": detail}), 502

    image = payload.get("output_image") if isinstance(payload, dict) else None
    image_data = image.get("data") if isinstance(image, dict) else None
    if not isinstance(image_data, str) or not image_data:
        return jsonify({"error": "The image generation service returned no image."}), 502

    return jsonify(
        {
            "image_base64": image_data,
            "image_mime_type": image.get("mime_type") or "image/png",
        }
    )


@app.route("/loggin")
@app.route("/loggin.html")
def loggin():
    get_current_owner()
    return render_template("loggin.html", auth_error=session.pop("auth_error", None))


@app.route("/login")
def login():
    return loggin()


OAUTH_PROVIDERS = {
    "google": {
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
    },
    "facebook": {
        "client_id_env": "FACEBOOK_APP_ID",
        "client_secret_env": "FACEBOOK_APP_SECRET",
        "authorize_url": "https://www.facebook.com/v25.0/dialog/oauth",
        "token_url": "https://graph.facebook.com/v25.0/oauth/access_token",
        "scope": "email,public_profile",
    },
    "github": {
        "client_id_env": "GITHUB_CLIENT_ID",
        "client_secret_env": "GITHUB_CLIENT_SECRET",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scope": "read:user",
    },
}


def _oauth_request_profile(provider, config, code, callback_url):
    client_id = os.getenv(config["client_id_env"], "").strip()
    client_secret = os.getenv(config["client_secret_env"], "").strip()
    token_response = requests.post(
        config["token_url"],
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback_url,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    token_response.raise_for_status()
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise ValueError(f"{provider} did not return an access token.")

    if provider == "google":
        profile_response = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    elif provider == "facebook":
        profile_response = requests.get(
            "https://graph.facebook.com/v25.0/me",
            params={"fields": "id,name", "access_token": access_token},
            timeout=15,
        )
    else:
        profile_response = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )

    profile_response.raise_for_status()
    profile = profile_response.json()
    provider_user_id = profile.get("sub") if provider == "google" else profile.get("id")
    if not provider_user_id:
        raise ValueError(f"{provider} did not return a user identifier.")

    if provider == "github":
        name = profile.get("name") or profile.get("login")
    else:
        name = profile.get("name")
    return str(provider_user_id), (name or f"{provider.title()} User").strip()


@app.route("/oauth/<provider>")
def oauth_login(provider):
    config = OAUTH_PROVIDERS.get(provider)
    if not config:
        return jsonify({"error": "Unsupported sign-in provider."}), 404

    client_id = os.getenv(config["client_id_env"], "").strip()
    client_secret = os.getenv(config["client_secret_env"], "").strip()
    if not client_id or not client_secret:
        session["auth_error"] = (
            f"{provider.title()} sign-in is not configured. Add its OAuth app credentials "
            "to the server environment."
        )
        return redirect(url_for("loggin"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_provider"] = provider
    callback_url = url_for("oauth_callback", provider=provider, _external=True)
    return redirect(
        config["authorize_url"]
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": callback_url,
                "response_type": "code",
                "scope": config["scope"],
                "state": state,
            }
        )
    )


@app.route("/oauth/<provider>/callback")
def oauth_callback(provider):
    config = OAUTH_PROVIDERS.get(provider)
    expected_state = session.pop("oauth_state", None)
    expected_provider = session.pop("oauth_provider", None)
    if not config or not expected_state or not secrets.compare_digest(
        expected_state, request.args.get("state", "")
    ) or expected_provider != provider:
        session["auth_error"] = "Sign-in expired or could not be verified. Please try again."
        return redirect(url_for("loggin"))
    if request.args.get("error"):
        session["auth_error"] = "Sign-in was cancelled or declined by the provider."
        return redirect(url_for("loggin"))

    code = request.args.get("code")
    if not code:
        session["auth_error"] = "The provider did not return a sign-in code."
        return redirect(url_for("loggin"))

    callback_url = url_for("oauth_callback", provider=provider, _external=True)
    try:
        provider_user_id, name = _oauth_request_profile(
            provider, config, code, callback_url
        )
        identity_digest = hashlib.sha256(
            f"{provider}:{provider_user_id}".encode("utf-8")
        ).hexdigest()
        identity_email = f"oauth+{identity_digest}@oauth.invalid"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_email FROM oauth_accounts WHERE provider = ? AND provider_user_id = ?",
                (provider, provider_user_id),
            )
            account = cursor.fetchone()
            if account:
                identity_email = account[0]
            else:
                cursor.execute(
                    "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
                    (
                        identity_email,
                        name,
                        generate_password_hash(secrets.token_urlsafe(32)),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO oauth_accounts (provider, provider_user_id, user_email)
                    VALUES (?, ?, ?)
                    """,
                    (provider, provider_user_id, identity_email),
                )
            guest_id = session.get("guest_id")
            if guest_id:
                cursor.execute(
                    """
                    UPDATE sessions
                    SET user_email = ?, username = ?, owner_key = ?
                    WHERE owner_key = ? AND user_email IS NULL
                    """,
                    (identity_email, name, identity_email, guest_id),
                )
            conn.commit()
    except (requests.RequestException, ValueError, sqlite3.IntegrityError):
        app.logger.exception("OAuth sign-in failed for provider %s", provider)
        session["auth_error"] = (
            f"Could not complete {provider.title()} sign-in. Please try again."
        )
        return redirect(url_for("loggin"))

    session["user_email"] = identity_email
    session["user_name"] = name
    return redirect(url_for("home"))


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


@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    name = data.get("name", "").strip()
    password = data.get("password", "").strip()

    if not email or not name or not password:
        return jsonify({"error": "Name, email, and password are required."}), 400
    if email.endswith("@oauth.invalid"):
        return jsonify({"error": "Please use a valid email address."}), 400

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            return jsonify(
                {"error": "An account with this email already exists. Please sign in."}
            ), 409
        cursor.execute(
            "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
            (email, name, generate_password_hash(password)),
        )
        guest_id = session.get("guest_id")
        if guest_id:
            cursor.execute(
                """
                UPDATE sessions
                SET user_email = ?, username = ?, owner_key = ?
                WHERE owner_key = ? AND user_email IS NULL
                """,
                (email, name, email, guest_id),
            )
        conn.commit()

    session["user_email"] = email
    session["user_name"] = name

    return jsonify(
        {"message": "Account created successfully!", "user": {"email": email, "name": name}}
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
MAX_IMAGE_PIXELS = 40_000_000


def get_supported_image_extensions():
    """Return image extensions supported by Pillow, including HEIF/AVIF."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            "Image support requires Pillow. Install dependencies from requirements.txt."
        ) from exc

    try:
        import pillow_heif
    except ImportError:
        pass
    else:
        pillow_heif.register_heif_opener()
    return {
        extension
        for extension, image_format in Image.registered_extensions().items()
        if not Image.MIME.get(image_format)
        or Image.MIME[image_format].startswith("image/")
    }


def normalize_uploaded_image(raw):
    """Decode an image and convert it to a broadly supported JPEG or PNG."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ValueError(
            "Image support requires Pillow and pillow-heif. Install dependencies "
            "from requirements.txt."
        ) from exc

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions are too large to process.")
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            has_transparency = (
                "A" in image.getbands() or "transparency" in image.info
            )
            output = io.BytesIO()
            if has_transparency:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime_type = "image/png"
            else:
                image.convert("RGB").save(
                    output, format="JPEG", quality=90, optimize=True
                )
                mime_type = "image/jpeg"
    except UnidentifiedImageError as exc:
        raise ValueError("The uploaded file is not a supported image.") from exc
    except Image.DecompressionBombError as exc:
        raise ValueError("Image dimensions are too large to process.") from exc
    except OSError as exc:
        raise ValueError(f"Could not decode the uploaded image: {exc}") from exc

    image_bytes = output.getvalue()
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("The processed image is too large to send (maximum 12 MB).")
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded_image}"


def extract_uploaded_file(upload):
    """Extract readable text from common office/document formats."""
    filename = Path(upload.filename or "uploaded_file").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(
            "Unsupported file type. Upload PDF, DOCX, XLSX, XLSM, PPTX, CSV, TXT, "
            "MD, JSON, or a supported image format."
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
    image_data_url = ""
    safe_filename = "uploaded_file"
    if upload and getattr(upload, "filename", None):
        safe_filename = Path(upload.filename).name or "uploaded_file"
        suffix = Path(safe_filename).suffix.lower()
        try:
            supported_image_extensions = get_supported_image_extensions()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 503

        is_image_upload = (
            suffix in supported_image_extensions
            or (upload.mimetype or "").lower().startswith("image/")
        )
        if is_image_upload:
            raw_image = upload.read(MAX_UPLOAD_BYTES + 1)
            if len(raw_image) > MAX_UPLOAD_BYTES:
                return jsonify(
                    {"error": "File is too large. Maximum upload size is 12 MB."}
                ), 400
            if not raw_image:
                return jsonify({"error": "The uploaded image is empty."}), 400
            try:
                image_data_url = normalize_uploaded_image(raw_image)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        else:
            try:
                file_context = extract_uploaded_file(upload).strip()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            except Exception as exc:
                app.logger.exception("Error extracting uploaded file '%s'", safe_filename)
                return jsonify(
                    {"error": f"Could not read '{safe_filename}': {exc}"}
                ), 400

        if not is_image_upload and not file_context:
            return jsonify(
                {
                    "error": (
                        f"No readable text was found in '{safe_filename}'. "
                        "Scanned PDFs or image-only files require OCR before uploading."
                    )
                }
            ), 400

    if not user_message and not file_context and not image_data_url:
        return jsonify({"error": "Type a message or attach a readable file."}), 400

    raw_title_source = user_message or f"File: {safe_filename}"

    if file_context:
        prompt_prefix = (
            user_message + "\n\n"
            if user_message
            else f"Please read and analyze '{safe_filename}'."
        )
        user_message = f"{prompt_prefix}[FILE: {safe_filename}]\n{file_context[:100000]}"
    if image_data_url:
        if not user_message:
            user_message = f"Please inspect the attached image '{safe_filename}' and describe it."
        user_message = f"{user_message}\n\n[Attached image: {safe_filename}]"

    web_results = []
    web_search_requested = str(data.get("web_search", "false")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    web_search_enabled = web_search_requested or should_auto_search_web(user_message)
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
        "1. Format every mathematical, scientific, or chemical equation as valid LaTeX inside math delimiters: use $...$ inline and $$...$$ on its own line for display. In chemical formulas, put atom counts in subscripts and use \\rightarrow for reaction arrows; for example, write $$\\mathrm{C_6H_{12}O_6 + 6 O_2 \\rightarrow 6 CO_2 + 6 H_2O}$$, not plain text, bracketed pseudo-math, or malformed commands such as \\text{C}6. Use LaTeX Greek commands (such as \\alpha, \\beta, \\theta, and \\Delta) when a Greek variable or symbol is actually part of the subject, but never force Greek letters into chemical formulas. Do not output bare LaTeX without math delimiters.\n"
        "2. Ensure your answers are complete, detailed, well-structured, and explain steps. Strictly use clear headings, paragraphs, and bullet points.\n"
        "3. When a user uploads a document, spreadsheet, presentation, or text file, base your answer on its extracted contents. State clearly if the file does not contain the requested information. Do not invent missing file contents.\n"
        "4. When live web search results are supplied, use them carefully, cite the supplied URLs as Markdown links, and distinguish search snippets from facts verified by opening a page.\n"
        "5. When providing an image URL, format it as a Markdown image `![description](https://...)` so the interface displays the image directly.\n"
        "6. Do not use tables in your answers.\n"
        "7. Remember the full conversation history in this thread and any previous conversation memory provided below so you can recall user preferences, names, and earlier topics."
    )

    cross_session_memory = (
        fetch_recent_cross_session_memory(owner_key, session_id) if user_email else ""
    )
    if cross_session_memory:
        system_prompt += "\n\n" + cross_session_memory

    litellm_messages = [{"role": "system", "content": system_prompt}]

    if user_email:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
            )
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

            cursor.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, "user", user_message),
            )
            conn.commit()
    else:
        raw_conversation = data.get("conversation", "[]")
        try:
            conversation = (
                json.loads(raw_conversation)
                if isinstance(raw_conversation, str)
                else raw_conversation
            )
        except (TypeError, json.JSONDecodeError):
            return jsonify({"error": "Invalid conversation history."}), 400
        if not isinstance(conversation, list) or any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            for message in conversation
        ):
            return jsonify({"error": "Invalid conversation history."}), 400
        litellm_messages.extend(
            {"role": message["role"], "content": message["content"]}
            for message in conversation[-40:]
        )

    if image_data_url:
        litellm_messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_message},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url},
                    },
                ],
            }
        )
    else:
        litellm_messages.append({"role": "user", "content": user_message})

    config = MODEL_MODES.get(selected_mode, MODEL_MODES["auto"])
    if image_data_url:
        vision_model = "gemini/gemini-2.5-flash"
        configured_models = [vision_model] if has_provider_credentials(vision_model) else []
    else:
        configured_models = [
            model
            for model in [config["primary"], *config["fallbacks"]]
            if has_provider_credentials(model)
        ]
    if not configured_models:
        if image_data_url:
            return jsonify(
                {
                    "error": (
                        "Image questions require GEMINI_API_KEY. Configure it in your "
                        "environment and try again."
                    )
                }
            ), 503
        return jsonify(
            {
                "error": (
                    "No AI provider API keys are configured. Set GEMINI_API_KEY, "
                    "GROQ_API_KEY, or OPENROUTER_API_KEY in your environment."
                )
            }
        ), 503

    primary_model = configured_models[0]
    fallback_models = configured_models[1:]

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

    if user_email:
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
            "history_saved": bool(user_email),
            "web_search_used": web_search_enabled,
            "sources": web_results,
        }
    )


@app.route("/sessions/<email_or_username>", methods=["GET"])
def get_user_sessions(email_or_username):
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"error": "Sign in to access chat history."}), 401

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, title, created_at
            FROM sessions
            WHERE user_email = ? OR owner_key = ?
            ORDER BY created_at DESC
            """,
            (user_email, user_email),
        )
        rows = cursor.fetchall()

    sessions = [{"session_id": r[0], "title": r[1], "created_at": r[2]} for r in rows]
    return jsonify(sessions)


@app.route("/history/<session_id>", methods=["GET"])
def get_session_history(session_id):
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"error": "Sign in to access chat history."}), 401

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT m.role, m.content
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE m.session_id = ? AND (s.user_email = ? OR s.owner_key = ?)
            ORDER BY m.id ASC
            """,
            (session_id, user_email, user_email),
        )
        rows = cursor.fetchall()

    messages = [{"role": r[0], "content": r[1]} for r in rows]
    return jsonify(messages)


@app.route("/session/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"error": "Sign in to manage chat history."}), 401

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND (user_email = ? OR owner_key = ?)",
                (session_id, user_email, user_email),
            )
            if not cursor.fetchone():
                return jsonify({"error": "Chat history not found."}), 404
            cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor.execute(
                "DELETE FROM sessions WHERE session_id = ? AND (user_email = ? OR owner_key = ?)",
                (session_id, user_email, user_email),
            )
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