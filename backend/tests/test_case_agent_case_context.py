from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_case_context import (
    BoundCaseContextProjection,
    BoundCaseContextSource,
    CASE_CONTEXT_ARTIFACT_KIND,
    CASE_CONTEXT_CANDIDATE_SCHEMA,
    CaseContextReviewBlocked,
    CaseContextSourceType,
    build_case_context_review_candidate,
)
from case_kernel.case_agent_case_context_adapters import (
    CASE_CONTEXT_REVIEW_MANIFEST,
    DeterministicCaseContextTaskAdapter,
)
from case_kernel.case_agent_case_context_postgres import (
    PostgresCaseContextProjectionPort,
    _parse_refs,
    _read_task_binding,
)
from case_kernel.case_agent_planner import PlanningInputStatus
from case_kernel.case_agent_skill_adapters import (
    ReviewCandidateStagingRequest,
    StagedReviewCandidate,
)
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    ArtifactReceipt,
    ExternalSubmissionState,
    ResultStatus,
    TaskResultPayload,
    reduce_agent_event,
)
from case_kernel.case_agent_verifier import (
    ArtifactVerificationRejected,
    CanonicalJsonArtifactVerifier,
    CaseAgentRunVerifier,
    ManagedArtifactRead,
    VerificationOutcome,
)
from case_kernel.models import Actor, Role
from case_kernel.skill_registry import (
    CapabilityScope,
    SkillRegistryBlocked,
    default_case_skill_registry,
)

from backend.tests import test_case_agent_supervisor as supervisor_fixture


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _source(
    source_type: CaseContextSourceType,
    status: PlanningInputStatus,
    title: str,
    *,
    prefix: str,
    signals: tuple[str, ...] = (),
    confidence: float | None = None,
) -> BoundCaseContextSource:
    object_id = str(uuid4())
    return BoundCaseContextSource(
        input_ref=f"{prefix}:{object_id}",
        source_type=source_type,
        object_id=object_id,
        object_version="v3",
        content_hash=_digest(f"{source_type}:{object_id}"),
        status=status,
        primary_text=title,
        secondary_text=f"{title}的服务端台账摘要",
        signals=tuple(sorted(signals)),
        confidence=confidence,
    )


def _sources() -> tuple[BoundCaseContextSource, ...]:
    return (
        _source(
            CaseContextSourceType.CASE_FACT,
            PlanningInputStatus.DISPUTED,
            "借款交付时间存在争议",
            prefix="fact",
            signals=("DISPUTED",),
        ),
        _source(
            CaseContextSourceType.CASE_CLAIM,
            PlanningInputStatus.REVIEW_REQUIRED,
            "原告请求支付利息",
            prefix="claim",
            signals=("CANDIDATE",),
        ),
        _source(
            CaseContextSourceType.DISPUTE_ISSUE,
            PlanningInputStatus.CONFIRMED,
            "已付利息是否应冲抵本金",
            prefix="issue",
            signals=("CONFIRMED",),
        ),
        _source(
            CaseContextSourceType.CASE_TRANSACTION,
            PlanningInputStatus.CONFIRMED,
            "2020-09-01｜支出｜1000 CNY",
            prefix="transaction",
            signals=("CONFIRMED",),
        ),
        _source(
            CaseContextSourceType.POSTURE_PROFILE,
            PlanningInputStatus.CONFIRMED,
            "代理身份：DEFENDANT｜程序阶段：FIRST_INSTANCE",
            prefix="posture-profile",
            signals=("CURRENT_POSTURE",),
        ),
        _source(
            CaseContextSourceType.WORK_PLAN_ITEM,
            PlanningInputStatus.OPEN,
            "补充核对送达日期",
            prefix="work-plan-item",
            signals=("DEADLINE_RISK", "NEEDS_INFORMATION"),
            confidence=0.82,
        ),
        _source(
            CaseContextSourceType.VERIFIED_LEGAL_SOURCE,
            PlanningInputStatus.LOCKED,
            "最高人民法院｜相关条款",
            prefix="legal-source",
            signals=("LICENSE_ACTIVE", "VERIFIED"),
        ),
        _source(
            CaseContextSourceType.APPROVED_LEGAL_RULE,
            PlanningInputStatus.LOCKED,
            "已批准规则：民间借贷利息｜受控版本",
            prefix="legal-rule",
            signals=("APPROVED", "RULE_BINDING"),
        ),
        _source(
            CaseContextSourceType.PROCEDURAL_EVENT,
            PlanningInputStatus.CONFIRMED,
            "2026-08-01｜CLAIM_FILED",
            prefix="legal-event",
            signals=("APPROVED", "CLAIM_FILED"),
        ),
    )


