from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0060_case_agent_verifier_snapshot_authority.sql"


class CaseAgentVerifierSnapshotAuthorityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_authority_is_one_security_definer_lock_not_direct_update(self) -> None:
        self.assertIn(
            "CREATE FUNCTION public.authorize_case_agent_verification_snapshot",
            self.sql,
        )
        self.assertIn("SECURITY DEFINER", self.sql)
        self.assertIn("SET search_path = pg_catalog, public", self.sql)
        self.assertIn("FOR UPDATE", self.sql)
        self.assertNotIn("GRANT UPDATE", self.sql)

    def test_authority_binds_transaction_identity_and_dedicated_role(self) -> None:
        self.assertIn("current_setting('app.firm_id', true)", self.sql)
        self.assertIn("current_setting('app.actor_id', true)", self.sql)
        self.assertIn("role.role = 'SYSTEM_WORKER'", self.sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", self.sql)
        self.assertIn("principal.status = 'ACTIVE'", self.sql)

    def test_only_verifier_receives_execute(self) -> None:
        self.assertIn("FROM PUBLIC, lawcase_web_application, lawcase_agent_worker", self.sql)
        self.assertIn("TO lawcase_agent_verifier", self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":
    unittest.main()
