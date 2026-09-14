from __future__ import annotations

from pathlib import Path
import unittest


class LawyerDecisionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0035_case_agent_lawyer_decision_signals.sql"
        ).read_text(encoding="utf-8")

    def test_is_rls_isolated_and_source_bound(self) -> None:
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("current_setting('app.firm_id', true)", self.sql)
        self.assertIn("REFERENCES case_agent_tasks", self.sql)
        self.assertIn("REFERENCES case_agent_events", self.sql)
        self.assertIn("source_ref_ids jsonb NOT NULL", self.sql)

    def test_adds_exact_supervisor_event(self) -> None:
        self.assertIn("LAWYER_PLAN_CORRECTION_RECORDED", self.sql)
        self.assertIn("DROP CONSTRAINT case_agent_events_event_type_check", self.sql)

    def test_history_is_append_only_except_exact_supersession(self) -> None:
        self.assertIn("case_agent_lawyer_decision_signals_guard", self.sql)
        self.assertIn("OR NOT OLD.is_current", self.sql)
        self.assertIn("OR NEW.is_current", self.sql)
        self.assertIn("case_agent_lawyer_signal_current_subject_idx", self.sql)


if __name__ == "__main__":
    unittest.main()
