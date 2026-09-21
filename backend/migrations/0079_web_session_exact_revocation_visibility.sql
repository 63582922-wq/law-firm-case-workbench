-- ADR-0089: UPDATE's WHERE/RETURNING also requires SELECT visibility.
-- Preserve 0024's exact-session revocation and append-then-revoke guard.
CREATE POLICY web_sessions_gateway_exact_revocation
    ON public.web_sessions
    FOR SELECT
    TO lawcase_web_session_gateway
    USING (
        session_id::text = pg_catalog.current_setting('app.web_session_id', true)
    );
