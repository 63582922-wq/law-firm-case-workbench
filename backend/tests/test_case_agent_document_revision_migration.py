from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0073_deterministic_document_package_revisions.sql"
)


class CaseAgentDocumentRevisionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_revision_chain_is_append_only_and_linear(self) -> None:
        for fragment in (
            "DETERMINISTIC_TEMPLATE_REVISION",
            "UNIQUE (revision_request_id)",
            "predecessor.revision_number + 1",
            "successor.supersedes_package_id = predecessor.package_id",
            "successor_receipt.outcome = 'PASSED'",
            "prior_receipt.outcome = 'PASSED'",
        ):
            self.assertIn(fragment, self.sql)
        self.assertNotIn("UNIQUE (supersedes_package_id)", self.sql)
        self.assertNotIn("UNIQUE (root_package_id, revision_number)", self.sql)
        self.assertNotIn("UPDATE case_agent_reviewable_document_packages", self.sql)
        self.assertNotIn("DELETE FROM case_agent_reviewable_document_packages", self.sql)

    def test_only_current_verified_agent_output_can_enter_revision_inbox(self) -> None:
        for fragment in (
            "receipt.outcome = 'PASSED'",
            "run.status = 'READY_FOR_REVIEW'",
            "predecessor.current_graph_id IS DISTINCT FROM predecessor.graph_id",
            "predecessor.package_receipt_hash <> NEW.source_package_receipt_hash",
            "predecessor.template_hash = NEW.target_template_hash",
            "predecessor.revision_number <> NEW.expected_revision_number",
        ):
            self.assertIn(fragment, self.sql)

    def test_execution_and_independent_verification_remain_distinct(self) -> None:
        for fragment in (
            "external_calls integer NOT NULL DEFAULT 0 CHECK (external_calls = 0)",
            "verified_by <> executed_by",
            "successor.package_receipt_hash <> NEW.successor_package_receipt_hash",
            "GRANT INSERT ON case_agent_document_revision_receipts\n    TO lawcase_agent_worker",
            "GRANT INSERT, SELECT ON case_agent_document_revision_receipts\n    TO lawcase_agent_verifier",
        ):
            self.assertIn(fragment, self.sql)

    def test_all_revision_tables_force_rls_and_revoke_public_access(self) -> None:
        for table in (
            "case_agent_document_revision_requests",
            "case_agent_document_revision_inbox",
            "case_agent_document_revision_receipts",
        ):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", self.sql)


if __name__ == "__main__":
    unittest.main()
