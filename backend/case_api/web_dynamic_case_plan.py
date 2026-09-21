"""Browser-safe application service for governed dynamic case plans.

The browser may read the latest reviewable projection, append one structured
lawyer review signal, or activate the *server-selected* current candidate.  It
never sends a plan hash, Agent graph, posture hash, source binding or model
instruction.  The PostgreSQL store must re-read all of those authorities in
the same transaction that activates the plan.

An item review is deliberately not an edit to the immutable 0030 candidate:
``REQUEST_CHANGE`` and ``REJECT`` make that candidate ineligible for
activation and require a newly verified Agent graph.  Counsel can also skip
item-by-item approval and confirm the exact whole plan once; this keeps review
exception-oriented instead of recreating a manual checklist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.models import Actor, Role

from .web_app import (
    WebDynamicCasePlanActivationReceipt,
    WebDynamicCasePlanDecisionReceipt,
    WebDynamicCasePlanItemResponse,
    WebDynamicCasePlanReferenceResponse,
    WebDynamicCasePlanResponse,
)


class WebDynamicCasePlanBlocked(PermissionError):
    """The current identity or plan projection cannot cross the Web boundary."""


class DynamicCasePlanStorePort(Protocol):
    """Real persistence contract; no in-memory fallback is accepted."""

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> object: ...

    def review_item(self, **kwargs: Any) -> object: ...

    def activate_current_plan(self, **kwargs: Any) -> object: ...


_HUMAN_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_SOURCE_LABELS = {
    "MATERIAL_OBJECT": "案件材料",
    "POSTURE_PROFILE": "当前代理与程序档案",
    "LAWYER_OBJECTIVE": "律师确认的办案目标",
    "EVIDENCE_PAGE": "证据页面",
    "EVIDENCE_MANIFEST": "锁定证据清单",
    "CASE_FACT": "已确认案件事实",
    "CLAIM": "已确认诉请范围",
    "CASE_CLAIM": "已确认诉请范围",
    "DISPUTE_ISSUE": "争议焦点",
    "TRANSACTION": "已确认交易记录",
    "CASE_TRANSACTION": "已确认交易记录",
    "LEGAL_EVENT": "法律事件",
    "LEGAL_RULE_VERSION": "已批准规则版本",
    "APPROVED_LEGAL_RULE": "已批准规则版本",
    "LEGAL_SOURCE_SNAPSHOT": "已核验官方法源",
    "VERIFIED_LEGAL_SOURCE": "已核验官方法源",
    "LEGAL_BUNDLE": "已核验法律依据包",
    "CALCULATION_RUN": "已复算测算结果",
    "COURT_PROCEEDING": "当前程序记录",
    "SERVICE_EVENT": "送达事件",
    "PROCEDURAL_EVENT": "程序事件",
    "PROCEDURAL_DEADLINE": "程序期限",
    "WORK_PLAN_ITEM": "既有办案计划事项",
    "REVIEW_OBLIGATION": "待核事项（非确认事实）",
    "TRANSACTION_CANDIDATE": "未确认交易候选",
    "FACT_CANDIDATE": "未确认材料记载",
    "AGENT_TASK_INPUT": "Agent 已核验输入",
}


class WebDynamicCasePlanService:
    """Project and govern one current PostgreSQL-backed work-plan candidate."""

    def __init__(self, *, store: DynamicCasePlanStorePort) -> None:
        required = ("get_snapshot", "review_item", "activate_current_plan")
        if any(not callable(getattr(store, method, None)) for method in required):
            raise ValueError("Web dynamic case-plan store is invalid")
        self._store = store

    def current_plan(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> WebDynamicCasePlanResponse | None:
        actor = _actor(identity, write=False)
        matter = _uuid(matter_id, "案件编号")
        snapshot = self._store.get_snapshot(matter_id=matter, actor=actor)
        return _project_snapshot(snapshot, actor=actor, expected_matter_id=matter)

    def decide_item(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        plan_id: str,
        item_id: str,
        expected_version: int,
        idempotency_key: str,
        decision: str,
        reason_code: str,
        readiness_override: str | None,
        required_for_delivery_override: bool | None,
    ) -> WebDynamicCasePlanDecisionReceipt:
        actor = _actor(identity, write=True)
        matter = _uuid(matter_id, "案件编号")
        version = _positive_int(expected_version, "案件版本")
        receipt = self._store.review_item(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=idempotency_key,
            plan_id=_uuid(plan_id, "办案计划编号"),
            item_id=_uuid(item_id, "办案计划事项编号"),
            decision={"APPROVE": "APPROVE", "MODIFY": "REQUEST_CHANGE", "REJECT": "REJECT"}.get(
                decision, ""
            ),
            reason_code=reason_code,
            readiness_override=readiness_override,
            required_for_delivery_override=required_for_delivery_override,
        )
        status = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGE": "CHANGE_REQUESTED",
            "REJECT": "REJECTED",
        }.get(str(getattr(receipt, "decision", "")))
        if status is None:
            raise WebDynamicCasePlanBlocked("计划事项复核回执无效")
        if str(getattr(receipt, "plan_id", "")) != plan_id or str(
            getattr(receipt, "item_id", "")
        ) != item_id:
            raise WebDynamicCasePlanBlocked("计划事项复核回执与请求不一致")
        receipt_version = _positive_int(
            getattr(receipt, "matter_version", None), "计划事项复核案件版本"
        )
        if receipt_version != version:
            raise WebDynamicCasePlanBlocked("计划事项复核不得推进案件版本")
        return WebDynamicCasePlanDecisionReceipt(
            plan_id=plan_id,
            item_id=item_id,
            decision_status=status,
            matter_version=receipt_version,
            requires_replanning=status in {"CHANGE_REQUESTED", "REJECTED"},
        )

    def activate_current_plan(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> WebDynamicCasePlanActivationReceipt:
        actor = _actor(identity, write=True)
        matter = _uuid(matter_id, "案件编号")
        version = _positive_int(expected_version, "案件版本")
        receipt = self._store.activate_current_plan(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=idempotency_key,
        )
        if (
            str(getattr(receipt, "command_name", ""))
            != "ACTIVATE_CURRENT_CASE_WORK_PLAN"
            or str(getattr(receipt, "matter_id", "")) != matter
            or str(getattr(receipt, "idempotency_key", "")) != idempotency_key
            or str(getattr(receipt, "object_type", "")) != "CASE_WORK_PLAN"
        ):
            raise WebDynamicCasePlanBlocked("办案计划激活回执类型无效")
        receipt_version = _positive_int(
            getattr(receipt, "matter_version", None), "办案计划激活案件版本"
        )
        if receipt_version != version + 1:
            raise WebDynamicCasePlanBlocked("办案计划激活回执版本无效")
        return WebDynamicCasePlanActivationReceipt(
            plan_id=_uuid(getattr(receipt, "object_id", None), "办案计划编号"),
            status="ACTIVE",
            matter_version=receipt_version,
        )


def _project_snapshot(
    snapshot: object, *, actor: Actor, expected_matter_id: str
) -> WebDynamicCasePlanResponse | None:
    if str(getattr(snapshot, "matter_id", "")) != expected_matter_id:
        raise WebDynamicCasePlanBlocked("动态办案计划不属于当前案件")
    current_version = _positive_int(
        getattr(snapshot, "matter_version", None), "当前案件版本"
    )
    plans = tuple(getattr(snapshot, "plans", ()))
    if not plans:
        return None
    if any(not isinstance(row, dict) for row in plans):
        raise WebDynamicCasePlanBlocked("动态办案计划投影无效")
    # The newest candidate takes review priority over the prior active plan.
    # Superseded history never becomes the browser's current recommendation.
    eligible = [row for row in plans if str(row.get("status")) != "SUPERSEDED"]
    if not eligible:
        return None
    plan = max(eligible, key=lambda row: _positive_int(row.get("plan_version"), "计划版本"))
    plan_id = _uuid(plan.get("plan_id"), "办案计划编号")
    status = str(plan.get("status", ""))
    if status not in {"CANDIDATE", "ACTIVE", "STALE"}:
        raise WebDynamicCasePlanBlocked("动态办案计划状态无效")
    generated_version = _positive_int(
        plan.get("planned_matter_version"), "计划生成案件版本"
    )
    if status == "CANDIDATE":
        inputs_current = current_version == generated_version + 1
    elif status == "ACTIVE":
        inputs_current = current_version == _positive_int(
            plan.get("activated_matter_version"), "计划激活案件版本"
        )
    else:
        inputs_current = False
    projected_status = status if inputs_current else "STALE"
    stale_reasons: tuple[str, ...] = ()
    if not inputs_current:
        reason = str(plan.get("stale_reason_code") or "CASE_INPUTS_CHANGED")
        stale_reasons = (_stale_reason(reason),)

    item_rows = tuple(
        row
        for row in getattr(snapshot, "items", ())
        if isinstance(row, dict) and str(row.get("plan_id")) == plan_id
    )
    if not item_rows:
        raise WebDynamicCasePlanBlocked("动态办案计划没有可复核事项")
    item_rows = tuple(sorted(item_rows, key=lambda row: _positive_int(row.get("sequence"), "事项顺序")))
    prerequisites = tuple(
        row
        for row in getattr(snapshot, "prerequisites", ())
        if isinstance(row, dict) and str(row.get("plan_id")) == plan_id
    )
    item_references = tuple(
        row
        for row in getattr(snapshot, "item_references", ())
        if isinstance(row, dict)
        and str(row.get("plan_id")) == plan_id
        and str(row.get("reference_role")) == "SOURCE"
    )
    reviews = {
        str(row.get("item_id")): row
        for row in getattr(snapshot, "item_reviews", ())
        if isinstance(row, dict) and str(row.get("plan_id")) == plan_id
    }
    blocking_reviews = tuple(
        row for row in reviews.values() if str(row.get("decision")) in {"REQUEST_CHANGE", "REJECT"}
    )
    blockers: list[str] = []
    if projected_status != "CANDIDATE":
        blockers.append("当前显示的不是可激活候选")
    if not inputs_current:
        blockers.append("案件输入已变化，需要 Agent 重新研判")
    if blocking_reviews:
        blockers.append("律师已对部分事项提出调整或驳回，需要 Agent 重新研判并生成新候选")
    structured_deliverables = tuple(
        row
        for row in item_rows
        if row.get("deliverable_kind") is not None
    )
    actionable_deliverables = tuple(
        row
        for row in structured_deliverables
        if str(row.get("readiness")) == "ACTIONABLE"
    )
    if structured_deliverables and not actionable_deliverables:
        blockers.append(
            "暂未具备可进入律师审阅的成果；请先补充或确认案件材料"
        )
    if any(
        row.get("deliverable_kind") is not None
        and bool(row.get("required_for_delivery"))
        and str(row.get("readiness")) != "ACTIONABLE"
        for row in item_rows
    ):
        blockers.append(
            "仍有必须交付的成果缺少已确认来源；请先补充或确认案件材料"
        )
    if Role.LEAD_LAWYER not in actor.roles:
        blockers.append("仅主办律师可以确认整案计划")

    items = tuple(
        _project_item(
            row,
            plan_status=projected_status,
            review=reviews.get(str(row.get("item_id"))),
            prerequisites=prerequisites,
            references=item_references,
        )
        for row in item_rows
    )
    generated_at = plan.get("generated_at")
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise WebDynamicCasePlanBlocked("计划生成时间无效")
    return WebDynamicCasePlanResponse(
        plan_id=plan_id,
        matter_id=expected_matter_id,
        generated_matter_version=generated_version,
        current_matter_version=current_version,
        status=projected_status,
        inputs_current=inputs_current,
        stale_reasons=stale_reasons,
        generated_at=generated_at,
        items=items,
        can_activate=not blockers,
        activation_blockers=tuple(dict.fromkeys(blockers)),
        reviewed_item_count=len(reviews),
    )


def _project_item(
    row: dict[str, Any],
    *,
    plan_status: str,
    review: dict[str, Any] | None,
    prerequisites: tuple[dict[str, Any], ...],
    references: tuple[dict[str, Any], ...],
) -> WebDynamicCasePlanItemResponse:
    item_id = _uuid(row.get("item_id"), "办案计划事项编号")
    if plan_status == "ACTIVE":
        item_status = "APPROVED"
    elif plan_status == "STALE":
        item_status = "SUPERSEDED"
    else:
        item_status = {
            None: "CANDIDATE",
            "APPROVE": "APPROVED",
            "REQUEST_CHANGE": "CHANGE_REQUESTED",
            "REJECT": "REJECTED",
        }.get(None if review is None else str(review.get("decision")))
        if item_status is None:
            raise WebDynamicCasePlanBlocked("计划事项复核状态无效")
    item_prerequisites = tuple(
        _uuid(value.get("prerequisite_item_id"), "事项前置条件")
        for value in prerequisites
        if str(value.get("item_id")) == item_id
    )
    source_rows = [value for value in references if str(value.get("item_id")) == item_id]
    source_refs = tuple(_project_reference(value) for value in source_rows)
    return WebDynamicCasePlanItemResponse(
        item_id=item_id,
        sequence=_positive_int(row.get("sequence"), "事项顺序"),
        category=str(row.get("item_kind", "")),
        status=item_status,
        readiness=str(row.get("readiness", "")),
        title=_text(row.get("title"), "事项标题", 500),
        purpose=_text(row.get("purpose"), "事项目的", 2_000),
        rationale=_text(row.get("rationale"), "事项理由", 4_000),
        risk_if_omitted=_text(row.get("risk_if_omitted"), "遗漏风险", 2_000),
        prerequisites=item_prerequisites,
        confidence=float(row.get("confidence")),
        review_gate=str(row.get("review_gate", "")),
        source_refs=source_refs,
        delivery_target=str(row.get("delivery_target")) if row.get("delivery_target") is not None else None,
        deliverable_kind=str(row.get("deliverable_kind")) if row.get("deliverable_kind") is not None else None,
        required_for_delivery=bool(row.get("required_for_delivery")),
    )


def _project_reference(row: dict[str, Any]) -> WebDynamicCasePlanReferenceResponse:
    source_kind = str(row.get("display_source_kind") or row.get("source_type") or "")
    source_id = _uuid(
        row.get("display_source_id") or row.get("source_id"), "计划事项来源编号"
    )
    label = _SOURCE_LABELS.get(source_kind)
    if label is None:
        raise WebDynamicCasePlanBlocked("计划事项包含未支持的来源类型")
    locator_value = row.get("display_locator") or row.get("source_version")
    locator = None if locator_value is None else _text(locator_value, "计划事项来源位置", 500)
    return WebDynamicCasePlanReferenceResponse(
        source_kind=source_kind,
        source_id=source_id,
        label=label,
        locator=locator,
    )


def _actor(identity: ServerIdentityContext, *, write: bool) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebDynamicCasePlanBlocked("律师身份无效")
    try:
        identity.validate()
    except Exception:
        raise WebDynamicCasePlanBlocked("律师登录已失效") from None
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebDynamicCasePlanBlocked("动态办案计划只接受已通过 MFA 的 Web 律师身份")
    actor = identity.actor
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_HUMAN_ROLES):
        raise WebDynamicCasePlanBlocked("当前身份不能查看动态办案计划")
    if write and Role.LEAD_LAWYER not in actor.roles:
        raise WebDynamicCasePlanBlocked("只有主办律师可以复核或激活动态办案计划")
    return actor


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebDynamicCasePlanBlocked(f"{label}无效") from None


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise WebDynamicCasePlanBlocked(f"{label}无效")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise WebDynamicCasePlanBlocked(f"{label}无效")
    return value.strip()


def _stale_reason(code: str) -> str:
    return {
        "NEW_PLAN_ACTIVATED": "已有更新的律师确认计划",
        "CASE_INPUTS_CHANGED": "案件事实、材料、程序或法源已发生变化",
        "POSTURE_CHANGED": "代理对象或程序档案已发生变化",
    }.get(code, "案件输入已发生变化")


__all__ = (
    "DynamicCasePlanStorePort",
    "WebDynamicCasePlanBlocked",
    "WebDynamicCasePlanService",
)
