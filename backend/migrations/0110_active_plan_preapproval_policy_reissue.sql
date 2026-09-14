-- PostgreSQL 16+; apply after 0109_active_plan_compile_failure_reissue.sql.
--
-- An ACTIVE plan is the lead lawyer's bounded instruction to create the
-- selected, local, review-only candidates.  If an older server policy still
-- asks for a second per-task approval before any candidate is created, permit
-- one replacement run only after proving that the graph has made no attempt,
-- external call, submission, or artifact.

BEGIN;

CREATE OR REPLACE FUNCTION public.case_agent_local_plan_reissue_allowed(
    p_run uuid,
    p_firm uuid,
    p_matter uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
SELECT p_firm::text = current_setting('app.firm_id', true)
 AND (
    -- Existing compiler/pre-graph recovery: the local planner stopped before
    -- it could persist a graph or create any task.
    EXISTS (
        SELECT 1
        FROM public.case_agent_runs run
        JOIN public.case_agent_goals goal
          ON goal.goal_id = run.goal_id
         AND goal.firm_id = run.firm_id
         AND goal.matter_id = run.matter_id
        WHERE run.run_id = p_run
          AND run.firm_id = p_firm
          AND run.matter_id = p_matter
          AND run.status = 'WAITING_INPUT'
          AND run.failure_code = 'PLANNER_PROPOSAL_REJECTED'
          AND NOT run.is_stale
          AND NOT run.is_cancelled
          AND run.current_graph_id IS NULL
          AND goal.active_plan_execution IS NOT NULL
          AND (
              SELECT count(*)
              FROM public.case_agent_planning_attempts attempt
              WHERE attempt.run_id = p_run
                AND attempt.firm_id = p_firm
                AND attempt.matter_id = p_matter
                AND attempt.status IN ('FAILED', 'SUCCEEDED')
          ) = 1
          AND NOT EXISTS (
              SELECT 1
              FROM public.case_agent_tasks task
              WHERE task.run_id = p_run
                AND task.firm_id = p_firm
                AND task.matter_id = p_matter
          )
    )
    OR EXISTS (
        -- One historical graph may have been compiled with the obsolete
        -- per-task LAWYER_REVIEW gate.  It is eligible only when every task
        -- is an untouched, local ACTIVE-plan document projection and no
        -- earlier graph-gate recovery exists for this plan lineage.
        SELECT 1
        FROM public.case_agent_runs run
        JOIN public.case_agent_goals goal
          ON goal.goal_id = run.goal_id
         AND goal.firm_id = run.firm_id
         AND goal.matter_id = run.matter_id
        JOIN public.case_agent_active_plan_execution_runs execution
          ON execution.run_id = run.run_id
         AND execution.firm_id = run.firm_id
         AND execution.matter_id = run.matter_id
        WHERE run.run_id = p_run
          AND run.firm_id = p_firm
          AND run.matter_id = p_matter
          AND run.status = 'WAITING_APPROVAL'
          AND run.failure_code IS NULL
          AND NOT run.is_stale
          AND NOT run.is_cancelled
          AND run.current_graph_id IS NOT NULL
          AND goal.active_plan_execution IS NOT NULL
          AND (
              SELECT count(*)
              FROM public.case_agent_planning_attempts attempt
              WHERE attempt.run_id = p_run
                AND attempt.firm_id = p_firm
                AND attempt.matter_id = p_matter
                AND attempt.status = 'SUCCEEDED'
          ) = 1
          AND (
              SELECT count(*)
              FROM public.case_agent_tasks task
              WHERE task.run_id = p_run
                AND task.firm_id = p_firm
                AND task.matter_id = p_matter
          ) BETWEEN 1 AND 4
          AND NOT EXISTS (
              SELECT 1
              FROM public.case_agent_tasks task
              WHERE task.run_id = p_run
                AND task.firm_id = p_firm
                AND task.matter_id = p_matter
                AND (
                    task.skill_id NOT IN (
                        'dynamic_document_delivery',
                        'dynamic_spreadsheet_delivery'
                    )
                    OR task.approval_gate <> 'LAWYER_REVIEW'
                    OR task.autonomy_level <> 'A3_LAWYER_APPROVAL'
                    OR task.network_policy <> 'DENY'
                    OR task.external_request_approval_required
                    OR NOT task.writes_managed_derivatives
                    OR jsonb_array_length(task.input_refs) <> 1
                    OR NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(goal.active_plan_execution->'items') item
                        WHERE concat('work-plan-item:', item->>'item_id')
                              = task.input_refs->>0
                    )
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM public.case_agent_active_plan_execution_runs prior_execution
              JOIN public.case_agent_runs prior_run
                ON prior_run.run_id = prior_execution.run_id
               AND prior_run.firm_id = prior_execution.firm_id
               AND prior_run.matter_id = prior_execution.matter_id
              WHERE prior_execution.firm_id = execution.firm_id
                AND prior_execution.matter_id = execution.matter_id
                AND prior_execution.plan_id = execution.plan_id
                AND prior_execution.plan_hash = execution.plan_hash
                AND prior_execution.source_run_id = execution.source_run_id
                AND prior_execution.run_id <> p_run
                AND prior_run.current_graph_id IS NOT NULL
          )
    )
 )
 AND NOT EXISTS (
    SELECT 1
    FROM public.case_agent_task_receipts receipt
    WHERE receipt.run_id = p_run
      AND receipt.firm_id = p_firm
      AND receipt.matter_id = p_matter
 )
 AND NOT EXISTS (
    SELECT 1
    FROM public.case_agent_planning_external_events event
    WHERE event.run_id = p_run
      AND event.firm_id = p_firm
      AND event.matter_id = p_matter
 )
 AND NOT EXISTS (
    SELECT 1
    FROM public.case_agent_external_submissions submission
    WHERE submission.run_id = p_run
      AND submission.firm_id = p_firm
      AND submission.matter_id = p_matter
 );
$$;

REVOKE ALL ON FUNCTION public.case_agent_local_plan_reissue_allowed(uuid, uuid, uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.case_agent_local_plan_reissue_allowed(uuid, uuid, uuid)
    TO lawcase_web_application;

-- The current guard already defers the failure proof to the function above.
-- Its historical WAITING_INPUT literal must also admit the strictly proven
-- graph-only approval mismatch, while failed task receipts remain governed by
-- their original safe-local predicate.
DO $migration$
DECLARE
    definition text;
    old_status_predicate text := 'AND predecessor_run.status = ''WAITING_INPUT''';
BEGIN
    definition := pg_get_functiondef('public.guard_case_agent_active_plan_execution_run()'::regprocedure);
    IF position(old_status_predicate IN definition) = 0
       OR position('case_agent_local_plan_reissue_allowed(predecessor.run_id' IN definition) = 0 THEN
        RAISE EXCEPTION 'active-plan reissue guard differs from expected predecessor';
    END IF;
    EXECUTE replace(
        definition,
        old_status_predicate,
        'AND predecessor_run.status IN (''WAITING_INPUT'', ''WAITING_APPROVAL'')'
    );
END;
$migration$;

REVOKE ALL ON FUNCTION public.guard_case_agent_active_plan_execution_run()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

COMMIT;
