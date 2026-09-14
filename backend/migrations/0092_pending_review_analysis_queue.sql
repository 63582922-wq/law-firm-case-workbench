-- ADR-0103: analyse pending work without taking over its control cursor.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE FUNCTION public.case_agent_is_bounded_pending_review_analysis(
    p_run_id uuid, p_firm_id uuid, p_matter_id uuid
) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog
AS $body$
    SELECT EXISTS (
        SELECT 1 FROM public.case_agent_runs r
        JOIN public.case_agent_goals g
          ON g.goal_id=r.goal_id AND g.firm_id=r.firm_id AND g.matter_id=r.matter_id
        WHERE r.run_id=p_run_id AND r.firm_id=p_firm_id AND r.matter_id=p_matter_id
          AND p_firm_id::text = current_setting('app.firm_id', true)
          AND g.requested_deliverables='["DEFENCE_STATEMENT"]'::jsonb
          AND g.active_plan_execution IS NULL
          AND NOT r.is_cancelled AND NOT r.is_stale
          AND (r.run_budget->>'max_external_calls')::int=1
          AND (r.run_budget->>'max_cost_minor_units')::int BETWEEN 1 AND 120
          AND (r.current_graph_id IS NULL OR (
              EXISTS (SELECT 1 FROM public.case_agent_tasks t
                  WHERE t.graph_id=r.current_graph_id AND t.run_id=r.run_id
                    AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id)
              AND NOT EXISTS (SELECT 1 FROM public.case_agent_tasks t
                  WHERE t.graph_id=r.current_graph_id AND t.run_id=r.run_id
                    AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id
                    AND (t.tool_id NOT IN ('review_case_context','analyze_lawyer_decision_package')
                      OR t.skill_id NOT IN ('case_context_review','lawyer_decision_package')))
          ))
    )
$body$;
REVOKE ALL ON FUNCTION public.case_agent_is_bounded_pending_review_analysis(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.case_agent_is_bounded_pending_review_analysis(uuid,uuid,uuid)
    TO lawcase_agent_worker, lawcase_ledger_confirmation_owner;

-- Keep all historical recovery/quarantine/transfer checks byte-for-byte.
-- Refuse installation if the expected single active-followup branch changed.
DO $patch$
DECLARE
    original text;
    needle text := 'OR assignment.control_run_id <> NEW.run_id';
    replacement text := 'OR (assignment.control_run_id <> NEW.run_id AND NOT public.case_agent_is_bounded_pending_review_analysis(NEW.run_id, NEW.firm_id, NEW.matter_id))';
BEGIN
    SELECT pg_get_functiondef('public.wake_case_agent_run()'::regprocedure) INTO original;
    IF (length(original)-length(replace(original,needle,'')))/length(needle) <> 1 THEN
        RAISE EXCEPTION 'pending review wake branch differs; migration refused';
    END IF;
    EXECUTE replace(original,needle,replacement);
END
$patch$;
COMMIT;
