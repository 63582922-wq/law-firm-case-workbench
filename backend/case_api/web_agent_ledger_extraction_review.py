"""Browser-safe review of automatically staged fact/transaction extraction.

The Worker stages independently verified extraction candidates without a
browser command.  This application service exposes only a bounded, source-
linked review projection and one lead-lawyer action over the complete
low-risk lane.  It never accepts candidate content, hashes, page identifiers,
provider metadata, prompts or a caller-selected subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Protocol
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.case_agent_ledger_exception_review import (
    LedgerExceptionBatchState,
    LedgerExceptionDecision,
    LedgerExceptionGroup,
    LedgerExceptionGroupMemberPage,
    LedgerExceptionReason,
    LedgerExceptionReextractionCohortCapacityExceeded,
    LedgerExceptionReextractionSourceWindowExceeded,
    LedgerExceptionRiskPolicy,
    LedgerExceptionSourcePolicy,
    LedgerExtractionBatchReviewStatus,
    LedgerLowRiskLaneStatus,
)
from case_kernel.models import Actor, Role


class WebAgentLedgerExtractionReviewBlocked(ValueError):
    """The staged projection or requested transition is not browser-safe."""


class WebAgentLedgerReextractionSourceWindowExceeded(
    WebAgentLedgerExtractionReviewBlocked
):
    """The selected immutable group is too large for one re-extraction run."""


class WebAgentLedgerReextractionCohortCapacityExceeded(
    WebAgentLedgerExtractionReviewBlocked
):
    """The matter has no remaining active re-extraction cohort capacity."""


@dataclass(frozen=True)
class CaseLedgerExtractionReviewExcerptProjection:
    evidence_page_id: str
    page_number: int
    text: str


@dataclass(frozen=True)
class CaseLedgerExtractionReviewCandidateProjection:
    extraction_candidate_id: str
    candidate_kind: str
    summary: str
    confidence: float
    review_lane: str
    review_reason_codes: tuple[str, ...]
    excerpts: tuple[CaseLedgerExtractionReviewExcerptProjection, ...]


@dataclass(frozen=True)
class CaseLedgerExtractionReviewBatchProjection:
    extraction_batch_id: str
    run_id: str
    matter_id: str
    current_matter_version: int
    source_matter_version: int
    candidate_count: int
    low_risk_count: int
    exception_count: int
    staged_at: datetime
    is_current: bool
    exception_review_is_current: bool
    confirmed_matter_version: int | None
    confirmed_at: datetime | None
    candidates: tuple[CaseLedgerExtractionReviewCandidateProjection, ...]


class AgentLedgerExtractionReviewStorePort(Protocol):
    """PostgreSQL-only read/command boundary used by the Web service."""

    def is_available_for_actor(self, *, actor: Actor) -> bool: ...

    def list_review_batches(
        self, *, matter_id: str, actor: Actor
    ) -> tuple[CaseLedgerExtractionReviewBatchProjection, ...]: ...

    def confirm_low_risk_batch(
        self,
        *,
        matter_id: str,
        actor: Actor,
        session_id: str,
        expected_version: int,
        idempotency_key: str,
        extraction_batch_id: str,
    ) -> object: ...

    def list_exception_groups(
        self, *, matter_id: str, actor: Actor, extraction_batch_id: str
    ) -> tuple[LedgerExceptionGroup, ...]: ...

    def list_exception_group_members(
        self,
        *,
        matter_id: str,
        actor: Actor,
        exception_group_id: str,
        offset: int,
        limit: int,
    ) -> LedgerExceptionGroupMemberPage: ...

    def read_exception_batch_state(
        self, *, matter_id: str, actor: Actor, extraction_batch_id: str
    ) -> LedgerExceptionBatchState: ...

    def decide_exception_group(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        expected_version: int,
        idempotency_key: str,
        exception_group_id: str,
        decision: LedgerExceptionDecision,
        reason: LedgerExceptionReason,
        reason_note: str | None,
    ) -> object: ...


@dataclass(frozen=True)
class WebAgentLedgerExtractionExcerpt:
    evidence_page_id: str
    page_number: int
    text: str


@dataclass(frozen=True)
class WebAgentLedgerExtractionCandidate:
    sequence: int
    candidate_kind: str
    summary: str
    confidence: float
    review_status: str
    review_reasons: tuple[str, ...]
    excerpts: tuple[WebAgentLedgerExtractionExcerpt, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionReasonOption:
    code: str
    label: str


@dataclass(frozen=True)
class WebAgentLedgerExceptionAction:
    code: str
    label: str
    consequence: str
    requires_note: bool
    reasons: tuple[WebAgentLedgerExceptionReasonOption, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionGroup:
    group_id: str
    candidate_kind: str
    candidate_count: int
    summary: str
    review_reasons: tuple[str, ...]
    source_guidance: str
    risk_label: str
    status: str
    decision: str | None
    decision_label: str | None
    decision_reason: str | None
    decision_reason_label: str | None
    can_decide: bool
    allowed_actions: tuple[WebAgentLedgerExceptionAction, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionMember:
    sequence: int
    candidate_kind: str
    summary: str
    confidence: float
    review_reasons: tuple[str, ...]
    excerpts: tuple[WebAgentLedgerExtractionExcerpt, ...]
    extraction_candidate_id: str | None = None


@dataclass(frozen=True)
class WebAgentLedgerExceptionMemberPage:
    group_id: str
    total_count: int
    offset: int
    next_offset: int | None
    members: tuple[WebAgentLedgerExceptionMember, ...]


@dataclass(frozen=True)
class WebAgentLedgerExtractionBatch:
    batch_id: str
    matter_id: str
    status: str
    current_matter_version: int
    source_matter_version: int
    candidate_count: int
    low_risk_count: int
    exception_count: int
    staged_at: datetime
    confirmed_at: datetime | None
    can_confirm_low_risk: bool
    exception_review_status: str
    exception_group_count: int
    decided_exception_group_count: int
    exception_groups: tuple[WebAgentLedgerExceptionGroup, ...]
    low_risk_candidates: tuple[WebAgentLedgerExtractionCandidate, ...]
    exception_candidates: tuple[WebAgentLedgerExtractionCandidate, ...]


@dataclass(frozen=True)
class WebAgentLedgerExtractionConfirmationReceipt:
    batch_id: str
    matter_version: int
    confirmed_fact_count: int
    confirmed_transaction_count: int
    confirmed_total_count: int


@dataclass(frozen=True)
class WebAgentLedgerExceptionDecisionReceipt:
    batch_id: str
    group_id: str
    matter_version: int
    committed_matter_version: int
    decision: str
    exception_review_status: str
    decided_exception_group_count: int
    exception_group_count: int
    batch_resolved: bool


class WebAgentLedgerExtractionReviewPort(Protocol):
    def is_available(
        self, *, identity: ServerIdentityContext
    ) -> bool: ...

    def list_batches(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> tuple[WebAgentLedgerExtractionBatch, ...]: ...

    def confirm_low_risk_batch(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> WebAgentLedgerExtractionConfirmationReceipt: ...

    def list_exception_group_members(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        group_id: str,
        offset: int,
        limit: int,
    ) -> WebAgentLedgerExceptionMemberPage: ...

    def decide_exception_group(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        group_id: str,
        expected_version: int,
        idempotency_key: str,
        decision: str,
        reason: str,
        reason_note: str | None,
    ) -> WebAgentLedgerExceptionDecisionReceipt: ...


_HUMAN_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_REASON_LABELS = {
    "POSSIBLE_DUPLICATE": "可能与其他候选重复，需要核对后再处理。",
    "PARTY_AMBIGUOUS": "相关当事人或收付款人不明确。",
    "DATE_AMBIGUOUS": "日期无法从材料中唯一确定。",
    "AMOUNT_AMBIGUOUS": "金额无法从材料中唯一确定。",
    "CROSS_PAGE_CONFLICT": "不同页面的信息存在冲突。",
    "CONTRADICTS_CASE_LEDGER": "模型提示可能与台账矛盾，尚未核实；该标签不证明已有对应台账或实际冲突。",
    "OCR_DERIVED": "内容来自 OCR 或视觉识别，需要对照原页。",
    "LOW_CONFIDENCE": "模型对该候选的把握不足。",
    "LEGAL_CONCLUSION_RISK": "候选可能夹带法律判断，不能作为自动确认事实。",
    "INCOMPLETE_TRANSACTION": "收付款记录的必要字段不完整。",
    "UNTRUSTED_TEXT": "材料文本的可信边界不足。",
    "BELOW_BULK_CONFIDENCE_THRESHOLD": "未达到整组确认的置信门槛。",
    "NON_NATIVE_SOURCE": "来源不是可直接复核的原生文本。",
    "SOURCE_TEXT_NOT_REVERIFIED": "服务器未能重新核验来源文字。",
    "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE": "与当前案件台账冲突或重复。",
}
_SOURCE_GUIDANCE = {
    LedgerExceptionSourcePolicy.NATIVE_SOURCE_REVIEW: "来源原页可直接核对。",
    LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED: "来源包含 OCR、非原生文本或复核缺口，需先对照原页。",
}
_RISK_LABELS = {
    LedgerExceptionRiskPolicy.DUPLICATE_REVIEW: "可能重复",
    LedgerExceptionRiskPolicy.LEGAL_OR_LEDGER_CONFLICT_REVIEW: "台账或法律判断风险待核实",
    LedgerExceptionRiskPolicy.MISSING_FIELDS_OR_AMBIGUITY_REVIEW: "关键信息缺失或表述不唯一",
    LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW: "未达到自动整组确认门槛",
}
_DECISION_LABELS = {
    LedgerExceptionDecision.REJECT_AS_DUPLICATE: "确认整组为重复记录",
    LedgerExceptionDecision.REQUEST_REEXTRACTION: "交回 Agent 重新提取",
    LedgerExceptionDecision.REQUEST_MORE_EVIDENCE: "转为补充材料任务",
    LedgerExceptionDecision.DEFER_WITH_REASON: "说明原因后暂缓",
}
_DECISION_CONSEQUENCES = {
    LedgerExceptionDecision.REJECT_AS_DUPLICATE: "本组不会进入正式台账，并保留重复处置记录。",
    LedgerExceptionDecision.REQUEST_REEXTRACTION: "Agent 将按本组来源和冲突原因重新读取、提取并复核。",
    LedgerExceptionDecision.REQUEST_MORE_EVIDENCE: "Agent 会把本组缺口纳入下一版材料清单和办案计划。",
    LedgerExceptionDecision.DEFER_WITH_REASON: "本组保持未入账，暂缓原因会成为下一版计划的受控输入。",
}
_REASON_OPTION_LABELS = {
    LedgerExceptionReason.DUPLICATE_CONFIRMED: "已对照来源，确认属于重复记录",
    LedgerExceptionReason.SOURCE_QUALITY_INSUFFICIENT: "原页质量不足，需要重新识别",
    LedgerExceptionReason.EXTRACTION_CONFLICT: "提取结果存在冲突，需要重新分析",
    LedgerExceptionReason.EVIDENCE_GAP: "现有材料不足，需要补证",
    LedgerExceptionReason.PARTY_DATE_AMOUNT_UNCLEAR: "主体、日期或金额仍不明确",
    LedgerExceptionReason.AWAITING_CLIENT_INPUT: "等待当事人补充说明或材料",
    LedgerExceptionReason.AWAITING_EXTERNAL_RECORD: "等待银行、平台或其他外部记录",
    LedgerExceptionReason.NEEDS_LEAD_REVIEW: "需要主办律师进一步研判",
}
_DECISION_REASONS = {
    LedgerExceptionDecision.REJECT_AS_DUPLICATE: (
        LedgerExceptionReason.DUPLICATE_CONFIRMED,
    ),
    LedgerExceptionDecision.REQUEST_REEXTRACTION: (
        LedgerExceptionReason.SOURCE_QUALITY_INSUFFICIENT,
        LedgerExceptionReason.EXTRACTION_CONFLICT,
    ),
    LedgerExceptionDecision.REQUEST_MORE_EVIDENCE: (
        LedgerExceptionReason.EVIDENCE_GAP,
        LedgerExceptionReason.PARTY_DATE_AMOUNT_UNCLEAR,
    ),
    LedgerExceptionDecision.DEFER_WITH_REASON: (
        LedgerExceptionReason.AWAITING_CLIENT_INPUT,
        LedgerExceptionReason.AWAITING_EXTERNAL_RECORD,
        LedgerExceptionReason.NEEDS_LEAD_REVIEW,
    ),
}


class WebAgentLedgerExtractionReviewService:
    """Project staged batches and confirm only the complete low-risk lane."""

    def __init__(self, *, store: AgentLedgerExtractionReviewStorePort) -> None:
        if not all(
            callable(getattr(store, method, None))
            for method in (
                "is_available_for_actor",
                "list_review_batches",
                "confirm_low_risk_batch",
                "list_exception_groups",
                "list_exception_group_members",
                "read_exception_batch_state",
                "decide_exception_group",
            )
        ):
            raise ValueError("Web Agent ledger extraction review store is invalid")
        self._store = store

    def is_available(self, *, identity: ServerIdentityContext) -> bool:
        """Return false when this firm lacks the exact confirmation runtime."""

        try:
            actor = _actor(identity, write=False)
            return self._store.is_available_for_actor(actor=actor) is True
        except Exception:
            return False

    def list_batches(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> tuple[WebAgentLedgerExtractionBatch, ...]:
        actor = _actor(identity, write=False)
        if self._store.is_available_for_actor(actor=actor) is not True:
            raise WebAgentLedgerExtractionReviewBlocked(
                "当前律所尚未配置材料提取批次复核能力"
            )
        matter = _uuid(matter_id, "案件编号")
        rows = self._store.list_review_batches(matter_id=matter, actor=actor)
        if not isinstance(rows, tuple) or len(rows) > 100:
            raise WebAgentLedgerExtractionReviewBlocked("材料提取批次投影无效")
        projected_items: list[WebAgentLedgerExtractionBatch] = []
        for row in rows:
            batch = _validate_batch_projection(row, matter_id=matter)
            state = self._store.read_exception_batch_state(
                matter_id=matter,
                actor=actor,
                extraction_batch_id=batch.extraction_batch_id,
            )
            groups = self._store.list_exception_groups(
                matter_id=matter,
                actor=actor,
                extraction_batch_id=batch.extraction_batch_id,
            )
            projected_items.append(
                _project_batch(
                    batch,
                    actor=actor,
                    matter_id=matter,
                    state=state,
                    groups=groups,
                )
            )
        projected = tuple(projected_items)
        if len({item.batch_id for item in projected}) != len(projected):
            raise WebAgentLedgerExtractionReviewBlocked("材料提取批次重复")
        return projected

    def confirm_low_risk_batch(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> WebAgentLedgerExtractionConfirmationReceipt:
        actor = _actor(identity, write=True)
        if self._store.is_available_for_actor(actor=actor) is not True:
            raise WebAgentLedgerExtractionReviewBlocked(
                "当前律所尚未配置材料提取批次复核能力"
            )
        matter = _uuid(matter_id, "案件编号")
        batch_identifier = _uuid(batch_id, "提取批次编号")
        version = _positive_int(expected_version, "案件版本")
        rows = self._store.list_review_batches(matter_id=matter, actor=actor)
        if not isinstance(rows, tuple):
            raise WebAgentLedgerExtractionReviewBlocked("材料提取批次投影无效")
        matches = [row for row in rows if row.extraction_batch_id == batch_identifier]
        if len(matches) != 1:
            raise WebAgentLedgerExtractionReviewBlocked("当前案件没有该提取批次")
        batch = _validate_batch_projection(matches[0], matter_id=matter)
        low_risk = tuple(
            item for item in batch.candidates
            if item.review_lane == "BULK_PROMOTION_ELIGIBLE"
        )
        if len(low_risk) != batch.low_risk_count or not low_risk:
            raise WebAgentLedgerExtractionReviewBlocked("当前批次没有可整组确认的低风险候选")
        if batch.confirmed_matter_version is None:
            if not batch.is_current or batch.current_matter_version != version:
                raise WebAgentLedgerExtractionReviewBlocked("案件输入已变化，请先刷新提取批次")
        elif batch.confirmed_matter_version != version + 1:
            raise WebAgentLedgerExtractionReviewBlocked("该提取批次已经由其他案件版本处理")

        receipt = self._store.confirm_low_risk_batch(
            matter_id=matter,
            actor=actor,
            # This opaque UUID is resolved from the HttpOnly OIDC/MFA Web
            # session by the server.  It is never a route/body field and is
            # intentionally absent from every browser response projection.
            session_id=_uuid(identity.session_id, "服务器登录会话"),
            expected_version=version,
            idempotency_key=idempotency_key,
            extraction_batch_id=batch_identifier,
        )
        if (
            str(getattr(receipt, "command_name", ""))
            != "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH"
            or str(getattr(receipt, "idempotency_key", "")) != idempotency_key
            or str(getattr(receipt, "matter_id", "")) != matter
            or str(getattr(receipt, "object_type", ""))
            != "CASE_LEDGER_EXTRACTION_BATCH"
            or str(getattr(receipt, "object_id", "")) != batch_identifier
        ):
            raise WebAgentLedgerExtractionReviewBlocked("低风险批次确认回执不一致")
        receipt_version = _positive_int(
            getattr(receipt, "matter_version", None), "低风险批次确认案件版本"
        )
        if receipt_version != version + 1:
            raise WebAgentLedgerExtractionReviewBlocked("低风险批次确认版本无效")
        fact_count = sum(item.candidate_kind == "FACT" for item in low_risk)
        transaction_count = sum(
            item.candidate_kind == "TRANSACTION" for item in low_risk
        )
        if fact_count + transaction_count != batch.low_risk_count:
            raise WebAgentLedgerExtractionReviewBlocked("低风险候选类型统计不一致")
        return WebAgentLedgerExtractionConfirmationReceipt(
            batch_id=batch_identifier,
            matter_version=receipt_version,
            confirmed_fact_count=fact_count,
            confirmed_transaction_count=transaction_count,
            confirmed_total_count=len(low_risk),
        )

    def list_exception_group_members(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        group_id: str,
        offset: int,
        limit: int,
    ) -> WebAgentLedgerExceptionMemberPage:
        actor = _actor(identity, write=False)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        batch_identifier = _uuid(batch_id, "提取批次编号")
        group_identifier = _uuid(group_id, "异常组编号")
        if type(offset) is not int or not 0 <= offset < 500:
            raise WebAgentLedgerExtractionReviewBlocked("异常组分页位置无效")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise WebAgentLedgerExtractionReviewBlocked("异常组分页大小无效")
        groups = self._store.list_exception_groups(
            matter_id=matter,
            actor=actor,
            extraction_batch_id=batch_identifier,
        )
        group = _find_group(groups, group_id=group_identifier, batch_id=batch_identifier)
        page = self._store.list_exception_group_members(
            matter_id=matter,
            actor=actor,
            exception_group_id=group_identifier,
            offset=offset,
            limit=limit,
        )
        return _project_exception_member_page(page, group=group)

    def decide_exception_group(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        batch_id: str,
        group_id: str,
        expected_version: int,
        idempotency_key: str,
        decision: str,
        reason: str,
        reason_note: str | None,
    ) -> WebAgentLedgerExceptionDecisionReceipt:
        actor = _actor(identity, write=True)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        batch_identifier = _uuid(batch_id, "提取批次编号")
        group_identifier = _uuid(group_id, "异常组编号")
        version = _positive_int(expected_version, "案件版本")
        try:
            decision_value = LedgerExceptionDecision(decision)
            reason_value = LedgerExceptionReason(reason)
        except (TypeError, ValueError):
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置选项无效") from None
        note = _optional_note(reason_note)
        groups = self._store.list_exception_groups(
            matter_id=matter,
            actor=actor,
            extraction_batch_id=batch_identifier,
        )
        group = _find_group(groups, group_id=group_identifier, batch_id=batch_identifier)
        state = self._store.read_exception_batch_state(
            matter_id=matter,
            actor=actor,
            extraction_batch_id=batch_identifier,
        )
        _validate_exception_state(
            state,
            matter_id=matter,
            batch_id=batch_identifier,
            group_count=len(groups),
        )
        if decision_value not in group.allowed_decisions:
            raise WebAgentLedgerExtractionReviewBlocked("该异常组不支持所选处置")
        if reason_value not in _DECISION_REASONS[decision_value]:
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置原因与动作不一致")
        if decision_value is LedgerExceptionDecision.DEFER_WITH_REASON and note is None:
            raise WebAgentLedgerExtractionReviewBlocked("暂缓处置必须填写原因说明")
        if group.decision is None:
            if state.matter_version != version:
                raise WebAgentLedgerExtractionReviewBlocked("案件输入已变化，请先刷新异常组")
        elif group.decision is not decision_value or group.decision_reason is not reason_value:
            raise WebAgentLedgerExtractionReviewBlocked("该异常组已经采用其他处置")
        # A decided group may be an exact response-loss replay after sibling
        # batches or unrelated work advanced the matter again.  Do not reject
        # it on the current version here: the 0048 definer owns the exact
        # idempotency-key + complete-payload comparison and rejects any drift.
        elif state.matter_version < version:
            raise WebAgentLedgerExtractionReviewBlocked("异常组案件版本无效")

        try:
            receipt = self._store.decide_exception_group(
                matter_id=matter,
                actor=actor,
                server_session_id=_uuid(identity.session_id, "服务器登录会话"),
                expected_version=version,
                idempotency_key=idempotency_key,
                exception_group_id=group_identifier,
                decision=decision_value,
                reason=reason_value,
                reason_note=note,
            )
        except LedgerExceptionReextractionSourceWindowExceeded as error:
            raise WebAgentLedgerReextractionSourceWindowExceeded(
                "该异常组来源页超过单次重新提取上限"
            ) from error
        except LedgerExceptionReextractionCohortCapacityExceeded as error:
            raise WebAgentLedgerReextractionCohortCapacityExceeded(
                "本案重新提取来源范围已达到治理上限"
            ) from error
        if (
            str(getattr(receipt, "command_name", ""))
            != "DECIDE_CASE_LEDGER_EXCEPTION_GROUP"
            or str(getattr(receipt, "idempotency_key", "")) != idempotency_key
            or str(getattr(receipt, "matter_id", "")) != matter
        ):
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置回执不一致")
        receipt_version = _positive_int(
            getattr(receipt, "matter_version", None), "异常组处置案件版本"
        )
        if receipt_version not in {version, version + 1}:
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置版本无效")

        current_groups = self._store.list_exception_groups(
            matter_id=matter,
            actor=actor,
            extraction_batch_id=batch_identifier,
        )
        current_group = _find_group(
            current_groups, group_id=group_identifier, batch_id=batch_identifier
        )
        current_state = self._store.read_exception_batch_state(
            matter_id=matter,
            actor=actor,
            extraction_batch_id=batch_identifier,
        )
        _validate_exception_state(
            current_state,
            matter_id=matter,
            batch_id=batch_identifier,
            group_count=len(current_groups),
        )
        if (
            current_group.decision is not decision_value
            or current_group.decision_reason is not reason_value
            or current_state.matter_version < receipt_version
        ):
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置结果尚不能核验")
        exception_status = _exception_review_status(current_state)
        return WebAgentLedgerExceptionDecisionReceipt(
            batch_id=batch_identifier,
            group_id=group_identifier,
            matter_version=current_state.matter_version,
            committed_matter_version=receipt_version,
            decision=decision_value.value,
            exception_review_status=exception_status,
            decided_exception_group_count=current_state.decided_exception_group_count,
            exception_group_count=current_state.exception_group_count,
            batch_resolved=(
                current_state.batch_status is LedgerExtractionBatchReviewStatus.RESOLVED
            ),
        )

    def _require_available(self, actor: Actor) -> None:
        if self._store.is_available_for_actor(actor=actor) is not True:
            raise WebAgentLedgerExtractionReviewBlocked(
                "当前律所尚未配置材料提取批次复核能力"
            )


def _project_batch(
    value: object,
    *,
    actor: Actor,
    matter_id: str,
    state: LedgerExceptionBatchState,
    groups: tuple[LedgerExceptionGroup, ...],
) -> WebAgentLedgerExtractionBatch:
    batch = _validate_batch_projection(value, matter_id=matter_id)
    _validate_exception_state(
        state,
        matter_id=matter_id,
        batch_id=batch.extraction_batch_id,
        group_count=len(groups),
    )
    if sum(group.candidate_count for group in groups) != batch.exception_count:
        raise WebAgentLedgerExtractionReviewBlocked("异常组没有覆盖完整异常候选范围")
    if len({group.group_id for group in groups}) != len(groups):
        raise WebAgentLedgerExtractionReviewBlocked("异常组投影重复")
    if state.matter_version != batch.current_matter_version:
        raise WebAgentLedgerExtractionReviewBlocked("异常组与提取批次案件版本不一致")
    exception_status = _exception_review_status(state)
    if state.batch_status is LedgerExtractionBatchReviewStatus.RESOLVED:
        status = "CONFIRMED" if batch.exception_count == 0 else "RESOLVED"
    elif not batch.exception_review_is_current:
        status = "STALE"
    elif state.low_risk_lane_status is LedgerLowRiskLaneStatus.CONFIRMED:
        status = (
            "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL"
            if exception_status == "PARTIALLY_RESOLVED"
            else "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN"
        )
    elif batch.low_risk_count == 0 and exception_status == "PARTIALLY_RESOLVED":
        status = "EXCEPTIONS_PARTIALLY_RESOLVED"
    elif batch.low_risk_count == 0:
        status = "EXCEPTIONS_ONLY"
    else:
        status = "REVIEW_READY"
    candidates = tuple(
        _project_candidate(candidate, sequence=index)
        for index, candidate in enumerate(batch.candidates, start=1)
    )
    low_risk = tuple(
        item for item, raw in zip(candidates, batch.candidates, strict=True)
        if raw.review_lane == "BULK_PROMOTION_ELIGIBLE"
    )
    exceptions = tuple(
        item for item, raw in zip(candidates, batch.candidates, strict=True)
        if raw.review_lane == "EXCEPTION_REVIEW"
    )
    projected_groups = tuple(
        _project_exception_group(
            group,
            actor=actor,
            batch_id=batch.extraction_batch_id,
            batch_status=status,
        )
        for group in groups
    )
    return WebAgentLedgerExtractionBatch(
        batch_id=batch.extraction_batch_id,
        matter_id=matter_id,
        status=status,
        current_matter_version=batch.current_matter_version,
        source_matter_version=batch.source_matter_version,
        candidate_count=batch.candidate_count,
        low_risk_count=batch.low_risk_count,
        exception_count=batch.exception_count,
        staged_at=batch.staged_at,
        confirmed_at=batch.confirmed_at,
        can_confirm_low_risk=(
            status == "REVIEW_READY"
            and state.low_risk_lane_status is LedgerLowRiskLaneStatus.OPEN
            and Role.LEAD_LAWYER in actor.roles
        ),
        exception_review_status=exception_status,
        exception_group_count=state.exception_group_count,
        decided_exception_group_count=state.decided_exception_group_count,
        exception_groups=projected_groups,
        low_risk_candidates=low_risk,
        exception_candidates=exceptions,
    )


def _project_exception_group(
    value: LedgerExceptionGroup,
    *,
    actor: Actor,
    batch_id: str,
    batch_status: str,
) -> WebAgentLedgerExceptionGroup:
    if not isinstance(value, LedgerExceptionGroup):
        raise WebAgentLedgerExtractionReviewBlocked("异常组投影无效")
    group_id = _uuid(value.group_id, "异常组编号")
    if _uuid(value.extraction_batch_id, "异常组批次编号") != batch_id:
        raise WebAgentLedgerExtractionReviewBlocked("异常组不属于当前提取批次")
    if value.candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebAgentLedgerExtractionReviewBlocked("异常组候选类型无效")
    if type(value.candidate_count) is not int or not 1 <= value.candidate_count <= 500:
        raise WebAgentLedgerExtractionReviewBlocked("异常组候选数量无效")
    if (
        not isinstance(value.reason_codes, tuple)
        or not value.reason_codes
        or len(set(value.reason_codes)) != len(value.reason_codes)
        or any(code not in _REASON_LABELS for code in value.reason_codes)
    ):
        raise WebAgentLedgerExtractionReviewBlocked("异常组原因无效")
    if value.source_policy not in _SOURCE_GUIDANCE or value.risk_policy not in _RISK_LABELS:
        raise WebAgentLedgerExtractionReviewBlocked("异常组策略无效")
    if (
        not isinstance(value.allowed_decisions, tuple)
        or not value.allowed_decisions
        or len(set(value.allowed_decisions)) != len(value.allowed_decisions)
        or any(item not in _DECISION_LABELS for item in value.allowed_decisions)
    ):
        raise WebAgentLedgerExtractionReviewBlocked("异常组可选处置无效")
    if (value.decision is None) != (value.decision_reason is None):
        raise WebAgentLedgerExtractionReviewBlocked("异常组处置状态不完整")
    if value.decision is not None:
        if (
            value.decision not in value.allowed_decisions
            or value.decision_reason not in _DECISION_REASONS[value.decision]
        ):
            raise WebAgentLedgerExtractionReviewBlocked("异常组既有处置不符合策略")
    actions = tuple(
        WebAgentLedgerExceptionAction(
            code=decision.value,
            label=_DECISION_LABELS[decision],
            consequence=_DECISION_CONSEQUENCES[decision],
            requires_note=(decision is LedgerExceptionDecision.DEFER_WITH_REASON),
            reasons=tuple(
                WebAgentLedgerExceptionReasonOption(
                    code=reason.value,
                    label=_REASON_OPTION_LABELS[reason],
                )
                for reason in _DECISION_REASONS[decision]
            ),
        )
        for decision in value.allowed_decisions
    )
    summary = _text(value.summary, "异常组摘要", 1_000)
    if "CONTRADICTS_CASE_LEDGER" in value.reason_codes:
        # Stored model flags are not deterministic ledger comparisons. Keep
        # historical group identity/policies unchanged, but do not present its
        # old assertive summary as a verified finding in the lawyer surface.
        summary = f"{value.candidate_count}条候选：模型提示可能与台账矛盾，尚未核实。"
    return WebAgentLedgerExceptionGroup(
        group_id=group_id,
        candidate_kind=value.candidate_kind,
        candidate_count=value.candidate_count,
        summary=summary,
        review_reasons=tuple(_REASON_LABELS[code] for code in value.reason_codes),
        source_guidance=_SOURCE_GUIDANCE[value.source_policy],
        risk_label=_RISK_LABELS[value.risk_policy],
        status="OPEN" if value.decision is None else "DECIDED",
        decision=None if value.decision is None else value.decision.value,
        decision_label=(
            None if value.decision is None else _DECISION_LABELS[value.decision]
        ),
        decision_reason=(
            None if value.decision_reason is None else value.decision_reason.value
        ),
        decision_reason_label=(
            None
            if value.decision_reason is None
            else _REASON_OPTION_LABELS[value.decision_reason]
        ),
        can_decide=(
            value.decision is None
            and batch_status != "STALE"
            and Role.LEAD_LAWYER in actor.roles
        ),
        allowed_actions=actions,
    )


def _project_exception_member_page(
    value: object, *, group: LedgerExceptionGroup
) -> WebAgentLedgerExceptionMemberPage:
    if not isinstance(value, LedgerExceptionGroupMemberPage):
        raise WebAgentLedgerExtractionReviewBlocked("异常组成员分页无效")
    if _uuid(value.group_id, "异常组编号") != group.group_id:
        raise WebAgentLedgerExtractionReviewBlocked("异常组成员分页不属于当前组")
    if (
        type(value.total_count) is not int
        or value.total_count != group.candidate_count
        or type(value.offset) is not int
        or not 0 <= value.offset < value.total_count
        or not isinstance(value.members, tuple)
        or not 1 <= len(value.members) <= 50
    ):
        raise WebAgentLedgerExtractionReviewBlocked("异常组成员分页范围无效")
    expected_next = value.offset + len(value.members)
    if value.next_offset not in {
        None if expected_next >= value.total_count else expected_next
    }:
        raise WebAgentLedgerExtractionReviewBlocked("异常组成员分页游标无效")
    members: list[WebAgentLedgerExceptionMember] = []
    for index, member in enumerate(value.members, start=value.offset + 1):
        if member.sequence != index or member.candidate_kind != group.candidate_kind:
            raise WebAgentLedgerExtractionReviewBlocked("异常组成员顺序或类型无效")
        if (
            isinstance(member.confidence, bool)
            or not isinstance(member.confidence, (int, float))
            or not math.isfinite(float(member.confidence))
            or not 0 <= float(member.confidence) <= 1
            or member.reason_codes != group.reason_codes
            or not isinstance(member.excerpts, tuple)
            or not 1 <= len(member.excerpts) <= 20
        ):
            raise WebAgentLedgerExtractionReviewBlocked("异常组成员投影无效")
        excerpts = tuple(
            WebAgentLedgerExtractionExcerpt(
                evidence_page_id=_uuid(item.evidence_page_id, "证据页面编号"),
                page_number=_positive_int(item.page_number, "证据页码"),
                text=_text(item.text, "异常组来源摘录", 2_000),
            )
            for item in member.excerpts
        )
        members.append(
            WebAgentLedgerExceptionMember(
                extraction_candidate_id=(_uuid(member.extraction_candidate_id, "原候选编号") if member.extraction_candidate_id else None),
                sequence=index,
                candidate_kind=member.candidate_kind,
                summary=_text(member.summary, "异常组成员摘要", 1_000),
                confidence=round(float(member.confidence), 4),
                review_reasons=tuple(
                    _REASON_LABELS[code] for code in member.reason_codes
                ),
                excerpts=excerpts,
            )
        )
    return WebAgentLedgerExceptionMemberPage(
        group_id=group.group_id,
        total_count=value.total_count,
        offset=value.offset,
        next_offset=value.next_offset,
        members=tuple(members),
    )


def _find_group(
    groups: object, *, group_id: str, batch_id: str
) -> LedgerExceptionGroup:
    if not isinstance(groups, tuple) or len(groups) > 500:
        raise WebAgentLedgerExtractionReviewBlocked("异常组投影无效")
    matches = [
        group
        for group in groups
        if isinstance(group, LedgerExceptionGroup)
        and group.group_id == group_id
        and group.extraction_batch_id == batch_id
    ]
    if len(matches) != 1:
        raise WebAgentLedgerExtractionReviewBlocked("当前提取批次没有该异常组")
    return matches[0]


def _validate_exception_state(
    value: object, *, matter_id: str, batch_id: str, group_count: int
) -> LedgerExceptionBatchState:
    if not isinstance(value, LedgerExceptionBatchState):
        raise WebAgentLedgerExtractionReviewBlocked("异常组批次状态无效")
    if (
        value.matter_id != matter_id
        or value.extraction_batch_id != batch_id
        or _uuid(value.run_id, "Agent 运行编号") != value.run_id
        or type(value.matter_version) is not int
        or value.matter_version < 1
        or value.exception_group_count != group_count
        or not 0 <= value.decided_exception_group_count <= value.exception_group_count
    ):
        raise WebAgentLedgerExtractionReviewBlocked("异常组批次状态不一致")
    return value


def _exception_review_status(value: LedgerExceptionBatchState) -> str:
    if value.exception_group_count == 0:
        if value.decided_exception_group_count != 0:
            raise WebAgentLedgerExtractionReviewBlocked("异常组处置数量无效")
        return "NONE"
    if value.decided_exception_group_count == 0:
        return "OPEN"
    if value.decided_exception_group_count < value.exception_group_count:
        return "PARTIALLY_RESOLVED"
    return "RESOLVED"


def _validate_batch_projection(
    value: object, *, matter_id: str
) -> CaseLedgerExtractionReviewBatchProjection:
    if not isinstance(value, CaseLedgerExtractionReviewBatchProjection):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取批次投影无效")
    if _uuid(value.extraction_batch_id, "提取批次编号") != value.extraction_batch_id:
        raise WebAgentLedgerExtractionReviewBlocked("提取批次编号无效")
    _uuid(value.run_id, "Agent 运行编号")
    if value.matter_id != matter_id:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取批次不属于当前案件")
    _positive_int(value.current_matter_version, "当前案件版本")
    _positive_int(value.source_matter_version, "提取来源案件版本")
    for count in (
        value.candidate_count,
        value.low_risk_count,
        value.exception_count,
    ):
        if type(count) is not int or not 0 <= count <= 500:
            raise WebAgentLedgerExtractionReviewBlocked("材料提取候选数量无效")
    if value.low_risk_count + value.exception_count != value.candidate_count:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取候选分组数量不一致")
    if not isinstance(value.staged_at, datetime) or value.staged_at.tzinfo is None:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取批次时间无效")
    if (
        type(value.is_current) is not bool
        or type(value.exception_review_is_current) is not bool
    ):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取批次有效状态无效")
    if value.confirmed_matter_version is not None:
        _positive_int(value.confirmed_matter_version, "批次确认案件版本")
        if not isinstance(value.confirmed_at, datetime) or value.confirmed_at.tzinfo is None:
            raise WebAgentLedgerExtractionReviewBlocked("材料提取确认时间无效")
    elif value.confirmed_at is not None:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取确认状态不一致")
    if not isinstance(value.candidates, tuple) or len(value.candidates) != value.candidate_count:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取候选投影不完整")
    for index, candidate in enumerate(value.candidates, start=1):
        _project_candidate(candidate, sequence=index)
    ids = [candidate.extraction_candidate_id for candidate in value.candidates]
    if len(set(ids)) != len(ids):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取候选重复")
    return value


def _project_candidate(
    value: object, *, sequence: int
) -> WebAgentLedgerExtractionCandidate:
    if not isinstance(value, CaseLedgerExtractionReviewCandidateProjection):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取候选投影无效")
    _uuid(value.extraction_candidate_id, "提取候选编号")
    if value.candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取候选类型无效")
    if value.review_lane not in {"BULK_PROMOTION_ELIGIBLE", "EXCEPTION_REVIEW"}:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取复核分组无效")
    if (
        isinstance(value.confidence, bool)
        or not isinstance(value.confidence, (int, float))
        or not math.isfinite(float(value.confidence))
        or not 0 <= float(value.confidence) <= 1
    ):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取置信度无效")
    summary = _text(value.summary, "材料提取候选摘要", 2_000)
    if (
        not isinstance(value.review_reason_codes, tuple)
        or len(set(value.review_reason_codes)) != len(value.review_reason_codes)
        or any(code not in _REASON_LABELS for code in value.review_reason_codes)
    ):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取复核原因无效")
    if value.review_lane == "BULK_PROMOTION_ELIGIBLE" and value.review_reason_codes:
        raise WebAgentLedgerExtractionReviewBlocked("低风险候选不得携带例外原因")
    if value.review_lane == "EXCEPTION_REVIEW" and not value.review_reason_codes:
        raise WebAgentLedgerExtractionReviewBlocked("例外候选缺少复核原因")
    if not isinstance(value.excerpts, tuple) or not 1 <= len(value.excerpts) <= 20:
        raise WebAgentLedgerExtractionReviewBlocked("材料提取来源摘录无效")
    excerpts = tuple(_project_excerpt(item) for item in value.excerpts)
    if len({item.evidence_page_id for item in excerpts}) != len(excerpts):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取来源页面重复")
    return WebAgentLedgerExtractionCandidate(
        sequence=sequence,
        candidate_kind=value.candidate_kind,
        summary=summary,
        confidence=round(float(value.confidence), 4),
        review_status=(
            "LOW_RISK" if value.review_lane == "BULK_PROMOTION_ELIGIBLE" else "EXCEPTION"
        ),
        review_reasons=tuple(_REASON_LABELS[code] for code in value.review_reason_codes),
        excerpts=excerpts,
    )


def _project_excerpt(value: object) -> WebAgentLedgerExtractionExcerpt:
    if not isinstance(value, CaseLedgerExtractionReviewExcerptProjection):
        raise WebAgentLedgerExtractionReviewBlocked("材料提取来源摘录无效")
    return WebAgentLedgerExtractionExcerpt(
        evidence_page_id=_uuid(value.evidence_page_id, "证据页面编号"),
        page_number=_positive_int(value.page_number, "证据页码"),
        text=_text(value.text, "证据来源摘录", 2_000),
    )


def _actor(identity: ServerIdentityContext, *, write: bool) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebAgentLedgerExtractionReviewBlocked("律师身份无效")
    try:
        identity.validate()
    except Exception:
        raise WebAgentLedgerExtractionReviewBlocked("律师登录已失效") from None
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebAgentLedgerExtractionReviewBlocked("该复核只接受已通过 MFA 的律师身份")
    actor = identity.actor
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_HUMAN_ROLES):
        raise WebAgentLedgerExtractionReviewBlocked("当前身份不能查看材料提取批次")
    if write and Role.LEAD_LAWYER not in actor.roles:
        raise WebAgentLedgerExtractionReviewBlocked("仅主办律师可以确认低风险候选组")
    return actor


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebAgentLedgerExtractionReviewBlocked(f"{label}无效") from None


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise WebAgentLedgerExtractionReviewBlocked(f"{label}无效")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise WebAgentLedgerExtractionReviewBlocked(f"{label}无效")
    return value.strip()


def _optional_note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WebAgentLedgerExtractionReviewBlocked("异常组处置说明无效")
    normalized = value.strip()
    if not normalized:
        return None
    if (
        len(normalized) > 500
        or len(normalized.encode("utf-8")) > 2_000
        or any(
            ord(character) < 32 and character not in "\n\t"
            for character in normalized
        )
    ):
        raise WebAgentLedgerExtractionReviewBlocked("异常组处置说明无效")
    return normalized


__all__ = (
    "AgentLedgerExtractionReviewStorePort",
    "CaseLedgerExtractionReviewBatchProjection",
    "CaseLedgerExtractionReviewCandidateProjection",
    "CaseLedgerExtractionReviewExcerptProjection",
    "WebAgentLedgerExtractionBatch",
    "WebAgentLedgerExtractionCandidate",
    "WebAgentLedgerExtractionConfirmationReceipt",
    "WebAgentLedgerExtractionExcerpt",
    "WebAgentLedgerExceptionAction",
    "WebAgentLedgerExceptionDecisionReceipt",
    "WebAgentLedgerExceptionGroup",
    "WebAgentLedgerExceptionMember",
    "WebAgentLedgerExceptionMemberPage",
    "WebAgentLedgerExceptionReasonOption",
    "WebAgentLedgerExtractionReviewBlocked",
    "WebAgentLedgerReextractionCohortCapacityExceeded",
    "WebAgentLedgerReextractionSourceWindowExceeded",
    "WebAgentLedgerExtractionReviewPort",
    "WebAgentLedgerExtractionReviewService",
)
