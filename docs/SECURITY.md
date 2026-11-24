# Email Agent Security Model & Hardening

## 1. Threat Model & Principles of Defense

Email Agent handles sensitive user communications and OAuth credentials. The system is engineered according to the principle of least privilege, defense-in-depth, and strict human authorization.

---

## 2. Row Level Security (RLS) & Database Hardening

### 2.1 Default-Deny Architecture
- Every table created in the `public` schema has Row Level Security explicitly enabled (`ALTER TABLE ... ENABLE ROW LEVEL SECURITY;`).
- **NO public RLS policies exist.** Any direct request utilizing the Supabase `anon` or `authenticated` publishable keys is denied by default (returns 0 rows for reads, and fails for inserts/updates).
- The backend interacts exclusively using the **Supabase Secret Key** (`SUPABASE_SECRET_KEY`), which operates with administrative bypass rights over HTTPS PostgREST endpoints.
- Tests in `tests/test_phase1_database.py` verify that anonymous queries to all public tables are rejected.

### 2.2 Stored Procedure Hardening
- Every database function created in `supabase/migrations/` is defined with `SECURITY INVOKER` and an explicit search path:
  ```sql
  SECURITY INVOKER SET search_path = public, extensions
  ```
- Public and anonymous execution privileges are revoked immediately upon function definition:
  ```sql
  REVOKE ALL ON FUNCTION public.claim_due_users(INT) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.acquire_sync_lock(UUID, INT) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.release_sync_lock(UUID) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.transition_draft(UUID, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.create_draft_version(UUID, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.match_emails(UUID, vector, INT, FLOAT) FROM PUBLIC, anon, authenticated;
  REVOKE ALL ON FUNCTION public.delete_user_data(UUID) FROM PUBLIC, anon, authenticated;
  ```

---

## 3. Cryptographic Token Vault (AES-256-GCM)

### 3.1 Refresh Token Encryption
- Google OAuth refresh tokens are sensitive long-lived credentials. They are encrypted using **AES-256-GCM** authenticated encryption (`app/core/vault.py`).
- A cryptographically random **12-byte IV (nonce)** is generated for each individual token record.
- The ciphertext, authentication tag, and IV are bundled and stored as Base64 text in `oauth_tokens.refresh_token_enc`.
- The database never stores plaintext refresh tokens, and tokens are never printed to logs or transmitted across client APIs.

### 3.2 Key Rotation Support
- Every encrypted token record references a `key_version` integer.
- The system supports active multi-key rotation via `scripts/rotate_vault_keys.py`:
  1. The new 32-byte encryption key is provisioned with a higher version number.
  2. The rotation script fetches existing records, decrypts using the previous key version, re-encrypts with the latest key version, and updates the database atomically.

---

## 4. OWASP Top 10 for LLM Applications Compliance

| Threat | Risk Description | Mitigation in Email Agent |
| :--- | :--- | :--- |
| **LLM01: Prompt Injection** | Attackers craft email bodies attempting to hijack LLM system prompts | Untrusted email text is wrapped inside strict `<untrusted_email_content>` XML-style delimiters with explicit system instructions to ignore prompt overrides and never execute instructions found within the email body. |
| **LLM02: Sensitive Information Disclosure** | Exposure of PII, credit cards, or passwords in LLM responses | `app/services/redactor.py` scrubs credit card numbers (Luhn match) and password patterns before LLM submission. 2FA / OTP verification codes bypass the LLM completely via pre-filtering. |
| **LLM06: Excessive Agency** | Autonomous execution of damaging actions without oversight | **Core invariant:** Email Agent NEVER auto-sends an email. State transitions enforce explicit human approval before any Gmail API dispatch. |
| **LLM07: System Prompt Leakage** | Extraction of internal system prompts via adversarial queries | Prompts are segregated from untrusted input and forbid revealing prompt instructions or metadata. |
| **LLM10: Model Denial of Service** | Extremely long payloads causing high API latency or token exhaustion | Email bodies are capped at 15,000 characters during preprocessing, and Groq concurrency is bounded with semaphores. |

---

## 5. Authentication, Sessions & CSRF

- **Google OAuth 2.0 PKCE Flow:** Enforces RFC 7636 Proof Key for Code Exchange with SHA-256 code challenges and signed short-lived state parameters (`HMAC-SHA256`).
- **Opaque Sessions:** Session cookies deliver 32-byte cryptographically secure random tokens. The database stores ONLY the SHA-256 hash of the session token.
- **Cookie Security:** Cookies are tagged with `HttpOnly`, `Secure` (in production), and `SameSite=Lax`.
- **Double-Submit CSRF Defense:** State-changing browser requests (`POST`, `PUT`, `DELETE`) require a matching `X-CSRF-Token` header and `csrf_token` cookie. Programmatic API callers using `Authorization: Bearer <token>` bypass cookie CSRF.

---

## 6. Telegram Bot Webhook Security

- **Constant-Time Verification:** Webhook payloads on `POST /api/v1/telegram/webhook` require the `X-Telegram-Bot-Api-Secret-Token` header, validated using `hmac.compare_digest` to prevent timing attacks.
- **Replay Attack Defense:** Every incoming `update_id` is recorded in Redis with a 24-hour expiration (`SET ... NX EX 86400`). Duplicate updates are immediately discarded with a fast HTTP 200 response.
- **Single-Use Callback Tokens:** Inline buttons use 16-hex-character single-use tokens stored in `callback_tokens`. When tapped, the token is atomically consumed with `UPDATE ... WHERE used_at IS NULL AND expires_at > now()`, preventing duplicate button clicks.
