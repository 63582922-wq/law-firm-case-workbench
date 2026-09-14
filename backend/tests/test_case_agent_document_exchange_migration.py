from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0040_case_agent_document_draft_exchange.sql"
)


class CaseAgentDocumentExchangeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_exchange_binds_exact_0031_run_task_attempt_and_hashes(self) -> None:
        sql = self.sql
        self.assertIn("CREATE TABLE case_agent_document_draft_exchanges", sql)
        for field in (
            "run_id", "graph_id", "task_id", "attempt_id",
            "external_request_id", "submission_record_id", "request_hash",
            "binding_hash", "source_set_hash", "started_by_worker",
        ):
            self.assertIn(field, sql)
        self.assertIn("case_agent_external_submissions", sql)
        self.assertIn("submission_row.submission_state IS DISTINCT FROM 'STARTED'", sql)
        self.assertIn("attempt_id uuid NOT NULL UNIQUE", sql)
        self.assertIn("external_request_id uuid NOT NULL UNIQUE", sql)
        self.assertIn("UNIQUE (run_id, task_id)", sql)

    def test_outcome_is_append_only_and_unknown_can_only_be_reconciled(self) -> None:
        sql = self.sql
        self.assertIn("CREATE TABLE case_agent_document_draft_outcomes", sql)
        self.assertIn("UNKNOWN_SUBMISSION", sql)
        self.assertIn("outcome_sequence IN (1, 2)", sql)
        self.assertIn("prior.status = 'UNKNOWN_SUBMISSION'", sql)
        self.assertIn("recovered_from_unknown", sql)
        self.assertGreaterEqual(sql.count("BEFORE UPDATE OR DELETE"), 2)

    def test_raw_provider_body_is_private_object_only(self) -> None:
        sql = self.sql
        self.assertIn("response_object_key text", sql)
        self.assertIn("response_sha256", sql)
        self.assertIn("response_bytes", sql)
        self.assertNotIn("response_body bytea", sql)
        self.assertNotIn("api_key", sql.lower())
        self.assertNotIn("prompt text", sql.lower())
        self.assertNotIn("download_url", sql.lower())

    def test_tenant_rls_is_forced_and_worker_identity_is_exact(self) -> None:
        sql = self.sql
        for table in (
            "case_agent_document_draft_exchanges",
            "case_agent_document_draft_outcomes",
        ):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", sql)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", sql)
        self.assertIn("current_setting('app.firm_id', true)", sql)
        self.assertGreaterEqual(
            sql.count("current_setting('app.actor_id', true)"), 2
        )
        self.assertIn("role.role = 'SYSTEM_WORKER'", sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", sql)

    def test_dynamic_docx_and_xlsx_capabilities_are_both_exact(self) -> None:
        sql = self.sql
        self.assertIn("dynamic_document_delivery", sql)
        self.assertIn("draft_reviewable_docx_package", sql)
        self.assertIn("dynamic-reviewable-docx-delivery", sql)
        self.assertIn("dynamic_spreadsheet_delivery", sql)
        self.assertIn("draft_reviewable_xlsx_package", sql)
        self.assertIn("dynamic-reviewable-xlsx-delivery", sql)
        self.assertIn("NEVER_AUTOMATIC", sql)
        self.assertIn("max_external_calls", sql)
        self.assertIn("max_attempts", sql)


if __name__ == "__main__":
    unittest.main()
