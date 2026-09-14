-- PostgreSQL 16+; apply after 0106_evidence_catalogue_promotion_binding_identity.sql.
--
-- A source run may request several internal review outputs while one optional
-- output is honestly waiting for a missing source (for example, a payment
-- ledger without a confirmed transaction).  Preserve the complete source-goal
-- intent, but permit the explicit second run to bind only its exact, source-
-- ready subset.  The guard still requires every selected item to be actionable
-- and forbids any broadened output kind or source lineage.

BEGIN;

CREATE OR REPLACE FUNCTION public.guard_case_agent_active_plan_execution_run()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    exact_binding boolean;
    exact_retry boolean;
    session_firm_id text;
BEGIN
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
          AND source_goal.requested_deliverables @> goal.requested_deliverables
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

    IF NEW.execution_attempt = 1 THEN
        exact_retry := NEW.supersedes_execution_id IS NULL
            AND NEW.retry_reason_code IS NULL;
    ELSE
        SELECT EXISTS (
            SELECT 1
            FROM public.case_agent_active_plan_execution_runs predecessor
            JOIN public.case_agent_runs predecessor_run
              ON predecessor_run.run_id = predecessor.run_id
             AND predecessor_run.firm_id = predecessor.firm_id
             AND predecessor_run.matter_id = predecessor.matter_id
            WHERE predecessor.execution_id = NEW.supersedes_execution_id
              AND predecessor.firm_id = NEW.firm_id
              AND predecessor.matter_id = NEW.matter_id
              AND predecessor.plan_id = NEW.plan_id
              AND predecessor.plan_hash = NEW.plan_hash
              AND predecessor.source_run_id = NEW.source_run_id
              AND predecessor.execution_attempt = NEW.execution_attempt - 1
              AND predecessor_run.status = 'WAITING_INPUT'
              AND NOT predecessor_run.is_stale
              AND NOT predecessor_run.is_cancelled
              AND NEW.retry_reason_code = 'SAFE_LOCAL_FAILURE_REISSUE'
              AND EXISTS (
                    SELECT 1
                    FROM public.case_agent_task_receipts receipt
                    WHERE receipt.run_id = predecessor.run_id
                      AND receipt.firm_id = predecessor.firm_id
                      AND receipt.matter_id = predecessor.matter_id
                      AND receipt.result_status = 'FAILED'
                      AND receipt.external_submission_state = 'NOT_APPLICABLE'
                      AND receipt.error_code IN (
                          'LOCAL_ADAPTER_FAILED',
                          'DOCUMENT_RENDERER_REJECTED',
                          'DOCUMENT_RENDERER_RESULT_UNKNOWN'
                      )
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM public.case_agent_task_receipts receipt
                    WHERE receipt.run_id = predecessor.run_id
                      AND receipt.firm_id = predecessor.firm_id
                      AND receipt.matter_id = predecessor.matter_id
                      AND (
                          receipt.external_submission_state <> 'NOT_APPLICABLE'
                          OR receipt.external_calls <> 0
                          OR receipt.result_status = 'UNKNOWN'
                      )
              )
        ) INTO exact_retry;
    END IF;
    IF NOT exact_retry THEN
        RAISE EXCEPTION USING
            MESSAGE = 'active work-plan execution reissue is not a safe local retry',
            ERRCODE = 'P4093';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.guard_case_agent_active_plan_execution_run()
    FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;

COMMIT;
