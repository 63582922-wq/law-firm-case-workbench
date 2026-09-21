-- Correct the 0105 catalogue-reference comparison.  Work-plan item
-- references point to the immutable promotion binding identity, not directly
-- to the planning object identity.  The object/version/hash checks remain in
-- the binding row and the item reference must use that exact binding id/hash.

BEGIN;

CREATE OR REPLACE FUNCTION public.validate_case_agent_work_plan_promotion()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM case_work_plans plan
        JOIN case_agent_runs run
          ON run.run_id = NEW.run_id
         AND run.firm_id = NEW.firm_id AND run.matter_id = NEW.matter_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = NEW.graph_id AND graph.run_id = NEW.run_id
         AND graph.firm_id = NEW.firm_id AND graph.matter_id = NEW.matter_id
        JOIN case_agent_verification_receipts receipt
          ON receipt.verification_receipt_id = NEW.verification_receipt_id
         AND receipt.run_id = NEW.run_id
         AND receipt.firm_id = NEW.firm_id AND receipt.matter_id = NEW.matter_id
        JOIN case_agent_goals goal
          ON goal.goal_id = NEW.goal_id
         AND goal.firm_id = NEW.firm_id AND goal.matter_id = NEW.matter_id
        JOIN case_posture_profiles posture
          ON posture.profile_id = NEW.posture_profile_id
         AND posture.firm_id = NEW.firm_id AND posture.matter_id = NEW.matter_id
        JOIN case_posture_profile_heads posture_head
          ON posture_head.current_profile_id = posture.profile_id
         AND posture_head.firm_id = posture.firm_id
         AND posture_head.matter_id = posture.matter_id
        WHERE plan.plan_id = NEW.plan_id
          AND plan.firm_id = NEW.firm_id AND plan.matter_id = NEW.matter_id
          AND plan.status = 'CANDIDATE'
          AND plan.required_court_document_kinds = '[]'::jsonb
          AND plan.primary_court_document_kind IS NULL
          AND plan.agent_goal_id = goal.goal_id
          AND plan.objective_approval_id IS NULL
          AND plan.objective_hash = goal.goal_hash
          AND plan.planned_matter_version = run.snapshot_matter_version
          AND plan.planned_matter_version = graph.snapshot_matter_version
          AND plan.profile_id = NEW.posture_profile_id
          AND plan.profile_version = NEW.posture_profile_version
          AND plan.profile_hash = NEW.posture_profile_hash
          AND run.goal_id = goal.goal_id
          AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
          AND NOT run.is_stale AND NOT run.is_cancelled
          AND run.current_graph_id = graph.graph_id
          AND run.current_graph_version = graph.graph_version
          AND run.current_graph_hash = graph.graph_hash
          AND run.snapshot_hash = graph.snapshot_hash
          AND run.snapshot_schema_version = graph.snapshot_schema_version
          AND run.verification_hash = receipt.verification_hash
          AND receipt.outcome = 'PASSED'
          AND receipt.graph_hash = graph.graph_hash
          AND receipt.snapshot_hash = graph.snapshot_hash
          AND receipt.verification_hash = NEW.verification_hash
          AND receipt.verifier_actor_id = NEW.verifier_actor_id
          AND receipt.execution_actor_id = NEW.execution_actor_id
          AND graph.graph_version = NEW.graph_version
          AND graph.graph_hash = NEW.graph_hash
          AND graph.snapshot_matter_version = NEW.snapshot_matter_version
          AND graph.snapshot_schema_version = NEW.snapshot_schema_version
          AND graph.snapshot_hash = NEW.snapshot_hash
          AND goal.goal_hash = NEW.goal_hash
          AND posture.status = 'CONFIRMED'
          AND posture.profile_version = NEW.posture_profile_version
          AND posture.profile_hash = NEW.posture_profile_hash
          AND NEW.task_count = (
              SELECT count(*)
              FROM case_agent_tasks task
              WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                AND task.firm_id = NEW.firm_id AND task.matter_id = NEW.matter_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM case_agent_tasks task
              CROSS JOIN LATERAL jsonb_array_elements_text(task.input_refs) input_ref
              WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                AND task.firm_id = NEW.firm_id AND task.matter_id = NEW.matter_id
                AND NOT EXISTS (
                    SELECT 1 FROM case_agent_work_plan_input_bindings binding
                    WHERE binding.plan_id = NEW.plan_id
                      AND binding.promotion_id = NEW.promotion_id
                      AND binding.firm_id = NEW.firm_id
                      AND binding.matter_id = NEW.matter_id
                      AND binding.input_ref = input_ref.value
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM case_agent_work_plan_input_bindings binding
              WHERE binding.plan_id = NEW.plan_id
                AND binding.promotion_id = NEW.promotion_id
                AND binding.firm_id = NEW.firm_id
                AND binding.matter_id = NEW.matter_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM case_agent_tasks task
                    CROSS JOIN LATERAL jsonb_array_elements_text(task.input_refs) input_ref
                    WHERE task.graph_id = NEW.graph_id AND task.run_id = NEW.run_id
                      AND task.firm_id = NEW.firm_id
                      AND task.matter_id = NEW.matter_id
                      AND input_ref.value = binding.input_ref
                )
                AND NOT (
                    binding.object_type = 'EVIDENCE_PAGE'
                    AND binding.source_status = 'CONFIRMED'
                    AND binding.reference_use = 'EVIDENCE'
                    AND binding.input_ref = 'evidence-page:' || binding.object_id::text
                    AND EXISTS (
                        SELECT 1
                        FROM case_work_plan_items item
                        JOIN case_work_plan_item_references item_reference
                          ON item_reference.plan_id = item.plan_id
                         AND item_reference.item_id = item.item_id
                         AND item_reference.firm_id = item.firm_id
                         AND item_reference.matter_id = item.matter_id
                        WHERE item.plan_id = NEW.plan_id
                          AND item.firm_id = NEW.firm_id
                          AND item.matter_id = NEW.matter_id
                          AND item.item_kind = 'DOCUMENT_CANDIDATE'
                          AND item.deliverable_kind = 'EVIDENCE_CATALOGUE'
                          AND item_reference.reference_role = 'SOURCE'
                          AND item_reference.source_type = 'AGENT_TASK_INPUT'
                          AND item_reference.source_id = binding.binding_id
                          AND item_reference.source_version = binding.object_version
                          AND item_reference.source_hash = binding.binding_hash
                          AND item_reference.reference_use = 'EVIDENCE'
                    )
                )
          )
    ) THEN
        RAISE EXCEPTION 'work plan promotion differs from its PASSED Agent graph';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
