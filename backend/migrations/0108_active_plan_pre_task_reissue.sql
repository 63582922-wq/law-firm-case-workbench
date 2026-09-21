-- PostgreSQL 16+; apply after 0107_partial_internal_deliverable_execution.sql.
--
-- A corrected local planner may be reissued once when the predecessor stopped
-- before graph creation, task creation, or any external activity.  This is a
-- recovery of the same ACTIVE-plan authority, not a new model invocation or
-- a way to broaden the selected deliverables.

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
 AND EXISTS (
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
 )
 AND (
    SELECT count(*)
    FROM public.case_agent_planning_attempts attempt
    WHERE attempt.run_id = p_run
      AND attempt.firm_id = p_firm
      AND attempt.matter_id = p_matter
      AND attempt.status = 'FAILED'
 ) = 1
 AND NOT EXISTS (
    SELECT 1
    FROM public.case_agent_tasks task
    WHERE task.run_id = p_run
      AND task.firm_id = p_firm
      AND task.matter_id = p_matter
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

-- Migration 0107 intentionally replaced the execution guard to admit a
-- source-bound subset.  Reapply the existing safe-local reissue predicate to
-- that current definition, failing closed if the predecessor body changed.
DO $migration$
DECLARE
    definition text;
    old_predicate text := $predicate$AND EXISTS (
                    SELECT 1
                    FROM public.case_agent_task_receipts receipt
                    WHERE receipt.run_id = predecessor.run_id
                      AND receipt.firm_id = predecessor.firm_id
                      AND receipt.matter_id = predecessor.matter_id
                      AND receipt.result_status = 'FAILED'
                      AND receipt.external_submission_state = 'NOT_APPLICABLE'
                      AND receipt.error_code IN (
                          'LOCAL_ADAPTER_FAILED',
                          'DOCUMENT_RENDERER_REJECTED',
                          'DOCUMENT_RENDERER_RESULT_UNKNOWN'
                      )
              )$predicate$;
BEGIN
    definition := pg_get_functiondef('public.guard_case_agent_active_plan_execution_run()'::regprocedure);
    IF position(old_predicate IN definition) = 0 THEN
        RAISE EXCEPTION 'active-plan reissue predicate differs from expected predecessor';
    END IF;
    EXECUTE replace(
        definition,
        old_predicate,
        'AND (public.case_agent_local_plan_reissue_allowed(predecessor.run_id, predecessor.firm_id, predecessor.matter_id) OR '
        || substring(old_predicate FROM 5) || ')'
    );
END;
$migration$;

REVOKE ALL ON FUNCTION public.guard_case_agent_active_plan_execution_run()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

COMMIT;
