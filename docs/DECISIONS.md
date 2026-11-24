# Architecture & Design Decisions

This document records key technical decisions, trade-offs, and rationale for Email Agent.

## ADR-001: Async Supabase Client Repository Pattern
- **Decision:** Wrap the official `supabase-py` async client in repository classes under `app/db/repos/`. No direct calls to PostgREST/Supabase outside repositories.
- **Rationale:** Ensures clean separation of concerns, testability via dependency injection/mocking, strict validation using Pydantic row models, and centralized error mapping.

## ADR-002: Token Vault & Base64 Text Storage
- **Decision:** Encrypt Google OAuth refresh tokens using AES-256-GCM with 12-byte random IVs and store ciphertexts as base64 strings in `oauth_tokens.refresh_token_enc` (text column). Store key version in `key_version`.
- **Rationale:** Aligns with database access requirement ("Store encrypted tokens as base64 text, not bytea") and supports multi-version key rotation.

## ADR-003: Model Selection & Groq Structured Output
- **Decision:**
  - Fast analysis & classification: `llama-3.1-8b-instant`
  - High quality drafting & revision: `llama-3.3-70b-versatile`
  - Voice transcription: `whisper-large-v3-turbo`
  - Structured output mode: `response_format={"type": "json_object"}` with Pydantic validation and retry fallback.
- **Rationale:** `llama-3.1-8b-instant` provides sub-second latency for classification and summary; `llama-3.3-70b-versatile` matches high precision for tone and context adherence; `whisper-large-v3-turbo` delivers rapid transcription of Telegram voice notes.

## ADR-004: State Machine Isolation and Concurrency
- **Decision:** Centralize all draft status transitions in `ApprovalService` using atomic compare-and-set via SQL RPC `transition_draft`.
- **Rationale:** Guarantees strict single-transition semantics and idempotency under concurrent web and Telegram requests. Prevents double-sending.

## ADR-005: Telegram Webhook Callback Data Tokenization
- **Decision:** Telegram callback data is strictly capped at 64 bytes. All action buttons encode a short opaque token (nanoid/uuid hex prefix) mapped to `callback_tokens`.
- **Rationale:** Satisfies Telegram's payload limit while maintaining full auditability, cryptographic randomness, expiry, and single-use invalidation.

## ADR-006: RLS and Security Invoker Functions
- **Decision:** Enable RLS on every table in `public` with no policies (default deny for `anon` and `authenticated`). Mark all atomic stored procedures `SECURITY INVOKER` with explicit `search_path = public, extensions`, and revoke `EXECUTE` from `PUBLIC`, `anon`, and `authenticated`.
- **Rationale:** Enforces least privilege. Only backend requests authenticated with `SUPABASE_SECRET_KEY` can access tables and execute RPC functions.

## ADR-007: Zero Auto-Send Invariant
- **Decision:** The system strictly prohibits automated email sending. Even when a user sets an automation rule to `auto` trust level, the rule may only label, categorize, or create tasks. Outgoing messages must always be approved by the user via web app or Telegram.
- **Rationale:** Prevents unintended communications, hallucinated promises, and unauthorized outgoing emails.

## ADR-008: High-Performance List Projections
- **Decision:** API list endpoints (e.g. `GET /api/v1/emails`) return `EmailListItem` schemas that deliberately omit `body_clean`. Full body text is only returned on individual item detail lookups.
- **Rationale:** Drastically reduces network payload sizes and memory consumption during pagination.

## ADR-009: 2FA / OTP Pre-Filtering
- **Decision:** Sensitive security and two-factor verification emails (containing OTP codes) are pre-filtered via regex before reaching Groq LLMs.
- **Rationale:** Protects user privacy, eliminates unnecessary LLM API costs, and avoids leaking temporary authentication codes to external models.

## ADR-010: Data Retention and Body Pruning
- **Decision:** Raw email bodies older than 30 days are pruned to `[RETENTION_PRUNED]` via scheduled retention jobs, while AI summaries and classifications are preserved in `email_analysis`.
- **Rationale:** Minimizes long-term data storage risks, enforces data minimization principles, while maintaining search and executive digest history.
