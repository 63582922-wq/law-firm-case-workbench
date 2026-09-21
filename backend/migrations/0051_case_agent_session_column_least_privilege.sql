-- Forward-only repair for 0049/0050 runtime read grants.
--
-- Earlier revisions granted whole-table SELECT on lifecycle rows that retain
-- server Web-session UUIDs.  RLS limits firms, but it does not make a session
-- identifier safe to enumerate inside the firm: those UUIDs are accepted by
-- tightly scoped SECURITY DEFINER commands.  Remove both table- and column-
-- level historical grants, then restore only columns used by current Python
-- Web/Worker queries.  The isolated lifecycle owner keeps the full rows.

BEGIN;

-- Column grants are unusable without schema lookup rights.  The Worker needs
-- USAGE to resolve the exact runtime projections below, but must never be
-- able to create a shadow relation or function in the trusted schema.
REVOKE CREATE ON SCHEMA public FROM lawcase_agent_worker;
GRANT USAGE ON SCHEMA public TO lawcase_agent_worker;

REVOKE SELECT ON TABLE
    public.case_agent_ledger_exception_control_assignments,
    public.case_agent_ledger_exception_followup_events,
    public.case_agent_ledger_exception_recovery_intents,
    public.case_agent_ledger_exception_recovery_intent_heads,
    public.case_agent_ledger_exception_recovery_quarantines
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

-- Clear any explicit legacy column grants as well.  Listing the complete
-- schemas makes this migration deterministic and lets the startup preflight
-- reject a later schema/grant drift rather than silently broadening access.
REVOKE SELECT (
    control_assignment_id, firm_id, matter_id, assignment_sequence,
    control_run_id, state_after, transition_type,
    supersedes_control_assignment_id, source_exception_decision_id,
    source_agent_event_id, actor_id, web_session_id,
    expected_matter_version, reason_code, idempotency_key, request_hash,
    audit_event_id, assigned_at
) ON TABLE public.case_agent_ledger_exception_control_assignments
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE SELECT (
    followup_event_id, followup_id, event_sequence, firm_id, matter_id,
    subject_hash, event_type, state_after, actor_id, web_session_id,
    expected_matter_version, managed_evidence_request_id,
    managed_evidence_source_set_hash, managed_evidence_source_count,
    reextraction_binding_id, reason_note, idempotency_key, request_hash,
    event_hash, audit_event_id, occurred_at
) ON TABLE public.case_agent_ledger_exception_followup_events
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE SELECT (
    recovery_intent_id, firm_id, matter_id, source_control_assignment_id,
    replacement_run_id, actor_id, prepared_web_session_id,
    expected_matter_version, idempotency_key, request_hash, prepared_at
) ON TABLE public.case_agent_ledger_exception_recovery_intents
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE SELECT (
    recovery_intent_id, firm_id, matter_id, current_outcome,
    transfer_control_assignment_id, outcome_reason_code, outcome_actor_id,
    outcome_web_session_id, outcome_at, outcome_version, updated_at
) ON TABLE public.case_agent_ledger_exception_recovery_intent_heads
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE SELECT (
    run_id, firm_id, matter_id, source_control_assignment_id,
    reason_code, quarantined_at
) ON TABLE public.case_agent_ledger_exception_recovery_quarantines
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

-- Shared Web/Worker current-control projection.
GRANT SELECT (
    control_assignment_id, firm_id, matter_id, assignment_sequence,
    control_run_id, state_after
) ON TABLE public.case_agent_ledger_exception_control_assignments
    TO lawcase_web_application, lawcase_agent_worker;

-- Worker-only immutable projections used by planning and claim fencing.
GRANT SELECT (
    followup_event_id, followup_id, event_sequence, firm_id, matter_id,
    subject_hash, state_after, expected_matter_version, event_hash
) ON TABLE public.case_agent_ledger_exception_followup_events
    TO lawcase_agent_worker;
GRANT SELECT (
    recovery_intent_id, firm_id, matter_id, replacement_run_id
) ON TABLE public.case_agent_ledger_exception_recovery_intents
    TO lawcase_agent_worker;
GRANT SELECT (
    recovery_intent_id, firm_id, matter_id, current_outcome,
    transfer_control_assignment_id
) ON TABLE public.case_agent_ledger_exception_recovery_intent_heads
    TO lawcase_agent_worker;
GRANT SELECT (run_id, firm_id, matter_id)
    ON TABLE public.case_agent_ledger_exception_recovery_quarantines
    TO lawcase_agent_worker;

COMMIT;
