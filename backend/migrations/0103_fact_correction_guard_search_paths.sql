-- 0102 repaired the first 0082 trigger discovered by real low-risk batch
-- confirmation.  The sibling guards are also invoked in the same restricted
-- Web-session search_path and must not rely on the caller exposing public.
BEGIN;

ALTER FUNCTION public.guard_case_agent_fact_correction_proposal()
    SET search_path TO pg_catalog, public;
ALTER FUNCTION public.guard_extraction_promotion_after_correction()
    SET search_path TO pg_catalog, public;

REVOKE ALL ON FUNCTION public.guard_case_agent_fact_correction_proposal() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.guard_extraction_promotion_after_correction() FROM PUBLIC;

COMMIT;
