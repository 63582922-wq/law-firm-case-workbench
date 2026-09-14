-- ADR-0101: explicit reissue after a proven local pre-task planning rejection.
BEGIN;
CREATE FUNCTION public.case_agent_local_plan_reissue_allowed(p_run uuid, p_firm uuid, p_matter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
SELECT p_firm::text = current_setting('app.firm_id', true)
 AND EXISTS (
    SELECT 1 FROM public.case_agent_runs r
    JOIN public.case_agent_goals g ON g.goal_id=r.goal_id AND g.firm_id=r.firm_id AND g.matter_id=r.matter_id
    WHERE r.run_id=p_run AND r.firm_id=p_firm AND r.matter_id=p_matter
      AND r.status='WAITING_INPUT' AND r.failure_code='PLANNER_PROPOSAL_REJECTED'
      AND NOT r.is_stale AND NOT r.is_cancelled AND r.current_graph_id IS NULL
      AND g.active_plan_execution IS NOT NULL
 )
 AND (SELECT count(*) FROM public.case_agent_planning_attempts a
      WHERE a.run_id=p_run AND a.firm_id=p_firm AND a.matter_id=p_matter)=1
 AND EXISTS (
    SELECT 1 FROM public.case_agent_planning_attempts a
    JOIN public.case_agent_planning_local_events l ON l.planning_attempt_id=a.planning_attempt_id
      AND l.run_id=a.run_id AND l.firm_id=a.firm_id AND l.matter_id=a.matter_id
    WHERE a.run_id=p_run AND a.firm_id=p_firm AND a.matter_id=p_matter
      AND a.status='FAILED' AND l.planner_id='controlled-first-release-defence-planner-v1'
 )
 AND NOT EXISTS (SELECT 1 FROM public.case_agent_tasks t WHERE t.run_id=p_run)
 AND NOT EXISTS (SELECT 1 FROM public.case_agent_planning_external_events e WHERE e.run_id=p_run)
 AND NOT EXISTS (SELECT 1 FROM public.case_agent_external_submissions e WHERE e.run_id=p_run);
$$;
REVOKE ALL ON FUNCTION public.case_agent_local_plan_reissue_allowed(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.case_agent_local_plan_reissue_allowed(uuid,uuid,uuid) TO lawcase_web_application;

-- Retain the complete existing authority and append-only predecessor checks.
-- Replace only its known-local-failure predicate, failing closed on schema drift.
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
    IF position(old_predicate IN definition)=0 THEN
        RAISE EXCEPTION 'active-plan reissue predicate differs from expected predecessor';
    END IF;
    EXECUTE replace(definition, old_predicate,
        'AND (public.case_agent_local_plan_reissue_allowed(predecessor.run_id, predecessor.firm_id, predecessor.matter_id) OR '
        || substring(old_predicate FROM 5) || ')');
END;
$migration$;
COMMIT;
