# Email Agent Architecture & Design

## 1. System Overview

**Email Agent** is an enterprise-grade autonomous assistant that synchronizes an authenticated user's Gmail inbox, performs real-time classification and action item extraction using high-speed Groq LLMs (`llama-3.1-8b-instant`), generates proposed email replies using reasoning-focused Groq LLMs (`llama-3.3-70b-versatile`), and coordinates human-in-the-loop review through both a web application and Telegram.

### Core Invariant
> **The service NEVER auto-sends an email.** An email draft is only dispatched to Google's Gmail API when the human user explicitly approves it via the web interface or Telegram inline button.

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

## 2. Component Design

### 2.1 API Layer (FastAPI & Pydantic v2)
- Built on Python 3.12 with strict typing (mypy configured with `disallow_untyped_defs = true`).
- Uniform error handling converting domain exceptions (`NotFoundError`, `ConflictError`, `ForbiddenError`, `ValidationError`) into standardized `{code, message, request_id}` JSON responses.
- Security middleware enforcing `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Strict-Transport-Security`, and double-submit CSRF cookie protection on state-changing endpoints.
- High-performance projections: list views strictly omit heavy text bodies (e.g. `EmailListItem` omits `body_clean`).

### 2.2 Database Layer (Official Async Supabase Client)
- All database operations route strictly over HTTPS via `create_async_client(SUPABASE_URL, SUPABASE_SECRET_KEY)`.
- No direct Postgres connection strings, SQLAlchemy, or Alembic are used.
- Atomic multi-statement transactions are encapsulated in `SECURITY INVOKER` SQL functions in `supabase/migrations/`:
  - `claim_due_users(limit)`: `FOR UPDATE SKIP LOCKED` row locking for scheduler workers.
  - `acquire_sync_lock(user_id, ttl)` & `release_sync_lock(user_id)`: distributed sync concurrency control.
  - `transition_draft(draft_id, from_status, to_status, ...)`: atomic compare-and-set state transitions with audit logging.
  - `create_draft_version(draft_id, body, ...)`: atomic version incrementing.
  - `match_emails(user_id, query_embedding, k)`: pgvector cosine similarity search.
  - `delete_user_data(user_id)`: GDPR / Right-to-be-Forgotten data deletion.
- Single-statement atomic operations use filtered updates with length verification (`len(data) == 1`) to consume single-use tokens (`callback_tokens`, `telegram_link_tokens`).

### 2.3 Gmail Sync & Preprocessing Engine
- Synchronous `google-api-python-client` calls are isolated inside `asyncio.to_thread` to maintain event-loop throughput.
- Two-tier synchronization:
  - **Initial Sync:** Lookback window (default 3 days) fetching message IDs, thread IDs, and metadata.
  - **Incremental Sync:** Uses Gmail `users.history.list` tracked in `sync_state.last_history_id`. If history expires (HTTP 404), smoothly falls back to partial sync.
- Preprocessing Pipeline (`app/services/email_cleaner.py`):
  - Strips HTML tags using `html.parser` while preserving structure.
  - Strips quoted email replies (`On ... wrote:`, `> ...`) and signature blocks (`--`, `Best regards,`).
  - Caps body length at 15,000 characters to prevent LLM token flooding.
  - Identifies bulk emails and newsletters via `List-Unsubscribe` headers or `Precedence: bulk`.
  - Sensitive data redactor (`app/services/redactor.py`) masks credit card numbers and pre-filters 2FA / OTP verification codes before reaching the LLM.

### 2.4 Groq LLM Intelligence Pipeline
- **Fast Analysis Model (`llama-3.1-8b-instant`):** Classifies emails into categories (`work`, `personal`, `finance`, `security`, `promo`, `travel`), assigns priorities, generates a 2-sentence summary, extracts action items, and flags risks.
- **Drafting & Revision Model (`llama-3.3-70b-versatile`):** Generates context-aware reply drafts adhering to user style, tone presets (`shorter`, `formal`, `friendly`, `firm`), and custom revision prompts.
- **Whisper Voice Transcription (`whisper-large-v3-turbo`):** Transcribes Telegram voice notes directly into revision instructions.
- **Prompt Injection Defense:** Untrusted email text is wrapped inside strict `<untrusted_email_content>` delimiters with adversarial override instructions ignored.

### 2.5 Telegram Bot & Notification Service
- **Webhook Mode Only:** Listens on `POST /api/v1/telegram/webhook` with constant-time verification of `X-Telegram-Bot-Api-Secret-Token`.
- **Deduplication:** Dedupes incoming `update_id`s in Redis with a 24-hour TTL.
- **Single-Use Callback Tokens:** When rendering a draft approval card, 16-hex-character single-use tokens are created in `callback_tokens` (<= 64-byte Telegram callback_data limit). Upon tap, the token is atomically consumed, preventing double-clicks or re-execution.
- **Rate Limiting:** Enforces ~1 msg/sec per chat and ~30 msg/sec globally via Redis token buckets.
- **403 Forbidden Handling:** When a user blocks the bot, HTTP 403 automatically marks `telegram_links.is_active = false` without throwing unhandled exceptions.

### 2.6 ARQ Scheduler & Periodic Worker
- Redis-backed background worker (`WorkerSettings`).
- 5-minute cron job claims due users via `claim_due_users`, calculates next occurrence using Python `zoneinfo` and `tzdata` (accounting for DST), and schedules deterministic jobs (`digest_{user_id}_{date}` and `sync_{user_id}_{hour}`).
- Daily executive digest generation with category breakdowns and pending draft summaries.
