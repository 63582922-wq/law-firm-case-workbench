import json
import unittest
from uuid import uuid4

import test_case_agent_ledger_extraction as fixtures
from case_kernel.case_agent_ledger_extraction import (CaseLedgerExtractionCandidate, ExtractionCandidateKind,
    ExtractionSupportingExcerpt, ExtractionDatePrecision, ExtractionTransactionDirection,
    ExtractionTransactionChannel, build_case_ledger_extraction_candidate)
from case_kernel.case_agent_transaction_candidates import (bind_transaction_candidate,
    transaction_candidate_planning_object, bind_fact_candidate, fact_candidate_planning_object,
    read_fact_candidates)
from case_kernel.case_agent_planner import PlanningInputStatus


class TransactionCandidateSourcesTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CaseLedgerExtractionTests()
        fixture.setUp()
        candidate = CaseLedgerExtractionCandidate(kind=ExtractionCandidateKind.TRANSACTION,
            source_refs=(fixture.page.input_ref,), evidence_page_ids=(fixture.page_id,),
            confidence=0.95, conflict_codes=(), risk_codes=(),
            supporting_excerpts=(ExtractionSupportingExcerpt(fixture.page_id, "2020年1月1日转账100元"),),
            local_date="2020-01-01", date_precision=ExtractionDatePrecision.EXACT_DATE,
            amount="100.00", currency="CNY", direction=ExtractionTransactionDirection.OUTGOING,
            payer_label="甲", payee_label="乙", channel=ExtractionTransactionChannel.WECHAT,
            transaction_reference=None)
        raw, source_hash = build_case_ledger_extraction_candidate(task_input_hash="a" * 64,
            source_pages=(fixture.page,), candidates=(candidate,))
        payload = json.loads(raw)["candidates"][0]
        self.row = dict(extraction_candidate_id=str(uuid4()), candidate_hash=payload["candidate_hash"],
            candidate_payload=payload, review_status="NEEDS_LAWYER_REVIEW",
            review_reason_codes=["BELOW_BULK_CONFIDENCE_THRESHOLD"], source_hash=source_hash,
            artifact_content_sha256="b" * 64, page_ids=[fixture.page_id], decisions=[], original_labels=["合成流水.pdf"])

    def test_unconfirmed_status_and_literal_source_survive_analysis_binding(self):
        bound = bind_transaction_candidate(self.row)
        self.assertIn("未确认", bound.primary_text)
        self.assertIn("2020年1月1日转账100元", bound.secondary_text)
        self.assertIn("不得直接计入正式金额", bound.secondary_text)
        obj = transaction_candidate_planning_object(bound)
        self.assertEqual(obj.status, PlanningInputStatus.REVIEW_REQUIRED)
        self.assertTrue(obj.ref_id.startswith("transaction-candidate:"))
        self.assertEqual(obj.content_hash, bound.content_hash)

    def test_lawyer_decision_changes_binding_without_changing_candidate(self):
        before = bind_transaction_candidate(self.row)
        decisions = [{"decision": "DEFER_WITH_REASON", "reason_code": "NEEDS_LEAD_REVIEW",
                      "reason_note": "身份尚未核实", "decision_hash": "c" * 64}]
        after = bind_transaction_candidate({**self.row, "decisions": decisions})
        self.assertNotEqual(before.content_hash, after.content_hash)
        self.assertIn("身份尚未核实", after.secondary_text)
        self.assertEqual(before.primary_text, after.primary_text)

    def test_wrong_page_or_false_confirmation_is_rejected(self):
        for update in ({"page_ids": [str(uuid4())]}, {"review_status": "CONFIRMED"},
                       {"candidate_hash": "0" * 64}, {"source_hash": "bad"}):
            with self.assertRaises(ValueError):
                bind_transaction_candidate({**self.row, **update})

    def test_candidate_body_tampering_is_rejected(self):
        payload = {**self.row["candidate_payload"], "amount": "1000.00"}
        with self.assertRaises(ValueError):
            bind_transaction_candidate({**self.row, "candidate_payload": payload})

    def test_fact_candidate_remains_unconfirmed_and_distinct_from_transaction(self):
        fixture = fixtures.CaseLedgerExtractionTests()
        fixture.setUp()
        candidate = CaseLedgerExtractionCandidate(kind=ExtractionCandidateKind.FACT,
            source_refs=(fixture.page.input_ref,), evidence_page_ids=(fixture.page_id,),
            confidence=0.95, conflict_codes=(), risk_codes=(),
            supporting_excerpts=(ExtractionSupportingExcerpt(fixture.page_id, "账户名称：合成甲"),),
            fact_text="账户名称：合成甲")
        raw, source_hash = build_case_ledger_extraction_candidate(task_input_hash="a" * 64,
            source_pages=(fixture.page,), candidates=(candidate,))
        payload = json.loads(raw)["candidates"][0]
        row = {**self.row, "candidate_payload": payload, "candidate_hash": payload["candidate_hash"],
               "source_hash": source_hash, "page_ids": [fixture.page_id]}
        bound = bind_fact_candidate(row)
        self.assertIn("账户名称：合成甲", bound.primary_text)
        self.assertIn("尚未经律师确认", bound.secondary_text)
        obj = fact_candidate_planning_object(bound)
        self.assertEqual(obj.status, PlanningInputStatus.REVIEW_REQUIRED)
        self.assertTrue(obj.ref_id.startswith("fact-candidate:"))
        with self.assertRaises(ValueError):
            transaction_candidate_planning_object(bound)
        with self.assertRaises(ValueError):
            bind_transaction_candidate(row)

    def test_reader_excludes_promoted_and_submitted_corrections_in_same_matter(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        firm, matter = str(uuid4()), str(uuid4())
        self.assertEqual(read_fact_candidates(connection, firm_id=firm, matter_id=matter), ())
        sql, params = connection.execute.call_args.args
        self.assertEqual(params, (firm, matter, "FACT"))
        self.assertIn("case_agent_ledger_extraction_promotions", sql)
        self.assertIn("fact.correction_candidate_id=c.extraction_candidate_id", sql)
        self.assertIn("fact.firm_id=c.firm_id AND fact.matter_id=c.matter_id", sql)
