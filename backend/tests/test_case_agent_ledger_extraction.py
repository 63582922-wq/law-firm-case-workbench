from __future__ import annotations

import json
import unittest
from hashlib import sha256
from uuid import uuid4

from case_kernel.case_agent_ledger_extraction import (
    CASE_LEDGER_EXTRACTION_SCHEMA,
    CaseLedgerExtractionBlocked,
    CaseLedgerExtractionCandidate,
    CaseLedgerExtractionSourcePage,
    ExtractionCandidateKind,
    ExtractionConflictCode,
    ExtractionDatePrecision,
    ExtractionRiskCode,
    ExtractionSourceMode,
    ExtractionSupportingExcerpt,
    ExtractionTransactionChannel,
    ExtractionTransactionDirection,
    build_case_ledger_extraction_candidate,
    build_fact_correction_proposal,
    extraction_candidate_is_eligible,
    extraction_source_refs,
    parse_case_ledger_extraction_candidate,
)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class CaseLedgerExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page_id = str(uuid4())
        self.page = CaseLedgerExtractionSourcePage(
            input_ref=f"evidence-page:{self.page_id}",
            evidence_page_id=self.page_id,
            source_file_sha256=digest("source-file"),
            page_number=1,
            source_text_sha256=digest("2020年1月1日转账100元"),
            source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )

    def _fact(self, **changes):
        values = {
            "kind": ExtractionCandidateKind.FACT,
            "source_refs": (self.page.input_ref,),
            "evidence_page_ids": (self.page_id,),
            "confidence": 0.99,
            "conflict_codes": (),
            "risk_codes": (),
            "supporting_excerpts": (
                ExtractionSupportingExcerpt(self.page_id, "2020年1月1日转账100元"),
            ),
            "fact_text": "[合成] 付款页面记载一笔转账。",
        }
        values.update(changes)
        return CaseLedgerExtractionCandidate(**values)

    def test_build_parse_and_eligible_native_fact(self) -> None:
        content, source_hash = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(self.page,),
            candidates=(self._fact(),),
        )
        parsed = parse_case_ledger_extraction_candidate(content)
        self.assertEqual(parsed["schema_version"], CASE_LEDGER_EXTRACTION_SCHEMA)
        self.assertEqual(parsed["source_hash"], source_hash)
        self.assertEqual(extraction_source_refs(parsed), {self.page.input_ref})
        self.assertTrue(extraction_candidate_is_eligible(parsed, parsed["candidates"][0]))

    def test_fact_correction_preserves_original_and_does_not_inherit_approval(self):
        raw, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(self.page,),
            candidates=(self._fact(risk_codes=(ExtractionRiskCode.LOW_CONFIDENCE,)),),
        )
        original = json.loads(raw)["candidates"][0]
        kwargs = dict(expected_artifact_hash=sha256(raw).hexdigest(),
                      candidate_hash=original["candidate_hash"],
                      revised_text="材料记载付款，实际履行仍需核对。", reason="保留材料归属，不作履行认定。")
        proposal = build_fact_correction_proposal(raw, **kwargs)
        self.assertEqual(proposal, build_fact_correction_proposal(raw, **kwargs))
        value = json.loads(proposal)
        self.assertEqual(value["original_candidate"], original)
        self.assertEqual(json.loads(raw)["candidates"][0], original)
        self.assertEqual(value["review_status"], "NEEDS_LAWYER_REVIEW")
        self.assertFalse(value["court_ready"])
        self.assertNotIn("confidence", value)
        for change in (
            {"expected_artifact_hash": digest("wrong")},
            {"candidate_hash": digest("other candidate")},
            {"revised_text": original["fact_text"]},
            {"revised_text": " "}, {"revised_text": "字" * 4001},
            {"reason": ""},
        ):
            with self.subTest(change=list(change)):
                with self.assertRaises(CaseLedgerExtractionBlocked):
                    build_fact_correction_proposal(raw, **{**kwargs, **change})

    def test_transaction_is_typed_and_source_bound(self) -> None:
        transaction = CaseLedgerExtractionCandidate(
            kind=ExtractionCandidateKind.TRANSACTION,
            source_refs=(self.page.input_ref,), evidence_page_ids=(self.page_id,),
            confidence=0.99, conflict_codes=(), risk_codes=(),
            supporting_excerpts=(ExtractionSupportingExcerpt(self.page_id, "2020年1月1日转账100元"),),
            local_date="2020-01-01", date_precision=ExtractionDatePrecision.EXACT_DATE,
            amount="100.00", currency="CNY",
            direction=ExtractionTransactionDirection.OUTGOING,
            payer_label="[合成] 付款方", payee_label="[合成] 收款方",
            channel=ExtractionTransactionChannel.WECHAT,
            transaction_reference="[合成] 交易号",
        )
        content, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(self.page,), candidates=(transaction,)
        )
        parsed = parse_case_ledger_extraction_candidate(content)
        self.assertEqual(parsed["candidates"][0]["kind"], "TRANSACTION")
        with self.assertRaisesRegex(CaseLedgerExtractionBlocked, "exact FACT"):
            build_fact_correction_proposal(
                content, expected_artifact_hash=sha256(content).hexdigest(),
                candidate_hash=parsed["candidates"][0]["candidate_hash"],
                revised_text="不能用自由文字改变交易金额。", reason="应使用结构化交易修订。",
            )

    def test_ocr_low_confidence_or_conflict_never_qualifies_for_auto_staging(self) -> None:
        ocr_page = CaseLedgerExtractionSourcePage(
            input_ref=self.page.input_ref, evidence_page_id=self.page_id,
            source_file_sha256=self.page.source_file_sha256, page_number=1,
            source_text_sha256=self.page.source_text_sha256,
            source_mode=ExtractionSourceMode.OCR,
        )
        for candidate in (
            self._fact(confidence=0.97),
            self._fact(conflict_codes=(ExtractionConflictCode.POSSIBLE_DUPLICATE,)),
            self._fact(risk_codes=(ExtractionRiskCode.OCR_DERIVED,)),
        ):
            content, _ = build_case_ledger_extraction_candidate(
                task_input_hash=digest("task"), source_pages=(self.page,), candidates=(candidate,)
            )
            parsed = parse_case_ledger_extraction_candidate(content)
            self.assertFalse(extraction_candidate_is_eligible(parsed, parsed["candidates"][0]))
        content, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(ocr_page,), candidates=(self._fact(),)
        )
        parsed = parse_case_ledger_extraction_candidate(content)
        self.assertFalse(extraction_candidate_is_eligible(parsed, parsed["candidates"][0]))

    def test_tampered_page_and_candidate_hash_are_rejected(self) -> None:
        content, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(self.page,), candidates=(self._fact(),)
        )
        value = json.loads(content)
        value["candidates"][0]["fact_text"] = "[合成] 被篡改文字。"
        tampered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(CaseLedgerExtractionBlocked):
            parse_case_ledger_extraction_candidate(tampered)
        value = json.loads(content)
        value["source_pages"][0]["page_number"] = 2
        tampered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(CaseLedgerExtractionBlocked):
            parse_case_ledger_extraction_candidate(tampered)

    def test_legal_conclusion_and_client_page_substitution_are_rejected(self) -> None:
        with self.assertRaises(CaseLedgerExtractionBlocked):
            build_case_ledger_extraction_candidate(
                task_input_hash=digest("task"), source_pages=(self.page,),
                candidates=(self._fact(source_refs=(f"evidence-page:{uuid4()}",)),),
            )
        content, _ = build_case_ledger_extraction_candidate(
            task_input_hash=digest("task"), source_pages=(self.page,), candidates=(),
        )
        value = json.loads(content)
        value["legal_conclusion"] = True
        bad = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(CaseLedgerExtractionBlocked):
            parse_case_ledger_extraction_candidate(bad)
