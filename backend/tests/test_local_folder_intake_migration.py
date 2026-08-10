from pathlib import Path
import unittest


class LocalFolderIntakeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1] / "migrations" / "0010_local_folder_intake.sql"
        ).read_text(encoding="utf-8")

    def test_scan_and_file_tables_are_tenant_scoped_and_rls_forced(self) -> None:
        for table in ("local_folder_scans", "local_folder_scan_files"):
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE {table}", self.sql)
                self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_absolute_paths_and_grants_are_not_persisted(self) -> None:
        lowered = self.sql.lower()
        self.assertNotIn("selected_root", lowered)
        self.assertNotIn("absolute_path", lowered)
        self.assertNotIn("folder_grant_id", lowered)
        self.assertIn("relative_path", lowered)
        self.assertIn("manifest_hash", lowered)

    def test_only_one_candidate_and_one_approved_scope_can_be_current(self) -> None:
        self.assertIn("local_folder_scans_one_candidate_per_matter", self.sql)
        self.assertIn("local_folder_scans_one_approved_per_matter", self.sql)
        self.assertIn("local_folder_scan_files_append_only", self.sql)
        self.assertIn("local_folder_scans_no_delete", self.sql)


if __name__ == "__main__":
    unittest.main()
