BEGIN;

-- The extraction Worker may append the governed staging event, but the
-- exception-group materializer remains an internal schema operation.  Run
-- the trigger under the isolated schema owner instead of granting the Worker
-- a directly callable grouping function with arbitrary batch identifiers.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
         FROM pg_catalog.pg_roles
         WHERE rolname = 'lawcase_schema_owner'
           AND NOT rolcanlogin AND NOT rolinherit AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION 'lawcase_schema_owner must remain a NOLOGIN NOINHERIT NOBYPASSRLS role';
    END IF;
END
$$;

ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    OWNER TO lawcase_schema_owner;
ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    SECURITY DEFINER;
ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()
    SET search_path = pg_catalog, public, pg_temp;

REVOKE ALL ON FUNCTION
    public.group_case_agent_ledger_exceptions_after_staging()
FROM PUBLIC, lawcase_web_application, lawcase_agent_worker,
     lawcase_agent_verifier, lawcase_ledger_confirmation_owner;

COMMIT;
