from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LawyerDecisionPackageSourceMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            ROOT
            / "backend/migrations/0072_lawyer_decision_package_document_source.sql"
        ).read_text(encoding="utf-8")

    def test_only_the_verified_decision_package_kind_is_added(self) -> None:
        self.assertIn("VERIFIED_LAWYER_DECISION_PACKAGE", self.sql)
        self.assertIn("MODEL_SUPPLIED_SOURCE", self.sql)
        self.assertIn("rejected IS DISTINCT FROM false", self.sql)
        self.assertNotIn("GRANT INSERT", self.sql)
        self.assertNotIn("GRANT UPDATE", self.sql)

    def test_manifest_shape_and_hash_guards_are_preserved(self) -> None:
        for guard in (
            "jsonb_array_length(value) BETWEEN 1 AND 400",
            "count(DISTINCT entry ->> 'input_ref')",
            "entry ->> 'source_hash' !~ '^[0-9a-f]{64}$'",
            "entry ->> 'text_sha256' !~ '^[0-9a-f]{64}$'",
            "entry ->> 'label' <> btrim(entry ->> 'label')",
        ):
            self.assertIn(guard, self.sql)


if __name__ == "__main__":
    unittest.main()
