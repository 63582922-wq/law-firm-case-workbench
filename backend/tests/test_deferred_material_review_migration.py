from pathlib import Path
import unittest


class DeferredMaterialReviewMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0100_deferred_material_review_does_not_block_analysis.sql"
        ).read_text(encoding="utf-8")

    def test_wake_keeps_source_blocking_followups_but_excludes_deferred_review(self) -> None:
        self.assertIn("wake_case_agent_run()", self.sql)
        self.assertIn("case_agent_ledger_exception_followups followup", self.sql)
        self.assertIn(
            "followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')",
            self.sql,
        )
        self.assertNotIn("DEFERRED_REVIEW')", self.sql)

    def test_only_pristine_non_control_runs_are_requeued(self) -> None:
        for clause in (
            "run.status = 'CREATED'",
            "run.current_event_version = 1",
            "NOT run.is_cancelled",
            "case_agent_ledger_exception_recovery_quarantines",
            "case_agent_ledger_exception_recovery_intents",
            "case_agent_ledger_exception_control_assignments",
        ):
            self.assertIn(clause, self.sql)

    def test_follow_up_migration_uses_migrator_only_for_preexisting_rows(self) -> None:
        sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0101_requeue_pristine_deferred_review_runs.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("RESET ROLE", sql)
        self.assertIn("run.status = 'CREATED'", sql)
        self.assertIn("run.current_event_version = 1", sql)
        self.assertIn(
            "followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')",
            sql,
        )


if __name__ == "__main__":
    unittest.main()
