from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_memory import (
    MemoryAuthority,
    MemoryLayer,
    SourceExposure,
)
from case_kernel.case_agent_planner import (
    CasePlanningSignal,
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlanningSignalCategory,
    planning_snapshot_public_payload,
)
from case_kernel.case_agent_planning_memory import (
    AuthorizedPlanningLegalPeriod,
    DynamicPlanningMemorySearchRequestFactory,
    MemoryEnrichedPlanningSnapshotProvider,
    PlanningMemoryBlocked,
    PlanningMemoryItem,
    PlanningMemoryPurpose,
    PlanningMemorySearchRequest,
    PlanningMemorySourceRef,
    build_receipt,
)
from case_kernel.case_agent_supervisor import (
    AgentGoal,
    AgentRunState,
    AgentRunStatus,
    BudgetUsage,
    CaseSnapshotRef,
    RunResourceBudget,
)
from case_kernel.models import Actor, Role


def uid() -> str:
    return str(uuid4())


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _BaseProvider:
    def __init__(self, snapshot: CasePlanningSnapshot) -> None:
        self.snapshot = snapshot

    def build_for_run(self, **_: object) -> CasePlanningSnapshot:
        return self.snapshot


class _EnrichmentPort:
    def __init__(self, receipt) -> None:
        self.receipt = receipt

    def current_enrichment(self, **_: object):
        return self.receipt


class _LegalPeriodResolver:
    def __init__(self, period: AuthorizedPlanningLegalPeriod | None) -> None:
        self.period = period

    def current_period(self, **_: object) -> AuthorizedPlanningLegalPeriod | None:
        return self.period


class _RequestFactory:
    def __init__(self, request: PlanningMemorySearchRequest) -> None:
        self.request = request
        self.base_snapshot: CasePlanningSnapshot | None = None

    def build_for_run(
        self, *, base_snapshot: CasePlanningSnapshot, **_: object
    ) -> PlanningMemorySearchRequest:
        self.base_snapshot = base_snapshot
        return self.request


class PlanningMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = uid()
        self.matter_id = uid()
        self.owner_id = uid()
        self.run_id = uid()
        self.worker = Actor(uid(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.case_snapshot = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=7,
            snapshot_hash=digest("case-snapshot"),
            schema_version="case-ledger-snapshot-v1",
        )
        self.goal = AgentGoal.build(
            goal_id=uid(),
            objective="审查本案材料并动态规划下一步",
            success_criteria=("给出有来源的任务计划",),
            constraints=("不得编造法源",),
            requested_by=self.owner_id,
        )
        self.state = AgentRunState(
            run_id=self.run_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            event_version=1,
            goal=self.goal,
            snapshot=self.case_snapshot,
            budget=RunResourceBudget(
                max_tasks=20,
                max_total_attempts=40,
                max_external_calls=10,
                max_cost_minor_units=10000,
                max_runtime_seconds=3600,
                max_output_bytes=10_000_000,
            ),
            status=AgentRunStatus.CREATED,
            graph=None,
            tasks=(),
            approvals=(),
            artifacts=(),
            budget_usage=BudgetUsage(),
        )
        self.base_ref = PlanningInputRef(
            ref_id=f"material-object:{uid()}",
            kind=PlanningInputKind.MATERIAL,
            object_version="v1",
            content_hash=digest("material"),
            status=PlanningInputStatus.AVAILABLE,
            allowed_skill_ids=("material_inventory",),
        )
        self.base = CasePlanningSnapshot.build(
            case_snapshot=self.case_snapshot,
            authorized_inputs=(self.base_ref,),
            signals=(
                CasePlanningSignal(
                    signal_id="signal:base",
                    category=PlanningSignalCategory.WORK_PLAN,
                    code="BASE_SIGNAL",
                    status=PlanningInputStatus.OPEN,
                    summary="等待动态规划。",
                    source_ref_ids=(self.base_ref.ref_id,),
                ),
            ),
        )
        self.request = PlanningMemorySearchRequest(
            purpose=PlanningMemoryPurpose.DYNAMIC_CASE_PLANNING,
            query_text="借款利息、还款记录和当前有效法律规则",
            layers=(MemoryLayer.CASE_LONG_TERM, MemoryLayer.PUBLIC_LEGAL),
            allowed_skill_ids=("legal_rule_research_planning",),
            legal_period_start=date(2019, 1, 1),
            legal_period_end=date(2026, 8, 13),
            legal_period_source_ref_ids=(self.base_ref.ref_id,),
            legal_period_binding_hash=digest("legal-period-binding"),
        )

    def item(
        self,
        *,
        layer: MemoryLayer,
        exposure: SourceExposure,
        externally_disclosable: bool,
        authorization_hash: str | None = None,
    ) -> PlanningMemoryItem:
        return PlanningMemoryItem.build(
            record_id=uid(),
            record_version=2,
            layer=layer,
            authority=(
                MemoryAuthority.CONFIRMED_CASE_LEDGER
                if layer is MemoryLayer.CASE_LONG_TERM
                else MemoryAuthority.PRIMARY_LAW
            ),
            content_hash=digest(f"content:{layer}"),
            provenance_hash=digest(f"provenance:{layer}"),
            summary="已确认还款记录与利息争点。" if layer is MemoryLayer.CASE_LONG_TERM else "当前有效法律规则候选，须复核适用期间。",
            source_refs=(
                PlanningMemorySourceRef(
                    source_type=(
                        "CASE_FACT"
                        if layer is MemoryLayer.CASE_LONG_TERM
                        else "OFFICIAL_LEGAL_SNAPSHOT"
                    ),
                    source_id=uid(),
                    source_version="v1",
                    content_hash=digest(f"source:{layer}"),
                    exposure=exposure,
                    page_number=None,
                ),
            ),
            externally_disclosable=externally_disclosable,
            external_authorization_hash=authorization_hash,
        )

    def receipt(self, *items: PlanningMemoryItem):
        now = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
        return build_receipt(
            enrichment_id=uid(),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            run_id=self.run_id,
            goal_id=self.goal.goal_id,
            goal_hash=self.goal.goal_hash,
            owner_actor_id=self.owner_id,
            purpose=self.request.purpose,
            case_snapshot_hash=self.case_snapshot.snapshot_hash,
            case_snapshot_version=self.case_snapshot.matter_version,
            case_snapshot_schema_version=self.case_snapshot.schema_version,
            base_planning_hash=self.base.planning_hash,
            query_hash=self.request.query_hash,
            query_contract_hash=self.request.query_contract_hash,
            query_fingerprint=digest("query-fingerprint"),
            retrieval_id=uid(),
            retrieval_scope_hash=digest("scope"),
            owner_grant_hash=digest("grant"),
            owner_roles=(Role.LEAD_LAWYER,),
            permission_group_ids=(uid(),),
            items=items,
            retrieved_at=now,
            verified_at=now,
            final_verified_at=now,
        )

    def test_query_contract_hash_covers_filters_and_skill_exposure(self) -> None:
        changed_filter = replace(self.request, issue_tags=("interest",))
        changed_skill = replace(
            self.request, allowed_skill_ids=("material_inventory",)
        )
        changed_external_boundary = replace(
            self.request,
            external_planner_contract_hash=digest("deepseek-contract"),
        )
        self.assertEqual(self.request.query_hash, changed_filter.query_hash)
        self.assertNotEqual(
            self.request.query_contract_hash, changed_filter.query_contract_hash
        )
        self.assertNotEqual(
            self.request.query_contract_hash, changed_skill.query_contract_hash
        )
        self.assertNotEqual(
            self.request.query_contract_hash,
            changed_external_boundary.query_contract_hash,
        )

    def test_dynamic_request_uses_current_goal_and_signals_without_guessing_period(self) -> None:
        factory = DynamicPlanningMemorySearchRequestFactory(
            allowed_skill_ids=("legal_rule_research_planning",),
        )
        request = factory.build_for_run(state=self.state, base_snapshot=self.base)
        self.assertIn(self.goal.objective, request.query_text)
        self.assertIn("等待动态规划", request.query_text)
        self.assertNotIn(MemoryLayer.PUBLIC_LEGAL, request.layers)
        self.assertIsNone(request.legal_period_start)
        self.assertEqual(request.legal_period_source_ref_ids, ())
        self.assertEqual(request.issue_tags, ("BASE_SIGNAL",))

        changed_base = CasePlanningSnapshot.build(
            case_snapshot=self.case_snapshot,
            authorized_inputs=(self.base_ref,),
            signals=(
                replace(
                    self.base.signals[0],
                    summary="被告已确认借款本金，但利息期间仍有争议。",
                ),
            ),
        )
        changed = factory.build_for_run(
            state=self.state,
            base_snapshot=changed_base,
        )
        self.assertNotEqual(request.query_hash, changed.query_hash)

    def test_public_legal_requires_period_bound_to_authorized_current_input(self) -> None:
        period = AuthorizedPlanningLegalPeriod.build(
            period_start=date(2019, 6, 17),
            period_end=date(2026, 8, 13),
            source_ref_ids=(self.base_ref.ref_id,),
            base_planning_hash=self.base.planning_hash,
        )
        factory = DynamicPlanningMemorySearchRequestFactory(
            allowed_skill_ids=("legal_rule_research_planning",),
            legal_period_resolver=_LegalPeriodResolver(period),
        )
        request = factory.build_for_run(state=self.state, base_snapshot=self.base)
        self.assertIn(MemoryLayer.PUBLIC_LEGAL, request.layers)
        self.assertEqual(request.legal_period_start, date(2019, 6, 17))
        self.assertEqual(
            request.legal_period_source_ref_ids,
            (self.base_ref.ref_id,),
        )
        self.assertEqual(request.legal_period_binding_hash, period.binding_hash)

        unauthorized = AuthorizedPlanningLegalPeriod.build(
            period_start=date(2019, 6, 17),
            period_end=date(2026, 8, 13),
            source_ref_ids=(f"material-object:{uid()}",),
            base_planning_hash=self.base.planning_hash,
        )
        blocked_factory = DynamicPlanningMemorySearchRequestFactory(
            allowed_skill_ids=("legal_rule_research_planning",),
            legal_period_resolver=_LegalPeriodResolver(unauthorized),
        )
        with self.assertRaisesRegex(PlanningMemoryBlocked, "unauthorized source"):
            blocked_factory.build_for_run(state=self.state, base_snapshot=self.base)

    def test_composite_provider_builds_dynamic_request_after_base_snapshot(self) -> None:
        public = self.item(
            layer=MemoryLayer.PUBLIC_LEGAL,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            externally_disclosable=True,
        )
        factory = _RequestFactory(self.request)
        provider = MemoryEnrichedPlanningSnapshotProvider(
            base_provider=_BaseProvider(self.base),
            enrichment_port=_EnrichmentPort(self.receipt(public)),
            request_factory=factory,
        )
        enriched = provider.build_for_run(state=self.state, actor=self.worker)
        self.assertIs(factory.base_snapshot, self.base)
        self.assertNotEqual(enriched.planning_hash, self.base.planning_hash)

    def test_composite_provider_adds_hash_bound_memory_refs(self) -> None:
        public = self.item(
            layer=MemoryLayer.PUBLIC_LEGAL,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            externally_disclosable=True,
        )
        provider = MemoryEnrichedPlanningSnapshotProvider(
            base_provider=_BaseProvider(self.base),
            enrichment_port=_EnrichmentPort(self.receipt(public)),
            request=self.request,
        )
        enriched = provider.build_for_run(state=self.state, actor=self.worker)
        memory = next(
            item
            for item in enriched.authorized_inputs
            if item.kind is PlanningInputKind.AUTHORIZED_MEMORY
        )
        self.assertEqual(memory.content_hash, public.content_hash)
        self.assertTrue(memory.planner_visible)
        self.assertNotEqual(enriched.planning_hash, self.base.planning_hash)

    def test_private_case_memory_is_server_only_without_exact_external_grant(self) -> None:
        private = self.item(
            layer=MemoryLayer.CASE_LONG_TERM,
            exposure=SourceExposure.CASE_PRIVATE,
            externally_disclosable=False,
        )
        provider = MemoryEnrichedPlanningSnapshotProvider(
            base_provider=_BaseProvider(self.base),
            enrichment_port=_EnrichmentPort(self.receipt(private)),
            request=self.request,
        )
        enriched = provider.build_for_run(state=self.state, actor=self.worker)
        full_ids = {item.ref_id for item in enriched.authorized_inputs}
        external = planning_snapshot_public_payload(enriched)
        external_ids = {item["ref_id"] for item in external["authorized_inputs"]}
        self.assertIn(private.ref_id, full_ids)
        self.assertNotIn(private.ref_id, external_ids)
        self.assertFalse(
            any(private.summary == signal["summary"] for signal in external["signals"])
        )

    def test_receipt_bound_to_other_goal_or_snapshot_is_rejected(self) -> None:
        public = self.item(
            layer=MemoryLayer.PUBLIC_LEGAL,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            externally_disclosable=True,
        )
        wrong = replace(self.receipt(public), goal_hash=digest("other-goal"))
        provider = MemoryEnrichedPlanningSnapshotProvider(
            base_provider=_BaseProvider(self.base),
            enrichment_port=_EnrichmentPort(wrong),
            request=self.request,
        )
        with self.assertRaisesRegex(PlanningMemoryBlocked, "receipt hash"):
            provider.build_for_run(state=self.state, actor=self.worker)

    def test_system_worker_cannot_become_owner_or_bypass_disclosure(self) -> None:
        valid = self.receipt(
            self.item(
                layer=MemoryLayer.PUBLIC_LEGAL,
                exposure=SourceExposure.PUBLIC_OFFICIAL,
                externally_disclosable=True,
            )
        )
        invalid = valid.__class__(
                **{
                    **valid.__dict__,
                    "owner_roles": (Role.SYSTEM_WORKER,),
                }
        )
        with self.assertRaisesRegex(PlanningMemoryBlocked, "SYSTEM_WORKER"):
            invalid.validate()


if __name__ == "__main__":
    unittest.main()
