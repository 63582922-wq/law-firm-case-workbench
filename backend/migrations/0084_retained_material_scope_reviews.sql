-- ADR-0095. Original goals/responses remain immutable; scope is event-derived.
BEGIN;
ALTER TABLE case_agent_events DROP CONSTRAINT case_agent_events_event_type_check;
ALTER TABLE case_agent_events ADD CONSTRAINT case_agent_events_event_type_check CHECK (event_type IN (
 'RUN_CREATED','PLANNING_STARTED','PLANNING_FAILED','PLANNING_BUDGET_REVIEWED','PLANNING_MATERIAL_SCOPE_REVIEWED',
 'PLANNING_RESULT_UNKNOWN','TASK_GRAPH_ACCEPTED','APPROVAL_GRANTED','LAWYER_PLAN_CORRECTION_RECORDED',
 'TASK_STARTED','TASK_RESULT_RECORDED','CASE_SNAPSHOT_CHANGED','RUN_PAUSED','RUN_RESUMED','RUN_CANCELLED',
 'VERIFICATION_STARTED','VERIFICATION_PASSED','VERIFICATION_FAILED','RUN_COMPLETED'
));

CREATE FUNCTION guard_case_agent_material_scope_review() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE head public.case_agent_runs%ROWTYPE; checkpoint jsonb; attempt record; current_version integer;
 review jsonb; effective_goal jsonb; derived jsonb; graph jsonb; refs jsonb; fingerprint text;
 item jsonb; keys text[] := ARRAY['snapshot','original_goal_hash','original_proposal_hash','request_hash',
 'planning_hash','material_read_refs','previous_output_bytes','approved_output_bytes','effective_goal_hash',
 'derived_proposal_hash','compiled_graph_hash','approved_by'];
