-- The 0082 trigger is invoked while the Web transaction deliberately exposes
-- only pg_catalog in its session search_path.  Bind the immutable guard to
-- public explicitly so ordinary low-risk extraction confirmation cannot fail
-- during PL/pgSQL compilation after the 0081 proposal table is deployed.
-- public CREATE remains revoked by the managed schema hardening.
BEGIN;

ALTER FUNCTION public.guard_fact_correction_candidate_lineage()
    SET search_path TO pg_catalog, public;

REVOKE ALL ON FUNCTION public.guard_fact_correction_candidate_lineage() FROM PUBLIC;

COMMIT;
