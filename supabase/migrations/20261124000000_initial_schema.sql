-- Migration: 20261124000000_initial_schema.sql
-- Description: Core tables, indexes, triggers, RLS, and atomic RPC functions for Email Agent.

-- 1. Extensions
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA extensions;

-- 2. Trigger for automatic updated_at timestamp
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public;

-- 3. Tables Definition

-- 3.1 Users
CREATE TABLE IF NOT EXISTS public.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    timezone TEXT DEFAULT 'UTC' NOT NULL,
    digest_time TIME DEFAULT '09:00:00' NOT NULL,
    locale TEXT DEFAULT 'en' NOT NULL,
    prefs JSONB DEFAULT '{"tone": "friendly", "signature": "", "language": "en"}'::jsonb NOT NULL,
    feature_flags JSONB DEFAULT '{}'::jsonb NOT NULL,
    next_digest_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_google_sub ON public.users(google_sub);
CREATE INDEX IF NOT EXISTS idx_users_next_digest ON public.users(next_digest_at) WHERE deleted_at IS NULL;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.2 OAuth Tokens (Refresh tokens encrypted with AES-256-GCM as base64 text)
CREATE TABLE IF NOT EXISTS public.oauth_tokens (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    refresh_token_enc TEXT NOT NULL,
    key_version INT DEFAULT 1 NOT NULL,
    scopes TEXT[] NOT NULL,
    status TEXT DEFAULT 'active' NOT NULL CHECK (status IN ('active', 'needs_reauth', 'revoked')),
    last_refreshed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE TRIGGER trg_oauth_tokens_updated_at
    BEFORE UPDATE ON public.oauth_tokens
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.3 Sessions
CREATE TABLE IF NOT EXISTS public.sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    user_agent TEXT,
    ip TEXT,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON public.sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON public.sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user_expires ON public.sessions(user_id, expires_at);

-- 3.4 Sync State
CREATE TABLE IF NOT EXISTS public.sync_state (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    last_history_id TEXT,
    last_run_at TIMESTAMPTZ,
    last_error TEXT,
    lock_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE TRIGGER trg_sync_state_updated_at
    BEFORE UPDATE ON public.sync_state
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.5 Emails
CREATE TABLE IF NOT EXISTS public.emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    gmail_msg_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    rfc_message_id TEXT,
    sender TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    reply_to TEXT,
    subject TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    snippet TEXT,
    body_clean TEXT NOT NULL,
    has_attachments BOOLEAN DEFAULT false NOT NULL,
    list_unsubscribe TEXT,
    labels TEXT[] DEFAULT '{}'::text[] NOT NULL,
    embedding extensions.vector(384),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT uq_emails_user_gmail_msg UNIQUE (user_id, gmail_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_emails_user_received ON public.emails(user_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_user_thread ON public.emails(user_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_rfc_id ON public.emails(rfc_message_id);

-- HNSW Vector Index for Semantic Search
CREATE INDEX IF NOT EXISTS idx_emails_embedding_hnsw ON public.emails
USING hnsw (embedding extensions.vector_cosine_ops);

CREATE TRIGGER trg_emails_updated_at
    BEFORE UPDATE ON public.emails
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.6 Email Analysis
CREATE TABLE IF NOT EXISTS public.email_analysis (
    email_id UUID PRIMARY KEY REFERENCES public.emails(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('work', 'personal', 'finance', 'promo', 'newsletter', 'security', 'travel', 'other')),
    needs_reply BOOLEAN NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    summary TEXT NOT NULL,
    action_items JSONB DEFAULT '[]'::jsonb NOT NULL,
    deadline DATE,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    risk_flags TEXT[] DEFAULT '{}'::text[] NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    tokens_in INT DEFAULT 0 NOT NULL,
    tokens_out INT DEFAULT 0 NOT NULL,
    latency_ms INT DEFAULT 0 NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

-- 3.7 Drafts
CREATE TABLE IF NOT EXISTS public.drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID NOT NULL REFERENCES public.emails(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'draft_generated',
        'pending_approval',
        'approved',
        'sending',
        'sent',
        'failed',
        'rejected',
        'revising',
        'expired'
    )),
    current_version INT DEFAULT 1 NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    sent_gmail_msg_id TEXT,
    sent_at TIMESTAMPTZ,
    failure_reason TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_user_status ON public.drafts(user_id, status);
CREATE INDEX IF NOT EXISTS idx_drafts_email_id ON public.drafts(email_id);
CREATE INDEX IF NOT EXISTS idx_drafts_expires_at ON public.drafts(expires_at) WHERE status IN ('draft_generated', 'pending_approval', 'revising');

CREATE TRIGGER trg_drafts_updated_at
    BEFORE UPDATE ON public.drafts
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.8 Draft Versions
CREATE TABLE IF NOT EXISTS public.draft_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES public.drafts(id) ON DELETE CASCADE,
    version INT NOT NULL,
    body TEXT NOT NULL,
    user_instruction TEXT,
    source TEXT NOT NULL CHECK (source IN ('llm', 'user_edit', 'revision')),
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT uq_draft_versions_version UNIQUE (draft_id, version)
);

CREATE INDEX IF NOT EXISTS idx_draft_versions_draft_id ON public.draft_versions(draft_id);

-- 3.9 Telegram Links
CREATE TABLE IF NOT EXISTS public.telegram_links (
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    chat_id BIGINT PRIMARY KEY,
    is_active BOOLEAN DEFAULT true NOT NULL,
    linked_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_links_user_id ON public.telegram_links(user_id);

CREATE TRIGGER trg_telegram_links_updated_at
    BEFORE UPDATE ON public.telegram_links
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.10 Telegram Link Tokens (Single-use)
CREATE TABLE IF NOT EXISTS public.telegram_link_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_link_tokens_user ON public.telegram_link_tokens(user_id);

-- 3.11 Notification Preferences
CREATE TABLE IF NOT EXISTS public.notification_prefs (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    channel TEXT DEFAULT 'telegram' NOT NULL CHECK (channel IN ('telegram', 'web', 'email', 'all')),
    quiet_start TIME,
    quiet_end TIME,
    minimal_mode BOOLEAN DEFAULT false NOT NULL,
    snoozed_until TIMESTAMPTZ,
    digest_enabled BOOLEAN DEFAULT true NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE TRIGGER trg_notification_prefs_updated_at
    BEFORE UPDATE ON public.notification_prefs
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.12 Callback Tokens (Single-use opaque tokens for Telegram inline buttons)
CREATE TABLE IF NOT EXISTS public.callback_tokens (
    token TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    draft_id UUID NOT NULL REFERENCES public.drafts(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_callback_tokens_draft ON public.callback_tokens(draft_id);

-- 3.13 Pending Interactions (State for Telegram conversation flows like ForceReply)
CREATE TABLE IF NOT EXISTS public.pending_interactions (
    chat_id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    draft_id UUID NOT NULL REFERENCES public.drafts(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_interactions_user ON public.pending_interactions(user_id);

-- 3.14 Rules
CREATE TABLE IF NOT EXISTS public.rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    condition JSONB NOT NULL,
    action JSONB NOT NULL,
    trust_level TEXT DEFAULT 'suggest' NOT NULL CHECK (trust_level IN ('suggest', 'act_notify', 'auto')),
    enabled BOOLEAN DEFAULT true NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_user_id ON public.rules(user_id);

CREATE TRIGGER trg_rules_updated_at
    BEFORE UPDATE ON public.rules
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 3.15 Feedback
CREATE TABLE IF NOT EXISTS public.feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    rating INT NOT NULL CHECK (rating >= 1 AND rating <= 5),
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_user_entity ON public.feedback(user_id, entity_type, entity_id);

-- 3.16 Audit Log
CREATE TABLE IF NOT EXISTS public.audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    meta JSONB DEFAULT '{}'::jsonb NOT NULL,
    ts TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user_ts ON public.audit_log(user_id, ts DESC);

-- 3.17 Later-phase Extension Tables (Phase 8: Tasks, Reminders, Followups, Commitments, Contacts, Bills)
CREATE TABLE IF NOT EXISTS public.tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID REFERENCES public.emails(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    due_date DATE,
    status TEXT DEFAULT 'pending' NOT NULL CHECK (status IN ('pending', 'completed', 'cancelled')),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON public.tasks(user_id);

CREATE TABLE IF NOT EXISTS public.reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID REFERENCES public.emails(id) ON DELETE SET NULL,
    remind_at TIMESTAMPTZ NOT NULL,
    note TEXT,
    triggered BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_user_remind ON public.reminders(user_id, remind_at);

CREATE TABLE IF NOT EXISTS public.followups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID REFERENCES public.emails(id) ON DELETE SET NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    resolved BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_user_due ON public.followups(user_id, due_at);

CREATE TABLE IF NOT EXISTS public.commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID REFERENCES public.emails(id) ON DELETE SET NULL,
    promise TEXT NOT NULL,
    promised_to TEXT NOT NULL,
    due_date DATE,
    status TEXT DEFAULT 'open' NOT NULL CHECK (status IN ('open', 'fulfilled', 'cancelled')),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commitments_user_due ON public.commitments(user_id, due_date);

CREATE TABLE IF NOT EXISTS public.contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT,
    vip BOOLEAN DEFAULT false NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT uq_contacts_user_email UNIQUE (user_id, email)
);
CREATE INDEX IF NOT EXISTS idx_contacts_user_email ON public.contacts(user_id, email);

CREATE TABLE IF NOT EXISTS public.bills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    email_id UUID REFERENCES public.emails(id) ON DELETE SET NULL,
    biller TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    currency TEXT DEFAULT 'USD' NOT NULL,
    due_date DATE,
    paid BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bills_user_due ON public.bills(user_id, due_date);

-- 4. Row-Level Security (RLS) Configuration
-- Enforce RLS on EVERY table in public schema with NO policies.
-- Anonymous and standard authenticated roles will be denied by default.
-- Only backend service key (bypassing RLS) can access.

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_analysis ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.draft_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.telegram_link_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.callback_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bills ENABLE ROW LEVEL SECURITY;

-- 5. Stored Procedures & Atomic Functions (Called via rpc())

-- 5.1 Claim due users for scheduler (FOR UPDATE SKIP LOCKED)
CREATE OR REPLACE FUNCTION public.claim_due_users(p_limit INT DEFAULT 20)
RETURNS SETOF public.users AS $$
BEGIN
    RETURN QUERY
    SELECT *
    FROM public.users
    WHERE next_digest_at <= now()
      AND deleted_at IS NULL
    ORDER BY next_digest_at ASC
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.2 Acquire per-user sync lock
CREATE OR REPLACE FUNCTION public.acquire_sync_lock(
    p_user_id UUID,
    p_ttl_seconds INT DEFAULT 300
)
RETURNS BOOLEAN AS $$
DECLARE
    v_locked BOOLEAN := false;
BEGIN
    INSERT INTO public.sync_state (user_id, lock_until, last_run_at)
    VALUES (p_user_id, now() + (p_ttl_seconds || ' seconds')::interval, now())
    ON CONFLICT (user_id) DO UPDATE
    SET lock_until = now() + (p_ttl_seconds || ' seconds')::interval,
        last_run_at = now()
    WHERE public.sync_state.lock_until IS NULL
       OR public.sync_state.lock_until < now();

    IF FOUND THEN
        v_locked := true;
    END IF;

    RETURN v_locked;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.3 Release per-user sync lock
CREATE OR REPLACE FUNCTION public.release_sync_lock(p_user_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE public.sync_state
    SET lock_until = NULL
    WHERE user_id = p_user_id;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.4 Transition Draft (Atomic Compare-and-Set with Audit Log)
CREATE OR REPLACE FUNCTION public.transition_draft(
    p_draft_id UUID,
    p_from_status TEXT,
    p_to_status TEXT,
    p_failure_reason TEXT DEFAULT NULL,
    p_sent_gmail_msg_id TEXT DEFAULT NULL
)
RETURNS public.drafts AS $$
DECLARE
    v_updated_draft public.drafts;
BEGIN
    UPDATE public.drafts
    SET status = p_to_status,
        failure_reason = p_failure_reason,
        sent_gmail_msg_id = COALESCE(p_sent_gmail_msg_id, sent_gmail_msg_id),
        sent_at = CASE WHEN p_to_status = 'sent' THEN now() ELSE sent_at END,
        updated_at = now()
    WHERE id = p_draft_id
      AND status = p_from_status
    RETURNING * INTO v_updated_draft;

    IF v_updated_draft.id IS NOT NULL THEN
        INSERT INTO public.audit_log (
            user_id,
            action,
            entity,
            entity_id,
            meta,
            ts
        ) VALUES (
            v_updated_draft.user_id,
            'draft_transition',
            'draft',
            v_updated_draft.id::text,
            jsonb_build_object(
                'from_status', p_from_status,
                'to_status', p_to_status,
                'failure_reason', p_failure_reason,
                'sent_gmail_msg_id', p_sent_gmail_msg_id
            ),
            now()
        );
    END IF;

    RETURN v_updated_draft;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.5 Create Draft Version (Atomic version bump)
CREATE OR REPLACE FUNCTION public.create_draft_version(
    p_draft_id UUID,
    p_body TEXT,
    p_user_instruction TEXT,
    p_source TEXT,
    p_model TEXT
)
RETURNS public.draft_versions AS $$
DECLARE
    v_next_version INT;
    v_created_version public.draft_versions;
BEGIN
    -- Lock draft row for update
    SELECT current_version + 1 INTO v_next_version
    FROM public.drafts
    WHERE id = p_draft_id
    FOR UPDATE;

    IF v_next_version IS NULL THEN
        RAISE EXCEPTION 'Draft % not found', p_draft_id;
    END IF;

    -- Insert new version record
    INSERT INTO public.draft_versions (
        draft_id,
        version,
        body,
        user_instruction,
        source,
        model,
        created_at
    ) VALUES (
        p_draft_id,
        v_next_version,
        p_body,
        p_user_instruction,
        p_source,
        p_model,
        now()
    ) RETURNING * INTO v_created_version;

    -- Update current_version on parent draft
    UPDATE public.drafts
    SET current_version = v_next_version,
        updated_at = now()
    WHERE id = p_draft_id;

    RETURN v_created_version;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.6 Match emails using pgvector cosine similarity
CREATE OR REPLACE FUNCTION public.match_emails(
    p_user_id UUID,
    p_query_embedding extensions.vector(384),
    p_k INT DEFAULT 5,
    p_threshold FLOAT DEFAULT 0.5
)
RETURNS TABLE (
    id UUID,
    gmail_msg_id TEXT,
    subject TEXT,
    snippet TEXT,
    similarity FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        e.id,
        e.gmail_msg_id,
        e.subject,
        e.snippet,
        (1 - (e.embedding <=> p_query_embedding))::FLOAT AS similarity
    FROM public.emails e
    WHERE e.user_id = p_user_id
      AND e.embedding IS NOT NULL
      AND (1 - (e.embedding <=> p_query_embedding)) >= p_threshold
    ORDER BY e.embedding <=> p_query_embedding ASC
    LIMIT p_k;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 5.7 Delete all user data ("Delete my data")
CREATE OR REPLACE FUNCTION public.delete_user_data(p_user_id UUID)
RETURNS VOID AS $$
BEGIN
    -- Write audit log entry before deletion
    INSERT INTO public.audit_log (
        user_id,
        action,
        entity,
        entity_id,
        meta,
        ts
    ) VALUES (
        p_user_id,
        'user_data_deleted',
        'user',
        p_user_id::text,
        jsonb_build_object('event', 'complete_deletion_requested'),
        now()
    );

    -- Delete user (cascades to all user tables)
    DELETE FROM public.users WHERE id = p_user_id;
END;
$$ LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, extensions;

-- 6. Permissions Hardening
-- Revoke all execute privileges on functions from PUBLIC, anon, and authenticated roles.
REVOKE ALL ON FUNCTION public.set_updated_at() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_due_users(INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.acquire_sync_lock(UUID, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_sync_lock(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.transition_draft(UUID, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.create_draft_version(UUID, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.match_emails(UUID, extensions.vector(384), INT, FLOAT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.delete_user_data(UUID) FROM PUBLIC, anon, authenticated;
