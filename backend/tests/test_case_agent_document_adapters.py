from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_document_adapters import (
    CaseAgentDocumentAdapterBlocked,
    DOCUMENT_CANDIDATE_ARTIFACT_KIND,
    DOCUMENT_EDITABLE_ARTIFACT_KIND,
    DOCUMENT_PDF_ARTIFACT_KIND,
    DynamicDocumentTaskAdapter,
    RecoveredDocumentDraft,
    StagedDocumentPackage,
)
from case_kernel.case_agent_document_delivery import (
    ReviewableDocumentFormat,
    build_document_draft_request,
    parse_reviewable_document_candidate,
)
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    ArtifactReceipt,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
)
from case_kernel.deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
)
from case_kernel.deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftNotSubmitted,
    DeepSeekDocumentDraftProvider,
)
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf
from case_kernel.reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
)
from backend.tests.test_case_agent_document_delivery import (
    CaseAgentDocumentDeliveryTests,
)


class BindingPort:
    def __init__(self, binding):
        self.binding = binding
        self.calls = []

    def resolve_document_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.binding


class Exchange:
    def __init__(self, candidate, *, recovered=None, fail=None):
        self.candidate = candidate
        self.recovered = recovered
        self.fail = fail
        self.send_calls = 0
        self.recover_calls = 0

    def send(self, **kwargs):
        self.send_calls += 1
        if self.fail:
            raise self.fail
        return self.candidate

    def recover(self, **kwargs):
        self.recover_calls += 1
        if self.recovered is None:
            raise AssertionError("recovery was not configured")
        return self.recovered


class Converter:
    def __init__(self):
        self.calls = 0

    def convert_generated_document(self, content, *, content_sha256, source_name, detected_kind):
        self.calls += 1
        pdf = b"%PDF-1.4\nreview\n%%EOF"
        return ConvertedOfficePdf(
            source_sha256=content_sha256,
            detected_kind=detected_kind,
            converter_id="test-isolated-converter",
            converter_version="1.0.0",
            transform_hash=sha256((content_sha256 + detected_kind).encode()).hexdigest(),
            pdf_sha256=sha256(pdf).hexdigest(),
            pdf_bytes=len(pdf),
            page_count=1,
            render_verification_hash=sha256(b"render").hexdigest(),
            pdf_content=pdf,
        )


