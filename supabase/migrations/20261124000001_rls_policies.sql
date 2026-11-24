-- Migration: 20261124000001_rls_policies.sql
-- Description: Explicit Row-Level Security (RLS) policies for multi-tenant isolation.
-- Adheres to Supabase Postgres Best Practices:
--   - Uses (SELECT auth.uid()) to prevent per-row evaluation (100x+ faster).
--   - Strictly isolates all user data by user_id so users can only ever see their own records.
--   - Protects sensitive credentials (oauth_tokens, sessions, callback_tokens) from client access.

-- 1. Users Table
CREATE POLICY users_select_own ON public.users
    FOR SELECT TO authenticated
    USING (id = (SELECT auth.uid()));

CREATE POLICY users_update_own ON public.users
    FOR UPDATE TO authenticated
    USING (id = (SELECT auth.uid()))
    WITH CHECK (id = (SELECT auth.uid()));

-- 2. Emails Table
CREATE POLICY emails_owner_policy ON public.emails
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 3. Email Analysis (Linked via email_id)
CREATE POLICY email_analysis_owner_policy ON public.email_analysis
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.emails
            WHERE emails.id = email_analysis.email_id
              AND emails.user_id = (SELECT auth.uid())
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.emails
            WHERE emails.id = email_analysis.email_id
              AND emails.user_id = (SELECT auth.uid())
        )
    );

-- 4. Drafts Table
CREATE POLICY drafts_owner_policy ON public.drafts
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 5. Draft Versions (Linked via draft_id)
CREATE POLICY draft_versions_owner_policy ON public.draft_versions
    FOR ALL TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.drafts
            WHERE drafts.id = draft_versions.draft_id
              AND drafts.user_id = (SELECT auth.uid())
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.drafts
            WHERE drafts.id = draft_versions.draft_id
              AND drafts.user_id = (SELECT auth.uid())
        )
    );

-- 6. Sync State
CREATE POLICY sync_state_select_own ON public.sync_state
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

-- 7. Telegram Links
CREATE POLICY telegram_links_select_own ON public.telegram_links
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));

-- 8. Notification Preferences
CREATE POLICY notification_prefs_owner_policy ON public.notification_prefs
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 9. Rules Table
CREATE POLICY rules_owner_policy ON public.rules
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 10. Feedback Table
CREATE POLICY feedback_owner_policy ON public.feedback
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 11. Tasks Table
CREATE POLICY tasks_owner_policy ON public.tasks
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 12. Reminders Table
CREATE POLICY reminders_owner_policy ON public.reminders
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 13. Followups Table
CREATE POLICY followups_owner_policy ON public.followups
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 14. Commitments Table
CREATE POLICY commitments_owner_policy ON public.commitments
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 15. Contacts Table
CREATE POLICY contacts_owner_policy ON public.contacts
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 16. Bills Table
CREATE POLICY bills_owner_policy ON public.bills
    FOR ALL TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- 17. Audit Log
CREATE POLICY audit_log_select_own ON public.audit_log
    FOR SELECT TO authenticated
    USING (user_id = (SELECT auth.uid()));
