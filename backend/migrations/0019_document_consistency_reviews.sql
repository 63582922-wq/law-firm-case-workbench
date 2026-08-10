-- Immutable, hash-bound document consistency reviews and their safe findings.
-- Review records contain no document body, canonical value or provider prompt.

BEGIN;

CREATE TABLE document_consistency_reviews (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    reviewed_matter_version integer NOT NULL CHECK (reviewed_matter_version > 0),
    canonical_fields_hash char(64) NOT NULL CHECK (canonical_fields_hash ~ '^[0-9a-f]{64}$'),
    input_hash char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    output_hash char(64) NOT NULL CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    blocking_count integer NOT NULL CHECK (blocking_count >= 0),
    warning_count integer NOT NULL CHECK (warning_count >= 0),
    status text NOT NULL CHECK (status IN ('PASS', 'BLOCKED')),
    recorded_by uuid NOT NULL REFERENCES users(user_id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (review_id, firm_id, matter_id),
    CHECK (
        (status = 'PASS' AND blocking_count = 0)
        OR (status = 'BLOCKED' AND blocking_count > 0)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (recorded_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE document_consistency_review_documents (
    review_id uuid NOT NULL,
    work_product_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    review_input_hash char(64) NOT NULL CHECK (review_input_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (review_id, work_product_id),
    UNIQUE (review_id, work_product_id, firm_id, matter_id),
    FOREIGN KEY (review_id, firm_id, matter_id)
        REFERENCES document_consistency_reviews(review_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (work_product_id, firm_id, matter_id)
        REFERENCES submission_work_products(work_product_id, firm_id, matter_id)
);

CREATE TABLE document_consistency_review_findings (
    review_id uuid NOT NULL,
    finding_id char(64) NOT NULL CHECK (finding_id ~ '^[0-9a-f]{64}$'),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    work_product_id uuid NOT NULL,
    severity text NOT NULL CHECK (severity IN ('BLOCKING', 'WARNING')),
    code text NOT NULL CHECK (code ~ '^[A-Z][A-Z0-9_]{1,119}$'),
    field_id_hash char(64),
    source_refs_hash char(64) NOT NULL CHECK (source_refs_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (review_id, finding_id),
    FOREIGN KEY (review_id, firm_id, matter_id)
        REFERENCES document_consistency_reviews(review_id, firm_id, matter_id) ON DELETE CASCADE,
    FOREIGN KEY (review_id, work_product_id, firm_id, matter_id)
        REFERENCES document_consistency_review_documents(review_id, work_product_id, firm_id, matter_id)
);

ALTER TABLE submission_compilation_specs
    ADD COLUMN consistency_review_id uuid,
    ADD COLUMN consistency_input_hash char(64),
    ADD COLUMN consistency_output_hash char(64);

ALTER TABLE submission_compilation_specs
    ADD CONSTRAINT submission_compilation_specs_consistency_hashes_valid CHECK (
        (consistency_review_id IS NULL AND consistency_input_hash IS NULL AND consistency_output_hash IS NULL)
        OR (consistency_review_id IS NOT NULL
            AND consistency_input_hash ~ '^[0-9a-f]{64}$'
            AND consistency_output_hash ~ '^[0-9a-f]{64}$')
    );

ALTER TABLE submission_compilation_specs
    ADD CONSTRAINT submission_compilation_specs_consistency_review_fk
    FOREIGN KEY (consistency_review_id, firm_id, matter_id)
    REFERENCES document_consistency_reviews(review_id, firm_id, matter_id);

CREATE INDEX document_consistency_reviews_matter_idx
    ON document_consistency_reviews (matter_id, reviewed_matter_version, status, recorded_at DESC);
CREATE INDEX document_consistency_findings_matter_idx
    ON document_consistency_review_findings (matter_id, review_id, severity, code);

ALTER TABLE document_consistency_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_consistency_reviews FORCE ROW LEVEL SECURITY;
ALTER TABLE document_consistency_review_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_consistency_review_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_consistency_review_findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_consistency_review_findings FORCE ROW LEVEL SECURITY;

CREATE POLICY document_consistency_reviews_firm_isolation ON document_consistency_reviews
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY document_consistency_review_documents_firm_isolation ON document_consistency_review_documents
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY document_consistency_review_findings_firm_isolation ON document_consistency_review_findings
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION prohibit_document_consistency_review_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'document consistency review records are append-only';
END;
$$;

CREATE TRIGGER document_consistency_reviews_append_only
    BEFORE UPDATE OR DELETE ON document_consistency_reviews
    FOR EACH ROW EXECUTE FUNCTION prohibit_document_consistency_review_mutation();
CREATE TRIGGER document_consistency_review_documents_append_only
    BEFORE UPDATE OR DELETE ON document_consistency_review_documents
    FOR EACH ROW EXECUTE FUNCTION prohibit_document_consistency_review_mutation();
CREATE TRIGGER document_consistency_review_findings_append_only
    BEFORE UPDATE OR DELETE ON document_consistency_review_findings
    FOR EACH ROW EXECUTE FUNCTION prohibit_document_consistency_review_mutation();

COMMIT;
