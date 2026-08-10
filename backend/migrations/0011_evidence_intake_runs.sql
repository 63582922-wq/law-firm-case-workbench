-- Recoverable material-intake work bound to a lawyer-approved local folder scan.
-- Apply after 0010_local_folder_intake.sql.  Folder grants and absolute paths
-- remain process-local and are deliberately absent from this schema.

BEGIN;

CREATE TABLE evidence_intake_runs (
    run_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    scan_id uuid NOT NULL,
    scan_manifest_hash char(64) NOT NULL CHECK (scan_manifest_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'STALE')),
    created_by uuid NOT NULL,
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    stale_at timestamptz,
    stale_reason text,
    UNIQUE (run_id, firm_id, matter_id),
    CHECK (
        (status IN ('QUEUED', 'RUNNING') AND completed_at IS NULL AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status IN ('SUCCEEDED', 'PARTIAL') AND completed_at IS NOT NULL AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'STALE' AND stale_at IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (scan_id, firm_id, matter_id)
        REFERENCES local_folder_scans(scan_id, firm_id, matter_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX evidence_intake_runs_one_active_per_scan
    ON evidence_intake_runs (scan_id) WHERE status IN ('QUEUED', 'RUNNING');
CREATE UNIQUE INDEX evidence_intake_runs_one_success_per_scan
    ON evidence_intake_runs (scan_id) WHERE status = 'SUCCEEDED';
CREATE INDEX evidence_intake_runs_matter_created_idx
    ON evidence_intake_runs (matter_id, created_at DESC, run_id DESC);

CREATE TABLE evidence_intake_items (
    item_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    scan_id uuid NOT NULL,
    relative_path text NOT NULL CHECK (
        length(relative_path) BETWEEN 1 AND 4096
        AND relative_path !~ '(^|/)\.\.(/|$)'
        AND relative_path !~ '^/'
    ),
    expected_byte_size bigint NOT NULL CHECK (expected_byte_size >= 0),
    expected_sha256 char(64) NOT NULL CHECK (expected_sha256 ~ '^[0-9a-f]{64}$'),
    detected_kind text NOT NULL CHECK (detected_kind IN (
        'PDF', 'IMAGE', 'WORD_DOCUMENT', 'SPREADSHEET', 'TEXT', 'EMAIL', 'ARCHIVE', 'OTHER'
    )),
    status text NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'REGISTERED', 'REVIEW_REQUIRED', 'BLOCKED', 'FAILED', 'STALE'
    )),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    lease_id uuid,
    lease_expires_at timestamptz,
    evidence_file_id uuid,
    inspection_hash char(64) CHECK (inspection_hash IS NULL OR inspection_hash ~ '^[0-9a-f]{64}$'),
    scanner_name text,
    scanner_definitions_version text,
    outcome_code text CHECK (outcome_code IS NULL OR outcome_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    stale_at timestamptz,
    UNIQUE (item_id, firm_id, matter_id),
    UNIQUE (run_id, relative_path),
    CHECK (
        (
            status = 'QUEUED' AND attempt_count = 0 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND evidence_file_id IS NULL AND inspection_hash IS NULL AND scanner_name IS NULL
            AND scanner_definitions_version IS NULL AND outcome_code IS NULL AND completed_at IS NULL AND stale_at IS NULL
        ) OR (
            status = 'RUNNING' AND attempt_count > 0 AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL
            AND evidence_file_id IS NULL AND completed_at IS NULL AND stale_at IS NULL
        ) OR (
            status = 'REGISTERED' AND attempt_count > 0 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND evidence_file_id IS NOT NULL AND inspection_hash IS NOT NULL AND scanner_name IS NOT NULL
            AND scanner_definitions_version IS NOT NULL AND outcome_code IS NULL AND completed_at IS NOT NULL AND stale_at IS NULL
        ) OR (
            status IN ('REVIEW_REQUIRED', 'BLOCKED') AND attempt_count > 0 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND evidence_file_id IS NULL AND inspection_hash IS NOT NULL AND scanner_name IS NOT NULL
            AND scanner_definitions_version IS NOT NULL AND outcome_code IS NOT NULL AND completed_at IS NOT NULL AND stale_at IS NULL
        ) OR (
            status = 'FAILED' AND attempt_count > 0 AND lease_id IS NULL AND lease_expires_at IS NULL
            AND evidence_file_id IS NULL AND outcome_code IS NOT NULL AND completed_at IS NOT NULL AND stale_at IS NULL
        ) OR (
            status = 'STALE' AND lease_id IS NULL AND lease_expires_at IS NULL AND stale_at IS NOT NULL
        )
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES evidence_intake_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (scan_id, relative_path)
        REFERENCES local_folder_scan_files(scan_id, relative_path),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)
);

CREATE INDEX evidence_intake_items_claim_idx
    ON evidence_intake_items (run_id, status, created_at, item_id);
CREATE INDEX evidence_intake_items_matter_status_idx
    ON evidence_intake_items (matter_id, status, updated_at DESC);

CREATE FUNCTION prohibit_evidence_intake_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence intake history cannot be deleted';
END;
$$;

CREATE TRIGGER evidence_intake_runs_no_delete
    BEFORE DELETE ON evidence_intake_runs
    FOR EACH ROW EXECUTE FUNCTION prohibit_evidence_intake_delete();
CREATE TRIGGER evidence_intake_items_no_delete
    BEFORE DELETE ON evidence_intake_items
    FOR EACH ROW EXECUTE FUNCTION prohibit_evidence_intake_delete();

ALTER TABLE evidence_intake_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_intake_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_intake_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_intake_items FORCE ROW LEVEL SECURITY;

CREATE POLICY evidence_intake_runs_firm_isolation ON evidence_intake_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_intake_items_firm_isolation ON evidence_intake_items
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
