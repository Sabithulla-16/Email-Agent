-- Migration: 20261125000000_ist_digest_and_security.sql
-- Description: Sync default user timezone to Asia/Kolkata (IST), add last_digest_at for
--              incremental 9 AM / 3 PM / 9 PM digests.

ALTER TABLE public.users
    ALTER COLUMN timezone SET DEFAULT 'Asia/Kolkata';

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS last_digest_at TIMESTAMPTZ;

-- Migrate existing default UTC users to Asia/Kolkata and reset next_digest_at so
-- the scheduler recomputes the next IST slot (09:00, 15:00, or 21:00 IST).
UPDATE public.users
SET timezone = 'Asia/Kolkata',
    next_digest_at = NULL
WHERE timezone = 'UTC';
