from pathlib import Path
import unittest


class LedgerExceptionReviewMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0047_case_agent_ledger_exception_groups.sql"
        ).read_text()

    def test_groups_members_decisions_and_events_are_immutable_and_tenant_scoped(self) -> None:
        for table in (
            "case_agent_ledger_exception_groups",
            "case_agent_ledger_exception_group_members",
            "case_agent_ledger_exception_group_decisions",
            "case_agent_ledger_exception_decision_events",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("case Agent ledger exception records are append-only", self.sql)
        self.assertIn("bound_candidate_set_hash", self.sql)
        self.assertIn("bound_candidate_count", self.sql)

    def test_server_materializes_exact_policy_groups_and_complete_membership(self) -> None:
        self.assertIn("materialize_case_agent_ledger_exception_groups", self.sql)
        self.assertIn("case_agent_ledger_exception_canonical_reasons", self.sql)
        self.assertIn("case_agent_ledger_exception_source_policy", self.sql)
        self.assertIn("case_agent_ledger_exception_risk_policy", self.sql)
        self.assertIn("exception groups do not bind the complete immutable lane", self.sql)
        self.assertIn("string_agg(candidate_hash, ',' ORDER BY candidate_hash)", self.sql)

    def test_empty_low_risk_and_terminal_exception_lanes_have_distinct_statuses(self) -> None:
        for value in (
            "EMPTY",
            "OPEN",
            "CONFIRMED",
            "EXCEPTIONS_OPEN",
            "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
            "RESOLVED",
        ):
            self.assertIn(f"'{value}'", self.sql)
        self.assertIn("confirmed_candidate_count = eligible_candidate_count", self.sql)

    def test_one_run_proof_requires_current_receipt_complete_staging_and_all_batches(self) -> None:
        self.assertIn("case_agent_ledger_extraction_run_staging_complete", self.sql)
        self.assertIn("receipt.verification_hash = run.verification_hash", self.sql)
        self.assertIn("receipt.snapshot_hash = run.snapshot_hash", self.sql)
        self.assertIn("JOIN current_receipt receipt", self.sql)
        self.assertIn("case_agent_ledger_extraction_run_review_resolved", self.sql)
        self.assertIn("WHEN NOT EXISTS (SELECT 1 FROM review) THEN true", self.sql)

    def test_multi_batch_version_chain_ignores_non_advancing_audit_and_rejects_other_graphs(self) -> None:
        self.assertIn("case_agent_ledger_extraction_current_review_version", self.sql)
        self.assertGreaterEqual(
            self.sql.count("audit.output_version = audit.input_version + 1"), 2
        )
        self.assertIn("confirmed_batch.graph_id = source_graph_id", self.sql)
        self.assertIn("valid_transition_count <> transition_count", self.sql)

    def test_refresh_and_plan_gates_reuse_run_resolved_without_fake_exception_promotions(self) -> None:
        self.assertIn(
            "CREATE OR REPLACE FUNCTION enqueue_case_agent_snapshot_refresh_from_ledger_confirmation",
            self.sql,
        )
        self.assertIn(
            "CREATE OR REPLACE FUNCTION block_unresolved_ledger_review_work_plan_promotion",
            self.sql,
        )
        self.assertIn(
            "CREATE OR REPLACE FUNCTION block_unresolved_ledger_review_work_plan_activation",
            self.sql,
        )
        self.assertNotIn("case_agent_ledger_extraction_promotions", self.sql)
        self.assertIn("CASE_LEDGER_EXTRACTION_EXCEPTION_ONLY_RUN_RESOLVED", self.sql)


if __name__ == "__main__":
    unittest.main()
