from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0046_case_agent_ledger_replan_bridge.sql"
)


class CaseAgentLedgerReplanBridgeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_confirmation_outbox_derives_server_owned_refresh_and_wake(self) -> None:
        self.assertIn("case_agent_snapshot_refresh_requests", self.sql)
        self.assertIn(
            "CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED", self.sql
        )
        self.assertIn(
            "case_agent_ledger_confirmation_enqueues_snapshot_refresh", self.sql
        )
        self.assertIn("source_outbox_id uuid NOT NULL UNIQUE", self.sql)
        self.assertIn("source_audit_event_id", self.sql)
        self.assertIn("case_agent_snapshot_refresh_wakes_worker", self.sql)
        self.assertIn("case_agent_run_inbox", self.sql)
        self.assertIn("pg_notify('case_agent_run_ready'", self.sql)

    def test_open_review_is_not_mislabeled_resolved(self) -> None:
        for status in (
            "BLOCKED_BY_OPEN_REVIEW",
            "BLOCKED_BY_OPEN_EXCEPTIONS",
            "SUPERSEDED",
            "PENDING",
            "APPLIED",
        ):
            self.assertIn(status, self.sql)
        self.assertIn("open_review_count", self.sql)
        self.assertIn("open_exception_count", self.sql)
        self.assertIn("review_lane = 'EXCEPTION_REVIEW'", self.sql)
        self.assertIn(
            "request_status IN (\n                'PENDING', 'BLOCKED_BY_OPEN_REVIEW'",
            self.sql,
        )

    def test_latest_request_supersedes_older_versions_and_consumer_can_coalesce(self) -> None:
        self.assertIn("prior.request_status IN", self.sql)
        self.assertIn("prior.target_matter_version <= NEW.aggregate_version", self.sql)
        self.assertIn("replacement.target_matter_version >= OLD.target_matter_version", self.sql)
        self.assertIn("UNIQUE (run_id, target_matter_version)", self.sql)

    def test_request_binds_run_through_batch_without_inverse_run_row_lock(self) -> None:
        self.assertIn(
            "case_agent_ledger_extraction_batches_run_binding_unique",
            self.sql,
        )
        self.assertIn(
            "FOREIGN KEY (extraction_batch_id, run_id, firm_id, matter_id)",
            self.sql,
        )
        self.assertNotIn(
            "FOREIGN KEY (run_id, firm_id, matter_id)\n"
            "        REFERENCES case_agent_runs",
            self.sql,
        )

    def test_plan_promotion_and_activation_have_database_backstops(self) -> None:
        self.assertIn("case_agent_work_plan_open_ledger_review_block", self.sql)
        self.assertIn(
            "case_work_plan_open_ledger_review_activation_block", self.sql
        )
        self.assertIn(
            "Agent work plan promotion is blocked by open ledger review", self.sql
        )
        self.assertIn(
            "work plan activation is blocked by open ledger review", self.sql
        )

    def test_request_is_tenant_isolated_guarded_and_indexed(self) -> None:
        self.assertIn(
            "ALTER TABLE case_agent_snapshot_refresh_requests FORCE ROW LEVEL SECURITY",
            self.sql,
        )
        self.assertIn(
            "case_agent_snapshot_refresh_requests_guard", self.sql
        )
        self.assertIn("case_agent_snapshot_refresh_pending_idx", self.sql)
        self.assertIn("case_agent_snapshot_refresh_run_idx", self.sql)
        self.assertIn(
            "REVOKE ALL ON TABLE case_agent_snapshot_refresh_requests FROM PUBLIC",
            self.sql,
        )

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
