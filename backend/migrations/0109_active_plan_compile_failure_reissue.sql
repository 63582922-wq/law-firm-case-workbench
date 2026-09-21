-- PostgreSQL 16+; apply after 0108_active_plan_pre_task_reissue.sql.
--
-- The active-plan compiler can reject before graph persistence when the
-- server-owned run envelope is below the sum of its already selected local
-- document budgets.  This is still a proven zero-external, zero-task local
-- failure.  Permit one explicit recovery of that exact plan lineage.

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
      AND attempt.status IN ('FAILED', 'SUCCEEDED')
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

COMMIT;
