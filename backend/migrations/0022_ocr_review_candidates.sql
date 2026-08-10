-- Encrypted, page-scoped OCR output.  Model text is never a fact by itself.

BEGIN;

CREATE TABLE ocr_review_candidates (
    candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL REFERENCES matters(matter_id),
    external_request_id uuid NOT NULL REFERENCES external_request_authorizations(request_id),
    evidence_page_id uuid NOT NULL,
    provider_id text NOT NULL CHECK (provider_id = 'qwen'),
    source_page_sha256 char(64) NOT NULL CHECK (source_page_sha256 ~ '^[0-9a-f]{64}$'),
    content_object_key text NOT NULL CHECK (content_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\\.lca$'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    content_bytes bigint NOT NULL CHECK (content_bytes > 0 AND content_bytes <= 524288),
    provider_request_ref_hash char(64) NOT NULL CHECK (provider_request_ref_hash ~ '^[0-9a-f]{64}$'),
    review_hash char(64) NOT NULL CHECK (review_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'ACCEPTED', 'REJECTED')),
    staged_by uuid NOT NULL REFERENCES users(user_id),
    reviewed_by uuid REFERENCES users(user_id),
    reviewed_at timestamptz,
    review_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, firm_id, matter_id),
    UNIQUE (matter_id, external_request_id),
    FOREIGN KEY (external_request_id, firm_id, matter_id) REFERENCES external_request_authorizations(request_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (staged_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (reviewed_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (content_object_key = substring(content_sha256 from 1 for 2) || '/' || substring(content_sha256 from 3 for 2) || '/' || content_sha256 || '.lca'),
    CHECK ((status = 'CANDIDATE' AND reviewed_by IS NULL AND reviewed_at IS NULL AND review_reason IS NULL) OR (status IN ('ACCEPTED', 'REJECTED') AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND length(trim(review_reason)) > 0))
);

CREATE INDEX ocr_review_candidates_matter_status_idx ON ocr_review_candidates (matter_id, status, created_at DESC);
ALTER TABLE ocr_review_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE ocr_review_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY ocr_review_candidates_firm_isolation ON ocr_review_candidates USING (firm_id::text = current_setting('app.firm_id', true)) WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE FUNCTION restrict_ocr_review_candidate_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status <> 'CANDIDATE' OR NEW.status NOT IN ('ACCEPTED', 'REJECTED')
       OR (to_jsonb(NEW) - ARRAY['status', 'reviewed_by', 'reviewed_at', 'review_reason']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'reviewed_by', 'reviewed_at', 'review_reason']) THEN
        RAISE EXCEPTION 'OCR candidate permits only one exact lawyer review decision';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ocr_review_candidates_guard BEFORE UPDATE OR DELETE ON ocr_review_candidates FOR EACH ROW EXECUTE FUNCTION restrict_ocr_review_candidate_mutation();

COMMIT;
