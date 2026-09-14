-- Reviewable Office pairs are derived candidates, not matter-input mutations.
--
-- Registering, viewing or approving a DOCX/XLSX plus its rendered PDF changes
-- only an internal review artifact.  It must remain auditable and idempotent,
-- but it cannot make facts, evidence, claims, rules, an active Agent plan or a
-- court-submission bundle stale merely because a lawyer generated a draft.

BEGIN;

ALTER TABLE public.audit_events
    DROP CONSTRAINT audit_events_version_transition_valid;

ALTER TABLE public.audit_events
    ADD CONSTRAINT audit_events_version_transition_valid CHECK (
        output_version > input_version
        OR (
            event_type IN (
                'CASE_WORK_PLAN_ITEM_REVIEWED',
                'CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED',
                'REVIEWABLE_OFFICE_DRAFT_PAIR_REGISTERED',
                'REVIEWABLE_OFFICE_DRAFT_PAIR_APPROVED'
            )
            AND output_version = input_version
        )
    ) NOT VALID;

ALTER TABLE public.audit_events
    VALIDATE CONSTRAINT audit_events_version_transition_valid;

COMMIT;
