from __future__ import annotations

from pathlib import Path
import unittest


class DefenceStatementActivePlanMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0075_defence_statement_active_plan_delivery.sql"
        ).read_text(encoding="utf-8")

    def test_extends_the_closed_goal_catalogue_without_removing_existing_kinds(self) -> None:
        self.assertIn("CREATE OR REPLACE FUNCTION public.case_agent_requested_deliverables_valid", self.sql)
        self.assertIn("jsonb_array_length(value) > 3", self.sql)
        for kind in ("CASE_REVIEW_MEMO", "DEFENCE_STATEMENT", "PAYMENT_LEDGER"):
            self.assertIn(kind, self.sql)

    def test_active_plan_execution_keeps_exact_format_and_canonical_item_guards(self) -> None:
        self.assertIn("CREATE OR REPLACE FUNCTION public.case_agent_active_plan_execution_valid", self.sql)
        self.assertIn("jsonb_array_length(execution->'items') NOT BETWEEN 1 AND 3", self.sql)
        self.assertIn("item->>'deliverable_kind' = 'DEFENCE_STATEMENT'", self.sql)
        self.assertIn("item->>'output_format' <> 'DOCX'", self.sql)
        self.assertIn("requested = execution_kinds", self.sql)
        self.assertIn("count(DISTINCT value->>'item_id')", self.sql)


if __name__ == "__main__":
    unittest.main()
