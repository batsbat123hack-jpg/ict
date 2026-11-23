# Bats AI — updated project

## File understanding added
The chat accepts PDF, DOCX, XLSX/XLSM, PPTX, TXT, Markdown, CSV, JSON, and image uploads. Images supported by Pillow—including common JPEG, PNG, GIF, WebP, BMP, TIFF, HEIC/HEIF and AVIF formats—are converted to JPEG or PNG before being sent to the vision model. Camera RAW formats may not be supported. The backend extracts document text/table contents and sends that context to the selected AI model, so you can ask questions, request summaries, calculations, comparisons and explanations based on the uploaded file.

**Limitations:** scanned/image-only PDFs need OCR before their text can be read. Password-protected or damaged files may fail. Each upload is limited to 12 MB, and only extracted text (up to 100,000 characters) is sent to the model.

## Setup (Windows PowerShell)
1. Extract the ZIP and open the folder in VS Code.
2. Create a virtual environment: `py -m venv venv`
3. Activate it: `.\venv\Scripts\Activate.ps1`
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python ai.py`
6. Open `http://127.0.0.1:5000`.

## API keys and free usage
The existing API-key configuration and original key values have been retained in `ai.py` as requested. Providers can change quotas, impose rate limits, or charge for usage. Therefore, no one can guarantee unlimited free access through these external AI APIs. The app will try configured fallback providers when one provider is unavailable, but all providers must have active quota and valid credentials.

**Security warning:** the keys in the original uploaded source were exposed in this chat's file-processing workflow. Rotate/revoke them with their providers before public deployment. For safer operation, set replacement values in environment variables; environment variables override the defaults. Never commit `.env`, API keys, or the SQLite database to GitHub.

## Other notes
- Email/password accounts are created immediately and do not require email verification.
- Social sign-in is available for Google, Facebook, and GitHub. Register OAuth apps and set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET`, and `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in the server environment. Set each provider's callback URL to `https://your-domain/oauth/<provider>/callback` (replace `<provider>` with `google`, `facebook`, or `github`). Keep these credentials private. ChatGPT sign-in is not offered because ChatGPT does not provide an identity-provider flow for third-party apps.
- Chat history is saved only for signed-in accounts. Guest chats stay in browser memory until a new chat or page refresh.
- Image generation and other provider features may incur provider charges or have free-tier limits.
- To generate images, configure `GEMINI_API_KEY`, then choose **Create image** from the **+** menu, describe the image, and send. The optional `GEMINI_IMAGE_MODEL` setting defaults to `gemini-nano-banana-2.1`. Gemini free-tier access and quotas vary by account and can change; prompts are sent to Google. Generated images can be downloaded from the chat.
- Embedded YouTube videos depend on the video owner's embed permissions.


## Free live web search (SearXNG)

Bats AI can search the live web through a self-hosted SearXNG instance. This avoids a paid search API subscription, but it is not unlimited or guaranteed: search engines may rate-limit or block requests, and you need Docker running.

1. Install Docker Desktop and start it.
2. Open a terminal in this project folder.
3. Start the search service: `docker compose up -d searxng`.
4. Install the Python packages: `pip install -r requirements.txt`.
5. Copy `.env.example` to `.env` and set `SEARXNG_URL=http://127.0.0.1:8080`. Keep `.env` private.
6. Start Bats AI with `python ai.py`.
7. Tick **Web** beside the message box when you want a live search. Untick it for ordinary AI chat.

If the Web switch reports that search is unavailable, check Docker Desktop and run `docker compose logs searxng` to inspect the SearXNG service. If deploying Bats AI to a hosting provider, set `SEARXNG_URL` to the reachable SearXNG service address; `127.0.0.1` only works when both services share the same machine/network namespace.

SearXNG search results are search-engine results/snippets, not proof that every linked page was fully opened or verified.
