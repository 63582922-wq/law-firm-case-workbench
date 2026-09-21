-- Production visual-page sources and recoverable Qwen OCR exchange ledger.
-- PostgreSQL 16+; apply after 0037_case_agent_public_research.sql.
--
-- Browser/model inputs contain only ``evidence-page:<uuid>`` references.  The
-- private object key below is server-only and is never copied into an Agent
-- task, candidate, receipt, audit event or Web response.  PDF evidence keeps
-- using the immutable 0025 original binding; this table admits native JPEG or
-- PNG originals through the same content-addressed private storage boundary.

BEGIN;

CREATE TABLE web_evidence_native_image_source_objects (
    evidence_file_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    source_object_key text NOT NULL CHECK (
        source_object_key ~
        '^original-images/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f-]{36}\.(jpg|png)$'
    ),
    source_object_version_id text,
    source_object_sha256 char(64) NOT NULL
        CHECK (source_object_sha256 ~ '^[0-9a-f]{64}$'),
    source_object_bytes bigint NOT NULL
        CHECK (source_object_bytes BETWEEN 1 AND 67108864),
    source_media_type text NOT NULL
        CHECK (source_media_type IN ('image/jpeg', 'image/png')),
    source_reference_hash char(64) NOT NULL
        CHECK (source_reference_hash ~ '^[0-9a-f]{64}$'),
    admitted_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evidence_file_id, firm_id, matter_id),
    UNIQUE (firm_id, matter_id, source_reference_hash),
    FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id),
    FOREIGN KEY (evidence_file_id, firm_id, matter_id)
        REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id),
    FOREIGN KEY (admitted_by, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (split_part(source_object_key, '/', 3) = firm_id::text),
    CHECK (split_part(source_object_key, '/', 4) = matter_id::text),
    CHECK (split_part(source_object_key, '/', 5) = substring(source_object_sha256 from 1 for 2)),
    CHECK (split_part(source_object_key, '/', 6) = source_object_sha256),
    CHECK (
        (source_media_type = 'image/jpeg' AND source_object_key ~ '\.jpg$')
        OR (source_media_type = 'image/png' AND source_object_key ~ '\.png$')
    ),
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

COMMENT ON TABLE web_evidence_native_image_source_objects IS
    'Server-only immutable JPEG/PNG source binding for one-page evidence; never browser-addressable.';
COMMENT ON COLUMN web_evidence_native_image_source_objects.source_object_key IS
    'Private object key. It must never enter task input, model output, audit payload or Web response.';

CREATE INDEX web_evidence_native_image_source_objects_matter_idx
    ON web_evidence_native_image_source_objects
    (matter_id, created_at, evidence_file_id);

CREATE FUNCTION enforce_web_native_image_source_integrity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM evidence_original_files source
        JOIN evidence_pages page
          ON page.evidence_file_id = source.evidence_file_id
         AND page.firm_id = source.firm_id
         AND page.matter_id = source.matter_id
        WHERE source.evidence_file_id = NEW.evidence_file_id
          AND source.firm_id = NEW.firm_id
          AND source.matter_id = NEW.matter_id
          AND source.original_file_sha256 = NEW.source_object_sha256
          AND source.byte_size = NEW.source_object_bytes
          AND source.media_type = NEW.source_media_type
          AND source.page_count = 1
          AND page.page_number = 1
        GROUP BY source.evidence_file_id
        HAVING count(*) = 1
    ) THEN
        RAISE EXCEPTION 'native visual source must match one immutable evidence page';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER web_evidence_native_image_source_objects_integrity
    BEFORE INSERT ON web_evidence_native_image_source_objects
    FOR EACH ROW EXECUTE FUNCTION enforce_web_native_image_source_integrity();

CREATE FUNCTION prohibit_web_native_image_source_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'native visual source bindings are append-only';
END;
$$;

CREATE TRIGGER web_evidence_native_image_source_objects_append_only
    BEFORE UPDATE OR DELETE ON web_evidence_native_image_source_objects
    FOR EACH ROW EXECUTE FUNCTION prohibit_web_native_image_source_change();

