from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_lawyer_analysis_adapters import LAWYER_ANALYSIS_SKILL_ID
from case_kernel.case_agent_planner import (
    CasePlanningSignal,
    PlanningSignalCategory,
    CasePlanProposal,
    CasePlannerBlocked,
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlannerSemanticSkill,
)
from case_kernel.case_agent_planning_snapshot import (
    AuthoritativeCasePlanningProjection,
    AuthoritativePlanningObject,
    CasePlanningProjectionBlocked,
    ConfirmedPostureProjection,
    ExecutablePlanningSkill,
    PlanningProjectionObjectType,
    ProjectionSectionState,
)
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    ActivePlanDeliverableRef,
    ActivePlanExecutionRef,
    AgentDeliverableFormat,
    AgentDeliverableKind,
    AgentRunState,
    AgentRunStatus,
    BudgetUsage,
    CaseSnapshotRef,
    RunResourceBudget,
    RuntimeAdapterManifest,
    SupervisorCommandKind,
)
from case_kernel.case_agent_worker import DurablePlanningClaim
from case_kernel.controlled_defence_case_agent_planner import (
    CONTROLLED_DEFENCE_PLANNER_ID,
    ControlledDefencePlanningSnapshotProvider,
    ControlledDefencePlanningRouter,
)
from case_kernel.case_agent_supervisor import AgentGoal
from case_kernel.models import Actor, Role


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_local_planning_outcome(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _Fallback:
    planner_id = "fallback-planner"

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, **_: object) -> CasePlanProposal:
        self.calls += 1
        raise AssertionError("defence route must not invoke the external planner")


class _ProjectionRepository:
    def __init__(self, projection: AuthoritativeCasePlanningProjection) -> None:
        self.projection = projection

    def read_atomic_projection(self, **_: object) -> AuthoritativeCasePlanningProjection:
        return self.projection


class _RuntimeAdapter:
    def __init__(self, *, skill_id: str) -> None:
        self.manifest = RuntimeAdapterManifest(
            tool_id=f"test-{skill_id}-tool",
            adapter_id=f"test-{skill_id}-adapter",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.IN_PROCESS,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash=digest(f"{skill_id}-policy"),
        )

    def execute(self, **_: object) -> object:
        raise AssertionError("planning snapshot tests must not execute adapters")

    def reconcile(self, **_: object) -> object:
        raise AssertionError("planning snapshot tests must not reconcile adapters")


class ControlledDefencePlanningRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.run_id = str(uuid4())
        self.worker = Actor(
            actor_id=str(uuid4()),
            firm_id=self.firm_id,
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        self.goal = AgentGoal.build(
            goal_id=str(uuid4()),
            objective="形成一审被告民事答辩状候选。",
            success_criteria=("形成可复核律师决策包",),
            constraints=("不得自动批准、锁定或提交",),
            requested_by=str(uuid4()),
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
        )
        self.snapshot_ref = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=9,
            snapshot_hash=digest("case-v9"),
            schema_version="case-ledger-snapshot-v1",
        )
        self.state = AgentRunState(
            run_id=self.run_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            event_version=2,
            goal=self.goal,
            snapshot=self.snapshot_ref,
            budget=RunResourceBudget(
                max_tasks=10,
                max_total_attempts=20,
                max_external_calls=1,
                max_runtime_seconds=1_200,
                max_cost_minor_units=120,
                max_output_bytes=256 * 1024 * 1024,
            ),
            status=AgentRunStatus.PLANNING,
            graph=None,
            tasks=(),
            approvals=(),
            artifacts=(),
            budget_usage=BudgetUsage(),
        )
        self.skills = (
            PlannerSemanticSkill(
                skill_id="case_context_review",
                title="案件情境核对",
                output_kind="CaseContextReviewCandidate",
            ),
            PlannerSemanticSkill(
                skill_id=LAWYER_ANALYSIS_SKILL_ID,
                title="律师决策包",
                output_kind="LawyerDecisionPackageCandidate",
            ),
        )

    def test_defence_route_records_local_plan_and_reserves_one_model_task(self) -> None:
        snapshot = self._snapshot()
        recorder = _Recorder()
        fallback = _Fallback()
        planner = ControlledDefencePlanningRouter(
            fallback=fallback, outcome_recorder=recorder
        )

        proposal = planner.plan(
            goal=self.goal,
            snapshot=snapshot,
            skills=self.skills,
            execution=self._claim(snapshot),
        )

        self.assertEqual(fallback.calls, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["planner_id"], CONTROLLED_DEFENCE_PLANNER_ID)
        self.assertEqual(
            tuple(task.skill_id for task in proposal.tasks),
            ("case_context_review", LAWYER_ANALYSIS_SKILL_ID),
        )
        self.assertEqual(
            sum(task.skill_id == LAWYER_ANALYSIS_SKILL_ID for task in proposal.tasks),
            1,
        )
        for task in proposal.tasks:
            self.assertFalse(
                any(ref.startswith("evidence-page:") for ref in task.input_ref_ids)
            )
            self.assertEqual(
                set(prefix for prefix in ("posture-profile:", "fact:", "claim:", "legal-source:", "legal-rule:") if any(ref.startswith(prefix) for ref in task.input_ref_ids)),
                {"posture-profile:", "fact:", "claim:", "legal-source:", "legal-rule:"},
            )

    def test_initial_analysis_does_not_require_preapproved_legal_rules(self) -> None:
        snapshot = self._snapshot(include_rule=False)
        recorder = _Recorder()
        fallback = _Fallback()
        planner = ControlledDefencePlanningRouter(
            fallback=fallback, outcome_recorder=recorder
        )

        proposal = planner.plan(goal=self.goal, snapshot=snapshot, skills=self.skills,
                                execution=self._claim(snapshot))
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks),
                         ("case_context_review", LAWYER_ANALYSIS_SKILL_ID))
        self.assertFalse(any(ref.startswith("legal-rule:") for task in proposal.tasks for ref in task.input_ref_ids))

    def test_initial_analysis_keeps_uncertain_fact_status_and_source(self) -> None:
        from case_kernel.controlled_defence_case_agent_planner import _controlled_sources
        base = self._snapshot()
        for status in (PlanningInputStatus.REVIEW_REQUIRED, PlanningInputStatus.DISPUTED,
                       PlanningInputStatus.BLOCKED):
            with self.subTest(status=status):
                candidate = self._input("fact", PlanningInputKind.CONFIRMED_FACT, status,
                    tuple(sorted(("case_context_review", LAWYER_ANALYSIS_SKILL_ID))))
                snapshot = CasePlanningSnapshot.build(case_snapshot=self.snapshot_ref,
                    authorized_inputs=(*base.authorized_inputs, candidate), signals=())
                planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
                proposal = planner.plan(goal=self.goal, snapshot=snapshot, skills=self.skills,
                    execution=self._claim(snapshot))
                self.assertTrue(all(candidate.ref_id in task.input_ref_ids for task in proposal.tasks))
                self.assertEqual(candidate.status, status)
                # The same pending source cannot enter document execution.
                with self.assertRaisesRegex(CasePlannerBlocked, "status is not governed"):
                    _controlled_sources(snapshot)

    def test_initial_analysis_cannot_treat_only_candidates_as_confirmed_facts(self) -> None:
        base = self._snapshot()
        snapshot = CasePlanningSnapshot.build(case_snapshot=self.snapshot_ref,
            authorized_inputs=tuple(replace(item, status=PlanningInputStatus.REVIEW_REQUIRED)
                if item.ref_id.startswith("fact:") else item for item in base.authorized_inputs), signals=())
        planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
        with self.assertRaisesRegex(CasePlannerBlocked, "requires a confirmed source fact"):
            planner.plan(goal=self.goal, snapshot=snapshot, skills=self.skills, execution=self._claim(snapshot))

    def test_active_plan_execution_cannot_reintroduce_the_model_task(self) -> None:
        active_goal = AgentGoal.build(
            goal_id=str(uuid4()),
            objective="执行已激活的被告应诉计划并生成答辩状候选。",
            success_criteria=("生成可复核答辩状候选",),
            constraints=("不得发起新的模型分析",),
            requested_by=str(uuid4()),
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            active_plan_execution=ActivePlanExecutionRef(
                plan_id=str(uuid4()),
                plan_hash=digest("active-plan"),
                source_run_id=self.run_id,
                items=(
                    ActivePlanDeliverableRef(
                        item_id=str(uuid4()),
                        item_hash=digest("defence-item"),
                        deliverable_kind=AgentDeliverableKind.DEFENCE_STATEMENT,
                        output_format=AgentDeliverableFormat.DOCX,
                    ),
                ),
            ),
        )
        state = replace(self.state, goal=active_goal)
        # Document execution must retain the authoritative ACTIVE-plan signal.
        # The analysis-only projection intentionally removes it.
        from unittest.mock import Mock
        provider = object.__new__(ControlledDefencePlanningSnapshotProvider)
        provider._standard_provider = Mock()
        provider._defence_provider = Mock()
        provider.build_for_run(state=state, actor=self.worker)
        provider._standard_provider.build_for_run.assert_called_once_with(
            state=state, actor=self.worker)
        provider._defence_provider.build_for_run.assert_not_called()
        active_item = PlanningInputRef(
            ref_id=f"work-plan-item:{active_goal.active_plan_execution.items[0].item_id}",
            kind=PlanningInputKind.WORK_PLAN_ITEM,
            object_version="v1",
            content_hash=active_goal.active_plan_execution.items[0].item_hash,
            status=PlanningInputStatus.CONFIRMED,
            allowed_skill_ids=("dynamic_document_delivery",),
            planner_visible=False,
        )
        snapshot = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref,
            authorized_inputs=(active_item,),
            signals=(
                CasePlanningSignal(
                    signal_id=f"signal:work-plan:{active_goal.active_plan_execution.plan_id}",
                    category=PlanningSignalCategory.WORK_PLAN,
                    code="ACTIVE_DYNAMIC_WORK_PLAN",
                    status=PlanningInputStatus.CONFIRMED,
                    summary="主办律师已激活当前动态计划。",
                    source_ref_ids=(active_item.ref_id,),
                ),
            ),
        )
        recorder = _Recorder()
        fallback = _Fallback()
        planner = ControlledDefencePlanningRouter(
            fallback=fallback, outcome_recorder=recorder
        )

        proposal = planner.plan(
            goal=active_goal,
            snapshot=snapshot,
            skills=self.skills,
            execution=self._claim(snapshot, state=state),
        )

        self.assertEqual(fallback.calls, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(
            tuple(task.skill_id for task in proposal.tasks),
            ("case_context_review",),
        )
        self.assertNotIn(
            LAWYER_ANALYSIS_SKILL_ID,
            tuple(task.skill_id for task in proposal.tasks),
        )
        no_active_item = self._snapshot()
        with self.assertRaisesRegex(CasePlannerBlocked, "work-plan inputs"):
            planner.plan(goal=active_goal, snapshot=no_active_item, skills=self.skills,
                         execution=self._claim(no_active_item, state=state))

    def test_raw_pdf_pages_are_batched_then_extracted_once(self) -> None:
        raw = tuple(self._input("evidence-page", PlanningInputKind.EVIDENCE_PAGE,
            PlanningInputStatus.REVIEW_REQUIRED, ("case_ledger_extraction", "image_visual_ocr", "pdf_reading"))
            for _ in range(84))
        snapshot = CasePlanningSnapshot.build(case_snapshot=self.snapshot_ref,
                                              authorized_inputs=raw, signals=())
        recorder, fallback = _Recorder(), _Fallback()
        planner = ControlledDefencePlanningRouter(fallback=fallback, outcome_recorder=recorder)
        proposal = planner.plan(goal=self.goal, snapshot=snapshot,
            skills=tuple(PlannerSemanticSkill(skill_id=reader, title="材料整理", output_kind="ReviewCandidate")
                         for reader in ("pdf_reading", "case_ledger_extraction", "image_visual_ocr")),
            execution=self._claim(snapshot))
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks),
                         ("pdf_reading", "case_ledger_extraction"))
        expected_refs = tuple(sorted(item.ref_id for item in raw))
        self.assertEqual(proposal.tasks[0].input_ref_ids, expected_refs)
        self.assertEqual(proposal.tasks[1].input_ref_ids, expected_refs)
        self.assertEqual(proposal.tasks[1].dependency_ids, (proposal.tasks[0].proposal_id,))
        self.assertEqual(proposal.goal_hash, self.goal.goal_hash)
        self.assertEqual(fallback.calls, 0)

    def test_analysis_with_governed_inputs_does_not_reread_preserved_originals(self) -> None:
        snapshot = self._snapshot()
        original_page = self._input(
            "evidence-page", PlanningInputKind.EVIDENCE_PAGE,
            PlanningInputStatus.REVIEW_REQUIRED,
            ("case_ledger_extraction", "pdf_reading"),
        )
        # The original remains in the durable snapshot for traceability.  It
        # must not make an already-governed analysis run extraction again.
        governed = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref,
            authorized_inputs=(*snapshot.authorized_inputs, original_page),
            signals=(),
        )
        planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
        proposal = planner.plan(goal=self.goal, snapshot=governed, skills=self.skills,
                                execution=self._claim(governed))
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks),
                         ("case_context_review", LAWYER_ANALYSIS_SKILL_ID))
        for task in proposal.tasks:
            self.assertEqual(
                set(task.input_ref_ids),
                {item.ref_id for item in snapshot.authorized_inputs},
            )
            self.assertNotIn(original_page.ref_id, task.input_ref_ids)

    def test_confirmed_issue_enters_governed_analysis_without_becoming_required_for_first_read(self) -> None:
        snapshot = self._snapshot()
        issue = self._input(
            "issue", PlanningInputKind.LEGAL_GAP,
            PlanningInputStatus.CONFIRMED,
            ("case_context_review", LAWYER_ANALYSIS_SKILL_ID),
        )
        governed = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref,
            authorized_inputs=(*snapshot.authorized_inputs, issue),
            signals=(),
        )
        planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
        proposal = planner.plan(goal=self.goal, snapshot=governed, skills=self.skills,
                                execution=self._claim(governed))
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks),
                         ("case_context_review", LAWYER_ANALYSIS_SKILL_ID))
        self.assertTrue(all(issue.ref_id in task.input_ref_ids for task in proposal.tasks))

    def test_defence_projection_retains_confirmed_issue_for_analysis(self) -> None:
        from case_kernel.controlled_defence_case_agent_planner import _CONTROLLED_DEFENCE_OBJECT_TYPES
        self.assertIn(
            PlanningProjectionObjectType.DISPUTE_ISSUE,
            _CONTROLLED_DEFENCE_OBJECT_TYPES,
        )

    def test_unconfirmed_transaction_stays_in_material_review_not_model_premise(self):
        from case_kernel.controlled_defence_case_agent_planner import _controlled_sources
        snapshot = self._snapshot()
        candidate = self._input("transaction-candidate", PlanningInputKind.LEGAL_GAP,
            PlanningInputStatus.REVIEW_REQUIRED, ("case_context_review", LAWYER_ANALYSIS_SKILL_ID))
        snapshot = CasePlanningSnapshot.build(case_snapshot=self.snapshot_ref,
            authorized_inputs=(*snapshot.authorized_inputs, candidate), signals=())
        planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
        proposal = planner.plan(goal=self.goal, snapshot=snapshot, skills=self.skills,
                                execution=self._claim(snapshot))
        self.assertTrue(all(candidate.ref_id not in task.input_ref_ids for task in proposal.tasks))
        self.assertEqual(candidate.status, PlanningInputStatus.REVIEW_REQUIRED)
        with self.assertRaisesRegex(CasePlannerBlocked, "unconfirmed transactions"):
            _controlled_sources(snapshot)

    def test_ocr_inputs_do_not_enter_native_pdf_extraction(self) -> None:
        raw = self._input("evidence-page", PlanningInputKind.EVIDENCE_PAGE,
                         PlanningInputStatus.REVIEW_REQUIRED, ("case_ledger_extraction", "image_visual_ocr"))
        snapshot = CasePlanningSnapshot.build(case_snapshot=self.snapshot_ref,
                                              authorized_inputs=(raw,), signals=())
        planner = ControlledDefencePlanningRouter(fallback=_Fallback(), outcome_recorder=_Recorder())
        proposal = planner.plan(goal=self.goal, snapshot=snapshot,
            skills=tuple(PlannerSemanticSkill(skill_id=reader, title="读取", output_kind="ReviewCandidate")
                         for reader in ("image_visual_ocr", "case_ledger_extraction")),
            execution=self._claim(snapshot))
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks), ("image_visual_ocr",))

    def test_incomplete_defence_inputs_read_originals_without_external_planning(self) -> None:
        raw = self._input("evidence-page", PlanningInputKind.EVIDENCE_PAGE,
                          PlanningInputStatus.REVIEW_REQUIRED, ("pdf_reading",))
        snapshot = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref, authorized_inputs=(raw,), signals=())
        recorder, fallback = _Recorder(), _Fallback()
        planner = ControlledDefencePlanningRouter(fallback=fallback, outcome_recorder=recorder)
        proposal = planner.plan(goal=self.goal, snapshot=snapshot,
            skills=self.skills + (PlannerSemanticSkill(skill_id="pdf_reading",
                title="读取PDF", output_kind="PdfReadingCandidate"),),
            execution=self._claim(snapshot))
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(proposal.goal_hash, self.goal.goal_hash)
        self.assertEqual(tuple(task.skill_id for task in proposal.tasks), ("pdf_reading",))
        self.assertEqual(proposal.tasks[0].input_ref_ids, (raw.ref_id,))

    def test_preparation_never_guesses_an_ambiguous_reader(self) -> None:
        raw = self._input("evidence-page", PlanningInputKind.EVIDENCE_PAGE,
                          PlanningInputStatus.REVIEW_REQUIRED, ("office_reading", "pdf_reading"))
        snapshot = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref, authorized_inputs=(raw,), signals=())
        recorder, fallback = _Recorder(), _Fallback()
        planner = ControlledDefencePlanningRouter(fallback=fallback, outcome_recorder=recorder)
        with self.assertRaisesRegex(CasePlannerBlocked, "one authorized reading"):
            planner.plan(goal=self.goal, snapshot=snapshot,
                skills=self.skills + tuple(PlannerSemanticSkill(skill_id=reader,
                    title="读取", output_kind="ReadingCandidate")
                    for reader in ("pdf_reading", "office_reading")),
                execution=self._claim(snapshot))
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(recorder.calls, [])

    def test_defence_snapshot_excludes_private_raw_evidence_without_weakening_generic_fail_closed(self) -> None:
        """ADR-0056 excludes raw evidence only from the one-call route."""

        profile_id = str(uuid4())
        posture = ConfirmedPostureProjection(
            profile_id=profile_id,
            profile_version="v1",
            profile_hash=digest("defence-posture"),
            effective_status="CURRENT",
            case_type_code="PRIVATE_LENDING",
            procedure_stage="FIRST_INSTANCE",
            represented_position="DEFENDANT",
            authority_scope_code="GENERAL_REPRESENTATION",
            engagement_state="ACTIVE",
        )

        def object(
            object_type: PlanningProjectionObjectType,
            *,
            status: PlanningInputStatus,
            source_media_type: str | None = None,
            content_hash: str | None = None,
        ) -> AuthoritativePlanningObject:
            object_id = profile_id if object_type is PlanningProjectionObjectType.POSTURE_PROFILE else str(uuid4())
            return AuthoritativePlanningObject(
                object_type=object_type,
                object_id=object_id,
                object_version="v1",
                content_hash=(
                    digest(f"{object_type.value}:{object_id}")
                    if content_hash is None
                    else content_hash
                ),
                status=status,
                source_media_type=source_media_type,
            )

        projection = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot_ref,
            closing_case_snapshot=self.snapshot_ref,
            objects=(
                object(
                    PlanningProjectionObjectType.EVIDENCE_PAGE,
                    status=PlanningInputStatus.REVIEW_REQUIRED,
                    source_media_type="image/jpeg",
                ),
                object(
                    PlanningProjectionObjectType.POSTURE_PROFILE,
                    status=PlanningInputStatus.CONFIRMED,
                    content_hash=posture.profile_hash,
                ),
                object(
                    PlanningProjectionObjectType.CASE_FACT,
                    status=PlanningInputStatus.CONFIRMED,
                ),
                object(
                    PlanningProjectionObjectType.CASE_CLAIM,
                    status=PlanningInputStatus.CONFIRMED,
                ),
                object(
                    PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                    status=PlanningInputStatus.LOCKED,
                ),
                object(
                    PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                    status=PlanningInputStatus.LOCKED,
                ),
            ),
            posture_state=ProjectionSectionState.AVAILABLE,
            posture=posture,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=ProjectionSectionState.AVAILABLE,
            procedure_state=ProjectionSectionState.EMPTY,
        )
        full_context_types = frozenset(
            {
                PlanningProjectionObjectType.POSTURE_PROFILE,
                PlanningProjectionObjectType.CASE_FACT,
                PlanningProjectionObjectType.CASE_CLAIM,
                PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
            }
        )
        skills = (
            ExecutablePlanningSkill(
                skill_id="case_context_review",
                adapter=_RuntimeAdapter(skill_id="case_context_review"),
                supported_object_types=full_context_types,
            ),
            ExecutablePlanningSkill(
                skill_id=LAWYER_ANALYSIS_SKILL_ID,
                adapter=_RuntimeAdapter(skill_id=LAWYER_ANALYSIS_SKILL_ID),
                supported_object_types=full_context_types,
            ),
        )
        provider = ControlledDefencePlanningSnapshotProvider(
            repository=_ProjectionRepository(projection),
            executable_skills=skills,
        )
        for complete in (True, False, None):
            values = {key: value for key, value in projection.__dict__.items()
                      if key != "projection_hash"}
            values["objects"] = tuple(replace(item, extraction_complete=complete)
                if item.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE else item
                for item in projection.objects)
            covered = AuthoritativeCasePlanningProjection.build(**values)
            coverage_provider = ControlledDefencePlanningSnapshotProvider(
                repository=_ProjectionRepository(covered), executable_skills=skills)
            if complete:
                result = coverage_provider.build_for_run(state=self.state, actor=self.worker)
                self.assertFalse(any(item.ref_id.startswith("evidence-page:") for item in result.authorized_inputs))
            else:
                with self.assertRaisesRegex(CasePlanningProjectionBlocked, "no registered executable adapter"):
                    coverage_provider.build_for_run(state=self.state, actor=self.worker)

        # An old or unavailable coverage ledger is not evidence of completion.
        # Without a reader, report the unsupported source instead of hiding it.
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "no registered executable adapter"):
            provider.build_for_run(state=self.state, actor=self.worker)

        generic_goal = AgentGoal.build(
            goal_id=str(uuid4()),
            objective="形成案件审阅意见候选。",
            success_criteria=("核对材料边界",),
            constraints=("不得跳过不支持的证据格式",),
            requested_by=str(uuid4()),
            requested_deliverables=(AgentDeliverableKind.CASE_REVIEW_MEMO,),
        )
        with self.assertRaisesRegex(
            CasePlanningProjectionBlocked, "no registered executable adapter"
        ):
            provider.build_for_run(
                state=replace(self.state, goal=generic_goal), actor=self.worker
            )

        # Missing facts must not make raw evidence disappear. Without an OCR
        # adapter the standard provider rejects the page instead of silently
        # producing a narrow, unusable defence snapshot.
        incomplete = AuthoritativeCasePlanningProjection.build(
            firm_id=self.firm_id, matter_id=self.matter_id,
            opening_case_snapshot=self.snapshot_ref, closing_case_snapshot=self.snapshot_ref,
            objects=tuple(item for item in projection.objects
                if item.object_type is not PlanningProjectionObjectType.CASE_FACT),
            posture_state=ProjectionSectionState.AVAILABLE, posture=posture,
            work_plan_state=ProjectionSectionState.EMPTY, active_work_plan=None,
            legal_state=ProjectionSectionState.AVAILABLE,
            procedure_state=ProjectionSectionState.EMPTY)
        incomplete_provider = ControlledDefencePlanningSnapshotProvider(
            repository=_ProjectionRepository(incomplete), executable_skills=skills)
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "no registered executable adapter"):
            incomplete_provider.build_for_run(state=self.state, actor=self.worker)

    def _snapshot(self, *, include_rule: bool = True) -> CasePlanningSnapshot:
        allowed = tuple(sorted(("case_context_review", LAWYER_ANALYSIS_SKILL_ID)))
        inputs = [
            self._input("posture-profile", PlanningInputKind.PROCEDURAL_EVENT, PlanningInputStatus.CONFIRMED, allowed),
            self._input("fact", PlanningInputKind.CONFIRMED_FACT, PlanningInputStatus.CONFIRMED, allowed),
            self._input("claim", PlanningInputKind.WORK_PLAN_ITEM, PlanningInputStatus.CONFIRMED, allowed),
            self._input("legal-source", PlanningInputKind.VERIFIED_SOURCE, PlanningInputStatus.LOCKED, allowed),
        ]
        if include_rule:
            inputs.append(
                self._input("legal-rule", PlanningInputKind.VERIFIED_SOURCE, PlanningInputStatus.LOCKED, allowed)
            )
        return CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref,
            authorized_inputs=inputs,
            signals=(),
        )

    def _input(
        self,
        prefix: str,
        kind: PlanningInputKind,
        status: PlanningInputStatus,
        allowed: tuple[str, ...],
    ) -> PlanningInputRef:
        object_id = str(uuid4())
        return PlanningInputRef(
            ref_id=f"{prefix}:{object_id}",
            kind=kind,
            object_version="v1",
            content_hash=digest(prefix + object_id),
            status=status,
            allowed_skill_ids=allowed,
        )

    def _claim(
        self,
        snapshot: CasePlanningSnapshot,
        *,
        state: AgentRunState | None = None,
    ) -> DurablePlanningClaim:
        now = datetime.now(timezone.utc)
        current_state = self.state if state is None else state
        return DurablePlanningClaim(
            planning_attempt_id=str(uuid4()),
            run_id=self.run_id,
            command_kind=SupervisorCommandKind.REQUEST_PLAN,
            command_id=digest("plan-command"),
            planning_hash=snapshot.planning_hash,
            claim_lease_id=str(uuid4()),
            lease_token=str(uuid4()),
            external_request_id=str(uuid4()),
            event_version=current_state.event_version,
            attempt_version=1,
            lease_owner="test-worker",
            lease_expires_at=now + timedelta(minutes=2),
            graph_id=str(uuid4()),
            graph_version=1,
            state=current_state,
        )


if __name__ == "__main__":
    unittest.main()
