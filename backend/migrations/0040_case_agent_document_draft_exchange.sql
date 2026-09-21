-- Recoverable, one-call DeepSeek exchange for dynamic reviewable documents.
--
-- The existing 0031 submission row is the durable "about to cross HTTPS"
-- boundary.  This ledger adds an immutable binding to the exact current
-- run/task/attempt and records only hashes plus a private object locator for
-- the raw provider response.  Provider bytes, prompts and credentials are
-- never stored in PostgreSQL or exposed to a browser.

BEGIN;

CREATE TABLE case_agent_document_draft_exchanges (
    exchange_id uuid PRIMARY KEY,
    external_request_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    task_id uuid NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    submission_record_id uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    source_set_hash char(64) NOT NULL CHECK (source_set_hash ~ '^[0-9a-f]{64}$'),
    endpoint_host text NOT NULL CHECK (endpoint_host = 'api.deepseek.com'),
    provider_id text NOT NULL CHECK (provider_id = 'deepseek'),
    model_id text NOT NULL CHECK (
        model_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
    ),
    service_id text NOT NULL CHECK (
        service_id = 'deepseek-document-drafting'
    ),
    started_by_worker uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (run_id, task_id),
    UNIQUE (exchange_id, external_request_id, firm_id, matter_id),
    FOREIGN KEY (attempt_id, graph_id, task_id, run_id, firm_id, matter_id)
        REFERENCES case_agent_task_attempts(
            attempt_id, graph_id, task_id, run_id, firm_id, matter_id
        ),
    FOREIGN KEY (submission_record_id, firm_id, matter_id)
        REFERENCES case_agent_external_submissions(
            submission_id, firm_id, matter_id
        ),
    FOREIGN KEY (started_by_worker, firm_id)
        REFERENCES users(user_id, firm_id)
);

CREATE TABLE case_agent_document_draft_outcomes (
    outcome_id uuid PRIMARY KEY,
    exchange_id uuid NOT NULL,
    external_request_id uuid NOT NULL,
    firm_id uuid NOT NULL REFERENCES firms(firm_id),
    matter_id uuid NOT NULL,
    outcome_sequence smallint NOT NULL CHECK (outcome_sequence IN (1, 2)),
    status text NOT NULL CHECK (
        status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION')
    ),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    binding_hash char(64) NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    response_sha256 char(64) CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    response_bytes integer CHECK (response_bytes BETWEEN 2 AND 6291456),
    response_object_key text CHECK (
        response_object_key IS NULL OR response_object_key ~
        '^case-agent-document-drafts/v1/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{64}\.json$'
    ),
    response_object_version_id text,
    error_code text CHECK (
        error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,79}$'
    ),
    recovered_from_unknown boolean NOT NULL DEFAULT false,
    recorded_by_worker uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (exchange_id, outcome_sequence),
    UNIQUE (external_request_id, outcome_sequence),
    UNIQUE (outcome_id, firm_id, matter_id),
    FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id)
        REFERENCES case_agent_document_draft_exchanges(
            exchange_id, external_request_id, firm_id, matter_id
        ),
    FOREIGN KEY (recorded_by_worker, firm_id)
        REFERENCES users(user_id, firm_id),

    CHECK (
        (status = 'SUCCEEDED'
         AND response_sha256 IS NOT NULL
         AND response_bytes IS NOT NULL
         AND response_object_key IS NOT NULL
         AND error_code IS NULL)
        OR
        (status = 'FAILED'
         AND error_code IS NOT NULL
         AND ((response_sha256 IS NULL AND response_bytes IS NULL
               AND response_object_key IS NULL)
              OR
              (response_sha256 IS NOT NULL AND response_bytes IS NOT NULL
               AND response_object_key IS NOT NULL)))
        OR
        (status = 'UNKNOWN_SUBMISSION'
         AND outcome_sequence = 1
         AND response_sha256 IS NULL
         AND response_bytes IS NULL
         AND response_object_key IS NULL
         AND response_object_version_id IS NULL
         AND error_code = 'DOCUMENT_DRAFT_OUTCOME_UNKNOWN')
    ),
    CHECK (
        (outcome_sequence = 1 AND recovered_from_unknown = false)
        OR (outcome_sequence = 2 AND recovered_from_unknown = true
            AND status IN ('SUCCEEDED', 'FAILED'))
    ),
    CHECK (
        response_object_key IS NULL OR (
            split_part(response_object_key, '/', 3) = firm_id::text
            AND split_part(response_object_key, '/', 4) = matter_id::text
            AND split_part(response_object_key, '/', 5) = external_request_id::text
            AND split_part(response_object_key, '/', 6) = request_hash || '.json'
        )
    ),
    CHECK (
        response_object_version_id IS NULL OR (
            length(response_object_version_id) BETWEEN 1 AND 512
            AND response_object_version_id = btrim(response_object_version_id)
            AND response_object_version_id !~ '[[:cntrl:]]'
        )
    )
);

