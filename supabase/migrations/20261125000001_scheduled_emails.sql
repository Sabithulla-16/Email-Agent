-- Migration: 20261125000001_scheduled_emails.sql
-- Description: Encrypted scheduled outbound emails table with RLS.

CREATE TABLE IF NOT EXISTS public.scheduled_emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    recipient_enc TEXT NOT NULL,
    subject_enc TEXT NOT NULL,
    body_enc TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    status TEXT DEFAULT 'pending' NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')
    ),
    sent_gmail_msg_id TEXT,
    failure_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_emails_due
    ON public.scheduled_emails(status, scheduled_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scheduled_emails_user
    ON public.scheduled_emails(user_id, scheduled_at DESC);

CREATE TRIGGER trg_scheduled_emails_updated_at
    BEFORE UPDATE ON public.scheduled_emails
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.scheduled_emails ENABLE ROW LEVEL SECURITY;

CREATE POLICY scheduled_emails_owner_policy ON public.scheduled_emails
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));
