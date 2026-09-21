-- Unconfirmed extracted transactions may support a work plan, never an approved ledger amount.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE public.case_agent_work_plan_input_bindings
 DROP CONSTRAINT case_agent_work_plan_input_bindings_object_type_check,
 ADD CONSTRAINT case_agent_work_plan_input_bindings_object_type_check
 CHECK (object_type IN ('MATERIAL_OBJECT','EVIDENCE_PAGE','CASE_FACT','CASE_CLAIM',
  'DISPUTE_ISSUE','CASE_TRANSACTION','POSTURE_PROFILE','WORK_PLAN_ITEM','VERIFIED_LEGAL_SOURCE',
  'APPROVED_LEGAL_RULE','PROCEDURAL_EVENT','REVIEW_OBLIGATION','TRANSACTION_CANDIDATE')),
 ADD CONSTRAINT case_agent_work_plan_input_bindings_transaction_candidate_scope
 CHECK (object_type <> 'TRANSACTION_CANDIDATE' OR (
  source_status='REVIEW_REQUIRED' AND reference_use='WORK_PLAN'
  AND input_ref='transaction-candidate:' || object_id::text));
COMMIT;
