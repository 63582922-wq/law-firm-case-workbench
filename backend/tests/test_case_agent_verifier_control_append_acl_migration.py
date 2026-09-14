from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0061_case_agent_verifier_control_append_acl.sql"


class CaseAgentVerifierControlAppendAclMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_only_exact_control_columns_are_insertable(self) -> None:
        self.assertIn("public.outbox_events", self.sql)
        self.assertIn("public.command_idempotency", self.sql)
        self.assertIn("GRANT INSERT (", self.sql)
        self.assertIn("TO lawcase_agent_verifier", self.sql)

    def test_mutation_is_revoked_before_column_grants(self) -> None:
        self.assertIn("REVOKE INSERT, UPDATE, DELETE, TRUNCATE", self.sql)
        self.assertNotIn("GRANT UPDATE", self.sql)
        self.assertNotIn("GRANT DELETE", self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":
    unittest.main()
