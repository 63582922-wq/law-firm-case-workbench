BEGIN;

-- 0049 double-escaped the PostgreSQL control-character ranges.  The regex
-- therefore matched ordinary Chinese/ASCII reason notes and made every Web
-- follow-up action fail before authority and idempotency checks.  Use the
-- same single-escaped expression already restored for exception decisions.
SET LOCAL ROLE lawcase_ledger_confirmation_owner;

DO $migration$
DECLARE
    old_clause constant text :=
        $old$OR normalized_note ~ '[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]'$old$;
    new_clause constant text :=
        $new$OR normalized_note ~ '[\x00-\x08\x0B\x0C\x0E-\x1F]'$new$;
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
            'resolve_case_agent_ledger_exception_followup_from_web_session'
       AND procedure.pronargs = 10;
    IF definition IS NULL THEN
        RAISE EXCEPTION 'session-bound ledger follow-up function is missing';
    END IF;
    occurrence_count := (
        pg_catalog.length(definition)
        - pg_catalog.length(pg_catalog.replace(definition, old_clause, ''))
    ) / pg_catalog.length(old_clause);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION
            'ledger follow-up reason-note guard differs from the expected 0049 definition';
    END IF;
    patched_definition := pg_catalog.replace(
        definition, old_clause, new_clause
    );
    EXECUTE patched_definition;
END
$migration$;

RESET ROLE;
SET LOCAL ROLE lawcase_schema_owner;

ALTER TABLE public.case_agent_ledger_exception_followup_events
    DROP CONSTRAINT case_agent_ledger_exception_followup_events_reason_note_check;
ALTER TABLE public.case_agent_ledger_exception_followup_events
    ADD CONSTRAINT case_agent_ledger_exception_followup_events_reason_note_check
    CHECK (
        reason_note IS NULL OR (
            reason_note = pg_catalog.btrim(reason_note)
            AND pg_catalog.length(reason_note) BETWEEN 1 AND 500
            AND pg_catalog.octet_length(reason_note) <= 2000
            AND reason_note !~ '[\x00-\x08\x0B\x0C\x0E-\x1F]'
        )
    );

COMMIT;
