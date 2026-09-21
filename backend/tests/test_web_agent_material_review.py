from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from case_kernel.models import Actor, Role
from case_kernel.web_agent_material_review import (
    AgentEvidencePageBinding,
    AgentEvidencePageProjection,
    AgentMaterialReviewBlocked,
    AgentPlanContext,
    AgentProviderUnknownSubmission,
    AgentRunStatus,
    AgentTaskStatus,
    InMemoryAgentMaterialRunStore,
    WebMaterialAgentCoordinator,
    build_material_analysis_request,
    parse_material_candidate_response,
)


class _Provider:
    def __init__(self, *, store, worker, response: str | None = None, error: Exception | None = None) -> None:
        self.store = store
        self.worker = worker
        self.response = response
        self.error = error
        self.calls = 0

    def analyze_materials(self, request):
        self.calls += 1
        claimed = self.store.get_run(actor=self.worker, run_id=request.run_id)
        self.store.mark_submission_started(
            actor=self.worker,
            run_id=request.run_id,
            expected_run_version=claimed.run_version,
            lease_id=request.claim_lease_id,
            external_request_id=request.external_request_id,
            request_hash="9" * 64,
            idempotency_key=f"mark-started:{request.run_id}",
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _PageSource:
    def __init__(self, pages) -> None:
        self.pages = {page.evidence_page_id: page for page in pages}

    def load_pages(self, *, actor, matter_id, evidence_page_ids):
        del actor, matter_id
        return tuple(self.pages[page_id] for page_id in evidence_page_ids)


def _page(page_id: str, page_number: int, text: str) -> AgentEvidencePageProjection:
    return AgentEvidencePageProjection.build(
        evidence_page_id=page_id,
        source_file_sha256=("a" if page_number == 1 else "b") * 64,
        page_number=page_number,
        extracted_text=text,
    )


def _response(request, pages):
    return json.dumps(
        {
            "schema_version": "web-agent-material-review-v1",
            "input_hash": request.input_hash,
            "candidates": [
                {
                    "evidence_page_id": pages[0].evidence_page_id,
                    "source_file_sha256": pages[0].source_file_sha256,
                    "page_number": pages[0].page_number,
                    "kind": "RELEVANT_PAGE",
                    "confidence": 0.96,
                    "review_priority": "LOW",
                    "reason_codes": ["TARGET_ALIAS_MATCH", "TRANSACTION_ENTRY"],
                    "supporting_excerpt": "寒雪青松 转账 5000元",
                    "duplicate_of_page_id": None,
                },
                {
                    "evidence_page_id": pages[1].evidence_page_id,
                    "source_file_sha256": pages[1].source_file_sha256,
                    "page_number": pages[1].page_number,
                    "kind": "OCR_REQUIRED",
                    "confidence": 0.32,
                    "review_priority": "HIGH",
                    "reason_codes": ["LOW_OCR_CONFIDENCE"],
                    "supporting_excerpt": "",
                    "duplicate_of_page_id": None,
                },
            ],
        },
        ensure_ascii=False,
    )


def _bindings(request):
    return tuple(
        AgentEvidencePageBinding(
            page.evidence_page_id,
            page.source_file_sha256,
            page.page_number,
            page.extracted_text_sha256,
        )
        for page in request.pages
    )


class WebAgentMaterialReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lawyer = Actor("lawyer-1", "firm-1", frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor("worker-1", "firm-1", frozenset({Role.SYSTEM_WORKER}))
        self.pages = (
            _page("page-1", 1, "2020年8月20日 寒雪青松 转账 5000元"),
            _page("page-2", 2, ""),
        )
        self.plan_context = AgentPlanContext.neutral(
            representation_profile_version=1,
            representation_profile_hash="c" * 64,
        )

    def _queue_and_bind(self, coordinator, *, key: str):
        queued = coordinator.queue_run(
            actor=self.lawyer,
            matter_id="matter-1",
            matter_version=7,
            idempotency_key=f"queue-{key}",
            evidence_page_ids=tuple(page.evidence_page_id for page in self.pages),
            plan_context=self.plan_context,
        )
        return coordinator.bind_external_request(
            actor=self.lawyer,
            run_id=queued.run_id,
            expected_run_version=queued.run_version,
            authorized_matter_version=8,
            external_request_id=f"external-{key}",
            idempotency_key=f"bind-{key}",
        )

    def test_fixed_plan_moves_valid_model_output_only_to_needs_review(self) -> None:
        request = build_material_analysis_request(matter_id="matter-1", matter_version=7, pages=self.pages)
        store = InMemoryAgentMaterialRunStore(
            clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
        )
        provider = _Provider(store=store, worker=self.worker, response=_response(request, self.pages))
        coordinator = WebMaterialAgentCoordinator(
            store=store, provider=provider, page_source=_PageSource(self.pages)
        )
        queued = self._queue_and_bind(coordinator, key="run-001")
        self.assertEqual(queued.status, AgentRunStatus.QUEUED)
        self.assertEqual(len(queued.tasks), 4)
        completed = coordinator.execute_run(
            actor=self.worker,
            run_id=queued.run_id,
            expected_run_version=queued.run_version,
            idempotency_key="execute-run-001",
        )
        self.assertEqual(completed.status, AgentRunStatus.NEEDS_REVIEW)
        self.assertEqual(len(completed.candidates), 2)
        self.assertTrue(all(candidate.matter_id == "matter-1" for candidate in completed.candidates))
        self.assertTrue(all(len(candidate.source_file_sha256) == 64 for candidate in completed.candidates))
        self.assertEqual(completed.tasks[-1].status, AgentTaskStatus.NEEDS_REVIEW)
        self.assertIsNone(completed.failure_code)

    def test_parser_rejects_invented_source_and_non_verbatim_excerpt(self) -> None:
        request = build_material_analysis_request(matter_id="matter-1", matter_version=7, pages=self.pages)
        value = json.loads(_response(request, self.pages))
        value["candidates"][0]["evidence_page_id"] = "invented-page"
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "unknown page"):
            parse_material_candidate_response(json.dumps(value, ensure_ascii=False), request=request)
        value = json.loads(_response(request, self.pages))
        value["candidates"][0]["supporting_excerpt"] = "模型编造的文字"
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "not present"):
            parse_material_candidate_response(json.dumps(value, ensure_ascii=False), request=request)

    def test_invalid_model_output_is_failed_not_completed(self) -> None:
        store = InMemoryAgentMaterialRunStore()
        provider = _Provider(store=store, worker=self.worker, response='{"schema_version":"wrong"}')
        coordinator = WebMaterialAgentCoordinator(
            store=store, provider=provider, page_source=_PageSource(self.pages)
        )
        queued = self._queue_and_bind(coordinator, key="run-002")
        result = coordinator.execute_run(
            actor=self.worker,
            run_id=queued.run_id,
            expected_run_version=queued.run_version,
            idempotency_key="execute-run-002",
        )
        self.assertEqual(result.status, AgentRunStatus.FAILED)
        self.assertEqual(result.failure_code.value, "MODEL_RESPONSE_INVALID")
        self.assertEqual(result.candidates, ())

    def test_unknown_submission_is_terminal_and_never_silently_retried(self) -> None:
        store = InMemoryAgentMaterialRunStore()
        provider = _Provider(store=store, worker=self.worker, error=AgentProviderUnknownSubmission("timeout"))
        coordinator = WebMaterialAgentCoordinator(
            store=store, provider=provider, page_source=_PageSource(self.pages)
        )
        queued = self._queue_and_bind(coordinator, key="run-003")
        result = coordinator.execute_run(
            actor=self.worker,
            run_id=queued.run_id,
            expected_run_version=queued.run_version,
            idempotency_key="execute-run-003",
        )
        self.assertEqual(result.failure_code.value, "PROVIDER_RESULT_UNKNOWN")
        replay = coordinator.execute_run(
            actor=self.worker,
            run_id=queued.run_id,
            expected_run_version=1,
            idempotency_key="execute-run-003",
        )
        self.assertEqual(replay, result)
        self.assertEqual(provider.calls, 1)

    def test_idempotency_and_versions_are_fail_closed(self) -> None:
        store = InMemoryAgentMaterialRunStore()
        request = build_material_analysis_request(matter_id="matter-1", matter_version=7, pages=self.pages)
        first = store.create_run(
            actor=self.lawyer,
            matter_id="matter-1",
            matter_version=7,
            input_hash=request.input_hash,
            request_hash="d" * 64,
            page_bindings=_bindings(request),
            plan_context=self.plan_context,
            idempotency_key="queue-run-004",
        )
        replay = store.create_run(
            actor=self.lawyer,
            matter_id="matter-1",
            matter_version=7,
            input_hash=request.input_hash,
            request_hash="d" * 64,
            page_bindings=first.page_bindings,
            plan_context=self.plan_context,
            idempotency_key="queue-run-004",
        )
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "idempotency"):
            store.create_run(
                actor=self.lawyer,
                matter_id="matter-1",
                matter_version=7,
                input_hash=request.input_hash,
                request_hash="f" * 64,
                page_bindings=first.page_bindings,
                plan_context=self.plan_context,
                idempotency_key="queue-run-004",
            )
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "version conflict"):
            store.claim_run(
                actor=self.worker,
                run_id=first.run_id,
                expected_run_version=2,
                idempotency_key="start-run-004",
            )

    def test_cross_firm_read_is_indistinguishable_from_missing_run(self) -> None:
        store = InMemoryAgentMaterialRunStore()
        request = build_material_analysis_request(matter_id="matter-1", matter_version=7, pages=self.pages)
        run = store.create_run(
            actor=self.lawyer,
            matter_id="matter-1",
            matter_version=7,
            input_hash=request.input_hash,
            request_hash="e" * 64,
            page_bindings=_bindings(request),
            plan_context=self.plan_context,
            idempotency_key="queue-run-005",
        )
        outsider = Actor("worker-2", "firm-2", frozenset({Role.SYSTEM_WORKER}))
        with self.assertRaises(KeyError):
            store.get_run(actor=outsider, run_id=run.run_id)

    def test_external_execution_requires_exact_authorization_and_confirmed_profile(self) -> None:
        store = InMemoryAgentMaterialRunStore()
        request = build_material_analysis_request(
            matter_id="matter-1", matter_version=7, pages=self.pages
        )
        run = store.create_run(
            actor=self.lawyer,
            matter_id="matter-1",
            matter_version=7,
            input_hash=request.input_hash,
            request_hash="f" * 64,
            page_bindings=_bindings(request),
            plan_context=AgentPlanContext.neutral(),
            idempotency_key="queue-neutral-no-profile",
        )
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "representation profile"):
            store.bind_external_request(
                actor=self.lawyer,
                run_id=run.run_id,
                expected_run_version=run.run_version,
                authorized_matter_version=8,
                external_request_id="external-no-profile",
                idempotency_key="bind-neutral-no-profile",
            )

    def test_input_hash_is_content_bound_not_matter_version_bound(self) -> None:
        first = build_material_analysis_request(
            matter_id="matter-1", matter_version=7, pages=self.pages
        )
        after_authorization = build_material_analysis_request(
            matter_id="matter-1", matter_version=8, pages=self.pages
        )
        self.assertEqual(first.input_hash, after_authorization.input_hash)

    def test_one_run_accepts_current_36_page_case_boundary(self) -> None:
        pages = tuple(
            AgentEvidencePageProjection.build(
                evidence_page_id=f"page-{index}",
                source_file_sha256=f"{index:064x}"[-64:],
                page_number=1,
                extracted_text=f"第 {index} 页",
            )
            for index in range(1, 37)
        )
        request = build_material_analysis_request(
            matter_id="matter-1", matter_version=7, pages=pages
        )
        self.assertEqual(len(request.pages), 36)


if __name__ == "__main__":
    unittest.main()