ALTER TABLE web_evidence_native_image_source_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_evidence_native_image_source_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY web_evidence_native_image_source_objects_firm_isolation
    ON web_evidence_native_image_source_objects
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE web_evidence_native_image_source_objects FROM PUBLIC;

-- The immutable exchange row is inserted only after the existing 0031
-- submission marker is visible and before any network byte is sent.  It stores
-- no API key, prompt, source bytes, private object key or public URL.
CREATE TABLE case_agent_visual_ocr_exchanges (
    exchange_id uuid PRIMARY KEY,
    external_request_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    evidence_page_id uuid NOT NULL,
    submission_record_id uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    projection_hash char(64) NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    rendered_page_sha256 char(64) NOT NULL
        CHECK (rendered_page_sha256 ~ '^[0-9a-f]{64}$'),
    endpoint_host text NOT NULL CHECK (
        endpoint_host ~ '^[a-z0-9][a-z0-9-]{2,62}[.]cn-beijing[.]maas[.]aliyuncs[.]com$'
    ),
    provider_id text NOT NULL CHECK (provider_id = 'qwen'),
    model_id text NOT NULL CHECK (model_id = 'qwen3.5-ocr'),
    service_id text NOT NULL CHECK (service_id = 'qwen-visual-ocr'),
    started_by_worker uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exchange_id, external_request_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, run_id, task_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(
            attempt_id, run_id, task_id, firm_id, matter_id
        ),
    FOREIGN KEY (evidence_page_id, firm_id, matter_id)
        REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id),
    FOREIGN KEY (submission_record_id, firm_id, matter_id)
        REFERENCES case_agent_external_submissions(
            submission_id, firm_id, matter_id
        ),
    FOREIGN KEY (started_by_worker, firm_id) REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_visual_ocr_outcomes (
    outcome_id uuid PRIMARY KEY,
    exchange_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    status text NOT NULL CHECK (
        status IN ('SUCCEEDED', 'UNKNOWN_SUBMISSION')
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    provider_request_id text,
    response_sha256 char(64) CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    response_bytes integer CHECK (response_bytes BETWEEN 2 AND 2097152),
    response_body bytea,
    error_code text CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,79}$'
    ),
    recorded_by_worker uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exchange_id),
    UNIQUE (external_request_id),
    UNIQUE (outcome_id, firm_id, matter_id),
    FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_visual_ocr_exchanges(
            exchange_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by_worker, firm_id) REFERENCES users(user_id, firm_id),
    CHECK (
        (status = 'SUCCEEDED'
         AND provider_request_id ~ '^[A-Za-z0-9._:-]{1,500}$'
         AND response_sha256 IS NOT NULL
         AND response_bytes IS NOT NULL
         AND octet_length(response_body) = response_bytes
         AND encode(digest(response_body, 'sha256'), 'hex') = response_sha256
         AND error_code IS NULL)
        OR
        (status = 'UNKNOWN_SUBMISSION'
         AND provider_request_id IS NULL
         AND response_sha256 IS NULL
         AND response_bytes IS NULL
         AND response_body IS NULL
         AND error_code = 'QWEN_VISUAL_OCR_OUTCOME_UNKNOWN')
    )
);

CREATE INDEX case_agent_visual_ocr_exchange_matter_idx
    ON case_agent_visual_ocr_exchanges(
        firm_id, matter_id, run_id, task_id
    );

CREATE FUNCTION validate_case_agent_visual_ocr_exchange()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    task_row record;
    submission_row record;
