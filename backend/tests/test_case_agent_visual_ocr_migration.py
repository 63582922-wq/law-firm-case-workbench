from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "migrations" / "0038_case_agent_visual_ocr.sql"
        ).read_text(encoding="utf-8")

    def test_0038_reuses_control_plane_and_adds_visual_source_and_exchange_only(self):
        self.assertIn("CREATE TABLE web_evidence_native_image_source_objects", self.sql)
        self.assertIn("CREATE TABLE case_agent_visual_ocr_exchanges", self.sql)
        self.assertIn("CREATE TABLE case_agent_visual_ocr_outcomes", self.sql)
        self.assertIn("image/jpeg", self.sql)
        self.assertIn("image/png", self.sql)
        self.assertNotIn("CREATE TABLE case_agent_task_attempts", self.sql)
        self.assertNotIn("CREATE TABLE case_agent_external_submissions", self.sql)
        self.assertNotIn("0039", self.sql)

    def test_source_key_is_private_tenant_content_addressed_and_hash_bound(self):
        self.assertIn("^original-images/v1/", self.sql)
        self.assertIn("split_part(source_object_key, '/', 3) = firm_id::text", self.sql)
        self.assertIn("split_part(source_object_key, '/', 4) = matter_id::text", self.sql)
        self.assertIn("source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')", self.sql)
        self.assertNotRegex(self.sql, r"https?://")

    def test_only_one_page_original_can_bind_and_history_is_append_only(self):
        self.assertIn("source.page_count = 1", self.sql)
        self.assertIn("page.page_number = 1", self.sql)
        self.assertIn("HAVING count(*) = 1", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)

    def test_rls_is_forced_and_public_access_revoked(self):
        for table in (
            "web_evidence_native_image_source_objects",
            "case_agent_visual_ocr_exchanges",
            "case_agent_visual_ocr_outcomes",
        ):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", self.sql)
        self.assertIn("current_setting('app.firm_id', true)", self.sql)

    def test_network_crossing_requires_existing_started_boundary_and_no_resend(self):
        self.assertIn("case_agent_external_submissions", self.sql)
        self.assertIn("submission_row.submission_state IS DISTINCT FROM 'STARTED'", self.sql)
        self.assertIn("task_row.autonomy_level IS DISTINCT FROM 'A3_LAWYER_APPROVAL'", self.sql)
        self.assertIn("task_row.approval_gate IS DISTINCT FROM 'LAWYER_REVIEW'", self.sql)
        self.assertIn("task_row.skill_id IS DISTINCT FROM 'image_visual_ocr'", self.sql)
        self.assertIn("task_row.adapter_version IS DISTINCT FROM '1.0.0'", self.sql)
        self.assertRegex(
            self.sql,
            r"SELECT task[.]skill_id,[\s\S]{0,500}task[.]input_hash,",
        )
        self.assertIn(
            "task_row.attempt_input_hash IS DISTINCT FROM task_row.input_hash",
            self.sql,
        )
        self.assertIn("task_row.external_approval_id IS NULL", self.sql)
        self.assertIn("task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash", self.sql)
        self.assertIn("task_row.resource_budget->>'max_attempts'", self.sql)
        self.assertIn("attempt_id uuid NOT NULL UNIQUE", self.sql)
        self.assertIn("external_request_id uuid NOT NULL UNIQUE", self.sql)
        self.assertIn("UNKNOWN_SUBMISSION", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)

    def test_exchange_and_outcome_require_the_exact_dedicated_worker(self):
        self.assertIn(
            "submission_row.recorded_by IS DISTINCT FROM NEW.started_by_worker",
            self.sql,
        )
        self.assertGreaterEqual(
            self.sql.count(
                "NULLIF(current_setting('app.actor_id', true), '')::uuid"
            ),
            2,
        )
        self.assertGreaterEqual(self.sql.count("IS DISTINCT FROM NEW."), 2)
        self.assertIn("role.role = 'SYSTEM_WORKER'", self.sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", self.sql)
        self.assertIn("principal.status = 'ACTIVE'", self.sql)

    def test_exact_response_is_hash_bound_and_private(self):
        self.assertIn("response_body bytea", self.sql)
        self.assertIn("octet_length(response_body) = response_bytes", self.sql)
        self.assertIn("digest(response_body, 'sha256')", self.sql)
        self.assertNotIn("api_key", self.sql.lower())


if __name__ == "__main__":
    unittest.main()
