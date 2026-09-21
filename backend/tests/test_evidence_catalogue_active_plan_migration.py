from __future__ import annotations

from pathlib import Path
import unittest


class EvidenceCatalogueActivePlanMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0104_evidence_catalogue_active_plan_delivery.sql"
        ).read_text(encoding="utf-8")

    def test_closed_catalogue_adds_evidence_without_reopening_delivery_scope(self) -> None:
        self.assertIn(
            "CREATE OR REPLACE FUNCTION public.case_agent_requested_deliverables_valid",
            self.sql,
        )
        for kind in (
            "CASE_REVIEW_MEMO",
            "DEFENCE_STATEMENT",
            "EVIDENCE_CATALOGUE",
            "PAYMENT_LEDGER",
        ):
            self.assertIn(kind, self.sql)
        self.assertIn("jsonb_array_length(value) > 4", self.sql)
        self.assertIn("jsonb_array_length(value) = distinct_count", self.sql)

    def test_execution_contract_requires_xlsx_for_catalogue_and_keeps_canonical_guards(self) -> None:
        self.assertIn(
            "item->>'deliverable_kind' IN ('EVIDENCE_CATALOGUE', 'PAYMENT_LEDGER')",
            self.sql,
        )
        self.assertIn("item->>'output_format' <> 'XLSX'", self.sql)
        self.assertIn("requested = execution_kinds", self.sql)
        self.assertIn("count(DISTINCT value->>'item_id')", self.sql)
        self.assertIn("SET search_path = pg_catalog", self.sql)

    def test_grants_only_existing_web_control_path(self) -> None:
        self.assertIn("TO lawcase_web_application;", self.sql)
        self.assertNotIn("GRANT INSERT", self.sql)
        self.assertNotIn("GRANT UPDATE", self.sql)
        self.assertNotIn("GRANT DELETE", self.sql)


if __name__ == "__main__":
    unittest.main()