COMMENT ON TABLE case_agent_document_draft_exchanges IS
    'Immutable one-call DeepSeek document drafting request identity; no prompt, credential or provider response bytes.';
COMMENT ON TABLE case_agent_document_draft_outcomes IS
    'Append-only result ledger. Raw response bytes live only in encrypted private object storage.';
COMMENT ON COLUMN case_agent_document_draft_outcomes.response_object_key IS
    'Private server locator; never include in Web responses, audit payloads or logs.';

CREATE INDEX case_agent_document_draft_exchanges_matter_idx
    ON case_agent_document_draft_exchanges(
        firm_id, matter_id, run_id, task_id, started_at
    );
CREATE INDEX case_agent_document_draft_outcomes_latest_idx
    ON case_agent_document_draft_outcomes(
        firm_id, external_request_id, outcome_sequence DESC
    );

CREATE FUNCTION validate_case_agent_document_draft_exchange()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    task_row record;
    submission_row record;
BEGIN
    SELECT task.skill_id, task.skill_version, task.tool_id, task.tool_version,
           task.adapter_id, task.adapter_version, task.execution_mode,
           task.input_hash, task.input_refs, task.network_policy,
           task.allowed_domains, task.granted_scopes,
           task.writes_managed_derivatives, task.sandbox_policy_version,
           task.sandbox_policy_hash, task.risk_level, task.autonomy_level,
           task.approval_gate, task.external_request_approval_required,
           task.retry_mode, task.resource_budget,
           attempt.status AS attempt_status,
           attempt.input_hash AS attempt_input_hash,
           attempt.retry_mode AS attempt_retry_mode,
           attempt.adapter_id AS attempt_adapter_id,
           attempt.adapter_version AS attempt_adapter_version,
           attempt.external_approval_id, attempt.external_request_id,
           head.active_attempt_id, head.status AS head_status, head.is_current,
           run.current_graph_id, run.current_graph_hash,
           run.status AS run_status, run.is_stale, run.is_cancelled,
           graph.graph_hash, graph.snapshot_matter_version,
           matter.version AS matter_version
      INTO task_row
      FROM case_agent_task_attempts attempt
      JOIN case_agent_tasks task
        ON task.graph_id = attempt.graph_id AND task.task_id = attempt.task_id
       AND task.run_id = attempt.run_id AND task.firm_id = attempt.firm_id
       AND task.matter_id = attempt.matter_id
      JOIN case_agent_task_heads head
        ON head.graph_id = task.graph_id AND head.task_id = task.task_id
       AND head.run_id = task.run_id AND head.firm_id = task.firm_id
       AND head.matter_id = task.matter_id
      JOIN case_agent_runs run
        ON run.run_id = task.run_id AND run.firm_id = task.firm_id
       AND run.matter_id = task.matter_id
      JOIN case_agent_task_graphs graph
        ON graph.graph_id = task.graph_id AND graph.run_id = task.run_id
       AND graph.firm_id = task.firm_id AND graph.matter_id = task.matter_id
      JOIN matters matter
        ON matter.matter_id = task.matter_id AND matter.firm_id = task.firm_id
     WHERE attempt.attempt_id = NEW.attempt_id
       AND attempt.graph_id = NEW.graph_id
       AND attempt.task_id = NEW.task_id
       AND attempt.run_id = NEW.run_id
       AND attempt.firm_id = NEW.firm_id
       AND attempt.matter_id = NEW.matter_id;

    IF task_row IS NULL
       OR task_row.skill_version IS DISTINCT FROM '1.0.0'
       OR task_row.tool_version IS DISTINCT FROM '1.0.0'
       OR task_row.adapter_version IS DISTINCT FROM '1.0.0'
       OR NOT (
            (task_row.skill_id = 'dynamic_document_delivery'
             AND task_row.tool_id = 'draft_reviewable_docx_package'
             AND task_row.adapter_id = 'dynamic-reviewable-docx-delivery'
             AND task_row.sandbox_policy_hash =
                 'f7e698d61d86240dcbd377b62a0d8dd5cca407a4a10dd5662b99240d76a6371a')
            OR
            (task_row.skill_id = 'dynamic_spreadsheet_delivery'
             AND task_row.tool_id = 'draft_reviewable_xlsx_package'
             AND task_row.adapter_id = 'dynamic-reviewable-xlsx-delivery'
             AND task_row.sandbox_policy_hash =
                 'a2b29b003bb6301854a6a53a27cc0d4790baacc600a3f61efd1170ce4f9c4f1c')
       )
       OR task_row.execution_mode IS DISTINCT FROM 'NETWORK_CONNECTOR'
       OR task_row.network_policy IS DISTINCT FROM 'EXACT_ALLOWLIST'
       OR task_row.allowed_domains IS DISTINCT FROM '["api.deepseek.com"]'::jsonb
       OR task_row.granted_scopes IS DISTINCT FROM
          '["CASE_READ","MANAGED_DERIVATIVE_WRITE"]'::jsonb
       OR task_row.writes_managed_derivatives IS DISTINCT FROM true
       OR task_row.sandbox_policy_version IS DISTINCT FROM '1.0.0'
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
       OR task_row.external_request_id IS DISTINCT FROM NEW.external_request_id::text
       OR task_row.active_attempt_id IS DISTINCT FROM NEW.attempt_id
       OR task_row.head_status IS DISTINCT FROM 'RUNNING'
       OR task_row.is_current IS DISTINCT FROM true
       OR task_row.current_graph_id IS DISTINCT FROM NEW.graph_id
       OR task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash
       OR task_row.snapshot_matter_version IS DISTINCT FROM task_row.matter_version
       OR task_row.run_status IS DISTINCT FROM 'EXECUTING'
       OR task_row.is_stale OR task_row.is_cancelled THEN
        RAISE EXCEPTION 'document draft exchange is not a current exact approved task';
    END IF;

    IF jsonb_array_length(task_row.input_refs) <> 1
       OR task_row.input_refs->>0 !~
          '^work-plan-item:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'document draft task input is not one work-plan item';
    END IF;

    SELECT submission_id, external_request_id, destination, request_hash,
           submission_state, recorded_by INTO submission_row
      FROM case_agent_external_submissions
     WHERE submission_id = NEW.submission_record_id
       AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
       AND task_id = NEW.task_id AND firm_id = NEW.firm_id
       AND matter_id = NEW.matter_id;
    IF submission_row IS NULL
       OR submission_row.external_request_id IS DISTINCT FROM
          NEW.external_request_id::text
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
        RAISE EXCEPTION 'document draft 0031 submission boundary is absent or differs';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_draft_exchange_guard
    BEFORE INSERT ON case_agent_document_draft_exchanges
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_document_draft_exchange();

