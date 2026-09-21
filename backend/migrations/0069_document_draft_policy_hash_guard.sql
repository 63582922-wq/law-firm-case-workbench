-- PostgreSQL 16+; apply after 0068_case_agent_active_followup_gate_owner.sql.
-- Keep the durable document-exchange trigger aligned with the exact runtime
-- adapter manifests.  A stale hash must fail Worker startup, not appear after
-- a lawyer has approved a task as an ambiguous provider submission.

BEGIN;

CREATE OR REPLACE FUNCTION validate_case_agent_document_draft_exchange()
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
                 'a9e693a8a46c34469573ca67393d8ce5a95764e37d0e74e7c2b15b945a7e14c4')
            OR
            (task_row.skill_id = 'dynamic_spreadsheet_delivery'
             AND task_row.tool_id = 'draft_reviewable_xlsx_package'
             AND task_row.adapter_id = 'dynamic-reviewable-xlsx-delivery'
             AND task_row.sandbox_policy_hash =
                 '22286b322d62e850f56ff7e4e8dd82493ca8df5f39e1f42bf20f6f951adb38ed')
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

COMMIT;
