SET ROLE lawcase_schema_owner;

REVOKE CREATE ON SCHEMA public FROM PUBLIC,
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_ledger_confirmation_owner;
GRANT USAGE ON SCHEMA public TO
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_ledger_confirmation_owner;

-- Identity and session control planes never inherit the broad RLS application
-- surface. Rebuild their migration-defined column grants exactly.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM
    lawcase_identity_directory,
    lawcase_web_session_gateway;

GRANT SELECT (issuer, subject, firm_id, user_id, is_active, active_human_roles)
    ON TABLE public.web_oidc_identities TO lawcase_identity_directory;
GRANT SELECT (firm_id)
    ON TABLE public.firms TO lawcase_identity_directory;
GRANT SELECT (user_id, firm_id, status)
    ON TABLE public.users TO lawcase_identity_directory;

GRANT SELECT (
    session_id, firm_id, user_id, issuer, session_token_sha256,
    csrf_token_sha256, authenticated_at, created_at, expires_at, revoked_at
) ON TABLE public.web_sessions TO lawcase_web_session_gateway;
GRANT INSERT (
    session_id, firm_id, user_id, issuer, session_token_sha256,
    csrf_token_sha256, authenticated_at, created_at, expires_at
) ON TABLE public.web_sessions TO lawcase_web_session_gateway;
GRANT UPDATE (revoked_at)
    ON TABLE public.web_sessions TO lawcase_web_session_gateway;
GRANT REFERENCES (firm_id)
    ON TABLE public.firms TO lawcase_web_session_gateway;
GRANT REFERENCES (user_id, firm_id)
    ON TABLE public.users TO lawcase_web_session_gateway;

-- Ordinary application/Agent connections must not enumerate the global OIDC
-- directory or raw session-control rows. Web confirmation goes only through
-- the 0048+ SECURITY DEFINER boundary.
REVOKE ALL ON TABLE public.web_oidc_identities FROM
    lawcase_web_application,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_web_session_gateway;
REVOKE ALL ON TABLE public.web_sessions FROM
    lawcase_web_application,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_identity_directory;

-- Firm/user provisioning is an admin/seed operation. Runtime services receive
-- only the reads they need to re-authorize humans and dedicated workers.
REVOKE INSERT, UPDATE, DELETE ON TABLE public.firms, public.users FROM
    lawcase_web_application,
    lawcase_agent_worker,
    lawcase_agent_verifier;

-- CREATE_MATTER locks the two server-owned Agent identities before inserting
-- their case-role bindings. PostgreSQL requires UPDATE privilege for that
-- lock even though the Web role never changes a user row. Retain only the
-- immutable primary-key column needed for this lock; all other user writes
-- remain revoked above.
GRANT UPDATE (user_id) ON TABLE public.users TO lawcase_web_application;

-- The independent verifier reads all tenant tables but writes only the
-- append/projection surface used by PostgresCaseAgentStore verification.
GRANT INSERT ON TABLE
    public.case_agent_events,
    public.case_agent_verification_attempts,
    public.case_agent_verification_receipts,
    public.case_agent_checkpoints,
    public.case_agent_command_audits
TO lawcase_agent_verifier;
GRANT UPDATE ON TABLE
    public.case_agent_runs,
    public.case_agent_run_inbox
TO lawcase_agent_verifier;

REVOKE ALL ON FUNCTION
    public.authorize_case_agent_verification_snapshot(uuid, uuid, uuid, integer)
FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
GRANT EXECUTE ON FUNCTION
    public.authorize_case_agent_verification_snapshot(uuid, uuid, uuid, integer)
TO lawcase_agent_verifier;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
    public.outbox_events,
    public.command_idempotency
FROM lawcase_agent_verifier;
GRANT INSERT (
    firm_id, matter_id, aggregate_version, event_type, payload
) ON TABLE public.outbox_events TO lawcase_agent_verifier;
GRANT INSERT (
    firm_id, matter_id, actor_id, command_name, idempotency_key,
    request_hash, response_json
) ON TABLE public.command_idempotency TO lawcase_agent_verifier;

ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    OWNER TO lawcase_schema_owner;
ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    SECURITY DEFINER;
ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    SET search_path = pg_catalog, public, pg_temp;
REVOKE ALL ON FUNCTION
    public.group_case_agent_ledger_exceptions_after_staging()
FROM PUBLIC, lawcase_web_application, lawcase_agent_worker,
     lawcase_agent_verifier, lawcase_ledger_confirmation_owner;

REVOKE ALL ON TABLE public.lawcase_schema_migrations FROM PUBLIC,
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_ledger_confirmation_owner;
