-- PostgreSQL 16+; apply after 0071_safe_local_active_plan_execution_reissue.sql.
--
-- CASE_REVIEW_MEMO 1.2.0 is bound to an independently verified lawyer
-- decision package.  The application enum and verifier already understand
-- that source, but the original 0039 database whitelist predates it.  Keep
-- the database as the hard controller by adding only this exact source kind;
-- arbitrary model/browser supplied source kinds remain rejected.

BEGIN;

CREATE OR REPLACE FUNCTION public.case_agent_document_source_manifest_is_valid(
    value jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND jsonb_array_length(value) BETWEEN 1 AND 400
       AND jsonb_array_length(value) = (
           SELECT count(DISTINCT entry ->> 'input_ref')
           FROM jsonb_array_elements(value) AS sources(entry)
       )
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(value) AS sources(entry)
           WHERE jsonb_typeof(entry) <> 'object'
              OR ARRAY(
                    SELECT key
                    FROM jsonb_object_keys(entry) AS keys(key)
                    ORDER BY key
                 ) <> ARRAY[
                    'input_ref', 'label', 'source_hash', 'source_kind',
                    'source_version', 'text_sha256'
                 ]::text[]
              OR entry ->> 'input_ref' !~
                    '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
              OR entry ->> 'source_kind' NOT IN (
                    'POSTURE_PROFILE', 'WORK_PLAN_ITEM', 'CONFIRMED_FACT',
                    'CONFIRMED_CLAIM', 'CONFIRMED_ISSUE',
                    'CONFIRMED_TRANSACTION', 'VERIFIED_LEGAL_SOURCE',
                    'APPROVED_LEGAL_RULE', 'APPROVED_CALCULATION',
                    'CONFIRMED_PROCEDURAL_EVENT', 'APPROVED_EVIDENCE_ITEM',
                    'VERIFIED_LAWYER_DECISION_PACKAGE'
                 )
              OR entry ->> 'source_version' !~
                    '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
              OR entry ->> 'source_hash' !~ '^[0-9a-f]{64}$'
              OR entry ->> 'text_sha256' !~ '^[0-9a-f]{64}$'
              OR length(entry ->> 'label') NOT BETWEEN 1 AND 240
              OR entry ->> 'label' <> btrim(entry ->> 'label')
       );
$$;

DO $migration$
DECLARE
    accepted boolean;
    rejected boolean;
BEGIN
    SELECT public.case_agent_document_source_manifest_is_valid(
        jsonb_build_array(
            jsonb_build_object(
                'input_ref',
                    'lawyer-decision-package:00000000-0000-0000-0000-000000000001',
                'label', '已验证律师决策包',
                'source_hash', repeat('a', 64),
                'source_kind', 'VERIFIED_LAWYER_DECISION_PACKAGE',
                'source_version', 'verified-' || repeat('b', 64),
                'text_sha256', repeat('c', 64)
            )
        )
    ) INTO accepted;
    SELECT public.case_agent_document_source_manifest_is_valid(
        jsonb_build_array(
            jsonb_build_object(
                'input_ref', 'model-source:uncontrolled',
                'label', '未受控来源',
                'source_hash', repeat('a', 64),
                'source_kind', 'MODEL_SUPPLIED_SOURCE',
                'source_version', 'v1',
                'text_sha256', repeat('c', 64)
            )
        )
    ) INTO rejected;
    IF accepted IS DISTINCT FROM true OR rejected IS DISTINCT FROM false THEN
        RAISE EXCEPTION
            'lawyer decision package document-source whitelist is not fail-closed';
    END IF;
END;
$migration$;

COMMIT;
