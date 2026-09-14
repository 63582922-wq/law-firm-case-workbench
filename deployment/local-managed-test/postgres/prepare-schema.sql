SET ROLE lawcase_schema_owner;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_ledger_confirmation_owner;

-- PostgreSQL requires a new table/function owner to have CREATE on the
-- containing schema at ALTER OWNER time. Migration 0048 transfers only its
-- tightly scoped definer objects and then revokes this bit itself; the final
-- hardening pass repeats that revocation before any runtime service starts.
GRANT CREATE ON SCHEMA public TO lawcase_ledger_confirmation_owner;

-- The Web and execution adapters span the complete current application
-- schema, but remain tenant-constrained by FORCE RLS and later migrations
-- explicitly revoke sensitive direct-DML/column surfaces. Setting defaults
-- before 0001 ensures those forward revocations are effective on a fresh DB.
ALTER DEFAULT PRIVILEGES FOR ROLE lawcase_schema_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES
    TO lawcase_web_application, lawcase_agent_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE lawcase_schema_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO lawcase_agent_verifier;

CREATE TABLE IF NOT EXISTS public.lawcase_schema_migrations (
    migration_number integer PRIMARY KEY CHECK (migration_number > 0),
    filename text NOT NULL UNIQUE CHECK (
        filename ~ '^[0-9]{4}_[a-z0-9_]+[.]sql$'
    ),
    source_sha256 char(64) NOT NULL CHECK (
        source_sha256 ~ '^[0-9a-f]{64}$'
    ),
    state text NOT NULL CHECK (state IN ('APPLYING', 'APPLIED')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_at timestamptz,
    CHECK (
        (state = 'APPLYING' AND applied_at IS NULL)
        OR (state = 'APPLIED' AND applied_at IS NOT NULL)
    )
);

REVOKE ALL ON TABLE public.lawcase_schema_migrations FROM PUBLIC,
    lawcase_web_application,
    lawcase_identity_directory,
    lawcase_web_session_gateway,
    lawcase_agent_worker,
    lawcase_agent_verifier,
    lawcase_ledger_confirmation_owner;
