from pathlib import Path
import unittest


SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "0045_case_agent_ledger_extraction_exchange.sql"
).read_text(encoding="utf-8")


class LedgerExtractionExchangeMigrationTests(unittest.TestCase):
    def test_exchange_is_exactly_bound_to_0031_task_attempt_and_fixed_provider(self):
        self.assertIn("external_request_id uuid NOT NULL UNIQUE", SQL)
        self.assertIn(
            "FOREIGN KEY (attempt_id, graph_id, task_id, run_id, firm_id, matter_id)",
            SQL,
        )
        self.assertIn("submission_record_id uuid NOT NULL", SQL)
        self.assertIn("submission_state IS DISTINCT FROM 'STARTED'", SQL)
        self.assertIn("task_row.external_request_id IS DISTINCT FROM NEW.external_request_id::text", SQL)
        self.assertIn("task_row.skill_id IS DISTINCT FROM 'case_ledger_extraction'", SQL)
        self.assertIn("task_row.retry_mode IS DISTINCT FROM 'NEVER_AUTOMATIC'", SQL)
        self.assertIn("task_row.external_approval_id IS NULL", SQL)
        self.assertIn("endpoint_url = 'https://api.deepseek.com/chat/completions'", SQL)
        self.assertIn("model_id = 'deepseek-v4-pro'", SQL)
        self.assertIn("deepseek-case-ledger-extraction-response-v1", SQL)

    def test_response_is_private_hash_bound_and_unknown_has_one_recovery_slot(self):
        self.assertIn("provider_response_id_hash char(64)", SQL)
        self.assertIn("response_sha256 char(64)", SQL)
        self.assertIn("response_object_key text", SQL)
        self.assertIn("case-agent-ledger-extractions/v1", SQL)
        self.assertIn("status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION')", SQL)
        self.assertIn("outcome_sequence smallint NOT NULL CHECK (outcome_sequence IN (1, 2))", SQL)
        self.assertIn("recovered_from_unknown = true", SQL)
        self.assertIn("prior.status = 'UNKNOWN_SUBMISSION'", SQL)

    def test_both_ledgers_are_append_only_force_rls_and_not_public(self):
        for table in (
            "case_agent_ledger_extraction_exchanges",
            "case_agent_ledger_extraction_outcomes",
        ):
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", SQL)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", SQL)
            self.assertIn(f"CREATE TRIGGER {table}_append_only", SQL)


if __name__ == "__main__":
    unittest.main()
