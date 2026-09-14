-- PostgreSQL 16+; apply after 0069_document_draft_policy_hash_guard.sql.
-- The first-release CASE_REVIEW_MEMO delivery is now a local, source-bound
-- analysis projection.  Keep the legacy exchange trigger fail-closed under
-- the current manifest identity: its existing NETWORK_CONNECTOR checks mean
-- the local task still cannot create a provider exchange.

BEGIN;

DO $migration$
DECLARE
    function_definition text;
    old_policy_hash constant text :=
        'a9e693a8a46c34469573ca67393d8ce5a95764e37d0e74e7c2b15b945a7e14c4';
    new_policy_hash constant text :=
        'f881e5b2762b1228f4123e611875c506eebe8d27f8ca3960a0701d21a0e872e1';
BEGIN
    SELECT pg_get_functiondef(
        'public.validate_case_agent_document_draft_exchange()'::regprocedure
    ) INTO function_definition;

    IF position(new_policy_hash IN function_definition) > 0 THEN
        NULL;
    ELSIF position(old_policy_hash IN function_definition) > 0 THEN
        function_definition := replace(
            function_definition,
            old_policy_hash,
            new_policy_hash
        );
        EXECUTE function_definition;
    ELSE
        RAISE EXCEPTION
            'document draft exchange policy identity differs before 0070';
    END IF;

    SELECT pg_get_functiondef(
        'public.validate_case_agent_document_draft_exchange()'::regprocedure
    ) INTO function_definition;
    IF position(new_policy_hash IN function_definition) = 0
       OR position('NETWORK_CONNECTOR' IN function_definition) = 0
       OR position('EXACT_ALLOWLIST' IN function_definition) = 0
       OR position('api.deepseek.com' IN function_definition) = 0 THEN
        RAISE EXCEPTION
            'document draft exchange fail-closed guard differs after 0070';
    END IF;
END;
$migration$;

COMMIT;
