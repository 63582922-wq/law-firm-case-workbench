from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0007_submission_compilation.sql"
CORE_MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_core.sql"


class SubmissionCompilationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.core_sql = CORE_MIGRATION.read_text(encoding="utf-8")

    def test_core_bundle_has_exact_tenant_composite_key_for_compilation_fk(self) -> None:
        self.assertIn(
            "UNIQUE (bundle_id, firm_id, matter_id)",
            self.core_sql,
        )

    def test_all_submission_compilation_tables_force_tenant_rls(self) -> None:
        for table in (
            "submission_work_products",
            "submission_compilation_specs",
            "submission_bundle_components",
            "submission_compilation_exports",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_compilation_is_bound_to_same_matter_evidence_law_calculation_and_final_text(self) -> None:
        self.assertIn("REFERENCES evidence_manifests(manifest_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES case_legal_bundles(bundle_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES calculation_runs(run_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES approvals(approval_id, firm_id, matter_id)", self.sql)
        self.assertIn("currency char(3) NOT NULL CHECK (currency = 'CNY')", self.sql)

    def test_court_files_are_approved_content_addressed_pdf_components(self) -> None:
        self.assertIn("audience IN ('COURT_SUBMISSION', 'INTERNAL_ONLY')", self.sql)
        self.assertIn("media_type text NOT NULL CHECK (media_type = 'application/pdf')", self.sql)
        self.assertIn("storage_object_key = substring(artifact_sha256", self.sql)
        self.assertIn("UNIQUE (bundle_id, court_filename)", self.sql)
        self.assertIn("最新|最终|修订|终稿|定稿", self.sql)

    def test_zip_and_internal_manifest_are_separate_verified_encrypted_objects(self) -> None:
        self.assertIn("court_zip_object_key", self.sql)
        self.assertIn("internal_manifest_object_key", self.sql)
        self.assertIn("verification_hash", self.sql)
        self.assertIn("UNIQUE (bundle_id)", self.sql)

    def test_locked_spec_components_and_export_are_append_only(self) -> None:
        self.assertIn("CREATE FUNCTION prohibit_submission_compilation_mutation", self.sql)
        self.assertIn("submission_compilation_specs_append_only", self.sql)
        self.assertIn("submission_bundle_components_append_only", self.sql)
        self.assertIn("submission_compilation_exports_append_only", self.sql)


if __name__ == "__main__":
    unittest.main()
