-- ADR-0102: pending review metadata is not a confirmed fact or legal rule.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.case_agent_work_plan_input_bindings
    DROP CONSTRAINT case_agent_work_plan_input_bindings_object_type_check,
    ADD CONSTRAINT case_agent_work_plan_input_bindings_object_type_check
    CHECK (object_type IN (
        'MATERIAL_OBJECT', 'EVIDENCE_PAGE', 'CASE_FACT', 'CASE_CLAIM',
        'DISPUTE_ISSUE', 'CASE_TRANSACTION', 'POSTURE_PROFILE',
        'WORK_PLAN_ITEM', 'VERIFIED_LEGAL_SOURCE', 'APPROVED_LEGAL_RULE',
        'PROCEDURAL_EVENT', 'REVIEW_OBLIGATION'
    )),
    ADD CONSTRAINT case_agent_work_plan_input_bindings_review_obligation_scope
    CHECK (object_type <> 'REVIEW_OBLIGATION' OR (
        source_status IN ('OPEN', 'BLOCKED', 'DISPUTED')
        AND reference_use = 'WORK_PLAN'
        AND input_ref = 'review-obligation:' || object_id::text
    ));
COMMIT;
