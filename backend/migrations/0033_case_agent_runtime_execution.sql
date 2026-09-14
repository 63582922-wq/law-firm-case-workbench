-- Production execution bindings for the unified case-Agent worker.
--
-- The event stream in 0031 remains authoritative.  The inbox below is only a
-- recoverable wake-up projection: its trigger can rebuild it from
-- case_agent_runs, while task/planning leases still decide who may execute.
-- Browser paths, prompts and document text are deliberately absent.

BEGIN;

CREATE TABLE case_agent_run_inbox (
    run_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    inbox_status text NOT NULL DEFAULT 'READY'
        CHECK (inbox_status IN ('READY', 'LEASED', 'QUIET')),
    observed_event_version bigint NOT NULL CHECK (observed_event_version > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text CHECK (
        lease_owner IS NULL OR length(trim(lease_owner)) BETWEEN 1 AND 200
    ),
    lease_token uuid,
    lease_expires_at timestamptz,
    inbox_version bigint NOT NULL DEFAULT 1 CHECK (inbox_version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    CHECK (
        (inbox_status IN ('READY', 'QUIET')
            AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR
        (inbox_status = 'LEASED'
            AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX case_agent_run_inbox_claim_idx
    ON case_agent_run_inbox (firm_id, inbox_status, available_at, updated_at, run_id);
CREATE INDEX case_agent_run_inbox_lease_idx
    ON case_agent_run_inbox (firm_id, lease_expires_at)
    WHERE lease_token IS NOT NULL;

CREATE FUNCTION wake_case_agent_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO case_agent_run_inbox (
        run_id, firm_id, matter_id, inbox_status, observed_event_version, available_at,
        lease_owner, lease_token, lease_expires_at, inbox_version, updated_at
    ) VALUES (
        NEW.run_id, NEW.firm_id, NEW.matter_id, 'READY', NEW.current_event_version, now(),
        NULL, NULL, NULL, 1, now()
    )
    ON CONFLICT (run_id) DO UPDATE SET
        observed_event_version = EXCLUDED.observed_event_version,
        inbox_status = 'READY',
        available_at = now(),
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        inbox_version = case_agent_run_inbox.inbox_version + 1,
        updated_at = now();
    PERFORM pg_notify('case_agent_run_ready', NEW.firm_id::text);
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_runs_wake_worker_after_insert
    AFTER INSERT ON case_agent_runs
    FOR EACH ROW EXECUTE FUNCTION wake_case_agent_run();
CREATE TRIGGER case_agent_runs_wake_worker_after_event
    AFTER UPDATE OF current_event_version ON case_agent_runs
    FOR EACH ROW
    WHEN (NEW.current_event_version IS DISTINCT FROM OLD.current_event_version)
    EXECUTE FUNCTION wake_case_agent_run();

-- Runs that pre-date this migration are recoverable without manufacturing a
-- new event.  Terminal runs are retained as QUIET audit projections; any
-- subsequent legitimate event wakes them through the trigger above.
INSERT INTO case_agent_run_inbox (
    run_id, firm_id, matter_id, inbox_status, observed_event_version,
    available_at, inbox_version, updated_at
)
SELECT
    run_id,
    firm_id,
    matter_id,
    CASE WHEN status IN ('COMPLETED', 'CANCELLED', 'FAILED')
        THEN 'QUIET' ELSE 'READY' END,
    current_event_version,
    now(),
    1,
    now()
FROM case_agent_runs
ON CONFLICT (run_id) DO NOTHING;

CREATE FUNCTION guard_case_agent_run_inbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.run_id <> OLD.run_id
       OR NEW.firm_id <> OLD.firm_id
       OR NEW.matter_id <> OLD.matter_id
       OR NEW.observed_event_version < OLD.observed_event_version
       OR NEW.inbox_version <> OLD.inbox_version + 1 THEN
        RAISE EXCEPTION 'case Agent run inbox transition is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_run_inbox_guard
    BEFORE UPDATE OR DELETE ON case_agent_run_inbox
    FOR EACH ROW EXECUTE FUNCTION guard_case_agent_run_inbox();

-- Immutable Office/text objects admitted by a separate scanner/ingestion
-- service.  This migration does not create a browser upload bypass.  v1
-- execution admits only DOCX and XLSX because those are the formats with a
-- bounded, literal-text parser and an implemented runtime adapter.
CREATE TABLE case_agent_material_objects (
    material_object_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    admitted_format text NOT NULL CHECK (admitted_format IN ('DOCX', 'XLSX')),
    media_type text NOT NULL CHECK (media_type IN (
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 104857600),
    source_object_key text NOT NULL CHECK (
        source_object_key ~
        '^case-materials/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}$'
    ),
    source_object_version_id text,
    source_reference_hash char(64) NOT NULL
        CHECK (source_reference_hash ~ '^[0-9a-f]{64}$'),
    scanner_name text NOT NULL CHECK (length(trim(scanner_name)) BETWEEN 1 AND 200),
    scanner_definitions_version text NOT NULL
        CHECK (length(trim(scanner_definitions_version)) BETWEEN 1 AND 200),
    inspection_hash char(64) NOT NULL CHECK (inspection_hash ~ '^[0-9a-f]{64}$'),
    admitted_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (material_object_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, source_reference_hash),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (admitted_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (split_part(source_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(source_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(source_object_key, '/', 5) = substring(content_sha256 from 1 for 2)),
    CHECK (split_part(source_object_key, '/', 6) = content_sha256),
    CHECK (source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')),
    CHECK (
        source_object_version_id IS NULL
        OR (
            length(source_object_version_id) BETWEEN 1 AND 512
            AND source_object_version_id = btrim(source_object_version_id)
            AND source_object_version_id !~ '[[:cntrl:]]'
        )
    ),
    CHECK (
        (admitted_format = 'DOCX' AND media_type =
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        OR
        (admitted_format = 'XLSX' AND media_type =
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    )
);

-- This is an intentionally narrow v1 extension seam.  New formats must add a
-- scanner admission path, immutable metadata constraints, a runtime adapter
-- and a planning capability declaration together.  The existing parser's
-- wider type support is not itself permission to insert those formats here.
COMMENT ON TABLE case_agent_material_objects IS
    'First executable whitelist: DOCX/XLSX only. PPTX/RTF/TXT/CSV/HTML/EML and images require separately reviewed migrations and adapters.';

CREATE TABLE case_agent_material_object_tombstones (
    tombstone_id uuid PRIMARY KEY,
    material_object_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
    revoked_by uuid NOT NULL,
    revoked_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (material_object_id),
    FOREIGN KEY (material_object_id, firm_id, matter_id)
        REFERENCES case_agent_material_objects(material_object_id, firm_id, matter_id),
    FOREIGN KEY (revoked_by, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE INDEX case_agent_material_objects_matter_idx
    ON case_agent_material_objects (matter_id, created_at, material_object_id);

-- Canonical JSON candidates remain review-only.  Payload bytes live in the
-- encrypted private object store; the database keeps only a hash-bound,
-- server-only locator and never promotes the row to a formal fact.
CREATE TABLE case_agent_review_candidates (
    artifact_id uuid PRIMARY KEY,
    idempotency_key char(64) NOT NULL CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    task_input_hash char(64) NOT NULL CHECK (task_input_hash ~ '^[0-9a-f]{64}$'),
    source_hash char(64) NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    artifact_kind text NOT NULL CHECK (artifact_kind ~ '^[A-Za-z][A-Za-z0-9._:-]{0,199}$'),
    media_type text NOT NULL CHECK (media_type = 'application/json'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 2 AND 67108864),
    review_status text NOT NULL CHECK (review_status = 'NEEDS_LAWYER_REVIEW'),
    source_object_key text NOT NULL CHECK (
        source_object_key ~
        '^case-agent-candidates/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{64}\.json$'
    ),
    source_object_version_id text,
    receipt_hash char(64) NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    staged_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (firm_id, idempotency_key),
    UNIQUE (artifact_id, run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id),
    FOREIGN KEY (staged_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (split_part(source_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(source_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(source_object_key, '/', 5) = artifact_id::text),
    CHECK (split_part(source_object_key, '/', 6) = content_sha256 || '.json'),
    CHECK (
        source_object_version_id IS NULL
        OR (
            length(source_object_version_id) BETWEEN 1 AND 512
            AND source_object_version_id = btrim(source_object_version_id)
            AND source_object_version_id !~ '[[:cntrl:]]'
        )
    )
);

CREATE INDEX case_agent_review_candidates_run_idx
    ON case_agent_review_candidates (run_id, created_at, artifact_id);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_run_inbox', 'case_agent_material_objects',
        'case_agent_material_object_tombstones', 'case_agent_review_candidates'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (firm_id::text = current_setting(''app.firm_id'', true)) WITH CHECK (firm_id::text = current_setting(''app.firm_id'', true))',
            table_name || '_firm_isolation', table_name
        );
    END LOOP;
END;
$$;

CREATE FUNCTION prohibit_case_agent_runtime_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case Agent material and review-candidate history is append-only';
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'case_agent_material_objects', 'case_agent_material_object_tombstones',
        'case_agent_review_candidates'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_runtime_history_mutation()',
            table_name || '_append_only', table_name
        );
    END LOOP;
END;
$$;

REVOKE ALL ON TABLE case_agent_run_inbox FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_material_objects FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_material_object_tombstones FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_review_candidates FROM PUBLIC;

COMMIT;
