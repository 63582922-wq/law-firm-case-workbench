from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0039_case_agent_reviewable_documents.sql"
)


class CaseAgentReviewableDocumentMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_package_is_one_immutable_three_artifact_binding(self) -> None:
        sql = self.sql
        self.assertIn("CREATE TABLE case_agent_reviewable_document_packages", sql)
        for field in (
            "candidate_content_sha256",
            "editable_sha256",
            "review_pdf_sha256",
            "review_input_hash",
            "package_receipt_hash",
            "template_hash",
            "binding_hash",
            "source_set_hash",
            "authorized_source_refs",
            "authorized_source_refs_hash",
            "authorized_source_manifest",
            "candidate_hash",
        ):
            self.assertIn(field, sql)
        self.assertIn("REVIEWABLE_DOCUMENT_CANDIDATE_JSON", sql)
        self.assertIn("REVIEWABLE_DOCUMENT_EDITABLE", sql)
        self.assertIn("REVIEWABLE_DOCUMENT_PDF_PREVIEW", sql)
        self.assertIn("review_status = 'NEEDS_LAWYER_REVIEW'", sql)
        self.assertIn("BEFORE UPDATE OR DELETE", sql)

    def test_package_is_exactly_tenant_task_plan_profile_bound(self) -> None:
        sql = self.sql
        self.assertIn(
            "REFERENCES case_agent_tasks(graph_id, task_id, run_id, firm_id, matter_id)",
            sql,
        )
        self.assertIn(
            "REFERENCES case_agent_task_attempts(\n            attempt_id, graph_id, task_id, run_id, firm_id, matter_id",
            sql,
        )
        self.assertIn(
            "REFERENCES case_work_plan_items(item_id, plan_id, firm_id, matter_id)",
            sql,
        )
        self.assertIn(
            "REFERENCES case_posture_profiles(profile_id, firm_id, matter_id)",
            sql,
        )
        self.assertIn("current_plan_id IS DISTINCT FROM NEW.work_plan_id", sql)
        self.assertIn("current_profile_id IS DISTINCT FROM NEW.posture_profile_id", sql)
        self.assertIn("task_row.input_hash IS DISTINCT FROM NEW.task_input_hash", sql)
        self.assertIn("task_row.active_attempt_id IS DISTINCT FROM NEW.attempt_id", sql)
        self.assertIn("case_agent_document_source_refs_are_canonical", sql)
        self.assertIn("case_agent_document_source_refs_hash", sql)
        self.assertIn("case_agent_document_source_manifest_is_valid", sql)
        self.assertIn("case_agent_document_source_manifest_refs", sql)
        self.assertIn(
            "authorized_source_refs =\n            case_agent_document_source_manifest_refs",
            sql,
        )
        self.assertNotIn(
            "posture-profile:' || NEW.posture_profile_id::text", sql
        )
        self.assertIn(
            "task_row.input_refs IS DISTINCT FROM jsonb_build_array", sql
        )
        self.assertIn(
            "task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash", sql
        )
        self.assertIn(
            "plan_row.actual_profile_hash IS DISTINCT FROM NEW.posture_profile_hash",
            sql,
        )

    def test_private_keys_rls_and_worker_only_insert_are_fail_closed(self) -> None:
        sql = self.sql
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("SELECT current_setting('app.firm_id', true)", sql)
        self.assertIn("current_setting('app.actor_id', true)", sql)
        self.assertIn("role.role = 'SYSTEM_WORKER'", sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", sql)
        self.assertIn("REVOKE ALL ON TABLE", sql)
        self.assertNotIn("download_url", sql.lower())
        self.assertNotIn("presigned", sql.lower())

    def test_staging_never_updates_matter_or_promotes_candidate(self) -> None:
        sql = self.sql.lower()
        self.assertNotIn("update matters", sql)
        self.assertNotIn("insert into audit_events", sql)
        self.assertNotIn("insert into submission", sql)
        self.assertNotIn("'approved'", sql)
        self.assertNotIn("'locked'", sql)
        self.assertNotIn("'submitted'", sql)


if __name__ == "__main__":
    unittest.main()
