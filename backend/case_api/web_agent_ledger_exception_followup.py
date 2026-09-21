"""Browser-safe application service for routed ledger-exception follow-ups.

An exception-group decision in migration 0047 records only where the work was
routed.  Migration 0049 owns the durable work that remains after that route:
re-extraction, a managed request for new evidence, or a deferred lawyer
review.  This module exposes that distinction to an OIDC/MFA lawyer without
leaking control-run identifiers, graph/task identifiers, semantic hashes,
object-store keys, or a browser-selected evidence request.

The PostgreSQL store remains authoritative.  In particular, every write is
session-bound and the store re-reads the current follow-up, managed request,
source set, matter version, role and idempotency record in one transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.case_agent_ledger_exception_followup import (
    LedgerExceptionControlHealth,
    LedgerExceptionFollowup,
    LedgerExceptionFollowupAction,
    LedgerExceptionFollowupKind,
    LedgerExceptionFollowupSnapshot,
    LedgerExceptionFollowupState,
    ManagedEvidenceSourceCandidate,
    ManagedEvidenceSourceRef,
    ManagedEvidenceSourceType,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    ActiveLedgerExceptionFollowupPage,
    ExceptionControlRecoveryIntentReceipt,
    ExceptionControlTransferReceipt,
    ExceptionControlState,
    FollowupEvidencePageIdPage,
    ManagedEvidenceSourceCandidatePage,
)
from case_kernel.models import Actor, Role

from .web_case_agent_run_identity import derive_web_case_agent_entity_id


class WebAgentLedgerExceptionFollowupBlocked(ValueError):
    """The requested projection or command is not safe for the Web boundary."""


class AgentLedgerExceptionFollowupStorePort(Protocol):
    """Narrow PostgreSQL authority used by the Web application service."""

    def list_active_followups(
        self, *, matter_id: str, actor: Actor, offset: int, limit: int
    ) -> ActiveLedgerExceptionFollowupPage: ...

    def get_followup(
        self, *, matter_id: str, actor: Actor, followup_id: str
    ) -> LedgerExceptionFollowup: ...

    def list_eligible_managed_evidence_sources(
        self, *, matter_id: str, actor: Actor, followup_id: str,
        offset: int, limit: int
    ) -> ManagedEvidenceSourceCandidatePage: ...

    def list_followup_evidence_page_ids(
        self, *, matter_id: str, actor: Actor, followup_id: str,
        offset: int, limit: int
    ) -> FollowupEvidencePageIdPage: ...

    def read_current_control_state(
        self, *, matter_id: str, actor: Actor
    ) -> ExceptionControlState | None: ...

    def resolve_followup(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        expected_version: int,
        idempotency_key: str,
        followup_id: str,
        action: LedgerExceptionFollowupAction,
        reason_note: str | None,
        managed_evidence_sources: Sequence[ManagedEvidenceSourceRef] = (),
    ) -> CaseLedgerCommandReceipt: ...

    def transfer_control_to_recovery_run(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        replacement_run_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ExceptionControlTransferReceipt: ...

    def prepare_control_recovery(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        replacement_run_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ExceptionControlRecoveryIntentReceipt: ...


class MatterVersionReaderPort(Protocol):
    def get_case_snapshot(self, *, matter_id: str, actor: Actor) -> object: ...


class CaseAgentRecoveryRunPort(Protocol):
    def create_recovery_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        replacement_run_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> object: ...


@dataclass(frozen=True)
class WebAgentLedgerFollowupAction:
    code: str
    label: str
    consequence: str
    requires_reason: bool


@dataclass(frozen=True)
class WebManagedEvidenceSource:
    object_type: str
    object_id: str
    display_label: str
    created_at: datetime


@dataclass(frozen=True)
class WebManagedEvidenceSourceSelection:
    object_type: str
    object_id: str


@dataclass(frozen=True)
class WebManagedEvidenceSourcePage:
    total_count: int
    offset: int
    next_offset: int | None
    sources: tuple[WebManagedEvidenceSource, ...]


@dataclass(frozen=True)
class WebFollowupEvidencePageIdPage:
    total_count: int
    offset: int
    next_offset: int | None
    evidence_page_ids: tuple[str, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionFollowup:
    followup_id: str
    kind: str
    state: str
    head_sequence: int
    origin_batch_id: str
    origin_group_id: str
    current_matter_version: int
    created_matter_version: int
    created_at: datetime
    reason: str
    reason_note: str | None
    candidate_count: int
    review_reasons: tuple[str, ...]
    evidence_page_count: int
    acceptance_requirements: tuple[str, ...]
    automation_status: str | None
    can_act: bool
    allowed_actions: tuple[WebAgentLedgerFollowupAction, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionFollowupPage:
    total_count: int
    offset: int
    next_offset: int | None
    control_health: str | None
    can_recover: bool
    followups: tuple[WebAgentLedgerExceptionFollowup, ...]


@dataclass(frozen=True)
class WebAgentLedgerExceptionFollowupReceipt:
    followup_id: str
    action: str
    terminal_state: str
    matter_version: int


@dataclass(frozen=True)
class WebAgentLedgerExceptionRecoveryReceipt:
    matter_version: int
    control_health: str
    recovery_started: bool


class WebAgentLedgerExceptionFollowupPort(Protocol):
    def is_available(self, *, identity: ServerIdentityContext) -> bool: ...

    def list_followups(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        offset: int,
        limit: int,
    ) -> WebAgentLedgerExceptionFollowupPage: ...

    def list_eligible_managed_evidence_sources(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> WebManagedEvidenceSourcePage: ...

    def list_followup_evidence_page_ids(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> WebFollowupEvidencePageIdPage: ...

    def resolve_followup(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        expected_version: int,
        idempotency_key: str,
        action: str,
        reason_note: str,
        managed_evidence_sources: tuple[WebManagedEvidenceSourceSelection, ...],
    ) -> WebAgentLedgerExceptionFollowupReceipt: ...

    def recover_exception_followups(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebAgentLedgerExceptionRecoveryReceipt: ...


_HUMAN_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)

_REASON_LABELS = {
    "DUPLICATE_CONFIRMED": "已对照来源，确认属于重复记录",
    "SOURCE_QUALITY_INSUFFICIENT": "原页质量不足，需要重新识别",
    "EXTRACTION_CONFLICT": "提取结果存在冲突，需要重新分析",
    "EVIDENCE_GAP": "现有材料不足，需要补证",
    "PARTY_DATE_AMOUNT_UNCLEAR": "主体、日期或金额仍不明确",
    "AWAITING_CLIENT_INPUT": "等待当事人补充说明或材料",
    "AWAITING_EXTERNAL_RECORD": "等待银行、平台或其他外部记录",
    "NEEDS_LEAD_REVIEW": "需要主办律师进一步研判",
}

_CANONICAL_REASON_LABELS = {
    "POSSIBLE_DUPLICATE": "可能与其他候选重复",
    "PARTY_AMBIGUOUS": "相关当事人或收付款人不明确",
    "DATE_AMBIGUOUS": "日期无法从材料中唯一确定",
    "AMOUNT_AMBIGUOUS": "金额无法从材料中唯一确定",
    "CROSS_PAGE_CONFLICT": "不同页面的信息存在冲突",
    "CONTRADICTS_CASE_LEDGER": "与本案当前台账存在矛盾",
    "OCR_DERIVED": "内容来自 OCR 或视觉识别",
    "LOW_CONFIDENCE": "未达到自动整组确认门槛",
    "LEGAL_CONCLUSION_RISK": "候选可能夹带法律判断",
    "INCOMPLETE_TRANSACTION": "收付款必要字段不完整",
    "UNTRUSTED_TEXT": "材料文本的可信边界不足",
    "BELOW_BULK_CONFIDENCE_THRESHOLD": "未达到整组确认的置信门槛",
    "NON_NATIVE_SOURCE": "来源不是可直接复核的原生文本",
    "SOURCE_TEXT_NOT_REVERIFIED": "服务器未能重新核验来源文字",
    "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE": "与当前案件台账冲突或重复",
}

_ACTION_POLICY = {
    LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE: WebAgentLedgerFollowupAction(
        code=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE.value,
        label="确认新增材料满足本次补证",
        consequence="服务器只接受本案补证请求之后新入卷、仍有效的受管材料，并保留精确来源绑定。",
        requires_reason=True,
    ),
    LedgerExceptionFollowupAction.RESUME: WebAgentLedgerFollowupAction(
        code=LedgerExceptionFollowupAction.RESUME.value,
        label="恢复本组复核",
        consequence="暂缓状态终止，Agent 会依据当前案件版本重新规划后续工作。",
        requires_reason=True,
    ),
    LedgerExceptionFollowupAction.WITHDRAW: WebAgentLedgerFollowupAction(
        code=LedgerExceptionFollowupAction.WITHDRAW.value,
        label="撤回本项后续工作",
        consequence="本项不再阻挡后续计划；撤回原因会保留在审计记录中。",
        requires_reason=True,
    ),
    LedgerExceptionFollowupAction.SUPERSEDE: WebAgentLedgerFollowupAction(
        code=LedgerExceptionFollowupAction.SUPERSEDE.value,
        label="标记已由新情况替代",
        consequence="本项不再作为当前工作；替代原因会保留，Agent 必须基于当前案件重新研判。",
        requires_reason=True,
    ),
}

_ACTIONS_BY_KIND = {
    LedgerExceptionFollowupKind.REEXTRACTION: (
        LedgerExceptionFollowupAction.WITHDRAW,
        LedgerExceptionFollowupAction.SUPERSEDE,
    ),
    LedgerExceptionFollowupKind.MORE_EVIDENCE: (
        LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
        LedgerExceptionFollowupAction.WITHDRAW,
        LedgerExceptionFollowupAction.SUPERSEDE,
    ),
    LedgerExceptionFollowupKind.DEFERRED_REVIEW: (
        LedgerExceptionFollowupAction.RESUME,
        LedgerExceptionFollowupAction.WITHDRAW,
        LedgerExceptionFollowupAction.SUPERSEDE,
    ),
}

_TERMINAL_STATE_BY_ACTION = {
    LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE: "SATISFIED",
    LedgerExceptionFollowupAction.RESUME: "RESUMED",
    LedgerExceptionFollowupAction.WITHDRAW: "WITHDRAWN",
    LedgerExceptionFollowupAction.SUPERSEDE: "SUPERSEDED",
}

class WebAgentLedgerExceptionFollowupService:
    """Expose ACTIVE 0049 work and bounded lead-lawyer transitions."""

    def __init__(
        self,
        *,
        store: AgentLedgerExceptionFollowupStorePort,
        matter_version_reader: MatterVersionReaderPort,
        recovery_run_service: CaseAgentRecoveryRunPort,
        configured_firm_ids: frozenset[str],
    ) -> None:
        if not all(
            callable(getattr(store, method, None))
            for method in (
                "list_active_followups",
                "get_followup",
                "list_eligible_managed_evidence_sources",
                "list_followup_evidence_page_ids",
                "read_current_control_state",
                "resolve_followup",
                "prepare_control_recovery",
                "transfer_control_to_recovery_run",
            )
        ):
            raise ValueError("Web Agent ledger exception follow-up store is invalid")
        if not callable(getattr(matter_version_reader, "get_case_snapshot", None)):
            raise ValueError("Web Agent ledger exception matter reader is invalid")
        if not callable(
            getattr(recovery_run_service, "create_recovery_run", None)
        ):
            raise ValueError("Web Agent ledger exception recovery run service is invalid")
        if (
            not isinstance(configured_firm_ids, frozenset)
            or any(_try_uuid(value) is None for value in configured_firm_ids)
        ):
            raise ValueError("Web Agent ledger exception configured firms are invalid")
        self._store = store
        self._matter_version_reader = matter_version_reader
        self._recovery_run_service = recovery_run_service
        self._configured_firm_ids = configured_firm_ids

    def is_available(self, *, identity: ServerIdentityContext) -> bool:
        try:
            actor = _actor(identity, write=False)
            return actor.firm_id in self._configured_firm_ids
        except Exception:
            return False

    def list_followups(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        offset: int,
        limit: int,
    ) -> WebAgentLedgerExceptionFollowupPage:
        actor = _actor(identity, write=False)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        if type(offset) is not int or offset < 0:
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作分页位置无效")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作分页大小无效")
        current_version = self._current_matter_version(matter_id=matter, actor=actor)
        page = self._store.list_active_followups(
            matter_id=matter,
            actor=actor,
            offset=offset,
            limit=limit,
        )
        if (
            not isinstance(page, ActiveLedgerExceptionFollowupPage)
            or page.offset != offset
            or type(page.total_count) is not int
            or page.total_count < offset + len(page.followups)
            or len(page.followups) > limit
        ):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作投影无效")
        rows = page.followups
        control_health_values = {
            item.control_health
            for item in rows
            if isinstance(item, LedgerExceptionFollowupSnapshot)
        }
        if len(control_health_values) != (1 if rows else 0):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作控制状态不一致")
        control_health = next(iter(control_health_values), None)
        projected = tuple(
            self._project_followup(
                row,
                actor=actor,
                current_matter_version=current_version,
            )
            for row in rows
        )
        if len({item.followup_id for item in projected}) != len(projected):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作重复")
        total_count = page.total_count
        next_offset = offset + len(projected)
        return WebAgentLedgerExceptionFollowupPage(
            total_count=total_count,
            offset=offset,
            next_offset=next_offset if next_offset < total_count else None,
            control_health=(
                None if control_health is None else control_health.value
            ),
            can_recover=(
                Role.LEAD_LAWYER in actor.roles
                and control_health is LedgerExceptionControlHealth.RECOVERY_REQUIRED
            ),
            followups=projected,
        )

    def list_eligible_managed_evidence_sources(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> WebManagedEvidenceSourcePage:
        actor = _actor(identity, write=False)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        followup = _uuid(followup_id, "异常后续工作编号")
        _page_request(offset=offset, limit=limit)
        active = self._exact_followup(
            matter_id=matter,
            actor=actor,
            followup_id=followup,
        )
        if (
            active.state is not LedgerExceptionFollowupState.ACTIVE
            or active.kind is not LedgerExceptionFollowupKind.MORE_EVIDENCE
        ):
            raise WebAgentLedgerExceptionFollowupBlocked("当前后续工作不是补证请求")
        page = self._store.list_eligible_managed_evidence_sources(
            matter_id=matter,
            actor=actor,
            followup_id=followup,
            offset=offset,
            limit=limit,
        )
        if (
            not isinstance(page, ManagedEvidenceSourceCandidatePage)
            or page.offset != offset
            or type(page.total_count) is not int
            or page.total_count < offset + len(page.sources)
            or len(page.sources) > limit
        ):
            raise WebAgentLedgerExceptionFollowupBlocked("可用补证材料投影无效")
        projected: list[WebManagedEvidenceSource] = []
        seen: set[str] = set()
        for row in page.sources:
            if not isinstance(row, ManagedEvidenceSourceCandidate):
                raise WebAgentLedgerExceptionFollowupBlocked("可用补证材料投影无效")
            try:
                row.validate()
            except Exception:
                raise WebAgentLedgerExceptionFollowupBlocked(
                    "可用补证材料投影无效"
                ) from None
            key = f"{row.object_type.value}:{row.object_id}"
            if key in seen:
                raise WebAgentLedgerExceptionFollowupBlocked("可用补证材料重复")
            seen.add(key)
            projected.append(
                WebManagedEvidenceSource(
                    object_type=row.object_type.value,
                    object_id=row.object_id,
                    display_label=row.display_label,
                    created_at=row.created_at,
                )
            )
        next_offset = offset + len(projected)
        return WebManagedEvidenceSourcePage(
            total_count=page.total_count,
            offset=offset,
            next_offset=(next_offset if next_offset < page.total_count else None),
            sources=tuple(projected),
        )

    def list_followup_evidence_page_ids(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> WebFollowupEvidencePageIdPage:
        actor = _actor(identity, write=False)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        followup = _uuid(followup_id, "异常后续工作编号")
        _page_request(offset=offset, limit=limit)
        exact = self._exact_followup(
            matter_id=matter,
            actor=actor,
            followup_id=followup,
        )
        if exact.state is not LedgerExceptionFollowupState.ACTIVE:
            raise WebAgentLedgerExceptionFollowupBlocked(
                "当前案件没有该待完成的异常后续工作"
            )
        page = self._store.list_followup_evidence_page_ids(
            matter_id=matter,
            actor=actor,
            followup_id=followup,
            offset=offset,
            limit=limit,
        )
        if (
            not isinstance(page, FollowupEvidencePageIdPage)
            or page.offset != offset
            or type(page.total_count) is not int
            or page.total_count < offset + len(page.evidence_page_ids)
            or not page.evidence_page_ids
            or len(page.evidence_page_ids) > limit
            or len(set(page.evidence_page_ids)) != len(page.evidence_page_ids)
        ):
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作来源页投影无效"
            )
        for page_id in page.evidence_page_ids:
            _uuid(page_id, "证据页编号")
        next_offset = offset + len(page.evidence_page_ids)
        return WebFollowupEvidencePageIdPage(
            total_count=page.total_count,
            offset=offset,
            next_offset=(next_offset if next_offset < page.total_count else None),
            evidence_page_ids=page.evidence_page_ids,
        )

    def resolve_followup(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        followup_id: str,
        expected_version: int,
        idempotency_key: str,
        action: str,
        reason_note: str,
        managed_evidence_sources: tuple[WebManagedEvidenceSourceSelection, ...],
    ) -> WebAgentLedgerExceptionFollowupReceipt:
        actor = _actor(identity, write=True)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        followup = _uuid(followup_id, "异常后续工作编号")
        version = _positive_int(expected_version, "案件版本")
        try:
            action_value = LedgerExceptionFollowupAction(action)
        except (TypeError, ValueError):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作动作无效") from None
        note = _required_note(reason_note)
        source_refs = _source_refs(managed_evidence_sources)
        if action_value is LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE:
            if not source_refs:
                raise WebAgentLedgerExceptionFollowupBlocked("确认补证必须选择新增受管材料")
        elif source_refs:
            raise WebAgentLedgerExceptionFollowupBlocked("该动作不能携带补证材料")

        # On an ordinary first submission, pre-validate the advertised policy
        # and the exact opaque source set for a clearer lawyer-facing error.
        # If the ACTIVE head is absent, continue to the store: this may be the
        # explicit same-key replay of a response-lost command whose terminal
        # head is intentionally excluded from GET.  The 0049 definer then
        # returns the saved receipt or rejects a different intent.
        current = self._exact_followup(
            matter_id=matter,
            actor=actor,
            followup_id=followup,
        )
        if current.state is LedgerExceptionFollowupState.ACTIVE:
            if action_value not in _ACTIONS_BY_KIND[current.kind]:
                raise WebAgentLedgerExceptionFollowupBlocked(
                    "当前后续工作不支持所选动作"
                )

        receipt = self._store.resolve_followup(
            matter_id=matter,
            actor=actor,
            server_session_id=_uuid(identity.session_id, "服务器登录会话"),
            expected_version=version,
            idempotency_key=idempotency_key,
            followup_id=followup,
            action=action_value,
            reason_note=note,
            managed_evidence_sources=source_refs,
        )
        if (
            not isinstance(receipt, CaseLedgerCommandReceipt)
            or receipt.command_name != "RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP"
            or receipt.idempotency_key != idempotency_key
            or receipt.matter_id != matter
            or receipt.matter_version != version + 1
            or receipt.object_type != "CASE_LEDGER_EXCEPTION_FOLLOWUP"
            or receipt.object_id != followup
        ):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作回执不一致")
        return WebAgentLedgerExceptionFollowupReceipt(
            followup_id=followup,
            action=action_value.value,
            terminal_state=_TERMINAL_STATE_BY_ACTION[action_value],
            matter_version=receipt.matter_version,
        )

    def recover_exception_followups(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebAgentLedgerExceptionRecoveryReceipt:
        """Create one governed recovery run and atomically transfer control.

        The browser never sees or supplies either run identity.  On an
        ordinary recovery-required request the server first persists an
        immutable intent, then creates the exact deterministic run.  If the
        transfer response was lost, the now-healthy control head skips run
        creation and replays the same transfer receipt.  After a browser
        reload, the server resumes an already-created pending run; only a
        run-less or stale pending intent is abandoned and replaced.
        """

        if not isinstance(now, datetime) or now.tzinfo is None:
            raise WebAgentLedgerExceptionFollowupBlocked("恢复任务时间无效")
        actor = _actor(identity, write=True, now=now)
        self._require_available(actor)
        matter = _uuid(matter_id, "案件编号")
        version = _positive_int(expected_matter_version, "案件版本")
        run_key = _child_idempotency_key(idempotency_key, "run")
        transfer_key = _child_idempotency_key(idempotency_key, "transfer")
        requested_replacement_run_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter,
            idempotency_key=run_key,
            entity="run",
        )
        session_id = _uuid(identity.session_id, "服务器登录会话")
        # The immutable intent commits before RUN_CREATED.  Both the wake
        # trigger and the Worker claim treat PENDING as non-runnable, so a
        # crash after either following call cannot create a claimable orphan.
        # The same-key prepare replay returns TRANSFERRED even if the Worker
        # already closed the final ACTIVE follow-up before HTTP delivery.
        intent = self._store.prepare_control_recovery(
            matter_id=matter,
            actor=actor,
            server_session_id=session_id,
            replacement_run_id=requested_replacement_run_id,
            expected_version=version,
            idempotency_key=transfer_key,
        )
        if (
            not isinstance(intent, ExceptionControlRecoveryIntentReceipt)
            or intent.command_name
                != "PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY"
            or intent.idempotency_key != transfer_key
            or intent.matter_id != matter
            or intent.matter_version != version
            or intent.recovery_state not in {
                "PENDING", "TRANSFERRED", "ABANDONED"
            }
        ):
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作恢复意图不一致"
            )
        if intent.recovery_state == "ABANDONED":
            raise WebAgentLedgerExceptionFollowupBlocked(
                "本次恢复请求已被新的恢复请求替代，请刷新后重试"
            )
        replacement_run_id = intent.replacement_run_id
        transfer_key = intent.transfer_idempotency_key
        if intent.recovery_state == "PENDING" and not intent.run_exists:
            if replacement_run_id != requested_replacement_run_id:
                raise WebAgentLedgerExceptionFollowupBlocked(
                    "恢复任务的续办运行标识不一致"
                )
            created = self._recovery_run_service.create_recovery_run(
                identity=identity,
                matter_id=matter,
                replacement_run_id=replacement_run_id,
                expected_matter_version=version,
                idempotency_key=run_key,
                now=now,
            )
            if str(getattr(created, "run_id", "")) != replacement_run_id:
                raise WebAgentLedgerExceptionFollowupBlocked(
                    "恢复任务的受控运行标识不一致"
                )

        receipt = self._store.transfer_control_to_recovery_run(
            matter_id=matter,
            actor=actor,
            server_session_id=session_id,
            replacement_run_id=replacement_run_id,
            expected_version=version,
            idempotency_key=transfer_key,
        )
        if (
            not isinstance(receipt, ExceptionControlTransferReceipt)
            or receipt.command_name != "TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL"
            or receipt.idempotency_key != transfer_key
            or receipt.matter_id != matter
            or receipt.matter_version != version
            or receipt.control_health is not LedgerExceptionControlHealth.HEALTHY
        ):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作恢复回执不一致")
        return WebAgentLedgerExceptionRecoveryReceipt(
            matter_version=receipt.matter_version,
            control_health=receipt.control_health.value,
            recovery_started=True,
        )

    def _exact_followup(
        self, *, matter_id: str, actor: Actor, followup_id: str
    ) -> LedgerExceptionFollowup:
        current = self._store.get_followup(
            matter_id=matter_id,
            actor=actor,
            followup_id=followup_id,
        )
        if not isinstance(current, LedgerExceptionFollowup):
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作投影无效"
            )
        try:
            current.validate()
        except Exception:
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作投影无效"
            ) from None
        if current.matter_id != matter_id or current.followup_id != followup_id:
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作与当前案件不一致"
            )
        return current

    def _project_followup(
        self,
        value: object,
        *,
        actor: Actor,
        current_matter_version: int,
    ) -> WebAgentLedgerExceptionFollowup:
        if not isinstance(value, LedgerExceptionFollowupSnapshot):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作投影无效")
        try:
            value.validate()
        except Exception:
            raise WebAgentLedgerExceptionFollowupBlocked(
                "异常后续工作投影无效"
            ) from None
        if value.state is not LedgerExceptionFollowupState.ACTIVE:
            raise WebAgentLedgerExceptionFollowupBlocked("只允许投影待完成的后续工作")
        review_reasons = tuple(
            _CANONICAL_REASON_LABELS.get(code, f"受控异常原因：{code}")
            for code in value.canonical_reason_codes
        )
        if len(set(review_reasons)) != len(review_reasons):
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作原因重复")
        actions = tuple(_ACTION_POLICY[item] for item in _ACTIONS_BY_KIND[value.kind])
        is_lead = Role.LEAD_LAWYER in actor.roles
        return WebAgentLedgerExceptionFollowup(
            followup_id=value.followup_id,
            kind=value.kind.value,
            state=value.state.value,
            head_sequence=value.head_sequence,
            origin_batch_id=value.origin_extraction_batch_id,
            origin_group_id=value.origin_exception_group_id,
            current_matter_version=current_matter_version,
            created_matter_version=value.created_matter_version,
            created_at=value.created_at,
            reason=_REASON_LABELS.get(value.reason_code, value.reason_code),
            reason_note=value.reason_note,
            candidate_count=value.candidate_count,
            review_reasons=review_reasons,
            evidence_page_count=(
                len(value.evidence_page_ids)
                if value.evidence_page_count is None
                else value.evidence_page_count
            ),
            acceptance_requirements=(
                (
                    "至少选择一份在本次补证请求之后新入卷的受管材料。",
                    "材料必须仍属于本案、未被替代，并由服务器重新核验不可变来源。",
                )
                if value.kind is LedgerExceptionFollowupKind.MORE_EVIDENCE
                else ()
            ),
            automation_status=value.automation_status,
            can_act=is_lead,
            allowed_actions=actions,
        )

    def _current_matter_version(self, *, matter_id: str, actor: Actor) -> int:
        snapshot = self._matter_version_reader.get_case_snapshot(
            matter_id=matter_id,
            actor=actor,
        )
        if str(getattr(snapshot, "matter_id", "")) != matter_id:
            raise WebAgentLedgerExceptionFollowupBlocked("异常后续工作案件不一致")
        return _positive_int(getattr(snapshot, "version", None), "当前案件版本")

    def _require_available(self, actor: Actor) -> None:
        if actor.firm_id not in self._configured_firm_ids:
            raise WebAgentLedgerExceptionFollowupBlocked(
                "当前律所尚未通过异常后续工作数据库预检"
            )


def _actor(
    identity: ServerIdentityContext, *, write: bool, now: datetime | None = None
) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebAgentLedgerExceptionFollowupBlocked("律师身份无效")
    try:
        identity.validate(now=now)
    except Exception:
        raise WebAgentLedgerExceptionFollowupBlocked("律师登录已失效") from None
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebAgentLedgerExceptionFollowupBlocked("该功能只接受已通过 MFA 的律师身份")
    actor = identity.actor
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_HUMAN_ROLES):
        raise WebAgentLedgerExceptionFollowupBlocked("当前身份不能查看异常后续工作")
    if write and Role.LEAD_LAWYER not in actor.roles:
        raise WebAgentLedgerExceptionFollowupBlocked("仅主办律师可以变更异常后续工作")
    return actor


def _page_request(*, offset: int, limit: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise WebAgentLedgerExceptionFollowupBlocked("分页参数无效")


def _child_idempotency_key(value: object, suffix: str) -> str:
    if not isinstance(value, str):
        raise WebAgentLedgerExceptionFollowupBlocked("幂等键无效")
    try:
        # Validate the exact browser key before deriving internal child intents.
        from case_kernel.case_agent_ledger_exception_followup import (
            validate_idempotency_key,
        )

        validate_idempotency_key(value)
    except Exception:
        raise WebAgentLedgerExceptionFollowupBlocked("幂等键无效") from None
    candidate = f"{value}.{suffix}"
    if len(candidate) <= 128:
        return candidate
    return f"ledger-followup-{suffix}-{sha256(value.encode('utf-8')).hexdigest()}"


def _source_refs(
    values: object,
) -> tuple[ManagedEvidenceSourceRef, ...]:
    if not isinstance(values, tuple) or len(values) > 100:
        raise WebAgentLedgerExceptionFollowupBlocked("补证材料选择无效")
    refs: list[ManagedEvidenceSourceRef] = []
    for value in values:
        if not isinstance(value, WebManagedEvidenceSourceSelection):
            raise WebAgentLedgerExceptionFollowupBlocked("补证材料选择无效")
        try:
            ref = ManagedEvidenceSourceRef(
                object_type=ManagedEvidenceSourceType(value.object_type),
                object_id=_uuid(value.object_id, "补证材料编号"),
            )
            ref.validate()
        except (TypeError, ValueError):
            raise WebAgentLedgerExceptionFollowupBlocked("补证材料选择无效") from None
        refs.append(ref)
    canonical = [item.canonical_ref for item in refs]
    if len(set(canonical)) != len(canonical):
        raise WebAgentLedgerExceptionFollowupBlocked("补证材料选择重复")
    return tuple(sorted(refs, key=lambda item: item.canonical_ref))


def _required_note(value: object) -> str:
    if not isinstance(value, str):
        raise WebAgentLedgerExceptionFollowupBlocked("必须说明本次操作依据")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 500
        or len(normalized.encode("utf-8")) > 2_000
        or any(ord(character) < 32 and character not in "\n\t" for character in normalized)
    ):
        raise WebAgentLedgerExceptionFollowupBlocked("操作说明应为 1–500 个有效字符")
    return normalized


def _try_uuid(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _uuid(value: object, label: str) -> str:
    normalized = _try_uuid(value)
    if normalized is None:
        raise WebAgentLedgerExceptionFollowupBlocked(f"{label}无效")
    return normalized


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise WebAgentLedgerExceptionFollowupBlocked(f"{label}无效")
    return value
