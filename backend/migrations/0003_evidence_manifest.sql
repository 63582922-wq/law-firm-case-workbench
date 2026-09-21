-- Immutable evidence originals, page decisions, duplicate resolution, annotations,
-- locked manifests, and derivative artifact lineage. Apply after 0002_case_ledgers.sql.

BEGIN;

CREATE TABLE evidence_original_files (
    evidence_file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    original_label text NOT NULL CHECK (length(trim(original_label)) > 0),
    original_file_sha256 char(64) NOT NULL CHECK (original_file_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    media_type text NOT NULL CHECK (length(trim(media_type)) > 0),
    page_count integer NOT NULL CHECK (page_count > 0),
    source_scan_fingerprint char(64) NOT NULL CHECK (source_scan_fingerprint ~ '^[0-9a-f]{64}$'),
    supersedes_file_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evidence_file_id, firm_id, matter_id),
    UNIQUE (matter_id, original_file_sha256, original_label),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (supersedes_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)
);

CREATE TABLE evidence_pages (
    evidence_page_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    evidence_file_id uuid NOT NULL,
    page_number integer NOT NULL CHECK (page_number > 0),
    rendered_page_sha256 char(64) CHECK (rendered_page_sha256 IS NULL OR rendered_page_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evidence_page_id, firm_id, matter_id),
    UNIQUE (evidence_file_id, page_number),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)
);

