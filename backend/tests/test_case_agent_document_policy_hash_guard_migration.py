from pathlib import Path
import unittest

from case_kernel.case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
)


MIGRATION_0069 = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0069_document_draft_policy_hash_guard.sql"
)
MIGRATION_0070 = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0070_local_case_review_memo_policy_hash.sql"
)


class CaseAgentDocumentPolicyHashGuardMigrationTests(unittest.TestCase):
    def test_database_guard_migrations_reach_current_runtime_manifests(self) -> None:
        initial = MIGRATION_0069.read_text(encoding="utf-8")
        sql = MIGRATION_0070.read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE FUNCTION", initial)
        self.assertIn(
            DOCX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash,
            sql,
        )
        self.assertIn(
            XLSX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash,
            initial,
        )
        self.assertIn("pg_get_functiondef", sql)
        self.assertIn("NETWORK_CONNECTOR", sql)
        self.assertIn("EXACT_ALLOWLIST", sql)
        self.assertIn(
            "document draft task input is not one work-plan item", initial
        )


if __name__ == "__main__":
    unittest.main()
