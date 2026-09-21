from pathlib import Path
import unittest


class ReviewableDraftNonAuthoritativeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0074_reviewable_office_draft_non_authoritative.sql"
        ).read_text(encoding="utf-8")

    def test_only_fixed_derived_draft_events_may_keep_the_matter_version(self) -> None:
        self.assertIn("DROP CONSTRAINT audit_events_version_transition_valid", self.sql)
        self.assertIn("ADD CONSTRAINT audit_events_version_transition_valid", self.sql)
        self.assertIn("VALIDATE CONSTRAINT audit_events_version_transition_valid", self.sql)
        self.assertIn("REVIEWABLE_OFFICE_DRAFT_PAIR_REGISTERED", self.sql)
        self.assertIn("REVIEWABLE_OFFICE_DRAFT_PAIR_APPROVED", self.sql)
        self.assertIn("CASE_WORK_PLAN_ITEM_REVIEWED", self.sql)
        self.assertIn("CASE_LEDGER_EXCEPTION_CONTROL_TRANSFERRED", self.sql)
        self.assertIn("output_version > input_version", self.sql)
        self.assertIn("output_version = input_version", self.sql)
        self.assertNotIn("OUTBOX", self.sql)


if __name__ == "__main__":
    unittest.main()
