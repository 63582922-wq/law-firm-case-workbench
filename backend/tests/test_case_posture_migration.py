from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0029_case_posture_profiles.sql"
)


class CasePostureMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_separates_party_proceeding_position_engagement_and_profile(self) -> None:
        tables = (
            "case_parties",
            "case_party_versions",
            "case_party_heads",
            "court_proceedings",
            "court_proceeding_versions",
            "court_proceeding_heads",
            "court_party_positions",
            "court_party_position_versions",
            "court_party_position_heads",
            "firm_engagements",
            "firm_engagement_versions",
            "firm_engagement_heads",
            "case_posture_profiles",
            "case_posture_profile_heads",
            "case_posture_profile_events",
        )
        for table in tables:
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_confirmed_meanings_are_append_only_and_heads_are_guarded(self) -> None:
        for table in (
            "case_parties",
            "case_party_versions",
            "court_proceedings",
            "court_proceeding_versions",
            "court_party_positions",
            "court_party_position_versions",
            "firm_engagements",
            "firm_engagement_versions",
            "case_posture_profiles",
            "case_posture_profile_events",
        ):
            self.assertIn(f"{table}_append_only BEFORE UPDATE OR DELETE", self.sql)
        for table in (
            "case_party_heads",
            "court_proceeding_heads",
            "court_party_position_heads",
            "firm_engagement_heads",
            "case_posture_profile_heads",
        ):
            self.assertIn(f"{table}_guard BEFORE INSERT OR UPDATE OR DELETE", self.sql)
        self.assertIn("version head cannot be deleted", self.sql)
        self.assertIn("profile head cannot be deleted", self.sql)
        for constraint in (
            "case_parties_head_same_party",
            "court_proceedings_head_same_proceeding",
            "court_party_positions_head_same_position",
            "firm_engagements_head_same_engagement",
        ):
            self.assertIn(constraint, self.sql)
        self.assertGreaterEqual(self.sql.count("DEFERRABLE INITIALLY DEFERRED"), 4)

    def test_profile_binds_exact_current_upstream_versions_and_server_hash(self) -> None:
        for column in (
            "represented_party_version_id",
            "proceeding_version_id",
            "position_version_id",
            "engagement_version_id",
            "profile_version",
            "profile_hash",
            "supersedes_profile_id",
        ):
            self.assertIn(column, self.sql)
        self.assertIn("case posture profile must bind the exact current compatible upstream versions", self.sql)
        self.assertIn("case-posture-profile-v1", self.sql)
        self.assertIn("digest(concat_ws", self.sql)
        self.assertIn("case posture profile hash does not bind its exact server inputs", self.sql)
        self.assertIn("case posture profile event cause is not bound to the profile lineage", self.sql)

    def test_upstream_changes_make_current_profile_stale_without_rewriting_it(self) -> None:
        for cause in (
            "PARTY_VERSION_CHANGED",
            "PROCEEDING_VERSION_CHANGED",
            "POSITION_VERSION_CHANGED",
            "ENGAGEMENT_VERSION_CHANGED",
        ):
            self.assertIn(cause, self.sql)
        self.assertIn("'STALE', cause", self.sql)
        self.assertIn("SET current_profile_id = NULL", self.sql)
        self.assertNotIn("UPDATE case_posture_profiles\n", self.sql)

    def test_schema_contains_no_fixed_material_or_deliverable_mapping(self) -> None:
        lowered = self.sql.lower()
        self.assertNotIn("required_material", lowered)
        self.assertNotIn("deliverable_template", lowered)
        self.assertNotIn("defence_template", lowered)
        self.assertNotIn("complaint_template", lowered)


if __name__ == "__main__":
    unittest.main()
