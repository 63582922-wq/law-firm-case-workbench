-- Source-bound unconfirmed statements support analysis, not approved facts.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE public.case_agent_work_plan_input_bindings
 DROP CONSTRAINT case_agent_work_plan_input_bindings_object_type_check,
 ADD CONSTRAINT case_agent_work_plan_input_bindings_object_type_check
 CHECK (object_type IN ('MATERIAL_OBJECT','EVIDENCE_PAGE','CASE_FACT','CASE_CLAIM',
  'DISPUTE_ISSUE','CASE_TRANSACTION','POSTURE_PROFILE','WORK_PLAN_ITEM','VERIFIED_LEGAL_SOURCE',
  'APPROVED_LEGAL_RULE','PROCEDURAL_EVENT','REVIEW_OBLIGATION','TRANSACTION_CANDIDATE','FACT_CANDIDATE')),
 ADD CONSTRAINT case_agent_work_plan_input_bindings_fact_candidate_scope
 CHECK (object_type <> 'FACT_CANDIDATE' OR (
  source_status='REVIEW_REQUIRED' AND reference_use='WORK_PLAN'
  AND input_ref='fact-candidate:' || object_id::text));
COMMIT;
