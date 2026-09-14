from pathlib import Path
import unittest

from case_kernel.qwen_visual_ocr_adapter import QWEN_VISUAL_OCR_POLICY_HASH


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrPolicyHashGuardMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT
            / "migrations"
            / "0083_visual_ocr_cost_reservation.sql"
        ).read_text(encoding="utf-8")

    def test_database_guard_uses_the_runtime_policy_hash(self):
        self.assertIn("resource_budget->>'max_cost_minor_units'", self.sql)
        self.assertIn(QWEN_VISUAL_OCR_POLICY_HASH, self.sql)
        self.assertNotIn(
            "9cd89e64af567a9b1396ca723e934b2b7b2889fd2377227260766136dcab7305",
            self.sql,
        )

    def test_guard_remains_exact_current_approved_task_only(self):
        for value in (
            "task_row.attempt_status IS DISTINCT FROM 'RUNNING'",
            "task_row.external_approval_id IS NULL",
            "task_row.current_graph_hash IS DISTINCT FROM task_row.graph_hash",
            "submission_row.submission_state IS DISTINCT FROM 'STARTED'",
            "role.role = 'SYSTEM_WORKER'",
        ):
            self.assertIn(value, self.sql)

    def test_function_replacement_is_atomic(self):
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("CREATE OR REPLACE FUNCTION", self.sql)
        self.assertIn("COMMIT;", self.sql)


if __name__ == "__main__":
    unittest.main()
