-- Phase 2 core persistence schema.
-- PostgreSQL 16+ only. This migration contains no personal or case data.
-- The application transaction must SET LOCAL app.firm_id before every tenant query.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE firms (
    firm_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    external_subject text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'OFFBOARDED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, external_subject)
);

ALTER TABLE users ADD CONSTRAINT users_user_firm_unique UNIQUE (user_id, firm_id);

CREATE TABLE matters (
    matter_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    title text NOT NULL,
    stage text NOT NULL CHECK (stage IN (
        'CREATED', 'INGESTING', 'MATERIAL_REVIEW', 'FACT_REVIEW', 'CLAIM_REVIEW',
        'LEGAL_REVIEW', 'CALCULATION_REVIEW', 'DRAFT_REVIEW', 'FINAL_QA',
        'READY_TO_EXPORT', 'EXPORTED', 'ARCHIVED'
    )),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    current_submission_bundle_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE matters ADD CONSTRAINT matters_matter_firm_unique UNIQUE (matter_id, firm_id);

CREATE TABLE matter_actor_roles (
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    user_id uuid NOT NULL REFERENCES users(user_id),
    role text NOT NULL CHECK (role IN (
        'ASSISTANT', 'COLLABORATING_LAWYER', 'LEAD_LAWYER', 'REVIEWER', 'FIRM_ADMIN', 'SYSTEM_WORKER'
    )),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    PRIMARY KEY (matter_id, user_id, role),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id),
    FOREIGN KEY (user_id, firm_id) REFERENCES users (user_id, firm_id)
);

CREATE TABLE approvals (
    approval_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    approval_type text NOT NULL,
    object_hash char(64) NOT NULL CHECK (object_hash ~ '^[0-9a-f]{64}$'),
    approved_matter_version integer NOT NULL CHECK (approved_matter_version > 0),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    permission_snapshot jsonb NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (matter_id, approval_type, approved_matter_version),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users (user_id, firm_id)
);

CREATE TABLE submission_bundles (
    bundle_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    lifecycle text NOT NULL CHECK (lifecycle IN ('DRAFT', 'QA_READY', 'LOCKED', 'EXPORTED')),
    validity text NOT NULL CHECK (validity IN ('VALID', 'STALE', 'REVOKED')),
    final_text_hash char(64) NOT NULL CHECK (final_text_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    approved_matter_version integer NOT NULL CHECK (approved_matter_version > 0),
    locked_at timestamptz,
    exported_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matter_id, bundle_id),
    -- Later tenant-bound submission tables reference the immutable bundle
    -- together with its firm and matter. PostgreSQL requires that exact
    -- column tuple to be backed by a unique key; the bundle_id primary key
    -- alone does not satisfy a composite foreign key.
    UNIQUE (bundle_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users (user_id, firm_id)
);

ALTER TABLE matters
    ADD CONSTRAINT matters_current_bundle_same_matter
    FOREIGN KEY (matter_id, current_submission_bundle_id)
    REFERENCES submission_bundles (matter_id, bundle_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX submission_bundles_one_current_valid_locked_per_matter
    ON submission_bundles (matter_id)
    WHERE lifecycle = 'LOCKED' AND validity = 'VALID';

CREATE TABLE command_idempotency (
    command_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    actor_id uuid NOT NULL REFERENCES users(user_id),
    command_name text NOT NULL,
    idempotency_key text NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_json jsonb NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, matter_id, actor_id, command_name, idempotency_key),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users (user_id, firm_id)
);

CREATE TABLE audit_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    actor_id uuid REFERENCES users(user_id),
    event_type text NOT NULL,
    input_version integer NOT NULL CHECK (input_version >= 0),
    output_version integer NOT NULL
        CONSTRAINT audit_events_output_version_check
        CHECK (output_version > input_version),
    request_id uuid NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users (user_id, firm_id)
);

CREATE TABLE outbox_events (
    outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid REFERENCES matters(matter_id),
    aggregate_version integer,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    CHECK ((matter_id IS NULL AND aggregate_version IS NULL) OR (matter_id IS NOT NULL AND aggregate_version > 0)),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id)
);

CREATE INDEX matters_firm_updated_idx ON matters (firm_id, updated_at DESC);
CREATE INDEX approvals_matter_version_idx ON approvals (matter_id, approved_matter_version DESC);
CREATE INDEX audit_events_matter_occurred_idx ON audit_events (matter_id, occurred_at ASC);
CREATE INDEX outbox_events_pending_idx ON outbox_events (created_at ASC) WHERE delivered_at IS NULL;

CREATE FUNCTION prohibit_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events are append-only';
END;
$$;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_audit_mutation();

-- Each tenant table carries firm_id to make row policies direct and reviewable.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE matters ENABLE ROW LEVEL SECURITY;
ALTER TABLE matters FORCE ROW LEVEL SECURITY;
ALTER TABLE matter_actor_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE matter_actor_roles FORCE ROW LEVEL SECURITY;
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE submission_bundles ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_bundles FORCE ROW LEVEL SECURITY;
ALTER TABLE command_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;

CREATE POLICY users_firm_isolation ON users
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY matters_firm_isolation ON matters
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY matter_actor_roles_firm_isolation ON matter_actor_roles
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY approvals_firm_isolation ON approvals
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY submission_bundles_firm_isolation ON submission_bundles
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY command_idempotency_firm_isolation ON command_idempotency
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY audit_events_firm_isolation ON audit_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY outbox_events_firm_isolation ON outbox_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
