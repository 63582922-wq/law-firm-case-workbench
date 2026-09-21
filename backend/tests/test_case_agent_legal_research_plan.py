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
    CaseContextSourceType,
)
from case_kernel.case_agent_legal_research_plan import (
    LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
    LEGAL_RESEARCH_PLAN_SCHEMA,
    LEGAL_RESEARCH_PLANNING_SKILL_ID,
    LEGAL_RESEARCH_PLANNING_TOOL_ID,
    LegalResearchPlanBlocked,
    build_legal_research_plan_candidate,
    legal_research_plan_source_refs,
    parse_legal_research_plan_candidate,
)
from case_kernel.case_agent_legal_research_plan_adapters import (
    LEGAL_RESEARCH_PLANNING_MANIFEST,
    DeterministicLegalResearchPlanningTaskAdapter,
    LegalResearchPlanningAdapterBlocked,
)
from case_kernel.case_agent_planner import PlanningInputStatus
from case_kernel.case_agent_skill_adapters import (
    ReviewCandidateStagingRequest,
    StagedReviewCandidate,
)
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    AgentAutonomyLevel,
    AgentEventType,
    AgentRiskLevel,
    AgentTaskSpec,
    ArtifactReceipt,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
    RetryMode,
    SkillBinding,
    TaskCapabilityContract,
    TaskResourceBudget,
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
from case_kernel.skill_registry import ApprovalGate, CapabilityScope

from backend.tests import test_case_agent_supervisor as supervisor_fixture


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source(
    source_type: CaseContextSourceType,
    status: PlanningInputStatus,
    primary_text: str,
    *,
    prefix: str,
) -> BoundCaseContextSource:
    object_id = str(uuid4())
    return BoundCaseContextSource(
        input_ref=f"{prefix}:{object_id}",
        source_type=source_type,
        object_id=object_id,
        object_version="v4",
        content_hash=_digest(f"{source_type.value}:{object_id}:{primary_text}"),
        status=status,
        primary_text=primary_text,
        secondary_text=f"{primary_text}；该文本只在案件边界内使用。",
        signals=(status.value,),
    )


def _sources(*, confirmed_issue: bool = False) -> tuple[BoundCaseContextSource, ...]:
    posture = _source(
        CaseContextSourceType.POSTURE_PROFILE,
        PlanningInputStatus.CONFIRMED,
        "买卖合同纠纷｜被告代理｜一审",
        prefix="posture-profile",
    )
    if confirmed_issue:
        return (
            posture,
            _source(
                CaseContextSourceType.DISPUTE_ISSUE,
                PlanningInputStatus.CONFIRMED,
                "货物是否完成交付以及付款义务是否到期",
                prefix="issue",
            ),
        )
    return (
        posture,
        _source(
            CaseContextSourceType.CASE_FACT,
            PlanningInputStatus.CONFIRMED,
            "飞腾陈主张已交付货物并请求支付30000元货款",
            prefix="fact",
        ),
    )


def _projection(
    sources: tuple[BoundCaseContextSource, ...],
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    task_input_hash: str | None = None,
) -> BoundCaseContextProjection:
    return BoundCaseContextProjection.build(
        run_id=run_id or str(uuid4()),
        task_id=task_id or str(uuid4()),
        task_input_hash=task_input_hash or _digest("legal-research-task"),
        firm_id=str(uuid4()),
        matter_id=str(uuid4()),
        matter_version=28,
        case_snapshot_hash=_digest("case-v28"),
        input_refs=tuple(item.input_ref for item in sources),
        sources=sources,
    )


