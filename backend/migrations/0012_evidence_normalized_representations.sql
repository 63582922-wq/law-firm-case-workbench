-- Immutable encrypted PDF representations for non-PDF evidence originals.
-- Apply after 0011_evidence_intake_runs.sql.  The original remains solely in
-- the lawyer-selected folder; the representation is a managed encrypted copy.

BEGIN;

CREATE TABLE evidence_normalized_representations (
    representation_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    evidence_file_id uuid NOT NULL,
    source_media_type text NOT NULL CHECK (length(trim(source_media_type)) > 0 AND source_media_type <> 'application/pdf'),
    normalized_media_type text NOT NULL CHECK (normalized_media_type = 'application/pdf'),
    normalizer_id text NOT NULL CHECK (length(trim(normalizer_id)) BETWEEN 1 AND 160),
    normalizer_version text NOT NULL CHECK (length(trim(normalizer_version)) BETWEEN 1 AND 80),
    transform_hash char(64) NOT NULL CHECK (transform_hash ~ '^[0-9a-f]{64}$'),
    artifact_sha256 char(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    storage_object_key text NOT NULL CHECK (
        storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$'
    ),
    pdf_bytes bigint NOT NULL CHECK (pdf_bytes > 0),
    page_count integer NOT NULL CHECK (page_count > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (representation_id, firm_id, matter_id),
    UNIQUE (evidence_file_id),
    UNIQUE (matter_id, evidence_file_id, transform_hash),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)
);

CREATE INDEX evidence_normalized_representations_matter_idx
    ON evidence_normalized_representations (matter_id, evidence_file_id);

CREATE FUNCTION prohibit_evidence_normalized_representation_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence normalized representations are immutable';
END;
$$;

CREATE TRIGGER evidence_normalized_representations_no_update
    BEFORE UPDATE OR DELETE ON evidence_normalized_representations
    FOR EACH ROW EXECUTE FUNCTION prohibit_evidence_normalized_representation_change();

ALTER TABLE evidence_normalized_representations ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_normalized_representations FORCE ROW LEVEL SECURITY;

CREATE POLICY evidence_normalized_representations_firm_isolation
    ON evidence_normalized_representations
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

COMMIT;
