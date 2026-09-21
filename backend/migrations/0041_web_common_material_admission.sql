-- Immutable admission ledger for common non-PDF Web case materials.
-- PostgreSQL 16+; apply after 0040_case_agent_document_draft_exchange.sql.
--
-- Supported here: DOCX/XLSX/PPTX/RTF/TXT/CSV/HTML/EML/JPEG/PNG.
-- PDF remains on the existing evidence-original/page chain.  Legacy OLE
-- DOC/XLS/PPT/MSG, OFD and unknown formats are deliberately absent from every
-- CHECK constraint and therefore cannot be mislabeled as supported.
--
-- The browser never sees an object key/version, scanner payload, staging path,
-- audit payload or outbox payload.  A material object is an immutable source
-- candidate only; five CHECKed false flags prevent admission from becoming a
-- formal fact, transaction, legal conclusion, evidence decision or court-ready
-- artifact.  All three tables use FORCE RLS.

BEGIN;

CREATE TABLE web_common_material_uploads (
    upload_id uuid PRIMARY KEY,
    material_object_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    session_id uuid NOT NULL,
    expected_matter_version integer NOT NULL CHECK (expected_matter_version > 0),
    display_name text NOT NULL,
    declared_byte_size bigint NOT NULL CHECK (declared_byte_size BETWEEN 1 AND 104857600),
    declared_media_type text NOT NULL,
    reserve_idempotency_key text NOT NULL,
    reserve_request_hash char(64) NOT NULL CHECK (reserve_request_hash ~ '^[0-9a-f]{64}$'),
    content_idempotency_key text,
    status text NOT NULL CHECK (status IN (
        'RESERVED', 'CLAIMED', 'OBJECT_STORED', 'COMPLETED', 'FAILED',
        'RECONCILIATION_REQUIRED'
    )),
    attempt_id uuid,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    claimed_at timestamptz,

    admitted_format text CHECK (admitted_format IS NULL OR admitted_format IN (
        'DOCX', 'XLSX', 'PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML', 'JPEG', 'PNG'
    )),
    canonical_kind text CHECK (canonical_kind IS NULL OR canonical_kind IN (
        'WORD_DOCUMENT', 'SPREADSHEET', 'PRESENTATION', 'TEXT', 'EMAIL', 'IMAGE'
    )),
    admitted_media_type text,
    route text CHECK (route IS NULL OR route IN ('COMMON_DOCUMENT_READER', 'VISUAL_OCR')),
    admitted_byte_size bigint CHECK (admitted_byte_size IS NULL OR admitted_byte_size BETWEEN 1 AND 104857600),
    admitted_content_sha256 char(64) CHECK (
        admitted_content_sha256 IS NULL OR admitted_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    admitted_inspection_hash char(64) CHECK (
        admitted_inspection_hash IS NULL OR admitted_inspection_hash ~ '^[0-9a-f]{64}$'
    ),
    scanner_name text,
    scanner_definitions_version text,
    review_flags jsonb,
    review_status text CHECK (review_status IS NULL OR review_status = 'NEEDS_LAWYER_REVIEW'),
    formal_fact boolean,
    formal_transaction boolean,
    legal_conclusion boolean,
    evidence_decision boolean,
    court_ready boolean,

    -- Private server fields: never project them into an HTTP response, audit,
    -- outbox, model prompt or browser log.
    source_object_key text,
    source_object_version_id text,
    source_reference_hash char(64),
    object_stored_at timestamptz,

    result_matter_version integer CHECK (result_matter_version IS NULL OR result_matter_version > 0),
    agent_status text CHECK (agent_status IS NULL OR agent_status IN (
        'AGENT_READY', 'INGESTED_PENDING_ADAPTER'
    )),
    agent_source_ref text CHECK (
        agent_source_ref IS NULL OR agent_source_ref ~
        '^(material-object|evidence-page):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ),
    audit_event_id uuid REFERENCES audit_events(event_id),
    outbox_id uuid REFERENCES outbox_events(outbox_id),
    completed_at timestamptz,
    failure_code text CHECK (failure_code IS NULL OR failure_code IN (
        'CONTENT_REJECTED', 'ADMISSION_UNAVAILABLE', 'OBJECT_STATE_UNKNOWN', 'OBJECT_HANDOFF_UNKNOWN',
        'REGISTRATION_STATE_UNKNOWN'
    )),
    terminal_at timestamptz,
    updated_at timestamptz NOT NULL,

    UNIQUE (upload_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, actor_id, reserve_idempotency_key),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        octet_length(display_name) BETWEEN 1 AND 255
        AND display_name = btrim(display_name)
        AND display_name !~ '[[:cntrl:]]'
        AND display_name NOT IN ('.', '..')
        AND position('/' IN display_name) = 0
        AND position(E'\\' IN display_name) = 0
        AND coalesce(lower(substring(display_name FROM '\.[^.]+$')), '') IN (
            '.docx', '.xlsx', '.pptx', '.rtf', '.txt', '.csv', '.html', '.htm',
            '.eml', '.jpg', '.jpeg', '.jpe', '.png'
        )
    ),
    CHECK (
        octet_length(declared_media_type) BETWEEN 3 AND 160
        AND declared_media_type = lower(btrim(declared_media_type))
        AND declared_media_type !~ '[[:cntrl:]]'
    ),
    CHECK (
        octet_length(reserve_idempotency_key) BETWEEN 8 AND 160
        AND reserve_idempotency_key = btrim(reserve_idempotency_key)
        AND reserve_idempotency_key ~ '^[!-~]+$'
    ),
    CHECK (
        content_idempotency_key IS NULL OR (
            octet_length(content_idempotency_key) BETWEEN 8 AND 160
            AND content_idempotency_key = btrim(content_idempotency_key)
            AND content_idempotency_key ~ '^[!-~]+$'
        )
    ),
    CHECK (expires_at > created_at),
    CHECK (
        admitted_media_type IS NULL OR (
            octet_length(admitted_media_type) BETWEEN 3 AND 160
            AND admitted_media_type = lower(btrim(admitted_media_type))
            AND admitted_media_type !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        scanner_name IS NULL OR (
            octet_length(scanner_name) BETWEEN 1 AND 160
            AND scanner_name = btrim(scanner_name)
            AND scanner_name !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        scanner_definitions_version IS NULL OR (
            octet_length(scanner_definitions_version) BETWEEN 1 AND 160
            AND scanner_definitions_version = btrim(scanner_definitions_version)
            AND scanner_definitions_version !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        review_flags IS NULL OR (
            jsonb_typeof(review_flags) = 'array'
            AND jsonb_array_length(review_flags) <= 100
        )
    ),
    CHECK (
        source_object_key IS NULL OR (
            octet_length(source_object_key) BETWEEN 1 AND 700
            AND split_part(source_object_key, '/', 3) = firm_id::text
            AND split_part(source_object_key, '/', 4) = matter_id::text
            AND (
                (route = 'COMMON_DOCUMENT_READER'
                    AND source_object_key ~
                        '^case-materials/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}$'
                    AND split_part(source_object_key, '/', 5) = substring(admitted_content_sha256 from 1 for 2)
                    AND split_part(source_object_key, '/', 6) = admitted_content_sha256)
                OR (route = 'VISUAL_OCR'
                    AND source_object_key ~
                        '^original-images/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f-]{36}\.(jpg|png)$'
                    AND split_part(source_object_key, '/', 5) = substring(admitted_content_sha256 from 1 for 2)
                    AND split_part(source_object_key, '/', 6) = admitted_content_sha256
                    AND split_part(source_object_key, '/', 7) = material_object_id::text ||
                        CASE admitted_format WHEN 'JPEG' THEN '.jpg' ELSE '.png' END)
            )
        )
    ),
    CHECK (
        source_object_version_id IS NULL OR (
            octet_length(source_object_version_id) BETWEEN 1 AND 512
            AND source_object_version_id = btrim(source_object_version_id)
            AND source_object_version_id !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        source_reference_hash IS NULL OR (
            source_reference_hash ~ '^[0-9a-f]{64}$'
            AND source_object_key IS NOT NULL
            AND source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')
        )
    ),
    CHECK (
        (admitted_format IS NULL
            AND canonical_kind IS NULL AND admitted_media_type IS NULL AND route IS NULL
            AND admitted_byte_size IS NULL AND admitted_content_sha256 IS NULL
            AND admitted_inspection_hash IS NULL AND scanner_name IS NULL
            AND scanner_definitions_version IS NULL AND review_flags IS NULL
            AND review_status IS NULL AND formal_fact IS NULL
            AND formal_transaction IS NULL AND legal_conclusion IS NULL
            AND evidence_decision IS NULL AND court_ready IS NULL)
        OR (admitted_format IS NOT NULL
            AND admitted_byte_size = declared_byte_size
            AND admitted_content_sha256 IS NOT NULL
            AND admitted_inspection_hash IS NOT NULL
            AND admitted_media_type IS NOT NULL
            AND canonical_kind IS NOT NULL
            AND route IS NOT NULL
            AND scanner_name IS NOT NULL
            AND scanner_definitions_version IS NOT NULL
            AND review_flags IS NOT NULL
            AND review_status = 'NEEDS_LAWYER_REVIEW'
            AND formal_fact = false
            AND formal_transaction = false
            AND legal_conclusion = false
            AND evidence_decision = false
            AND court_ready = false
        )
    ),
    CHECK (
        admitted_format IS NULL OR (
            (admitted_format = 'DOCX' AND canonical_kind = 'WORD_DOCUMENT'
                AND admitted_media_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'XLSX' AND canonical_kind = 'SPREADSHEET'
                AND admitted_media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'PPTX' AND canonical_kind = 'PRESENTATION'
                AND admitted_media_type = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
                AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'RTF' AND canonical_kind = 'TEXT'
                AND admitted_media_type = 'application/rtf' AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'TXT' AND canonical_kind = 'TEXT'
                AND admitted_media_type = 'text/plain' AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'CSV' AND canonical_kind = 'TEXT'
                AND admitted_media_type = 'text/csv' AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'HTML' AND canonical_kind = 'TEXT'
                AND admitted_media_type = 'text/html' AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'EML' AND canonical_kind = 'EMAIL'
                AND admitted_media_type = 'message/rfc822' AND route = 'COMMON_DOCUMENT_READER')
            OR (admitted_format = 'JPEG' AND canonical_kind = 'IMAGE'
                AND admitted_media_type = 'image/jpeg' AND route = 'VISUAL_OCR')
            OR (admitted_format = 'PNG' AND canonical_kind = 'IMAGE'
                AND admitted_media_type = 'image/png' AND route = 'VISUAL_OCR')
        )
    ),
    CHECK (route IS NULL OR route <> 'VISUAL_OCR' OR admitted_byte_size <= 67108864),
    CHECK (
        (status <> 'COMPLETED' AND agent_status IS NULL AND agent_source_ref IS NULL)
        OR (status = 'COMPLETED' AND (
            (admitted_format IN ('DOCX', 'XLSX')
                AND agent_status = 'AGENT_READY'
                AND agent_source_ref = 'material-object:' || material_object_id::text)
            OR (admitted_format IN ('JPEG', 'PNG')
                AND agent_status = 'AGENT_READY'
                AND agent_source_ref ~ '^evidence-page:')
            OR (admitted_format IN ('PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML')
                AND agent_status = 'INGESTED_PENDING_ADAPTER'
                AND agent_source_ref IS NULL)
        ))
    ),
    CHECK (
        (status = 'RESERVED'
            AND content_idempotency_key IS NULL AND attempt_id IS NULL AND attempt_count = 0
            AND claimed_at IS NULL AND admitted_format IS NULL AND source_object_key IS NULL
            AND result_matter_version IS NULL AND failure_code IS NULL AND terminal_at IS NULL)
        OR (status = 'CLAIMED'
            AND content_idempotency_key IS NOT NULL AND attempt_id IS NOT NULL AND attempt_count = 1
            AND claimed_at IS NOT NULL AND admitted_format IS NULL AND source_object_key IS NULL
            AND result_matter_version IS NULL AND failure_code IS NULL AND terminal_at IS NULL)
        OR (status = 'OBJECT_STORED'
            AND content_idempotency_key IS NOT NULL AND attempt_id IS NOT NULL AND attempt_count = 1
            AND claimed_at IS NOT NULL AND admitted_format IS NOT NULL
            AND source_object_key IS NOT NULL AND source_reference_hash IS NOT NULL
            AND object_stored_at IS NOT NULL AND result_matter_version IS NULL
            AND failure_code IS NULL AND terminal_at IS NULL)
        OR (status = 'COMPLETED'
            AND content_idempotency_key IS NOT NULL AND attempt_id IS NOT NULL AND attempt_count = 1
            AND claimed_at IS NOT NULL AND admitted_format IS NOT NULL
            AND source_object_key IS NOT NULL AND source_reference_hash IS NOT NULL
            AND object_stored_at IS NOT NULL
            AND result_matter_version = expected_matter_version + 1
            AND audit_event_id IS NOT NULL AND outbox_id IS NOT NULL
            AND completed_at IS NOT NULL AND failure_code IS NULL AND terminal_at IS NULL)
        OR (status = 'FAILED'
            AND content_idempotency_key IS NOT NULL AND attempt_id IS NOT NULL AND attempt_count = 1
            AND claimed_at IS NOT NULL AND admitted_format IS NULL AND source_object_key IS NULL
            AND result_matter_version IS NULL AND completed_at IS NULL
            AND failure_code IN ('CONTENT_REJECTED', 'ADMISSION_UNAVAILABLE')
            AND terminal_at IS NOT NULL)
        OR (status = 'RECONCILIATION_REQUIRED'
            AND content_idempotency_key IS NOT NULL AND attempt_id IS NOT NULL AND attempt_count = 1
            AND claimed_at IS NOT NULL AND result_matter_version IS NULL AND completed_at IS NULL
            AND admitted_format IS NOT NULL AND source_object_key IS NOT NULL
            AND source_reference_hash IS NOT NULL
            AND (
                (failure_code = 'OBJECT_STATE_UNKNOWN' AND object_stored_at IS NULL)
                OR (failure_code IN ('OBJECT_HANDOFF_UNKNOWN', 'REGISTRATION_STATE_UNKNOWN')
                    AND object_stored_at IS NOT NULL)
            )
            AND terminal_at IS NOT NULL)
    )
);

COMMENT ON TABLE web_common_material_uploads IS
    'Server-only, no-retransmit saga for admitted common non-PDF browser materials.';
COMMENT ON COLUMN web_common_material_uploads.source_object_key IS
    'Private object locator; never return through HTTP, audit, outbox, model context or logs.';

CREATE TABLE case_material_objects (
    material_object_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_upload_id uuid NOT NULL,
    original_display_name text NOT NULL,
    admitted_format text NOT NULL CHECK (admitted_format IN (
        'DOCX', 'XLSX', 'PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML', 'JPEG', 'PNG'
    )),
    canonical_kind text NOT NULL CHECK (canonical_kind IN (
        'WORD_DOCUMENT', 'SPREADSHEET', 'PRESENTATION', 'TEXT', 'EMAIL', 'IMAGE'
    )),
    media_type text NOT NULL,
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 104857600),
    inspection_hash char(64) NOT NULL CHECK (inspection_hash ~ '^[0-9a-f]{64}$'),
    scanner_name text NOT NULL,
    scanner_definitions_version text NOT NULL,
    route text NOT NULL CHECK (route IN ('COMMON_DOCUMENT_READER', 'VISUAL_OCR')),
    review_flags jsonb NOT NULL CHECK (
        jsonb_typeof(review_flags) = 'array' AND jsonb_array_length(review_flags) <= 100
    ),
    status text NOT NULL CHECK (status = 'NEEDS_LAWYER_REVIEW'),
    record_version integer NOT NULL CHECK (record_version = 1),
    original_locked boolean NOT NULL CHECK (original_locked = true),
    formal_fact boolean NOT NULL CHECK (formal_fact = false),
    formal_transaction boolean NOT NULL CHECK (formal_transaction = false),
    legal_conclusion boolean NOT NULL CHECK (legal_conclusion = false),
    evidence_decision boolean NOT NULL CHECK (evidence_decision = false),
    court_ready boolean NOT NULL CHECK (court_ready = false),
    agent_status text NOT NULL CHECK (agent_status IN (
        'AGENT_READY', 'INGESTED_PENDING_ADAPTER'
    )),
    agent_source_ref text,
    source_object_key text NOT NULL,
    source_object_version_id text,
    source_reference_hash char(64) NOT NULL CHECK (source_reference_hash ~ '^[0-9a-f]{64}$'),
    created_matter_version integer NOT NULL CHECK (created_matter_version > 1),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (material_object_id, firm_id, matter_id),
    UNIQUE (source_upload_id, firm_id, matter_id),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (created_by, firm_id) REFERENCES users(user_id, firm_id),
    FOREIGN KEY (source_upload_id, firm_id, matter_id)
        REFERENCES web_common_material_uploads(upload_id, firm_id, matter_id),
    CHECK (octet_length(original_display_name) BETWEEN 1 AND 255
        AND original_display_name = btrim(original_display_name)
        AND original_display_name !~ '[[:cntrl:]]'
        AND position('/' IN original_display_name) = 0
        AND position(E'\\' IN original_display_name) = 0),
    CHECK (octet_length(media_type) BETWEEN 3 AND 160 AND media_type = lower(btrim(media_type))),
    CHECK (octet_length(scanner_name) BETWEEN 1 AND 160 AND scanner_name = btrim(scanner_name)),
    CHECK (octet_length(scanner_definitions_version) BETWEEN 1 AND 160
        AND scanner_definitions_version = btrim(scanner_definitions_version)),
    CHECK (route <> 'VISUAL_OCR' OR byte_size <= 67108864),
    CHECK (
        split_part(source_object_key, '/', 3) = firm_id::text
        AND split_part(source_object_key, '/', 4) = matter_id::text
        AND source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')
        AND (
            (route = 'COMMON_DOCUMENT_READER'
                AND source_object_key ~
                    '^case-materials/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}$'
                AND split_part(source_object_key, '/', 5) = substring(content_sha256 from 1 for 2)
                AND split_part(source_object_key, '/', 6) = content_sha256)
            OR (route = 'VISUAL_OCR'
                AND source_object_key ~
                    '^original-images/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f-]{36}\.(jpg|png)$'
                AND split_part(source_object_key, '/', 5) = substring(content_sha256 from 1 for 2)
                AND split_part(source_object_key, '/', 6) = content_sha256
                AND split_part(source_object_key, '/', 7) = material_object_id::text ||
                    CASE admitted_format WHEN 'JPEG' THEN '.jpg' ELSE '.png' END)
        )
    ),
    CHECK (source_object_version_id IS NULL OR (
        octet_length(source_object_version_id) BETWEEN 1 AND 512
        AND source_object_version_id = btrim(source_object_version_id)
        AND source_object_version_id !~ '[[:cntrl:]]'
    )),
    CHECK (
        (route = 'COMMON_DOCUMENT_READER' AND admitted_format IN (
            'DOCX', 'XLSX', 'PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML'
        ))
        OR (route = 'VISUAL_OCR' AND admitted_format IN ('JPEG', 'PNG'))
    ),
    CHECK (
        (admitted_format IN ('DOCX', 'XLSX')
            AND agent_status = 'AGENT_READY'
            AND agent_source_ref = 'material-object:' || material_object_id::text)
        OR (admitted_format IN ('JPEG', 'PNG')
            AND agent_status = 'AGENT_READY'
            AND agent_source_ref ~
                '^evidence-page:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        OR (admitted_format IN ('PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML')
            AND agent_status = 'INGESTED_PENDING_ADAPTER'
            AND agent_source_ref IS NULL)
    )
);

COMMENT ON TABLE case_material_objects IS
    'Immutable admitted common-material originals; review candidates only, never formal case conclusions.';
COMMENT ON COLUMN case_material_objects.source_object_key IS
    'Private server locator. CommonDocument/visual Workers resolve it by material-object UUID under RLS.';

CREATE TABLE web_common_material_upload_events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id uuid NOT NULL,
    material_object_id uuid NOT NULL,
    firm_id uuid NOT NULL,
    matter_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'RESERVED', 'CLAIMED', 'OBJECT_STORED', 'COMPLETED', 'FAILED',
        'RECONCILIATION_REQUIRED'
    )),
    attempt_id uuid,
    source_reference_hash char(64) CHECK (
        source_reference_hash IS NULL OR source_reference_hash ~ '^[0-9a-f]{64}$'
    ),
    failure_code text CHECK (failure_code IS NULL OR failure_code IN (
        'CONTENT_REJECTED', 'ADMISSION_UNAVAILABLE', 'OBJECT_STATE_UNKNOWN', 'OBJECT_HANDOFF_UNKNOWN',
        'REGISTRATION_STATE_UNKNOWN'
    )),
    matter_version integer CHECK (matter_version IS NULL OR matter_version > 0),
    occurred_at timestamptz NOT NULL,
    FOREIGN KEY (upload_id, firm_id, matter_id)
        REFERENCES web_common_material_uploads(upload_id, firm_id, matter_id),
    FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (event_type = 'RESERVED' AND attempt_id IS NULL AND source_reference_hash IS NULL
            AND failure_code IS NULL AND matter_version IS NULL)
        OR (event_type = 'CLAIMED' AND attempt_id IS NOT NULL AND source_reference_hash IS NULL
            AND failure_code IS NULL AND matter_version IS NULL)
        OR (event_type = 'OBJECT_STORED' AND attempt_id IS NOT NULL AND source_reference_hash IS NOT NULL
            AND failure_code IS NULL AND matter_version IS NULL)
        OR (event_type = 'COMPLETED' AND attempt_id IS NOT NULL AND source_reference_hash IS NOT NULL
            AND failure_code IS NULL AND matter_version IS NOT NULL)
        OR (event_type = 'FAILED' AND attempt_id IS NOT NULL
            AND failure_code IN ('CONTENT_REJECTED', 'ADMISSION_UNAVAILABLE')
            AND matter_version IS NULL)
        OR (event_type = 'RECONCILIATION_REQUIRED' AND attempt_id IS NOT NULL
            AND source_reference_hash IS NOT NULL
            AND failure_code IN ('OBJECT_STATE_UNKNOWN', 'OBJECT_HANDOFF_UNKNOWN', 'REGISTRATION_STATE_UNKNOWN')
            AND matter_version IS NULL)
    )
);

