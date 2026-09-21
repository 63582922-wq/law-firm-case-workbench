-- Server-owned OIDC identity directory. PostgreSQL 16+; apply after 0001.
--
-- A verified issuer/subject must resolve to exactly one internal firm/user.
-- This prohibits a browser from selecting a firm or supplying roles after OIDC
-- authentication. Multi-firm membership therefore requires an explicit admin
-- provisioning decision, never a client-side organisation switch.
--
-- This is deliberately a small global lookup, not a client-addressable or
-- case/tenant API table: the firm is unknown until this exact lookup succeeds.
-- It intentionally has no tenant RLS policy. Deployment MUST provision the
-- dedicated NOINHERIT least-privilege service role
-- ``lawcase_identity_directory`` before this migration; its credentials belong
-- only to the server identity resolver, never to browser code. The grants below
-- cause a safe migration failure when that role was not provisioned.
--
-- Once the mapped firm is known, the resolver SET LOCAL app.firm_id before it
-- reads ``users``. Existing FORCE RLS continues to protect tenant/case tables,
-- and this role receives SELECT-only, column-level access with no write grant.
-- Identity rows are provisioned or disabled by a separate controlled admin/seed
-- workflow. This migration creates no self-signup or browser write path.

BEGIN;

CREATE TABLE web_oidc_identities (
    web_oidc_identity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer text COLLATE "C" NOT NULL,
    subject text COLLATE "C" NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    user_id uuid NOT NULL REFERENCES users(user_id),
    is_active boolean NOT NULL DEFAULT TRUE,
    active_human_roles text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issuer, subject),
    FOREIGN KEY (user_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        length(issuer) BETWEEN 1 AND 1024
        AND issuer = btrim(issuer)
        AND issuer !~ '[[:cntrl:]]'
    ),
    CHECK (
        length(subject) BETWEEN 1 AND 255
        AND subject = btrim(subject)
        AND subject !~ '[[:cntrl:]]'
    ),
    CHECK (
        cardinality(active_human_roles) BETWEEN 1 AND 5
        AND array_position(active_human_roles, NULL) IS NULL
        AND active_human_roles <@ ARRAY[
            'ASSISTANT',
            'COLLABORATING_LAWYER',
            'LEAD_LAWYER',
            'REVIEWER',
            'FIRM_ADMIN'
        ]::text[]
    )
);

COMMENT ON TABLE web_oidc_identities IS
    'Global server-only mapping from verified OIDC issuer/subject to one active internal human actor.';

REVOKE ALL ON TABLE web_oidc_identities FROM PUBLIC;

GRANT SELECT (issuer, subject, firm_id, user_id, is_active, active_human_roles)
    ON TABLE web_oidc_identities TO lawcase_identity_directory;
GRANT SELECT (firm_id)
    ON TABLE firms TO lawcase_identity_directory;
GRANT SELECT (user_id, firm_id, status)
    ON TABLE users TO lawcase_identity_directory;

COMMIT;
