BEGIN;

-- The 0048 session-bound decision finalizer predates 0049 follow-up
-- lifecycles.  Once all exception groups had a decision it required one
-- PENDING refresh immediately, even when 0049 had correctly created ACTIVE
-- follow-ups that must keep planning blocked.  Teach the finalizer to accept
-- exactly that blocked state and to reject a pending refresh while it exists.
SET LOCAL ROLE lawcase_ledger_confirmation_owner;

DO $migration$
DECLARE
    old_clause constant text := $old$
        IF run_is_resolved AND (
            pending_refresh_count <> 1
            OR pending_refresh_version IS DISTINCT FROM newest_refresh_version
        ) THEN
            RAISE EXCEPTION
                'resolved exception run did not release one latest refresh';
        ELSIF NOT run_is_resolved AND pending_refresh_count <> 0 THEN
$old$;
    new_clause constant text := $new$
        IF run_is_resolved AND EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads head
             WHERE head.firm_id = session_row.firm_id
               AND head.matter_id = input_matter_id
               AND head.current_state = 'ACTIVE'
        ) AND pending_refresh_count <> 0 THEN
            RAISE EXCEPTION
                'resolved exception run with active follow-up exposed a pending refresh';
        ELSIF run_is_resolved AND NOT EXISTS (
            SELECT 1
              FROM public.case_agent_ledger_exception_followup_heads head
             WHERE head.firm_id = session_row.firm_id
               AND head.matter_id = input_matter_id
               AND head.current_state = 'ACTIVE'
        ) AND (
            pending_refresh_count <> 1
            OR pending_refresh_version IS DISTINCT FROM newest_refresh_version
        ) THEN
            RAISE EXCEPTION
                'resolved exception run did not release one latest refresh';
        ELSIF NOT run_is_resolved AND pending_refresh_count <> 0 THEN
$new$;
    definition text;
    patched_definition text;
    occurrence_count integer;
BEGIN
    SELECT pg_catalog.pg_get_functiondef(procedure.oid)
      INTO definition
      FROM pg_catalog.pg_proc procedure
      JOIN pg_catalog.pg_namespace namespace
        ON namespace.oid = procedure.pronamespace
     WHERE namespace.nspname = 'public'
       AND procedure.proname =
            'decide_case_agent_ledger_exception_group_from_web_session'
       AND procedure.pronargs = 9;
    IF definition IS NULL THEN
        RAISE EXCEPTION 'session-bound ledger exception decision function is missing';
    END IF;
    occurrence_count := (
        pg_catalog.length(definition)
        - pg_catalog.length(pg_catalog.replace(definition, old_clause, ''))
    ) / pg_catalog.length(old_clause);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION
            'session-bound exception refresh assertion differs from the expected 0048 definition';
    END IF;
    patched_definition := pg_catalog.replace(
        definition, old_clause, new_clause
    );
    EXECUTE patched_definition;
END
$migration$;

COMMIT;
