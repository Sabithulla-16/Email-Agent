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

## Deployment on Render

This repository includes a production-ready Render Blueprint ([render.yaml](render.yaml)) defining all three interconnected services in the same region (`oregon`):
1. **Web Service (`email-agent-api`)**: FastAPI API, Google OAuth endpoints, and Telegram webhook receiver.
2. **Background Worker (`email-agent-worker`)**: ARQ worker processing 5-minute cron inbox synchronization, Groq LLM analyses, draft generation, and daily digests.
3. **Key Value Store (`email-agent-redis`)**: Managed Redis instance providing ARQ job queueing, update deduplication, and rate limiting.

---

### Method A: 1-Click Blueprint Deployment (Recommended)

1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** > **Blueprint**.
3. Connect your GitHub account and select the **`Sabithulla-16/Email-Agent`** repository.
4. Render will parse `render.yaml` and display the three resources:
   - `email-agent-api` (Web Service)
   - `email-agent-worker` (Background Worker)
   - `email-agent-redis` (Key Value Store)
5. Fill in the prompted secret values (marked `sync: false` in `render.yaml`).
6. Click **Apply**. Render will provision Redis, build both Python services using `uv`, wire the internal `REDIS_URL`, and deploy!

---

### Method B: Manual Service-by-Service Configuration

If you prefer provisioning each service individually via the Render UI:

#### Service 1: Key Value (Redis)
1. In Render Dashboard, click **New +** > **Key Value**.
2. **Name**: `email-agent-redis`
3. **Region**: `Oregon (US West)` *(ensure all services share the exact same region)*.
4. **Plan**: `Starter` (or `Free` if available).
5. **Access Control**: Keep empty `[]` to restrict access to Render's internal private network.
6. Click **Create Key Value**. Note the **Internal Connection String** (format: `redis://...`).

#### Service 2: Web Service (FastAPI)
1. Click **New +** > **Web Service**.
2. Connect `Sabithulla-16/Email-Agent`.
3. **Name**: `email-agent-api`
4. **Region**: `Oregon (US West)` *(matches Key Value)*
5. **Runtime**: `Python 3`
6. **Build Command**: `pip install uv && uv sync --frozen --no-dev`
7. **Start Command**: `uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT`
8. **Health Check Path**: `/healthz`
9. **Environment Variables**: Add the variables listed in the checklist below. Note: Set `REDIS_URL` to the Internal Connection String of `email-agent-redis`.
10. Click **Create Web Service**. Once deployed, copy your public service domain (e.g. `https://email-agent-api.onrender.com`).

#### Service 3: Background Worker (ARQ)
1. Click **New +** > **Background Worker**.
2. Connect `Sabithulla-16/Email-Agent`.
3. **Name**: `email-agent-worker`
4. **Region**: `Oregon (US West)` *(matches Key Value)*
5. **Runtime**: `Python 3`
6. **Build Command**: `pip install uv && uv sync --frozen --no-dev`
7. **Start Command**: `uv run arq app.queue.worker.WorkerSettings`
8. **Environment Variables**: Add the variables listed in the checklist below.
9. Click **Create Background Worker**.

---

### Environment Variables Checklist

The application enforces **fail-fast startup validation**. In `production` mode, the server will immediately terminate on boot with a clear error report if any required variable is missing or set to a placeholder.

| Variable | Required For | Description | Example / Recommended Value |
| :--- | :--- | :--- | :--- |
| `ENVIRONMENT` | Web & Worker | Deployment environment | `production` |
| `PUBLIC_BASE_URL` | Web & Worker | Public URL of Web Service (no trailing slash) | `https://email-agent-api.onrender.com` |
| `PORT` | Web | Port dynamically assigned by Render | `8000` (Render overrides automatically via `$PORT`) |
| `PYTHON_VERSION` | Web & Worker | Pinned Python runtime version | `3.12.0` |
| `UV_PYTHON_DOWNLOADS` | Web & Worker | Use Render's system Python runtime | `never` |
| `TELEGRAM_MODE` | Web | Operational mode for bot updates | `webhook` |
| `TELEGRAM_BOT_TOKEN` | Web & Worker | Bot token issued by [@BotFather](https://t.me/BotFather) | `123456789:ABCdefGhIJK...` |
| `TELEGRAM_BOT_USERNAME` | Web | Username of your Telegram bot | `YourEmailAgentBot` |
| `TELEGRAM_WEBHOOK_SECRET`| Web | 32+ char secret for `X-Telegram-Bot-Api-Secret-Token` | Random urlsafe/hex string |
| `SUPABASE_URL` | Web & Worker | Supabase project HTTPS URL | `https://<ref>.supabase.co` |
| `SUPABASE_SECRET_KEY` | Web & Worker | Supabase Service Role Secret Key (bypasses RLS) | Real Supabase service role secret |
| `APP_SECRET_KEY` | Web & Worker | 32+ char key for session cookies and CSRF tokens | Random 32+ character hex string |
| `TOKEN_ENCRYPTION_KEY` | Web & Worker | Exact 32-byte key for AES-256-GCM token vault | Exact 32 alphanumeric bytes or hex string |
| `GROQ_API_KEY` | Web & Worker | Groq Cloud API Key | Real Groq API key |
| `GOOGLE_CLIENT_ID` | Web & Worker | Google Cloud OAuth 2.0 Client ID | `...apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Web & Worker | Google Cloud OAuth 2.0 Client Secret | Real Google OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Web & Worker | Public OAuth callback URL | `https://email-agent-api.onrender.com/api/v1/auth/google/callback` |
| `REDIS_URL` | Web & Worker | Internal connection URL to Key Value | Handled automatically via `render.yaml` service reference |
| `SENTRY_DSN` | Web & Worker | (Optional) Sentry error monitoring DSN | `https://...` |

---

### Post-Deployment Checklist

1. **Google Cloud Console**:
   - Go to [Google Cloud Console Credentials](https://console.cloud.google.com/apis/credentials).
   - Under your OAuth 2.0 Client ID, add `https://<your-render-domain>/api/v1/auth/google/callback` to **Authorized redirect URIs**.
2. **Verify Telegram Webhook**:
   - On deployment, `app/main.py` automatically registers the webhook with Telegram.
   - Verify health by checking `https://<your-render-domain>/healthz` or running:
     ```bash
     python scripts/webhook_info.py
     ```
3. **Connect Gmail in Telegram**:
   - Send `/start` to your Telegram bot.
   - Tap **⟡ Connect Gmail** to initiate OAuth and authorize your Gmail inbox!

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
