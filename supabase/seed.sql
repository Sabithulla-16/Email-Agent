-- Seed Data for local development
-- Inserts a test user, preferences, and sample email threads

INSERT INTO public.users (
    id,
    google_sub,
    email,
    name,
    timezone,
    digest_time,
    locale,
    prefs,
    feature_flags,
    next_digest_at
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    'google-sub-dev-user-12345',
    'dev.user@example.com',
    'Dev User',
    'America/New_York',
    '09:00:00',
    'en',
    '{"tone": "friendly", "signature": "Best regards,\nDev User", "language": "en"}'::jsonb,
    '{"smart_actions": true, "followup_tracker": true, "rules_engine": true, "inbox_qa": true, "extractors": true}'::jsonb,
    now() + interval '1 hour'
) ON CONFLICT (google_sub) DO NOTHING;

INSERT INTO public.sync_state (
    user_id,
    last_history_id,
    last_run_at
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    '10001',
    now()
) ON CONFLICT (user_id) DO NOTHING;

INSERT INTO public.notification_prefs (
    user_id,
    channel,
    quiet_start,
    quiet_end,
    minimal_mode,
    digest_enabled
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    'telegram',
    '22:00:00',
    '08:00:00',
    false,
    true
) ON CONFLICT (user_id) DO NOTHING;
