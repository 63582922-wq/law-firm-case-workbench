from pathlib import Path
import re
import unittest


class CaseWorkPlanLawyerReviewMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core_sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0001_core.sql"
        ).read_text(encoding="utf-8")
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0044_case_work_plan_lawyer_reviews.sql"
        ).read_text(encoding="utf-8")

    def test_non_mutating_audit_exception_is_narrow_and_migrated_without_gap(self) -> None:
        self.assertIn(
            "CONSTRAINT audit_events_output_version_check",
            self.core_sql,
        )
        add = self.sql.index("ADD CONSTRAINT audit_events_version_transition_valid")
        validate = self.sql.index(
            "VALIDATE CONSTRAINT audit_events_version_transition_valid"
        )
        drop = self.sql.index(
            "DROP CONSTRAINT audit_events_output_version_check"
        )
        self.assertLess(add, validate)
        self.assertLess(validate, drop)
        self.assertRegex(
            self.sql,
            re.compile(
                r"output_version > input_version\s+OR\s+\(\s*"
                r"event_type = 'CASE_WORK_PLAN_ITEM_REVIEWED'\s+"
                r"AND output_version = input_version",
                re.DOTALL,
            ),
        )
        self.assertIn(") NOT VALID;", self.sql)

    def test_reviews_are_append_only_tenant_isolated_and_source_bound(self) -> None:
        self.assertIn("CREATE TABLE case_work_plan_item_reviews", self.sql)
        self.assertIn("UNIQUE (plan_id, item_id)", self.sql)
        self.assertIn(
            "FOREIGN KEY (item_id, plan_id, firm_id, matter_id)", self.sql
        )
        self.assertIn(
            "ON case_work_plan_item_reviews (item_id, plan_id, firm_id, matter_id)",
            self.sql,
        )
        self.assertIn(
            "case_work_plan_item_reviews_append_only", self.sql
        )
        self.assertIn(
            "ALTER TABLE case_work_plan_item_reviews FORCE ROW LEVEL SECURITY",
            self.sql,
        )
        self.assertIn(
            "current_setting('app.firm_id', true)", self.sql
        )

    def test_adverse_review_blocks_activation_at_database_boundary(self) -> None:
        self.assertIn(
            "case_work_plan_adverse_review_activation_block", self.sql
        )
        self.assertRegex(
            self.sql,
            re.compile(
                r"OLD\.status = 'CANDIDATE'.+NEW\.status = 'ACTIVE'.+"
                r"decision IN \('REQUEST_CHANGE', 'REJECT'\)",
                re.DOTALL,
            ),
        )
        self.assertIn(
            "plan.plan_version = head.latest_plan_version", self.sql
        )
        self.assertIn(
            "plan.planned_matter_version + 1 = matter.version", self.sql
        )
        self.assertEqual(
            self.sql.count(
                "plan.planned_matter_version + 1 = matter.version"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
