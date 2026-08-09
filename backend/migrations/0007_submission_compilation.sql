-- Court submission work products, immutable compilation specs and verified exports.
-- The court ZIP and internal audit manifest are separate encrypted artifacts.

BEGIN;

ALTER TABLE approvals
    ADD CONSTRAINT approvals_id_firm_matter_unique
    UNIQUE (approval_id, firm_id, matter_id);

CREATE TABLE submission_work_products (
    work_product_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    document_kind text NOT NULL CHECK (length(trim(document_kind)) > 0),
    audience text NOT NULL CHECK (audience IN ('COURT_SUBMISSION', 'INTERNAL_ONLY')),
    media_type text NOT NULL CHECK (media_type IN (
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )),
    storage_object_key text NOT NULL CHECK (
        storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    artifact_sha256 char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size > 0 AND byte_size <= 134217728),
    page_count integer CHECK (page_count IS NULL OR page_count > 0),
    semantic_text_sha256 char(64) CHECK (
        semantic_text_sha256 IS NULL OR semantic_text_sha256 ~ '^[0-9a-f]{64}$'
    ),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'STALE', 'REVOKED')),
    registered_by uuid NOT NULL REFERENCES users(user_id),
    approved_by uuid REFERENCES users(user_id),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz,
    stale_at timestamptz,
    stale_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (work_product_id, firm_id, matter_id),
    CHECK (
        storage_object_key = substring(artifact_sha256 from 1 for 2) || '/' ||
            substring(artifact_sha256 from 3 for 2) || '/' || artifact_sha256 || '.lca'
    ),
    CHECK (
        (status = 'CANDIDATE' AND approved_by IS NULL AND approval_hash IS NULL
            AND approved_at IS NULL AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status = 'APPROVED' AND approved_by IS NOT NULL AND approval_hash IS NOT NULL
            AND approved_at IS NOT NULL AND stale_at IS NULL AND stale_reason IS NULL)
        OR (status IN ('STALE', 'REVOKED') AND approved_by IS NOT NULL AND approval_hash IS NOT NULL
            AND approved_at IS NOT NULL AND stale_at IS NOT NULL
            AND stale_reason IS NOT NULL AND length(trim(stale_reason)) > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (registered_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE submission_compilation_specs (
    bundle_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    export_profile text NOT NULL CHECK (export_profile = 'COURT_PDF_ONLY_V1'),
    currency char(3) NOT NULL CHECK (currency = 'CNY'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    required_document_kinds jsonb NOT NULL CHECK (
        jsonb_typeof(required_document_kinds) = 'array'
        AND jsonb_array_length(required_document_kinds) > 0
    ),
    evidence_manifest_id uuid NOT NULL,
    evidence_manifest_hash char(64) NOT NULL CHECK (evidence_manifest_hash ~ '^[0-9a-f]{64}$'),
    legal_bundle_id uuid NOT NULL,
    legal_bundle_hash char(64) NOT NULL CHECK (legal_bundle_hash ~ '^[0-9a-f]{64}$'),
    calculation_run_id uuid NOT NULL,
    calculation_output_hash char(64) NOT NULL CHECK (calculation_output_hash ~ '^[0-9a-f]{64}$'),
    final_text_approval_id uuid NOT NULL,
    final_text_hash char(64) NOT NULL CHECK (final_text_hash ~ '^[0-9a-f]{64}$'),
    qa_hash char(64) NOT NULL CHECK (qa_hash ~ '^[0-9a-f]{64}$'),
    qa_approved_by uuid NOT NULL REFERENCES users(user_id),
    qa_approved_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bundle_id, firm_id, matter_id),
    FOREIGN KEY (bundle_id, firm_id, matter_id)
        REFERENCES submission_bundles(bundle_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_manifest_id, firm_id, matter_id)
        REFERENCES evidence_manifests(manifest_id, firm_id, matter_id),
    FOREIGN KEY (legal_bundle_id, firm_id, matter_id)
        REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id),
    FOREIGN KEY (calculation_run_id, firm_id, matter_id)
        REFERENCES calculation_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (final_text_approval_id, firm_id, matter_id)
        REFERENCES approvals(approval_id, firm_id, matter_id),
    FOREIGN KEY (qa_approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE submission_bundle_components (
    bundle_id uuid NOT NULL,
    work_product_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    document_kind text NOT NULL CHECK (length(trim(document_kind)) > 0),
    court_filename text NOT NULL CHECK (
        length(trim(court_filename)) > 4
        AND court_filename !~ '[\\/]'
        AND court_filename ~ '\.pdf$'
        AND court_filename !~* '(最新|最终|修订|终稿|定稿|第[[:space:]]*[0-9]+[[:space:]]*版|v[0-9]+|final)'
    ),
    media_type text NOT NULL CHECK (media_type = 'application/pdf'),
    storage_object_key text NOT NULL CHECK (
        storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    artifact_sha256 char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size > 0 AND byte_size <= 134217728),
    approval_hash char(64) NOT NULL CHECK (approval_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bundle_id, work_product_id),
    UNIQUE (bundle_id, sequence),
    UNIQUE (bundle_id, court_filename),
    CHECK (
        storage_object_key = substring(artifact_sha256 from 1 for 2) || '/' ||
            substring(artifact_sha256 from 3 for 2) || '/' || artifact_sha256 || '.lca'
    ),
    FOREIGN KEY (bundle_id, firm_id, matter_id)
        REFERENCES submission_compilation_specs(bundle_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (work_product_id, firm_id, matter_id)
        REFERENCES submission_work_products(work_product_id, firm_id, matter_id)
);

CREATE TABLE submission_compilation_exports (
    export_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bundle_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    court_zip_object_key text NOT NULL CHECK (
        court_zip_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    court_zip_sha256 char(64) NOT NULL CHECK (court_zip_sha256 ~ '^[0-9a-f]{64}$'),
    court_zip_bytes bigint NOT NULL CHECK (court_zip_bytes > 0 AND court_zip_bytes <= 268435456),
    internal_manifest_object_key text NOT NULL CHECK (
        internal_manifest_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    internal_manifest_sha256 char(64) NOT NULL CHECK (internal_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    component_count integer NOT NULL CHECK (component_count > 0 AND component_count <= 100),
    verification_hash char(64) NOT NULL CHECK (verification_hash ~ '^[0-9a-f]{64}$'),
    verified_by uuid NOT NULL REFERENCES users(user_id),
    verified_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bundle_id),
    UNIQUE (export_id, firm_id, matter_id),
    CHECK (
        court_zip_object_key = substring(court_zip_sha256 from 1 for 2) || '/' ||
            substring(court_zip_sha256 from 3 for 2) || '/' || court_zip_sha256 || '.lca'
    ),
    CHECK (
        internal_manifest_object_key = substring(internal_manifest_sha256 from 1 for 2) || '/' ||
            substring(internal_manifest_sha256 from 3 for 2) || '/' || internal_manifest_sha256 || '.lca'
    ),
    FOREIGN KEY (bundle_id, firm_id, matter_id)
        REFERENCES submission_compilation_specs(bundle_id, firm_id, matter_id),
    FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX submission_work_products_matter_idx
    ON submission_work_products (matter_id, status, document_kind, created_at DESC);
CREATE INDEX submission_bundle_components_matter_idx
    ON submission_bundle_components (matter_id, bundle_id, sequence);
CREATE INDEX submission_compilation_exports_matter_idx
    ON submission_compilation_exports (matter_id, verified_at DESC);

ALTER TABLE submission_work_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_work_products FORCE ROW LEVEL SECURITY;
ALTER TABLE submission_compilation_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_compilation_specs FORCE ROW LEVEL SECURITY;
ALTER TABLE submission_bundle_components ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_bundle_components FORCE ROW LEVEL SECURITY;
ALTER TABLE submission_compilation_exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE submission_compilation_exports FORCE ROW LEVEL SECURITY;

CREATE POLICY submission_work_products_firm_isolation ON submission_work_products
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY submission_compilation_specs_firm_isolation ON submission_compilation_specs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY submission_bundle_components_firm_isolation ON submission_bundle_components
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY submission_compilation_exports_firm_isolation ON submission_compilation_exports
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_submission_compilation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'locked submission compilation records are append-only';
END;
$$;

CREATE TRIGGER submission_compilation_specs_append_only
    BEFORE UPDATE OR DELETE ON submission_compilation_specs
    FOR EACH ROW EXECUTE FUNCTION prohibit_submission_compilation_mutation();
CREATE TRIGGER submission_bundle_components_append_only
    BEFORE UPDATE OR DELETE ON submission_bundle_components
    FOR EACH ROW EXECUTE FUNCTION prohibit_submission_compilation_mutation();
CREATE TRIGGER submission_compilation_exports_append_only
    BEFORE UPDATE OR DELETE ON submission_compilation_exports
    FOR EACH ROW EXECUTE FUNCTION prohibit_submission_compilation_mutation();

COMMIT;
