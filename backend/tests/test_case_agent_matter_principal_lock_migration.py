from __future__ import annotations

from pathlib import Path
import unittest


class CaseAgentMatterPrincipalLockPrivilegeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.migration_sql = (
            root / "migrations" / "0054_case_agent_matter_principal_lock_privilege.sql"
        ).read_text(encoding="utf-8")
        cls.hardening_sql = (
            root.parent
            / "deployment"
            / "local-managed-test"
            / "postgres"
            / "post-migrate-hardening.sql"
        ).read_text(encoding="utf-8")

    def test_web_gets_only_the_user_identity_row_lock_entitlement(self) -> None:
        grant = "GRANT UPDATE (user_id) ON TABLE public.users TO lawcase_web_application;"
        self.assertIn(grant, self.migration_sql)
        self.assertNotIn("GRANT UPDATE ON TABLE public.users", self.migration_sql)
        self.assertIn("FORCE RLS", self.migration_sql)

    def test_managed_hardening_restores_exact_grant_after_broad_write_revoke(self) -> None:
        revoke = self.hardening_sql.index(
            "REVOKE INSERT, UPDATE, DELETE ON TABLE public.firms, public.users"
        )
        grant = self.hardening_sql.index(
            "GRANT UPDATE (user_id) ON TABLE public.users TO lawcase_web_application;"
        )
        self.assertLess(revoke, grant)
        self.assertNotIn("GRANT UPDATE ON TABLE public.users", self.hardening_sql)


if __name__ == "__main__":
    unittest.main()
