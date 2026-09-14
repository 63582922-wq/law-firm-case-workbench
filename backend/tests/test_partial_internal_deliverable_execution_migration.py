from __future__ import annotations

from pathlib import Path
import unittest


class PartialInternalDeliverableExecutionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0107_partial_internal_deliverable_execution.sql"
        ).read_text(encoding="utf-8")

    def test_guard_accepts_only_a_source_goal_subset_with_actionable_plan_items(self) -> None:
        self.assertIn(
            "source_goal.requested_deliverables @> goal.requested_deliverables",
            self.sql,
        )
        self.assertNotIn(
            "goal.requested_deliverables = source_goal.requested_deliverables",
            self.sql,
        )
        self.assertIn("plan_item.readiness = 'ACTIONABLE'", self.sql)
        self.assertIn("plan_item.delivery_target = 'INTERNAL_WORK_PRODUCT'", self.sql)

    def test_guard_keeps_tenant_and_safe_retry_boundaries(self) -> None:
        self.assertIn("session_firm_id <> NEW.firm_id::text", self.sql)
        self.assertIn("SAFE_LOCAL_FAILURE_REISSUE", self.sql)
        self.assertIn("receipt.external_calls <> 0", self.sql)
        self.assertIn("REVOKE ALL ON FUNCTION public.guard_case_agent_active_plan_execution_run()", self.sql)


if __name__ == "__main__":
    unittest.main()
