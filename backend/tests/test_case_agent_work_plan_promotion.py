from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_planner import PlanningInputStatus
from case_kernel.case_agent_planning_snapshot import (
    AuthoritativeCasePlanningProjection,
    AuthoritativePlanningObject,
    ConfirmedPostureProjection,
    PlanningProjectionObjectType,
    ProjectionSectionState,
    planning_object_ref_id,
)
from case_kernel.case_agent_supervisor import (
    AgentDeliverableKind,
    AgentRiskLevel,
    CaseSnapshotRef,
)
from case_kernel.case_agent_verifier import VerificationOutcome
from case_kernel.case_agent_work_plan_promotion import (
    AgentWorkPlanPromotionBlocked,
    VerifiedGraphPromotionSource,
    VerifiedGraphTask,
    compile_verified_graph_work_plan_candidate,
)
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked, _payload_hash
from case_kernel.case_work_plan import (
    DeliveryTarget,
    ResolvedWorkPlanReference,
    WorkPlanReference,
    WorkPlanReadiness,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
    validate_case_work_plan_candidate,
)
from case_kernel.case_work_plan_postgres import PostgresCaseWorkPlanStore
from case_kernel.models import Actor, Role
from case_kernel.skill_registry import ApprovalGate


def digest(label: str) -> str:
    return sha256(label.encode()).hexdigest()


class VerifiedGraphWorkPlanCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.profile_id = str(uuid4())
        self.material_id = str(uuid4())
        self.fact_id = str(uuid4())
        self.snapshot = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=17,
            snapshot_hash=digest("snapshot"),
            schema_version="case-ledger-snapshot-v1",
        )
        self.posture = ConfirmedPostureProjection(
            profile_id=self.profile_id,
            profile_version="v3",
            profile_hash=digest("posture"),
            effective_status="CURRENT",
            case_type_code="CIVIL.PRIVATE_LENDING",
            procedure_stage="FIRST_INSTANCE",
            represented_position="OTHER",
            authority_scope_code="GENERAL_AUTHORITY",
            engagement_state="ACTIVE",
        )
        self.posture_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.POSTURE_PROFILE,
            self.profile_id,
            "v3",
            self.posture.profile_hash,
            PlanningInputStatus.CONFIRMED,
        )
        self.material_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.MATERIAL_OBJECT,
            self.material_id,
            "v2",
            digest("material"),
            PlanningInputStatus.AVAILABLE,
        )
        self.fact_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.CASE_FACT,
            self.fact_id,
            "v17",
            digest("confirmed-fact"),
            PlanningInputStatus.CONFIRMED,
        )
        self.projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(self.posture_object, self.material_object, self.fact_object),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=self.posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.EMPTY,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        posture_ref = planning_object_ref_id(
            PlanningProjectionObjectType.POSTURE_PROFILE, self.profile_id
        )
        material_ref = planning_object_ref_id(
            PlanningProjectionObjectType.MATERIAL_OBJECT, self.material_id
        )
        first_id = str(uuid4())
        second_id = str(uuid4())
        self.source = VerifiedGraphPromotionSource(
            run_id=str(uuid4()),
            run_status="READY_FOR_REVIEW",
            run_is_stale=False,
            run_is_cancelled=False,
            graph_id=str(uuid4()),
            graph_version=4,
            graph_hash=digest("graph"),
            snapshot=self.snapshot,
            goal_id=str(uuid4()),
            goal_hash=digest("goal"),
            requested_deliverables=(),
            verification_receipt_id=str(uuid4()),
            verification_outcome=VerificationOutcome.PASSED,
            verification_graph_hash=digest("graph"),
            verification_snapshot_hash=digest("snapshot"),
            verification_hash=digest("verification"),
            run_verification_hash=digest("verification"),
            verifier_actor_id=str(uuid4()),
            execution_actor_id=str(uuid4()),
            verified_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
            tasks=(
                VerifiedGraphTask(
                    task_id=first_id,
                    sequence=1,
                    title="核对案卷材料范围",
                    purpose="确认后续分析使用的服务器材料范围。",
                    rationale="当前目标引用了已接收材料和律师确认态势。",
                    dependency_ids=(),
                    input_refs=(posture_ref, material_ref),
                    skill_id="common_document_read",
                    risk_level=AgentRiskLevel.MEDIUM,
                    approval_gate=ApprovalGate.LAWYER_REVIEW,
                ),
                VerifiedGraphTask(
                    task_id=second_id,
                    sequence=2,
                    title="形成内部案件审阅候选",
                    purpose="将已核对内容组织成供律师复核的内部工作成果。",
                    rationale="前置材料范围已经过独立验证。",
                    dependency_ids=(first_id,),
                    input_refs=(posture_ref, material_ref),
                    skill_id="document_drafting",
                    risk_level=AgentRiskLevel.LOW,
                    approval_gate=ApprovalGate.LAWYER_REVIEW,
                ),
            ),
        )

    def test_verified_graph_is_preserved_as_review_only_candidate(self) -> None:
        compiled = compile_verified_graph_work_plan_candidate(
            source=self.source, projection=self.projection
        )
        self.assertEqual(compiled.candidate.context.matter_version, 17)
        self.assertEqual(compiled.candidate.context.objective.goal_id, self.source.goal_id)
        self.assertEqual(
            [item.purpose for item in compiled.candidate.items],
            [task.purpose for task in self.source.tasks],
        )
        self.assertEqual(
            compiled.candidate.items[1].prerequisites,
            (self.source.tasks[0].task_id,),
        )
        self.assertEqual(
            compiled.candidate.items[1].delivery_target,
            DeliveryTarget.NOT_APPLICABLE,
        )
        self.assertFalse(compiled.candidate.items[1].required_for_delivery)
        self.assertFalse(compiled.candidate.items[1].is_primary_document)
        self.assertTrue(
            all(
                reference.source_type is WorkPlanSourceType.AGENT_TASK_INPUT
                for item in compiled.candidate.items
                for reference in item.source_refs
            )
        )

        resolved: dict[tuple[WorkPlanSourceType, str], ResolvedWorkPlanReference] = {}
        for reference in compiled.candidate.context.all_references:
            resolved[(reference.source_type, reference.source_id)] = ResolvedWorkPlanReference(
                matter_id=self.matter_id,
                source_type=reference.source_type,
                source_id=reference.source_id,
                source_version=reference.source_version,
                source_hash=reference.source_hash,
                is_current=True,
                is_confirmed=True,
            )
        plan = validate_case_work_plan_candidate(
            compiled.candidate,
            resolve_reference=lambda reference: resolved.get(
                (reference.source_type, reference.source_id)
            ),
        )
        self.assertEqual(plan.status, "CANDIDATE")
        self.assertEqual(plan.required_court_document_kinds, ())
        self.assertIsNone(plan.primary_court_document_kind)

    def test_evidence_catalogue_requires_confirmed_pages_and_keeps_them_as_review_sources(self) -> None:
        evidence_id = str(uuid4())
        evidence = AuthoritativePlanningObject(
            PlanningProjectionObjectType.EVIDENCE_PAGE,
            evidence_id,
            "v17",
            digest("approved-evidence-page"),
            PlanningInputStatus.CONFIRMED,
            source_media_type="application/pdf",
        )
        projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(*self.projection.objects, evidence),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=self.posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.EMPTY,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        source = replace(
            self.source,
            requested_deliverables=(AgentDeliverableKind.EVIDENCE_CATALOGUE,),
        )
        compiled = compile_verified_graph_work_plan_candidate(
            source=source,
            projection=projection,
        )
        item = next(
            value for value in compiled.candidate.items
            if value.deliverable_kind == "EVIDENCE_CATALOGUE"
        )
        self.assertEqual(item.readiness, WorkPlanReadiness.ACTIONABLE)
        self.assertEqual(item.delivery_target, DeliveryTarget.INTERNAL_WORK_PRODUCT)
        self.assertEqual(
            {reference.source_type for reference in item.source_refs},
            {WorkPlanSourceType.POSTURE_PROFILE, WorkPlanSourceType.AGENT_TASK_INPUT},
        )
        self.assertEqual(
            len(
                [
                    reference
                    for reference in item.source_refs
                    if reference.source_type is WorkPlanSourceType.AGENT_TASK_INPUT
                ]
            ),
            1,
        )
        graph_refs = {
            reference
            for task in source.tasks
            for reference in task.input_refs
        }
        extra_bindings = [
            binding
            for binding in compiled.bindings
            if binding.input_ref not in graph_refs
        ]
        self.assertEqual(len(extra_bindings), 1)
        self.assertEqual(
            extra_bindings[0].object_type,
            PlanningProjectionObjectType.EVIDENCE_PAGE,
        )
        self.assertEqual(extra_bindings[0].source_status, "CONFIRMED")
        self.assertEqual(extra_bindings[0].reference_use, WorkPlanReferenceUse.EVIDENCE)

    def test_repeated_verified_task_titles_remain_distinct_review_items(self) -> None:
        repeated = replace(
            self.source,
            tasks=(
                self.source.tasks[0],
                replace(
                    self.source.tasks[1],
                    title=self.source.tasks[0].title,
                    input_refs=self.source.tasks[0].input_refs,
                ),
            ),
        )

        compiled = compile_verified_graph_work_plan_candidate(
            source=repeated, projection=self.projection
        )
        self.assertEqual(
            [item.title for item in compiled.candidate.items],
            ["核对案卷材料范围（步骤 1）", "核对案卷材料范围（步骤 2）"],
        )
        self.assertEqual(
            [item.purpose for item in compiled.candidate.items],
            [task.purpose for task in repeated.tasks],
        )
        resolved: dict[tuple[WorkPlanSourceType, str], ResolvedWorkPlanReference] = {}
        for reference in compiled.candidate.context.all_references:
            resolved[(reference.source_type, reference.source_id)] = ResolvedWorkPlanReference(
                matter_id=self.matter_id,
                source_type=reference.source_type,
                source_id=reference.source_id,
                source_version=reference.source_version,
                source_hash=reference.source_hash,
                is_current=True,
                is_confirmed=True,
            )
        validate_case_work_plan_candidate(
            compiled.candidate,
            resolve_reference=lambda reference: resolved.get(
                (reference.source_type, reference.source_id)
            ),
        )

    def test_failed_stale_or_wrong_snapshot_graph_is_rejected(self) -> None:
        for changed in (
            replace(self.source, verification_outcome=VerificationOutcome.FAILED),
            replace(self.source, run_is_stale=True),
            replace(self.source, verification_graph_hash=digest("other-graph")),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(AgentWorkPlanPromotionBlocked):
                    compile_verified_graph_work_plan_candidate(
                        source=changed, projection=self.projection
                    )

    def test_graph_cannot_invent_source_or_omit_current_posture(self) -> None:
        unknown = replace(
            self.source,
            tasks=(
                replace(
                    self.source.tasks[0],
                    input_refs=(self.source.tasks[0].input_refs[0], f"fact:{uuid4()}"),
                ),
            ),
        )
        with self.assertRaisesRegex(AgentWorkPlanPromotionBlocked, "outside"):
            compile_verified_graph_work_plan_candidate(
                source=unknown, projection=self.projection
            )
        no_posture = replace(
            self.source,
            tasks=(
                replace(
                    self.source.tasks[0],
                    input_refs=(self.source.tasks[0].input_refs[1],),
                ),
            ),
        )
        with self.assertRaisesRegex(AgentWorkPlanPromotionBlocked, "posture"):
            compile_verified_graph_work_plan_candidate(
                source=no_posture, projection=self.projection
            )

    def test_compiler_does_not_infer_a_party_role_from_the_document_kind(self) -> None:
        source = inspect.getsource(compile_verified_graph_work_plan_candidate)
        self.assertNotIn("PLAINTIFF", source)
        self.assertNotIn("DEFENDANT", source)
        self.assertNotIn("CIVIL_COMPLAINT", source)
        self.assertIn("DEFENCE_STATEMENT", source)

    def test_requested_memo_requires_and_binds_confirmed_case_fact(self) -> None:
        compiled = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(AgentDeliverableKind.CASE_REVIEW_MEMO,),
            ),
            projection=self.projection,
        )
        memo = compiled.candidate.items[-1]
        self.assertEqual(memo.deliverable_kind, "CASE_REVIEW_MEMO")
        self.assertEqual(memo.readiness, WorkPlanReadiness.ACTIONABLE)
        fact_bindings = tuple(
            binding
            for binding in compiled.bindings
            if binding.object_type is PlanningProjectionObjectType.CASE_FACT
        )
        self.assertEqual(len(fact_bindings), 1)
        self.assertIn(
            fact_bindings[0].as_reference(),
            memo.source_refs,
        )
        self.assertEqual(
            fact_bindings[0].reference_use,
            WorkPlanReferenceUse.FACT,
        )
        no_fact_projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(self.posture_object, self.material_object),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=self.posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.EMPTY,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        blocked_candidate = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(AgentDeliverableKind.CASE_REVIEW_MEMO,),
            ),
            projection=no_fact_projection,
        )
        self.assertEqual(
            blocked_candidate.candidate.items[-1].readiness,
            WorkPlanReadiness.NEEDS_INFORMATION,
        )

    def test_requested_supplementary_evidence_checklist_reuses_confirmed_fact_boundary(self) -> None:
        compiled = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(
                    AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST,
                ),
            ),
            projection=self.projection,
        )
        checklist = compiled.candidate.items[-1]
        self.assertEqual(
            checklist.deliverable_kind,
            "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
        )
        self.assertEqual(checklist.readiness, WorkPlanReadiness.ACTIONABLE)
        self.assertEqual(checklist.delivery_target, DeliveryTarget.INTERNAL_WORK_PRODUCT)
        fact_binding = next(
            binding
            for binding in compiled.bindings
            if binding.object_type is PlanningProjectionObjectType.CASE_FACT
        )
        self.assertIn(fact_binding.as_reference(), checklist.source_refs)
        self.assertEqual(fact_binding.reference_use, WorkPlanReferenceUse.FACT)

    def test_requested_defence_binds_only_confirmed_response_sources(self) -> None:
        claim_id = str(uuid4())
        legal_source_id = str(uuid4())
        legal_rule_id = str(uuid4())
        defendant_posture = replace(
            self.posture,
            represented_position="DEFENDANT",
        )
        defendant_posture_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.POSTURE_PROFILE,
            self.profile_id,
            "v3",
            defendant_posture.profile_hash,
            PlanningInputStatus.CONFIRMED,
        )
        claim_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.CASE_CLAIM,
            claim_id,
            "v17",
            digest("confirmed-claim-with-approved-response"),
            PlanningInputStatus.CONFIRMED,
        )
        legal_source_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            legal_source_id,
            "v1",
            digest("verified-legal-source"),
            PlanningInputStatus.LOCKED,
        )
        legal_rule_object = AuthoritativePlanningObject(
            PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
            legal_rule_id,
            "v1",
            digest("approved-legal-rule"),
            PlanningInputStatus.LOCKED,
        )
        projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(
                defendant_posture_object,
                self.material_object,
                self.fact_object,
                claim_object,
                legal_source_object,
                legal_rule_object,
            ),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=defendant_posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.AVAILABLE,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        compiled = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            ),
            projection=projection,
        )
        defence = compiled.candidate.items[-1]
        self.assertEqual(defence.deliverable_kind, "DEFENCE_STATEMENT")
        self.assertEqual(defence.readiness, WorkPlanReadiness.ACTIONABLE)
        expected_types = {
            PlanningProjectionObjectType.CASE_FACT,
            PlanningProjectionObjectType.CASE_CLAIM,
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
        }
        defence_refs = set(defence.source_refs)
        self.assertIn(
            compiled.candidate.context.posture.as_reference(),
            defence_refs,
        )
        self.assertTrue(
            all(
                binding.as_reference() in defence_refs
                for binding in compiled.bindings
                if binding.object_type in expected_types
            )
        )
        self.assertTrue(
            all(
                binding.reference_use
                in {
                    WorkPlanReferenceUse.FACT,
                    WorkPlanReferenceUse.CLAIM_SCOPE,
                    WorkPlanReferenceUse.LEGAL_AUTHORITY,
                    WorkPlanReferenceUse.LEGAL_RULE,
                }
                for binding in compiled.bindings
                if binding.object_type in expected_types
            )
        )

        incomplete_projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(
                defendant_posture_object,
                self.material_object,
                self.fact_object,
                claim_object,
            ),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=defendant_posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.EMPTY,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        incomplete = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            ),
            projection=incomplete_projection,
        ).candidate.items[-1]
        self.assertEqual(incomplete.readiness, WorkPlanReadiness.NEEDS_INFORMATION)

        wrong_posture_projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot,
            closing_case_snapshot=self.snapshot,
            objects=(
                self.posture_object,
                self.material_object,
                self.fact_object,
                claim_object,
                legal_source_object,
                legal_rule_object,
            ),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=self.posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.AVAILABLE,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        wrong_posture = compile_verified_graph_work_plan_candidate(
            source=replace(
                self.source,
                requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            ),
            projection=wrong_posture_projection,
        ).candidate.items[-1]
        self.assertEqual(wrong_posture.readiness, WorkPlanReadiness.NEEDS_INFORMATION)

