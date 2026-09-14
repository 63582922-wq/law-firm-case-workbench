from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5

from case_kernel.brave_public_search import (
    BRAVE_SEARCH_HOST,
    BravePublicSearchProvider,
    BraveSearchCredentials,
    BraveSearchTransportResult,
)
from case_kernel.case_agent_research_adapters import (
    AuthorizedPublicResearchBinding,
    CaseAgentResearchAdapterBlocked,
    PUBLIC_RESEARCH_ARTIFACT_KIND,
    PUBLIC_WEB_RESEARCH_MANIFEST,
    PublicWebResearchTaskAdapter,
    RecoveredPublicSearch,
    RecoveredSearchStatus,
)
from case_kernel.case_agent_skill_adapters import StagedReviewCandidate
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
)
from case_kernel.controlled_web_research import (
    EgressReceiptRef,
    ResearchPurpose,
)


def digest(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return sha256(raw).hexdigest()


class _BindingPort:
    def __init__(self, binding):
        self.binding = binding
        self.calls = []

    def resolve_public_research(self, **kwargs):
        self.calls.append(kwargs)
        return self.binding


class _Staging:
    def __init__(self):
        self.requests = []

    def stage_review_candidate(self, request):
        request.validate()
        self.requests.append(request)
        return StagedReviewCandidate.build(
            request,
            artifact_id=str(uuid5(UUID(request.task_id), request.idempotency_key)),
        )


class _Exchange:
    def __init__(self, raw: bytes, *, recovered=None):
        self.raw = raw
        self.recovered = recovered
        self.send_calls = 0
        self.recover_calls = 0
        self.last_request = None

    def send(self, *, request, external_request):
        self.send_calls += 1
        self.last_request = request
        return BraveSearchTransportResult(
            response_body=self.raw,
            egress_receipt=_receipt(request, external_request, self.raw),
        )

    def recover(self, *, external_request_id, request_hash):
        self.recover_calls += 1
        if self.recovered is not None:
            return self.recovered
        raise AssertionError("recovery was not configured")


class _Context:
    def __init__(self, *, binding, reconciliation=False, max_output_bytes=1024 * 1024):
        self.claim = SimpleNamespace(
            run_id=binding.run_id,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            reconciliation=reconciliation,
        )
        self.task = SimpleNamespace(
            task_id=binding.task_id,
            input_hash=binding.task_input_hash,
            input_refs=binding.input_refs,
            skill=SimpleNamespace(tool_id="search_public_web"),
            capability=SimpleNamespace(
                execution_mode=AdapterExecutionMode.NETWORK_CONNECTOR,
                network_policy=NetworkPolicy.EXACT_ALLOWLIST,
                allowed_domains=(BRAVE_SEARCH_HOST,),
            ),
            budget=SimpleNamespace(
                max_external_calls=1,
                timeout_seconds=30,
                max_output_bytes=max_output_bytes,
            ),
        )
        self.input_refs = binding.input_refs
        self.external_boundary_committed = reconciliation
        self.external_request_id = (
            binding.external_request.request_id if reconciliation else None
        )
        self.started = []

    def begin_external_submission(self, **kwargs):
        if self.external_boundary_committed:
            raise AssertionError("submission boundary repeated")
        self.started.append(kwargs)
        self.external_boundary_committed = True
        self.external_request_id = kwargs["external_request_id"]


def _receipt(request, external_request, raw):
    return EgressReceiptRef(
        egress_grant_id=external_request.egress_grant_id,
        egress_grant_hash=external_request.egress_grant_hash,
        request_id=external_request.request_id,
        request_url=request.endpoint,
        redirect_chain=(),
        resolved_peer_ips=("8.8.8.8",),
        connected_peer_ip="8.8.8.8",
        method="GET",
        request_count=1,
        response_bytes=len(raw),
        response_sha256=digest(raw),
    )


class CaseAgentResearchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
        self.raw = json.dumps(
            {
                "type": "search",
                "web": {
                    "results": [
                        {
                            "title": "最高人民法院司法解释",
                            "url": "https://www.court.gov.cn/fabu-xiangqing-282671.html",
                            "description": "公开检索摘要，仍需抓取官方原文并复核。",
                            "page_age": "2020-12-31T00:00:00Z",
                        }
                    ]
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.binding = AuthorizedPublicResearchBinding.build(
            run_id=str(uuid4()),
            task_id=str(uuid4()),
            attempt_id=str(uuid4()),
            task_input_hash=digest("task-input"),
            matter_id=str(uuid4()),
            question_id=str(uuid4()),
            input_refs=(f"issue:{uuid4()}",),
            confidential_question="寒雪青松案件2019年至今利息适用规则是什么？",
            proposed_public_terms=("民间借贷", "利率保护上限", "过渡规则"),
            private_terms=("寒雪青松",),
            purpose=ResearchPurpose.LEGAL_AUTHORITY_DISCOVERY,
            external_request_id=str(uuid4()),
            egress_grant_id=str(uuid4()),
            expires_at=self.now + timedelta(minutes=15),
        )

    def adapter(self, exchange, staging=None):
        return PublicWebResearchTaskAdapter(
            binding_port=_BindingPort(self.binding),
            provider=BravePublicSearchProvider(
                credentials=BraveSearchCredentials("k" * 40),
                transport=None,
            ),
            exchange=exchange,
            staging_port=staging or _Staging(),
            clock=lambda: self.now,
            monotonic_clock=lambda: 1.0,
        )

    def test_binding_is_attempt_stable_private_and_tamper_evident(self):
        self.binding.validate()
        self.assertNotIn("寒雪青松", repr(self.binding))
        self.assertNotIn("寒雪青松", self.binding.binding_hash)
        changed = AuthorizedPublicResearchBinding(
            **{**self.binding.__dict__, "max_results": 1}
        )
        with self.assertRaisesRegex(CaseAgentResearchAdapterBlocked, "binding"):
            changed.validate()

    def test_execute_persists_boundary_before_exchange_and_stages_review_leads(self):
        staging = _Staging()
        exchange = _Exchange(self.raw)
        context = _Context(binding=self.binding)
        outcome = self.adapter(exchange, staging).execute(context=context)

        self.assertEqual(exchange.send_calls, 1)
        self.assertEqual(len(context.started), 1)
        self.assertEqual(
            context.started[0]["external_request_id"],
            self.binding.external_request.request_id,
        )
        self.assertEqual(context.started[0]["request_hash"], exchange.last_request.request_hash)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.SUBMITTED
        )
        self.assertEqual(outcome.artifacts[0].artifact_kind, PUBLIC_RESEARCH_ARTIFACT_KIND)
        self.assertFalse(outcome.artifacts[0].managed_derivative)
        payload = json.loads(staging.requests[0].payload)
        self.assertEqual(payload["review_status"], "NEEDS_LAWYER_REVIEW")
        self.assertFalse(payload["legal_effect_confirmed"])
        self.assertFalse(payload["legal_conclusion"])
        self.assertNotIn("寒雪青松", staging.requests[0].payload.decode("utf-8"))

    def test_reconciliation_recovers_without_resubmitting(self):
        # Build the exact request through the adapter once without sending.
        exchange = _Exchange(self.raw)
        context = _Context(binding=self.binding, reconciliation=True)
        adapter = self.adapter(exchange)
        _, _, external_request, prepared = adapter._prepare(
            context, for_reconciliation=True
        )
        recovered_receipt = _receipt(prepared, external_request, self.raw)
        exchange.recovered = RecoveredPublicSearch(
            RecoveredSearchStatus.SUCCEEDED,
            response_body=self.raw,
            egress_receipt=recovered_receipt,
        )
        outcome = adapter.reconcile(context=context)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 1)

    def test_unresolved_reconciliation_never_calls_send(self):
        exchange = _Exchange(
            self.raw,
            recovered=RecoveredPublicSearch(RecoveredSearchStatus.UNRESOLVED),
        )
        context = _Context(binding=self.binding, reconciliation=True)
        with self.assertRaisesRegex(
            CaseAgentResearchAdapterBlocked, "must not be repeated"
        ):
            self.adapter(exchange).reconcile(context=context)
        self.assertEqual(exchange.send_calls, 0)
        self.assertEqual(exchange.recover_calls, 1)

    def test_manifest_is_network_reconcilable_and_exact(self):
        PUBLIC_WEB_RESEARCH_MANIFEST.validate()
        self.assertTrue(PUBLIC_WEB_RESEARCH_MANIFEST.network_capable)
        self.assertTrue(PUBLIC_WEB_RESEARCH_MANIFEST.supports_reconciliation)


if __name__ == "__main__":
    unittest.main()
