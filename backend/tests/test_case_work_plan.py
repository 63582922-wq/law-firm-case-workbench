from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import unittest

from case_kernel.case_work_plan import (
    CaseWorkPlanBlocked,
    CaseWorkPlanCandidate,
    CaseWorkPlanItem,
    DeliveryTarget,
    LawyerObjectiveRef,
    PostureProfileRef,
    ResolvedWorkPlanReference,
    ReviewGate,
    WorkPlanItemKind,
    WorkPlanReadiness,
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
    activate_case_work_plan,
    build_case_work_plan_context,
    case_work_plan_candidate_input_hash,
    validate_case_work_plan_candidate,
)
from case_kernel.models import Actor, Role


def digest(label: str) -> str:
    return sha256(label.encode()).hexdigest()


class CaseWorkPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.firm_id = str(uuid4())
        self.posture = PostureProfileRef(str(uuid4()), 3, digest("posture"))
        self.objective = LawyerObjectiveRef(str(uuid4()), 21, digest("objective"))
        self.claim = WorkPlanReference(
            WorkPlanSourceType.CLAIM,
            str(uuid4()),
            "7",
            digest("claim"),
            WorkPlanReferenceUse.CLAIM_SCOPE,
        )
        self.source = WorkPlanReference(
            WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT,
            str(uuid4()),
            "2026-08-13T00:00:00+00:00",
            digest("source"),
            WorkPlanReferenceUse.LEGAL_AUTHORITY,
        )
        self.rule = WorkPlanReference(
            WorkPlanSourceType.LEGAL_RULE_VERSION,
            str(uuid4()),
            "RULE-2",
            digest("rule"),
            WorkPlanReferenceUse.LEGAL_RULE,
        )
        self.evidence = WorkPlanReference(
            WorkPlanSourceType.EVIDENCE_PAGE,
            str(uuid4()),
            "1",
            digest("evidence"),
            WorkPlanReferenceUse.EVIDENCE,
        )
        self.context = build_case_work_plan_context(
            matter_id=self.matter_id,
            matter_version=21,
            posture=self.posture,
            confirmed_claim_refs=(self.claim,),
            legal_source_refs=(self.source,),
            legal_rule_refs=(self.rule,),
            eligible_source_refs=(self.evidence,),
            objective=self.objective,
        )
        self.resolved = {
            (item.source_type, item.source_id): ResolvedWorkPlanReference(
                matter_id=self.matter_id,
                source_type=item.source_type,
                source_id=item.source_id,
                source_version=item.source_version,
                source_hash=item.source_hash,
                is_current=True,
                is_confirmed=True,
            )
            for item in (*self.context.all_references, self.evidence)
        }

    def resolver(self, reference: WorkPlanReference):
        return self.resolved.get((reference.source_type, reference.source_id))

    def item(self, *, deliverable_kind: str, primary: bool = True) -> CaseWorkPlanItem:
        return CaseWorkPlanItem(
            item_id=str(uuid4()),
            sequence=1,
            kind=WorkPlanItemKind.DOCUMENT_CANDIDATE,
            readiness=WorkPlanReadiness.ACTIONABLE,
            title="根据已确认范围起草程序文书候选",
            purpose="形成一份与当前案件范围相符的可复核文书候选。",
            rationale="当前诉请范围、律师目标、案件证据和已审查法源共同支持该候选。",
            prerequisites=(),
            trigger_refs=(self.posture.as_reference(), self.claim, self.rule),
            source_refs=(self.evidence, self.source, self.rule),
            risk_if_omitted="可能遗漏已由当前程序和诉请触发的法院材料。",
            confidence=0.86,
            review_gate=ReviewGate.LEAD_LAWYER_CONFIRMATION,
            delivery_target=DeliveryTarget.COURT_SUBMISSION,
            deliverable_kind=deliverable_kind,
            required_for_delivery=True,
            is_primary_document=primary,
        )

    def candidate(self, item: CaseWorkPlanItem) -> CaseWorkPlanCandidate:
        return CaseWorkPlanCandidate(
            agent_id="case-work-planner",
            agent_version="1.0.0",
            candidate_input_hash=case_work_plan_candidate_input_hash(self.context),
            generated_at=datetime.now(timezone.utc),
            context=self.context,
            items=(item,),
        )

    def test_plaintiff_and_defendant_are_not_hard_coded(self) -> None:
        complaint = validate_case_work_plan_candidate(
            self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
            resolve_reference=self.resolver,
        )
        defence = validate_case_work_plan_candidate(
            self.candidate(self.item(deliverable_kind="DEFENCE_STATEMENT")),
            resolve_reference=self.resolver,
        )
        self.assertEqual(complaint.required_court_document_kinds, ("CIVIL_COMPLAINT",))
        self.assertEqual(defence.required_court_document_kinds, ("DEFENCE_STATEMENT",))
        self.assertNotEqual(complaint.plan_hash, defence.plan_hash)

    def test_information_gap_cannot_be_required_delivery(self) -> None:
        unsupported = replace(
            self.item(deliverable_kind="CIVIL_COMPLAINT"),
            readiness=WorkPlanReadiness.NEEDS_INFORMATION,
        )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "cannot become formal deliverables"):
            validate_case_work_plan_candidate(
                self.candidate(unsupported), resolve_reference=self.resolver
            )

    def test_actionable_item_requires_source_not_just_model_rationale(self) -> None:
        unsupported = replace(
            self.item(deliverable_kind="CIVIL_COMPLAINT"), source_refs=()
        )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "requires current source"):
            validate_case_work_plan_candidate(
                self.candidate(unsupported), resolve_reference=self.resolver
            )

    def test_missing_claim_scope_allows_intake_plan_but_not_court_document(self) -> None:
        self.context = build_case_work_plan_context(
            matter_id=self.matter_id,
            matter_version=21,
            posture=self.posture,
            confirmed_claim_refs=(),
            objective=self.objective,
        )
        self.resolved = {
            (item.source_type, item.source_id): ResolvedWorkPlanReference(
                matter_id=self.matter_id,
                source_type=item.source_type,
                source_id=item.source_id,
                source_version=item.source_version,
                source_hash=item.source_hash,
                is_current=True,
                is_confirmed=True,
            )
            for item in self.context.all_references
        }
        request = CaseWorkPlanItem(
            item_id=str(uuid4()),
            sequence=1,
            kind=WorkPlanItemKind.MATERIAL_REQUEST,
            readiness=WorkPlanReadiness.NEEDS_INFORMATION,
            title="取得起诉状并核验诉请范围",
            purpose="补齐决定后续工作范围所必需的诉请资料。",
            rationale="当前尚无已确认诉请范围，不能开始法院文书交付。",
            prerequisites=(),
            trigger_refs=(self.posture.as_reference(), self.objective.as_reference()),
            source_refs=(),
            risk_if_omitted="在不清楚诉请范围时可能错误确定交付物。",
            confidence=1.0,
            review_gate=ReviewGate.LEAD_LAWYER_CONFIRMATION,
        )
        plan = validate_case_work_plan_candidate(
            self.candidate(request), resolve_reference=self.resolver
        )
        self.assertEqual(plan.required_court_document_kinds, ())

        unsupported_document = replace(
            request,
            item_id=str(uuid4()),
            kind=WorkPlanItemKind.DOCUMENT_CANDIDATE,
            readiness=WorkPlanReadiness.ACTIONABLE,
            source_refs=(self.posture.as_reference(),),
            delivery_target=DeliveryTarget.COURT_SUBMISSION,
            deliverable_kind="DEFENCE_STATEMENT",
            required_for_delivery=True,
            is_primary_document=True,
        )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "claim scope"):
            validate_case_work_plan_candidate(
                self.candidate(unsupported_document), resolve_reference=self.resolver
            )

    def test_stale_authority_blocks_candidate(self) -> None:
        current = self.resolved[(self.source.source_type, self.source.source_id)]
        self.resolved[(self.source.source_type, self.source.source_id)] = replace(
            current, is_effective=False
        )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "stale, unconfirmed or not effective"):
            validate_case_work_plan_candidate(
                self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
                resolve_reference=self.resolver,
            )

    def test_changed_posture_hash_blocks_candidate(self) -> None:
        current = self.resolved[(WorkPlanSourceType.POSTURE_PROFILE, self.posture.profile_id)]
        self.resolved[(WorkPlanSourceType.POSTURE_PROFILE, self.posture.profile_id)] = replace(
            current, source_hash=digest("changed")
        )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "version or hash changed"):
            validate_case_work_plan_candidate(
                self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
                resolve_reference=self.resolver,
            )

    def test_only_lead_lawyer_can_activate_exact_plan_hash(self) -> None:
        plan = validate_case_work_plan_candidate(
            self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
            resolve_reference=self.resolver,
        )
        assistant = Actor(str(uuid4()), self.firm_id, frozenset({Role.ASSISTANT}))
        with self.assertRaises(PermissionError):
            activate_case_work_plan(plan, actor=assistant, confirmation_hash=plan.plan_hash)
        lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        active = activate_case_work_plan(plan, actor=lead, confirmation_hash=plan.plan_hash)
        self.assertEqual(active.status, "ACTIVE")
        self.assertEqual(active.primary_court_document_kind, "CIVIL_COMPLAINT")

    def test_model_cannot_replace_server_owned_context(self) -> None:
        changed = replace(self.context, matter_version=self.context.matter_version + 1)
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "server-owned planning snapshot"):
            validate_case_work_plan_candidate(
                replace(
                    self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
                    context=changed,
                ),
                authoritative_context=self.context,
                resolve_reference=self.resolver,
            )

    def test_unresolved_competing_rules_block_plan(self) -> None:
        competing = replace(
            self.rule,
            source_id=str(uuid4()),
            source_version="RULE-3",
            source_hash=digest("rule-3"),
        )
        self.context = build_case_work_plan_context(
            matter_id=self.matter_id,
            matter_version=21,
            posture=self.posture,
            confirmed_claim_refs=(self.claim,),
            legal_source_refs=(self.source,),
            legal_rule_refs=(self.rule, competing),
            eligible_source_refs=(self.evidence,),
            objective=self.objective,
        )
        for rule in (self.rule, competing):
            self.resolved[(rule.source_type, rule.source_id)] = ResolvedWorkPlanReference(
                matter_id=self.matter_id,
                source_type=rule.source_type,
                source_id=rule.source_id,
                source_version=rule.source_version,
                source_hash=rule.source_hash,
                is_current=True,
                is_confirmed=True,
                conflict_key="INTEREST_RATE_BASIS",
            )
        with self.assertRaisesRegex(CaseWorkPlanBlocked, "unresolved conflicting"):
            validate_case_work_plan_candidate(
                self.candidate(self.item(deliverable_kind="CIVIL_COMPLAINT")),
                resolve_reference=self.resolver,
            )


if __name__ == "__main__":
    unittest.main()
