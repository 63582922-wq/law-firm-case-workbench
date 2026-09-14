BEGIN;

-- Once the low-risk lane advances V -> V+1, 0047 deliberately keeps the
-- source Run's snapshot refresh request BLOCKED_BY_OPEN_EXCEPTIONS.  The
-- 0049 lifecycle trigger must nevertheless be able to attach the first
-- controlled follow-up to that same verified, non-stale Run.  It previously
-- accepted only PENDING, making every non-duplicate exception route
-- impossible immediately after a mixed batch confirmation.
SET LOCAL ROLE lawcase_ledger_confirmation_owner;

DO $migration$
DECLARE
    old_clause constant text :=
        'AND request.request_status = ''PENDING''';
    new_clause constant text :=
        'AND request.request_status IN (''PENDING'', ''BLOCKED_BY_OPEN_EXCEPTIONS'')';
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
            'initialize_case_agent_ledger_exception_lifecycle'
       AND procedure.pronargs = 3;
    IF definition IS NULL THEN
        RAISE EXCEPTION 'ledger exception lifecycle function is missing';
    END IF;
    occurrence_count := (
        pg_catalog.length(definition)
        - pg_catalog.length(pg_catalog.replace(definition, old_clause, ''))
    ) / pg_catalog.length(old_clause);
    IF occurrence_count <> 1 THEN
        RAISE EXCEPTION
            'ledger exception lifecycle refresh guard differs from the expected 0049 definition';
    END IF;
    patched_definition := pg_catalog.replace(
        definition, old_clause, new_clause
    );
    EXECUTE patched_definition;
END
$migration$;

COMMIT;