CREATE TABLE evidence_page_decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    evidence_page_id uuid NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('INCLUDE', 'EXCLUDE')),
    reason text NOT NULL CHECK (length(trim(reason)) > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'INVALIDATED')),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (decision_id, firm_id, matter_id),
    CHECK (
        (status = 'APPROVED' AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status <> 'APPROVED' AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX evidence_page_decisions_one_approved_per_page
    ON evidence_page_decisions (matter_id, evidence_page_id)
    WHERE status = 'APPROVED';

CREATE TABLE evidence_page_annotations (
    annotation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    evidence_page_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('HIGHLIGHT_RELEVANT_REGION')),
    x0 numeric(12,9) NOT NULL,
    y0 numeric(12,9) NOT NULL,
    x1 numeric(12,9) NOT NULL,
    y1 numeric(12,9) NOT NULL,
    label text NOT NULL CHECK (length(trim(label)) > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'INVALIDATED')),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (annotation_id, firm_id, matter_id),
    CHECK (x0 >= 0 AND x0 < x1 AND x1 <= 1 AND y0 >= 0 AND y0 < y1 AND y1 <= 1),
    CHECK (
        (status = 'APPROVED' AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status <> 'APPROVED' AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE evidence_page_duplicate_groups (
    duplicate_group_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'SAME_SOURCE_PAGE', 'DISTINCT_PAGES', 'INVALIDATED')),
    canonical_page_id uuid,
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (duplicate_group_id, firm_id, matter_id),
    CHECK (
        (status = 'CANDIDATE' AND canonical_page_id IS NULL AND approval_hash IS NULL AND approved_by IS NULL)
        OR (status = 'SAME_SOURCE_PAGE' AND canonical_page_id IS NOT NULL AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status = 'DISTINCT_PAGES' AND canonical_page_id IS NULL AND approval_hash IS NOT NULL AND approved_by IS NOT NULL)
        OR (status = 'INVALIDATED' AND canonical_page_id IS NULL AND approval_hash IS NULL AND approved_by IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (canonical_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE evidence_page_duplicate_members (
    duplicate_group_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (duplicate_group_id, evidence_page_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (duplicate_group_id, firm_id, matter_id)
        REFERENCES evidence_page_duplicate_groups(duplicate_group_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id)
);

CREATE TABLE evidence_manifests (
    manifest_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    ledger_version integer NOT NULL CHECK (ledger_version > 0),
    status text NOT NULL CHECK (status IN ('LOCKED', 'INVALIDATED')),
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    total_pages integer NOT NULL CHECK (total_pages > 0),
    included_pages integer NOT NULL CHECK (included_pages >= 0),
    excluded_pages integer NOT NULL CHECK (excluded_pages >= 0),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES users(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    invalidated_at timestamptz,
    UNIQUE (manifest_id, firm_id, matter_id),
    CHECK (included_pages + excluded_pages = total_pages),
    CHECK ((status = 'LOCKED' AND invalidated_at IS NULL) OR (status = 'INVALIDATED' AND invalidated_at IS NOT NULL)),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE UNIQUE INDEX evidence_manifests_one_locked_per_matter
    ON evidence_manifests (matter_id)
    WHERE status = 'LOCKED';

CREATE TABLE evidence_manifest_pages (
    manifest_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('INCLUDE', 'EXCLUDE')),
    derivative_sequence integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (manifest_id, evidence_page_id),
    UNIQUE (manifest_id, evidence_page_id, firm_id, matter_id),
    UNIQUE (manifest_id, decision_id),
    UNIQUE (manifest_id, derivative_sequence),
    CHECK (
        (disposition = 'INCLUDE' AND derivative_sequence IS NOT NULL AND derivative_sequence > 0)
        OR (disposition = 'EXCLUDE' AND derivative_sequence IS NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (manifest_id, firm_id, matter_id)
        REFERENCES evidence_manifests(manifest_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (decision_id, firm_id, matter_id)
        REFERENCES evidence_page_decisions(decision_id, firm_id, matter_id)
);

CREATE TABLE evidence_manifest_page_annotations (
    manifest_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    annotation_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (manifest_id, evidence_page_id, annotation_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (manifest_id, evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_manifest_pages(manifest_id, evidence_page_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (annotation_id, firm_id, matter_id)
        REFERENCES evidence_page_annotations(annotation_id, firm_id, matter_id)
);

CREATE TABLE evidence_derivative_artifacts (
    derivative_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    manifest_id uuid NOT NULL,
    artifact_type text NOT NULL CHECK (artifact_type IN ('RELATED_PAGES_PDF', 'ANNOTATED_RELATED_PAGES_PDF')),
    storage_object_key text NOT NULL CHECK (storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'),
    artifact_sha256 char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    page_count integer NOT NULL CHECK (page_count > 0),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'VERIFIED', 'STALE', 'REVOKED')),
    verification_hash char(64) CHECK (verification_hash IS NULL OR verification_hash ~ '^[0-9a-f]{64}$'),
    verified_by uuid REFERENCES users(user_id),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    invalidated_at timestamptz,
    invalidation_reason text,
    UNIQUE (derivative_id, firm_id, matter_id),
    UNIQUE (manifest_id, artifact_type, artifact_sha256),
    CHECK (
        (
            status = 'CANDIDATE'
            AND verification_hash IS NULL
            AND verified_by IS NULL
            AND verified_at IS NULL
            AND invalidated_at IS NULL
            AND invalidation_reason IS NULL
        )
        OR (
            status = 'VERIFIED'
            AND verification_hash IS NOT NULL
            AND verified_by IS NOT NULL
            AND verified_at IS NOT NULL
            AND invalidated_at IS NULL
            AND invalidation_reason IS NULL
        )
        OR (
            status IN ('STALE', 'REVOKED')
            AND invalidated_at IS NOT NULL
            AND length(trim(invalidation_reason)) > 0
            AND (
                (verification_hash IS NULL AND verified_by IS NULL AND verified_at IS NULL)
                OR (verification_hash IS NOT NULL AND verified_by IS NOT NULL AND verified_at IS NOT NULL)
            )
        )
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (manifest_id, firm_id, matter_id)
        REFERENCES evidence_manifests(manifest_id, firm_id, matter_id),
    FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE FUNCTION prohibit_evidence_original_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence originals and source pages are append-only';
END;
$$;

CREATE TRIGGER evidence_original_files_append_only
    BEFORE UPDATE OR DELETE ON evidence_original_files
    FOR EACH ROW EXECUTE FUNCTION prohibit_evidence_original_mutation();
CREATE TRIGGER evidence_pages_append_only
    BEFORE UPDATE OR DELETE ON evidence_pages
    FOR EACH ROW EXECUTE FUNCTION prohibit_evidence_original_mutation();

CREATE INDEX evidence_original_files_matter_idx ON evidence_original_files (matter_id, created_at, evidence_file_id);
CREATE INDEX evidence_pages_file_page_idx ON evidence_pages (evidence_file_id, page_number);
CREATE INDEX evidence_page_decisions_matter_status_idx ON evidence_page_decisions (matter_id, status, created_at);
CREATE INDEX evidence_page_annotations_matter_status_idx ON evidence_page_annotations (matter_id, status, created_at);
CREATE INDEX evidence_duplicate_groups_matter_status_idx ON evidence_page_duplicate_groups (matter_id, status, created_at);
CREATE INDEX evidence_manifests_matter_status_idx ON evidence_manifests (matter_id, status, created_at);
CREATE INDEX evidence_derivatives_manifest_status_idx ON evidence_derivative_artifacts (manifest_id, status, created_at);
CREATE UNIQUE INDEX evidence_derivatives_one_verified_type_per_manifest
    ON evidence_derivative_artifacts (manifest_id, artifact_type)
    WHERE status = 'VERIFIED';

ALTER TABLE evidence_original_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_original_files FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_pages FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_decisions FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_annotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_annotations FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_duplicate_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_duplicate_groups FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_duplicate_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_page_duplicate_members FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifests FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifest_pages ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifest_pages FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifest_page_annotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_manifest_page_annotations FORCE ROW LEVEL SECURITY;
ALTER TABLE evidence_derivative_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_derivative_artifacts FORCE ROW LEVEL SECURITY;

CREATE POLICY evidence_original_files_firm_isolation ON evidence_original_files USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_pages_firm_isolation ON evidence_pages USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_page_decisions_firm_isolation ON evidence_page_decisions USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_page_annotations_firm_isolation ON evidence_page_annotations USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_page_duplicate_groups_firm_isolation ON evidence_page_duplicate_groups USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_page_duplicate_members_firm_isolation ON evidence_page_duplicate_members USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_manifests_firm_isolation ON evidence_manifests USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_manifest_pages_firm_isolation ON evidence_manifest_pages USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_manifest_page_annotations_firm_isolation ON evidence_manifest_page_annotations USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY evidence_derivative_artifacts_firm_isolation ON evidence_derivative_artifacts USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
