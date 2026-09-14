-- Hash-only, server-owned browser session records. PostgreSQL 16+.
--
-- Apply after 0023_web_oidc_identity_directory.sql.  This is an
-- authentication-control table, not a client-addressable case-data table:
-- before a valid opaque session digest is matched, the server does not know a
-- tenant.  It therefore uses a narrow, exact-digest RLS selector for the
-- dedicated ``lawcase_web_session_gateway`` role only.
--
-- Deployment MUST provision that NOINHERIT role before migration and give its
-- credentials only to the server Web session gateway.  It must have no
-- BYPASSRLS privilege and no grants on matter, evidence, object-store, or
-- submission tables.  After session resolution, application work moves to a
-- separate transaction which executes ``SET LOCAL app.firm_id`` from this server-derived
-- session record.  Browser-supplied firm IDs, roles, JWTs and raw tokens are
-- never persisted here.

BEGIN;

CREATE TABLE web_sessions (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    user_id uuid NOT NULL,
    issuer text COLLATE "C" NOT NULL,
    session_token_sha256 bytea NOT NULL,
    csrf_token_sha256 bytea NOT NULL,
    authenticated_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    UNIQUE (session_token_sha256),
    FOREIGN KEY (user_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (octet_length(session_token_sha256) = 32),
    CHECK (octet_length(csrf_token_sha256) = 32),
    CHECK (
        length(issuer) BETWEEN 1 AND 1024
        AND issuer = btrim(issuer)
        AND issuer !~ '[[:cntrl:]]'
        AND issuer LIKE 'https://%'
    ),
    CHECK (authenticated_at <= created_at),
    CHECK (expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

COMMENT ON TABLE web_sessions IS
    'Server-only, SHA-256 hash-based Web session/CSRF control records; raw cookies and OIDC JWTs are forbidden.';

-- The digest unique constraint is the exact-session lookup index.  These two
-- partial indexes serve explicit revocation and expiry-reaping paths without
-- indexing every expired control record indefinitely.
CREATE INDEX web_sessions_active_actor_idx
    ON web_sessions (firm_id, user_id, created_at DESC)
    WHERE revoked_at IS NULL;
CREATE INDEX web_sessions_active_expiry_idx
    ON web_sessions (expires_at)
    WHERE revoked_at IS NULL;

ALTER TABLE web_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_sessions FORCE ROW LEVEL SECURITY;

-- Normal writes happen only after a verified server identity has supplied the
-- firm context.  A browser cannot set a PostgreSQL local setting itself.
CREATE POLICY web_sessions_create_in_firm ON web_sessions
    FOR INSERT
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

-- Reads are either inside an established tenant transaction or through one
-- exact SHA-256 selector.  The gateway sets the selector from the HttpOnly
-- cookie only after hashing it server-side; an absent/malformed selector sees
-- no rows.  This does not permit prefix, user, firm or role enumeration.
CREATE POLICY web_sessions_read_in_firm_or_exact_digest ON web_sessions
    FOR SELECT
    USING (
        firm_id::text = current_setting('app.firm_id', true)
        OR CASE
            WHEN current_setting('app.web_session_token_sha256', true) ~ '^[0-9a-f]{64}$'
                THEN session_token_sha256 = decode(current_setting('app.web_session_token_sha256', true), 'hex')
            ELSE FALSE
        END
    );

-- Session lifecycle is append-then-revoke.  A future logout/admin service may
-- use the known tenant context or the server-only random session UUID; the
-- trigger below permits no other mutation.
CREATE POLICY web_sessions_revoke_in_firm_or_exact_session ON web_sessions
    FOR UPDATE
    USING (
        firm_id::text = current_setting('app.firm_id', true)
        OR session_id::text = current_setting('app.web_session_id', true)
    )
    WITH CHECK (
        firm_id::text = current_setting('app.firm_id', true)
        OR session_id::text = current_setting('app.web_session_id', true)
    );

CREATE FUNCTION restrict_web_session_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR OLD.revoked_at IS NOT NULL
       OR NEW.revoked_at IS NULL
       OR NEW.revoked_at < OLD.created_at
       OR (to_jsonb(NEW) - ARRAY['revoked_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['revoked_at']) THEN
        RAISE EXCEPTION 'web session permits only one revocation timestamp';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER web_sessions_append_then_revoke
    BEFORE UPDATE OR DELETE ON web_sessions
    FOR EACH ROW EXECUTE FUNCTION restrict_web_session_mutation();

REVOKE ALL ON TABLE web_sessions FROM PUBLIC;
GRANT SELECT (
    session_id,
    firm_id,
    user_id,
    issuer,
    session_token_sha256,
    csrf_token_sha256,
    authenticated_at,
    created_at,
    expires_at,
    revoked_at
) ON web_sessions TO lawcase_web_session_gateway;
GRANT INSERT (
    session_id,
    firm_id,
    user_id,
    issuer,
    session_token_sha256,
    csrf_token_sha256,
    authenticated_at,
    created_at,
    expires_at
) ON web_sessions TO lawcase_web_session_gateway;
GRANT UPDATE (revoked_at) ON web_sessions TO lawcase_web_session_gateway;

-- Foreign-key checks need only these reference privileges; they do not grant
-- row reads and do not broaden the session gateway into a tenant-data role.
GRANT REFERENCES (firm_id) ON firms TO lawcase_web_session_gateway;
GRANT REFERENCES (user_id, firm_id) ON users TO lawcase_web_session_gateway;

COMMIT;
