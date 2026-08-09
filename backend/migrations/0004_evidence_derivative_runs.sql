-- Recoverable local Worker runs for locked evidence Manifests.
-- Apply after 0003_evidence_manifest.sql.

BEGIN;

CREATE TABLE evidence_derivative_runs (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    manifest_id uuid NOT NULL,
    manifest_content_hash char(64) NOT NULL CHECK (manifest_content_hash ~ '^[0-9a-f]{64}$'),
    input_matter_version integer NOT NULL CHECK (input_matter_version > 0),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'STALE')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0 AND attempt_count <= 3),
    lease_id uuid,
    lease_expires_at timestamptz,
    failure_code text CHECK (failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
    related_derivative_id uuid,
    annotated_derivative_id uuid,
    created_by uuid NOT NULL REFERENCES users(user_id),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    stale_at timestamptz,
    stale_reason text,
    UNIQUE (run_id, firm_id, matter_id),
    CHECK (
        (
            status = 'QUEUED' AND attempt_count = 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND failure_code IS NULL
            AND related_derivative_id IS NULL AND annotated_derivative_id IS NULL
            AND completed_at IS NULL AND stale_at IS NULL AND stale_reason IS NULL
        )
        OR (
            status = 'RUNNING' AND attempt_count > 0 AND lease_id IS NOT NULL
            AND lease_expires_at IS NOT NULL AND failure_code IS NULL
            AND related_derivative_id IS NULL AND annotated_derivative_id IS NULL
            AND completed_at IS NULL AND stale_at IS NULL AND stale_reason IS NULL
        )
        OR (
            status = 'SUCCEEDED' AND attempt_count > 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND failure_code IS NULL
            AND related_derivative_id IS NOT NULL AND annotated_derivative_id IS NOT NULL
            AND completed_at IS NOT NULL AND stale_at IS NULL AND stale_reason IS NULL
        )
        OR (
            status = 'FAILED' AND attempt_count > 0 AND lease_id IS NULL
            AND lease_expires_at IS NULL AND failure_code IS NOT NULL
            AND related_derivative_id IS NULL AND annotated_derivative_id IS NULL
            AND completed_at IS NOT NULL AND stale_at IS NULL AND stale_reason IS NULL
        )
        OR (
            status = 'STALE' AND lease_id IS NULL AND lease_expires_at IS NULL
            AND related_derivative_id IS NULL AND annotated_derivative_id IS NULL
            AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0
        )
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (manifest_id, firm_id, matter_id)
        REFERENCES evidence_manifests(manifest_id, firm_id, matter_id),
    FOREIGN KEY (related_derivative_id, firm_id, matter_id)
        REFERENCES evidence_derivative_artifacts(derivative_id, firm_id, matter_id),
    FOREIGN KEY (annotated_derivative_id, firm_id, matter_id)
        REFERENCES evidence_derivative_artifacts(derivative_id, firm_id, matter_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX evidence_derivative_runs_one_active_per_manifest
    ON evidence_derivative_runs (manifest_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE UNIQUE INDEX evidence_derivative_runs_one_success_per_manifest
    ON evidence_derivative_runs (manifest_id)
    WHERE status = 'SUCCEEDED';
CREATE INDEX evidence_derivative_runs_matter_status_idx
    ON evidence_derivative_runs (matter_id, status, created_at DESC);

ALTER TABLE evidence_derivative_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_derivative_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY evidence_derivative_runs_firm_isolation ON evidence_derivative_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