BEGIN
    SELECT task.skill_id, task.skill_version, task.tool_id, task.tool_version,
           task.adapter_id, task.adapter_version, task.execution_mode,
           task.input_hash,
           task.network_policy, task.allowed_domains,
           task.granted_scopes, task.writes_managed_derivatives,
           task.sandbox_policy_version, task.sandbox_policy_hash,
           task.risk_level, task.autonomy_level, task.approval_gate,
           task.external_request_approval_required, task.retry_mode,
           task.resource_budget, attempt.status AS attempt_status,
           attempt.graph_id, attempt.input_hash AS attempt_input_hash,
           attempt.retry_mode AS attempt_retry_mode,
           attempt.adapter_id AS attempt_adapter_id,
           attempt.adapter_version AS attempt_adapter_version,
           attempt.external_approval_id, attempt.external_request_id,
           run.current_graph_id, run.current_graph_hash,
           graph.graph_hash, graph.snapshot_matter_version,
           matter.version AS matter_version
      INTO task_row
      FROM case_agent_task_attempts attempt
      JOIN case_agent_tasks task
        ON task.graph_id = attempt.graph_id
       AND task.task_id = attempt.task_id
       AND task.run_id = attempt.run_id
       AND task.firm_id = attempt.firm_id
       AND task.matter_id = attempt.matter_id
      JOIN case_agent_runs run
        ON run.run_id = task.run_id AND run.firm_id = task.firm_id
       AND run.matter_id = task.matter_id
      JOIN case_agent_task_graphs graph
        ON graph.graph_id = task.graph_id AND graph.run_id = task.run_id
       AND graph.firm_id = task.firm_id AND graph.matter_id = task.matter_id
      JOIN matters matter
        ON matter.matter_id = task.matter_id AND matter.firm_id = task.firm_id
     WHERE attempt.attempt_id = NEW.attempt_id
       AND attempt.run_id = NEW.run_id AND attempt.task_id = NEW.task_id
       AND attempt.firm_id = NEW.firm_id AND attempt.matter_id = NEW.matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'visual OCR exchange has no matching current task';
    END IF;
    IF task_row.skill_id IS DISTINCT FROM 'image_visual_ocr'
       OR task_row.skill_version IS DISTINCT FROM '1.0.0'
       OR task_row.tool_id IS DISTINCT FROM 'understand_visual_page'
       OR task_row.tool_version IS DISTINCT FROM '1.0.0'
       OR task_row.adapter_id IS DISTINCT FROM 'qwen-visual-ocr-review'
       OR task_row.adapter_version IS DISTINCT FROM '1.0.0'
       OR task_row.execution_mode IS DISTINCT FROM 'NETWORK_CONNECTOR'
       OR task_row.network_policy IS DISTINCT FROM 'EXACT_ALLOWLIST'
       OR task_row.allowed_domains IS DISTINCT FROM jsonb_build_array(NEW.endpoint_host)
       OR task_row.granted_scopes IS DISTINCT FROM '["CASE_READ"]'::jsonb
       OR task_row.writes_managed_derivatives IS DISTINCT FROM false
       OR task_row.sandbox_policy_version IS DISTINCT FROM '1.0.0'
       OR task_row.sandbox_policy_hash IS DISTINCT FROM
          '9cd89e64af567a9b1396ca723e934b2b7b2889fd2377227260766136dcab7305'
       OR task_row.risk_level IS DISTINCT FROM 'HIGH'
       OR task_row.autonomy_level IS DISTINCT FROM 'A3_LAWYER_APPROVAL'
       OR task_row.approval_gate IS DISTINCT FROM 'LAWYER_REVIEW'
       OR task_row.external_request_approval_required IS DISTINCT FROM true
       OR task_row.retry_mode IS DISTINCT FROM 'NEVER_AUTOMATIC'
       OR COALESCE((task_row.resource_budget->>'max_external_calls')::integer, -1) <> 1
       OR COALESCE((task_row.resource_budget->>'max_attempts')::integer, -1) <> 1
       OR task_row.attempt_status IS DISTINCT FROM 'RUNNING'
       OR task_row.attempt_input_hash IS DISTINCT FROM task_row.input_hash
       OR task_row.attempt_retry_mode IS DISTINCT FROM task_row.retry_mode
       OR task_row.attempt_adapter_id IS DISTINCT FROM task_row.adapter_id
       OR task_row.attempt_adapter_version IS DISTINCT FROM task_row.adapter_version
       OR task_row.external_approval_id IS NULL
       OR task_row.current_graph_id IS DISTINCT FROM task_row.graph_id
       OR task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash
       OR task_row.external_request_id IS DISTINCT FROM NEW.external_request_id::text
       OR task_row.snapshot_matter_version IS DISTINCT FROM task_row.matter_version THEN
        RAISE EXCEPTION 'visual OCR exchange is not a current exact approved task';
    END IF;

    SELECT submission_id, external_request_id, destination, request_hash,
           submission_state, recorded_by INTO submission_row
      FROM case_agent_external_submissions
     WHERE submission_id = NEW.submission_record_id
       AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
       AND task_id = NEW.task_id AND firm_id = NEW.firm_id
       AND matter_id = NEW.matter_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'visual OCR 0031 submission boundary is absent';
    END IF;
    IF submission_row.external_request_id IS DISTINCT FROM NEW.external_request_id::text
       OR submission_row.destination IS DISTINCT FROM NEW.endpoint_host
       OR submission_row.request_hash IS DISTINCT FROM NEW.request_hash
       OR submission_row.submission_state IS DISTINCT FROM 'STARTED'
       OR submission_row.recorded_by IS DISTINCT FROM NEW.started_by_worker
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.started_by_worker
       OR NOT EXISTS (
           SELECT 1 FROM users principal
           JOIN matter_actor_roles role
             ON role.user_id = principal.user_id
            AND role.firm_id = principal.firm_id
          WHERE principal.user_id = NEW.started_by_worker
            AND principal.firm_id = NEW.firm_id
            AND principal.status = 'ACTIVE'
            AND role.matter_id = NEW.matter_id
            AND role.role = 'SYSTEM_WORKER'
            AND role.revoked_at IS NULL
       ) OR EXISTS (
           SELECT 1 FROM matter_actor_roles role
          WHERE role.user_id = NEW.started_by_worker
            AND role.firm_id = NEW.firm_id
            AND role.matter_id = NEW.matter_id
            AND role.role <> 'SYSTEM_WORKER'
            AND role.revoked_at IS NULL
       ) THEN
        RAISE EXCEPTION 'visual OCR 0031 submission boundary is absent or differs';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_visual_ocr_exchange_guard
    BEFORE INSERT ON case_agent_visual_ocr_exchanges
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_visual_ocr_exchange();

