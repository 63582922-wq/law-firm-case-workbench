from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0102_fact_correction_lineage_search_path.sql"
)
FOLLOWUP_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0103_fact_correction_guard_search_paths.sql"
)


class FactCorrectionLineageSearchPathMigrationTests(unittest.TestCase):
    def test_lineage_trigger_has_a_safe_schema_binding(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn(
            "ALTER FUNCTION public.guard_fact_correction_candidate_lineage()",
            sql,
        )
        self.assertIn("SET search_path TO pg_catalog, public", sql)
        self.assertIn(
            "REVOKE ALL ON FUNCTION public.guard_fact_correction_candidate_lineage() FROM PUBLIC",
            sql,
        )

    def test_sibling_guards_share_the_same_safe_schema_binding(self) -> None:
        sql = FOLLOWUP_MIGRATION.read_text(encoding="utf-8")
        for function in (
            "guard_case_agent_fact_correction_proposal()",
            "guard_extraction_promotion_after_correction()",
        ):
            self.assertIn(f"ALTER FUNCTION public.{function}", sql)
            self.assertIn(
                f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC",
                sql,
            )
        self.assertEqual(sql.count("SET search_path TO pg_catalog, public"), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
