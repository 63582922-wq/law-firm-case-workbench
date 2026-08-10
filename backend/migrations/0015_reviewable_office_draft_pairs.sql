-- Immutable lawyer-review pairs for generated Word/Excel work.
-- A pair keeps the editable Office source and its independently rendered PDF
-- together.  It is intentionally not a court-submission work product.

BEGIN;

CREATE TABLE reviewable_office_draft_pairs (
    pair_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    document_kind text NOT NULL CHECK (length(trim(document_kind)) > 0),
    editable_media_type text NOT NULL CHECK (editable_media_type IN (
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )),
    editable_object_key text NOT NULL CHECK (
        editable_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    editable_sha256 char(64) NOT NULL CHECK (editable_sha256 ~ '^[0-9a-f]{64}$'),
    editable_bytes bigint NOT NULL CHECK (editable_bytes > 0 AND editable_bytes <= 67108864),
    review_pdf_object_key text NOT NULL CHECK (
        review_pdf_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    review_pdf_sha256 char(64) NOT NULL CHECK (review_pdf_sha256 ~ '^[0-9a-f]{64}$'),
    review_pdf_bytes bigint NOT NULL CHECK (review_pdf_bytes > 0 AND review_pdf_bytes <= 134217728),
    review_pdf_page_count integer NOT NULL CHECK (review_pdf_page_count > 0),
    approval_input_hash char(64) NOT NULL CHECK (approval_input_hash ~ '^[0-9a-f]{64}$'),
    render_verification_hash char(64) NOT NULL CHECK (render_verification_hash ~ '^[0-9a-f]{64}$'),
    review_input_hash char(64) NOT NULL CHECK (review_input_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED')),
    registered_by uuid NOT NULL REFERENCES users(user_id),
    approved_by uuid REFERENCES users(user_id),
    approval_hash char(64) CHECK (approval_hash IS NULL OR approval_hash ~ '^[0-9a-f]{64}$'),
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pair_id, firm_id, matter_id),
    UNIQUE (matter_id, review_input_hash),
    CHECK (
        editable_object_key = substring(editable_sha256 from 1 for 2) || '/' ||
            substring(editable_sha256 from 3 for 2) || '/' || editable_sha256 || '.lca'
    ),
    CHECK (
        review_pdf_object_key = substring(review_pdf_sha256 from 1 for 2) || '/' ||
            substring(review_pdf_sha256 from 3 for 2) || '/' || review_pdf_sha256 || '.lca'
    ),
    CHECK (
        (status = 'CANDIDATE' AND approved_by IS NULL AND approval_hash IS NULL AND approved_at IS NULL)
        OR (status = 'APPROVED' AND approved_by IS NOT NULL AND approval_hash IS NOT NULL
            AND approval_hash = review_input_hash AND approved_at IS NOT NULL)
    ),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (registered_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (approved_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX reviewable_office_draft_pairs_matter_idx
    ON reviewable_office_draft_pairs (matter_id, status, document_kind, created_at DESC);

ALTER TABLE reviewable_office_draft_pairs ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviewable_office_draft_pairs FORCE ROW LEVEL SECURITY;

CREATE POLICY reviewable_office_draft_pairs_firm_isolation ON reviewable_office_draft_pairs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION restrict_reviewable_office_draft_pair_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'reviewable Office draft pairs cannot be deleted';
    END IF;
    IF OLD.status <> 'CANDIDATE' OR NEW.status <> 'APPROVED'
       OR NEW.approval_hash IS DISTINCT FROM OLD.review_input_hash
       OR NEW.approved_by IS NULL OR NEW.approved_at IS NULL
       OR (to_jsonb(NEW) - ARRAY['status', 'approved_by', 'approval_hash', 'approved_at'])
          IS DISTINCT FROM
          (to_jsonb(OLD) - ARRAY['status', 'approved_by', 'approval_hash', 'approved_at']) THEN
        RAISE EXCEPTION 'reviewable Office draft pairs permit only exact lawyer approval';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER reviewable_office_draft_pairs_guard
    BEFORE UPDATE OR DELETE ON reviewable_office_draft_pairs
    FOR EACH ROW EXECUTE FUNCTION restrict_reviewable_office_draft_pair_mutation();

COMMIT;
