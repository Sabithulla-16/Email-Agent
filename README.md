# ⚡ Email Agent

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2.10+-E92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Groq Cloud](https://img.shields.io/badge/Groq-gpt--oss--120b%20%7C%20qwen3.8--27b-F55036.svg)](https://groq.com/)
[![Supabase](https://img.shields.io/badge/Supabase-Postgres%20%2B%20RLS-3ECF8E.svg?logo=supabase&logoColor=white)](https://supabase.com/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Webhook%20Bot-26A5E4.svg?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Tests](https://img.shields.io/badge/Tests-79%20Passed-brightgreen.svg)]()
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An intelligent, autonomous, production-grade email assistant. It continuously syncs Gmail inboxes, generates executive daily digests, analyzes message intent using high-throughput Groq LLMs, drafts contextual replies in your personal voice, and delivers an interactive experience via Telegram and a modern REST API.

> 🔒 **CRITICAL INVARIANT:** Email Agent **NEVER auto-sends emails**. All reply drafts require explicit human approval via the Telegram bot or web dashboard before being dispatched through the Gmail API.

---

## 📑 Table of Contents

- [Architecture Overview](#-architecture-overview)
- [Key Features](#-key-features)
- [LLM Engine & Fallback Strategy](#-llm-engine--fallback-strategy)
- [Telegram Bot Experience](#-telegram-bot-experience)
- [Multi-Tenant Security & Supabase RLS](#-multi-tenant-security--supabase-rls)
- [Local Development Setup](#-local-development-setup)
- [Production Deployment (Render)](#-production-deployment-render)
- [API Endpoints Reference](#-api-endpoints-reference)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [License](#-license)

---

## 🏛️ Architecture Overview

```
       +-------------------------------------------------------+
       |                     Google Cloud                      |
       |     Gmail API (Sync & Send)  |  OAuth 2.0 PKCE Flow   |
       +---------------------------+---------------------------+
                                   ^
                                   | (asyncio.to_thread / HTTPS)
                                   v
+-------------------------------------------------------------------------+
|                           Email Agent Backend                           |
|                                                                         |
|  +--------------------+   +---------------------+   +----------------+  |
|  | FastAPI REST API   |   | Background Tasks    |   | Telegram Bot   |  |
|  | - OAuth PKCE flow  |   | - 5-min Scheduler   |   | - Webhook mode |  |
|  | - Sync & Approvals |   | - Sync & Analyze    |   | - Token CBs    |  |
|  | - Cron trigger     |   | - Daily Digests     |   | - Whisper STT  |  |
|  | - Grounded Q&A     |   | - Retention Cleanup |   | - Auto-cleanup |  |
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
    |       Supabase Postgres       |       |          Groq Cloud           |
    | - Multi-tenant RLS Policies   |       | - openai/gpt-oss-120b         |
    | - AES-256-GCM Token Vault     |       | - openai/gpt-oss-20b          |
    | - pgvector HNSW Index         |       | - qwen/qwen3.8-27b (Fallback) |
    | - Atomic CAS Transitions      |       | - whisper-large-v3-turbo (STT)|
    +-------------------------------+       +-------------------------------+
```

---

## ✨ Key Features

1. **Non-Disruptive Gmail Sync:**
   - Initial lookback sync paired with incremental `history.list` synchronization.
   - Synchronous Google API SDK calls wrapped safely in `asyncio.to_thread`.
   - MIME parser, HTML-to-text converter, quote and signature stripper, and attachment metadata extraction.
   - OTP and 2FA verification code pre-filtering protects sensitive authentication tokens from ever reaching LLMs.

2. **Executive Daily Digest:**
   - Synthesizes all received emails into a clean executive summary per email (Sender, Subject, 1-sentence digest).
   - Delivered automatically at the user's scheduled local time (accounting for IANA timezone & DST).
   - Triggerable on-demand anytime via `/digest`.

3. **Contextual Reply Drafting:**
   - Detects when an email necessitates a response and drafts intelligent replies.
   - Tone adjustments available at the touch of a button: *Shorter*, *Formal*, *Friendly*, or *Firm*.
   - Revision instruction flow via typed chat messages or voice notes.

4. **Natural Language Q&A (`/ask`):**
   - Grounded strictly in the continuously updated digested email catalog.
   - Semantic reasoning without brittle stopword filters or keyword substring tricks.
   - Zero hallucinations: explicitly reports when no emails match your topic, omitting phantom citations.

5. **Extensible Productivity Engines:**
   - **Rules Engine:** Multi-stage trust ladder (`suggest`, `act_notify`, `auto`).
   - **Meeting Detection:** Identifies calendar requests and generates 1-click Google Calendar scheduling links.
   - **Commitment Tracking:** Extracts promises made in outgoing emails (e.g., *"I will send the slides tomorrow"*).
   - **Domain Extractors:** Detects bills/invoices, flight/hotel bookings, and scans for phishing indicators.

---

## 🤖 LLM Engine & Fallback Strategy

The application leverages Groq's high-speed inference engine with a resilient automatic fallback chain to ensure high availability and rate-limit immunity:

| Capability | Primary Model | Fallback Models |
| :--- | :--- | :--- |
| **Email Analysis & Classification** | `openai/gpt-oss-120b` | `qwen/qwen3.8-27b` $\to$ `openai/gpt-oss-20b` |
| **Contextual Reply Drafting** | `openai/gpt-oss-20b` | `openai/gpt-oss-120b` $\to$ `qwen/qwen3.8-27b` |
| **Grounded Inbox Q&A** | `openai/gpt-oss-120b` | `qwen/qwen3.8-27b` $\to$ `openai/gpt-oss-20b` |
| **Daily Digest Summaries** | `openai/gpt-oss-120b` | `qwen/qwen3.8-27b` $\to$ `openai/gpt-oss-20b` |
| **Voice Note Transcription** | `whisper-large-v3-turbo` | — |

- **Resilience:** If HTTP 429 (Rate Limit) or capacity errors occur on the primary model, calls automatically route to secondary fallback models without dropping requests or crashing the server.
- **Prompt Injection Defense:** Strict `<untrusted_email_content>` boundaries ensure prompt instructions cannot be overridden by untrusted email text.

---

## 📱 Telegram Bot Experience

The Telegram bot acts as an executive command center:

```
User: /start
Bot:  👋 Welcome back, Valtry!
      Your Telegram is linked to valtryfreefire@gmail.com.

      Quick Actions:
      • /pending — Review drafts awaiting approval
      • /sync — Pull new emails immediately
      • /digest — Generate your daily digest
      • /urgent — View urgent unread emails
      • /help — View all commands
```

### Supported Commands
- `/start` — Smart onboarding (presents *Connect Gmail* button for new users, or status overview for connected users).
- `/pending` — Displays pending drafts with 1-click inline buttons: `✅ Approve & Send`, `✍️ Revise`, `❌ Reject`, `⚡ Shorter`, `👔 Formal`, `🤝 Friendly`, `💼 Firm`.
- `/sync` — Manually forces an immediate Gmail synchronization cycle.
- `/digest` — Compiles and delivers the daily executive email summary.
- `/urgent` — Filters high-priority unread items.
- `/ask <question>` — Queries your email archive in plain English with grounded citations.
- `/snooze <hours>` — Temporarily mutes incoming draft notifications (e.g., `/snooze 4`).
- `/mute` — Silences notifications for 24 hours.
- `/settings` — Inspects notification mode, quiet hours, and channel configuration.
- `/disconnect` — Unlinks the Telegram chat from your account.

### Polished UX Enhancements
- **Dynamic Indicator Cleanup:** Loading notices (*"Searching your emails..."*, *"Synchronizing inbox..."*, *"Compiling digest..."*) are automatically deleted once the final response is delivered.
- **In-App Browser Auto-Close:** Completing Google OAuth inside Telegram's browser displays a 3-second countdown and closes the tab cleanly via `Telegram.WebApp.close()`, returning you straight to the chat.
- **Voice Note Revision:** Send an audio note with feedback (e.g. *"Make it more polite and mention Friday at 2 PM"*); the bot transcribes it via Whisper and regenerates the draft.

---

## 🛡️ Multi-Tenant Security & Supabase RLS

1. **Encrypted Token Vault:**
   - OAuth refresh tokens are encrypted at rest using **AES-256-GCM** with unique 12-byte initialization vectors (IV) per record.
   - Supported by versioned encryption keys (`TOKEN_KEY_VERSION`) for zero-downtime key rotation.

2. **Per-User Isolation:**
   - Every email, draft, analysis, rule, and preference record is strictly keyed to a `user_id`.
   - Before syncing, the system acquires an atomic per-user lock in Postgres (`acquire_sync_lock`), preventing race conditions.

3. **Row-Level Security (RLS) on Supabase:**
   - All 22 tables enforce RLS.
   - Implemented using optimized `(SELECT auth.uid())` subqueries to avoid per-row function evaluation overhead.
   - Standard user and anonymous roles have zero unauthorized read/write access.

---

## 🚀 Local Development Setup

### Prerequisites
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) package manager
- Redis 7+
- Supabase project credentials
- Google Cloud OAuth credentials (with Gmail scopes: `https://www.googleapis.com/auth/gmail.modify`)
- Groq Cloud API key
- Telegram Bot token from [@BotFather](https://t.me/BotFather)

### 1. Installation
```bash
git clone https://github.com/Sabithulla-16/Email-Agent.git
cd Email-Agent

# Create virtual environment and install all dependencies
uv sync
```

### 2. Environment Configuration
Copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### 3. Run Database Migrations
Apply the initial schema and RLS policies in your Supabase SQL Editor:
- `supabase/migrations/20261124000000_initial_schema.sql`
- `supabase/migrations/20261124000001_rls_policies.sql`

### 4. Start Services
```bash
# Terminal 1: Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Terminal 2: Start API Server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 3: Start Background Worker
uv run arq app.queue.worker.WorkerSettings
```

---

## ☁️ Production Deployment (Render)

This repository includes a native [render.yaml](render.yaml) blueprint.

### Option 1: 1-Click Blueprint (Recommended)
1. In the [Render Dashboard](https://dashboard.render.com), click **New +** > **Blueprint**.
2. Select your `Email-Agent` repository.
3. Render will provision:
   - **`email-agent-api`**: FastAPI Web Service.
   - **`email-agent-worker`**: ARQ Background Worker.
   - **`email-agent-redis`**: Key-Value Redis store.
4. Supply your secret environment variables when prompted and click **Apply**.

### Option 2: Free-Tier Deployment (Web Service Only)
If deploying on Render's free tier without a paid background worker:
- Deploy the Web Service alone.
- Set `ENVIRONMENT=production`.
- The web service includes a built-in background scheduler that runs sync, analysis, and digests automatically.
- Alternatively, ping `/api/v1/cron/trigger` every 5 minutes from an external uptime service (e.g. Cron-Job.org, UptimeKuma).

---

## 📡 API Endpoints Reference

| Method | Endpoint | Description | Auth Required |
| :--- | :--- | :--- | :--- |
| `GET` | `/healthz` | Liveness health check | No |
| `GET` | `/readyz` | Readiness probe (verifies Supabase & Redis connectivity) | No |
| `POST`| `/api/v1/cron/trigger` | Webhook endpoint for external cron schedulers | No |
| `GET` | `/api/v1/auth/google/start`| Initiates Google OAuth 2.0 PKCE flow | No |
| `GET` | `/api/v1/auth/google/callback`| Handles Google OAuth callback and session creation | No |
| `GET` | `/api/v1/auth/me` | Fetches authenticated user profile | Yes |
| `POST`| `/api/v1/auth/logout` | Revokes user session and clears cookies | Yes |
| `POST`| `/api/v1/emails/sync` | Triggers immediate Gmail inbox sync | Yes |
| `GET` | `/api/v1/emails` | Lists synced emails (metadata projection) | Yes |
| `GET` | `/api/v1/emails/{id}` | Retrieves full email content and LLM analysis | Yes |
| `GET` | `/api/v1/drafts` | Lists pending and processed reply drafts | Yes |
| `GET` | `/api/v1/drafts/{id}` | Retrieves draft and complete version history | Yes |
| `POST`| `/api/v1/drafts/{id}/approve` | **Approves and sends draft via Gmail API** | Yes |
| `POST`| `/api/v1/drafts/{id}/reject` | Rejects and archives a draft | Yes |
| `POST`| `/api/v1/drafts/{id}/revise` | Revises draft text via Groq LLM (prompt or tone preset) | Yes |
| `POST`| `/api/v1/drafts/{id}/edit` | Manually overwrites draft content | Yes |
| `POST`| `/api/v1/telegram/webhook` | Telegram Bot webhook receiver | Secret Header |
| `GET` | `/api/v1/features/qa` | Inbox Q&A with grounded email citations | Yes |
| `GET` | `/api/v1/features/rules` | Lists automated inbox rules | Yes |
| `POST`| `/api/v1/features/rules` | Creates a new automation rule | Yes |
| `GET` | `/api/v1/features/smart-actions/{id}` | Extracts meetings & calendar URLs | Yes |
| `GET` | `/api/v1/features/extractors/{id}` | Extracts bills, travel details, and phishing warnings | Yes |

---

## 🧪 Testing & Quality Assurance

The codebase maintains a 100% test pass rate across unit, integration, and security test suites:

```bash
# Run all 79 test cases
uv run pytest

# Check code formatting & linting with Ruff
uv run ruff check .

# Apply auto-fixes
uv run ruff check . --fix
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
