from __future__ import annotations

from pathlib import Path
from unittest import TestCase


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0023_web_oidc_identity_directory.sql"
)


class WebOidcIdentityDirectoryMigrationTests(TestCase):
    def test_global_directory_is_single_mapping_with_explicit_human_roles(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE web_oidc_identities", sql)
        self.assertIn("UNIQUE (issuer, subject)", sql)
        self.assertIn("FOREIGN KEY (user_id, firm_id) REFERENCES users(user_id, firm_id)", sql)
        self.assertIn("active_human_roles text[] NOT NULL", sql)
        self.assertIn("cardinality(active_human_roles) BETWEEN 1 AND 5", sql)
        self.assertIn("'FIRM_ADMIN'", sql)
        self.assertNotIn("'SYSTEM_WORKER'", sql)

    def test_directory_role_is_read_only_and_tenant_reads_remain_rls_scoped(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("not a client-addressable", sql)
        self.assertIn("lawcase_identity_directory", sql)
        self.assertIn("REVOKE ALL ON TABLE web_oidc_identities FROM PUBLIC", sql)
        self.assertIn("GRANT SELECT (issuer, subject, firm_id, user_id, is_active, active_human_roles)", sql)
        self.assertIn("GRANT SELECT (user_id, firm_id, status)", sql)
        self.assertIn("FORCE RLS", sql)
        self.assertIn("SET LOCAL app.firm_id", sql)
        self.assertNotIn("INSERT INTO web_oidc_identities", sql)
        self.assertNotIn("UPDATE web_oidc_identities", sql)
        self.assertNotIn("DELETE FROM web_oidc_identities", sql)
