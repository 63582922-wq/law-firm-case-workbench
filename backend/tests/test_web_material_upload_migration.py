from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0026_web_material_upload_slots.sql"


class WebMaterialUploadMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_slot_is_bound_to_server_identity_session_matter_and_linear_replay_safe_states(self) -> None:
        self.assertIn("CREATE TABLE web_material_upload_slots", self.sql)
        self.assertIn("actor_id uuid NOT NULL", self.sql)
        self.assertIn("session_id uuid NOT NULL", self.sql)
        self.assertIn("expected_matter_version integer NOT NULL", self.sql)
        self.assertIn("FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)", self.sql)
        self.assertIn("FOREIGN KEY (actor_id, firm_id) REFERENCES users(user_id, firm_id)", self.sql)
        self.assertIn("'RESERVED', 'CLAIMED', 'OBJECT_STORED', 'COMPLETED', 'FAILED'", self.sql)
        self.assertIn("'RECONCILIATION_REQUIRED'", self.sql)
        self.assertIn("enforce_web_material_upload_slot_transition", self.sql)
        self.assertIn("web_material_upload_slots_linear_state", self.sql)
        self.assertIn("status = 'RESERVED' AND NEW.status = 'CLAIMED'", self.sql)
        self.assertIn("OLD.status = 'OBJECT_STORED' AND NEW.status IN", self.sql)
        self.assertIn("expires_at > created_at", self.sql)
        self.assertNotIn("REFERENCES web_sessions", self.sql)

    def test_private_object_hand_off_is_recovery_only_and_completion_must_match_atomic_evidence_binding(self) -> None:
        self.assertIn("source_object_key text", self.sql)
        self.assertIn("source_object_version_id text", self.sql)
        self.assertIn("source_reference_hash char(64)", self.sql)
        self.assertIn("source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')", self.sql)
        self.assertIn("source_object_key = NEW.source_object_key", self.sql)
        self.assertIn("JOIN web_evidence_original_source_objects binding", self.sql)
        self.assertIn("completed Web material upload must match its immutable evidence object binding", self.sql)
        self.assertIn("RECONCILIATION_REQUIRED; it must never be deleted", self.sql)
        self.assertIn("never be selected into an HTTP", self.sql)
        self.assertIn("response, snapshot, audit payload, outbox payload", self.sql)
        events = self.sql[self.sql.index("CREATE TABLE web_material_upload_slot_events") :]
        self.assertNotIn("source_object_key", events)
        self.assertNotIn("source_object_version_id", events)

    def test_slots_and_lifecycle_events_are_force_rls_append_only_and_not_public(self) -> None:
        self.assertIn("CREATE TABLE web_material_upload_slot_events", self.sql)
        self.assertIn("web_material_upload_slot_events_append_only", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE ON web_material_upload_slot_events", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("web_material_upload_slots_firm_isolation", self.sql)
        self.assertIn("web_material_upload_slot_events_firm_isolation", self.sql)
        self.assertIn("REVOKE ALL ON TABLE web_material_upload_slots FROM PUBLIC", self.sql)
        self.assertIn("REVOKE ALL ON TABLE web_material_upload_slot_events FROM PUBLIC", self.sql)
        self.assertNotIn("GRANT ALL ON TABLE web_material_upload_slots TO PUBLIC", self.sql)
        self.assertNotIn("CREATE POLICY web_material_upload_slots_public", self.sql)


if __name__ == "__main__":
    unittest.main()
