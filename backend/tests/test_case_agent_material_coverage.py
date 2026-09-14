import unittest
from uuid import uuid4
from case_kernel.case_agent_material_coverage import read_material_extraction_coverage


class Rows:
    def __init__(self, rows):
        self.rows = rows
    def execute(self, sql, args):
        self.sql, self.args = sql, args
        return self
    def fetchall(self):
        return self.rows


class CoverageTests(unittest.TestCase):
    def test_pending_pages_preserve_original_identity_even_for_equal_content(self):
        files = [str(uuid4()), str(uuid4())]
        pages = [str(uuid4()), str(uuid4())]
        connection = Rows([dict(evidence_file_id=files[n], original_file_sha256="a"*64,
            evidence_page_id=pages[n], page_number=1, extracted=n==0) for n in range(2)])
        result = read_material_extraction_coverage(connection, firm_id=str(uuid4()), matter_id=str(uuid4()))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].pending_page_ids, ())
        self.assertEqual(result[1].pending_page_ids, (pages[1],))
        self.assertIn("attempt.status='SUCCEEDED'", connection.sql)
        self.assertIn("verification.outcome='PASSED'", connection.sql)
        self.assertIn("task.tool_id='extract_case_ledger'", connection.sql)
        self.assertIn("run.is_stale = false", connection.sql)
        self.assertIn("run.is_cancelled = false", connection.sql)
        self.assertIn("case_agent_ledger_extraction_batch_confirmations", connection.sql)
        self.assertIn("batch.task_id = task.task_id", connection.sql)
        self.assertNotIn("run.status", connection.sql)

    def test_duplicate_page_rows_cannot_inflate_coverage(self):
        row = dict(evidence_file_id=str(uuid4()), original_file_sha256="a"*64,
            evidence_page_id=str(uuid4()), page_number=1, extracted=True)
        with self.assertRaises(ValueError):
            read_material_extraction_coverage(Rows([row,row]), firm_id=str(uuid4()), matter_id=str(uuid4()))

    def test_unknown_success_value_is_not_treated_as_coverage(self):
        row = dict(evidence_file_id=str(uuid4()), original_file_sha256="a"*64,
            evidence_page_id=str(uuid4()), page_number=1, extracted="true")
        with self.assertRaises(ValueError):
            read_material_extraction_coverage(Rows([row]), firm_id=str(uuid4()), matter_id=str(uuid4()))
