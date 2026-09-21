-- Exact reviewable-deliverable intent and one explicit ACTIVE-plan execution.
--
-- A browser may request only installed first-release deliverable kinds on the
-- first run.  After lead-lawyer activation, the server binds the exact plan,
-- source run and actionable item ids to at most one second run.  No trigger
-- below starts a run or calls a model automatically.

BEGIN;

ALTER TABLE public.case_agent_goals
    ADD COLUMN requested_deliverables jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN active_plan_execution jsonb;

CREATE FUNCTION public.case_agent_requested_deliverables_valid(value jsonb)
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
    IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) > 2 THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(value) AS requested_items(item)
        WHERE item NOT IN ('CASE_REVIEW_MEMO', 'PAYMENT_LEDGER')
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

CREATE FUNCTION public.case_agent_active_plan_execution_valid(
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
       OR jsonb_array_length(execution->'items') NOT BETWEEN 1 AND 2 THEN
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
                item->>'deliverable_kind' = 'PAYMENT_LEDGER'
                AND item->>'output_format' <> 'XLSX'
           )
           OR coalesce(item->>'deliverable_kind', '') NOT IN
              ('CASE_REVIEW_MEMO', 'PAYMENT_LEDGER') THEN
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

ALTER TABLE public.case_agent_goals
    ADD CONSTRAINT case_agent_goals_requested_deliverables_shape
        CHECK (public.case_agent_requested_deliverables_valid(requested_deliverables)),
    ADD CONSTRAINT case_agent_goals_active_plan_execution_shape
        CHECK (public.case_agent_active_plan_execution_valid(
            requested_deliverables, active_plan_execution
        ));

CREATE TABLE public.case_agent_active_plan_execution_runs (
    execution_id uuid PRIMARY KEY,
    firm_id uuid NOT NULL REFERENCES public.firms(firm_id),
    matter_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    plan_hash char(64) NOT NULL CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    activated_matter_version integer NOT NULL CHECK (activated_matter_version > 0),
    source_run_id uuid NOT NULL,
    run_id uuid NOT NULL,
    requested_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, firm_id, matter_id),
    UNIQUE (run_id, firm_id, matter_id),
    CHECK (source_run_id <> run_id),
    FOREIGN KEY (matter_id, firm_id)
        REFERENCES public.matters(matter_id, firm_id),
    FOREIGN KEY (plan_id, firm_id, matter_id)
        REFERENCES public.case_work_plans(plan_id, firm_id, matter_id),
    FOREIGN KEY (source_run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (run_id, firm_id, matter_id)
        REFERENCES public.case_agent_runs(run_id, firm_id, matter_id),
    FOREIGN KEY (requested_by, firm_id)
        REFERENCES public.users(user_id, firm_id)
);

CREATE FUNCTION public.guard_case_agent_active_plan_execution_run()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    exact_binding boolean;
    session_firm_id text;
BEGIN
    -- The row is browser-triggered input.  A SECURITY DEFINER guard must
    -- never replace the server-established tenant context with NEW.firm_id;
    -- doing so would turn a forged row into the RLS authority used below.
    session_firm_id := pg_catalog.current_setting('app.firm_id', true);
    IF session_firm_id IS NULL
       OR session_firm_id = ''
       OR session_firm_id <> NEW.firm_id::text THEN
        RAISE EXCEPTION 'active-plan execution tenant context differs from row'
            USING ERRCODE = '42501';
    END IF;
    SELECT EXISTS (
        SELECT 1
        FROM public.case_work_plan_heads head
        JOIN public.case_work_plans plan
          ON plan.plan_id = head.current_plan_id
         AND plan.firm_id = head.firm_id AND plan.matter_id = head.matter_id
        JOIN public.matters matter
          ON matter.matter_id = plan.matter_id AND matter.firm_id = plan.firm_id
        JOIN public.case_agent_work_plan_promotions promotion
          ON promotion.plan_id = plan.plan_id
         AND promotion.firm_id = plan.firm_id
         AND promotion.matter_id = plan.matter_id
        JOIN public.case_agent_runs source_run
          ON source_run.run_id = promotion.run_id
         AND source_run.firm_id = promotion.firm_id
         AND source_run.matter_id = promotion.matter_id
        JOIN public.case_agent_runs execution_run
          ON execution_run.run_id = NEW.run_id
         AND execution_run.firm_id = NEW.firm_id
         AND execution_run.matter_id = NEW.matter_id
        JOIN public.case_agent_goals goal
          ON goal.goal_id = execution_run.goal_id
         AND goal.firm_id = execution_run.firm_id
         AND goal.matter_id = execution_run.matter_id
        JOIN public.case_agent_goals source_goal
          ON source_goal.goal_id = source_run.goal_id
         AND source_goal.firm_id = source_run.firm_id
         AND source_goal.matter_id = source_run.matter_id
        WHERE head.matter_id = NEW.matter_id
          AND head.firm_id = NEW.firm_id
          AND plan.plan_id = NEW.plan_id
          AND plan.plan_hash = NEW.plan_hash
          AND plan.activated_matter_version = NEW.activated_matter_version
          AND plan.status = 'ACTIVE'
          AND plan.activated_matter_version = matter.version
          AND source_run.run_id = NEW.source_run_id
          AND source_run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
          AND NOT source_run.is_stale
          AND NOT source_run.is_cancelled
          AND execution_run.snapshot_matter_version = matter.version
          AND execution_run.created_by = NEW.requested_by
          AND goal.requested_by = NEW.requested_by
          AND source_goal.active_plan_execution IS NULL
          AND goal.requested_deliverables = source_goal.requested_deliverables
          AND goal.active_plan_execution->>'plan_id' = NEW.plan_id::text
          AND goal.active_plan_execution->>'plan_hash' = NEW.plan_hash::text
          AND goal.active_plan_execution->>'source_run_id' = NEW.source_run_id::text
          AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(goal.active_plan_execution->'items')
                     AS execution_items(item)
                LEFT JOIN public.case_work_plan_items plan_item
                  ON plan_item.item_id = (item->>'item_id')::uuid
                 AND plan_item.plan_id = plan.plan_id
                 AND plan_item.firm_id = plan.firm_id
                 AND plan_item.matter_id = plan.matter_id
                 AND plan_item.item_kind = 'DOCUMENT_CANDIDATE'
                 AND plan_item.readiness = 'ACTIONABLE'
                 AND plan_item.delivery_target = 'INTERNAL_WORK_PRODUCT'
                 AND plan_item.deliverable_kind = item->>'deliverable_kind'
                WHERE plan_item.item_id IS NULL
          )
    ) INTO exact_binding;
    IF NOT exact_binding THEN
        RAISE EXCEPTION USING
            MESSAGE = 'active work-plan execution run differs from current server authority',
            ERRCODE = 'P4092';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER case_agent_active_plan_execution_runs_guard
    BEFORE INSERT ON public.case_agent_active_plan_execution_runs
    FOR EACH ROW EXECUTE FUNCTION public.guard_case_agent_active_plan_execution_run();

