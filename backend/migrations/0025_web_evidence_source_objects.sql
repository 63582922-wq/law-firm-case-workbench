-- Private object-store bindings for Web-uploaded PDF evidence. PostgreSQL 16+;
-- apply after 0024_web_sessions.sql.
--
-- A browser upload is first admitted and written to private object storage,
-- then this table is inserted in the *same* transaction as its immutable
-- evidence_original_files row and evidence_pages rows. The object key is an
-- internal storage capability: it must never be selected into evidence
-- snapshots, HTTP responses, command receipts, audit payloads, or outbox
-- payloads. Only server composition / SYSTEM_WORKER code may materialize it.
--
-- This is deliberately additive. Existing desktop local-folder originals keep
-- their current registration and page-access semantics and have no row here.
-- S3/object storage cannot share a transaction with PostgreSQL. If a later
-- bind fails, the server coordinator must reconcile the source_reference_hash
-- first and delete only a proven-unbound object; an unavailable/ambiguous DB
-- result is retained for server-side orphan reconciliation, never deleted.

BEGIN;

CREATE TABLE web_evidence_original_source_objects (
    evidence_file_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_object_key text NOT NULL,
    source_object_version_id text,
    source_object_sha256 char(64) NOT NULL CHECK (source_object_sha256 ~ '^[0-9a-f]{64}$'),
    source_object_bytes bigint NOT NULL CHECK (source_object_bytes > 0),
    -- A one-way SHA-256 of source_object_key. It is the only storage-reference
    -- value allowed into worker provenance; it is not an object locator.
    source_reference_hash char(64) NOT NULL CHECK (source_reference_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evidence_file_id, firm_id, matter_id),
    UNIQUE (source_reference_hash),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id),
    CHECK (
        source_object_key ~
            '^originals/v1/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$'
    ),
    CHECK (split_part(source_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(source_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(source_object_key, '/', 5) = substring(source_object_sha256 from 1 for 2)),
    CHECK (split_part(source_object_key, '/', 6) = source_object_sha256),
    CHECK (source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')),
    CHECK (
        source_object_version_id IS NULL
        OR (
            length(source_object_version_id) BETWEEN 1 AND 512
            AND source_object_version_id = btrim(source_object_version_id)
            AND source_object_version_id !~ '[[:cntrl:]]'
        )
    )
);

COMMENT ON TABLE web_evidence_original_source_objects IS
    'Server-only private object-storage binding for a Web-uploaded evidence original; never browser-addressable.';
COMMENT ON COLUMN web_evidence_original_source_objects.source_object_key IS
    'Private object key. Never include in snapshots, HTTP, command receipts, audit, or outbox payloads.';
COMMENT ON COLUMN web_evidence_original_source_objects.source_reference_hash IS
    'SHA-256 of the private object key, safe only as non-locator worker provenance.';

CREATE INDEX web_evidence_original_source_objects_matter_file_idx
    ON web_evidence_original_source_objects (matter_id, evidence_file_id);

CREATE FUNCTION enforce_web_evidence_source_object_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM evidence_original_files source
        WHERE source.evidence_file_id = NEW.evidence_file_id
          AND source.firm_id = NEW.firm_id
          AND source.matter_id = NEW.matter_id
          AND source.original_file_sha256 = NEW.source_object_sha256
          AND source.byte_size = NEW.source_object_bytes
          AND source.media_type = 'application/pdf'
    ) THEN
        RAISE EXCEPTION 'Web private source object must match its PDF evidence original';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER web_evidence_original_source_objects_integrity
    BEFORE INSERT ON web_evidence_original_source_objects
    FOR EACH ROW EXECUTE FUNCTION enforce_web_evidence_source_object_integrity();

CREATE FUNCTION prohibit_web_evidence_source_object_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Web evidence source-object bindings are append-only';
END;
$$;

CREATE TRIGGER web_evidence_original_source_objects_append_only
    BEFORE UPDATE OR DELETE ON web_evidence_original_source_objects
    FOR EACH ROW EXECUTE FUNCTION prohibit_web_evidence_source_object_change();

ALTER TABLE web_evidence_original_source_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_evidence_original_source_objects FORCE ROW LEVEL SECURITY;

CREATE POLICY web_evidence_original_source_objects_firm_isolation
    ON web_evidence_original_source_objects
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

-- Browser code never has database credentials. This explicit revoke protects
-- against accidental deployment defaults; the server application role keeps
-- its separately provisioned, RLS-constrained table privilege.
REVOKE ALL ON TABLE web_evidence_original_source_objects FROM PUBLIC;

COMMIT;
