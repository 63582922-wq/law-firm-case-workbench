"""Browser-safe evidence review adapter for the self-hosted Web workbench.

The PostgreSQL evidence ledger is intentionally richer than a browser API.
This adapter is the narrow projection between them: it exposes page labels,
page numbers, review decisions and normalized red-box coordinates, while
keeping source hashes, audit identities, object locators and internal
approval hashes out of the browser contract.

Write operations are two-step.  A browser creates a candidate and then sends
an explicit confirmation command.  The confirmation fingerprint is produced
by this server from the authenticated session and the exact candidate/version;
the browser cannot choose an arbitrary approval hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Protocol
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_manifest import PageDisposition
from case_kernel.evidence_manifest_postgres import (
    AgentEvidenceDecisionCandidateStaging,
    PersistentEvidencePageListPage,
    PersistentEvidenceReviewSummary,
    evidence_page_decision_batch_hash,
)
from case_kernel.models import Actor, Role


__all__ = (
    "WebEvidenceReviewBlocked",
    "WebEvidenceReviewPage",
    "WebEvidenceReviewService",
)


_HUMAN_ROLES = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER})
_WRITE_ROLES = frozenset({Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_MAX_CURSOR = 1_024


class WebEvidenceReviewBlocked(ValueError):
    """A page-review operation cannot safely cross the Web boundary."""


class _EvidenceStore(Protocol):
    def get_evidence_review_summary(self, *, matter_id: str, actor: Actor) -> PersistentEvidenceReviewSummary: ...

    def list_evidence_page(
        self,
        *,
        matter_id: str,
        actor: Actor,
        limit: int,
        cursor: str | None,
        expected_version: int | None = None,
    ) -> PersistentEvidencePageListPage: ...

    def create_page_decision_candidate(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def approve_page_decision(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def approve_page_decisions_batch(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def stage_low_risk_agent_page_decision_candidates(
        self, **kwargs: Any
    ) -> AgentEvidenceDecisionCandidateStaging: ...

    def create_annotation_candidate(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def approve_annotation(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class WebEvidenceReviewPage:
    matter_id: str
    matter_version: int
    total_count: int
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool


class WebEvidenceReviewService:
    """Project and command the immutable evidence ledger for a Web lawyer."""

    def __init__(self, *, evidence_store: _EvidenceStore) -> None:
        for method in (
            "get_evidence_review_summary",
            "list_evidence_page",
            "create_page_decision_candidate",
            "approve_page_decision",
            "approve_page_decisions_batch",
            "stage_low_risk_agent_page_decision_candidates",
            "create_annotation_candidate",
            "approve_annotation",
            "lock_manifest",
            "enqueue_derivative_run",
        ):
            if not callable(getattr(evidence_store, method, None)):
                raise ValueError("Web evidence review store is invalid")
        self._evidence_store = evidence_store

    def summary(self, *, identity: ServerIdentityContext, matter_id: str) -> dict[str, Any]:
        actor = _require_identity(identity, write=False)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        summary = self._evidence_store.get_evidence_review_summary(
            matter_id=normalized_matter,
            actor=actor,
        )
        if not isinstance(summary, PersistentEvidenceReviewSummary):
            raise WebEvidenceReviewBlocked("证据摘要格式无效")
        projection = _summary_projection(summary)
        projection["can_batch_confirm_page_decisions"] = Role.LEAD_LAWYER in actor.roles
        return projection

    def pages(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        limit: int = 50,
        cursor: str | None = None,
        expected_version: int | None = None,
    ) -> WebEvidenceReviewPage:
        actor = _require_identity(identity, write=False)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WebEvidenceReviewBlocked("页面数量超出受控范围")
        if cursor is not None and (not isinstance(cursor, str) or not 20 <= len(cursor) <= _MAX_CURSOR):
            raise WebEvidenceReviewBlocked("页面游标无效")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise WebEvidenceReviewBlocked("案件版本无效")
        page = self._evidence_store.list_evidence_page(
            matter_id=normalized_matter,
            actor=actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        if not isinstance(page, PersistentEvidencePageListPage):
            raise WebEvidenceReviewBlocked("证据页面列表格式无效")
        return WebEvidenceReviewPage(
            matter_id=page.matter_id,
            matter_version=page.matter_version,
            total_count=page.total_count,
            items=tuple(_page_projection(item) for item in page.items),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def create_page_decision_candidate(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        evidence_page_id: str,
        expected_version: int,
        disposition: str,
        reason: str,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        disposition_value = _validate_disposition(disposition)
        return self._evidence_store.create_page_decision_candidate(
            matter_id=_validate_uuid(matter_id, "案件编号"),
            evidence_page_id=_validate_uuid(evidence_page_id, "证据页面编号"),
            actor=actor,
            expected_version=_validate_version(expected_version),
            idempotency_key=_validate_idempotency_key(idempotency_key),
            disposition=disposition_value,
            reason=_validate_reason(reason),
        )

    def confirm_page_decision(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        decision_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        normalized_decision = _validate_uuid(decision_id, "决定编号")
        version = _validate_version(expected_version)
        return self._evidence_store.approve_page_decision(
            matter_id=normalized_matter,
            decision_id=normalized_decision,
            actor=actor,
            expected_version=version,
            idempotency_key=_validate_idempotency_key(idempotency_key),
            approval_hash=_confirmation_fingerprint(
                action="EVIDENCE_PAGE_DECISION_CONFIRM",
                identity=identity,
                matter_id=normalized_matter,
                object_id=normalized_decision,
                expected_version=version,
            ),
        )

    def confirm_page_decisions_batch(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        decision_ids: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_batch_approval_identity(identity)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        normalized_decisions = _validate_decision_batch(decision_ids)
        version = _validate_version(expected_version)
        batch_hash = evidence_page_decision_batch_hash(
            matter_id=normalized_matter,
            expected_version=version,
            decision_ids=normalized_decisions,
        )
        return self._evidence_store.approve_page_decisions_batch(
            matter_id=normalized_matter,
            decision_ids=normalized_decisions,
            actor=actor,
            expected_version=version,
            idempotency_key=_validate_idempotency_key(idempotency_key),
            batch_hash=batch_hash,
            approval_hash=_confirmation_fingerprint(
                action="EVIDENCE_PAGE_DECISIONS_BATCH_CONFIRM",
                identity=identity,
                matter_id=normalized_matter,
                object_id=batch_hash,
                expected_version=version,
            ),
        )

    def stage_agent_page_decision_candidates(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Adopt only server-selected low-risk suggestions as candidates.

        This does not approve evidence.  The browser cannot send candidate ids,
        page ids, dispositions, confidence, hashes, or thresholds.
        """

        actor = _require_identity(identity, write=True)
        result = self._evidence_store.stage_low_risk_agent_page_decision_candidates(
            matter_id=_validate_uuid(matter_id, "案件编号"),
            run_id=_validate_uuid(run_id, "Agent任务编号"),
            actor=actor,
            expected_version=_validate_version(expected_version),
            idempotency_key=_validate_idempotency_key(idempotency_key),
        )
        if not isinstance(result, AgentEvidenceDecisionCandidateStaging):
            raise WebEvidenceReviewBlocked("Agent证据候选转换结果无效")
        return {
            "receipt": result.receipt,
            "run_id": result.run_id,
            "decision_ids": result.decision_ids,
            "page_ids": result.page_ids,
            "include_count": result.include_count,
            "exclude_count": result.exclude_count,
            "excluded": tuple(
                {
                    "category": item.category,
                    "page_ids": item.page_ids,
                    "count": len(item.page_ids),
                }
                for item in result.exclusions
            ),
        }

    def create_annotation_candidate(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        evidence_page_id: str,
        expected_version: int,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        label: str,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        coordinates = _validate_box(x0, y0, x1, y1)
        return self._evidence_store.create_annotation_candidate(
            matter_id=_validate_uuid(matter_id, "案件编号"),
            evidence_page_id=_validate_uuid(evidence_page_id, "证据页面编号"),
            actor=actor,
            expected_version=_validate_version(expected_version),
            idempotency_key=_validate_idempotency_key(idempotency_key),
            x0=coordinates[0], y0=coordinates[1], x1=coordinates[2], y1=coordinates[3],
            label=_validate_reason(label, label_name="红框说明"),
        )

    def confirm_annotation(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        annotation_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        normalized_annotation = _validate_uuid(annotation_id, "红框编号")
        version = _validate_version(expected_version)
        return self._evidence_store.approve_annotation(
            matter_id=normalized_matter,
            annotation_id=normalized_annotation,
            actor=actor,
            expected_version=version,
            idempotency_key=_validate_idempotency_key(idempotency_key),
            approval_hash=_confirmation_fingerprint(
                action="EVIDENCE_ANNOTATION_CONFIRM",
                identity=identity,
                matter_id=normalized_matter,
                object_id=normalized_annotation,
                expected_version=version,
            ),
        )

    def lock_manifest(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        readiness_hash: str,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        version = _validate_version(expected_version)
        if not isinstance(readiness_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", readiness_hash):
            raise WebEvidenceReviewBlocked("证据清单就绪指纹无效")
        normalized_idempotency = _validate_idempotency_key(idempotency_key)
        return self._evidence_store.lock_manifest(
            matter_id=normalized_matter,
            actor=actor,
            expected_version=version,
            idempotency_key=normalized_idempotency,
            readiness_hash=readiness_hash,
            approval_hash=_confirmation_fingerprint(
                action="EVIDENCE_MANIFEST_LOCK",
                identity=identity,
                matter_id=normalized_matter,
                object_id=readiness_hash,
                expected_version=version,
            ),
        )

    def enqueue_derivative_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        manifest_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        actor = _require_identity(identity, write=True)
        normalized_matter = _validate_uuid(matter_id, "案件编号")
        normalized_manifest = _validate_uuid(manifest_id, "证据清单编号")
        version = _validate_version(expected_version)
        summary = self._evidence_store.get_evidence_review_summary(matter_id=normalized_matter, actor=actor)
        if not isinstance(summary, PersistentEvidenceReviewSummary) or summary.version != version:
            raise WebEvidenceReviewBlocked("案件版本已变化，请刷新证据清单")
        locked = summary.locked_manifest
        if not isinstance(locked, dict) or str(locked.get("manifest_id")) != normalized_manifest:
            raise WebEvidenceReviewBlocked("当前证据清单尚未锁定或已失效")
        content_hash = locked.get("content_hash")
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise WebEvidenceReviewBlocked("当前证据清单指纹不可用")
        return self._evidence_store.enqueue_derivative_run(
            matter_id=normalized_matter,
            manifest_id=normalized_manifest,
            actor=actor,
            expected_version=version,
            idempotency_key=_validate_idempotency_key(idempotency_key),
            manifest_content_hash=content_hash,
            approval_hash=_confirmation_fingerprint(
                action="EVIDENCE_DERIVATIVE_RUN_ENQUEUE",
                identity=identity,
                matter_id=normalized_matter,
                object_id=normalized_manifest,
                expected_version=version,
            ),
        )


def _require_identity(identity: ServerIdentityContext, *, write: bool) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebEvidenceReviewBlocked("登录身份无效")
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebEvidenceReviewBlocked("证据审阅需要多因素登录")
    try:
        identity.validate()
    except Exception:
        raise WebEvidenceReviewBlocked("登录身份已失效") from None
    actor = identity.actor
    if not isinstance(actor, Actor) or Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_HUMAN_ROLES):
        raise WebEvidenceReviewBlocked("当前角色不能审阅证据页面")
    if write and not actor.roles.intersection(_WRITE_ROLES):
        raise WebEvidenceReviewBlocked("当前角色不能修改证据页面决定")
    return actor


def _require_batch_approval_identity(identity: ServerIdentityContext) -> Actor:
    actor = _require_identity(identity, write=True)
    if Role.LEAD_LAWYER not in actor.roles:
        raise WebEvidenceReviewBlocked("只有主办律师可以批量确认页面决定")
    return actor


def _validate_uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebEvidenceReviewBlocked(f"{label}格式无效") from None


def _validate_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise WebEvidenceReviewBlocked("案件版本无效")
    return value


def _validate_decision_batch(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 100:
        raise WebEvidenceReviewBlocked("批量确认须包含 1 至 100 个待确认决定")
    normalized = tuple(_validate_uuid(item, "决定编号") for item in value)
    if len(set(normalized)) != len(normalized):
        raise WebEvidenceReviewBlocked("批量确认不能包含重复决定")
    return tuple(sorted(normalized))


def _validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise WebEvidenceReviewBlocked("幂等请求编号无效")
    return value


def _validate_reason(value: object, *, label_name: str = "决定理由") -> str:
    if not isinstance(value, str):
        raise WebEvidenceReviewBlocked(f"{label_name}无效")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 2_000 or any(ord(character) < 32 for character in normalized):
        raise WebEvidenceReviewBlocked(f"{label_name}无效")
    return normalized


def _validate_disposition(value: object) -> PageDisposition:
    if not isinstance(value, str):
        raise WebEvidenceReviewBlocked("页面处理方式无效")
    try:
        return PageDisposition(value)
    except ValueError:
        raise WebEvidenceReviewBlocked("页面处理方式无效") from None


def _validate_box(x0: object, y0: object, x1: object, y1: object) -> tuple[float, float, float, float]:
    values = (x0, y0, x1, y1)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        raise WebEvidenceReviewBlocked("红框坐标无效")
    normalized = tuple(float(value) for value in values)
    if not 0 <= normalized[0] < normalized[2] <= 1 or not 0 <= normalized[1] < normalized[3] <= 1:
        raise WebEvidenceReviewBlocked("红框坐标无效")
    return normalized


def _confirmation_fingerprint(
    *, action: str, identity: ServerIdentityContext, matter_id: str, object_id: str, expected_version: int
) -> str:
    payload = {
        "schema": "web-evidence-confirmation-v1",
        "action": action,
        "actor_id": identity.actor.actor_id,
        "firm_id": identity.actor.firm_id,
        "session_id": identity.session_id,
        "matter_id": matter_id,
        "object_id": object_id,
        "expected_version": expected_version,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _summary_projection(summary: PersistentEvidenceReviewSummary) -> dict[str, Any]:
    return {
        "matter_id": summary.matter_id,
        "matter_version": summary.version,
        "summary_hash": summary.summary_hash,
        "manifest_readiness_hash": summary.manifest_readiness_hash,
        "total_pages": summary.total_pages,
        "unresolved_page_count": summary.unresolved_page_count,
        "pending_decision_count": summary.pending_decision_count,
        "unresolved_duplicate_count": summary.unresolved_duplicate_count,
        "original_files": tuple(
            {
                "evidence_file_id": str(item["evidence_file_id"]),
                "original_label": str(item["original_label"]),
                "byte_size": int(item["byte_size"]),
                "media_type": str(item["media_type"]),
                "page_count": int(item["page_count"]),
            }
            for item in summary.original_files
        ),
        "duplicate_groups": tuple(_duplicate_projection(item) for item in summary.duplicate_groups),
        "locked_manifest": _manifest_projection(summary.locked_manifest),
        "derivatives": tuple(_derivative_projection(item) for item in summary.derivatives),
        "derivative_runs": tuple(_derivative_run_projection(item) for item in summary.derivative_runs),
    }


def _page_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_page_id": _validate_uuid(item.get("evidence_page_id"), "证据页面编号"),
        "evidence_file_id": _validate_uuid(item.get("evidence_file_id"), "证据文件编号"),
        "original_label": str(item.get("original_label", ""))[:500],
        "page_number": int(item.get("page_number", 0)),
        "decision": _decision_projection(item.get("decision")),
        "pending_decision": _decision_projection(item.get("pending_decision")),
        "annotations": tuple(_annotation_projection(annotation) for annotation in item.get("annotations", ())),
    }


def _decision_projection(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("页面决定格式无效")
    return {
        "decision_id": _validate_uuid(item.get("decision_id"), "页面决定编号"),
        "disposition": str(item.get("disposition", "")),
        "reason": str(item.get("reason", ""))[:2_000],
        "status": str(item.get("status", "")),
    }


def _annotation_projection(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("红框格式无效")
    try:
        coordinates = _validate_box(
            _coordinate_number(item["x0"]),
            _coordinate_number(item["y0"]),
            _coordinate_number(item["x1"]),
            _coordinate_number(item["y1"]),
        )
    except (KeyError, TypeError, ValueError):
        raise WebEvidenceReviewBlocked("红框格式无效") from None
    return {
        "annotation_id": _validate_uuid(item.get("annotation_id"), "红框编号"),
        "purpose": str(item.get("purpose", "")),
        "x0": coordinates[0], "y0": coordinates[1], "x1": coordinates[2], "y1": coordinates[3],
        "label": str(item.get("label", ""))[:500],
        "status": str(item.get("status", "")),
    }


def _coordinate_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("coordinate is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("coordinate is not numeric") from None
    if number != number or number in {float("inf"), float("-inf")}:
        raise ValueError("coordinate is not finite")
    return number


def _duplicate_projection(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("重复页信息格式无效")
    members = item.get("members", ())
    if not isinstance(members, (list, tuple)):
        raise WebEvidenceReviewBlocked("重复页信息格式无效")
    return {
        "duplicate_group_id": _validate_uuid(item.get("duplicate_group_id"), "重复组编号"),
        "status": str(item.get("status", "")),
        "canonical_page_id": _validate_uuid(item["canonical_page_id"], "重复组页面编号") if item.get("canonical_page_id") else None,
        "members": tuple(_validate_uuid(member.get("evidence_page_id"), "重复组页面编号") if isinstance(member, dict) else _validate_uuid(member, "重复组页面编号") for member in members),
    }


def _manifest_projection(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("证据清单状态格式无效")
    return {
        "manifest_id": _validate_uuid(item.get("manifest_id"), "证据清单编号"),
        "status": str(item.get("status", "")),
        "total_pages": int(item.get("total_pages", 0)),
        "included_pages": int(item.get("included_pages", 0)),
        "excluded_pages": int(item.get("excluded_pages", 0)),
    }


def _derivative_projection(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("派生件状态格式无效")
    return {
        "derivative_id": _validate_uuid(item.get("derivative_id"), "派生件编号"),
        "artifact_type": str(item.get("artifact_type", "")),
        "page_count": int(item.get("page_count", 0)),
        "status": str(item.get("status", "")),
    }


def _derivative_run_projection(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise WebEvidenceReviewBlocked("派生任务状态格式无效")
    return {
        "run_id": _validate_uuid(item.get("run_id"), "派生任务编号"),
        "manifest_id": _validate_uuid(item.get("manifest_id"), "证据清单编号"),
        "status": str(item.get("status", "")),
        "attempt_count": int(item.get("attempt_count", 0)),
        "failure_code": str(item["failure_code"]) if item.get("failure_code") else None,
    }