CREATE FUNCTION validate_case_agent_document_draft_outcome()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    exchange_row case_agent_document_draft_exchanges%ROWTYPE;
BEGIN
    SELECT * INTO exchange_row
      FROM case_agent_document_draft_exchanges
     WHERE exchange_id = NEW.exchange_id
       AND external_request_id = NEW.external_request_id
       AND firm_id = NEW.firm_id AND matter_id = NEW.matter_id;
    IF exchange_row.exchange_id IS NULL
       OR exchange_row.request_hash IS DISTINCT FROM NEW.request_hash
       OR exchange_row.binding_hash IS DISTINCT FROM NEW.binding_hash
       OR exchange_row.started_by_worker IS DISTINCT FROM NEW.recorded_by_worker
       OR NULLIF(current_setting('app.actor_id', true), '')::uuid
          IS DISTINCT FROM NEW.recorded_by_worker THEN
        RAISE EXCEPTION 'document draft outcome differs from its exchange';
    END IF;
    IF NEW.outcome_sequence = 2 AND NOT EXISTS (
        SELECT 1 FROM case_agent_document_draft_outcomes prior
         WHERE prior.exchange_id = NEW.exchange_id
           AND prior.external_request_id = NEW.external_request_id
           AND prior.firm_id = NEW.firm_id
           AND prior.matter_id = NEW.matter_id
           AND prior.outcome_sequence = 1
           AND prior.status = 'UNKNOWN_SUBMISSION'
    ) THEN
        RAISE EXCEPTION 'document draft recovery requires one prior unknown outcome';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_document_draft_outcome_guard
    BEFORE INSERT ON case_agent_document_draft_outcomes
    FOR EACH ROW EXECUTE FUNCTION validate_case_agent_document_draft_outcome();

CREATE FUNCTION prohibit_case_agent_document_draft_ledger_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'document draft exchange ledger is append-only';
END;
$$;

CREATE TRIGGER case_agent_document_draft_exchanges_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_draft_exchanges
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_draft_ledger_change();
CREATE TRIGGER case_agent_document_draft_outcomes_append_only
    BEFORE UPDATE OR DELETE ON case_agent_document_draft_outcomes
    FOR EACH ROW EXECUTE FUNCTION prohibit_case_agent_document_draft_ledger_change();

ALTER TABLE case_agent_document_draft_exchanges ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_draft_exchanges FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_document_draft_exchanges_firm_isolation
    ON case_agent_document_draft_exchanges
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

ALTER TABLE case_agent_document_draft_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_agent_document_draft_outcomes FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_document_draft_outcomes_firm_isolation
    ON case_agent_document_draft_outcomes
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

REVOKE ALL ON TABLE case_agent_document_draft_exchanges FROM PUBLIC;
REVOKE ALL ON TABLE case_agent_document_draft_outcomes FROM PUBLIC;

COMMIT;
