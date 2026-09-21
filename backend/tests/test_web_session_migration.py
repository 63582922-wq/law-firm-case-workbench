from __future__ import annotations

from pathlib import Path
from unittest import TestCase


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0024_web_sessions.sql"


class WebSessionMigrationTests(TestCase):
    def test_session_table_persists_only_fixed_length_hashes_and_server_identity_references(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE web_sessions", sql)
        self.assertIn("session_token_sha256 bytea NOT NULL", sql)
        self.assertIn("csrf_token_sha256 bytea NOT NULL", sql)
        self.assertIn("CHECK (octet_length(session_token_sha256) = 32)", sql)
        self.assertIn("CHECK (octet_length(csrf_token_sha256) = 32)", sql)
        self.assertIn("UNIQUE (session_token_sha256)", sql)
        self.assertIn("FOREIGN KEY (user_id, firm_id) REFERENCES users(user_id, firm_id)", sql)
        self.assertNotIn("session_token text", sql)
        self.assertNotIn("csrf_token text", sql)
        self.assertNotIn("jwt text", sql.lower())
        self.assertNotIn("active_human_roles", sql)

    def test_exact_digest_bootstrap_and_known_tenant_paths_are_both_rls_bound(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertIn("ALTER TABLE web_sessions ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("ALTER TABLE web_sessions FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("web_sessions_read_in_firm_or_exact_digest", sql)
        self.assertIn("app.web_session_token_sha256", sql)
        self.assertIn("^[0-9a-f]{64}$", sql)
        self.assertIn("SET LOCAL app.firm_id", sql)
        self.assertIn("web_sessions_revoke_in_firm_or_exact_session", sql)
        self.assertIn("restrict_web_session_mutation", sql)
        self.assertIn("REVOKE ALL ON TABLE web_sessions FROM PUBLIC", sql)
        self.assertIn("lawcase_web_session_gateway", sql)
        self.assertIn("GRANT REFERENCES (firm_id) ON firms", sql)
        self.assertIn("GRANT REFERENCES (user_id, firm_id) ON users", sql)
        self.assertIn("no\n-- BYPASSRLS privilege", sql)
        self.assertNotIn("ALTER ROLE lawcase_web_session_gateway BYPASSRLS", sql)


if __name__ == "__main__":
    import unittest

    unittest.main()