def _projection(
    sources: tuple[BoundCaseContextSource, ...] | None = None,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    task_input_hash: str | None = None,
) -> BoundCaseContextProjection:
    items = sources or _sources()
    return BoundCaseContextProjection.build(
        run_id=run_id or str(uuid4()),
        task_id=task_id or str(uuid4()),
        task_input_hash=task_input_hash or _digest("task-input"),
        firm_id=str(uuid4()),
        matter_id=str(uuid4()),
        matter_version=7,
        case_snapshot_hash=_digest("case-snapshot"),
        input_refs=tuple(item.input_ref for item in items),
        sources=items,
    )


class _ProjectionPort:
    def __init__(
        self,
        firm_id: str,
        matter_id: str,
        sources: tuple[BoundCaseContextSource, ...],
    ) -> None:
        self.firm_id = firm_id
        self.matter_id = matter_id
        self.sources = sources
        self.calls = []

    def project_case_context(self, **kwargs):
        self.calls.append(kwargs)
        return BoundCaseContextProjection.build(
            **kwargs,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            matter_version=3,
            case_snapshot_hash=_digest("snapshot"),
            sources=self.sources,
        )


class _Staging:
    def __init__(self) -> None:
        self.requests: list[ReviewCandidateStagingRequest] = []
        self.receipts: dict[str, StagedReviewCandidate] = {}

    def stage_review_candidate(self, request):
        request.validate()
        self.requests.append(request)
        return self.receipts.setdefault(
            request.idempotency_key,
            StagedReviewCandidate.build(
                request,
                artifact_id=str(uuid5(UUID(request.task_id), request.idempotency_key)),
            ),
        )


class _Cursor:
    def __init__(self, *, row=None, rows=()) -> None:
        self._row = row
        self._rows = rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.calls = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))
        return _Cursor(row=self.row)


class _ArtifactAccess:
    def __init__(self, artifact: ArtifactReceipt, payload: bytes) -> None:
        self.artifact = artifact
        self.payload = payload

    def read_managed_artifact(self, *, artifact, **_):
        assert artifact == self.artifact
        return ManagedArtifactRead(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=self.payload,
            source_input_hash=artifact.source_input_hash,
            object_receipt_hash=_digest("case-context-managed-object"),
            media_type="application/json",
        )


def _context(input_refs: tuple[str, ...]):
    task_id = str(uuid4())
    task = SimpleNamespace(
        task_id=task_id,
        input_hash=_digest("compiled-task"),
        input_refs=input_refs,
        budget=SimpleNamespace(
            timeout_seconds=30,
            max_output_bytes=4 * 1024 * 1024,
        ),
    )
    claim = SimpleNamespace(run_id=str(uuid4()), task_id=task_id)
    return SimpleNamespace(claim=claim, task=task, input_refs=input_refs)


