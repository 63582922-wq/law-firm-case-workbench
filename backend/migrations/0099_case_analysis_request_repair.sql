-- Same-run analysis after verified extraction, preserving cumulative usage.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE case_agent_events DROP CONSTRAINT case_agent_events_event_type_check;
ALTER TABLE case_agent_events ADD CONSTRAINT case_agent_events_event_type_check CHECK (event_type IN (
 'RUN_CREATED','PLANNING_STARTED','PLANNING_FAILED','PLANNING_BUDGET_REVIEWED','PLANNING_MATERIAL_SCOPE_REVIEWED',
 'SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED','CASE_ANALYSIS_STAGE_REVIEWED','CASE_ANALYSIS_REVISION_REVIEWED','CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED','PLANNING_RESULT_UNKNOWN','TASK_GRAPH_ACCEPTED','APPROVAL_GRANTED',
 'LAWYER_PLAN_CORRECTION_RECORDED','TASK_STARTED','TASK_RESULT_RECORDED','CASE_SNAPSHOT_CHANGED',
 'RUN_PAUSED','RUN_RESUMED','RUN_CANCELLED','VERIFICATION_STARTED','VERIFICATION_PASSED','VERIFICATION_FAILED','RUN_COMPLETED'
));

CREATE FUNCTION guard_case_agent_case_analysis_request_repair() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE head public.case_agent_runs%ROWTYPE; checkpoint jsonb; stage jsonb;
 old_budget jsonb; used jsonb; expected_budget jsonb; refs jsonb;
BEGIN
 IF NEW.event_type <> 'CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED' THEN RETURN NEW; END IF;
 SELECT * INTO STRICT head FROM public.case_agent_runs
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id FOR UPDATE;
 IF NOT COALESCE(current_user='lawcase_web_application' AND head.status='WAITING_INPUT'
  AND NOT head.is_stale AND NOT head.is_cancelled
  AND NEW.event_sequence=head.current_event_version+1
  AND EXISTS (SELECT 1 FROM public.matters WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id
    AND version=head.snapshot_matter_version)
  AND EXISTS (SELECT 1 FROM public.users principal JOIN public.matter_actor_roles assignment
    ON assignment.user_id=principal.user_id AND assignment.firm_id=principal.firm_id
    WHERE principal.user_id=NEW.actor_id AND principal.firm_id=NEW.firm_id AND principal.status='ACTIVE'
     AND assignment.matter_id=NEW.matter_id AND assignment.revoked_at IS NULL
     AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))
  AND EXISTS (SELECT 1 FROM public.case_agent_goals WHERE goal_id=head.goal_id
    AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id AND active_plan_execution IS NULL),false) THEN
  RAISE EXCEPTION 'case analysis stage state or reviewer differs';
 END IF;
 SELECT projection INTO STRICT checkpoint FROM public.case_agent_checkpoints
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id
  AND event_version=head.current_event_version;
 stage:=NEW.payload->'stage'; old_budget:=checkpoint->'budget'; used:=checkpoint->'budget_usage';
 expected_budget:=old_budget || jsonb_build_object(
  'max_total_attempts',greatest((old_budget->>'max_total_attempts')::bigint,(used->>'attempts')::bigint+3),
  'max_external_calls',greatest((old_budget->>'max_external_calls')::bigint,(used->>'external_calls')::bigint+1),
  'max_runtime_seconds',greatest((old_budget->>'max_runtime_seconds')::bigint,(used->>'runtime_seconds')::bigint+1200),
  'max_cost_minor_units',greatest((old_budget->>'max_cost_minor_units')::bigint,(used->>'cost_minor_units')::bigint+120),
  'max_output_bytes',greatest((old_budget->>'max_output_bytes')::bigint,(used->>'output_bytes')::bigint+4194304));
 IF NOT COALESCE(NEW.payload=jsonb_build_object('stage',stage)
  AND stage->>'run_id'=NEW.run_id::text AND (stage->>'expected_event_version')::bigint=head.current_event_version
  AND stage->>'snapshot_hash'=head.snapshot_hash AND stage->>'previous_graph_hash'=head.current_graph_hash
  AND stage->>'approved_by'=NEW.actor_id::text AND stage->'previous_budget'=old_budget
  AND stage->'proposed_budget'=expected_budget
  AND stage->>'stage_hash'=encode(public.digest(convert_to(public.case_agent_planning_compact_json(stage-'stage_hash'),'UTF8'),'sha256'),'hex'),false) THEN
  RAISE EXCEPTION 'case analysis stage bindings or budget differ';
 END IF;
 -- A changed request can follow one complete HTTP 400 rejection, never an unknown outcome.
 IF checkpoint->'analysis_stage' ? 'failed_attempt_id'
    OR NOT COALESCE(length(btrim(stage->>'repair_reason')) BETWEEN 10 AND 1000,false)
    OR NOT COALESCE(stage->>'repaired_request_hash' ~ '^[0-9a-f]{64}$',false)
    OR stage->>'repaired_request_hash'=stage->>'failed_request_hash'
    OR NOT EXISTS (SELECT 1 FROM public.case_agent_external_submissions submission
      JOIN public.case_agent_tasks task ON task.task_id=submission.task_id AND task.run_id=submission.run_id
       AND task.firm_id=submission.firm_id AND task.matter_id=submission.matter_id
      JOIN public.case_agent_events event ON event.run_id=submission.run_id AND event.firm_id=submission.firm_id
       AND event.matter_id=submission.matter_id AND event.event_type='TASK_RESULT_RECORDED'
      WHERE submission.run_id=NEW.run_id AND submission.firm_id=NEW.firm_id AND submission.matter_id=NEW.matter_id
       AND submission.attempt_id::text=stage->>'failed_attempt_id'
       AND submission.request_hash=stage->>'failed_request_hash'
       AND task.graph_id=head.current_graph_id AND task.tool_id='analyze_lawyer_decision_package'
       AND event.payload->'receipt'->>'attempt_id'=stage->>'failed_attempt_id'
       AND event.payload->'receipt'->>'status'='FAILED'
       AND event.payload->'receipt'->>'error_code'='LAWYER_ANALYSIS_HTTP_400'
       AND event.payload->'receipt'->>'cost_minor_units'='0'
       AND event.payload->'receipt'->'artifacts'='[]'::jsonb) THEN
  RAISE EXCEPTION 'request repair has no changed request and known HTTP rejection';
 END IF;
 IF jsonb_typeof(stage->'candidate_bindings') IS DISTINCT FROM 'array'
    OR jsonb_array_length(stage->'candidate_bindings')=0 THEN
  RAISE EXCEPTION 'analysis candidate scope is empty';
 END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(stage->'candidate_bindings') binding
   WHERE jsonb_typeof(binding)<>'array' OR jsonb_array_length(binding)<>2
    OR (binding->>1) !~ '^[0-9a-f]{64}$'
    OR NOT EXISTS (SELECT 1 FROM public.case_agent_ledger_extraction_candidates c
       WHERE c.firm_id=NEW.firm_id AND c.matter_id=NEW.matter_id
        AND c.review_status='NEEDS_LAWYER_REVIEW'
        AND (binding->>0)=(CASE c.candidate_kind WHEN 'FACT' THEN 'fact-candidate:'
                         WHEN 'TRANSACTION' THEN 'transaction-candidate:' END)||c.extraction_candidate_id::text))
 THEN RAISE EXCEPTION 'analysis candidate scope differs'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_case_analysis_request_repair_guard BEFORE INSERT ON case_agent_events
 FOR EACH ROW EXECUTE FUNCTION guard_case_agent_case_analysis_request_repair();

