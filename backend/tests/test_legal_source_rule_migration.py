from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0006_legal_source_rules.sql"


class LegalSourceRuleMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_source_rule_event_and_bundle_segment_tables_force_tenant_rls(self) -> None:
        for table in (
            "official_legal_source_snapshots",
            "legal_rule_versions",
            "case_legal_events",
            "case_legal_event_evidence_pages",
            "case_legal_fact_bindings",
            "case_legal_bundle_segments",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_formal_source_requires_https_hash_encrypted_object_and_human_verification(self) -> None:
        self.assertIn("official_url ~ '^https://'", self.sql)
        self.assertIn("content_sha256 ~ '^[0-9a-f]{64}$'", self.sql)
        self.assertIn("storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\\.lca$'", self.sql)
        self.assertIn("verification_status IN ('VERIFIED', 'SUPERSEDED')", self.sql)
        self.assertIn("FOREIGN KEY (verified_by, firm_id) REFERENCES users(user_id, firm_id)", self.sql)

    def test_rate_is_derived_by_constrained_formula_and_bundle_segments_are_source_bound(self) -> None:
        self.assertIn("formula_kind IN ('FIXED_ANNUAL_RATE', 'LPR_MULTIPLE', 'NO_INTEREST')", self.sql)
        self.assertIn("derived_annual_rate = base_annual_rate * rate_multiplier", self.sql)
        self.assertIn("parameter_source_snapshot_id uuid", self.sql)
        self.assertIn("parameter_evidence_locator text", self.sql)
        self.assertIn("REFERENCES legal_rule_versions(rule_version_id, firm_id)", self.sql)
        self.assertIn("REFERENCES official_legal_source_snapshots(snapshot_id, firm_id)", self.sql)
        self.assertIn("REFERENCES case_legal_events(legal_event_id, firm_id, matter_id)", self.sql)

    def test_rule_prerequisites_require_human_bound_confirmed_case_facts(self) -> None:
        self.assertIn("CREATE TABLE case_legal_fact_bindings", self.sql)
        self.assertIn("REFERENCES case_facts(fact_id, firm_id, matter_id)", self.sql)
        self.assertIn("WHERE status = 'APPROVED'", self.sql)
        self.assertIn("approval_hash char(64) NOT NULL", self.sql)

    def test_legal_events_reference_normalized_same_matter_evidence_pages(self) -> None:
        self.assertNotIn("evidence_ids jsonb", self.sql)
        self.assertIn("CREATE TABLE case_legal_event_evidence_pages", self.sql)
        self.assertIn(
            "REFERENCES evidence_pages(evidence_page_id, firm_id, matter_id)",
            self.sql,
        )

    def test_lpr_formula_keeps_legal_authority_and_official_rate_parameter_sources_distinct(self) -> None:
        self.assertIn("formula_kind = 'LPR_MULTIPLE'", self.sql)
        self.assertIn("parameter_source_snapshot_id IS NOT NULL", self.sql)
        self.assertIn("FOREIGN KEY (parameter_source_snapshot_id, firm_id)", self.sql)
        self.assertIn("parameter_source_sha256 char(64)", self.sql)


if __name__ == "__main__":
    unittest.main()
