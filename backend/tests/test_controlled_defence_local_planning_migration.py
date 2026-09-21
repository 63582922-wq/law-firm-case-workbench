from pathlib import Path
import unittest


class ControlledDefenceLocalPlanningMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0076_controlled_defence_local_planning.sql"
        ).read_text(encoding="utf-8")

    def test_local_outcome_is_immutable_firm_scoped_and_worker_only(self) -> None:
        self.assertIn("CREATE TABLE public.case_agent_planning_local_events", self.sql)
        self.assertIn("controlled-first-release-defence-planner-v1", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("case_agent_planning_local_events_firm_isolation", self.sql)
        self.assertIn("case_agent_planning_local_events_append_only", self.sql)
        self.assertIn("GRANT SELECT, INSERT ON TABLE public.case_agent_planning_local_events", self.sql)
        self.assertIn("TO lawcase_agent_worker", self.sql)

    def test_local_outcome_has_no_provider_or_external_request_columns(self) -> None:
        self.assertNotIn("external_request_id", self.sql)
        self.assertNotIn("provider_id", self.sql)
        self.assertNotIn("service_id", self.sql)
        self.assertIn("FOREIGN KEY (run_id, planning_attempt_id, firm_id, matter_id)", self.sql)


if __name__ == "__main__":
    unittest.main()
