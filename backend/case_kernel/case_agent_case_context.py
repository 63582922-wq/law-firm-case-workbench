"""Deterministic, source-bound case-context review candidate.

This module is the first whole-case reasoning Skill that can run without an
external model.  It does not decide facts or law.  It organises the exact
server-authorised case objects selected by a compiled Agent task into a
bounded review candidate: disputes, risks, information gaps, procedural
events, verified legal sources and transactions.

The source projection is deliberately typed and opaque.  A browser or model
cannot supply source references, statuses or content hashes.  Those are
resolved by the production projection port and every emitted item cites at
least one of those exact references.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping
from uuid import UUID

from .case_agent_planner import PlanningInputStatus


CASE_CONTEXT_CANDIDATE_SCHEMA = "agent-case-context-review-candidate-v1"
CASE_CONTEXT_ARTIFACT_KIND = "CASE_CONTEXT_REVIEW_CANDIDATE"
CASE_CONTEXT_REVIEW_STATUS = "NEEDS_LAWYER_REVIEW"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")


class CaseContextReviewBlocked(ValueError):
    """The current task cannot produce a trustworthy review candidate."""


class CaseContextSourceType(StrEnum):
    CASE_FACT = "CASE_FACT"
    CASE_CLAIM = "CASE_CLAIM"
    DISPUTE_ISSUE = "DISPUTE_ISSUE"
    CASE_TRANSACTION = "CASE_TRANSACTION"
    POSTURE_PROFILE = "POSTURE_PROFILE"
    WORK_PLAN_ITEM = "WORK_PLAN_ITEM"
    VERIFIED_LEGAL_SOURCE = "VERIFIED_LEGAL_SOURCE"
    APPROVED_LEGAL_RULE = "APPROVED_LEGAL_RULE"
    PROCEDURAL_EVENT = "PROCEDURAL_EVENT"
    REVIEW_OBLIGATION = "REVIEW_OBLIGATION"
    TRANSACTION_CANDIDATE = "TRANSACTION_CANDIDATE"
    FACT_CANDIDATE = "FACT_CANDIDATE"


class CaseContextSectionId(StrEnum):
    DISPUTES = "DISPUTES"
    RISKS = "RISKS"
    GAPS = "GAPS"
    PROCEDURE = "PROCEDURE"
    LEGAL_SOURCES = "LEGAL_SOURCES"
    TRANSACTIONS = "TRANSACTIONS"
    CASE_CONTEXT = "CASE_CONTEXT"


class CaseContextSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class BoundCaseContextSource:
    """One current, server-resolved object selected by the compiled task.

    ``primary_text`` and ``secondary_text`` are literal, bounded ledger
    projections.  ``signals`` are server-owned codes, never free-form model
    instructions.  The deterministic analyser uses only these typed fields.
    """

    input_ref: str
    source_type: CaseContextSourceType
    object_id: str
    object_version: str
    content_hash: str
    status: PlanningInputStatus
    primary_text: str
    secondary_text: str
    signals: tuple[str, ...] = ()
    confidence: float | None = None

    def validate(self) -> None:
        _ref(self.input_ref, "case-context input_ref")
        _uuid(self.object_id, "case-context object_id")
        _code(self.object_version, "case-context object_version")
        _sha256(self.content_hash, "case-context content_hash")
        if not isinstance(self.source_type, CaseContextSourceType):
            raise CaseContextReviewBlocked("case-context source type is invalid")
        if not isinstance(self.status, PlanningInputStatus):
            raise CaseContextReviewBlocked("case-context source status is invalid")
        _text(self.primary_text, "case-context primary_text", 4_000, allow_empty=False)
        _text(self.secondary_text, "case-context secondary_text", 8_000)
        if len(self.signals) > 20 or tuple(sorted(set(self.signals))) != self.signals:
            raise CaseContextReviewBlocked(
                "case-context source signals must be bounded, sorted and unique"
            )
        for value in self.signals:
            _code(value, "case-context source signal")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise CaseContextReviewBlocked("case-context confidence is invalid")


@dataclass(frozen=True)
class BoundCaseContextProjection:
    run_id: str
    task_id: str
    task_input_hash: str
    firm_id: str
    matter_id: str
    matter_version: int
    case_snapshot_hash: str
    input_refs: tuple[str, ...]
    sources: tuple[BoundCaseContextSource, ...]
    binding_hash: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        firm_id: str,
        matter_id: str,
        matter_version: int,
        case_snapshot_hash: str,
        input_refs: tuple[str, ...],
        sources: Iterable[BoundCaseContextSource],
    ) -> "BoundCaseContextProjection":
        items = tuple(sources)
        payload = _projection_payload(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            firm_id=firm_id,
            matter_id=matter_id,
            matter_version=matter_version,
            case_snapshot_hash=case_snapshot_hash,
            input_refs=input_refs,
            sources=items,
        )
        result = cls(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            firm_id=firm_id,
            matter_id=matter_id,
            matter_version=matter_version,
            case_snapshot_hash=case_snapshot_hash,
            input_refs=input_refs,
            sources=items,
            binding_hash=_canonical_hash(payload),
        )
        result.validate()
        return result

    def validate(self) -> None:
        _uuid(self.run_id, "case-context run_id")
        _uuid(self.task_id, "case-context task_id")
        _sha256(self.task_input_hash, "case-context task_input_hash")
        _uuid(self.firm_id, "case-context firm_id")
        _uuid(self.matter_id, "case-context matter_id")
        if (
            not isinstance(self.matter_version, int)
            or isinstance(self.matter_version, bool)
            or self.matter_version < 1
        ):
            raise CaseContextReviewBlocked("case-context matter version is invalid")
        _sha256(self.case_snapshot_hash, "case-context case_snapshot_hash")
        if not self.input_refs or len(self.input_refs) > 500:
            raise CaseContextReviewBlocked(
                "case-context projection requires 1 to 500 input refs"
            )
        if len(self.input_refs) != len(set(self.input_refs)):
            raise CaseContextReviewBlocked("case-context input refs are duplicated")
        for value in self.input_refs:
            _ref(value, "case-context input_ref")
        if len(self.sources) != len(self.input_refs):
            raise CaseContextReviewBlocked(
                "case-context projection must resolve every input exactly once"
            )
        if tuple(item.input_ref for item in self.sources) != self.input_refs:
            raise CaseContextReviewBlocked(
                "case-context sources differ from compiled input order"
            )
        for item in self.sources:
            item.validate()
        _sha256(self.binding_hash, "case-context binding_hash")
        expected = _canonical_hash(
            _projection_payload(
                run_id=self.run_id,
                task_id=self.task_id,
                task_input_hash=self.task_input_hash,
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                matter_version=self.matter_version,
                case_snapshot_hash=self.case_snapshot_hash,
                input_refs=self.input_refs,
                sources=self.sources,
            )
        )
        if self.binding_hash != expected:
            raise CaseContextReviewBlocked(
                "case-context projection binding hash differs"
            )


_SECTION_TITLES: Mapping[CaseContextSectionId, str] = {
    CaseContextSectionId.DISPUTES: "争议与已否认事项",
    CaseContextSectionId.RISKS: "当前办案风险",
    CaseContextSectionId.GAPS: "待补信息与前置缺口",
    CaseContextSectionId.PROCEDURE: "程序事件",
    CaseContextSectionId.LEGAL_SOURCES: "已核验法源与已批准规则",
    CaseContextSectionId.TRANSACTIONS: "交易核对",
    CaseContextSectionId.CASE_CONTEXT: "已确认案件情境",
}

_SECTION_ORDER = tuple(CaseContextSectionId)
_SECTION_SEVERITY: Mapping[CaseContextSectionId, CaseContextSeverity] = {
    CaseContextSectionId.DISPUTES: CaseContextSeverity.HIGH,
    CaseContextSectionId.RISKS: CaseContextSeverity.HIGH,
    CaseContextSectionId.GAPS: CaseContextSeverity.MEDIUM,
    CaseContextSectionId.PROCEDURE: CaseContextSeverity.MEDIUM,
    CaseContextSectionId.LEGAL_SOURCES: CaseContextSeverity.LOW,
    CaseContextSectionId.TRANSACTIONS: CaseContextSeverity.LOW,
    CaseContextSectionId.CASE_CONTEXT: CaseContextSeverity.LOW,
}


def build_case_context_review_candidate(
    projection: BoundCaseContextProjection,
) -> tuple[bytes, str]:
    """Return canonical review-only JSON and its exact source-set hash."""

    if not isinstance(projection, BoundCaseContextProjection):
        raise CaseContextReviewBlocked("case-context projection is invalid")
    projection.validate()
    source_hash = _canonical_hash(
        {
            "schema_version": "agent-case-context-source-set-v1",
            "case_snapshot_hash": projection.case_snapshot_hash,
            "matter_version": projection.matter_version,
            "sources": [_source_identity(item) for item in projection.sources],
        }
    )
    grouped: dict[CaseContextSectionId, list[dict[str, object]]] = {
        section: [] for section in _SECTION_ORDER
    }
    questions: list[dict[str, object]] = []
    for source in projection.sources:
        section = _section_for(source)
        grouped[section].append(_item_payload(source, section))
        question = _question_payload(source)
        if question is not None:
            questions.append(question)

    sections = [
        {
            "section_id": section.value,
            "title": _SECTION_TITLES[section],
            "severity": _SECTION_SEVERITY[section].value,
            "items": grouped[section],
        }
        for section in _SECTION_ORDER
        if grouped[section]
    ]
    severity_counts = {
        level: sum(
            len(section["items"])
            for section in sections
            if section["severity"] == level
        )
        for level in ("HIGH", "MEDIUM", "LOW")
    }
    headline = (
        f"已核对{len(projection.sources)}项案件对象："
        f"{severity_counts['HIGH']}项高优先、{len(questions)}项待律师确认。"
    )
    payload = {
        "schema_version": CASE_CONTEXT_CANDIDATE_SCHEMA,
        "task_input_hash": projection.task_input_hash,
        "source_hash": source_hash,
        "review_status": CASE_CONTEXT_REVIEW_STATUS,
        "formal_fact": False,
        "formal_transaction": False,
        "legal_conclusion": False,
        "evidence_decision": False,
        "court_ready": False,
        "headline": headline,
        "sections": sections,
        "open_questions": questions,
        "summary_counts": {
            "total_sources": len(projection.sources),
            "total_items": sum(len(section["items"]) for section in sections),
            "high_priority_items": severity_counts["HIGH"],
            "medium_priority_items": severity_counts["MEDIUM"],
            "low_priority_items": severity_counts["LOW"],
            "open_questions": len(questions),
        },
    }
    encoded = _json_bytes(payload)
    if len(encoded) > 4 * 1024 * 1024:
        raise CaseContextReviewBlocked("case-context candidate exceeds hard limit")
    return encoded, source_hash


def _section_for(source: BoundCaseContextSource) -> CaseContextSectionId:
    signals = set(source.signals)
    if source.source_type is CaseContextSourceType.TRANSACTION_CANDIDATE:
        return CaseContextSectionId.TRANSACTIONS
    if source.source_type is CaseContextSourceType.DISPUTE_ISSUE:
        return (
            CaseContextSectionId.GAPS
            if source.status is PlanningInputStatus.REVIEW_REQUIRED
            else CaseContextSectionId.DISPUTES
        )
    if (
        source.status in {PlanningInputStatus.DISPUTED, PlanningInputStatus.BLOCKED}
        or signals.intersection(
            {"DENIED", "DISPUTED", "PARTIALLY_ADMIT", "OUTSIDE_SCOPE"}
        )
    ):
        return CaseContextSectionId.DISPUTES
    if source.source_type is CaseContextSourceType.WORK_PLAN_ITEM:
        if "DEADLINE_RISK" in signals or "REQUIRED_FOR_DELIVERY" in signals:
            return CaseContextSectionId.RISKS
        if source.status in {PlanningInputStatus.OPEN, PlanningInputStatus.BLOCKED}:
            return CaseContextSectionId.GAPS
        return CaseContextSectionId.RISKS
    if source.status in {
        PlanningInputStatus.REVIEW_REQUIRED,
        PlanningInputStatus.OPEN,
    }:
        return CaseContextSectionId.GAPS
    if source.source_type is CaseContextSourceType.PROCEDURAL_EVENT:
        return CaseContextSectionId.PROCEDURE
    if source.source_type in {
        CaseContextSourceType.VERIFIED_LEGAL_SOURCE,
        CaseContextSourceType.APPROVED_LEGAL_RULE,
    }:
        return CaseContextSectionId.LEGAL_SOURCES
    if source.source_type is CaseContextSourceType.CASE_TRANSACTION:
        return CaseContextSectionId.TRANSACTIONS
    return CaseContextSectionId.CASE_CONTEXT


def _item_payload(
    source: BoundCaseContextSource, section: CaseContextSectionId
) -> dict[str, object]:
    payload: dict[str, object] = {
        "item_id": _stable_id("item", source.input_ref, source.content_hash),
        # Truncation can otherwise leave a trailing space exactly at the
        # boundary and produce bytes that the independent verifier correctly
        # rejects as non-canonical display text.
        "title": _display_text(source.primary_text, 500),
        "detail": _display_text(source.secondary_text, 4_000, allow_empty=True),
        "source_refs": [source.input_ref],
        "review_reason": _review_reason(source, section),
    }
    if source.confidence is not None:
        payload["confidence"] = round(float(source.confidence), 5)
    return payload


def _review_reason(
    source: BoundCaseContextSource, section: CaseContextSectionId
) -> str:
    if section is CaseContextSectionId.DISPUTES:
        return "该项存在争议、否认或限定答复，不能作为无争议事实使用。"
    if section is CaseContextSectionId.RISKS:
        return "该项来自当前有效办案计划，遗漏可能影响本案交付或期限。"
    if section is CaseContextSectionId.GAPS:
        return "该项尚未满足正式使用前置条件，需要补充信息或律师确认。"
    if section is CaseContextSectionId.PROCEDURE:
        return "程序日期会影响任务与期限，仅展示已批准事件供律师复核。"
    if source.source_type is CaseContextSourceType.APPROVED_LEGAL_RULE:
        return (
            "该项仅证明规则版本已由律师批准并绑定当前法源，"
            "不代表模型或系统已完成本案适用、金额计算或法律结论。"
        )
    if section is CaseContextSectionId.LEGAL_SOURCES:
        return "该项仅证明法源快照已核验，不代表其当然适用于本案。"
    if section is CaseContextSectionId.TRANSACTIONS:
        return "该项来自交易台账，金额性质、分配和法律效果仍受律师决定约束。"
    return "该项来自当前案件台账，用于核对上下文，不自动形成正式结论。"


def _question_payload(source: BoundCaseContextSource) -> dict[str, object] | None:
    signals = set(source.signals)
    if source.status is PlanningInputStatus.REVIEW_REQUIRED:
        question = f"请确认“{source.primary_text[:120]}”是否可以进入正式案件台账。"
    elif source.source_type is CaseContextSourceType.WORK_PLAN_ITEM and (
        source.status is PlanningInputStatus.OPEN
        or signals.intersection({"NEEDS_INFORMATION", "NEEDS_RESEARCH"})
    ):
        question = f"请处理办案计划缺口：“{source.primary_text[:120]}”。"
    else:
        return None
    return {
        "question_id": _stable_id("question", source.input_ref, source.content_hash),
        "question": question,
        "source_refs": [source.input_ref],
    }


def _display_text(value: str, maximum: int, *, allow_empty: bool = False) -> str:
    result = value[:maximum].strip()
    if result or allow_empty:
        return result
    raise CaseContextReviewBlocked("case-context display text is empty")


def _projection_payload(
    *,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    firm_id: str,
    matter_id: str,
    matter_version: int,
    case_snapshot_hash: str,
    input_refs: tuple[str, ...],
    sources: tuple[BoundCaseContextSource, ...],
) -> dict[str, object]:
    return {
        "schema_version": "agent-case-context-projection-binding-v1",
        "run_id": run_id,
        "task_id": task_id,
        "task_input_hash": task_input_hash,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "matter_version": matter_version,
        "case_snapshot_hash": case_snapshot_hash,
        "input_refs": list(input_refs),
        "sources": [_source_payload(item) for item in sources],
    }


def _source_payload(source: BoundCaseContextSource) -> dict[str, object]:
    return {
        **_source_identity(source),
        "primary_text": source.primary_text,
        "secondary_text": source.secondary_text,
        "signals": list(source.signals),
        "confidence": source.confidence,
    }


def _source_identity(source: BoundCaseContextSource) -> dict[str, object]:
    return {
        "input_ref": source.input_ref,
        "source_type": source.source_type.value,
        "object_id": source.object_id,
        "object_version": source.object_version,
        "content_hash": source.content_hash,
        "status": source.status.value,
    }


def _stable_id(kind: str, input_ref: str, content_hash: str) -> str:
    return f"ctx-{kind}-{_canonical_hash({'kind': kind, 'input_ref': input_ref, 'content_hash': content_hash})[:24]}"


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CaseContextReviewBlocked(
            "case-context candidate is not canonical JSON"
        ) from error


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseContextReviewBlocked(f"{label} must be a UUID") from error


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CaseContextReviewBlocked(f"{label} must be a SHA-256 digest")


def _ref(value: object, label: str) -> None:
    if not isinstance(value, str) or _REF_RE.fullmatch(value) is None:
        raise CaseContextReviewBlocked(f"{label} is invalid")


def _code(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 200 or any(
        character.isspace() for character in value
    ):
        raise CaseContextReviewBlocked(f"{label} is invalid")


def _text(
    value: object,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = True,
) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not allow_empty and not value.strip())
        or any(ord(character) < 32 and character not in "\n\r\t" for character in value)
    ):
        raise CaseContextReviewBlocked(f"{label} is invalid")


__all__ = (
    "BoundCaseContextProjection",
    "BoundCaseContextSource",
    "CASE_CONTEXT_ARTIFACT_KIND",
    "CASE_CONTEXT_CANDIDATE_SCHEMA",
    "CASE_CONTEXT_REVIEW_STATUS",
    "CaseContextReviewBlocked",
    "CaseContextSectionId",
    "CaseContextSeverity",
    "CaseContextSourceType",
    "build_case_context_review_candidate",
)
