-- Same-run, explicitly reviewed supplementary extraction. Original budget stays immutable.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE case_agent_events DROP CONSTRAINT case_agent_events_event_type_check;
ALTER TABLE case_agent_events ADD CONSTRAINT case_agent_events_event_type_check CHECK (event_type IN (
 'RUN_CREATED','PLANNING_STARTED','PLANNING_FAILED','PLANNING_BUDGET_REVIEWED','PLANNING_MATERIAL_SCOPE_REVIEWED',
 'SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED','PLANNING_RESULT_UNKNOWN','TASK_GRAPH_ACCEPTED','APPROVAL_GRANTED',
 'LAWYER_PLAN_CORRECTION_RECORDED','TASK_STARTED','TASK_RESULT_RECORDED','CASE_SNAPSHOT_CHANGED',
 'RUN_PAUSED','RUN_RESUMED','RUN_CANCELLED','VERIFICATION_STARTED','VERIFICATION_PASSED','VERIFICATION_FAILED','RUN_COMPLETED'
));

CREATE FUNCTION guard_case_agent_supplementary_material_stage() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE head public.case_agent_runs%ROWTYPE; checkpoint jsonb; stage jsonb;
 old_budget jsonb; used jsonb; expected_budget jsonb; refs jsonb;
BEGIN
 IF NEW.event_type <> 'SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED' THEN RETURN NEW; END IF;
 SELECT * INTO STRICT head FROM public.case_agent_runs
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id FOR UPDATE;
 IF NOT COALESCE(current_user='lawcase_web_application' AND head.status='READY_FOR_REVIEW'
  AND NOT head.is_stale AND NOT head.is_cancelled AND head.verification_hash IS NOT NULL
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
  RAISE EXCEPTION 'supplementary material stage state or reviewer differs';
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
  'max_output_bytes',greatest((old_budget->>'max_output_bytes')::bigint,(used->>'output_bytes')::bigint+67108864));
 IF NOT COALESCE(NEW.payload=jsonb_build_object('stage',stage)
  AND stage->>'run_id'=NEW.run_id::text AND (stage->>'expected_event_version')::bigint=head.current_event_version
  AND stage->>'snapshot_hash'=head.snapshot_hash AND stage->>'previous_graph_hash'=head.current_graph_hash
  AND stage->>'approved_by'=NEW.actor_id::text AND stage->'previous_budget'=old_budget
  AND stage->'proposed_budget'=expected_budget
  AND stage->>'stage_hash'=encode(public.digest(convert_to(public.case_agent_planning_compact_json(stage-'stage_hash'),'UTF8'),'sha256'),'hex'),false) THEN
  RAISE EXCEPTION 'supplementary material stage bindings or budget differ';
 END IF;
 -- Require exactly the currently unprocessed pages; upload or candidate count is not processing proof.
 SELECT jsonb_agg('evidence-page:'||page.evidence_page_id::text ORDER BY page.evidence_page_id::text)
 INTO refs FROM public.evidence_pages page JOIN public.evidence_original_files original
  ON original.evidence_file_id=page.evidence_file_id AND original.firm_id=page.firm_id
   AND original.matter_id=page.matter_id
 WHERE page.firm_id=NEW.firm_id AND page.matter_id=NEW.matter_id AND NOT EXISTS (
  SELECT 1 FROM public.case_agent_tasks task JOIN public.case_agent_task_graphs graph
   ON graph.graph_id=task.graph_id AND graph.run_id=task.run_id AND graph.firm_id=task.firm_id AND graph.matter_id=task.matter_id
  JOIN public.case_agent_task_attempts attempt ON attempt.task_id=task.task_id AND attempt.graph_id=task.graph_id
   AND attempt.run_id=task.run_id AND attempt.firm_id=task.firm_id AND attempt.matter_id=task.matter_id AND attempt.status='SUCCEEDED'
  JOIN public.case_agent_verification_receipts receipt ON receipt.run_id=task.run_id AND receipt.firm_id=task.firm_id
   AND receipt.matter_id=task.matter_id AND receipt.graph_hash=graph.graph_hash AND receipt.outcome='PASSED'
  WHERE task.firm_id=page.firm_id AND task.matter_id=page.matter_id AND task.tool_id='extract_case_ledger'
   AND task.input_refs ? ('evidence-page:'||page.evidence_page_id::text));
 IF refs IS NULL OR stage->'page_refs' IS DISTINCT FROM refs THEN
  RAISE EXCEPTION 'supplementary source coverage changed';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER case_agent_supplementary_material_stage_guard BEFORE INSERT ON case_agent_events
 FOR EACH ROW EXECUTE FUNCTION guard_case_agent_supplementary_material_stage();

CREATE FUNCTION public.case_agent_is_bounded_supplementary_material_stage(p_run_id uuid,p_firm_id uuid,p_matter_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT EXISTS (
  SELECT 1 FROM public.case_agent_runs r JOIN public.matters matter
   ON matter.matter_id=r.matter_id AND matter.firm_id=r.firm_id
  JOIN LATERAL (SELECT payload->'stage' AS stage FROM public.case_agent_events
   WHERE run_id=r.run_id AND firm_id=r.firm_id AND matter_id=r.matter_id
    AND event_type='SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED'
   ORDER BY event_sequence DESC LIMIT 1) review ON true
  WHERE r.run_id=p_run_id AND r.firm_id=p_firm_id AND r.matter_id=p_matter_id
   AND p_firm_id::text=current_setting('app.firm_id',true) AND NOT r.is_cancelled
   AND r.snapshot_matter_version=matter.version AND r.snapshot_hash=review.stage->>'snapshot_hash'
   AND (r.current_graph_hash=review.stage->>'previous_graph_hash' OR (
    EXISTS (SELECT 1 FROM public.case_agent_tasks t WHERE t.graph_id=r.current_graph_id
     AND t.run_id=r.run_id AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id AND t.tool_id='extract_case_ledger')
    AND NOT EXISTS (SELECT 1 FROM public.case_agent_tasks t WHERE t.graph_id=r.current_graph_id
     AND t.run_id=r.run_id AND t.firm_id=r.firm_id AND t.matter_id=r.matter_id
     AND NOT ((t.tool_id='extract_pdf_text' AND t.skill_id='pdf_reading')
      OR (t.tool_id='extract_case_ledger' AND t.skill_id='case_ledger_extraction')
      OR (t.tool_id='plan_authoritative_rule_research' AND t.skill_id='legal_rule_research_planning')))
   ))
 );
$$;
REVOKE ALL ON FUNCTION public.case_agent_is_bounded_supplementary_material_stage(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.case_agent_is_bounded_supplementary_material_stage(uuid,uuid,uuid)
 TO lawcase_agent_worker,lawcase_ledger_confirmation_owner;

-- Reuse the exact healthy-control exception in both wake and claim without changing ownership.
DO $patch$
DECLARE original text;
BEGIN
 SELECT pg_get_functiondef('public.case_agent_is_bounded_pending_review_analysis(uuid,uuid,uuid)'::regprocedure) INTO original;
 IF strpos(original,'SELECT EXISTS (')=0 OR strpos(original,'case_agent_is_bounded_supplementary_material_stage')>0 THEN
  RAISE EXCEPTION 'pending review function differs';
 END IF;
 EXECUTE replace(original,'SELECT EXISTS (',
  'SELECT public.case_agent_is_bounded_supplementary_material_stage(p_run_id,p_firm_id,p_matter_id) OR EXISTS (');
END;
$patch$;
COMMIT;
