-- Local proposal persistence succeeds before graph compilation can reject it.
-- The run is failed; its immutable local proposal remains successfully recorded.
BEGIN;
DO $migration$
DECLARE definition text;
BEGIN
    definition := pg_get_functiondef('public.case_agent_local_plan_reissue_allowed(uuid,uuid,uuid)'::regprocedure);
    IF position('a.status=''FAILED''' IN definition)=0 THEN
        RAISE EXCEPTION 'local proposal reissue guard differs';
    END IF;
    EXECUTE replace(definition, 'a.status=''FAILED''', 'a.status=''SUCCEEDED''');
END;
$migration$;
COMMIT;
