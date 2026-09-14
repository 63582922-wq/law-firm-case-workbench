-- Complete ADR-0100 against the actual promotion source-to-use mapping.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.case_agent_work_plan_input_bindings
    DROP CONSTRAINT case_agent_work_plan_input_bindings_reference_use_check,
    DROP CONSTRAINT case_agent_work_plan_input_bindings_approved_rule_scope,
    ADD CONSTRAINT case_agent_work_plan_input_bindings_reference_use_check
    CHECK (reference_use IN (
        'MATERIAL', 'EVIDENCE', 'FACT', 'CLAIM_SCOPE', 'TRANSACTION',
        'POSTURE', 'WORK_PLAN', 'LEGAL_AUTHORITY', 'LEGAL_RULE', 'COURT_EVENT'
    )),
    ADD CONSTRAINT case_agent_work_plan_input_bindings_approved_rule_scope
    CHECK (object_type <> 'APPROVED_LEGAL_RULE' OR (
        source_status = 'LOCKED' AND reference_use = 'LEGAL_RULE'
    ));
COMMIT;
