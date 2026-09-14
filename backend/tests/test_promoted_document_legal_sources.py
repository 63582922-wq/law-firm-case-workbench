import unittest
from case_kernel.case_agent_document_delivery_postgres import (
    _current_promoted_source_identity, CaseAgentDocumentPackageBlocked,
)


class PromotedDocumentLegalSourcesTests(unittest.TestCase):
    def test_claim_and_legal_sources_require_current_governed_identity(self):
        for kind, use, prefix in (("CASE_CLAIM", "CLAIM_SCOPE", "claim"),
                                  ("VERIFIED_LEGAL_SOURCE", "LEGAL_AUTHORITY", "legal-source"),
                                  ("APPROVED_LEGAL_RULE", "LEGAL_RULE", "legal-rule")):
            with self.subTest(kind=kind):
                binding = dict(object_type=kind, object_id="source", object_version="v1",
                               source_status="CONFIRMED" if kind == "CASE_CLAIM" else "LOCKED",
                               reference_use=use, snapshot_matter_version=1, content_hash="a" * 64)
                class Connection:
                    missing = False
                    def execute(self, sql, params):
                        if "FROM case_agent_work_plan_input_bindings" in sql:
                            row = binding
                        else:
                            row = None if self.missing else dict(confirmation_hash="a" * 64,
                                source_hash="a" * 64, source_version="v1")
                        class Cursor:
                            def fetchone(self): return row
                        return Cursor()
                connection = Connection()
                args = dict(firm_id="firm", matter_id="matter", work_plan_id="plan",
                            reference=dict(source_id="binding", source_version="v1",
                                           source_hash="b" * 64, reference_use=use))
                identity = _current_promoted_source_identity(connection, **args)
                self.assertEqual(identity[0], f"{prefix}:source")
                self.assertEqual(identity[1][2], "a" * 64)
                connection.missing = True
                with self.assertRaises(CaseAgentDocumentPackageBlocked):
                    _current_promoted_source_identity(connection, **args)
                connection.missing = False
                binding["source_status"] = "REVIEW_REQUIRED"
                with self.assertRaises(CaseAgentDocumentPackageBlocked):
                    _current_promoted_source_identity(connection, **args)
