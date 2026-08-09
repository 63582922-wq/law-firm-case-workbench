-- Recoverable, lawyer-authorized official-source capture and review runs.
-- Apply after 0007_submission_compilation.sql.

BEGIN;

CREATE TABLE official_source_capture_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    source_id text NOT NULL CHECK (length(trim(source_id)) > 0),
    publisher text NOT NULL CHECK (length(trim(publisher)) > 0),
    source_tier text NOT NULL CHECK (source_tier IN ('PRIMARY_LAW', 'JUDICIAL_INTERPRETATION', 'OFFICIAL_RATE_DATA', 'PUBLIC_CASE_RESEARCH')),
    target_url text NOT NULL CHECK (target_url ~ '^https://'),
    query_sha256 char(64) NOT NULL CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
    authorization_hash char(64) NOT NULL CHECK (authorization_hash ~ '^[0-9a-f]{64}$'),
    max_response_bytes integer NOT NULL CHECK (max_response_bytes > 0 AND max_response_bytes <= 67108864),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'REVIEW_REQUIRED', 'FAILED', 'STALE')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count IN (0, 1)),
    lease_id uuid,
    lease_expires_at timestamptz,
    authorized_by uuid NOT NULL REFERENCES users(user_id),
    authorized_at timestamptz NOT NULL,
    authorization_expires_at timestamptz NOT NULL,
    final_url text CHECK (final_url IS NULL OR final_url ~ '^https://'),
    retrieved_at timestamptz,
    peer_ip inet,
    content_media_type text,
    content_sha256 char(64) CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    content_bytes integer,
    storage_object_key text CHECK (storage_object_key IS NULL OR storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'),
    capture_verification_hash char(64) CHECK (capture_verification_hash IS NULL OR capture_verification_hash ~ '^[0-9a-f]{64}$'),
    parser_kind text,
    parsed_output_hash char(64) CHECK (parsed_output_hash IS NULL OR parsed_output_hash ~ '^[0-9a-f]{64}$'),
    parsed_summary jsonb,
    failure_code text CHECK (failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    stale_at timestamptz,
    stale_reason text,
    UNIQUE (run_id, firm_id),
    UNIQUE (run_id, firm_id, matter_id),
    CHECK (authorization_expires_at > authorized_at),
    CHECK (parsed_summary IS NULL OR jsonb_typeof(parsed_summary) = 'object'),
    CHECK (
        content_sha256 IS NULL OR storage_object_key = substring(content_sha256 from 1 for 2) || '/' ||
            substring(content_sha256 from 3 for 2) || '/' || content_sha256 || '.lca'
    ),
    CHECK (content_bytes IS NULL OR (content_bytes > 0 AND content_bytes <= max_response_bytes)),
    CHECK (
        (status = 'QUEUED' AND attempt_count = 0 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND content_sha256 IS NULL AND failure_code IS NULL AND completed_at IS NULL
            AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'RUNNING' AND attempt_count = 1 AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
            AND content_sha256 IS NULL AND failure_code IS NULL AND completed_at IS NULL
            AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'REVIEW_REQUIRED' AND attempt_count = 1 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND final_url IS NOT NULL AND retrieved_at IS NOT NULL AND peer_ip IS NOT NULL
            AND content_media_type IS NOT NULL AND content_sha256 IS NOT NULL AND content_bytes IS NOT NULL
            AND storage_object_key IS NOT NULL AND capture_verification_hash IS NOT NULL
            AND parser_kind IS NOT NULL AND parsed_output_hash IS NOT NULL AND parsed_summary IS NOT NULL
            AND failure_code IS NULL AND completed_at IS NOT NULL AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'FAILED' AND attempt_count = 1 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND content_sha256 IS NULL AND failure_code IS NOT NULL AND completed_at IS NOT NULL
            AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND lease_id IS NULL AND lease_expires_at IS NULL
            AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (authorized_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE official_source_capture_reviews (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('APPROVE_FOR_REGISTRATION', 'REJECT')),
    provision_locator text NOT NULL CHECK (length(trim(provision_locator)) > 0),
    review_hash char(64) NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    reviewed_by uuid NOT NULL REFERENCES users(user_id),
    reviewed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id),
    UNIQUE (review_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES official_source_capture_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (reviewed_by, firm_id) REFERENCES users(user_id, firm_id)
);

ALTER TABLE official_legal_source_snapshots
    ADD COLUMN capture_run_id uuid;
ALTER TABLE official_legal_source_snapshots
    ADD CONSTRAINT official_legal_source_snapshots_capture_run_fk
    FOREIGN KEY (capture_run_id, firm_id)
        REFERENCES official_source_capture_runs(run_id, firm_id);
CREATE UNIQUE INDEX official_legal_source_snapshots_capture_run_idx
    ON official_legal_source_snapshots (capture_run_id)
    WHERE capture_run_id IS NOT NULL;

CREATE UNIQUE INDEX official_source_capture_runs_one_active_source
    ON official_source_capture_runs (matter_id, source_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX official_source_capture_runs_matter_status_idx
    ON official_source_capture_runs (matter_id, status, created_at DESC);
CREATE INDEX official_source_capture_reviews_matter_idx
    ON official_source_capture_reviews (matter_id, reviewed_at DESC);

ALTER TABLE official_source_capture_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE official_source_capture_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE official_source_capture_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE official_source_capture_reviews FORCE ROW LEVEL SECURITY;

CREATE POLICY official_source_capture_runs_firm_isolation ON official_source_capture_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY official_source_capture_reviews_firm_isolation ON official_source_capture_reviews
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE TRIGGER official_source_capture_reviews_append_only
    BEFORE UPDATE OR DELETE ON official_source_capture_reviews
    FOR EACH ROW EXECUTE FUNCTION prohibit_submission_compilation_mutation();

COMMIT;
