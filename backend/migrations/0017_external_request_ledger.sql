-- Persistent external-call preflight ledger for models, OCR and remote MCP.
-- Request metadata is append-only and contains no case body, prompt, token or secret.

BEGIN;

CREATE TABLE external_request_authorizations (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    authorized_by uuid NOT NULL REFERENCES users(user_id),
    request_kind text NOT NULL CHECK (request_kind IN ('MODEL', 'OCR', 'MCP')),
    purpose text NOT NULL CHECK (length(trim(purpose)) > 0),
    provider_id text NOT NULL CHECK (length(trim(provider_id)) > 0),
    processor_region text NOT NULL CHECK (length(trim(processor_region)) > 0),
    retention_policy text NOT NULL CHECK (length(trim(retention_policy)) > 0),
    training_policy text NOT NULL CHECK (length(trim(training_policy)) > 0),
    selected_field_ids jsonb NOT NULL CHECK (jsonb_typeof(selected_field_ids) = 'array'),
    service_id text NOT NULL CHECK (length(trim(service_id)) > 0),
    call_cap integer NOT NULL CHECK (call_cap > 0 AND call_cap <= 100),
    cost_cap_minor bigint NOT NULL CHECK (cost_cap_minor >= 0),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    authorization_hash char(64) NOT NULL CHECK (authorization_hash ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz NOT NULL,
    authorized_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (authorized_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (expires_at > authorized_at)
);

CREATE TABLE external_request_attempts (
    attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    request_id uuid NOT NULL REFERENCES external_request_authorizations(request_id),
    attempted_by uuid NOT NULL REFERENCES users(user_id),
    sequence integer NOT NULL CHECK (sequence > 0 AND sequence <= 100),
    status text NOT NULL CHECK (status IN ('SUBMISSION_STARTED', 'SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION', 'CANCELLED', 'EXPIRED')),
    provider_request_ref_hash char(64) CHECK (provider_request_ref_hash IS NULL OR provider_request_ref_hash ~ '^[0-9a-f]{64}$'),
    output_hash char(64) CHECK (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$'),
    error_code text CHECK (error_code IS NULL OR length(trim(error_code)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, sequence),
    UNIQUE (attempt_id, firm_id, matter_id),
    FOREIGN KEY (request_id, firm_id, matter_id) REFERENCES external_request_authorizations(request_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (attempted_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (status = 'SUBMISSION_STARTED' AND provider_request_ref_hash IS NOT NULL AND output_hash IS NULL AND error_code IS NULL)
        OR (status = 'SUCCEEDED' AND output_hash IS NOT NULL AND error_code IS NULL)
        OR (status IN ('FAILED', 'UNKNOWN_SUBMISSION', 'CANCELLED', 'EXPIRED') AND output_hash IS NULL AND error_code IS NOT NULL)
    )
);

CREATE INDEX external_request_authorizations_matter_created_idx ON external_request_authorizations (matter_id, authorized_at DESC);
CREATE INDEX external_request_attempts_request_sequence_idx ON external_request_attempts (request_id, sequence DESC);

ALTER TABLE external_request_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_request_authorizations FORCE ROW LEVEL SECURITY;
ALTER TABLE external_request_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_request_attempts FORCE ROW LEVEL SECURITY;

CREATE POLICY external_request_authorizations_firm_isolation ON external_request_authorizations
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY external_request_attempts_firm_isolation ON external_request_attempts
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_external_request_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'external request ledger is append-only';
END;
$$;

CREATE TRIGGER external_request_authorizations_append_only BEFORE UPDATE OR DELETE ON external_request_authorizations
    FOR EACH ROW EXECUTE FUNCTION prohibit_external_request_ledger_mutation();
CREATE TRIGGER external_request_attempts_append_only BEFORE UPDATE OR DELETE ON external_request_attempts
    FOR EACH ROW EXECUTE FUNCTION prohibit_external_request_ledger_mutation();

COMMIT;
