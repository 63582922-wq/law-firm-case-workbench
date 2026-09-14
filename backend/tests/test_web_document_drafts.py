from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
import unittest

from case_api.web_document_drafts import (
    WebDocumentDraftBlocked,
    WebDocumentDraftRendererBlocked,
    WebDocumentDraftRendererUnknown,
    WebDocumentDraftService,
    _ledger_rows,
)
from case_kernel.models import Actor, Role
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf
from case_kernel.reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
)


class _Case:
    def get_case_snapshot(self, *, matter_id, actor):
        return SimpleNamespace(
            matter_id=matter_id, version=3, snapshot_hash="a" * 64,
            facts=({"fact_id": str(uuid4()), "original_text": "借款事实已由律师确认。", "status": "CONFIRMED"},),
            claims=({"claim_id": str(uuid4()), "original_claim_text": "请求返还本金。", "claimed_amount": "100000", "currency": "CNY", "status": "CONFIRMED_SCOPE"},),
            transactions=({"transaction_id": str(uuid4()), "local_date": "2020-08-20", "amount": "10000", "currency": "CNY", "direction": "INBOUND", "payer_label": "甲", "payee_label": "乙", "status": "CONFIRMED"},),
        )


class _Converter:
    def convert_generated_document(self, content, *, content_sha256, source_name, detected_kind):
        pdf = b"%PDF-1.4\n%%EOF"
        return ConvertedOfficePdf(
            source_sha256=content_sha256, detected_kind=detected_kind,
            converter_id="test", converter_version="test", transform_hash="b" * 64,
            pdf_sha256=sha256(pdf).hexdigest(), pdf_bytes=len(pdf), page_count=1,
            render_verification_hash="c" * 64, pdf_content=pdf,
        )


class _RendererBlocked:
    def convert_generated_document(self, *args, **kwargs):
        del args, kwargs
        raise ReviewOfficeConversionBlocked("renderer rejected")


class _RendererUnknown:
    def convert_generated_document(self, *args, **kwargs):
        del args, kwargs
        raise ReviewOfficeConversionUnknown("renderer status unknown")


class _Objects:
    def put_verified_office_artifact(self, content, *, content_sha256, media_type):
        return SimpleNamespace(object_key=f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca", content_sha256=content_sha256, byte_size=len(content))

    def put_verified_review_pdf(self, content, *, content_sha256):
        return SimpleNamespace(object_key=f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca", content_sha256=content_sha256, byte_size=len(content))


class _Reviewable:
    def __init__(self):
        self.registered = None

    def register_reviewable_office_draft_pair(self, **kwargs):
        self.registered = kwargs
        return SimpleNamespace(object_id=str(uuid4()), matter_version=4)

    def get_reviewable_office_draft_snapshot(self, **kwargs):
        return SimpleNamespace(matter_id=kwargs["matter_id"], matter_version=3, pairs=(), snapshot_hash="d" * 64)


class WebDocumentDraftTests(unittest.TestCase):
    def setUp(self):
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.actor.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.reviewable = _Reviewable()
        self.service = WebDocumentDraftService(
            case_ledger_store=_Case(), reviewable_store=self.reviewable, object_store=_Objects(),
            system_worker_for_firm=lambda firm_id: self.worker, converter=_Converter(),
        )

    def test_generates_server_bound_word_candidate_without_browser_text(self):
        receipt = self.service.generate(
            matter_id=str(uuid4()), actor=self.actor, expected_version=3,
            idempotency_key="document-candidate-0001", document_kind="CASE_REVIEW_MEMO",
        )
        self.assertEqual(receipt.document_kind, "CASE_REVIEW_MEMO")
        self.assertEqual(self.reviewable.registered["actor"].roles, frozenset({Role.SYSTEM_WORKER}))
        self.assertEqual(self.reviewable.registered["editable_media_type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_rejects_unconfirmed_only_case(self):
        self.service._case.get_case_snapshot = lambda **kwargs: SimpleNamespace(version=3, snapshot_hash="a" * 64, facts=(), claims=(), transactions=())
        with self.assertRaises(WebDocumentDraftBlocked):
            self.service.generate(matter_id=str(uuid4()), actor=self.actor, expected_version=3, idempotency_key="document-candidate-0002", document_kind="CASE_REVIEW_MEMO")

    def test_ledger_projection_uses_chinese_display_values_without_mutating_confirmed_inputs(self):
        transaction_id = str(uuid4())
        snapshot = SimpleNamespace(
            transactions=(
                {
                    "transaction_id": transaction_id,
                    "local_date": "2025-04-10",
                    "amount": "30000.000000",
                    "currency": "CNY",
                    "direction": "INCOMING",
                    "payer_label": "付款方（测试）",
                    "payee_label": "收款方（测试）",
                    "status": "CONFIRMED",
                },
                {
                    "transaction_id": str(uuid4()),
                    "status": "CANDIDATE",
                },
            )
        )

        self.assertEqual(
            _ledger_rows(snapshot),
            (
                (
                    date(2025, 4, 10),
                    "收款",
                    Decimal("30000.000000"),
                    "人民币",
                    "付款方（测试）",
                    "收款方（测试）",
                    "已确认",
                    transaction_id,
                ),
            ),
        )

    def test_maps_known_and_unknown_renderer_results_without_retrying(self):
        for converter, expected in (
            (_RendererBlocked(), WebDocumentDraftRendererBlocked),
            (_RendererUnknown(), WebDocumentDraftRendererUnknown),
        ):
            with self.subTest(converter=type(converter).__name__):
                self.service._converter = converter
                with self.assertRaises(expected):
                    self.service.generate(
                        matter_id=str(uuid4()), actor=self.actor, expected_version=3,
                        idempotency_key=f"document-candidate-{type(converter).__name__}",
                        document_kind="CASE_REVIEW_MEMO",
                    )


if __name__ == "__main__":
    unittest.main()
