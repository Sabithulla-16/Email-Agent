# Email Agent Backend

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2.10+-E92063.svg)](https://docs.pydantic.dev/)
[![Groq](https://img.shields.io/badge/Groq-Llama%203.3%20%7C%20Whisper-f55036.svg)](https://groq.com/)
[![Supabase](https://img.shields.io/badge/Supabase-Async%20Client-3ECF8E.svg)](https://supabase.com/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](http://mypy-lang.org/)

Production-grade, asynchronous backend service for **Email Agent**. The system synchronizes user inboxes via Gmail API, analyzes messages using high-throughput Groq LLMs, drafts replies for human approval, and lets users approve, reject, or revise drafts from a modern web interface or Telegram.

> **CRITICAL INVARIANT:** Email Agent **NEVER auto-sends emails**. Reply drafts require explicit human approval via the web UI or Telegram before being dispatched via Gmail API.

---

## Architecture Overview

```
       +-------------------------------------------------------+
       |                     Google APIs                       |
       |     Gmail API (Sync & Send)  |  OAuth 2.0 PKCE Flow   |
       +---------------------------+---------------------------+
                                   ^
                                   | (asyncio.to_thread / HTTPS)
                                   v
+-------------------------------------------------------------------------+
|                           Email Agent Backend                           |
|                                                                         |
|  +--------------------+   +---------------------+   +----------------+  |
|  | FastAPI REST API   |   | ARQ / Redis Worker  |   | Telegram Bot   |  |
|  | - Auth (Sessions)  |   | - 5-min Scheduler   |   | - Webhook mode |  |
|  | - Gmail Sync Trg   |   | - Sync & Analyze    |   | - Token CBs    |  |
|  | - Draft Approvals  |   | - Daily Digests     |   | - Whisper STT  |  |
|  | - Q&A & Features   |   | - Retention Cleanup |   | - Rate limiter |  |
|  +---------+----------+   +----------+----------+   +-------+--------+  |
|            |                         |                      |           |
|            +-------------------------+----------------------+           |
|                                      |                                  |
|                                      v                                  |
|                      +-------------------------------+                  |
|                      | BaseRepo / Async Supabase     |                  |
|                      |  - HTTPS PostgREST Client     |                  |
|                      |  - Atomic RPC Stored Procs    |                  |
|                      +---------------+---------------+                  |
+--------------------------------------|----------------------------------+
                                       |
                   +-------------------+-------------------+
                   |                                       |
                   v                                       v
    +-------------------------------+       +-------------------------------+
    |         Supabase Postgres     |       |          Groq Cloud           |
    | - RLS Default Deny on all     |       | - Llama 3.1 8B (Analysis)     |
    | - AES-256-GCM Token Vault     |       | - Llama 3.3 70B (Drafting)    |
    | - pgvector HNSW Index         |       | - Whisper Large v3 (Voice)    |
    | - Atomic CAS Transitions      |       +-------------------------------+
    +-------------------------------+
```

---

## Key Highlights

- **Python 3.12 & Strict Mypy:** Fully typed codebase with Pydantic v2 domain schemas for every database entity.
- **Supabase-Only Database Access:** Strict access over HTTPS via official `supabase-py` async client and `SUPABASE_SECRET_KEY`. No direct Postgres connection strings, SQLAlchemy, or Alembic.
- **Row Level Security (RLS):** Enabled on every table with zero public policies (default deny for `anon`/`authenticated`).
- **AES-256-GCM Token Vault:** Google OAuth refresh tokens are encrypted with unique 12-byte IVs, stored as Base64 strings, with multi-version key rotation support.
- **Groq LLM Pipeline:**
  - `llama-3.1-8b-instant`: Fast classification, summaries, and action item extraction.
  - `llama-3.3-70b-versatile`: Context-aware draft generation and tone revision loop (`shorter`, `formal`, `friendly`, `firm`).
  - `whisper-large-v3-turbo`: Audio transcription for Telegram voice notes.
  - Prompt injection defense using strict `<untrusted_email_content>` delimiters.
  - OTP / Security pre-filtering that protects 2FA verification codes from reaching external models.
- **Gmail Sync Engine:**
  - Initial lookback sync + incremental `history.list` synchronization.
  - MIME parser, HTML to text cleaner, quote and signature stripper, attachment metadata extractor.
  - Synchronous `google-api-python-client` calls run safely in `asyncio.to_thread`.
- **Telegram Bot Integration:**
  - Webhook mode only with constant-time secret token verification.
  - Single-use callback tokens for inline keyboard buttons (capped < 64 bytes).
  - Outgoing token-bucket rate limiter (~1 msg/sec per chat, ~30 msg/sec global) and 403 bot-blocked handling.
- **ARQ & Redis Scheduler:**
  - Periodic 5-minute cron job calls atomic SQL RPC `claim_due_users` (`FOR UPDATE SKIP LOCKED`).
  - Enqueues deterministic per-user jobs (`sync_{user_id}_{hour}`, `digest_{user_id}_{date}`).
  - Computes `next_digest_at` per user's IANA timezone (`zoneinfo`), handling DST shifts.
- **Feature-Flagged Capabilities:**
  - Rules Engine with Trust Ladder (`suggest`, `act_notify`, `auto`).
  - Smart Actions: Meeting detection & 1-click Google Calendar URL generation.
  - Follow-up & Commitment tracking from outgoing messages.
  - Inbox Q&A with grounded email citations.
  - Domain Extractors: Bills/invoices, travel confirmations, and phishing warnings.

---

## Quickstart

### Prerequisites
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) (Extremely fast Python package installer and resolver)
- Redis 7+
- Supabase Project (URL and Secret Key)
- Groq Cloud API Key
- Google Cloud OAuth 2.0 Credentials (with Gmail scopes)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/Sabithulla-16/Email-Agent.git
cd Email-Agent

# Create virtual environment and install dependencies
uv venv --python 3.12
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv sync
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

### 3. Apply Database Migrations
Apply the initial Postgres migration:
- **Via Supabase CLI:**
  ```bash
  supabase db push
  ```
- **Via Supabase Dashboard:** Copy `supabase/migrations/20261124000000_initial_schema.sql` into the Supabase SQL Editor and execute it. Then execute `NOTIFY pgrst, 'reload schema';`.

### 4. Run the API and Background Worker
```bash
# Terminal 1: Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Terminal 2: Start API Server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 3: Start ARQ Worker
uv run arq app.queue.worker.WorkerSettings
```

### 5. Run via Docker Compose
```bash
docker-compose up -d --build
```

---

## Deployment on Render (Webhook Mode)

To deploy the backend to [Render](https://render.com) (or any containerized hosting platform):

### 1. Build & Start Commands
- **Environment:** Python 3.12 (or Docker using `Dockerfile`)
- **Build Command:** `uv sync --frozen --no-dev`
- **Start Command:** `uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### 2. Environment Variables Checklist
Configure the following environment variables in your Render Web Service dashboard:

| Variable | Description | Example / Required |
| :--- | :--- | :--- |
| `ENVIRONMENT` | Deployment environment | `production` |
| `PUBLIC_BASE_URL` | Public Render domain (no trailing slash) | `https://email-agent-backend.onrender.com` |
| `TELEGRAM_MODE` | Bot operational mode (`webhook` or `polling`) | `webhook` |
| `TELEGRAM_BOT_TOKEN` | Bot API token from [@BotFather](https://t.me/BotFather) | `123456789:ABC...` |
| `TELEGRAM_BOT_USERNAME` | Username of your Telegram bot | `YourEmailAgentBot` |
| `TELEGRAM_WEBHOOK_SECRET` | 32+ character random secret string | Generated token (letters, digits, `_`, `-`) |
| `SUPABASE_URL` | Supabase project HTTPS URL | `https://xyzcompany.supabase.co` |
| `SUPABASE_SECRET_KEY` | Supabase Service Role / Secret Key | `your_supabase_secret_key` |
| `APP_SECRET_KEY` | Min 32-char key for session & state HMAC | Random hex or urlsafe string |
| `TOKEN_ENCRYPTION_KEY` | Exact 32-byte key for AES-256-GCM vault | 32 alphanumeric bytes or hex string |
| `GROQ_API_KEY` | Groq Cloud API Key | `your_groq_api_key` |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth 2.0 Client ID | `...apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Google Cloud OAuth 2.0 Client Secret | `your_google_client_secret` |
| `GOOGLE_REDIRECT_URI` | OAuth callback URL matching Google Console | `https://email-agent-backend.onrender.com/api/v1/auth/google/callback` |
| `REDIS_URL` | Redis instance connection string | `redis://red-xxx:6379` (e.g. Render Redis) |

### 3. Automated Webhook Setup
When `TELEGRAM_MODE=webhook`, the backend automatically:
1. Calls Telegram's `setWebhook` on startup with:
   - `url = PUBLIC_BASE_URL + "/api/v1/telegram/webhook"`
   - `secret_token = TELEGRAM_WEBHOOK_SECRET`
2. Validates registration with `getWebhookInfo` and logs pending updates without leaking tokens.
3. Serves the webhook on `POST /api/v1/telegram/webhook`, verifying `X-Telegram-Bot-Api-Secret-Token` via constant-time comparison, deduplicating updates in Redis, and executing handling in the background.

### 4. Manual Fallback Scripts
If you ever need to inspect or reset webhooks manually:
```bash
# Check current webhook status, URL, pending count, and last error
python scripts/webhook_info.py

# Manually configure the webhook
python scripts/set_webhook.py
```

---

## API Endpoints Reference

| Method | Endpoint | Description | Auth Required |
| :--- | :--- | :--- | :--- |
| `GET` | `/healthz` | Kubernetes liveness probe | No |
| `GET` | `/readyz` | Kubernetes readiness probe (checks Supabase & Redis) | No |
| `GET` | `/api/v1/auth/google/start` | Initiates Google OAuth 2.0 PKCE flow | No |
| `GET` | `/api/v1/auth/google/callback` | Handles OAuth callback, verifies ID token, sets session | No |
| `GET` | `/api/v1/auth/me` | Returns authenticated user profile | Yes |
| `POST`| `/api/v1/auth/logout` | Revokes current session | Yes |
| `POST`| `/api/v1/emails/sync` | Manually triggers Gmail synchronization | Yes |
| `GET` | `/api/v1/emails` | Lists synced emails (excludes heavy `body_clean`) | Yes |
| `GET` | `/api/v1/emails/{id}` | Retrieves full email details and analysis | Yes |
| `GET` | `/api/v1/drafts` | Lists pending/processed drafts | Yes |
| `GET` | `/api/v1/drafts/{id}` | Retrieves draft and version history | Yes |
| `POST`| `/api/v1/drafts/{id}/approve` | **Approves and sends draft via Gmail API** | Yes |
| `POST`| `/api/v1/drafts/{id}/reject` | Rejects and cancels a draft | Yes |
| `POST`| `/api/v1/drafts/{id}/revise` | Revises draft using Groq LLM (instructions / tone presets) | Yes |
| `POST`| `/api/v1/drafts/{id}/edit` | Manually updates draft body | Yes |
| `POST`| `/api/v1/telegram/link` | Generates deep link for Telegram bot account linking | Yes |
| `GET` | `/api/v1/telegram/status` | Checks user Telegram connection status | Yes |
| `POST`| `/api/v1/telegram/webhook` | Telegram Bot API webhook receiver | Secret Header |
| `GET` | `/api/v1/features/qa` | Inbox Q&A with grounded email citations | Yes |
| `GET` | `/api/v1/features/rules` | Lists automation rules | Yes |
| `POST`| `/api/v1/features/rules` | Creates automation rule | Yes |
| `GET` | `/api/v1/features/smart-actions/{id}` | Meeting detection and Google Calendar link | Yes |
| `GET` | `/api/v1/features/extractors/{id}` | Bills, travel, and phishing warnings | Yes |

---

## Testing & Quality Assurance

```bash
# Run complete test suite (70 unit & integration tests)
uv run pytest -v

# Run linting with Ruff
uv run ruff check app tests scripts

# Run strict type checking with Mypy
uv run mypy app

# Run dependency vulnerability audit
uv run pip-audit
```

---

## Documentation Links

- [Architecture & Design Details](docs/ARCHITECTURE.md)
- [Security Model & OWASP Review](docs/SECURITY.md)
- [Operational Runbook & Troubleshooting](docs/RUNBOOK.md)
- [Architectural Decision Records (ADRs)](docs/DECISIONS.md)

---

## License
MIT License.