class _ProjectionPort:
    def __init__(self, sources: tuple[BoundCaseContextSource, ...]) -> None:
        self.sources = sources
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.calls: list[dict[str, object]] = []

    def project_case_context(self, **kwargs):
        self.calls.append(kwargs)
        return BoundCaseContextProjection.build(
            **kwargs,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            matter_version=28,
            case_snapshot_hash=_digest("case-v28"),
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


class _ArtifactAccess:
    def __init__(self, artifact: ArtifactReceipt, payload: bytes) -> None:
        self.artifact = artifact
        self.payload = payload

    def read_managed_artifact(self, *, artifact, **_):
        if artifact != self.artifact:
            raise AssertionError("unexpected artifact")
        return ManagedArtifactRead(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=self.payload,
            source_input_hash=artifact.source_input_hash,
            object_receipt_hash=_digest("legal-research-managed-object"),
            media_type="application/json",
        )


def _task(input_refs: tuple[str, ...]) -> AgentTaskSpec:
    task_id = str(uuid4())
    return AgentTaskSpec(
        task_id=task_id,
        sequence=1,
        title="官方法源研究候选规划",
        purpose="形成不出网的研究候选",
        rationale="外部搜索另行批准",
        dependency_ids=(),
        input_refs=input_refs,
        input_hash=_digest(f"legal-research:{task_id}"),
        skill=SkillBinding(
            skill_id=LEGAL_RESEARCH_PLANNING_SKILL_ID,
            skill_version="1.1.0",
            tool_id=LEGAL_RESEARCH_PLANNING_TOOL_ID,
            tool_version="1.0.0",
            adapter_id=LEGAL_RESEARCH_PLANNING_MANIFEST.adapter_id,
            adapter_version=LEGAL_RESEARCH_PLANNING_MANIFEST.adapter_version,
        ),
        granted_scopes=frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
        capability=TaskCapabilityContract(
            execution_mode=AdapterExecutionMode.IN_PROCESS,
            network_policy=NetworkPolicy.DENY,
            allowed_domains=(),
            sandbox_profile="case-agent-legal-research-planning-v1",
            sandbox_policy_version=LEGAL_RESEARCH_PLANNING_MANIFEST.sandbox_policy_version,
            sandbox_policy_hash=LEGAL_RESEARCH_PLANNING_MANIFEST.sandbox_policy_hash,
            reads_case_objects=input_refs,
            writes_managed_derivatives=False,
            external_request_approval_required=False,
        ),
        risk_level=AgentRiskLevel.LOW,
        autonomy_level=AgentAutonomyLevel.A1_PROPOSE,
        approval_gate=ApprovalGate.NONE,
        retry_mode=RetryMode.IDEMPOTENT,
        budget=TaskResourceBudget(3, 120, 0, 0, 4 * 1024 * 1024),
    )


class LegalResearchPlanContractTests(unittest.TestCase):
    def test_fact_anchor_public_terms_never_export_private_case_text(self) -> None:
        projection = _projection(_sources())
        payload, source_hash = build_legal_research_plan_candidate(projection)
        parsed = parse_legal_research_plan_candidate(payload)

        self.assertEqual(source_hash, parsed["source_hash"])
        self.assertFalse(parsed["network_access"])
        self.assertFalse(parsed["legal_conclusion"])
        self.assertFalse(parsed["court_ready"])
        self.assertEqual(
            parsed["questions"][0]["status"], "NEEDS_ISSUE_CONFIRMATION"
        )
        self.assertIn("买卖合同", parsed["public_terms"])
        self.assertNotIn("飞腾陈", json.dumps(parsed["public_terms"], ensure_ascii=False))
        self.assertNotIn("30000", json.dumps(parsed["public_terms"], ensure_ascii=False))
        self.assertEqual(
            legal_research_plan_source_refs(payload), frozenset(projection.input_refs)
        )

    def test_confirmed_issue_is_ready_only_for_separate_search_approval(self) -> None:
        parsed = parse_legal_research_plan_candidate(
            build_legal_research_plan_candidate(
                _projection(_sources(confirmed_issue=True))
            )[0]
        )
        self.assertEqual(
            parsed["questions"][0]["status"], "READY_FOR_LAWYER_APPROVAL"
        )
        self.assertTrue(parsed["controls"]["external_search_separate_approval"])
        self.assertTrue(parsed["controls"]["search_leads_are_not_authority"])
        self.assertEqual(parsed["summary_counts"]["ready_for_search_approval"], 1)

    def test_parser_rejects_network_terms_and_source_binding_tampering(self) -> None:
        payload = build_legal_research_plan_candidate(_projection(_sources()))[0]
        mutations: list[dict[str, object]] = []

        changed = json.loads(payload)
        changed["network_access"] = True
        mutations.append(changed)

        changed = json.loads(payload)
        changed["public_terms"].append("飞腾陈")
        mutations.append(changed)

        changed = json.loads(payload)
        changed["questions"][0]["source_refs"].append(f"fact:{uuid4()}")
        mutations.append(changed)

        for changed in mutations:
            with self.subTest(fields=tuple(changed)):
                with self.assertRaises(LegalResearchPlanBlocked):
                    parse_legal_research_plan_candidate(_canonical(changed))

    def test_registered_verifier_rejects_legal_conclusion_escalation(self) -> None:
        payload = build_legal_research_plan_candidate(_projection(_sources()))[0]
        managed = ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
            content=payload,
            source_input_hash=json.loads(payload)["task_input_hash"],
            object_receipt_hash=_digest("managed-object"),
            media_type="application/json",
        )
        verifier = CanonicalJsonArtifactVerifier(
            artifact_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
            allowed_schema_versions=(LEGAL_RESEARCH_PLAN_SCHEMA,),
            payload_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
        )
        verifier.verify(managed)

        changed = json.loads(payload)
        changed["legal_conclusion"] = True
        with self.assertRaises(ArtifactVerificationRejected):
            verifier.verify(replace(managed, content=_canonical(changed)))