class FailingConverter:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def convert_generated_document(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error


class Staging:
    def __init__(self):
        self.requests = []

    def stage_document_package(self, request):
        self.requests.append(request)
        package_id = str(uuid5(UUID(request.task_id), request.candidate.candidate_hash))
        contents = {
            DOCUMENT_CANDIDATE_ARTIFACT_KIND: request.candidate_content,
            DOCUMENT_EDITABLE_ARTIFACT_KIND: request.generated.editable_artifact.content,
            DOCUMENT_PDF_ARTIFACT_KIND: request.generated.review_pdf.pdf_content,
        }
        artifacts = tuple(
            ArtifactReceipt(
                artifact_id=str(uuid5(UUID(request.task_id), f"{package_id}:{kind}")),
                artifact_kind=kind,
                content_hash=sha256(content).hexdigest(),
                byte_size=len(content),
                source_input_hash=request.task_input_hash,
                managed_derivative=True,
            )
            for kind, content in contents.items()
        )
        return StagedDocumentPackage(
            package_id=package_id,
            receipt_hash=sha256(package_id.encode()).hexdigest(),
            artifact_receipts=artifacts,
        )


class Context:
    def __init__(self, binding, *, reconciliation=False):
        self.claim = SimpleNamespace(
            run_id=binding.run_id,
            task_id=binding.task_id,
            attempt_id=str(uuid4()),
            reconciliation=reconciliation,
        )
        self.task = SimpleNamespace(
            skill=SimpleNamespace(tool_id=(
                "draft_reviewable_docx_package"
                if binding.template.output_format is ReviewableDocumentFormat.DOCX
                else "draft_reviewable_xlsx_package"
            )),
            input_hash=binding.task_input_hash,
            input_refs=(f"work-plan-item:{binding.work_plan_item.item_id}",),
            capability=SimpleNamespace(
                execution_mode=AdapterExecutionMode.IN_PROCESS,
                network_policy=NetworkPolicy.DENY,
                allowed_domains=(),
                writes_managed_derivatives=True,
            ),
            budget=SimpleNamespace(max_external_calls=0),
        )
        self.external_boundary_committed = reconciliation
        self.external_request_id = None
        self.started = []
        self.heartbeats = 0

    def begin_external_submission(self, **kwargs):
        if self.external_boundary_committed:
            raise AssertionError("boundary repeated")
        self.started.append(kwargs)
        self.external_boundary_committed = True
        self.external_request_id = kwargs["external_request_id"]

    def heartbeat(self):
        self.heartbeats += 1


class CaseAgentDocumentAdapterTests(unittest.TestCase):
    def setup_case(self, deliverable="CASE_REVIEW_MEMO"):
        helper = CaseAgentDocumentDeliveryTests()
        binding = helper.binding(deliverable)
        candidate = parse_reviewable_document_candidate(helper.response(binding), binding=binding)
        provider = DeepSeekDocumentDraftProvider(
            credentials=DeepSeekDocumentDraftCredentials("k" * 40),
            config=DeepSeekDocumentDraftConfig(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model="deepseek-chat",
                allowed_models=("deepseek-chat",),
            ),
            transport=lambda *_: b"{}",
        )
        return binding, candidate, provider

    def adapter(
        self,
        binding,
        candidate,
        provider,
        exchange=None,
        staging=None,
        converter=None,
    ):
        return DynamicDocumentTaskAdapter(
            output_format=binding.template.output_format,
            binding_port=BindingPort(binding),
            provider=provider,
            exchange=exchange or Exchange(candidate),
            converter=converter or Converter(),
            staging_port=staging or Staging(),
            monotonic_clock=lambda: 1.0,
        )

    def test_docx_task_is_deterministic_and_stages_three_bound_artifacts(self):
        binding, candidate, provider = self.setup_case()
        staging = Staging()
        context = Context(binding)
        exchange = Exchange(candidate)
        outcome = self.adapter(
            binding, candidate, provider, exchange=exchange, staging=staging
        ).execute(context=context)
        self.assertEqual(context.started, [])
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 0)
        self.assertGreaterEqual(context.heartbeats, 2)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertIsNone(outcome.external_request_id)
        self.assertEqual(len(outcome.artifacts), 3)
        self.assertEqual({item.artifact_kind for item in outcome.artifacts}, {
            DOCUMENT_CANDIDATE_ARTIFACT_KIND,
            DOCUMENT_EDITABLE_ARTIFACT_KIND,
            DOCUMENT_PDF_ARTIFACT_KIND,
        })
        self.assertEqual(staging.requests[0].binding.work_plan_item.item_id, binding.work_plan_item.item_id)
        self.assertEqual(staging.requests[0].candidate.deliverable_kind, "CASE_REVIEW_MEMO")
        memo_text = "\n".join(
            paragraph.text
            for section in staging.requests[0].candidate.sections
            for paragraph in section.paragraphs
        )
        self.assertIn("案件风险", memo_text)
        self.assertIn("由律师决定", memo_text)

    def test_xlsx_task_is_deterministic_and_never_calls_the_model_exchange(self):
        binding, candidate, provider = self.setup_case("PAYMENT_LEDGER")
        exchange = Exchange(candidate)
        staging = Staging()
        context = Context(binding)
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            staging=staging,
        ).execute(context=context)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertIsNone(outcome.external_request_id)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 0)
        self.assertEqual(context.started, [])
        self.assertEqual(len(staging.requests[0].candidate.rows), 1)
        self.assertEqual(
            staging.requests[0].candidate.rows[0].source_refs,
            (staging.requests[0].candidate.rows[0].row_id,),
        )
        self.assertTrue(all(item.managed_derivative for item in outcome.artifacts))

    def test_evidence_catalogue_task_is_deterministic_and_never_calls_the_model_exchange(self):
        binding, candidate, provider = self.setup_case("EVIDENCE_CATALOGUE")
        exchange = Exchange(candidate)
        staging = Staging()
        context = Context(binding)
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            staging=staging,
        ).execute(context=context)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(staging.requests[0].candidate.deliverable_kind, "EVIDENCE_CATALOGUE")

    def test_defence_statement_task_is_deterministic_and_never_calls_the_model_exchange(self):
        binding, candidate, provider = self.setup_case("DEFENCE_STATEMENT")
        exchange = Exchange(candidate)
        staging = Staging()
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            staging=staging,
        ).execute(context=Context(binding))
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 0)
        self.assertEqual(
            staging.requests[0].candidate.deliverable_kind,
            "DEFENCE_STATEMENT",
        )
        self.assertIn(
            "逐项回应",
            "\n".join(section.heading for section in staging.requests[0].candidate.sections),
        )

    def test_supplementary_evidence_checklist_is_deterministic_and_never_calls_the_model_exchange(self):
        binding, candidate, provider = self.setup_case("SUPPLEMENTARY_EVIDENCE_CHECKLIST")
        exchange = Exchange(candidate)
        staging = Staging()
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            staging=staging,
        ).execute(context=Context(binding))
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(
            staging.requests[0].candidate.deliverable_kind,
            "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
        )
        self.assertIn(
            "优先补齐材料",
            "\n".join(section.heading for section in staging.requests[0].candidate.sections),
        )

    def test_deterministic_documents_reject_network_capability_and_reconciliation(self):
        binding, candidate, provider = self.setup_case("PAYMENT_LEDGER")
        context = Context(binding)
        context.task.capability.execution_mode = AdapterExecutionMode.NETWORK_CONNECTOR
        context.task.capability.network_policy = NetworkPolicy.EXACT_ALLOWLIST
        context.task.capability.allowed_domains = ("api.deepseek.com",)
        context.task.budget.max_external_calls = 1
        with self.assertRaisesRegex(CaseAgentDocumentAdapterBlocked, "capability"):
            self.adapter(binding, candidate, provider).execute(context=context)
        with self.assertRaisesRegex(CaseAgentDocumentAdapterBlocked, "no external result"):
            self.adapter(binding, candidate, provider).reconcile(
                context=Context(binding, reconciliation=True)
            )

    def test_unknown_renderer_result_is_terminal_and_never_enters_auto_reconciliation(self):
        binding, candidate, provider = self.setup_case()
        exchange = Exchange(candidate)
        converter = FailingConverter(
            ReviewOfficeConversionUnknown("renderer response was lost")
        )
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            converter=converter,
        ).execute(context=Context(binding))

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE
        )
        self.assertEqual(outcome.error_code, "DOCUMENT_RENDERER_RESULT_UNKNOWN")
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 0)
        self.assertEqual(converter.calls, 1)

    def test_docx_never_calls_exchange_even_if_exchange_would_fail(self):
        binding, candidate, provider = self.setup_case()
        exchange = Exchange(
            candidate,
            fail=DeepSeekDocumentDraftNotSubmitted(
                "database policy blocked before transport"
            ),
        )
        context = Context(binding)

        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
        ).execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state,
            ExternalSubmissionState.NOT_APPLICABLE,
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertIsNone(outcome.error_code)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(context.started, [])

    def test_known_renderer_rejection_is_terminal_and_not_retried(self):
        binding, candidate, provider = self.setup_case()
        exchange = Exchange(candidate)
        converter = FailingConverter(
            ReviewOfficeConversionBlocked("renderer rejected generated bytes")
        )
        outcome = self.adapter(
            binding,
            candidate,
            provider,
            exchange=exchange,
            converter=converter,
        ).execute(context=Context(binding))

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(outcome.error_code, "DOCUMENT_RENDERER_REJECTED")
        self.assertEqual(outcome.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 0)
        self.assertEqual(converter.calls, 1)

    def test_task_binding_drift_fails_before_network(self):
        binding, candidate, provider = self.setup_case()
        context = Context(binding)
        context.task.input_refs = (f"work-plan-item:{uuid4()}",)
        exchange = Exchange(candidate)
        # A production binding port re-authorises exact refs.  This focused
        # fake returns the original binding, so the adapter's current contract
        # proves the task hash/binding identity before external submission.
        context.task.input_hash = sha256(b"changed").hexdigest()
        with self.assertRaisesRegex(RuntimeError, "binding differs"):
            self.adapter(binding, candidate, provider, exchange=exchange).execute(context=context)
        self.assertEqual(exchange.send_calls, 0)
        self.assertFalse(context.started)


if __name__ == "__main__":
    unittest.main()
