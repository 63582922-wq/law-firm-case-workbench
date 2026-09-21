-- ADR-0091. Initial run_budget remains immutable; effective budget is event-derived.
BEGIN;
ALTER TABLE case_agent_events DROP CONSTRAINT case_agent_events_event_type_check;
ALTER TABLE case_agent_events ADD CONSTRAINT case_agent_events_event_type_check CHECK (event_type IN (
 'RUN_CREATED','PLANNING_STARTED','PLANNING_FAILED','PLANNING_BUDGET_REVIEWED',
 'PLANNING_RESULT_UNKNOWN','TASK_GRAPH_ACCEPTED','APPROVAL_GRANTED','LAWYER_PLAN_CORRECTION_RECORDED',
 'TASK_STARTED','TASK_RESULT_RECORDED','CASE_SNAPSHOT_CHANGED','RUN_PAUSED','RUN_RESUMED','RUN_CANCELLED',
 'VERIFICATION_STARTED','VERIFICATION_PASSED','VERIFICATION_FAILED','RUN_COMPLETED'
));

-- The proposal contract consists of strings, arrays and objects; normalize its
-- JSON identically to the application's sorted-key compact UTF-8 encoding.
CREATE FUNCTION case_agent_planning_compact_json(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, public AS $$
DECLARE result text;
BEGIN
 CASE jsonb_typeof(value)
 WHEN 'object' THEN
  SELECT '{' || COALESCE(string_agg(to_jsonb(key)::text || ':' ||
   public.case_agent_planning_compact_json(item), ',' ORDER BY key COLLATE "C"), '') || '}'
  INTO result FROM jsonb_each(value) AS entry(key,item);
 WHEN 'array' THEN
  SELECT '[' || COALESCE(string_agg(public.case_agent_planning_compact_json(item), ',' ORDER BY ordinal), '') || ']'
  INTO result FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(item,ordinal);
 ELSE result := value::text;
 END CASE;
 RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION case_agent_planning_compact_json(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION case_agent_planning_compact_json(jsonb) TO lawcase_web_application;

CREATE FUNCTION guard_case_agent_planning_budget_review() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE head public.case_agent_runs%ROWTYPE; checkpoint jsonb; attempt record; current_version integer;
BEGIN
 IF NEW.event_type <> 'PLANNING_BUDGET_REVIEWED' THEN RETURN NEW; END IF;
 IF current_user <> 'lawcase_web_application' THEN
  RAISE EXCEPTION 'planning budget review requires the Web principal';
 END IF;
 SELECT * INTO STRICT head FROM public.case_agent_runs
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id FOR UPDATE;
 SELECT version INTO STRICT current_version FROM public.matters
 WHERE matter_id=NEW.matter_id AND firm_id=NEW.firm_id FOR UPDATE;
 IF head.status <> 'WAITING_INPUT' OR head.failure_code IS DISTINCT FROM 'PLANNER_PROPOSAL_REJECTED'
  OR head.current_graph_id IS NOT NULL OR head.is_stale OR head.is_cancelled
  OR NEW.event_sequence <> head.current_event_version+1 OR current_version <> head.snapshot_matter_version
  OR NOT EXISTS (SELECT 1 FROM public.users principal JOIN public.matter_actor_roles assignment
    ON assignment.user_id=principal.user_id AND assignment.firm_id=principal.firm_id
    WHERE principal.user_id=NEW.actor_id AND principal.firm_id=NEW.firm_id AND principal.status='ACTIVE'
      AND assignment.matter_id=NEW.matter_id AND assignment.revoked_at IS NULL
      AND assignment.role IN ('LEAD_LAWYER','REVIEWER')) THEN
  RAISE EXCEPTION 'planning budget review scope or state differs';
 END IF;
 SELECT projection INTO STRICT checkpoint FROM public.case_agent_checkpoints
 WHERE run_id=NEW.run_id AND firm_id=NEW.firm_id AND matter_id=NEW.matter_id
   AND event_version=head.current_event_version;
 IF jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
  OR NEW.payload - ARRAY['snapshot','previous_runtime_seconds','approved_runtime_seconds',
      'request_hash','planning_hash','proposal_hash','approved_by'] <> '{}'::jsonb
  OR NOT (NEW.payload ?& ARRAY['snapshot','previous_runtime_seconds','approved_runtime_seconds',
      'request_hash','planning_hash','proposal_hash','approved_by'])
  OR NEW.payload->'snapshot' IS DISTINCT FROM checkpoint->'snapshot'
  OR NEW.payload->>'approved_by' IS DISTINCT FROM NEW.actor_id::text
  OR NEW.payload->'previous_runtime_seconds' IS DISTINCT FROM checkpoint->'budget'->'max_runtime_seconds'
  OR (NEW.payload->>'approved_runtime_seconds') !~ '^[1-9][0-9]{0,5}$'
  OR jsonb_typeof(NEW.payload->'approved_runtime_seconds') IS DISTINCT FROM 'number'
  OR (NEW.payload->>'approved_runtime_seconds')::integer <= (NEW.payload->>'previous_runtime_seconds')::integer
  OR (NEW.payload->>'approved_runtime_seconds')::integer > 604800 THEN
  RAISE EXCEPTION 'planning budget review payload differs';
 END IF;
 SELECT candidate.status, candidate.planning_hash, candidate.matter_version,
   outcome.status AS outcome_status, outcome.request_hash, outcome.input_hash, outcome.structured_proposal,
   candidate.external_request_id, outcome.external_request_id AS outcome_request_id
 INTO STRICT attempt FROM public.case_agent_planning_attempts candidate
 LEFT JOIN LATERAL (SELECT * FROM public.case_agent_planning_external_events e
   WHERE e.planning_attempt_id=candidate.planning_attempt_id AND e.run_id=candidate.run_id
    AND e.firm_id=candidate.firm_id AND e.matter_id=candidate.matter_id
   ORDER BY e.ledger_version DESC LIMIT 1) outcome ON true
 WHERE candidate.run_id=NEW.run_id AND candidate.firm_id=NEW.firm_id AND candidate.matter_id=NEW.matter_id
 ORDER BY candidate.created_at DESC, candidate.planning_attempt_id DESC LIMIT 1 FOR UPDATE OF candidate;
 IF attempt.status <> 'SUCCEEDED' OR attempt.outcome_status IS DISTINCT FROM 'SUCCEEDED'
  OR jsonb_typeof(attempt.structured_proposal) IS DISTINCT FROM 'object'
  OR attempt.matter_version <> current_version
  OR attempt.external_request_id IS DISTINCT FROM attempt.outcome_request_id
  OR attempt.request_hash IS DISTINCT FROM NEW.payload->>'request_hash'
  OR attempt.planning_hash IS DISTINCT FROM NEW.payload->>'planning_hash'
  OR attempt.input_hash IS DISTINCT FROM attempt.planning_hash
  OR attempt.structured_proposal->>'goal_hash' IS DISTINCT FROM checkpoint->'goal'->>'goal_hash'
  OR attempt.structured_proposal->>'planning_snapshot_hash' IS DISTINCT FROM attempt.planning_hash
  OR encode(public.digest(convert_to(public.case_agent_planning_compact_json(attempt.structured_proposal),'UTF8'),'sha256'),'hex')
      IS DISTINCT FROM NEW.payload->>'proposal_hash' THEN
  RAISE EXCEPTION 'planning budget review proposal differs';
 END IF;
 RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION guard_case_agent_planning_budget_review() FROM PUBLIC;
CREATE TRIGGER case_agent_planning_budget_review_guard BEFORE INSERT ON case_agent_events
FOR EACH ROW EXECUTE FUNCTION guard_case_agent_planning_budget_review();
COMMIT;