class LegalResearchPlanningAdapterTests(unittest.TestCase):
    def test_adapter_is_local_zero_cost_and_idempotent(self) -> None:
        sources = _sources()
        task = _task(tuple(item.input_ref for item in sources))
        claim = SimpleNamespace(run_id=str(uuid4()), task_id=task.task_id)
        context = SimpleNamespace(claim=claim, task=task, input_refs=task.input_refs)
        projection = _ProjectionPort(sources)
        staging = _Staging()
        adapter = DeterministicLegalResearchPlanningTaskAdapter(
            projection_port=projection,
            staging_port=staging,
        )

        first = adapter.execute(context=context)
        second = adapter.execute(context=context)

        self.assertEqual(first.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            first.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE
        )
        self.assertEqual(first.external_calls, 0)
        self.assertEqual(first.cost_minor_units, 0)
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(
            first.artifacts[0].artifact_kind, LEGAL_RESEARCH_PLAN_ARTIFACT_KIND
        )
        self.assertFalse(first.artifacts[0].managed_derivative)
        self.assertEqual(staging.requests[0].payload, staging.requests[1].payload)
        self.assertEqual(projection.calls[0]["input_refs"], task.input_refs)

    def test_adapter_rejects_any_network_capability(self) -> None:
        sources = _sources()
        task = _task(tuple(item.input_ref for item in sources))
        task = replace(
            task,
            capability=replace(
                task.capability,
                network_policy=NetworkPolicy.EXACT_ALLOWLIST,
                allowed_domains=("example.com",),
                external_request_approval_required=True,
            ),
        )
        context = SimpleNamespace(
            claim=SimpleNamespace(run_id=str(uuid4()), task_id=task.task_id),
            task=task,
            input_refs=task.input_refs,
        )
        adapter = DeterministicLegalResearchPlanningTaskAdapter(
            projection_port=_ProjectionPort(sources),
            staging_port=_Staging(),
        )
        with self.assertRaises(LegalResearchPlanningAdapterBlocked):
            adapter.execute(context=context)

    def test_run_verifier_requires_every_exact_compiled_source_ref(self) -> None:
        projection = _projection(_sources())
        payload = build_legal_research_plan_candidate(projection)[0]
        extra_ref = f"fact:{uuid4()}"
        task_refs = projection.input_refs + (extra_ref,)
        helper = supervisor_fixture.CaseAgentSupervisorTests(methodName="runTest")
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
            artifact_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
            content_hash=sha256(payload).hexdigest(),
            byte_size=len(payload),
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        receipt = helper.result_receipt(
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
                TaskResultPayload(receipt),
            ),
        )
        verifier = CaseAgentRunVerifier(
            verifier_id="legal-research-run-verifier",
            verifier_version="1.0.0",
            artifact_access=_ArtifactAccess(artifact, payload),
            artifact_verifiers={
                LEGAL_RESEARCH_PLAN_ARTIFACT_KIND: CanonicalJsonArtifactVerifier(
                    artifact_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
                    allowed_schema_versions=(LEGAL_RESEARCH_PLAN_SCHEMA,),
                    payload_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
                )
            },
            clock=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
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
            "ARTIFACT_LEGAL_RESEARCH_PLAN_SOURCE_BINDING_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
