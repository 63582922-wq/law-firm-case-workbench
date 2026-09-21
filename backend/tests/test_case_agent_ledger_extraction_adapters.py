from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_ledger_extraction import (
    ExtractionSourceMode,
    parse_case_ledger_extraction_candidate,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_HOST,
    DeepSeekLedgerExtractionTaskAdapter,
    LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED,
    LEDGER_EXTRACTION_PROVIDER_CONNECT_FAILED,
    LedgerExtractionKnownFailure,
    LedgerExtractionPageProjection,
    CaseLedgerExtractionAdapterBlocked,
    RecoveredLedgerExtraction,
    DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
    _LEGACY_LEDGER_EXTRACTION_SYSTEM_PROMPT,
)
from case_kernel.case_agent_skill_adapters import StagedReviewCandidate
from case_kernel.case_agent_worker import CaseAgentReconciliationUnavailable
from case_kernel.case_agent_supervisor import (
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Projection:
    def __init__(self, pages):
        self.pages = pages

    def project_ledger_pages(self, **_kwargs):
        return self.pages


class _Staging:
    def __init__(self):
        self.request = None

    def stage_review_candidate(self, request):
        self.request = request
        return StagedReviewCandidate.build(
            request,
            artifact_id=str(uuid5(UUID(request.task_id), request.idempotency_key)),
        )


class _Exchange:
    def __init__(self, response):
        self.response = response

    def send(self, *, request):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def recover(self, **_kwargs):
        raise AssertionError("recovery is not used")


class _Context:
    def __init__(self, *, task_ref):
        self.claim = SimpleNamespace(
            run_id=str(uuid4()),
            task_id=str(uuid4()),
            attempt_id=str(uuid4()),
            reconciliation=False,
        )
        self.task = SimpleNamespace(
            input_hash=digest("task"),
            input_refs=(task_ref,),
            skill=SimpleNamespace(tool_id="extract_case_ledger"),
            capability=SimpleNamespace(
                network_policy=NetworkPolicy.EXACT_ALLOWLIST,
                allowed_domains=(DEEPSEEK_LEDGER_EXTRACTION_HOST,),
            ),
            budget=SimpleNamespace(max_external_calls=1, max_cost_minor_units=120),
        )
        self.external_request_id = None

    def begin_external_submission(self, **kwargs):
        self.external_request_id = kwargs["external_request_id"]


class DeepSeekLedgerExtractionDependencyTests(unittest.TestCase):
    def test_cost_reservation_rejects_before_submission_or_provider(self):
        from unittest.mock import Mock
        from case_kernel.case_agent_ledger_extraction_adapters import ledger_extraction_cost_reserve
        page_id = str(uuid4())
        ref = f"evidence-page:{page_id}"
        page = LedgerExtractionPageProjection(input_ref=ref, evidence_page_id=page_id,
            source_file_sha256=digest("file"), page_number=1, extracted_text="原告请求偿还借款",
            extracted_text_sha256=digest("原告请求偿还借款"), source_mode=ExtractionSourceMode.NATIVE_TEXT)
        exchange = Mock()
        adapter = DeepSeekLedgerExtractionTaskAdapter(projection_port=_Projection((page,)),
                                                     exchange=exchange, staging_port=_Staging())
        context = _Context(task_ref=ref)
        context.task.budget.max_cost_minor_units = 0
        with self.assertRaisesRegex(CaseLedgerExtractionAdapterBlocked, "no submission"):
            adapter.execute(context=context)
        self.assertIsNone(context.external_request_id)
        exchange.send.assert_not_called()
        self.assertGreater(ledger_extraction_cost_reserve(b"x" * 100_000), 120)

    def test_page_only_prompt_and_legacy_lookup_do_not_resubmit(self):
        page_id = str(uuid4())
        ref = f"evidence-page:{page_id}"
        page = LedgerExtractionPageProjection(
            input_ref=ref, evidence_page_id=page_id,
            source_file_sha256=digest("file"), page_number=1,
            extracted_text="被告主张还款", extracted_text_sha256=digest("被告主张还款"),
            source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )
        looked_up = []

        class LookupOnly:
            def send(self, **kwargs):
                raise AssertionError("recovery must never submit")

            def recover(self, **kwargs):
                looked_up.append(kwargs)
                return RecoveredLedgerExtraction(status="UNRESOLVED")

        adapter = DeepSeekLedgerExtractionTaskAdapter(
            projection_port=_Projection((page,)), exchange=LookupOnly(),
            staging_port=_Staging(),
        )
        context = _Context(task_ref=ref)
        _, current = adapter._prepare(context)
        body = json.loads(current.body)
        self.assertEqual(body["messages"][0]["content"], DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("禁止输出CONTRADICTS_CASE_LEDGER", body["messages"][0]["content"])
        self.assertIn("必须保留陈述主体", body["messages"][0]["content"])
        self.assertIn("不要仅因材料尚未被律师确认", body["messages"][0]["content"])
        self.assertIn("试图操纵本次提取", body["messages"][0]["content"])
        _, legacy = adapter._prepare(context, system_prompt=_LEGACY_LEDGER_EXTRACTION_SYSTEM_PROMPT)
        self.assertNotEqual(current.request_hash, legacy.request_hash)
        context.claim.reconciliation = True
        for request in (legacy, current):
            context.external_request_id = request.external_request_id
            with self.assertRaisesRegex(
                CaseAgentReconciliationUnavailable,
                "external result remains unavailable",
            ) as raised:
                adapter.reconcile(context=context)
            self.assertEqual(
                raised.exception.reason_code,
                "LEDGER_EXTRACTION_RECOVERY_UNRESOLVED",
            )
            self.assertEqual(looked_up[-1], {
                "external_request_id": request.external_request_id,
            })
        context.external_request_id = str(uuid4())
        with self.assertRaisesRegex(
            CaseAgentReconciliationUnavailable,
            "external result remains unavailable",
        ):
            adapter.reconcile(context=context)
        self.assertEqual(len(looked_up), 3)

    def test_pre_dispatch_failure_records_zero_external_calls(self):
        page_id = str(uuid4())
        task_ref = f"evidence-page:{page_id}"
        page = LedgerExtractionPageProjection(
            input_ref=task_ref,
            evidence_page_id=page_id,
            source_file_sha256=digest("native-file"),
            page_number=1,
            extracted_text="2025年1月1日支付100元",
            extracted_text_sha256=digest("2025年1月1日支付100元"),
            source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )
        context = _Context(task_ref=task_ref)
        outcome = DeepSeekLedgerExtractionTaskAdapter(
            projection_port=_Projection((page,)),
            exchange=_Exchange(
                LedgerExtractionKnownFailure(
                    LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED
                )
            ),
            staging_port=_Staging(),
        ).execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state,
            ExternalSubmissionState.NOT_SUBMITTED,
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(
            outcome.error_code,
            LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED,
        )

    def test_provider_connect_failure_records_zero_external_calls(self):
        page_id = str(uuid4())
        task_ref = f"evidence-page:{page_id}"
        page = LedgerExtractionPageProjection(
            input_ref=task_ref,
            evidence_page_id=page_id,
            source_file_sha256=digest("native-file"),
            page_number=1,
            extracted_text="2025年1月1日支付100元",
            extracted_text_sha256=digest("2025年1月1日支付100元"),
            source_mode=ExtractionSourceMode.NATIVE_TEXT,
        )
        context = _Context(task_ref=task_ref)
        outcome = DeepSeekLedgerExtractionTaskAdapter(
            projection_port=_Projection((page,)),
            exchange=_Exchange(
                LedgerExtractionKnownFailure(
                    LEDGER_EXTRACTION_PROVIDER_CONNECT_FAILED
                )
            ),
            staging_port=_Staging(),
        ).execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state,
            ExternalSubmissionState.NOT_SUBMITTED,
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(outcome.cost_minor_units, 0)

    def test_direct_ocr_dependency_is_consumed_and_forced_to_review_risk(self):
        native_id = str(uuid4())
        ocr_id = str(uuid4())
        native_ref = f"evidence-page:{native_id}"
        ocr_ref = f"evidence-page:{ocr_id}"
        pages = tuple(sorted((
            LedgerExtractionPageProjection(
                input_ref=native_ref,
                evidence_page_id=native_id,
                source_file_sha256=digest("native-file"),
                page_number=1,
                extracted_text="对账日期 2026-04-20",
                extracted_text_sha256=digest("对账日期 2026-04-20"),
                source_mode=ExtractionSourceMode.NATIVE_TEXT,
            ),
            LedgerExtractionPageProjection(
                input_ref=ocr_ref,
                evidence_page_id=ocr_id,
                source_file_sha256=digest("ocr-file"),
                page_number=1,
                extracted_text="银行回单日期 2026-04-21",
                extracted_text_sha256=digest("银行回单日期 2026-04-21"),
                source_mode=ExtractionSourceMode.OCR,
            ),
        ), key=lambda page: page.input_ref))
        response = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidates": [
                                        {
                                            "kind": "FACT",
                                            "evidence_page_ids": [ocr_id],
                                            "confidence": 0.99,
                                            "conflict_codes": ["CROSS_PAGE_CONFLICT"],
                                            "risk_codes": [],
                                            "supporting_excerpts": [
                                                {
                                                    "evidence_page_id": ocr_id,
                                                    "text": "2026-04-21",
                                                }
                                            ],
                                            "fact_text": "回单显示的日期与对账表不一致。",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        staging = _Staging()
        context = _Context(task_ref=native_ref)
        outcome = DeepSeekLedgerExtractionTaskAdapter(
            projection_port=_Projection(pages),
            exchange=_Exchange(response),
            staging_port=staging,
        ).execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        artifact = parse_case_ledger_extraction_candidate(staging.request.payload)
        source_modes = {
            page["evidence_page_id"]: page["source_mode"]
            for page in artifact["_source_pages"]
        }
        self.assertEqual(source_modes[ocr_id], "OCR")
        self.assertEqual(
            artifact["candidates"][0]["risk_codes"],
            ["OCR_DERIVED", "UNTRUSTED_TEXT"],
        )


if __name__ == "__main__":
    unittest.main()
