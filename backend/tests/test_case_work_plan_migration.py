from pathlib import Path
import unittest


class DynamicCaseWorkPlanMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1] / "migrations" / "0030_dynamic_case_work_plans.sql"
        ).read_text(encoding="utf-8")

    def test_plan_history_and_details_are_tenant_isolated_and_guarded(self) -> None:
        for table in (
            "case_work_plans",
            "case_work_plan_items",
            "case_work_plan_context_references",
            "case_work_plan_item_references",
            "case_work_plan_item_prerequisites",
            "case_work_plan_heads",
            "case_work_plan_events",
        ):
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("case work plan details and events are append-only", self.sql)
        self.assertIn("CREATE UNIQUE INDEX case_work_plans_one_active_per_matter", self.sql)

    def test_submission_spec_binds_dynamic_plan_and_posture(self) -> None:
        self.assertIn("ADD COLUMN work_plan_id uuid", self.sql)
        self.assertIn("ADD COLUMN work_plan_hash char(64)", self.sql)
        self.assertIn("ADD COLUMN posture_profile_id uuid", self.sql)
        self.assertIn("REFERENCES case_work_plans(plan_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES case_posture_profiles(profile_id, firm_id, matter_id)", self.sql)

    def test_no_role_or_defence_document_is_hard_coded(self) -> None:
        self.assertNotIn("DEFENCE_STATEMENT", self.sql)
        self.assertNotIn("CIVIL_COMPLAINT", self.sql)
        self.assertNotIn("represented_position =", self.sql)


if __name__ == "__main__":
    unittest.main()
