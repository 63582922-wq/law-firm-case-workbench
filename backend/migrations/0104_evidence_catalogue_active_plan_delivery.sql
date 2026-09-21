-- Add the source-bound evidence catalogue to the closed lawyer deliverable
-- catalogue.  The catalogue remains an internal review candidate: this
-- migration changes neither evidence approval, lawyer final review, bundle
-- locking nor any court-submission authority.

BEGIN;

CREATE OR REPLACE FUNCTION public.case_agent_requested_deliverables_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
DECLARE
    canonical jsonb;
    distinct_count integer;
BEGIN
    IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) > 4 THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(value) AS requested_items(item)
        WHERE item NOT IN (
            'CASE_REVIEW_MEMO',
            'DEFENCE_STATEMENT',
            'EVIDENCE_CATALOGUE',
            'PAYMENT_LEDGER'
        )
    ) THEN
        RETURN false;
    END IF;
    SELECT coalesce(jsonb_agg(item ORDER BY item), '[]'::jsonb),
           count(DISTINCT item)
      INTO canonical, distinct_count
      FROM jsonb_array_elements_text(value) AS requested_items(item);
    RETURN value = canonical AND jsonb_array_length(value) = distinct_count;
END;
$$;

CREATE OR REPLACE FUNCTION public.case_agent_active_plan_execution_valid(
    requested jsonb,
    execution jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog
AS $$
DECLARE
    item jsonb;
    top_keys text[];
    item_keys text[];
    canonical_items jsonb;
    execution_kinds jsonb;
    distinct_item_ids integer;
BEGIN
    IF execution IS NULL THEN
        RETURN true;
    END IF;
    IF jsonb_typeof(execution) <> 'object' THEN
        RETURN false;
    END IF;
    SELECT array_agg(key ORDER BY key)
      INTO top_keys
      FROM jsonb_object_keys(execution) AS execution_keys(key);
    IF top_keys <> ARRAY['items','plan_hash','plan_id','source_run_id']::text[]
       OR coalesce(execution->>'plan_id', '') !~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR coalesce(execution->>'source_run_id', '') !~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR coalesce(execution->>'plan_hash', '') !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(execution->'items') <> 'array'
       OR jsonb_array_length(execution->'items') NOT BETWEEN 1 AND 4 THEN
        RETURN false;
    END IF;
    FOR item IN
        SELECT value
        FROM jsonb_array_elements(execution->'items') AS execution_items(value)
    LOOP
        IF jsonb_typeof(item) <> 'object' THEN
            RETURN false;
        END IF;
        SELECT array_agg(key ORDER BY key)
          INTO item_keys
          FROM jsonb_object_keys(item) AS deliverable_keys(key);
        IF item_keys <>
           ARRAY['deliverable_kind','item_hash','item_id','output_format']::text[]
           OR coalesce(item->>'item_id', '') !~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR coalesce(item->>'item_hash', '') !~ '^[0-9a-f]{64}$'
           OR (
                item->>'deliverable_kind' = 'CASE_REVIEW_MEMO'
                AND item->>'output_format' <> 'DOCX'
           )
           OR (
                item->>'deliverable_kind' = 'DEFENCE_STATEMENT'
                AND item->>'output_format' <> 'DOCX'
           )
           OR (
                item->>'deliverable_kind' IN ('EVIDENCE_CATALOGUE', 'PAYMENT_LEDGER')
                AND item->>'output_format' <> 'XLSX'
           )
           OR coalesce(item->>'deliverable_kind', '') NOT IN
              ('CASE_REVIEW_MEMO', 'DEFENCE_STATEMENT', 'EVIDENCE_CATALOGUE', 'PAYMENT_LEDGER') THEN
            RETURN false;
        END IF;
    END LOOP;
    SELECT jsonb_agg(value ORDER BY value->>'deliverable_kind', value->>'item_id'),
           jsonb_agg(value->>'deliverable_kind' ORDER BY value->>'deliverable_kind'),
           count(DISTINCT value->>'item_id')
      INTO canonical_items, execution_kinds, distinct_item_ids
      FROM jsonb_array_elements(execution->'items') AS execution_items(value);
    RETURN execution->'items' = canonical_items
       AND requested = execution_kinds
       AND jsonb_array_length(execution->'items') = distinct_item_ids;
END;
$$;

GRANT EXECUTE ON FUNCTION
    public.case_agent_requested_deliverables_valid(jsonb),
    public.case_agent_active_plan_execution_valid(jsonb, jsonb)
    TO lawcase_web_application;

COMMIT;
