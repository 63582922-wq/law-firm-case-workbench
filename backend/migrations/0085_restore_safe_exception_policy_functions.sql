-- ADR-0096. pg_restore sets an empty search_path during data COPY.
BEGIN;
ALTER FUNCTION public.case_agent_ledger_exception_source_policy(text[])
 SET search_path = pg_catalog, public;
ALTER FUNCTION public.case_agent_ledger_exception_risk_policy(text[])
 SET search_path = pg_catalog, public;
ALTER FUNCTION public.case_agent_document_source_refs_hash(jsonb)
 SET search_path = pg_catalog, public;
COMMIT;