COMMENT ON TABLE web_common_material_upload_events IS
    'Append-only upload lifecycle audit without object key/version, scanner payload, path or browser bytes.';

CREATE INDEX web_common_material_upload_reserved_expiry_idx
    ON web_common_material_uploads (expires_at)
    WHERE status = 'RESERVED';
CREATE UNIQUE INDEX web_common_material_upload_content_idempotency_idx
    ON web_common_material_uploads (
        firm_id, matter_id, actor_id, content_idempotency_key
    ) WHERE content_idempotency_key IS NOT NULL;
CREATE INDEX web_common_material_upload_reconciliation_idx
    ON web_common_material_uploads (updated_at)
    WHERE status = 'RECONCILIATION_REQUIRED';
CREATE INDEX case_material_objects_matter_route_idx
    ON case_material_objects (firm_id, matter_id, route, created_at, material_object_id);
CREATE INDEX web_common_material_upload_events_timeline_idx
    ON web_common_material_upload_events (firm_id, matter_id, occurred_at, event_id);

CREATE FUNCTION enforce_web_common_material_upload_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'common material upload recovery records cannot be deleted';
    END IF;
    IF OLD.status IN ('COMPLETED', 'FAILED', 'RECONCILIATION_REQUIRED') THEN
        RAISE EXCEPTION 'terminal common material uploads are immutable';
    END IF;
    IF (
        to_jsonb(NEW) - ARRAY[
            'status', 'content_idempotency_key', 'attempt_id', 'attempt_count', 'claimed_at',
            'admitted_format', 'canonical_kind', 'admitted_media_type', 'route',
            'admitted_byte_size', 'admitted_content_sha256', 'admitted_inspection_hash',
            'scanner_name', 'scanner_definitions_version', 'review_flags', 'review_status',
            'formal_fact', 'formal_transaction', 'legal_conclusion', 'evidence_decision',
            'court_ready', 'source_object_key', 'source_object_version_id',
            'source_reference_hash', 'object_stored_at', 'result_matter_version',
            'agent_status', 'agent_source_ref',
            'audit_event_id', 'outbox_id', 'completed_at', 'failure_code', 'terminal_at',
            'updated_at'
        ]
    ) IS DISTINCT FROM (
        to_jsonb(OLD) - ARRAY[
            'status', 'content_idempotency_key', 'attempt_id', 'attempt_count', 'claimed_at',
            'admitted_format', 'canonical_kind', 'admitted_media_type', 'route',
            'admitted_byte_size', 'admitted_content_sha256', 'admitted_inspection_hash',
            'scanner_name', 'scanner_definitions_version', 'review_flags', 'review_status',
            'formal_fact', 'formal_transaction', 'legal_conclusion', 'evidence_decision',
            'court_ready', 'source_object_key', 'source_object_version_id',
            'source_reference_hash', 'object_stored_at', 'result_matter_version',
            'agent_status', 'agent_source_ref',
            'audit_event_id', 'outbox_id', 'completed_at', 'failure_code', 'terminal_at',
            'updated_at'
        ]
    ) THEN
        RAISE EXCEPTION 'common material upload ownership/reservation fields are immutable';
    END IF;
    IF OLD.status = 'RESERVED' AND NEW.status = 'CLAIMED' THEN
        RETURN NEW;
    ELSIF OLD.status = 'CLAIMED' AND NEW.status IN (
        'OBJECT_STORED', 'FAILED', 'RECONCILIATION_REQUIRED'
    ) THEN
        RETURN NEW;
    ELSIF OLD.status = 'OBJECT_STORED' AND NEW.status IN (
        'COMPLETED', 'RECONCILIATION_REQUIRED'
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'common material upload transition is not permitted';
END;
$$;

CREATE TRIGGER web_common_material_uploads_linear_state
    BEFORE UPDATE OR DELETE ON web_common_material_uploads
    FOR EACH ROW EXECUTE FUNCTION enforce_web_common_material_upload_transition();

CREATE FUNCTION enforce_case_material_object_immutability()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admitted common material originals are immutable';
END;
$$;

CREATE TRIGGER case_material_objects_immutable
    BEFORE UPDATE OR DELETE ON case_material_objects
    FOR EACH ROW EXECUTE FUNCTION enforce_case_material_object_immutability();

CREATE FUNCTION prohibit_web_common_material_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'common material upload events are append-only';
END;
$$;

CREATE TRIGGER web_common_material_upload_events_append_only
    BEFORE UPDATE OR DELETE ON web_common_material_upload_events
    FOR EACH ROW EXECUTE FUNCTION prohibit_web_common_material_event_mutation();

CREATE FUNCTION validate_web_common_material_completion()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'COMPLETED' AND OLD.status <> 'COMPLETED' AND NOT EXISTS (
        SELECT 1
          FROM case_material_objects material
          JOIN audit_events audit ON audit.event_id = NEW.audit_event_id
          JOIN outbox_events outbox ON outbox.outbox_id = NEW.outbox_id
         WHERE material.material_object_id = NEW.material_object_id
           AND material.firm_id = NEW.firm_id
           AND material.matter_id = NEW.matter_id
           AND material.source_upload_id = NEW.upload_id
           AND material.original_display_name = NEW.display_name
           AND material.admitted_format = NEW.admitted_format
           AND material.canonical_kind = NEW.canonical_kind
           AND material.media_type = NEW.admitted_media_type
           AND material.content_sha256 = NEW.admitted_content_sha256
           AND material.byte_size = NEW.admitted_byte_size
           AND material.inspection_hash = NEW.admitted_inspection_hash
           AND material.route = NEW.route
           AND material.source_object_key = NEW.source_object_key
           AND material.source_object_version_id IS NOT DISTINCT FROM NEW.source_object_version_id
           AND material.source_reference_hash = NEW.source_reference_hash
           AND material.created_matter_version = NEW.result_matter_version
           AND material.status = 'NEEDS_LAWYER_REVIEW'
           AND material.agent_status = NEW.agent_status
           AND material.agent_source_ref IS NOT DISTINCT FROM NEW.agent_source_ref
           AND (
               (NEW.admitted_format IN ('DOCX', 'XLSX') AND EXISTS (
                   SELECT 1 FROM case_agent_material_objects executable
                    WHERE executable.material_object_id = NEW.material_object_id
                      AND executable.firm_id = NEW.firm_id
                      AND executable.matter_id = NEW.matter_id
                      AND executable.admitted_format = NEW.admitted_format
                      AND executable.media_type = NEW.admitted_media_type
                      AND executable.content_sha256 = NEW.admitted_content_sha256
                      AND executable.byte_size = NEW.admitted_byte_size
                      AND executable.source_object_key = NEW.source_object_key
                      AND executable.source_object_version_id IS NOT DISTINCT FROM NEW.source_object_version_id
                      AND executable.source_reference_hash = NEW.source_reference_hash
                      AND executable.inspection_hash = NEW.admitted_inspection_hash
               ))
               OR (NEW.admitted_format IN ('JPEG', 'PNG') AND EXISTS (
                   SELECT 1
                     FROM evidence_original_files source
                     JOIN evidence_pages page
                       ON page.evidence_file_id = source.evidence_file_id
                      AND page.firm_id = source.firm_id
                      AND page.matter_id = source.matter_id
                     JOIN web_evidence_native_image_source_objects native
                       ON native.evidence_file_id = source.evidence_file_id
                      AND native.firm_id = source.firm_id
                      AND native.matter_id = source.matter_id
                    WHERE source.evidence_file_id = NEW.material_object_id
                      AND source.firm_id = NEW.firm_id
                      AND source.matter_id = NEW.matter_id
                      AND source.original_file_sha256 = NEW.admitted_content_sha256
                      AND source.byte_size = NEW.admitted_byte_size
                      AND source.media_type = NEW.admitted_media_type
                      AND source.page_count = 1 AND page.page_number = 1
                      AND NEW.agent_source_ref = 'evidence-page:' || page.evidence_page_id::text
                      AND native.source_object_key = NEW.source_object_key
                      AND native.source_object_version_id IS NOT DISTINCT FROM NEW.source_object_version_id
                      AND native.source_reference_hash = NEW.source_reference_hash
               ))
               OR (NEW.admitted_format IN ('PPTX', 'RTF', 'TXT', 'CSV', 'HTML', 'EML')
                   AND NEW.agent_status = 'INGESTED_PENDING_ADAPTER'
                   AND NEW.agent_source_ref IS NULL)
           )
           AND audit.firm_id = NEW.firm_id AND audit.matter_id = NEW.matter_id
           AND audit.actor_id = NEW.actor_id
           AND audit.event_type = 'COMMON_MATERIAL_ADMITTED'
           AND audit.input_version = NEW.expected_matter_version
           AND audit.output_version = NEW.result_matter_version
           AND outbox.firm_id = NEW.firm_id AND outbox.matter_id = NEW.matter_id
           AND outbox.aggregate_version = NEW.result_matter_version
           AND outbox.event_type = 'COMMON_MATERIAL_ADMITTED'
    ) THEN
        RAISE EXCEPTION 'completed common material upload differs from object/audit/outbox';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER web_common_material_upload_completion_guard
    AFTER UPDATE ON web_common_material_uploads
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_web_common_material_completion();

ALTER TABLE web_common_material_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_common_material_uploads FORCE ROW LEVEL SECURITY;
CREATE POLICY web_common_material_uploads_firm_isolation
    ON web_common_material_uploads
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE case_material_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_material_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY case_material_objects_firm_isolation
    ON case_material_objects
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE web_common_material_upload_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_common_material_upload_events FORCE ROW LEVEL SECURITY;
CREATE POLICY web_common_material_upload_events_firm_isolation
    ON web_common_material_upload_events
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE web_common_material_uploads FROM PUBLIC;
REVOKE ALL ON TABLE case_material_objects FROM PUBLIC;
REVOKE ALL ON TABLE web_common_material_upload_events FROM PUBLIC;

COMMIT;
