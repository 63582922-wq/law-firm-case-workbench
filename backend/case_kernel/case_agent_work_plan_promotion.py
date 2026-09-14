"""Promote one independently verified Agent task graph into a 0030 candidate.

This module is intentionally deterministic.  It does not ask a model which
documents a plaintiff or defendant should file.  Every candidate item mirrors
one server-compiled task, preserves its purpose/dependencies/sources, and stays
an internal ``CANDIDATE`` until the existing lead-lawyer activation gate is
used.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import re
from uuid import UUID, uuid5

from .case_agent_planning_snapshot import (
    AuthoritativeCasePlanningProjection,
    AuthoritativePlanningObject,
    PlanningProjectionObjectType,
    ProjectionSectionState,
    planning_object_ref_id,
)
from .case_agent_planner import PlanningInputStatus
from .case_agent_supervisor import (
    AgentDeliverableKind,
    AgentRiskLevel,
    CaseSnapshotRef,
)
from .case_agent_verifier import VerificationOutcome
from .case_work_plan import (
    AgentGoalRef,
    CaseWorkPlanCandidate,
    CaseWorkPlanItem,
    DeliveryTarget,
    PostureProfileRef,
    ReviewGate,
    WorkPlanItemKind,
    WorkPlanReadiness,
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
    build_case_work_plan_context,
    case_work_plan_candidate_input_hash,
)
from .skill_registry import ApprovalGate


PROMOTION_AGENT_ID = "case-agent-verified-graph-promotion"
PROMOTION_AGENT_VERSION = "1.0.0"


class AgentWorkPlanPromotionBlocked(ValueError):
    """The graph is not a current, independently verified promotion source."""


@dataclass(frozen=True)
class VerifiedGraphTask:
    task_id: str
    sequence: int
    title: str
    purpose: str
    rationale: str
    dependency_ids: tuple[str, ...]
    input_refs: tuple[str, ...]
    skill_id: str
    risk_level: AgentRiskLevel
    approval_gate: ApprovalGate


@dataclass(frozen=True)
class VerifiedGraphPromotionSource:
    run_id: str
    run_status: str
    run_is_stale: bool
    run_is_cancelled: bool
    graph_id: str
    graph_version: int
    graph_hash: str
    snapshot: CaseSnapshotRef
    goal_id: str
    goal_hash: str
    requested_deliverables: tuple[AgentDeliverableKind, ...]
    verification_receipt_id: str
    verification_outcome: VerificationOutcome
    verification_graph_hash: str
    verification_snapshot_hash: str
    verification_hash: str
    run_verification_hash: str
    verifier_actor_id: str
    execution_actor_id: str
    verified_at: datetime
    tasks: tuple[VerifiedGraphTask, ...]


@dataclass(frozen=True)
class PromotedTaskInputBinding:
    binding_id: str
    input_ref: str
    object_type: PlanningProjectionObjectType
    object_id: str
    object_version: str
    content_hash: str
    source_status: str
    reference_use: WorkPlanReferenceUse
    binding_hash: str

    def as_reference(self) -> WorkPlanReference:
        return WorkPlanReference(
            source_type=WorkPlanSourceType.AGENT_TASK_INPUT,
            source_id=self.binding_id,
            source_version=self.object_version,
            source_hash=self.binding_hash,
            use=self.reference_use,
        )


@dataclass(frozen=True)
class CompiledAgentWorkPlanPromotion:
    source: VerifiedGraphPromotionSource
    candidate: CaseWorkPlanCandidate
    bindings: tuple[PromotedTaskInputBinding, ...]


def compile_verified_graph_work_plan_candidate(
    *,
    source: VerifiedGraphPromotionSource,
    projection: AuthoritativeCasePlanningProjection,
) -> CompiledAgentWorkPlanPromotion:
    """Compile a current PASSED graph without inventing a litigation template."""

    validate_verified_graph_promotion_source(source)
    projection.validate()
    if (
        projection.firm_id == ""
        or projection.matter_id != source.snapshot.matter_id
        or projection.opening_case_snapshot != source.snapshot
        or projection.closing_case_snapshot != source.snapshot
    ):
        raise AgentWorkPlanPromotionBlocked(
            "verified graph differs from the current authoritative planning snapshot"
        )
    if (
        projection.posture_state is not ProjectionSectionState.AVAILABLE
        or projection.posture is None
    ):
        raise AgentWorkPlanPromotionBlocked(
            "verified graph promotion requires a current confirmed posture profile"
        )

    object_by_ref = {item.ref_id: item for item in projection.objects}
    task_input_refs = tuple(
        dict.fromkeys(ref for task in source.tasks for ref in task.input_refs)
    )
    unknown = tuple(ref for ref in task_input_refs if ref not in object_by_ref)
    if unknown:
        raise AgentWorkPlanPromotionBlocked(
            "verified graph cites an input outside the current server projection"
        )
    posture_ref = planning_object_ref_id(
        PlanningProjectionObjectType.POSTURE_PROFILE,
        projection.posture.profile_id,
    )
    if posture_ref not in task_input_refs:
        raise AgentWorkPlanPromotionBlocked(
            "verified graph is not bound to the current posture input"
        )

    promotion_input_refs = list(task_input_refs)
    if (
        AgentDeliverableKind.CASE_REVIEW_MEMO in source.requested_deliverables
        or AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST
        in source.requested_deliverables
    ):
        fact_refs = tuple(
            item.ref_id
            for item in projection.objects
            if item.object_type is PlanningProjectionObjectType.CASE_FACT
            and item.status is PlanningInputStatus.CONFIRMED
        )
        for ref in fact_refs:
            if ref not in promotion_input_refs:
                promotion_input_refs.append(ref)
    if AgentDeliverableKind.PAYMENT_LEDGER in source.requested_deliverables:
        transaction_refs = tuple(
            item.ref_id
            for item in projection.objects
            if item.object_type is PlanningProjectionObjectType.CASE_TRANSACTION
            and item.status is PlanningInputStatus.CONFIRMED
        )
        for ref in transaction_refs:
            if ref not in promotion_input_refs:
                promotion_input_refs.append(ref)
    if AgentDeliverableKind.EVIDENCE_CATALOGUE in source.requested_deliverables:
        for ref in (
            item.ref_id
            for item in projection.objects
            if item.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE
            and item.status is PlanningInputStatus.CONFIRMED
        ):
            if ref not in promotion_input_refs:
                promotion_input_refs.append(ref)
    if AgentDeliverableKind.DEFENCE_STATEMENT in source.requested_deliverables:
        # A response candidate is not inferred from the party label.  The
        # server takes the exact confirmed defendant-side record set from this
        # verified snapshot; the later binder rereads every item before any
        # text reaches the deterministic compiler.
        for object_type, required_status in (
            (PlanningProjectionObjectType.CASE_FACT, PlanningInputStatus.CONFIRMED),
            (PlanningProjectionObjectType.CASE_CLAIM, PlanningInputStatus.CONFIRMED),
            (
                PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                PlanningInputStatus.LOCKED,
            ),
            (
                PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                PlanningInputStatus.LOCKED,
            ),
        ):
            for ref in (
                item.ref_id
                for item in projection.objects
                if item.object_type is object_type and item.status is required_status
            ):
                if ref not in promotion_input_refs:
                    promotion_input_refs.append(ref)
    bindings = tuple(
        _binding(source=source, item=object_by_ref[ref])
        for ref in promotion_input_refs
    )
    binding_by_ref = {item.input_ref: item for item in bindings}
    posture_version = projection.posture.profile_version
    if not posture_version.startswith("v") or not posture_version[1:].isdigit():
        raise AgentWorkPlanPromotionBlocked("posture profile version is not promotable")
    context = build_case_work_plan_context(
        matter_id=source.snapshot.matter_id,
        matter_version=source.snapshot.matter_version,
        posture=PostureProfileRef(
            projection.posture.profile_id,
            int(posture_version[1:]),
            projection.posture.profile_hash,
        ),
        confirmed_claim_refs=(),
        eligible_source_refs=tuple(item.as_reference() for item in bindings),
        objective=AgentGoalRef(source.goal_id, source.goal_hash),
    )
    task_items = _disambiguate_repeated_task_titles(tuple(
        _work_plan_item(task=task, binding_by_ref=binding_by_ref)
        for task in source.tasks
    ))
    deliverable_items = _requested_deliverable_items(
        source=source,
        bindings=bindings,
        posture_reference=context.posture.as_reference(),
        posture=projection.posture,
        start_sequence=len(task_items) + 1,
    )
    if len(task_items) + len(deliverable_items) > 200:
        raise AgentWorkPlanPromotionBlocked(
            "verified graph leaves no capacity for requested deliverable candidates"
        )
    items = (*task_items, *deliverable_items)
    candidate = CaseWorkPlanCandidate(
        agent_id=PROMOTION_AGENT_ID,
        agent_version=PROMOTION_AGENT_VERSION,
        candidate_input_hash=case_work_plan_candidate_input_hash(context),
        generated_at=source.verified_at,
        context=context,
        items=items,
    )
    return CompiledAgentWorkPlanPromotion(source, candidate, bindings)


def validate_verified_graph_promotion_source(
    source: VerifiedGraphPromotionSource,
) -> None:
    """Validate the current server-owned run/graph/receipt projection."""

    for value, label in (
        (source.run_id, "run_id"),
        (source.graph_id, "graph_id"),
        (source.goal_id, "goal_id"),
        (source.verification_receipt_id, "verification_receipt_id"),
        (source.verifier_actor_id, "verifier_actor_id"),
        (source.execution_actor_id, "execution_actor_id"),
    ):
        _uuid(value, label)
    if source.run_status not in {"READY_FOR_REVIEW", "COMPLETED"}:
        raise AgentWorkPlanPromotionBlocked(
            "Agent run is not ready for a lawyer-reviewed plan candidate"
        )
    if source.run_is_stale or source.run_is_cancelled:
        raise AgentWorkPlanPromotionBlocked("stale or cancelled Agent run cannot be promoted")
    if not isinstance(source.graph_version, int) or source.graph_version < 1:
        raise AgentWorkPlanPromotionBlocked("graph_version is invalid")
    try:
        source.snapshot.validate()
    except Exception as error:
        raise AgentWorkPlanPromotionBlocked("graph snapshot is invalid") from error
    for value, label in (
        (source.graph_hash, "graph_hash"),
        (source.goal_hash, "goal_hash"),
        (source.verification_graph_hash, "verification graph_hash"),
        (source.verification_snapshot_hash, "verification snapshot_hash"),
        (source.verification_hash, "verification_hash"),
        (source.run_verification_hash, "run verification_hash"),
    ):
        _hash(value, label)
    if source.verification_outcome is not VerificationOutcome.PASSED:
        raise AgentWorkPlanPromotionBlocked("only a PASSED verification may be promoted")
    if (
        source.verification_graph_hash != source.graph_hash
        or source.verification_snapshot_hash != source.snapshot.snapshot_hash
        or source.verification_hash != source.run_verification_hash
    ):
        raise AgentWorkPlanPromotionBlocked(
            "verification receipt differs from the current graph or snapshot"
        )
    if source.verifier_actor_id == source.execution_actor_id:
        raise AgentWorkPlanPromotionBlocked("verification is not independently identified")
    if source.verified_at.tzinfo is None:
        raise AgentWorkPlanPromotionBlocked("verified_at must include a timezone")
    if not source.tasks or len(source.tasks) > 200:
        raise AgentWorkPlanPromotionBlocked("verified graph task count is invalid")
    ids = {task.task_id for task in source.tasks}
    if len(ids) != len(source.tasks):
        raise AgentWorkPlanPromotionBlocked("verified graph has duplicate tasks")
    sequences = {task.sequence for task in source.tasks}
    if sequences != set(range(1, len(source.tasks) + 1)):
        raise AgentWorkPlanPromotionBlocked("verified graph task sequence is invalid")
    for task in source.tasks:
        _validate_task(task, ids)
    if tuple(
        sorted(set(source.requested_deliverables), key=lambda item: item.value)
    ) != source.requested_deliverables:
        raise AgentWorkPlanPromotionBlocked(
            "requested deliverables are not a canonical server catalogue set"
        )
    if any(
        not isinstance(item, AgentDeliverableKind)
        for item in source.requested_deliverables
    ):
        raise AgentWorkPlanPromotionBlocked("requested deliverable kind is invalid")


def _validate_task(task: VerifiedGraphTask, task_ids: set[str]) -> None:
    _uuid(task.task_id, "task_id")
    _text(task.title, "task title", 500)
    _text(task.purpose, "task purpose", 2000)
    _text(task.rationale, "task rationale", 4000)
    if not task.input_refs or len(task.input_refs) > 500:
        raise AgentWorkPlanPromotionBlocked("verified task input references are invalid")
    if len(set(task.input_refs)) != len(task.input_refs):
        raise AgentWorkPlanPromotionBlocked("verified task input references are duplicated")
    if not set(task.dependency_ids).issubset(task_ids) or task.task_id in task.dependency_ids:
        raise AgentWorkPlanPromotionBlocked("verified task dependencies are invalid")
    if not isinstance(task.risk_level, AgentRiskLevel):
        raise AgentWorkPlanPromotionBlocked("verified task risk level is invalid")
    if task.risk_level is AgentRiskLevel.PROHIBITED:
        raise AgentWorkPlanPromotionBlocked("a prohibited Agent task cannot be promoted")
    if not isinstance(task.approval_gate, ApprovalGate):
        raise AgentWorkPlanPromotionBlocked("verified task approval gate is invalid")
    if _CODE_RE.fullmatch(task.skill_id) is None:
        raise AgentWorkPlanPromotionBlocked("verified task skill_id is invalid")


def _binding(
    *, source: VerifiedGraphPromotionSource, item: AuthoritativePlanningObject
) -> PromotedTaskInputBinding:
    binding_id = str(uuid5(UUID(source.graph_id), item.ref_id))
    if item.object_type not in _REFERENCE_USE:
        raise AgentWorkPlanPromotionBlocked(
            "analysis-only source requires an explicit governed plan binding")
    reference_use = _REFERENCE_USE[item.object_type]
    payload = {
        "schema_version": "case-agent-work-plan-input-binding-v1",
        "run_id": source.run_id,
        "graph_id": source.graph_id,
        "graph_version": source.graph_version,
        "graph_hash": source.graph_hash,
        "snapshot_hash": source.snapshot.snapshot_hash,
        "verification_hash": source.verification_hash,
        "binding_id": binding_id,
        "input_ref": item.ref_id,
        "object_type": item.object_type.value,
        "object_id": item.object_id,
        "object_version": item.object_version,
        "content_hash": item.content_hash,
        "source_status": item.status.value,
        "reference_use": reference_use.value,
    }
    return PromotedTaskInputBinding(
        binding_id=binding_id,
        input_ref=item.ref_id,
        object_type=item.object_type,
        object_id=item.object_id,
        object_version=item.object_version,
        content_hash=item.content_hash,
        source_status=item.status.value,
        reference_use=reference_use,
        binding_hash=_canonical_hash(payload),
    )


def _work_plan_item(
    *,
    task: VerifiedGraphTask,
    binding_by_ref: dict[str, PromotedTaskInputBinding],
) -> CaseWorkPlanItem:
    kind = _item_kind(task.skill_id)
    references = tuple(binding_by_ref[ref].as_reference() for ref in task.input_refs)
    delivery_target = (
        DeliveryTarget.INTERNAL_WORK_PRODUCT
        if kind is WorkPlanItemKind.DOCUMENT_CANDIDATE
        else DeliveryTarget.NOT_APPLICABLE
    )
    return CaseWorkPlanItem(
        item_id=task.task_id,
        sequence=task.sequence,
        kind=kind,
        readiness=WorkPlanReadiness.ACTIONABLE,
        title=task.title,
        purpose=task.purpose,
        rationale=task.rationale,
        prerequisites=task.dependency_ids,
        trigger_refs=references,
        source_refs=references,
        risk_if_omitted=_risk_if_omitted(task.risk_level),
        # Independent verification proves the graph/result binding, not the
        # correctness of a legal conclusion.  Keep the neutral midpoint rather
        # than fabricating a model confidence score.
        confidence=0.5,
        review_gate=_review_gate(kind=kind, approval_gate=task.approval_gate),
        delivery_target=delivery_target,
        deliverable_kind=None,
        required_for_delivery=False,
        is_primary_document=False,
    )


def _disambiguate_repeated_task_titles(
    items: tuple[CaseWorkPlanItem, ...],
) -> tuple[CaseWorkPlanItem, ...]:
    """Keep every verified task reviewable when the planner reused a title.

    The generic plan validator deliberately treats the same normalized title
    over the same sources as a duplicate.  Distinct verified graph tasks can
    still have different purposes under a shared human-facing title.  Add the
    server-owned graph sequence only for those collisions; task IDs, purposes,
    dependencies and source bindings remain unchanged and auditable.
    """

    normalized = [" ".join(item.title.casefold().split()) for item in items]
    repeated = {title for title in normalized if normalized.count(title) > 1}
    return tuple(
        replace(item, title=f"{item.title}（步骤 {item.sequence}）")
        if normalized[index] in repeated
        else item
        for index, item in enumerate(items)
    )


def _item_kind(skill_id: str) -> WorkPlanItemKind:
    if skill_id in {"legal_rule_research_planning", "controlled_web_search", "official_source_capture"}:
        return WorkPlanItemKind.RESEARCH_TASK
    if skill_id == "interest_calculation":
        return WorkPlanItemKind.CALCULATION
    return WorkPlanItemKind.REVIEW


def _requested_deliverable_items(
    *,
    source: VerifiedGraphPromotionSource,
    bindings: tuple[PromotedTaskInputBinding, ...],
    posture_reference: WorkPlanReference,
    posture: object,
    start_sequence: int,
) -> tuple[CaseWorkPlanItem, ...]:
    if not source.requested_deliverables:
        return ()
    by_type: dict[PlanningProjectionObjectType, list[PromotedTaskInputBinding]] = {}
    for binding in bindings:
        by_type.setdefault(binding.object_type, []).append(binding)
    posture_bindings = tuple(
        by_type.get(PlanningProjectionObjectType.POSTURE_PROFILE, ())
    )
    if len(posture_bindings) != 1:
        raise AgentWorkPlanPromotionBlocked(
            "requested deliverables require the exact confirmed posture binding"
        )
    transaction_bindings = tuple(
        item
        for item in by_type.get(PlanningProjectionObjectType.CASE_TRANSACTION, ())
        if item.source_status == PlanningInputStatus.CONFIRMED.value
    )
    fact_bindings = tuple(
        item
        for item in by_type.get(PlanningProjectionObjectType.CASE_FACT, ())
        if item.source_status == PlanningInputStatus.CONFIRMED.value
    )
    claim_bindings = tuple(
        item
        for item in by_type.get(PlanningProjectionObjectType.CASE_CLAIM, ())
        if item.source_status == PlanningInputStatus.CONFIRMED.value
    )
    legal_source_bindings = tuple(
        item
        for item in by_type.get(
            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE, ()
        )
        if item.source_status == PlanningInputStatus.LOCKED.value
    )
    legal_rule_bindings = tuple(
        item
        for item in by_type.get(
            PlanningProjectionObjectType.APPROVED_LEGAL_RULE, ()
        )
        if item.source_status == PlanningInputStatus.LOCKED.value
    )
    evidence_page_bindings = tuple(
        item
        for item in by_type.get(PlanningProjectionObjectType.EVIDENCE_PAGE, ())
        if item.source_status == PlanningInputStatus.CONFIRMED.value
    )
    result: list[CaseWorkPlanItem] = []
    for offset, kind in enumerate(source.requested_deliverables):
        if kind is AgentDeliverableKind.CASE_REVIEW_MEMO:
            # Raw material objects and model-facing projection bindings are not
            # document text.  The binder automatically adds current posture and
            # the active plan item; the memo additionally requires current,
            # independently re-readable confirmed facts from the case ledger.
            selected = (posture_reference, *fact_bindings)
            readiness = (
                WorkPlanReadiness.ACTIONABLE
                if fact_bindings
                else WorkPlanReadiness.NEEDS_INFORMATION
            )
            title = "生成案件审阅意见候选"
            purpose = "依据本次已核验分析与来源，形成可编辑 Word 及 PDF 审阅稿。"
            rationale = (
                "这是律师在办案目标中结构化选择的明确成果；模板、格式与已确认"
                "事实来源均由服务器目录绑定，不从任务说明或代理地位猜测。"
            )
            risk = "若不生成，已核验研判无法形成可逐段复核和下载的内部工作产品。"
        elif kind is AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST:
            selected = (posture_reference, *fact_bindings)
            readiness = (
                WorkPlanReadiness.ACTIONABLE
                if fact_bindings
                else WorkPlanReadiness.NEEDS_INFORMATION
            )
            title = "生成补证清单候选"
            purpose = "将本轮已核验分析中的材料缺口、当事人问题和取得动作整理为可编辑 Word 及 PDF 审阅稿。"
            rationale = (
                "清单只承接当前受管运行已验证的分析候选和确认事实；"
                "不会把建议补证自动当作已确认事实、证据资格或对外动作。"
            )
            risk = "若不生成，律师需要在分析正文中人工拆取补证与核对事项，容易遗漏时序和来源。"
        elif kind is AgentDeliverableKind.PAYMENT_LEDGER:
            selected = (posture_reference, *transaction_bindings)
            readiness = (
                WorkPlanReadiness.ACTIONABLE
                if transaction_bindings
                else WorkPlanReadiness.NEEDS_INFORMATION
            )
            title = "生成收付款核对表候选"
            purpose = "将已确认交易逐笔编入可编辑 Excel 及 PDF 审阅稿。"
            rationale = (
                "服务器只在当前投影存在已确认交易时允许执行；无已确认交易时"
                "保留为待补信息，不让文书绑定对缺失来源默认放行。"
            )
            risk = "若不生成，已确认交易缺少可复算、可逐行核对的结构化交付件。"
        elif kind is AgentDeliverableKind.EVIDENCE_CATALOGUE:
            selected = (posture_reference, *evidence_page_bindings)
            readiness = (
                WorkPlanReadiness.ACTIONABLE
                if evidence_page_bindings
                else WorkPlanReadiness.NEEDS_INFORMATION
            )
            title = "生成证据目录候选"
            purpose = "将本轮已纳入的证据页整理为可编辑 Excel 及 PDF 审阅稿。"
            rationale = (
                "目录只列出已确认纳入证据范围的页面；拟证明事项和三性审查仍保留为"
                "律师待核对事项，系统不会据此确认事实或证据资格。"
            )
            risk = "若不生成，已纳入材料缺少可逐页核对、可补充拟证明事项的独立目录。"
        elif kind is AgentDeliverableKind.DEFENCE_STATEMENT:
            selected = (
                posture_reference,
                *fact_bindings,
                *claim_bindings,
                *legal_source_bindings,
                *legal_rule_bindings,
            )
            readiness = (
                WorkPlanReadiness.ACTIONABLE
                if (
                    _is_defence_candidate_posture(posture)
                    and
                    fact_bindings
                    and claim_bindings
                    and legal_source_bindings
                    and legal_rule_bindings
                )
                else WorkPlanReadiness.NEEDS_INFORMATION
            )
            title = "生成民事答辩状候选"
            purpose = "将当前已确认诉请回应、事实、法源与规则编译为供律师终审的 Word 及 PDF 审阅稿。"
            rationale = (
                "答辩候选仅在受管运行已绑定被告侧来源时形成；服务器不会从代理地位、"
                "模型文本或浏览器输入猜测承认、争议、金额、法条或法院信息。"
            )
            risk = "若不生成，已验证的被告侧分析无法形成逐项诉请对应、可追溯的律师审阅候选。"
        else:  # pragma: no cover - enum exhaustiveness guard
            raise AgentWorkPlanPromotionBlocked(
                "requested deliverable is outside the first-release catalogue"
            )
        if len(selected) > 100:
            raise AgentWorkPlanPromotionBlocked(
                "requested deliverable source set exceeds the work-plan boundary"
            )
        references = tuple(
            item if isinstance(item, WorkPlanReference) else item.as_reference()
            for item in selected
        )
        result.append(
            CaseWorkPlanItem(
                item_id=str(
                    uuid5(
                        UUID(source.graph_id),
                        f"case-agent-requested-deliverable-v1:{kind.value}",
                    )
                ),
                sequence=start_sequence + offset,
                kind=WorkPlanItemKind.DOCUMENT_CANDIDATE,
                readiness=readiness,
                title=title,
                purpose=purpose,
                rationale=rationale,
                prerequisites=tuple(task.task_id for task in source.tasks),
                trigger_refs=references,
                source_refs=references,
                risk_if_omitted=risk,
                confidence=0.5,
                review_gate=ReviewGate.LEAD_LAWYER_CONFIRMATION,
                delivery_target=DeliveryTarget.INTERNAL_WORK_PRODUCT,
                deliverable_kind=kind.value,
                required_for_delivery=False,
                is_primary_document=False,
            )
        )
    return tuple(result)


def _review_gate(*, kind: WorkPlanItemKind, approval_gate: ApprovalGate) -> ReviewGate:
    if kind is WorkPlanItemKind.RESEARCH_TASK:
        return ReviewGate.LEGAL_AUTHORITY_REVIEW
    if kind is WorkPlanItemKind.CALCULATION:
        return ReviewGate.CALCULATION_REVIEW
    if kind is WorkPlanItemKind.DOCUMENT_CANDIDATE:
        return ReviewGate.LEAD_LAWYER_CONFIRMATION
    if approval_gate is ApprovalGate.RELEASE_LOCK:
        return ReviewGate.PROCEDURE_REVIEW
    return ReviewGate.EVIDENCE_REVIEW


def _is_defence_candidate_posture(posture: object) -> bool:
    """Return whether the already-confirmed profile supports this narrow draft.

    This is a fixed server rule, not a document-kind-to-role inference.  It is
    intentionally evaluated before an item becomes actionable and then
    repeated by the deterministic document compiler using the bound posture
    source.  Keeping the policy here prevents a mismatched matter from being
    activated merely because it has otherwise complete sources.
    """

    return (
        getattr(posture, "represented_position", None) == "DEFENDANT"
        and getattr(posture, "procedure_stage", None) == "FIRST_INSTANCE"
        and getattr(posture, "engagement_state", None) == "ACTIVE"
    )


def _risk_if_omitted(risk: AgentRiskLevel) -> str:
    return {
        AgentRiskLevel.LOW: "若暂不处理，本案当前目标的一项辅助工作将保持未完成；是否继续由律师决定。",
        AgentRiskLevel.MEDIUM: "若不处理，可能遗漏当前目标所需的核对或来源闭环；需由律师复核实际影响。",
        AgentRiskLevel.HIGH: "若不处理，可能影响当前目标的程序、法律或交付完整性；必须由律师决定是否接受该风险。",
        AgentRiskLevel.PROHIBITED: "禁止事项不能进入动态工作计划候选。",
    }[risk]


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise AgentWorkPlanPromotionBlocked(f"{label} must be a UUID") from error


def _hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise AgentWorkPlanPromotionBlocked(f"{label} must be a lowercase SHA-256")


def _text(value: str, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise AgentWorkPlanPromotionBlocked(f"{label} is missing or too long")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


_REFERENCE_USE = {
    PlanningProjectionObjectType.MATERIAL_OBJECT: WorkPlanReferenceUse.MATERIAL,
    PlanningProjectionObjectType.EVIDENCE_PAGE: WorkPlanReferenceUse.EVIDENCE,
    PlanningProjectionObjectType.CASE_FACT: WorkPlanReferenceUse.FACT,
    PlanningProjectionObjectType.CASE_CLAIM: WorkPlanReferenceUse.CLAIM_SCOPE,
    PlanningProjectionObjectType.DISPUTE_ISSUE: WorkPlanReferenceUse.FACT,
    PlanningProjectionObjectType.CASE_TRANSACTION: WorkPlanReferenceUse.TRANSACTION,
    PlanningProjectionObjectType.POSTURE_PROFILE: WorkPlanReferenceUse.POSTURE,
    PlanningProjectionObjectType.WORK_PLAN_ITEM: WorkPlanReferenceUse.WORK_PLAN,
    PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE: WorkPlanReferenceUse.LEGAL_AUTHORITY,
    PlanningProjectionObjectType.APPROVED_LEGAL_RULE: WorkPlanReferenceUse.LEGAL_RULE,
    PlanningProjectionObjectType.PROCEDURAL_EVENT: WorkPlanReferenceUse.COURT_EVENT,
    PlanningProjectionObjectType.REVIEW_OBLIGATION: WorkPlanReferenceUse.WORK_PLAN,
    PlanningProjectionObjectType.TRANSACTION_CANDIDATE: WorkPlanReferenceUse.WORK_PLAN,
    PlanningProjectionObjectType.FACT_CANDIDATE: WorkPlanReferenceUse.WORK_PLAN,
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")


__all__ = (
    "AgentWorkPlanPromotionBlocked",
    "CompiledAgentWorkPlanPromotion",
    "PROMOTION_AGENT_ID",
    "PROMOTION_AGENT_VERSION",
    "PromotedTaskInputBinding",
    "VerifiedGraphPromotionSource",
    "VerifiedGraphTask",
    "compile_verified_graph_work_plan_candidate",
    "validate_verified_graph_promotion_source",
)
