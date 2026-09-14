-- Follow-up to 0100.  FORCE ROW LEVEL SECURITY intentionally hid pre-existing
-- inbox rows from the isolated function owner during the first requeue.  This
-- migration is executed by the PostgreSQL migrator and uses its bounded
-- one-shot maintenance authority only for pristine, never-executed runs.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

RESET ROLE;

UPDATE public.case_agent_run_inbox inbox
   SET inbox_status = 'READY',
       available_at = pg_catalog.clock_timestamp(),
       lease_owner = NULL,
       lease_token = NULL,
       lease_expires_at = NULL,
       inbox_version = inbox.inbox_version + 1,
       updated_at = pg_catalog.clock_timestamp()
  FROM public.case_agent_runs run
 WHERE run.run_id = inbox.run_id
   AND run.firm_id = inbox.firm_id
   AND run.matter_id = inbox.matter_id
   AND inbox.inbox_status = 'QUIET'
   AND run.status = 'CREATED'
   AND run.current_event_version = 1
   AND NOT run.is_cancelled
   AND NOT EXISTS (
       SELECT 1
         FROM public.case_agent_ledger_exception_recovery_quarantines quarantine
        WHERE quarantine.run_id = inbox.run_id
          AND quarantine.firm_id = inbox.firm_id
          AND quarantine.matter_id = inbox.matter_id
   )
   AND NOT EXISTS (
       SELECT 1
         FROM public.case_agent_ledger_exception_recovery_intents intent
        WHERE intent.replacement_run_id = inbox.run_id
          AND intent.firm_id = inbox.firm_id
          AND intent.matter_id = inbox.matter_id
   )
   AND NOT EXISTS (
       SELECT 1
         FROM public.case_agent_ledger_exception_control_assignments assignment
        WHERE assignment.control_run_id = inbox.run_id
          AND assignment.firm_id = inbox.firm_id
          AND assignment.matter_id = inbox.matter_id
   )
   AND NOT EXISTS (
       SELECT 1
         FROM public.case_agent_ledger_exception_followup_heads followup_head
         JOIN public.case_agent_ledger_exception_followups followup
           ON followup.followup_id = followup_head.followup_id
          AND followup.firm_id = followup_head.firm_id
          AND followup.matter_id = followup_head.matter_id
        WHERE followup_head.firm_id = inbox.firm_id
          AND followup_head.matter_id = inbox.matter_id
          AND followup_head.current_state = 'ACTIVE'
          AND followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')
   );

COMMIT;
