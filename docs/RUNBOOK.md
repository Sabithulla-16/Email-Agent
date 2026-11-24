# Email Agent Operations & Runbook

## 1. Service Orchestration & Operations

Email Agent is composed of three core services:
1. **API Server:** FastAPI running under Uvicorn (`app.main:app`).
2. **Background Worker:** ARQ worker process (`arq app.queue.worker.WorkerSettings`).
3. **Queue / Cache:** Redis instance (for distributed locks, rate limiting, and job queues).

### Starting Services via Docker Compose
```bash
# Build and run API, Worker, and Redis in the background
docker-compose up -d --build

# Inspect service logs
docker-compose logs -f api
docker-compose logs -f worker
```

### Starting Services Locally with `uv`
```bash
# 1. Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# 2. Start FastAPI Server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 3. Start ARQ Worker
uv run arq app.queue.worker.WorkerSettings
```

---

## 2. Health Probes & Monitoring

The API exposes two standardized health endpoints for container orchestrators and load balancers:

- `GET /healthz`: Basic liveness probe. Returns HTTP 200 `{"status": "ok"}` when the Python event loop is alive.
- `GET /readyz`: Deep readiness probe. Verifies active connectivity to:
  1. **Supabase Database:** Queries `users` table via PostgREST HTTPS client.
  2. **Redis Queue:** Pings the Redis cache pool.
  Returns HTTP 200 when all systems are healthy, or HTTP 503 `{"status": "degraded"}` if any dependency is unreachable.

---

## 3. Database Migrations & Schema Management

### Applying Migrations via Supabase CLI
```bash
# Apply pending migrations to local or remote Supabase instance
supabase db push
```

### Applying Migrations via Supabase Dashboard SQL Editor
1. Open the Supabase project dashboard.
2. Navigate to **SQL Editor**.
3. Open `supabase/migrations/20261124000000_initial_schema.sql` and run the script.
4. (Optional) Run `supabase/seed.sql` to populate sample development data.

### Important: Reloading the PostgREST Schema Cache
After applying any new database migration or modifying SQL functions, PostgREST must reload its internal schema cache to register new tables and RPC functions.
Execute the following query in the Supabase SQL Editor:
```sql
NOTIFY pgrst, 'reload schema';
```

---

## 4. Operational Maintenance Procedures

### 4.1 Token Vault Key Rotation
When rotating `TOKEN_ENCRYPTION_KEY`:
1. Generate a new 32-byte key: `openssl rand -hex 16`.
2. Run the vault rotation script specifying the previous key, previous version, and new key:
   ```bash
   uv run python scripts/rotate_vault_keys.py \
       --old-key "<PREVIOUS_32_BYTE_KEY>" \
       --old-version 1 \
       --new-key "<NEW_32_BYTE_KEY>" \
       --new-version 2
   ```
3. Update `TOKEN_ENCRYPTION_KEY` and `TOKEN_KEY_VERSION=2` in your production environment variables.
4. Restart the API and Worker containers.

### 4.2 Data Retention Cleanup
Run the retention cleanup job regularly (e.g. daily cron) to prune raw email bodies older than 30 days while retaining AI summaries:
```bash
uv run python scripts/cleanup_retention.py --days 30
```

---

## 5. Troubleshooting & Incident Playbooks

### Issue 1: Google OAuth `invalid_grant`
- **Symptom:** Worker or sync logs report `google.auth.exceptions.RefreshError: ('invalid_grant', ...)`.
- **Cause:** User revoked access, changed their Google password, or refresh token expired (in Google OAuth Testing mode, refresh tokens expire after 7 days).
- **Resolution:**
  1. The system automatically marks `oauth_tokens.status = 'needs_reauth'` and halts scheduled sync for that user.
  2. A notification is dispatched informing the user to re-authenticate at `/api/v1/auth/google/start`.

### Issue 2: Telegram HTTP 403 Forbidden
- **Symptom:** `TelegramClient` logs warning `telegram_bot_blocked_by_user`.
- **Resolution:** The client automatically updates `telegram_links.is_active = false` for that `chat_id`. No action required; notifications will fallback to the web app.

### Issue 3: Telegram HTTP 429 Too Many Requests
- **Symptom:** Outgoing messages receive HTTP 429.
- **Resolution:** The client extracts `parameters.retry_after` from the Telegram JSON response or header and sleeps asynchronously before retrying.

### Issue 4: Groq LLM Rate Limits (TPM / RPM)
- **Symptom:** Groq returns HTTP 429.
- **Resolution:** `GroqService` executes bounded retries with exponential backoff and jitter. If retries exhaust, it gracefully degrades or defers draft generation to the next worker sync cycle.