BEGIN
 IF NEW.event_type NOT IN ('PLANNING_MATERIAL_SCOPE_REVIEWED','TASK_GRAPH_ACCEPTED') THEN RETURN NEW; END IF;
 SELECT * INTO STRICT head FROM public.case_agent_runs
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id FOR UPDATE;
 SELECT version INTO STRICT current_version FROM public.matters
 WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id FOR UPDATE;
 IF NEW.event_type='TASK_GRAPH_ACCEPTED' THEN
  SELECT payload INTO review FROM public.case_agent_events WHERE run_id=NEW.run_id
   AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id AND event_type='PLANNING_MATERIAL_SCOPE_REVIEWED';
  IF review IS NULL THEN RETURN NEW; END IF;
  graph := NEW.payload->'graph';
  IF NOT COALESCE(current_user='lawcase_agent_worker' AND head.status='PLANNING'
    AND NOT head.is_stale AND NOT head.is_cancelled AND current_version=head.snapshot_matter_version
    AND NEW.event_sequence=head.current_event_version+1 AND head.current_graph_id IS NULL
    AND graph->>'graph_id'=NEW.run_id::text AND graph->'graph_version'='1'::jsonb
    AND graph->>'goal_hash'=review->>'effective_goal_hash' AND graph->'snapshot'=review->'snapshot'
    AND graph->>'graph_hash'=review->>'compiled_graph_hash',false) THEN
   RAISE EXCEPTION 'reviewed material graph envelope differs';
  END IF;
  fingerprint := encode(public.digest(convert_to(public.case_agent_planning_compact_json(
    (graph-'graph_hash') || jsonb_build_object('schema_version','lawyer-agent-task-graph-v1')),'UTF8'),'sha256'),'hex');
  IF fingerprint IS DISTINCT FROM review->>'compiled_graph_hash' THEN
   RAISE EXCEPTION 'reviewed material graph content differs';
  END IF;
  RETURN NEW;
 END IF;
 IF NOT COALESCE(current_user='lawcase_web_application' AND head.status='WAITING_INPUT'
  AND head.failure_code='PLANNER_PROPOSAL_REJECTED' AND head.current_graph_id IS NULL
  AND NOT head.is_stale AND NOT head.is_cancelled AND NEW.event_sequence=head.current_event_version+1
  AND current_version=head.snapshot_matter_version
  AND EXISTS (SELECT 1 FROM public.users principal JOIN public.matter_actor_roles assignment
   ON assignment.user_id=principal.user_id AND assignment.firm_id=principal.firm_id
   WHERE principal.user_id=NEW.actor_id AND principal.firm_id=NEW.firm_id AND principal.status='ACTIVE'
    AND assignment.matter_id=NEW.matter_id AND assignment.revoked_at IS NULL
    AND assignment.role IN ('LEAD_LAWYER','REVIEWER'))
  AND NOT EXISTS (SELECT 1 FROM public.case_agent_events WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id
    AND matter_id=NEW.matter_id AND event_type='PLANNING_MATERIAL_SCOPE_REVIEWED'),false) THEN
  RAISE EXCEPTION 'material scope review state or authorization differs';
 END IF;
 SELECT projection INTO STRICT checkpoint FROM public.case_agent_checkpoints
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id
  AND event_version=head.current_event_version;
 review := NEW.payload;
 IF NOT COALESCE(jsonb_typeof(review)='object' AND review-keys='{}'::jsonb AND review ?& keys
  AND review->'snapshot'=checkpoint->'snapshot' AND review->>'approved_by'=NEW.actor_id::text
  AND review->>'original_goal_hash'=checkpoint->'goal'->>'goal_hash'
  AND NOT (checkpoint->'goal' ? 'material_read_refs')
  AND COALESCE(checkpoint->'goal'->'requested_deliverables','[]'::jsonb)='[]'::jsonb
  AND COALESCE(checkpoint->'goal'->'active_plan_execution','null'::jsonb)='null'::jsonb
  AND review->'previous_output_bytes'=checkpoint->'budget'->'max_output_bytes'
  AND jsonb_typeof(review->'approved_output_bytes')='number'
  AND review->>'approved_output_bytes' ~ '^[1-9][0-9]{0,8}$'
  AND jsonb_typeof(review->'material_read_refs')='array',false) THEN
  RAISE EXCEPTION 'material scope review payload differs';
 END IF;
 IF (review->>'approved_output_bytes')::bigint < (review->>'previous_output_bytes')::bigint
  OR (review->>'approved_output_bytes')::bigint > 67108864
  OR jsonb_array_length(review->'material_read_refs') NOT BETWEEN 1 AND 200 THEN
  RAISE EXCEPTION 'material scope review capacity differs';
 END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(review->'material_read_refs') LOOP
  IF NOT COALESCE(jsonb_typeof(item)='string' AND (item#>>'{}') ~
   '^(evidence-page|material-object):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false) THEN
   RAISE EXCEPTION 'material scope reference is invalid';
  END IF;
 END LOOP;
 SELECT jsonb_agg(value ORDER BY value COLLATE "C") INTO refs FROM
  (SELECT DISTINCT value FROM jsonb_array_elements_text(review->'material_read_refs')) canonical;
 IF refs IS DISTINCT FROM review->'material_read_refs' THEN RAISE EXCEPTION 'material scope references are not canonical'; END IF;
 FOREACH fingerprint IN ARRAY ARRAY['original_goal_hash','original_proposal_hash','request_hash','planning_hash',
   'effective_goal_hash','derived_proposal_hash','compiled_graph_hash'] LOOP
  IF NOT COALESCE(review->>fingerprint ~ '^[0-9a-f]{64}$',false) THEN RAISE EXCEPTION 'material scope hash invalid'; END IF;
 END LOOP;
 SELECT candidate.status, candidate.planning_hash, candidate.matter_version,
  outcome.status AS outcome_status, outcome.request_hash, outcome.input_hash, outcome.structured_proposal,
  candidate.external_request_id, outcome.external_request_id AS outcome_request_id
 INTO STRICT attempt FROM public.case_agent_planning_attempts candidate
 LEFT JOIN LATERAL (SELECT * FROM public.case_agent_planning_external_events e
  WHERE e.planning_attempt_id=candidate.planning_attempt_id AND e.run_id=candidate.run_id
   AND e.firm_id=candidate.firm_id AND e.matter_id=candidate.matter_id
  ORDER BY e.ledger_version DESC LIMIT 1) outcome ON true
 WHERE candidate.run_id=NEW.run_id AND candidate.firm_id=NEW.firm_id AND candidate.matter_id=NEW.matter_id
 ORDER BY candidate.created_at DESC,candidate.planning_attempt_id DESC LIMIT 1 FOR UPDATE OF candidate;
 IF NOT COALESCE(attempt.status='SUCCEEDED' AND attempt.outcome_status='SUCCEEDED'
  AND attempt.matter_version=current_version AND attempt.external_request_id=attempt.outcome_request_id
  AND attempt.request_hash=review->>'request_hash' AND attempt.planning_hash=review->>'planning_hash'
  AND attempt.input_hash=attempt.planning_hash
  AND attempt.structured_proposal->>'goal_hash'=review->>'original_goal_hash'
  AND attempt.structured_proposal->>'planning_snapshot_hash'=attempt.planning_hash
  AND encode(public.digest(convert_to(public.case_agent_planning_compact_json(attempt.structured_proposal),'UTF8'),'sha256'),'hex')
    =review->>'original_proposal_hash',false) THEN RAISE EXCEPTION 'material scope retained success differs'; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(attempt.structured_proposal->'tasks') LOOP
  IF NOT COALESCE(item->>'skill_id' IN ('pdf_reading','office_reading','image_visual_ocr')
    AND jsonb_typeof(item->'input_ref_ids')='array' AND jsonb_array_length(item->'input_ref_ids')>0
    AND (review->'material_read_refs') @> (item->'input_ref_ids'),false) THEN
   RAISE EXCEPTION 'retained task exceeds material scope';
  END IF;
 END LOOP;
 SELECT jsonb_agg(value ORDER BY value COLLATE "C") INTO refs FROM
  (SELECT DISTINCT r.value FROM jsonb_array_elements(attempt.structured_proposal->'tasks') t,
    LATERAL jsonb_array_elements_text(t.value->'input_ref_ids') r) canonical;
 IF refs IS DISTINCT FROM review->'material_read_refs' THEN RAISE EXCEPTION 'retained tasks omit selected sources'; END IF;
 effective_goal := ((checkpoint->'goal')-ARRAY['goal_hash','requested_deliverables','active_plan_execution'])
  || jsonb_build_object('schema_version','lawyer-agent-goal-v3','material_read_refs',review->'material_read_refs');
 IF encode(public.digest(convert_to(public.case_agent_planning_compact_json(effective_goal),'UTF8'),'sha256'),'hex')
  IS DISTINCT FROM review->>'effective_goal_hash' THEN RAISE EXCEPTION 'effective material goal differs'; END IF;
 derived := attempt.structured_proposal || jsonb_build_object('goal_hash',review->>'effective_goal_hash');
 IF encode(public.digest(convert_to(public.case_agent_planning_compact_json(derived),'UTF8'),'sha256'),'hex')
  IS DISTINCT FROM review->>'derived_proposal_hash' THEN RAISE EXCEPTION 'derived material proposal differs'; END IF;
 RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION guard_case_agent_material_scope_review() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION case_agent_planning_compact_json(jsonb) TO lawcase_agent_worker;
CREATE TRIGGER case_agent_material_scope_review_guard BEFORE INSERT ON case_agent_events
FOR EACH ROW EXECUTE FUNCTION guard_case_agent_material_scope_review();
COMMIT;
