from pathlib import Path
import unittest

from case_kernel.case_agent_runtime_postgres import (
    _CASE_AGENT_RUNTIME_REQUIRED_COLUMNS,
    _CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS,
)


class VerifiedAgentGraphWorkPlanPromotionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0043_verified_agent_graph_work_plan_promotions.sql"
        ).read_text(encoding="utf-8")

    def test_purpose_and_exact_objective_authority_are_persisted(self) -> None:
        self.assertIn("ADD COLUMN purpose text NOT NULL", self.sql)
        self.assertIn("case_work_plans_exact_objective_source", self.sql)
        self.assertIn("ADD COLUMN agent_goal_id uuid", self.sql)

    def test_legacy_plan_hashes_fail_closed_instead_of_receiving_fake_purpose(self) -> None:
        self.assertIn(
            "0043 requires an empty case_work_plans table",
            self.sql,
        )
        self.assertIn(
            "Legacy plan hashes do not cover item purpose.",
            self.sql,
        )
        self.assertNotIn("DEFAULT '旧计划事项", self.sql)
        self.assertNotIn("UPDATE case_work_plan_items", self.sql)

    def test_new_composite_foreign_keys_have_child_indexes(self) -> None:
        for index_name in (
            "case_work_plans_agent_goal_idx",
            "case_agent_work_plan_promotions_goal_idx",
            "case_agent_work_plan_promotions_posture_idx",
            "case_agent_work_plan_promotions_verifier_actor_idx",
            "case_agent_work_plan_promotions_execution_actor_idx",
            "case_agent_work_plan_promotions_promoted_by_idx",
        ):
            self.assertIn(f"CREATE INDEX {index_name}", self.sql)

    def test_only_current_passed_independent_graph_can_back_candidate(self) -> None:
        self.assertIn("receipt.outcome = 'PASSED'", self.sql)
        self.assertIn("run.current_graph_id = graph.graph_id", self.sql)
        self.assertIn("run.status IN ('READY_FOR_REVIEW', 'COMPLETED')", self.sql)
        self.assertIn("NOT run.is_stale AND NOT run.is_cancelled", self.sql)
        self.assertIn("verifier_actor_id <> execution_actor_id", self.sql)
        self.assertIn("DEFERRABLE INITIALLY DEFERRED", self.sql)

    def test_agent_candidate_cannot_become_court_output_or_active_on_insert(self) -> None:
        self.assertIn("plan.status = 'CANDIDATE'", self.sql)
        self.assertIn("plan.required_court_document_kinds = '[]'::jsonb", self.sql)
        self.assertIn("plan.primary_court_document_kind IS NULL", self.sql)
        self.assertNotIn("'ACTIVE'::text", self.sql)
        self.assertNotIn("DEFENCE_STATEMENT", self.sql)
        self.assertNotIn("CIVIL_COMPLAINT", self.sql)

    def test_provenance_is_append_only_tenant_isolated_and_input_complete(self) -> None:
        for table in (
            "case_agent_work_plan_promotions",
            "case_agent_work_plan_input_bindings",
        ):
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", self.sql)
        self.assertIn("Agent work plan promotion provenance is append-only", self.sql)
        self.assertIn("jsonb_array_elements_text(task.input_refs)", self.sql)
        self.assertIn("case_work_plan_context_references", self.sql)

    def test_worker_readiness_requires_0030_purpose_and_0043_guards(self) -> None:
        self.assertIn("purpose", _CASE_AGENT_RUNTIME_REQUIRED_COLUMNS["case_work_plan_items"])
        for table in (
            "case_agent_work_plan_promotions",
            "case_agent_work_plan_input_bindings",
        ):
            self.assertIn(table, _CASE_AGENT_RUNTIME_REQUIRED_COLUMNS)
        for trigger in (
            "case_agent_work_plan_promotion_valid",
            "case_work_plan_agent_goal_promotion_required",
            "case_agent_work_plan_promotions_append_only",
            "case_agent_work_plan_input_bindings_append_only",
        ):
            self.assertIn(trigger, _CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS)


class EvidenceCataloguePromotionGuardMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0106_evidence_catalogue_promotion_binding_identity.sql"
        ).read_text(encoding="utf-8")

    def test_only_confirmed_catalogue_source_pages_can_extend_graph_bindings(self) -> None:
        self.assertIn(
            "CREATE OR REPLACE FUNCTION public.validate_case_agent_work_plan_promotion()",
            self.sql,
        )
        for fragment in (
            "binding.object_type = 'EVIDENCE_PAGE'",
            "binding.source_status = 'CONFIRMED'",
            "binding.reference_use = 'EVIDENCE'",
            "binding.input_ref = 'evidence-page:' || binding.object_id::text",
            "item.deliverable_kind = 'EVIDENCE_CATALOGUE'",
            "item_reference.reference_role = 'SOURCE'",
            "item_reference.source_type = 'AGENT_TASK_INPUT'",
            "item_reference.source_id = binding.binding_id",
            "item_reference.source_hash = binding.binding_hash",
        ):
            self.assertIn(fragment, self.sql)

    def test_guard_keeps_passed_graph_and_candidate_only_boundaries(self) -> None:
        for fragment in (
            "receipt.outcome = 'PASSED'",
            "run.status IN ('READY_FOR_REVIEW', 'COMPLETED')",
            "plan.status = 'CANDIDATE'",
            "plan.required_court_document_kinds = '[]'::jsonb",
            "jsonb_array_elements_text(task.input_refs)",
        ):
            self.assertIn(fragment, self.sql)


if __name__ == "__main__":
    unittest.main()
