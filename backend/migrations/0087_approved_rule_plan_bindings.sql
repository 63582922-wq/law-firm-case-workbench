-- ADR-0100: align persisted planning provenance with the typed source contract.
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
        'PROCEDURAL_EVENT'
    )),
    ADD CONSTRAINT case_agent_work_plan_input_bindings_approved_rule_scope
    CHECK (object_type <> 'APPROVED_LEGAL_RULE' OR (
        source_status = 'LOCKED' AND reference_use = 'LEGAL_AUTHORITY'
    ));

COMMIT;