CREATE FUNCTION validate_case_agent_visual_ocr_outcome()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    exchange_row case_agent_visual_ocr_exchanges%ROWTYPE;
BEGIN
    SELECT * INTO exchange_row FROM case_agent_visual_ocr_exchanges
     WHERE exchange_id = NEW.exchange_id
       AND external_request_id = NEW.external_request_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF exchange_row.exchange_id IS NULL
       OR exchange_row.request_hash IS DISTINCT FROM NEW.request_hash
       OR exchange_row.started_by_worker IS DISTINCT FROM NEW.recorded_by_worker
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.recorded_by_worker THEN
        RAISE EXCEPTION 'visual OCR outcome differs from its exchange';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_visual_ocr_outcome_guard
    BEFORE INSERT ON case_agent_visual_ocr_outcomes
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_visual_ocr_outcome();

CREATE FUNCTION prohibit_case_agent_visual_ocr_ledger_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'visual OCR exchange ledger is append-only';
END;
$$;

CREATE TRIGGER case_agent_visual_ocr_exchanges_append_only
    BEFORE UPDATE OR DELETE ON case_agent_visual_ocr_exchanges
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_visual_ocr_ledger_change();
CREATE TRIGGER case_agent_visual_ocr_outcomes_append_only
    BEFORE UPDATE OR DELETE ON case_agent_visual_ocr_outcomes
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_visual_ocr_ledger_change();

ALTER TABLE case_agent_visual_ocr_exchanges ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_visual_ocr_exchanges FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_visual_ocr_exchanges_firm_isolation
    ON case_agent_visual_ocr_exchanges
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE case_agent_visual_ocr_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_visual_ocr_outcomes FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_visual_ocr_outcomes_firm_isolation
    ON case_agent_visual_ocr_outcomes
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_agent_visual_ocr_exchanges FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_visual_ocr_outcomes FROM PUBLIC;

COMMIT;
