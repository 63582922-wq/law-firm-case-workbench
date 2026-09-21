-- Versioned, path-minimized inventories for lawyer-selected local case folders.
-- Apply after 0009_legal_source_license_review.sql. Absolute paths and folder
-- grants are process-local and must never enter these tables.

BEGIN;

CREATE TABLE local_folder_scans (
    scan_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    root_fingerprint char(64) NOT NULL CHECK (root_fingerprint ~ '^[0-9a-f]{64}$'),
    manifest_hash char(64) NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    base_scan_id uuid,
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'INVALIDATED')),
    total_files integer NOT NULL CHECK (total_files >= 0 AND total_files <= 10000),
    total_bytes bigint NOT NULL CHECK (total_bytes >= 0 AND total_bytes <= 10737418240),
    skipped_symlinks integer NOT NULL CHECK (skipped_symlinks >= 0),
    new_count integer NOT NULL CHECK (new_count >= 0),
    modified_count integer NOT NULL CHECK (modified_count >= 0),
    moved_count integer NOT NULL CHECK (moved_count >= 0),
    missing_count integer NOT NULL CHECK (missing_count >= 0),
    unchanged_count integer NOT NULL CHECK (unchanged_count >= 0),
    duplicate_content_count integer NOT NULL CHECK (duplicate_content_count >= 0),
    created_by uuid NOT NULL,
    approved_by uuid,
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    scanned_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    invalidated_at timestamptz,
    UNIQUE (scan_id, firm_id, matter_id),
    CHECK (
        (status = 'CANDIDATE' AND approved_by IS NULL AND approval_hash IS NULL AND approved_at IS NULL AND invalidated_at IS NULL)
        OR (status = 'APPROVED' AND approved_by IS NOT NULL AND approval_hash IS NOT NULL AND approved_at IS NOT NULL AND invalidated_at IS NULL)
        OR (status = 'INVALIDATED' AND invalidated_at IS NOT NULL)
    ),
    CHECK (new_count + modified_count + moved_count + unchanged_count = total_files),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (base_scan_id, firm_id, matter_id)
        REFERENCES local_folder_scans(scan_id, firm_id, matter_id)
);

CREATE UNIQUE INDEX local_folder_scans_one_candidate_per_matter
    ON local_folder_scans (matter_id) WHERE status = 'CANDIDATE';
CREATE UNIQUE INDEX local_folder_scans_one_approved_per_matter
    ON local_folder_scans (matter_id) WHERE status = 'APPROVED';
CREATE INDEX local_folder_scans_matter_created_idx
    ON local_folder_scans (matter_id, created_at DESC, scan_id DESC);

CREATE TABLE local_folder_scan_files (
    scan_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    relative_path text NOT NULL CHECK (
        length(relative_path) BETWEEN 1 AND 4096
        AND relative_path !~ '(^|/)\.\.(/|$)'
        AND relative_path !~ '^/'
    ),
    previous_relative_path text CHECK (
        previous_relative_path IS NULL OR (
            length(previous_relative_path) BETWEEN 1 AND 4096
            AND previous_relative_path !~ '(^|/)\.\.(/|$)'
            AND previous_relative_path !~ '^/'
        )
    ),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    file_sha256 char(64) NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    detected_kind text NOT NULL CHECK (detected_kind IN (
        'PDF', 'IMAGE', 'WORD_DOCUMENT', 'SPREADSHEET', 'TEXT', 'EMAIL', 'ARCHIVE', 'OTHER'
    )),
    change_kind text NOT NULL CHECK (change_kind IN ('NEW', 'MODIFIED', 'MOVED', 'MISSING', 'UNCHANGED')),
    present boolean NOT NULL,
    sort_sequence integer NOT NULL CHECK (sort_sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scan_id, relative_path),
    UNIQUE (scan_id, sort_sequence),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (scan_id, firm_id, matter_id)
        REFERENCES local_folder_scans(scan_id, firm_id, matter_id) ON DELETE CASCADE,
    CHECK ((change_kind = 'MISSING' AND present = false) OR (change_kind <> 'MISSING' AND present = true)),
    CHECK ((change_kind IN ('MOVED', 'MISSING') AND previous_relative_path IS NOT NULL) OR change_kind NOT IN ('MOVED', 'MISSING'))
);

CREATE INDEX local_folder_scan_files_page_idx
    ON local_folder_scan_files (scan_id, sort_sequence);
CREATE INDEX local_folder_scan_files_hash_idx
    ON local_folder_scan_files (scan_id, file_sha256, byte_size) WHERE present = true;

CREATE FUNCTION prohibit_local_folder_scan_file_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'local folder scan file snapshots are append-only';
END;
$$;

CREATE TRIGGER local_folder_scan_files_append_only
    BEFORE UPDATE OR DELETE ON local_folder_scan_files
    FOR EACH ROW EXECUTE FUNCTION prohibit_local_folder_scan_file_mutation();

CREATE FUNCTION prohibit_local_folder_scan_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'local folder scan snapshots cannot be deleted';
END;
$$;

CREATE TRIGGER local_folder_scans_no_delete
    BEFORE DELETE ON local_folder_scans
    FOR EACH ROW EXECUTE FUNCTION prohibit_local_folder_scan_delete();

ALTER TABLE local_folder_scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE local_folder_scans FORCE ROW LEVEL SECURITY;
ALTER TABLE local_folder_scan_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE local_folder_scan_files FORCE ROW LEVEL SECURITY;

CREATE POLICY local_folder_scans_firm_isolation ON local_folder_scans
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY local_folder_scan_files_firm_isolation ON local_folder_scan_files
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