class _Cursor:
    def __init__(self, *, row=None, rows=()) -> None:
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _PromotionConnection:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.fixture = fixture
        self.executed: list[str] = []

    def execute(self, sql: str, params=()) -> _Cursor:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if "case_agent_ledger_extraction_run_review_resolved" in normalized:
            return _Cursor(
                row={"review_resolved": self.fixture.get("review_resolved", True)}
            )
        if "FROM case_agent_work_plan_promotions" in normalized and normalized.startswith(
            "SELECT promotion_id"
        ):
            return _Cursor(row=self.fixture["promotion"])
        if "FROM case_agent_runs agent_run" in normalized:
            return _Cursor(row=self.fixture["source"])
        if "FROM case_agent_tasks task" in normalized and normalized.startswith(
            "SELECT task.task_id"
        ):
            return _Cursor(rows=self.fixture["tasks"])
        if "FROM case_agent_task_dependencies" in normalized:
            return _Cursor(rows=self.fixture.get("dependencies", ()))
        if "FROM case_agent_work_plan_input_bindings" in normalized:
            return _Cursor(rows=self.fixture["bindings"])
        if "FROM case_work_plan_context_references" in normalized:
            return _Cursor(rows=self.fixture["context"])
        if "FROM case_work_plan_items" in normalized:
            return _Cursor(rows=self.fixture.get("deliverable_items", ()))
        raise AssertionError(f"unexpected SQL: {normalized}")


class AgentWorkPlanPostgresGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.run_id = str(uuid4())
        self.graph_id = str(uuid4())
        self.goal_id = str(uuid4())
        self.profile_id = str(uuid4())
        self.receipt_id = str(uuid4())
        self.verifier_id = str(uuid4())
        self.execution_id = str(uuid4())
        self.task_id = str(uuid4())
        self.promotion_id = str(uuid4())
        self.plan_id = str(uuid4())
        self.plan_version = 23
        self.graph_version = 4
        self.graph_hash = digest("graph")
        self.snapshot_hash = digest("snapshot")
        self.verification_hash = digest("verification")
        self.goal_hash = digest("goal")
        self.profile_hash = digest("profile")
        self.input_ref = f"posture-profile:{self.profile_id}"
        self.binding_id = str(uuid5(UUID(self.graph_id), self.input_ref))
        self.binding_payload = {
            "schema_version": "case-agent-work-plan-input-binding-v1",
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_hash": self.graph_hash,
            "snapshot_hash": self.snapshot_hash,
            "verification_hash": self.verification_hash,
            "binding_id": self.binding_id,
            "input_ref": self.input_ref,
            "object_type": "POSTURE_PROFILE",
            "object_id": self.profile_id,
            "object_version": "v2",
            "content_hash": self.profile_hash,
            "source_status": "CONFIRMED",
            "reference_use": "POSTURE",
        }
        self.binding_hash = _payload_hash(self.binding_payload)
        self.fixture = {
            "promotion": {
                "promotion_id": self.promotion_id,
                "run_id": self.run_id,
                "graph_id": self.graph_id,
                "graph_version": self.graph_version,
                "graph_hash": self.graph_hash,
                "snapshot_matter_version": self.plan_version,
                "snapshot_schema_version": "case-ledger-snapshot-v1",
                "snapshot_hash": self.snapshot_hash,
                "goal_id": self.goal_id,
                "goal_hash": self.goal_hash,
                "posture_profile_id": self.profile_id,
                "posture_profile_version": 2,
                "posture_profile_hash": self.profile_hash,
                "verification_receipt_id": self.receipt_id,
                "verification_hash": self.verification_hash,
                "verifier_actor_id": self.verifier_id,
                "execution_actor_id": self.execution_id,
                "task_count": 1,
            },
            "source": {
                "run_status": "READY_FOR_REVIEW",
                "run_is_stale": False,
                "run_is_cancelled": False,
                "run_snapshot_matter_version": self.plan_version,
                "run_snapshot_schema_version": "case-ledger-snapshot-v1",
                "run_snapshot_hash": self.snapshot_hash,
                "current_graph_id": self.graph_id,
                "current_graph_version": self.graph_version,
                "current_graph_hash": self.graph_hash,
                "run_verification_hash": self.verification_hash,
                "graph_id": self.graph_id,
                "graph_version": self.graph_version,
                "graph_hash": self.graph_hash,
                "graph_goal_hash": self.goal_hash,
                "graph_snapshot_matter_version": self.plan_version,
                "graph_snapshot_schema_version": "case-ledger-snapshot-v1",
                "graph_snapshot_hash": self.snapshot_hash,
                "goal_id": self.goal_id,
                "goal_hash": self.goal_hash,
                "requested_deliverables": [],
                "verification_receipt_id": self.receipt_id,
                "outcome": "PASSED",
                "verification_graph_hash": self.graph_hash,
                "verification_snapshot_hash": self.snapshot_hash,
                "verification_hash": self.verification_hash,
                "verifier_actor_id": self.verifier_id,
                "execution_actor_id": self.execution_id,
                "verified_at": datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
            },
            "tasks": (
                {
                    "task_id": self.task_id,
                    "sequence": 1,
                    "title": "核对当前态势",
                    "purpose": "确认后续工作使用的律师已确认态势。",
                    "rationale": "当前目标必须绑定可审计的态势输入。",
                    "input_refs": [self.input_ref],
                    "skill_id": "common_document_read",
                    "risk_level": "LOW",
                    "approval_gate": "LAWYER_REVIEW",
                },
            ),
            "bindings": (
                {
                    "binding_id": self.binding_id,
                    "input_ref": self.input_ref,
                    "object_type": "POSTURE_PROFILE",
                    "object_id": self.profile_id,
                    "object_version": "v2",
                    "content_hash": self.profile_hash,
                    "source_status": "CONFIRMED",
                    "reference_use": "POSTURE",
                    "binding_hash": self.binding_hash,
                },
            ),
            "context": (
                {
                    "source_id": self.binding_id,
                    "source_version": "v2",
                    "source_hash": self.binding_hash,
                    "reference_use": "POSTURE",
                },
            ),
        }
        self.store = PostgresCaseWorkPlanStore("postgresql://unused")
        self.lead = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.plan = {
            "plan_id": self.plan_id,
            "planned_matter_version": self.plan_version,
            "agent_goal_id": self.goal_id,
            "objective_hash": self.goal_hash,
            "profile_id": self.profile_id,
            "profile_version": 2,
            "profile_hash": self.profile_hash,
        }

    def test_activation_reloads_current_graph_goal_passed_receipt_and_bindings(self) -> None:
        connection = _PromotionConnection(self.fixture)
        self.store._assert_current_agent_promotion(
            connection,
            actor=self.lead,
            matter_id=self.matter_id,
            plan=self.plan,
            expected_version=self.plan_version + 1,
        )
        self.assertTrue(any("case_agent_verification_receipts" in sql for sql in connection.executed))
        self.assertTrue(any("case_agent_work_plan_input_bindings" in sql for sql in connection.executed))

    def test_activation_rejects_non_passed_current_receipt(self) -> None:
        self.fixture["source"] = {
            **self.fixture["source"],
            "outcome": "FAILED",
        }
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "PASSED"):
            self.store._assert_current_agent_promotion(
                _PromotionConnection(self.fixture),
                actor=self.lead,
                matter_id=self.matter_id,
                plan=self.plan,
                expected_version=self.plan_version + 1,
            )

    def test_activation_rejects_open_agent_ledger_review_before_reloading_graph(self) -> None:
        self.fixture["review_resolved"] = False
        connection = _PromotionConnection(self.fixture)
        with self.assertRaisesRegex(
            CaseLedgerPersistenceBlocked, "open ledger review"
        ):
            self.store._assert_current_agent_promotion(
                connection,
                actor=self.lead,
                matter_id=self.matter_id,
                plan=self.plan,
                expected_version=self.plan_version + 1,
            )
        self.assertFalse(
            any("FROM case_agent_runs agent_run" in sql for sql in connection.executed)
        )

    def test_activation_requires_every_structured_deliverable_to_be_actionable(self) -> None:
        source_fixture = VerifiedGraphWorkPlanCompilerTests(methodName="runTest")
        source_fixture.setUp()
        source = replace(
            source_fixture.source,
            requested_deliverables=(
                AgentDeliverableKind.CASE_REVIEW_MEMO,
                AgentDeliverableKind.PAYMENT_LEDGER,
            ),
        )
        memo_item_id, ledger_item_id = str(uuid4()), str(uuid4())
        fact_binding_id, transaction_binding_id = str(uuid4()), str(uuid4())

        class DeliverableConnection:
            def __init__(inner_self, *, ledger_readiness: str) -> None:
                inner_self.ledger_readiness = ledger_readiness

            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if "FROM case_work_plan_items" in normalized:
                    return _Cursor(
                        rows=(
                            {
                                "item_id": memo_item_id,
                                "item_kind": "DOCUMENT_CANDIDATE",
                                "readiness": "ACTIONABLE",
                                "delivery_target": "INTERNAL_WORK_PRODUCT",
                                "deliverable_kind": "CASE_REVIEW_MEMO",
                                "required_for_delivery": False,
                            },
                            {
                                "item_id": ledger_item_id,
                                "item_kind": "DOCUMENT_CANDIDATE",
                                "readiness": inner_self.ledger_readiness,
                                "delivery_target": "INTERNAL_WORK_PRODUCT",
                                "deliverable_kind": "PAYMENT_LEDGER",
                                "required_for_delivery": False,
                            },
                        )
                    )
                if "FROM case_work_plan_item_references" in normalized:
                    use = params[-1]
                    return _Cursor(
                        rows=(
                            {
                                "source_id": (
                                    fact_binding_id
                                    if use == "FACT"
                                    else transaction_binding_id
                                )
                            },
                        )
                    )
                raise AssertionError(normalized)

        complete_bindings = (
            {
                "binding_id": fact_binding_id,
                "object_type": "CASE_FACT",
                "source_status": "CONFIRMED",
            },
            {
                "binding_id": transaction_binding_id,
                "object_type": "CASE_TRANSACTION",
                "source_status": "CONFIRMED",
            },
        )
        self.store._assert_requested_deliverable_items(
            DeliverableConnection(ledger_readiness="NEEDS_INFORMATION"),
            actor=self.lead,
            matter_id=self.matter_id,
            plan_id=self.plan_id,
            source=source,
            binding_rows=complete_bindings[:1],
        )

        self.store._assert_requested_deliverable_items(
            DeliverableConnection(ledger_readiness="ACTIONABLE"),
            actor=self.lead,
            matter_id=self.matter_id,
            plan_id=self.plan_id,
            source=source,
            binding_rows=complete_bindings,
        )

    def test_activation_requires_all_defence_source_classes_and_exact_bindings(self) -> None:
        """A defensible pleading cannot activate from facts alone.

        This mirrors the production promotion shape instead of trusting the
        compiler's readiness label: the activation transaction must still see
        every governed source class and each work-plan source reference must
        bind the complete current set for that class.
        """
        source_fixture = VerifiedGraphWorkPlanCompilerTests(methodName="runTest")
        source_fixture.setUp()
        source = replace(
            source_fixture.source,
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
        )
        item_id = str(uuid4())
        bindings_by_use = {
            "FACT": {str(uuid4()), str(uuid4())},
            "CLAIM_SCOPE": {str(uuid4())},
            "LEGAL_AUTHORITY": {str(uuid4())},
            "LEGAL_RULE": {str(uuid4()), str(uuid4())},
        }
        binding_rows = tuple(
            {
                "binding_id": binding_id,
                "object_type": object_type,
                "source_status": source_status,
            }
            for reference_use, object_type, source_status in (
                ("FACT", "CASE_FACT", "CONFIRMED"),
                ("CLAIM_SCOPE", "CASE_CLAIM", "CONFIRMED"),
                ("LEGAL_AUTHORITY", "VERIFIED_LEGAL_SOURCE", "LOCKED"),
                ("LEGAL_RULE", "APPROVED_LEGAL_RULE", "LOCKED"),
            )
            for binding_id in bindings_by_use[reference_use]
        )

        class DefenceDeliverableConnection:
            def __init__(inner_self, *, missing_use: str | None = None) -> None:
                inner_self.missing_use = missing_use

            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if "FROM case_work_plan_items" in normalized:
                    return _Cursor(
                        rows=(
                            {
                                "item_id": item_id,
                                "item_kind": "DOCUMENT_CANDIDATE",
                                "readiness": "ACTIONABLE",
                                "delivery_target": "INTERNAL_WORK_PRODUCT",
                                "deliverable_kind": "DEFENCE_STATEMENT",
                            },
                        )
                    )
                if "FROM case_work_plan_item_references" in normalized:
                    use = str(params[-1])
                    ids = () if use == inner_self.missing_use else bindings_by_use[use]
                    return _Cursor(rows=tuple({"source_id": value} for value in ids))
                raise AssertionError(normalized)

        self.store._assert_requested_deliverable_items(
            DefenceDeliverableConnection(),
            actor=self.lead,
            matter_id=self.matter_id,
            plan_id=self.plan_id,
            source=source,
            binding_rows=binding_rows,
        )
        with self.assertRaisesRegex(
            CaseLedgerPersistenceBlocked,
            "does not bind its complete confirmed source set",
        ):
            self.store._assert_requested_deliverable_items(
                DefenceDeliverableConnection(missing_use="LEGAL_RULE"),
                actor=self.lead,
                matter_id=self.matter_id,
                plan_id=self.plan_id,
                source=source,
                binding_rows=binding_rows,
            )

    def test_activation_dispatches_agent_goal_away_from_lawyer_objective(self) -> None:
        plan_row = {
            **self.plan,
            "plan_version": 1,
            "status": "CANDIDATE",
            "objective_approval_id": None,
            "plan_hash": digest("plan"),
        }

        class ActivationConnection:
            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if "FROM matters m JOIN matter_actor_roles" in normalized:
                    return _Cursor(row={"ok": 1})
                if normalized.startswith("SELECT plan_id, plan_version, status"):
                    return _Cursor(row=plan_row)
                if normalized.startswith("SELECT current_plan_id FROM case_work_plan_heads"):
                    return _Cursor(row={"current_plan_id": None})
                return _Cursor()

        connection = ActivationConnection()

        @contextmanager
        def transaction(_firm_id):
            yield connection

        receipt = object()
        with (
            patch.object(self.store, "_transaction", side_effect=transaction),
            patch.object(self.store, "_begin", return_value=None),
            patch.object(self.store, "_assert_current_profile") as profile_guard,
            patch.object(self.store, "_assert_current_agent_promotion") as agent_guard,
            patch.object(self.store, "_assert_current_objective") as objective_guard,
            patch.object(self.store, "_assert_persisted_references_current") as refs_guard,
            patch(
                "case_kernel.case_work_plan_postgres._finish_command",
                return_value=receipt,
            ),
        ):
            result = self.store.activate_plan(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=self.plan_version + 1,
                idempotency_key="activate-agent-plan",
                plan_id=self.plan_id,
                confirmation_hash=plan_row["plan_hash"],
            )
        self.assertIs(result, receipt)
        profile_guard.assert_called_once()
        agent_guard.assert_called_once()
        objective_guard.assert_not_called()
        refs_guard.assert_called_once()

    def test_activation_aborts_before_any_plan_write_when_deliverable_is_not_actionable(self) -> None:
        plan_row = {
            **self.plan,
            "plan_version": 1,
            "status": "CANDIDATE",
            "objective_approval_id": None,
            "plan_hash": digest("plan"),
        }

        class ActivationConnection:
            def __init__(inner_self) -> None:
                inner_self.executed: list[str] = []

            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                inner_self.executed.append(normalized)
                if "FROM matters m JOIN matter_actor_roles" in normalized:
                    return _Cursor(row={"ok": 1})
                if normalized.startswith("SELECT plan_id, plan_version, status"):
                    return _Cursor(row=plan_row)
                return _Cursor()

        connection = ActivationConnection()

        @contextmanager
        def transaction(_firm_id):
            yield connection

        with (
            patch.object(self.store, "_transaction", side_effect=transaction),
            patch.object(self.store, "_begin", return_value=None),
            patch.object(self.store, "_assert_current_profile"),
            patch.object(
                self.store,
                "_assert_current_agent_promotion",
                side_effect=CaseLedgerPersistenceBlocked(
                    "Agent requested deliverables still need confirmed sources; "
                    "refresh and promote a new plan"
                ),
            ),
            patch(
                "case_kernel.case_work_plan_postgres._finish_command"
            ) as finish,
        ):
            with self.assertRaisesRegex(
                CaseLedgerPersistenceBlocked, "still need confirmed sources"
            ):
                self.store.activate_plan(
                    matter_id=self.matter_id,
                    actor=self.lead,
                    expected_version=self.plan_version + 1,
                    idempotency_key="activate-nonactionable-deliverables",
                    plan_id=self.plan_id,
                    confirmation_hash=plan_row["plan_hash"],
                )
        finish.assert_not_called()
        self.assertFalse(
            any(
                sql.startswith("UPDATE case_work_plans")
                or sql.startswith("UPDATE case_work_plan_heads")
                for sql in connection.executed
            )
        )

    def test_promotion_command_does_not_accept_client_owned_identity_or_hashes(self) -> None:
        parameters = inspect.signature(
            PostgresCaseWorkPlanStore.promote_verified_graph
        ).parameters
        self.assertEqual(
            set(parameters),
            {
                "self",
                "matter_id",
                "actor",
                "expected_version",
                "idempotency_key",
                "run_id",
            },
        )
        for forbidden in ("firm_id", "graph_hash", "snapshot_hash", "profile_hash", "candidate"):
            self.assertNotIn(forbidden, parameters)

    def test_promotion_command_appends_candidate_but_never_activates_it(self) -> None:
        compiler_fixture = VerifiedGraphWorkPlanCompilerTests(methodName="runTest")
        compiler_fixture.setUp()
        worker = Actor(
            str(uuid4()),
            compiler_fixture.firm_id,
            frozenset({Role.SYSTEM_WORKER}),
        )
        store = PostgresCaseWorkPlanStore("postgresql://unused")

        class PromotionCommandConnection:
            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if "case_agent_ledger_extraction_run_review_resolved" in normalized:
                    return _Cursor(row={"review_resolved": True})
                if normalized.startswith(
                    "SELECT plan_id FROM case_agent_work_plan_promotions"
                ):
                    return _Cursor(row=None)
                raise AssertionError(f"unexpected SQL: {normalized}")

        connection = PromotionCommandConnection()

        @contextmanager
        def transaction(_firm_id):
            yield connection

        def resolve(reference: WorkPlanReference) -> ResolvedWorkPlanReference:
            return ResolvedWorkPlanReference(
                matter_id=compiler_fixture.matter_id,
                source_type=reference.source_type,
                source_id=reference.source_id,
                source_version=reference.source_version,
                source_hash=reference.source_hash,
                is_current=True,
                is_confirmed=True,
            )

        command_receipt = object()
        with (
            patch.object(store, "_transaction", side_effect=transaction),
            patch.object(store, "_begin", return_value=None),
            patch.object(
                store,
                "_read_verified_promotion_source",
                return_value=compiler_fixture.source,
            ),
            patch(
                "case_kernel.case_work_plan_postgres."
                "read_authoritative_projection_in_transaction",
                return_value=compiler_fixture.projection,
            ),
            patch.object(
                store, "_resolve_promotion_reference", side_effect=lambda *_args, **kwargs: resolve(kwargs["reference"])
            ),
            patch.object(store, "_append_candidate", return_value=1) as append,
            patch.object(store, "_insert_promotion") as insert_promotion,
            patch(
                "case_kernel.case_work_plan_postgres._finish_command",
                return_value=command_receipt,
            ),
        ):
            result = store.promote_verified_graph(
                matter_id=compiler_fixture.matter_id,
                actor=worker,
                expected_version=compiler_fixture.snapshot.matter_version,
                idempotency_key="promote-passed-graph",
                run_id=compiler_fixture.source.run_id,
            )

        self.assertIs(result, command_receipt)
        appended = append.call_args.kwargs["plan"]
        self.assertEqual(appended.status, "CANDIDATE")
        self.assertEqual(appended.required_court_document_kinds, ())
        self.assertIsNone(appended.primary_court_document_kind)
        insert_promotion.assert_called_once()


if __name__ == "__main__":
    unittest.main()