CREATE FUNCTION public.case_agent_is_bounded_case_analysis_request_repair(p_run_id uuid,p_firm_id uuid,p_matter_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT EXISTS (
  SELECT 1 FROM public.case_agent_runs r JOIN public.matters matter
   ON matter.matter_id=r.matter_id AND matter.firm_id=r.firm_id
  JOIN LATERAL (SELECT payload->'stage' AS stage FROM public.case_agent_events
   WHERE run_id=r.run_id AND firm_id=r.firm_id AND matter_id=r.matter_id
    AND event_type='CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED'
   ORDER BY event_sequence DESC LIMIT 1) review ON true
  WHERE r.run_id=p_run_id AND r.firm_id=p_firm_id AND r.matter_id=p_matter_id
   AND p_firm_id::text=current_setting('app.firm_id',true) AND NOT r.is_cancelled
   AND r.snapshot_matter_version=matter.version AND r.snapshot_hash=review.stage->>'snapshot_hash'
   AND (r.current_graph_hash=review.stage->>'previous_graph_hash' OR (
    EXISTS (SELECT 1 FROM public.case_agent_tasks t WHERE t.graph_id=r.current_graph_id
     AND t.run_id=r.run_id AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id AND t.tool_id='analyze_lawyer_decision_package')
    AND NOT EXISTS (SELECT 1 FROM public.case_agent_tasks t WHERE t.graph_id=r.current_graph_id
     AND t.run_id=r.run_id AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id
     AND NOT ((t.tool_id='review_case_context' AND t.skill_id='case_context_review')
      OR (t.tool_id='analyze_lawyer_decision_package' AND t.skill_id='lawyer_decision_package')
      OR (t.tool_id='plan_authoritative_rule_research' AND t.skill_id='legal_rule_research_planning')))
   ))
 );
$$;
REVOKE ALL ON FUNCTION public.case_agent_is_bounded_case_analysis_request_repair(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.case_agent_is_bounded_case_analysis_request_repair(uuid,uuid,uuid)
 TO lawcase_agent_worker,lawcase_ledger_confirmation_owner;

-- Reuse the exact healthy-control exception in both wake and claim without changing ownership.
DO $patch$
DECLARE original text;
BEGIN
 SELECT pg_get_functiondef('public.case_agent_is_bounded_pending_review_analysis(uuid,uuid,uuid)'::regprocedure) INTO original;
 IF strpos(original,'SELECT public.case_agent_is_bounded_case_analysis_revision(')=0
    OR strpos(original,'case_agent_is_bounded_case_analysis_request_repair')>0 THEN
  RAISE EXCEPTION 'pending review function differs';
 END IF;
 EXECUTE replace(original,'SELECT public.case_agent_is_bounded_case_analysis_revision(',
  'SELECT public.case_agent_is_bounded_case_analysis_request_repair(p_run_id,p_firm_id,p_matter_id) OR public.case_agent_is_bounded_case_analysis_revision(');
END;
$patch$;
COMMIT;
