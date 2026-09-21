-- ZIP archive admission is a durable hand-off, not evidence completion.
-- Child PDFs must later pass the ordinary scan/ledger worker before they count
-- as case materials.
-- The browser projection for OBJECT_STORED is named STORED_PENDING_PROCESSING.
CREATE TABLE web_material_archive_uploads (
    archive_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    session_id uuid NOT NULL,
    expected_matter_version bigint NOT NULL CHECK (expected_matter_version > 0),
    display_name text NOT NULL,
    declared_content_length bigint,
    status text NOT NULL CHECK (status IN ('RESERVED', 'CLAIMED', 'OBJECT_STORED', 'RECONCILIATION_REQUIRED')),
    attempt_id uuid,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    archive_content_sha256 char(64),
    archive_byte_size bigint,
    entry_count integer,
    expanded_byte_size bigint,
    inventory_json jsonb,
    source_object_key text,
    source_object_version_id text,
    source_reference_hash char(64),
    object_stored_at timestamptz,
    reconciliation_required_at timestamptz,
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (display_name = btrim(display_name) AND octet_length(display_name) BETWEEN 5 AND 255
           AND display_name !~ '[[:cntrl:]]' AND display_name !~ '[\\/]'
           AND lower(display_name) LIKE '%.zip'),
    CHECK (declared_content_length IS NULL OR declared_content_length BETWEEN 1 AND 268435456),
    CHECK (expires_at > created_at),
    CHECK (archive_content_sha256 IS NULL OR archive_content_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (archive_byte_size IS NULL OR archive_byte_size BETWEEN 1 AND 268435456),
    CHECK (entry_count IS NULL OR entry_count BETWEEN 1 AND 1000),
    CHECK (expanded_byte_size IS NULL OR expanded_byte_size BETWEEN 1 AND 1073741824),
    CHECK (source_reference_hash IS NULL OR source_reference_hash ~ '^[0-9a-f]{64}$'),
    CHECK (
        (status = 'RESERVED' AND attempt_id IS NULL AND attempt_count = 0
         AND archive_content_sha256 IS NULL AND source_object_key IS NULL
         AND reconciliation_required_at IS NULL)
        OR (status = 'CLAIMED' AND attempt_id IS NOT NULL AND attempt_count = 1
            AND archive_content_sha256 IS NULL AND source_object_key IS NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'OBJECT_STORED' AND attempt_id IS NOT NULL AND attempt_count = 1
            AND archive_content_sha256 IS NOT NULL AND archive_byte_size IS NOT NULL
            AND entry_count IS NOT NULL AND expanded_byte_size IS NOT NULL
            AND inventory_json IS NOT NULL AND source_object_key IS NOT NULL
            AND object_stored_at IS NOT NULL AND reconciliation_required_at IS NULL)
        OR (status = 'RECONCILIATION_REQUIRED' AND attempt_id IS NOT NULL AND attempt_count = 1
            AND archive_content_sha256 IS NULL AND source_object_key IS NULL
            AND reconciliation_required_at IS NOT NULL)
    )
);

CREATE TABLE web_material_archive_upload_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    archive_id uuid NOT NULL REFERENCES web_material_archive_uploads(archive_id),
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('RESERVED', 'CLAIMED', 'OBJECT_STORED', 'RECONCILIATION_REQUIRED')),
    attempt_id uuid,
    source_reference_hash char(64),
    occurred_at timestamptz NOT NULL,
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE FUNCTION prohibit_web_material_archive_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'web material archive upload events are append-only';
END;
$$;
CREATE TRIGGER web_material_archive_upload_events_append_only
    BEFORE UPDATE OR DELETE ON web_material_archive_upload_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_web_material_archive_event_mutation();

CREATE FUNCTION enforce_web_material_archive_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'OBJECT_STORED' OR OLD.status = 'RECONCILIATION_REQUIRED' THEN
        RAISE EXCEPTION 'terminal Web material archive upload is immutable';
    END IF;
    IF OLD.status = 'RESERVED' AND NEW.status = 'CLAIMED' THEN RETURN NEW; END IF;
    IF OLD.status = 'CLAIMED' AND NEW.status IN ('OBJECT_STORED', 'RECONCILIATION_REQUIRED') THEN RETURN NEW; END IF;
    RAISE EXCEPTION 'Web material archive upload transition is not permitted';
END;
$$;
CREATE TRIGGER web_material_archive_upload_linear_state
    BEFORE UPDATE OR DELETE ON web_material_archive_uploads
    FOR EACH ROW EXECUTE FUNCTION enforce_web_material_archive_transition();

ALTER TABLE web_material_archive_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_material_archive_uploads FORCE ROW LEVEL SECURITY;
ALTER TABLE web_material_archive_upload_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_material_archive_upload_events FORCE ROW LEVEL SECURITY;

CREATE INDEX web_material_archive_uploads_reconciliation_idx
    ON web_material_archive_uploads(updated_at)
    WHERE status = 'RECONCILIATION_REQUIRED';