CREATE TRIGGER case_agent_active_plan_execution_runs_append_only
    BEFORE UPDATE OR DELETE ON public.case_agent_active_plan_execution_runs
    FOR EACH ROW EXECUTE FUNCTION public.prohibit_case_agent_history_mutation();

ALTER TABLE public.case_agent_active_plan_execution_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.case_agent_active_plan_execution_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY case_agent_active_plan_execution_runs_firm_isolation
    ON public.case_agent_active_plan_execution_runs
    USING (firm_id::text = current_setting('app.firm_id', true))
    WITH CHECK (firm_id::text = current_setting('app.firm_id', true));

CREATE INDEX case_agent_active_plan_execution_runs_matter_idx
    ON public.case_agent_active_plan_execution_runs(firm_id, matter_id, created_at DESC);

REVOKE ALL ON TABLE public.case_agent_active_plan_execution_runs
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.case_agent_goals
    FROM lawcase_web_application, lawcase_agent_worker;
REVOKE INSERT ON TABLE public.case_agent_goals
    FROM lawcase_agent_worker;

REVOKE ALL ON FUNCTION
    public.case_agent_requested_deliverables_valid(jsonb),
    public.case_agent_active_plan_execution_valid(jsonb, jsonb),
    public.guard_case_agent_active_plan_execution_run()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

-- The Web principal inserts governed goal rows, so PostgreSQL must be able to
-- invoke the two immutable CHECK helpers during that INSERT.  They expose no
-- tenant data or mutation authority.  The SECURITY DEFINER binding guard is
-- trigger-only and remains non-callable by both runtime roles.
GRANT EXECUTE ON FUNCTION
    public.case_agent_requested_deliverables_valid(jsonb),
    public.case_agent_active_plan_execution_valid(jsonb, jsonb)
    TO lawcase_web_application;

GRANT SELECT, INSERT ON TABLE public.case_agent_active_plan_execution_runs
    TO lawcase_web_application;
GRANT SELECT ON TABLE
    public.case_work_plan_heads,
    public.case_work_plans,
    public.case_work_plan_items,
    public.case_agent_work_plan_promotions
    TO lawcase_web_application;
GRANT SELECT (goal_id, firm_id, matter_id, requested_deliverables, active_plan_execution)
    ON TABLE public.case_agent_goals TO lawcase_web_application;
-- PostgreSQL row locks require a narrow UPDATE entitlement.  The Web service
-- uses these identity/version columns only to lock the current matter and
-- source run; it receives no UPDATE privilege on plan/promotion/goal history.
GRANT UPDATE (version) ON TABLE public.matters TO lawcase_web_application;
GRANT UPDATE (run_id) ON TABLE public.case_agent_runs TO lawcase_web_application;
GRANT SELECT ON TABLE public.case_agent_active_plan_execution_runs
    TO lawcase_agent_worker;
GRANT SELECT (requested_deliverables, active_plan_execution)
    ON TABLE public.case_agent_goals
    TO lawcase_web_application, lawcase_agent_worker;
GRANT INSERT (requested_deliverables, active_plan_execution)
    ON TABLE public.case_agent_goals
    TO lawcase_web_application;

COMMIT;
