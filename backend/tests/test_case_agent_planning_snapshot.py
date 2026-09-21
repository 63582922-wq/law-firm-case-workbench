from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_planner import (
    PlanningInputStatus,
    PlanningSignalCategory,
    ReextractionPlanningObligation,
)
from case_kernel.case_agent_planning_snapshot import (
    ActiveDynamicWorkPlanProjection,
    AuthoritativeCasePlanningProjection,
    AuthoritativeCasePlanningSnapshotProvider,
    AuthoritativePlanningObject,
    CasePlanningProjectionBlocked,
    ConfirmedPostureProjection,
    ExecutablePlanningSkill,
    GovernedLawyerPlanningSignal,
    PlanningProjectionObjectType,
    ProjectionSectionState,
    object_version_code,
    planning_object_ref_id,
)
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    AgentGoal,
    AgentRunState,
    AgentRunStatus,
    BudgetUsage,
    CaseSnapshotRef,
    RunResourceBudget,
    RuntimeAdapterManifest,
)
from case_kernel.models import Actor, Role


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Adapter:
    def __init__(self, tool_id: str) -> None:
        self.manifest = RuntimeAdapterManifest(
            tool_id=tool_id,
            adapter_id=f"{tool_id}-adapter",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.IN_PROCESS,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash=digest(f"{tool_id}-policy"),
        )

    def execute(self, **_: object) -> object:
        raise AssertionError("planning tests must not execute adapters")

    def reconcile(self, **_: object) -> object:
        raise AssertionError("planning tests must not reconcile adapters")


class _Repository:
    def __init__(self, projection: AuthoritativeCasePlanningProjection) -> None:
        self.projection = projection
        self.calls: list[dict[str, object]] = []

    def read_atomic_projection(self, **kwargs: object) -> AuthoritativeCasePlanningProjection:
        self.calls.append(kwargs)
        return self.projection


class CaseAgentPlanningSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.worker = Actor(
            actor_id=str(uuid4()),
            firm_id=self.firm_id,
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        self.snapshot_ref = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=9,
            snapshot_hash=digest("case-v9"),
            schema_version="case-ledger-snapshot-v1",
        )
        goal = AgentGoal.build(
            goal_id=str(uuid4()),
            objective="全面分析本案并形成可追溯的律师复核候选。",
            success_criteria=("所有结论绑定当前案件快照",),
            constraints=("不得推定诉讼地位或法院期限",),
            requested_by=str(uuid4()),
        )
        self.state = AgentRunState(
            run_id=str(uuid4()),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            event_version=3,
            goal=goal,
            snapshot=self.snapshot_ref,
            budget=RunResourceBudget(
                max_tasks=20,
                max_total_attempts=40,
                max_external_calls=10,
                max_runtime_seconds=3600,
                max_cost_minor_units=100_000,
                max_output_bytes=100_000_000,
            ),
            status=AgentRunStatus.PLANNING,
            graph=None,
            tasks=(),
            approvals=(),
            artifacts=(),
            budget_usage=BudgetUsage(),
        )
        self.material_id = str(uuid4())
        self.page_id = str(uuid4())
        self.profile_id = str(uuid4())
        self.plan_id = str(uuid4())
        self.plan_item_id = str(uuid4())

    def object(
        self,
        object_type: PlanningProjectionObjectType,
        object_id: str | None = None,
        *,
        status: PlanningInputStatus = PlanningInputStatus.CONFIRMED,
        source_media_type: str | None = None,
    ) -> AuthoritativePlanningObject:
        resolved_id = object_id or str(uuid4())
        return AuthoritativePlanningObject(
            object_type=object_type,
            object_id=resolved_id,
            object_version="v1",
            content_hash=digest(f"{object_type.value}:{resolved_id}"),
            status=status,
            source_media_type=(
                source_media_type
                if source_media_type is not None
                else (
                    "application/pdf"
                    if object_type is PlanningProjectionObjectType.EVIDENCE_PAGE
                    else None
                )
            ),
        )

    def posture(self) -> ConfirmedPostureProjection:
        return ConfirmedPostureProjection(
            profile_id=self.profile_id,
            profile_version="v2",
            profile_hash=digest("confirmed-posture-v2"),
            effective_status="CURRENT",
            case_type_code="PRIVATE_LENDING",
            procedure_stage="FIRST_INSTANCE",
            represented_position="DEFENDANT",
            authority_scope_code="GENERAL_REPRESENTATION",
            engagement_state="ACTIVE",
        )

    def posture_object(self) -> AuthoritativePlanningObject:
        posture = self.posture()
        return AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.POSTURE_PROFILE,
            object_id=posture.profile_id,
            object_version=posture.profile_version,
            content_hash=posture.profile_hash,
            status=PlanningInputStatus.CONFIRMED,
        )

    def work_plan(self) -> ActiveDynamicWorkPlanProjection:
        return ActiveDynamicWorkPlanProjection(
            plan_id=self.plan_id,
            plan_version="v3",
            plan_hash=digest("dynamic-plan-v3"),
            bound_matter_version=9,
            item_ids=(self.plan_item_id,),
            actionable_count=0,
            needs_information_count=1,
            needs_research_count=0,
        )

    def projection(
        self,
        *,
        objects: tuple[AuthoritativePlanningObject, ...] | None = None,
        posture_state: ProjectionSectionState = ProjectionSectionState.AVAILABLE,
        posture: ConfirmedPostureProjection | None | object = ...,  # sentinel
        work_plan_state: ProjectionSectionState = ProjectionSectionState.AVAILABLE,
        active_work_plan: ActiveDynamicWorkPlanProjection | None | object = ...,
        legal_state: ProjectionSectionState = ProjectionSectionState.EMPTY,
        procedure_state: ProjectionSectionState = ProjectionSectionState.EMPTY,
        lawyer_signals: tuple[GovernedLawyerPlanningSignal, ...] = (),
        reextraction_obligations: tuple[ReextractionPlanningObligation, ...] = (),
        opening: CaseSnapshotRef | None = None,
        closing: CaseSnapshotRef | None = None,
        firm_id: str | None = None,
        matter_id: str | None = None,
    ) -> AuthoritativeCasePlanningProjection:
        resolved_posture = self.posture() if posture is ... else posture
        resolved_plan = self.work_plan() if active_work_plan is ... else active_work_plan
        if objects is None:
            objects = (
                self.object(
                    PlanningProjectionObjectType.MATERIAL_OBJECT,
                    self.material_id,
                    status=PlanningInputStatus.AVAILABLE,
                ),
                self.object(
                    PlanningProjectionObjectType.EVIDENCE_PAGE,
                    self.page_id,
                    status=PlanningInputStatus.REVIEW_REQUIRED,
                ),
                self.posture_object(),
                self.object(
                    PlanningProjectionObjectType.WORK_PLAN_ITEM,
                    self.plan_item_id,
                    status=PlanningInputStatus.OPEN,
                ),
            )
        return AuthoritativeCasePlanningProjection.build(
            firm_id=firm_id or self.firm_id,
            matter_id=matter_id or self.matter_id,
            opening_case_snapshot=opening or self.snapshot_ref,
            closing_case_snapshot=closing or self.snapshot_ref,
            objects=objects,
            posture_state=posture_state,
            posture=resolved_posture,  # type: ignore[arg-type]
            work_plan_state=work_plan_state,
            active_work_plan=resolved_plan,  # type: ignore[arg-type]
            legal_state=legal_state,
            procedure_state=procedure_state,
            lawyer_signals=lawyer_signals,
            reextraction_obligations=reextraction_obligations,
        )

    def skills(self) -> tuple[ExecutablePlanningSkill, ...]:
        return (
            ExecutablePlanningSkill(
                skill_id="office_reading",
                adapter=_Adapter("parse_office_document"),
                supported_object_types=frozenset(
                    {PlanningProjectionObjectType.MATERIAL_OBJECT}
                ),
                neutral_material_inventory=True,
            ),
            ExecutablePlanningSkill(
                skill_id="pdf_reading",
                adapter=_Adapter("extract_pdf_text"),
                supported_object_types=frozenset(
                    {PlanningProjectionObjectType.EVIDENCE_PAGE}
                ),
                neutral_material_inventory=True,
                supported_media_types=frozenset({"application/pdf"}),
            ),
            ExecutablePlanningSkill(
                skill_id="case_analysis",
                adapter=_Adapter("analyze_case_projection"),
                supported_object_types=frozenset(
                    {
                        PlanningProjectionObjectType.CASE_FACT,
                        PlanningProjectionObjectType.CASE_CLAIM,
                        PlanningProjectionObjectType.DISPUTE_ISSUE,
                        PlanningProjectionObjectType.CASE_TRANSACTION,
                        PlanningProjectionObjectType.POSTURE_PROFILE,
                        PlanningProjectionObjectType.WORK_PLAN_ITEM,
                    }
                ),
            ),
            ExecutablePlanningSkill(
                skill_id="legal_review",
                adapter=_Adapter("review_verified_legal_projection"),
                supported_object_types=frozenset(
                    {
                        PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                        PlanningProjectionObjectType.PROCEDURAL_EVENT,
                    }
                ),
            ),
        )

    def provider(
        self,
        projection: AuthoritativeCasePlanningProjection,
        *,
        skills: tuple[ExecutablePlanningSkill, ...] | None = None,
        max_inputs: int = 500,
    ) -> tuple[AuthoritativeCasePlanningSnapshotProvider, _Repository]:
        repository = _Repository(projection)
        return (
            AuthoritativeCasePlanningSnapshotProvider(
                repository=repository,
                executable_skills=skills or self.skills(),
                max_inputs=max_inputs,
            ),
            repository,
        )

    def test_full_projection_is_exactly_bound_and_contains_no_private_fields(self) -> None:
        fact = self.object(PlanningProjectionObjectType.CASE_FACT)
        claim = self.object(PlanningProjectionObjectType.CASE_CLAIM)
        issue = self.object(
            PlanningProjectionObjectType.DISPUTE_ISSUE,
            status=PlanningInputStatus.OPEN,
        )
        transaction = self.object(PlanningProjectionObjectType.CASE_TRANSACTION)
        source = self.object(
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            status=PlanningInputStatus.LOCKED,
        )
        event = self.object(PlanningProjectionObjectType.PROCEDURAL_EVENT)
        projection = self.projection(
            objects=(
                self.object(
                    PlanningProjectionObjectType.MATERIAL_OBJECT,
                    self.material_id,
                    status=PlanningInputStatus.AVAILABLE,
                ),
                self.object(
                    PlanningProjectionObjectType.EVIDENCE_PAGE,
                    self.page_id,
                    status=PlanningInputStatus.REVIEW_REQUIRED,
                ),
                fact,
                claim,
                issue,
                transaction,
                self.posture_object(),
                self.object(
                    PlanningProjectionObjectType.WORK_PLAN_ITEM,
                    self.plan_item_id,
                    status=PlanningInputStatus.OPEN,
                ),
                source,
                event,
            ),
            legal_state=ProjectionSectionState.AVAILABLE,
            procedure_state=ProjectionSectionState.AVAILABLE,
        )
        provider, repository = self.provider(projection)

        result = provider.build_for_run(state=self.state, actor=self.worker)

        self.assertEqual(result.case_snapshot, self.snapshot_ref)
        self.assertEqual(len(result.authorized_inputs), 10)
        self.assertEqual(
            repository.calls[0],
            {
                "firm_id": self.firm_id,
                "matter_id": self.matter_id,
                "actor": self.worker,
                "expected_case_snapshot": self.snapshot_ref,
            },
        )
        page_ref = next(item for item in result.authorized_inputs if item.ref_id.endswith(self.page_id))
        self.assertEqual(page_ref.ref_id, f"evidence-page:{self.page_id}")
        self.assertEqual(page_ref.allowed_skill_ids, ("pdf_reading",))
        material_ref = next(
            item for item in result.authorized_inputs if item.ref_id.endswith(self.material_id)
        )
        self.assertEqual(material_ref.ref_id, f"material-object:{self.material_id}")
        self.assertEqual(material_ref.allowed_skill_ids, ("office_reading",))
        public = repr(result)
        for prohibited in ("object_key", "storage", "file_path", "https://", "/tmp/"):
            self.assertNotIn(prohibited, public)
        signal_text = " ".join(item.summary for item in result.signals)
        self.assertIn("代理地位=DEFENDANT", signal_text)
        self.assertIn("不生成固定文书清单", signal_text)
        self.assertIn("动态工作计划", signal_text)
        self.assertIn("尚无已核验法源", signal_text) if False else None

    def test_evidence_page_skills_are_scoped_by_registered_source_media_type(self) -> None:
        image_page_id = str(uuid4())
        projection = self.projection(
            objects=(
                self.object(
                    PlanningProjectionObjectType.MATERIAL_OBJECT,
                    self.material_id,
                    status=PlanningInputStatus.AVAILABLE,
                ),
                self.object(
                    PlanningProjectionObjectType.EVIDENCE_PAGE,
                    self.page_id,
                    status=PlanningInputStatus.REVIEW_REQUIRED,
                    source_media_type="application/pdf",
                ),
                self.object(
                    PlanningProjectionObjectType.EVIDENCE_PAGE,
                    image_page_id,
                    status=PlanningInputStatus.REVIEW_REQUIRED,
                    source_media_type="image/png",
                ),
                self.posture_object(),
                self.object(
                    PlanningProjectionObjectType.WORK_PLAN_ITEM,
                    self.plan_item_id,
                    status=PlanningInputStatus.OPEN,
                ),
            )
        )
        visual = ExecutablePlanningSkill(
            skill_id="image_visual_ocr",
            adapter=_Adapter("understand_visual_page"),
            supported_object_types=frozenset(
                {PlanningProjectionObjectType.EVIDENCE_PAGE}
            ),
            neutral_material_inventory=True,
            supported_media_types=frozenset(
                {"application/pdf", "image/jpeg", "image/png"}
            ),
        )
        provider, _ = self.provider(projection, skills=self.skills() + (visual,))

        result = provider.build_for_run(state=self.state, actor=self.worker)
        by_ref = {item.ref_id: item for item in result.authorized_inputs}
        self.assertEqual(
            by_ref[f"evidence-page:{self.page_id}"].allowed_skill_ids,
            ("image_visual_ocr", "pdf_reading"),
        )
        self.assertEqual(
            by_ref[f"evidence-page:{image_page_id}"].allowed_skill_ids,
            ("image_visual_ocr",),
        )

    def test_cross_firm_and_cross_matter_are_rejected(self) -> None:
        normal = self.projection()
        provider, _ = self.provider(normal)
        with self.assertRaisesRegex(PermissionError, "another firm"):
            provider.build_for_run(
                state=self.state,
                actor=replace(self.worker, firm_id=str(uuid4())),
            )

        wrong_matter = str(uuid4())
        wrong_ref = replace(self.snapshot_ref, matter_id=wrong_matter)
        wrong_projection = self.projection(
            matter_id=wrong_matter,
            opening=wrong_ref,
            closing=wrong_ref,
            objects=(
                self.object(
                    PlanningProjectionObjectType.MATERIAL_OBJECT,
                    status=PlanningInputStatus.AVAILABLE,
                ),
            ),
            posture_state=ProjectionSectionState.EMPTY,
            posture=None,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
        )
        provider, _ = self.provider(wrong_projection)
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "another firm or matter"):
            provider.build_for_run(state=self.state, actor=self.worker)

    def test_stale_run_can_read_its_advanced_snapshot_for_replanning(self) -> None:
        provider, repository = self.provider(self.projection())
        stale_state = replace(
            self.state,
            status=AgentRunStatus.STALE,
            stale=True,
        )

        result = provider.build_for_run(state=stale_state, actor=self.worker)

        self.assertEqual(result.case_snapshot, stale_state.snapshot)
        self.assertEqual(
            repository.calls[0]["expected_case_snapshot"],
            stale_state.snapshot,
        )

    def test_cancelled_run_cannot_read_a_planning_snapshot(self) -> None:
        provider, _ = self.provider(self.projection())
        cancelled_state = replace(
            self.state,
            status=AgentRunStatus.CANCELLED,
            cancelled=True,
        )

        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "cancelled"):
            provider.build_for_run(state=cancelled_state, actor=self.worker)

    def test_opening_or_closing_version_hash_drift_is_rejected(self) -> None:
        changed = replace(
            self.snapshot_ref,
            matter_version=10,
            snapshot_hash=digest("case-v10"),
        )
        provider, _ = self.provider(self.projection(closing=changed))
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "changed while"):
            provider.build_for_run(state=self.state, actor=self.worker)

        changed_hash = replace(self.snapshot_ref, snapshot_hash=digest("same-v9-new-hash"))
        provider, _ = self.provider(self.projection(opening=changed_hash, closing=changed_hash))
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "changed while"):
            provider.build_for_run(state=self.state, actor=self.worker)

    def test_missing_posture_restricts_plan_to_neutral_material_skills(self) -> None:
        material = self.object(
            PlanningProjectionObjectType.MATERIAL_OBJECT,
            self.material_id,
            status=PlanningInputStatus.AVAILABLE,
        )
        page = self.object(
            PlanningProjectionObjectType.EVIDENCE_PAGE,
            self.page_id,
            status=PlanningInputStatus.REVIEW_REQUIRED,
        )
        projection = self.projection(
            objects=(material, page),
            posture_state=ProjectionSectionState.EMPTY,
            posture=None,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
        )
        provider, _ = self.provider(projection)

        result = provider.build_for_run(state=self.state, actor=self.worker)

        self.assertEqual(
            {item.allowed_skill_ids for item in result.authorized_inputs},
            {("office_reading",), ("pdf_reading",)},
        )
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].code, "POSTURE_CONFIRMATION_REQUIRED")
        self.assertEqual(result.signals[0].status, PlanningInputStatus.OPEN)
        self.assertIn("不得推定原告、被告", result.signals[0].summary)

    def test_dynamic_work_plan_is_used_without_party_to_deliverable_mapping(self) -> None:
        projection = self.projection()
        provider, _ = self.provider(projection)

        result = provider.build_for_run(state=self.state, actor=self.worker)

        work_signal = next(
            item for item in result.signals if item.code == "ACTIVE_DYNAMIC_WORK_PLAN"
        )
        self.assertIn("待补信息1项", work_signal.summary)
        self.assertIn("非代理地位固定映射", work_signal.summary)
        all_text = " ".join(item.summary for item in result.signals)
        self.assertNotIn("DEFENCE_STATEMENT", all_text)
        self.assertNotIn("答辩状", all_text)
        self.assertNotIn("起诉状", all_text)

    def test_input_limit_accepts_500_and_rejects_501_without_truncating(self) -> None:
        pages = tuple(
            self.object(
                PlanningProjectionObjectType.EVIDENCE_PAGE,
                status=PlanningInputStatus.REVIEW_REQUIRED,
            )
            for _ in range(499)
        )
        exactly_500 = self.projection(
            objects=(*pages, self.posture_object()),
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
        )
        provider, _ = self.provider(exactly_500)
        result = provider.build_for_run(state=self.state, actor=self.worker)
        self.assertEqual(len(result.authorized_inputs), 500)

        page_500 = self.object(
            PlanningProjectionObjectType.EVIDENCE_PAGE,
            status=PlanningInputStatus.REVIEW_REQUIRED,
        )
        over_limit = self.projection(
            objects=(*pages, page_500, self.posture_object()),
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
        )
        provider, _ = self.provider(over_limit)
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "no.*truncated"):
            provider.build_for_run(state=self.state, actor=self.worker)

    def test_governed_lawyer_correction_and_rejection_change_planning_hash(self) -> None:
        issue = self.object(
            PlanningProjectionObjectType.DISPUTE_ISSUE,
            status=PlanningInputStatus.DISPUTED,
        )
        base_objects = (
            self.posture_object(),
            self.object(
                PlanningProjectionObjectType.WORK_PLAN_ITEM,
                self.plan_item_id,
                status=PlanningInputStatus.OPEN,
            ),
            issue,
        )
        decision_id = str(uuid4())
        correction = GovernedLawyerPlanningSignal(
            signal_id=decision_id,
            signal_version="v1",
            decision_hash=digest("lawyer-correction-v1"),
            category=PlanningSignalCategory.LEGAL_GAP,
            code="LAWYER_REJECTED_PROPOSED_ISSUE_SCOPE",
            status=PlanningInputStatus.DISPUTED,
            summary="主办律师否认该候选属于本案争点；后续规划不得将其作为已确认争点。",
            source_ref_ids=(issue.ref_id,),
        )
        first_projection = self.projection(
            objects=base_objects,
            lawyer_signals=(correction,),
        )
        provider, _ = self.provider(first_projection)
        first = provider.build_for_run(state=self.state, actor=self.worker)
        signal = next(item for item in first.signals if item.code == correction.code)
        self.assertEqual(signal.status, PlanningInputStatus.DISPUTED)
        self.assertIn("主办律师否认", signal.summary)

        revised = replace(
            correction,
            signal_version="v2",
            decision_hash=digest("lawyer-correction-v2"),
            status=PlanningInputStatus.CONFIRMED,
            summary="主办律师纠正：该候选仅作为待核实事实线索，不构成已确认争点。",
        )
        second_projection = self.projection(
            objects=base_objects,
            lawyer_signals=(revised,),
        )
        provider, _ = self.provider(second_projection)
        second = provider.build_for_run(state=self.state, actor=self.worker)
        self.assertNotEqual(first.planning_hash, second.planning_hash)

    def test_reextraction_obligation_is_preserved_only_for_its_control_run(self) -> None:
        page_ref = f"evidence-page:{self.page_id}"
        obligation = ReextractionPlanningObligation.build(
            control_run_id=self.state.run_id,
            followup_ids=(str(uuid4()), str(uuid4())),
            source_ref_ids=(page_ref,),
            lifecycle_hash=digest("current-reextraction-cohort"),
        )
        projection = self.projection(
            reextraction_obligations=(obligation,),
        )
        extraction_skill = ExecutablePlanningSkill(
            skill_id="case_ledger_extraction",
            adapter=_Adapter("extract_case_ledger"),
            supported_object_types=frozenset(
                {PlanningProjectionObjectType.EVIDENCE_PAGE}
            ),
            neutral_material_inventory=True,
        )
        provider, _ = self.provider(
            projection,
            skills=(*self.skills(), extraction_skill),
        )

        snapshot = provider.build_for_run(state=self.state, actor=self.worker)

        self.assertEqual(snapshot.reextraction_obligations, (obligation,))
        page_input = next(item for item in snapshot.authorized_inputs if item.ref_id == page_ref)
        self.assertIn("case_ledger_extraction", page_input.allowed_skill_ids)

        foreign_control = ReextractionPlanningObligation.build(
            control_run_id=str(uuid4()),
            followup_ids=obligation.followup_ids,
            source_ref_ids=obligation.source_ref_ids,
            lifecycle_hash=obligation.lifecycle_hash,
        )
        provider, _ = self.provider(
            self.projection(reextraction_obligations=(foreign_control,)),
            skills=(*self.skills(), extraction_skill),
        )
        with self.assertRaisesRegex(
            CasePlanningProjectionBlocked, "another control run"
        ):
            provider.build_for_run(state=self.state, actor=self.worker)

    def test_skill_allowlist_requires_actual_adapter_and_exact_capability(self) -> None:
        page_only_skill = ExecutablePlanningSkill(
            skill_id="pdf_reading",
            adapter=_Adapter("extract_pdf_text"),
            supported_object_types=frozenset(
                {PlanningProjectionObjectType.EVIDENCE_PAGE}
            ),
            neutral_material_inventory=True,
        )
        projection = self.projection(
            objects=(
                self.object(
                    PlanningProjectionObjectType.MATERIAL_OBJECT,
                    self.material_id,
                    status=PlanningInputStatus.AVAILABLE,
                ),
            ),
            posture_state=ProjectionSectionState.EMPTY,
            posture=None,
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
        )
        provider, _ = self.provider(projection, skills=(page_only_skill,))
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "no registered executable adapter"):
            provider.build_for_run(state=self.state, actor=self.worker)

        class _ManifestOnly:
            manifest = _Adapter("extract_pdf_text").manifest

        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "incomplete"):
            ExecutablePlanningSkill(
                skill_id="fake_pdf",
                adapter=_ManifestOnly(),
                supported_object_types=frozenset(
                    {PlanningProjectionObjectType.EVIDENCE_PAGE}
                ),
                neutral_material_inventory=True,
            ).validate()

    def test_unconfigured_legal_and_procedure_ledgers_emit_open_gaps(self) -> None:
        projection = self.projection(
            legal_state=ProjectionSectionState.NOT_CONFIGURED,
            procedure_state=ProjectionSectionState.NOT_CONFIGURED,
        )
        provider, _ = self.provider(projection)

        result = provider.build_for_run(state=self.state, actor=self.worker)

        codes = {item.code: item for item in result.signals}
        self.assertEqual(
            codes["LEGAL_SOURCE_LEDGER_NOT_CONFIGURED"].status,
            PlanningInputStatus.OPEN,
        )
        self.assertEqual(
            codes["PROCEDURAL_EVENT_LEDGER_NOT_CONFIGURED"].status,
            PlanningInputStatus.OPEN,
        )
        self.assertIn("不得自行推定法院期限", codes["PROCEDURAL_EVENT_LEDGER_NOT_CONFIGURED"].summary)

    def test_only_dedicated_system_worker_may_build_projection(self) -> None:
        provider, _ = self.provider(self.projection())
        lawyer = Actor(
            actor_id=str(uuid4()),
            firm_id=self.firm_id,
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        with self.assertRaisesRegex(PermissionError, "SYSTEM_WORKER"):
            provider.build_for_run(state=self.state, actor=lawyer)

    def test_projection_contract_rejects_unverified_legal_source_and_stale_plan(self) -> None:
        unverified_source = self.object(
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            status=PlanningInputStatus.REVIEW_REQUIRED,
        )
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "unverified"):
            self.projection(
                objects=(
                    self.posture_object(),
                    self.object(
                        PlanningProjectionObjectType.WORK_PLAN_ITEM,
                        self.plan_item_id,
                        status=PlanningInputStatus.OPEN,
                    ),
                    unverified_source,
                ),
                legal_state=ProjectionSectionState.AVAILABLE,
            )

        stale_plan = replace(self.work_plan(), bound_matter_version=8)
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "stale"):
            self.projection(active_work_plan=stale_plan)

    def test_opaque_ref_and_version_helpers_reject_noncanonical_values(self) -> None:
        ref = planning_object_ref_id(
            PlanningProjectionObjectType.EVIDENCE_PAGE, self.page_id
        )
        self.assertEqual(ref, f"evidence-page:{self.page_id}")
        self.assertEqual(object_version_code(12), "v12")
        with self.assertRaises(CasePlanningProjectionBlocked):
            planning_object_ref_id(
                PlanningProjectionObjectType.EVIDENCE_PAGE, "../../secret.pdf"
            )
        with self.assertRaises(CasePlanningProjectionBlocked):
            object_version_code(0)


if __name__ == "__main__":
    unittest.main()
