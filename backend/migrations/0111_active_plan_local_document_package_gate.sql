-- PostgreSQL 16+; apply after 0110_active_plan_preapproval_policy_reissue.sql.
--
-- An ACTIVE work plan is an explicit lawyer decision to produce its listed
-- internal review candidates.  0110 reissued untouched local document tasks
-- with the correct A2/NONE policy, but the package insert guard still checked
-- the superseded A3/LAWYER_REVIEW task policy.  Keep the guard fail-closed:
-- it admits only the two exact local document adapters, with no network,
-- provider request, retry, or external cost.  The package itself remains
-- NEEDS_LAWYER_REVIEW and cannot become a court submission here.

BEGIN;

CREATE OR REPLACE FUNCTION public.validate_case_agent_reviewable_document_package_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    task_row record;
    plan_row record;
BEGIN
    IF NEW.staged_by IS DISTINCT FROM
       NULLIF(current_setting('app.actor_id', true), '')::uuid THEN
        RAISE EXCEPTION 'reviewable document staging principal differs from transaction identity';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM matter_actor_roles role
        JOIN users principal
          ON principal.user_id = role.user_id AND principal.firm_id = role.firm_id
        WHERE role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id
          AND role.user_id = NEW.staged_by
          AND role.role = 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
          AND principal.status = 'ACTIVE'
    ) OR EXISTS (
        SELECT 1 FROM matter_actor_roles role
        WHERE role.firm_id = NEW.firm_id
          AND role.matter_id = NEW.matter_id
          AND role.user_id = NEW.staged_by
          AND role.role <> 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION 'reviewable document staging requires a dedicated active SYSTEM_WORKER';
    END IF;

    SELECT run.current_graph_id, run.current_graph_hash,
           run.snapshot_hash, run.snapshot_matter_version,
           run.status AS run_status, run.is_stale, run.is_cancelled,
           graph.graph_hash, graph.snapshot_hash AS graph_snapshot_hash,
           task.input_hash, task.input_refs, task.skill_id, task.tool_id,
           task.execution_mode, task.network_policy, task.allowed_domains,
           task.writes_managed_derivatives, task.risk_level,
           task.autonomy_level, task.approval_gate,
           task.external_request_approval_required, task.retry_mode,
           task.resource_budget, head.active_attempt_id,
           head.status AS head_status, head.is_current,
           attempt.status AS attempt_status, matter.version AS matter_version
      INTO task_row
      FROM case_agent_runs run
      JOIN case_agent_task_graphs graph
        ON graph.graph_id = NEW.graph_id AND graph.run_id = run.run_id
       AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
      JOIN case_agent_tasks task
        ON task.graph_id = graph.graph_id AND task.task_id = NEW.task_id
       AND task.run_id = run.run_id AND task.firm_id = run.firm_id
       AND task.matter_id = run.matter_id
      JOIN case_agent_task_heads head
        ON head.graph_id = task.graph_id AND head.task_id = task.task_id
       AND head.run_id = task.run_id AND head.firm_id = task.firm_id
       AND head.matter_id = task.matter_id
      JOIN case_agent_task_attempts attempt
        ON attempt.attempt_id = NEW.attempt_id
       AND attempt.graph_id = task.graph_id AND attempt.task_id = task.task_id
       AND attempt.run_id = task.run_id AND attempt.firm_id = task.firm_id
       AND attempt.matter_id = task.matter_id
      JOIN matters matter
        ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
     WHERE run.run_id = NEW.run_id AND run.firm_id = NEW.firm_id
       AND run.matter_id = NEW.matter_id;

    IF task_row IS NULL
       OR task_row.current_graph_id IS DISTINCT FROM NEW.graph_id
       OR task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash
       OR task_row.snapshot_hash IS DISTINCT FROM NEW.case_snapshot_hash
       OR task_row.graph_snapshot_hash IS DISTINCT FROM NEW.case_snapshot_hash
       OR task_row.input_hash IS DISTINCT FROM NEW.task_input_hash
       OR task_row.run_status <> 'EXECUTING'
       OR task_row.is_stale OR task_row.is_cancelled
       OR task_row.matter_version <> task_row.snapshot_matter_version
       OR NOT (
            (task_row.skill_id = 'dynamic_document_delivery'
             AND task_row.tool_id = 'draft_reviewable_docx_package')
            OR
            (task_row.skill_id = 'dynamic_spreadsheet_delivery'
             AND task_row.tool_id = 'draft_reviewable_xlsx_package')
       )
       OR task_row.execution_mode <> 'IN_PROCESS'
       OR task_row.network_policy <> 'DENY'
       OR task_row.allowed_domains <> '[]'::jsonb
       OR NOT task_row.writes_managed_derivatives
       OR task_row.risk_level <> 'MEDIUM'
       OR task_row.autonomy_level <> 'A2_INTERNAL_REVERSIBLE'
       OR task_row.approval_gate <> 'NONE'
       OR task_row.external_request_approval_required
       OR task_row.retry_mode <> 'NEVER_AUTOMATIC'
       OR COALESCE((task_row.resource_budget->>'max_external_calls')::integer, -1) <> 0
       OR COALESCE((task_row.resource_budget->>'max_attempts')::integer, -1) <> 1
       OR NOT task_row.is_current
       OR task_row.head_status <> 'RUNNING'
       OR task_row.active_attempt_id IS DISTINCT FROM NEW.attempt_id
       OR task_row.attempt_status <> 'RUNNING'
       OR task_row.input_refs IS DISTINCT FROM jsonb_build_array(
            'work-plan-item:' || NEW.work_plan_item_id::text
          ) THEN
        RAISE EXCEPTION 'reviewable document differs from the current bounded local Agent task';
    END IF;

    SELECT plan.status, plan.plan_hash, plan.profile_id, plan.profile_hash,
           plan.activated_matter_version, item.item_kind, item.readiness,
           item.delivery_target, item.deliverable_kind,
           plan_head.current_plan_id, profile_head.current_profile_id,
           profile.status AS profile_status,
           profile.profile_hash AS actual_profile_hash
      INTO plan_row
      FROM case_work_plans plan
      JOIN case_work_plan_heads plan_head
        ON plan_head.matter_id = plan.matter_id AND plan_head.firm_id = plan.firm_id
      JOIN case_work_plan_items item
        ON item.plan_id = plan.plan_id AND item.item_id = NEW.work_plan_item_id
       AND item.firm_id = plan.firm_id AND item.matter_id = plan.matter_id
      JOIN case_posture_profiles profile
        ON profile.profile_id = NEW.posture_profile_id
       AND profile.firm_id = plan.firm_id AND profile.matter_id = plan.matter_id
      JOIN case_posture_profile_heads profile_head
        ON profile_head.matter_id = profile.matter_id
       AND profile_head.firm_id = profile.firm_id
     WHERE plan.plan_id = NEW.work_plan_id AND plan.firm_id = NEW.firm_id
       AND plan.matter_id = NEW.matter_id;

    IF plan_row IS NULL
       OR plan_row.status <> 'ACTIVE'
       OR plan_row.current_plan_id IS DISTINCT FROM NEW.work_plan_id
       OR plan_row.plan_hash IS DISTINCT FROM NEW.work_plan_hash
       OR plan_row.profile_id IS DISTINCT FROM NEW.posture_profile_id
       OR plan_row.profile_hash IS DISTINCT FROM NEW.posture_profile_hash
       OR plan_row.actual_profile_hash IS DISTINCT FROM NEW.posture_profile_hash
       OR plan_row.current_profile_id IS DISTINCT FROM NEW.posture_profile_id
       OR plan_row.profile_status <> 'CONFIRMED'
       OR plan_row.activated_matter_version <> task_row.snapshot_matter_version
       OR plan_row.item_kind <> 'DOCUMENT_CANDIDATE'
       OR plan_row.readiness <> 'ACTIONABLE'
       OR plan_row.delivery_target = 'NOT_APPLICABLE'
       OR plan_row.deliverable_kind IS DISTINCT FROM NEW.deliverable_kind THEN
        RAISE EXCEPTION 'reviewable document differs from the active dynamic work plan';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.validate_case_agent_reviewable_document_package_insert() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.validate_case_agent_reviewable_document_package_insert()
    TO lawcase_agent_worker;

COMMIT;
