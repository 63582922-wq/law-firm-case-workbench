from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0003_evidence_manifest.sql"


class EvidenceMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_every_evidence_table_is_tenant_scoped_and_forces_rls(self) -> None:
        tables = (
            "evidence_original_files",
            "evidence_pages",
            "evidence_page_decisions",
            "evidence_page_annotations",
            "evidence_page_duplicate_groups",
            "evidence_page_duplicate_members",
            "evidence_manifests",
            "evidence_manifest_pages",
            "evidence_manifest_page_annotations",
            "evidence_derivative_artifacts",
        )
        for table in tables:
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_originals_are_append_only_and_page_review_is_explicit(self) -> None:
        self.assertIn("evidence_original_files_append_only", self.sql)
        self.assertIn("evidence_pages_append_only", self.sql)
        self.assertIn("'INCLUDE', 'EXCLUDE'", self.sql)
        self.assertIn("evidence_page_decisions_one_approved_per_page", self.sql)
        self.assertIn("evidence_manifests_one_locked_per_matter", self.sql)
        self.assertIn("included_pages + excluded_pages = total_pages", self.sql)

    def test_duplicate_annotations_and_derivative_lineage_are_normalized(self) -> None:
        self.assertIn("'SAME_SOURCE_PAGE', 'DISTINCT_PAGES'", self.sql)
        self.assertIn("canonical_page_id", self.sql)
        self.assertIn("x0 >= 0 AND x0 < x1 AND x1 <= 1", self.sql)
        self.assertIn("REFERENCES evidence_manifests(manifest_id, firm_id, matter_id)", self.sql)
        self.assertIn("'RELATED_PAGES_PDF', 'ANNOTATED_RELATED_PAGES_PDF'", self.sql)

    def test_manifest_page_scope_and_derivative_state_cannot_be_bypassed(self) -> None:
        self.assertIn(
            "UNIQUE (manifest_id, evidence_page_id, firm_id, matter_id)",
            self.sql,
        )
        self.assertIn(
            "FOREIGN KEY (manifest_id, evidence_page_id, firm_id, matter_id)",
            self.sql,
        )
        self.assertIn(
            "disposition = 'INCLUDE' AND derivative_sequence IS NOT NULL",
            self.sql,
        )
        self.assertIn("status IN ('STALE', 'REVOKED')", self.sql)
        self.assertIn("invalidated_at IS NOT NULL", self.sql)
        self.assertIn("length(trim(invalidation_reason)) > 0", self.sql)


if __name__ == "__main__":
    unittest.main()