class CaseContextReviewTests(unittest.TestCase):
    def test_candidate_is_deterministic_review_only_and_source_linked(self) -> None:
        projection = _projection()
        first, source_hash = build_case_context_review_candidate(projection)
        second, second_hash = build_case_context_review_candidate(projection)
        self.assertEqual(first, second)
        self.assertEqual(source_hash, second_hash)
        self.assertEqual(
            first,
            json.dumps(
                json.loads(first),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )
        payload = json.loads(first)
        self.assertEqual(payload["schema_version"], CASE_CONTEXT_CANDIDATE_SCHEMA)
        for field in (
            "formal_fact",
            "formal_transaction",
            "legal_conclusion",
            "evidence_decision",
            "court_ready",
        ):
            self.assertIs(payload[field], False)
        self.assertEqual(payload["review_status"], "NEEDS_LAWYER_REVIEW")
        section_ids = {item["section_id"] for item in payload["sections"]}
        self.assertEqual(
            section_ids,
            {
                "DISPUTES",
                "RISKS",
                "GAPS",
                "PROCEDURE",
                "LEGAL_SOURCES",
                "TRANSACTIONS",
                "CASE_CONTEXT",
            },
        )
        cited = {
            ref
            for section in payload["sections"]
            for item in section["items"]
            for ref in item["source_refs"]
        }
        self.assertEqual(cited, set(projection.input_refs))
        self.assertGreaterEqual(payload["summary_counts"]["open_questions"], 2)

    def test_projection_rejects_source_order_or_binding_drift(self) -> None:
        sources = _sources()[:2]
        with self.assertRaisesRegex(CaseContextReviewBlocked, "compiled input order"):
            BoundCaseContextProjection(
                run_id=str(uuid4()),
                task_id=str(uuid4()),
                task_input_hash=_digest("task"),
                firm_id=str(uuid4()),
                matter_id=str(uuid4()),
                matter_version=1,
                case_snapshot_hash=_digest("snapshot"),
                input_refs=tuple(reversed(tuple(item.input_ref for item in sources))),
                sources=sources,
                binding_hash=_digest("forged"),
            ).validate()

    def test_runtime_adapter_stages_one_idempotent_review_candidate(self) -> None:
        sources = _sources()[:3]
        execution = _context(tuple(item.input_ref for item in sources))
        port = _ProjectionPort(str(uuid4()), str(uuid4()), sources)
        staging = _Staging()
        adapter = DeterministicCaseContextTaskAdapter(
            projection_port=port,
            staging_port=staging,
        )
        first = adapter.execute(context=execution)
        second = adapter.execute(context=execution)
        self.assertEqual(first.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            first.external_submission_state,
            ExternalSubmissionState.NOT_APPLICABLE,
        )
        self.assertEqual(first.external_calls, 0)
        self.assertEqual(first.cost_minor_units, 0)
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(
            first.artifacts[0].artifact_kind, CASE_CONTEXT_ARTIFACT_KIND
        )
        self.assertFalse(first.artifacts[0].managed_derivative)
        self.assertEqual(staging.requests[0].payload, staging.requests[1].payload)
        self.assertEqual(port.calls[0]["input_refs"], execution.input_refs)
        payload = json.loads(staging.requests[0].payload)
        self.assertFalse(payload["court_ready"])
        self.assertEqual(payload["task_input_hash"], execution.task.input_hash)

    def test_manifest_is_local_idempotent_and_non_reconcilable(self) -> None:
        CASE_CONTEXT_REVIEW_MANIFEST.validate()
        self.assertEqual(
            CASE_CONTEXT_REVIEW_MANIFEST.tool_id, "review_case_context"
        )
        self.assertTrue(CASE_CONTEXT_REVIEW_MANIFEST.supports_idempotency)
        self.assertFalse(CASE_CONTEXT_REVIEW_MANIFEST.supports_reconciliation)
        self.assertFalse(CASE_CONTEXT_REVIEW_MANIFEST.network_capable)

    def test_registered_format_verifier_accepts_only_exact_review_contract(self) -> None:
        payload, _ = build_case_context_review_candidate(_projection())
        managed = ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind=CASE_CONTEXT_ARTIFACT_KIND,
            content=payload,
            source_input_hash=json.loads(payload)["task_input_hash"],
            object_receipt_hash=_digest("managed-object"),
            media_type="application/json",
        )
        verifier = CanonicalJsonArtifactVerifier(
            artifact_kind=CASE_CONTEXT_ARTIFACT_KIND,
            allowed_schema_versions=(CASE_CONTEXT_CANDIDATE_SCHEMA,),
            payload_kind=CASE_CONTEXT_ARTIFACT_KIND,
        )
        receipt = verifier.verify(managed)
        self.assertEqual(receipt.artifact_kind, CASE_CONTEXT_ARTIFACT_KIND)
        self.assertEqual(
            receipt.declared_source_input_hash,
            json.loads(payload)["task_input_hash"],
        )

        mutations = []
        value = json.loads(payload)
        value["court_ready"] = True
        mutations.append(value)
        value = json.loads(payload)
        value["model_legal_opinion"] = "应当支持"
        mutations.append(value)
        value = json.loads(payload)
        value["summary_counts"]["total_items"] += 1
        mutations.append(value)
        for value in mutations:
            changed = replace(
                managed,
                content=json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
            )
            with self.subTest(fields=tuple(value)):
                with self.assertRaises(ArtifactVerificationRejected):
                    verifier.verify(changed)

    def test_run_verifier_rejects_candidate_missing_one_compiled_task_ref(self) -> None:
        projection = _projection()
        payload, _ = build_case_context_review_candidate(projection)
        extra_ref = f"fact:{uuid4()}"
        task_refs = projection.input_refs + (extra_ref,)
        helper = supervisor_fixture.CaseAgentSupervisorTests(
            methodName="runTest"
        )
        helper.setUp()
        task = helper.local_task()
        task = replace(
            task,
            input_refs=task_refs,
            input_hash=projection.task_input_hash,
            capability=replace(
                task.capability,
                reads_case_objects=task_refs,
                writes_managed_derivatives=False,
            ),
        )
        state = helper.state_with_graph(helper.graph((task,)))
        state = helper.start_ready_task(state, sequence=3)
        artifact = ArtifactReceipt(
            artifact_id=str(uuid4()),
            artifact_kind=CASE_CONTEXT_ARTIFACT_KIND,
            content_hash=sha256(payload).hexdigest(),
            byte_size=len(payload),
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        result = helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.SUCCEEDED,
            artifacts=(artifact,),
        )
        state = reduce_agent_event(
            state,
            helper.event(
                4,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(result),
            ),
        )
        verifier = CaseAgentRunVerifier(
            verifier_id="case-context-run-verifier",
            verifier_version="1.0.0",
            artifact_access=_ArtifactAccess(artifact, payload),
            artifact_verifiers={
                CASE_CONTEXT_ARTIFACT_KIND: CanonicalJsonArtifactVerifier(
                    artifact_kind=CASE_CONTEXT_ARTIFACT_KIND,
                    allowed_schema_versions=(CASE_CONTEXT_CANDIDATE_SCHEMA,),
                    payload_kind=CASE_CONTEXT_ARTIFACT_KIND,
                )
            },
            clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
        )
        result = verifier.verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=str(uuid4()),
            execution_actor_id=str(uuid4()),
        )
        self.assertEqual(result.outcome, VerificationOutcome.FAILED)
        self.assertEqual(
            result.error_code,
            "ARTIFACT_CASE_CONTEXT_SOURCE_BINDING_INVALID",
        )

    def test_skill_registry_stays_gated_without_server_wiring(self) -> None:
        disabled = default_case_skill_registry()
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            disabled.authorize_tool(
                skill_id="case_context_review",
                tool_id="review_case_context",
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        enabled = default_case_skill_registry(case_context_review_enabled=True)
        tool = enabled.authorize_tool(
            skill_id="case_context_review",
            tool_id="review_case_context",
            granted_scopes=frozenset({CapabilityScope.CASE_READ}),
            lawyer_approved=False,
            release_locked=False,
        )
        self.assertFalse(tool.allows_external_network)
        self.assertFalse(tool.mutates_originals)
        self.assertFalse(tool.writes_only_managed_derivatives)

    def test_postgres_binding_parser_accepts_only_structured_case_objects(self) -> None:
        parsed = _parse_refs(tuple(item.input_ref for item in _sources()))
        self.assertEqual(len(parsed), 9)
        for invalid in (
            (f"evidence-page:{uuid4()}",),
            (f"material:{uuid4()}",),
            ("https://example.com/source",),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CaseContextReviewBlocked):
                    _parse_refs(invalid)

    def test_postgres_projection_accepts_dedicated_legal_planning_tool(self) -> None:
        worker = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        port = PostgresCaseContextProjectionPort(
            dsn="postgresql://unused-during-construction",
            worker_actor=worker,
            required_tool_id="plan_authoritative_rule_research",
        )
        self.assertIn("tenant-scoped", repr(port))
        with self.assertRaisesRegex(ValueError, "Tool identity"):
            PostgresCaseContextProjectionPort(
                dsn="postgresql://unused-during-construction",
                worker_actor=worker,
                required_tool_id="model_selected_tool",
            )

    def test_postgres_task_binding_fences_worker_task_graph_and_snapshot(self) -> None:
        worker = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        run_id = str(uuid4())
        task_id = str(uuid4())
        graph_id = str(uuid4())
        matter_id = str(uuid4())
        input_hash = _digest("persisted-task-input")
        input_refs = (f"fact:{uuid4()}",)
        snapshot_hash = _digest("persisted-snapshot")
        graph_hash = _digest("persisted-graph")
        row = {
            "matter_id": matter_id,
            "input_refs": list(input_refs),
            "input_hash": input_hash,
            "tool_id": "review_case_context",
            "graph_id": graph_id,
            "graph_hash": graph_hash,
            "graph_snapshot_matter_version": 4,
            "graph_snapshot_schema_version": "case-ledger-snapshot-v1",
            "graph_snapshot_hash": snapshot_hash,
            "current_graph_id": graph_id,
            "current_graph_hash": graph_hash,
            "run_snapshot_matter_version": 4,
            "run_snapshot_schema_version": "case-ledger-snapshot-v1",
            "run_snapshot_hash": snapshot_hash,
            "matter_version": 4,
        }
        result = _read_task_binding(
            _Connection(row),
            worker=worker,
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            input_refs=input_refs,
        )
        self.assertEqual(result["matter_id"], matter_id)
        self.assertEqual(result["snapshot_hash"], snapshot_hash)

        stale = dict(row, current_graph_hash=_digest("different-graph"))
        with self.assertRaisesRegex(CaseContextReviewBlocked, "snapshot is stale"):
            _read_task_binding(
                _Connection(stale),
                worker=worker,
                run_id=run_id,
                task_id=task_id,
                task_input_hash=input_hash,
                input_refs=input_refs,
            )


if __name__ == "__main__":
    unittest.main()
