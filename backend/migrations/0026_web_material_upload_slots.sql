-- Durable, server-owned Web PDF material-upload slots. PostgreSQL 16+;
-- apply after 0025_web_evidence_source_objects.sql.
--
-- A browser sees only an opaque upload UUID and a short expiry.  The slot is
-- permanently bound to the already verified firm, actor, opaque Web session,
-- matter and expected matter version; no request body can choose those fields.
-- The browser never has a database credential, object key, storage version,
-- staging path, scanner payload or source-reference locator.
--
-- This table is a recovery record for the unavoidable saga
-- staging/scan -> private S3 -> immutable evidence ledger.  After S3 succeeds,
-- the server first writes the private object hand-off here, then calls the
-- existing atomic evidence original/pages/source-binding command.  If any
-- post-object-store outcome is uncertain, the object is retained and the slot
-- enters RECONCILIATION_REQUIRED; it must never be deleted based on a failed
-- or ambiguous database operation.  The Web application role remains normal
-- tenant/RLS constrained application code, not lawcase_web_session_gateway.

BEGIN;

CREATE TABLE web_material_upload_slots (
    upload_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    -- This is an opaque server-derived session UUID, not a browser token.  It
    -- intentionally has no FK to web_sessions: that table is gateway-only and
    -- adding a cross-role FK would widen its least-privilege/RLS boundary.
    session_id uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    display_name text NOT NULL,
    declared_content_length bigint,
    status text NOT NULL CHECK (status IN (
        'RESERVED', 'CLAIMED', 'OBJECT_STORED', 'COMPLETED', 'FAILED',
        'RECONCILIATION_REQUIRED'
    )),
    attempt_id uuid,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    claimed_at timestamptz,

    -- Admitted metadata and private object values are server-only.  In
    -- particular source_object_key/version must never be selected into an HTTP
    -- response, snapshot, audit payload, outbox payload, trace or browser log.
    admitted_upload_id uuid,
    admitted_content_sha256 char(64),
    admitted_byte_size bigint,
    admitted_page_count integer,
    admitted_inspection_hash char(64),
    scanner_name text,
    scanner_definitions_version text,
    source_object_key text,
    source_object_version_id text,
    source_reference_hash char(64),
    object_stored_at timestamptz,

    evidence_file_id uuid,
    evidence_matter_version integer,
    evidence_audit_event_id uuid,
    completed_at timestamptz,
    failure_code text CHECK (failure_code IS NULL OR failure_code IN (
        'CONTENT_REJECTED', 'OBJECT_STORE_FAILED', 'LEDGER_REJECTED',
        'OBJECT_STATE_UNKNOWN', 'BINDING_STATE_UNKNOWN',
        'COMPLETION_STATE_UNKNOWN'
    )),
    failed_at timestamptz,
    reconciliation_required_at timestamptz,
    updated_at timestamptz NOT NULL,

    UNIQUE (upload_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id),
    CHECK (
        octet_length(display_name) BETWEEN 1 AND 255
        AND display_name = btrim(display_name)
        AND display_name !~ '[[:cntrl:]]'
        AND display_name NOT IN ('.', '..')
        AND position('/' IN display_name) = 0
        AND position(E'\\' IN display_name) = 0
    ),
    CHECK (declared_content_length IS NULL OR declared_content_length BETWEEN 1 AND 268435456),
    CHECK (expires_at > created_at),
    CHECK (
        admitted_content_sha256 IS NULL
        OR admitted_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (
        admitted_inspection_hash IS NULL
        OR admitted_inspection_hash ~ '^[0-9a-f]{64}$'
    ),
    CHECK (admitted_byte_size IS NULL OR admitted_byte_size BETWEEN 1 AND 268435456),
    CHECK (admitted_page_count IS NULL OR admitted_page_count BETWEEN 1 AND 10000),
    CHECK (
        scanner_name IS NULL
        OR (
            octet_length(scanner_name) BETWEEN 1 AND 160
            AND scanner_name = btrim(scanner_name)
            AND scanner_name !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        scanner_definitions_version IS NULL
        OR (
            octet_length(scanner_definitions_version) BETWEEN 1 AND 160
            AND scanner_definitions_version = btrim(scanner_definitions_version)
            AND scanner_definitions_version !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        source_object_key IS NULL
        OR (
            octet_length(source_object_key) BETWEEN 1 AND 512
            AND source_object_key ~
                '^originals/v1/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$'
            AND split_part(source_object_key, '/', 3) = firm_id::text
            AND split_part(source_object_key, '/', 4) = matter_id::text
            AND split_part(source_object_key, '/', 5) = substring(admitted_content_sha256 FROM 1 FOR 2)
            AND split_part(source_object_key, '/', 6) = admitted_content_sha256
        )
    ),
    CHECK (
        source_object_version_id IS NULL
        OR (
            octet_length(source_object_version_id) BETWEEN 1 AND 512
            AND source_object_version_id = btrim(source_object_version_id)
            AND source_object_version_id !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        source_reference_hash IS NULL
        OR (
            source_reference_hash ~ '^[0-9a-f]{64}$'
            AND source_object_key IS NOT NULL
            AND source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')
        )
    ),
    CHECK (evidence_matter_version IS NULL OR evidence_matter_version > 0),
    CHECK (
        -- No partially-populated admission/object metadata is valid.
        (
            admitted_upload_id IS NULL
            AND admitted_content_sha256 IS NULL
            AND admitted_byte_size IS NULL
            AND admitted_page_count IS NULL
            AND admitted_inspection_hash IS NULL
            AND scanner_name IS NULL
            AND scanner_definitions_version IS NULL
            AND source_object_key IS NULL
            AND source_object_version_id IS NULL
            AND source_reference_hash IS NULL
            AND object_stored_at IS NULL
        )
        OR (
            admitted_upload_id IS NOT NULL
            AND admitted_content_sha256 IS NOT NULL
            AND admitted_byte_size IS NOT NULL
            AND admitted_page_count IS NOT NULL
            AND admitted_inspection_hash IS NOT NULL
            AND scanner_name IS NOT NULL
            AND scanner_definitions_version IS NOT NULL
            AND source_object_key IS NOT NULL
            AND source_reference_hash IS NOT NULL
            AND object_stored_at IS NOT NULL
        )
    ),
    CHECK (
        (status = 'RESERVED'
            AND attempt_id IS NULL AND attempt_count = 0 AND claimed_at IS NULL
            AND admitted_upload_id IS NULL AND evidence_file_id IS NULL
            AND completed_at IS NULL AND failure_code IS NULL AND failed_at IS NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'CLAIMED'
            AND attempt_id IS NOT NULL AND attempt_count = 1 AND claimed_at IS NOT NULL
            AND admitted_upload_id IS NULL AND evidence_file_id IS NULL
            AND completed_at IS NULL AND failure_code IS NULL AND failed_at IS NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'OBJECT_STORED'
            AND attempt_id IS NOT NULL AND attempt_count = 1 AND claimed_at IS NOT NULL
            AND admitted_upload_id IS NOT NULL AND evidence_file_id IS NULL
            AND completed_at IS NULL AND failure_code IS NULL AND failed_at IS NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'COMPLETED'
            AND attempt_id IS NOT NULL AND attempt_count = 1 AND claimed_at IS NOT NULL
            AND admitted_upload_id IS NOT NULL AND evidence_file_id IS NOT NULL
            AND evidence_matter_version = expected_matter_version + 1
            AND evidence_audit_event_id IS NOT NULL AND completed_at IS NOT NULL
            AND failure_code IS NULL AND failed_at IS NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'FAILED'
            AND attempt_id IS NOT NULL AND attempt_count = 1 AND claimed_at IS NOT NULL
            AND evidence_file_id IS NULL AND evidence_matter_version IS NULL
            AND evidence_audit_event_id IS NULL AND completed_at IS NULL
            AND failure_code IS NOT NULL AND failed_at IS NOT NULL
            AND reconciliation_required_at IS NULL)
        OR (status = 'RECONCILIATION_REQUIRED'
            AND attempt_id IS NOT NULL AND attempt_count = 1 AND claimed_at IS NOT NULL
            AND evidence_file_id IS NULL AND evidence_matter_version IS NULL
            AND evidence_audit_event_id IS NULL AND completed_at IS NULL
            AND failure_code IS NOT NULL AND failed_at IS NULL
            AND reconciliation_required_at IS NOT NULL)
    )
);

COMMENT ON TABLE web_material_upload_slots IS
    'Server-only recoverable saga state for browser PDF uploads; private object locators never enter HTTP/snapshots/audit/outbox.';
COMMENT ON COLUMN web_material_upload_slots.source_object_key IS
    'Private S3 object key. Never select into browser responses, evidence snapshots, audit events, outbox, logs, or frontend state.';
COMMENT ON COLUMN web_material_upload_slots.session_id IS
    'Opaque server-derived Web session UUID used only for exact owner/session replay protection.';

CREATE INDEX web_material_upload_slots_reserved_expiry_idx
    ON web_material_upload_slots (expires_at)
    WHERE status = 'RESERVED';
CREATE INDEX web_material_upload_slots_reconciliation_idx
    ON web_material_upload_slots (updated_at)
    WHERE status = 'RECONCILIATION_REQUIRED';

CREATE FUNCTION enforce_web_material_upload_slot_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'web material upload slots are retained recovery records';
    END IF;
    IF OLD.status IN ('COMPLETED', 'FAILED', 'RECONCILIATION_REQUIRED') THEN
        RAISE EXCEPTION 'terminal web material upload slots are immutable';
    END IF;
    IF (
        to_jsonb(NEW) - ARRAY[
            'status', 'attempt_id', 'attempt_count', 'claimed_at',
            'admitted_upload_id', 'admitted_content_sha256',
            'admitted_byte_size', 'admitted_page_count',
            'admitted_inspection_hash', 'scanner_name',
            'scanner_definitions_version', 'source_object_key',
            'source_object_version_id', 'source_reference_hash',
            'object_stored_at', 'evidence_file_id',
            'evidence_matter_version', 'evidence_audit_event_id',
            'completed_at', 'failure_code', 'failed_at',
            'reconciliation_required_at', 'updated_at'
        ]
    ) IS DISTINCT FROM (
        to_jsonb(OLD) - ARRAY[
            'status', 'attempt_id', 'attempt_count', 'claimed_at',
            'admitted_upload_id', 'admitted_content_sha256',
            'admitted_byte_size', 'admitted_page_count',
            'admitted_inspection_hash', 'scanner_name',
            'scanner_definitions_version', 'source_object_key',
            'source_object_version_id', 'source_reference_hash',
            'object_stored_at', 'evidence_file_id',
            'evidence_matter_version', 'evidence_audit_event_id',
            'completed_at', 'failure_code', 'failed_at',
            'reconciliation_required_at', 'updated_at'
        ]
    ) THEN
        RAISE EXCEPTION 'web material upload ownership and reservation fields are immutable';
    END IF;

    IF OLD.status = 'RESERVED' AND NEW.status = 'CLAIMED' THEN
        RETURN NEW;
    ELSIF OLD.status = 'CLAIMED' AND NEW.status IN (
        'OBJECT_STORED', 'FAILED', 'RECONCILIATION_REQUIRED'
    ) THEN
        RETURN NEW;
    ELSIF OLD.status = 'OBJECT_STORED' AND NEW.status IN (
        'COMPLETED', 'FAILED', 'RECONCILIATION_REQUIRED'
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'web material upload slot transition is not permitted';
END;
$$;

CREATE TRIGGER web_material_upload_slots_linear_state
    BEFORE UPDATE OR DELETE ON web_material_upload_slots
    FOR EACH ROW EXECUTE FUNCTION enforce_web_material_upload_slot_transition();

CREATE FUNCTION enforce_web_material_upload_completion_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'COMPLETED' AND OLD.status <> 'COMPLETED' AND NOT EXISTS (
        SELECT 1
        FROM evidence_original_files source
        JOIN web_evidence_original_source_objects binding
          ON binding.evidence_file_id = source.evidence_file_id
         AND binding.firm_id = source.firm_id
         AND binding.matter_id = source.matter_id
        WHERE source.evidence_file_id = NEW.evidence_file_id
          AND source.firm_id = NEW.firm_id
          AND source.matter_id = NEW.matter_id
          AND source.original_label = NEW.display_name
          AND source.original_file_sha256 = NEW.admitted_content_sha256
          AND source.byte_size = NEW.admitted_byte_size
          AND source.page_count = NEW.admitted_page_count
          AND source.media_type = 'application/pdf'
          AND source.source_scan_fingerprint = NEW.admitted_inspection_hash
          AND binding.source_reference_hash = NEW.source_reference_hash
          AND binding.source_object_key = NEW.source_object_key
          AND binding.source_object_version_id IS NOT DISTINCT FROM NEW.source_object_version_id
    ) THEN
        RAISE EXCEPTION 'completed Web material upload must match its immutable evidence object binding';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER web_material_upload_slots_completion_binding
    BEFORE UPDATE ON web_material_upload_slots
    FOR EACH ROW EXECUTE FUNCTION enforce_web_material_upload_completion_binding();

CREATE TABLE web_material_upload_slot_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id uuid NOT NULL,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    session_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'RESERVED', 'CLAIMED', 'OBJECT_STORED', 'COMPLETED', 'FAILED',
        'RECONCILIATION_REQUIRED'
    )),
    attempt_id uuid,
    source_reference_hash char(64) CHECK (
        source_reference_hash IS NULL OR source_reference_hash ~ '^[0-9a-f]{64}$'
    ),
    terminal_code text CHECK (terminal_code IS NULL OR terminal_code IN (
        'CONTENT_REJECTED', 'OBJECT_STORE_FAILED', 'LEDGER_REJECTED',
        'OBJECT_STATE_UNKNOWN', 'BINDING_STATE_UNKNOWN',
        'COMPLETION_STATE_UNKNOWN'
    )),
    occurred_at timestamptz NOT NULL,
    FOREIGN KEY (upload_id, firm_id, matter_id)
        REFERENCES web_material_upload_slots(upload_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (event_type = 'RESERVED' AND attempt_id IS NULL AND source_reference_hash IS NULL AND terminal_code IS NULL)
        OR (event_type = 'CLAIMED' AND attempt_id IS NOT NULL AND source_reference_hash IS NULL AND terminal_code IS NULL)
        OR (event_type = 'OBJECT_STORED' AND attempt_id IS NOT NULL AND source_reference_hash IS NOT NULL AND terminal_code IS NULL)
        OR (event_type = 'COMPLETED' AND attempt_id IS NOT NULL AND source_reference_hash IS NOT NULL AND terminal_code IS NULL)
        OR (event_type = 'FAILED' AND attempt_id IS NOT NULL AND terminal_code IS NOT NULL)
        OR (event_type = 'RECONCILIATION_REQUIRED' AND attempt_id IS NOT NULL AND terminal_code IS NOT NULL)
    )
);

COMMENT ON TABLE web_material_upload_slot_events IS
    'Append-only server lifecycle audit for upload slots; it contains no object key, object version, path, scanner payload, or browser token.';

CREATE FUNCTION prohibit_web_material_upload_slot_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'web material upload slot events are append-only';
END;
$$;

CREATE TRIGGER web_material_upload_slot_events_append_only
    BEFORE UPDATE OR DELETE ON web_material_upload_slot_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_web_material_upload_slot_event_mutation();

ALTER TABLE web_material_upload_slots ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_material_upload_slots FORCE ROW LEVEL SECURITY;
ALTER TABLE web_material_upload_slot_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_material_upload_slot_events FORCE ROW LEVEL SECURITY;

CREATE POLICY web_material_upload_slots_firm_isolation
    ON web_material_upload_slots
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));
CREATE POLICY web_material_upload_slot_events_firm_isolation
    ON web_material_upload_slot_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

-- Browsers have no database credentials.  Deployment grants a normal,
-- non-BYPASSRLS tenant application role only the narrow server-side table
-- privileges it needs; this migration never grants any access to PUBLIC.
REVOKE ALL ON TABLE web_material_upload_slots FROM PUBLIC;
REVOKE ALL ON TABLE web_material_upload_slot_events FROM PUBLIC;

COMMIT;
