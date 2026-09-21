-- ADR-0112. A deferred extraction candidate remains a lawyer material-review
-- item. It is excluded from the initial analysis package, so it must not
-- freeze an otherwise bounded run. Required re-extraction and missing-evidence
-- follow-ups continue to hold the execution cursor.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SET LOCAL ROLE lawcase_ledger_confirmation_owner;

DO $migration$
DECLARE
    old_clause constant text := $old$
              FROM public.case_agent_ledger_exception_followup_heads followup_head
              JOIN public.case_agent_ledger_exception_control_heads control_head
$old$;
    new_clause constant text := $new$
              FROM public.case_agent_ledger_exception_followup_heads followup_head
              JOIN public.case_agent_ledger_exception_followups followup
                ON followup.followup_id = followup_head.followup_id
               AND followup.firm_id = followup_head.firm_id
               AND followup.matter_id = followup_head.matter_id
              JOIN public.case_agent_ledger_exception_control_heads control_head
$new$;
    old_predicate constant text := $old$
               AND followup_head.current_state = 'ACTIVE'
               AND (
$old$;
    new_predicate constant text := $new$
               AND followup_head.current_state = 'ACTIVE'
               AND followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')
               AND (
$new$;
    definition text;
    patched_definition text;
    occurrence_count integer;
BEGIN
    SELECT pg_catalog.pg_get_functiondef(
        'public.wake_case_agent_run()'::pg_catalog.regprocedure
    ) INTO definition;
    IF definition IS NULL THEN
        RAISE EXCEPTION 'case Agent run wake function is missing';
    END IF;
    occurrence_count := (
        pg_catalog.length(definition)
        - pg_catalog.length(pg_catalog.replace(definition, old_clause, ''))
    ) / pg_catalog.length(old_clause);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION 'case Agent wake follow-up join differs from the expected control definition';
    END IF;
    patched_definition := pg_catalog.replace(definition, old_clause, new_clause);
    occurrence_count := (
        pg_catalog.length(patched_definition)
        - pg_catalog.length(pg_catalog.replace(patched_definition, old_predicate, ''))
    ) / pg_catalog.length(old_predicate);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION 'case Agent wake follow-up predicate differs from the expected control definition';
    END IF;
    EXECUTE pg_catalog.replace(
        patched_definition, old_predicate, new_predicate
    );
END
$migration$;

-- Existing pristine runs were inserted while the old trigger was in effect.
-- Wake only runs that have never advanced beyond RUN_CREATED, are not a
-- recovery/control cursor, and have no ACTIVE source-blocking follow-up. The
-- Worker repeats these fences while claiming the row.
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

RESET ROLE;
COMMIT;
