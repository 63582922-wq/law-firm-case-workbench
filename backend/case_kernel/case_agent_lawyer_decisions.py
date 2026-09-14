"""Governed lawyer corrections that cause a case Agent to re-plan.

Rejecting an Agent task is not a negative approval flag.  It changes the
planning inputs.  This module defines the small, structured domain object that
is persisted beside the supervisor event and later projected into the next
authoritative planning snapshot.

The browser may choose only one bounded reason code and an optional business
note.  It cannot choose a planning category, status, source references, hash,
Tool, URL, command or model instruction.  Those bindings are derived from the
current compiled task by the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable
from uuid import UUID

from .case_agent_planner import PlanningInputStatus, PlanningSignalCategory


class LawyerPlanningDecisionBlocked(ValueError):
    """A proposed lawyer correction is not safe to persist or re-plan from."""


class LawyerPlanningDecisionCode(StrEnum):
    WRONG_SCOPE = "LAWYER_REJECT_WRONG_SCOPE"
    MISSING_MATERIAL = "LAWYER_REJECT_MISSING_MATERIAL"
    WRONG_FACT_ASSUMPTION = "LAWYER_REJECT_WRONG_FACT_ASSUMPTION"
    WRONG_LEGAL_DIRECTION = "LAWYER_REJECT_WRONG_LEGAL_DIRECTION"
    DUPLICATE_OR_UNNECESSARY = "LAWYER_REJECT_DUPLICATE_OR_UNNECESSARY"
    OTHER = "LAWYER_REJECT_OTHER"


@dataclass(frozen=True)
class LawyerPlanningDecisionPolicy:
    category: PlanningSignalCategory
    status: PlanningInputStatus
    title: str
    note_required: bool = False


_POLICIES: dict[LawyerPlanningDecisionCode, LawyerPlanningDecisionPolicy] = {
    LawyerPlanningDecisionCode.WRONG_SCOPE: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.WORK_PLAN,
        PlanningInputStatus.DISPUTED,
        "律师认为该任务超出或偏离本次办案范围。",
    ),
    LawyerPlanningDecisionCode.MISSING_MATERIAL: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.WORK_PLAN,
        PlanningInputStatus.OPEN,
        "律师认为继续处理前仍缺少必要材料。",
    ),
    LawyerPlanningDecisionCode.WRONG_FACT_ASSUMPTION: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.CONFIRMED_FACT,
        PlanningInputStatus.DISPUTED,
        "律师认为该任务依赖了错误或尚未确认的事实前提。",
        note_required=True,
    ),
    LawyerPlanningDecisionCode.WRONG_LEGAL_DIRECTION: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.LEGAL_GAP,
        PlanningInputStatus.DISPUTED,
        "律师认为该任务的法律研究或适用方向需要调整。",
        note_required=True,
    ),
    LawyerPlanningDecisionCode.DUPLICATE_OR_UNNECESSARY: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.WORK_PLAN,
        PlanningInputStatus.BLOCKED,
        "律师认为该任务重复、当前无必要或不应继续执行。",
    ),
    LawyerPlanningDecisionCode.OTHER: LawyerPlanningDecisionPolicy(
        PlanningSignalCategory.WORK_PLAN,
        PlanningInputStatus.DISPUTED,
        "律师要求按补充说明调整当前计划。",
        note_required=True,
    ),
}


@dataclass(frozen=True)
class GovernedLawyerPlanningDecision:
    signal_id: str
    run_id: str
    firm_id: str
    matter_id: str
    graph_id: str
    task_id: str
    signal_version: int
    decision_code: LawyerPlanningDecisionCode
    category: PlanningSignalCategory
    status: PlanningInputStatus
    summary: str
    source_ref_ids: tuple[str, ...]
    task_input_hash: str
    graph_hash: str
    subject_hash: str
    decision_hash: str
    recorded_event_sequence: int
    recorded_by: str
    decided_at: datetime
    supersedes_signal_id: str | None = None

    @classmethod
    def build(
        cls,
        *,
        signal_id: str,
        run_id: str,
        firm_id: str,
        matter_id: str,
        graph_id: str,
        task_id: str,
        signal_version: int,
        decision_code: LawyerPlanningDecisionCode,
        note: str | None,
        source_ref_ids: Iterable[str],
        task_input_hash: str,
        graph_hash: str,
        recorded_event_sequence: int,
        recorded_by: str,
        decided_at: datetime,
        supersedes_signal_id: str | None = None,
    ) -> "GovernedLawyerPlanningDecision":
        if not isinstance(decision_code, LawyerPlanningDecisionCode):
            raise LawyerPlanningDecisionBlocked("lawyer decision code is invalid")
        policy = _POLICIES[decision_code]
        normalized_note = _normalize_note(note, required=policy.note_required)
        summary = policy.title if normalized_note is None else f"{policy.title} 补充说明：{normalized_note}"
        normalized_refs = tuple(sorted(set(source_ref_ids)))
        subject_hash = _canonical_hash(
            {
                "schema_version": "case-agent-lawyer-decision-subject-v1",
                "matter_id": matter_id,
                "category": policy.category.value,
                "source_ref_ids": list(normalized_refs),
            }
        )
        payload = {
            "schema_version": "case-agent-lawyer-planning-decision-v1",
            "signal_id": signal_id,
            "run_id": run_id,
            "firm_id": firm_id,
            "matter_id": matter_id,
            "graph_id": graph_id,
            "task_id": task_id,
            "signal_version": signal_version,
            "decision_code": decision_code.value,
            "category": policy.category.value,
            "status": policy.status.value,
            "summary": summary,
            "source_ref_ids": list(normalized_refs),
            "task_input_hash": task_input_hash,
            "graph_hash": graph_hash,
            "subject_hash": subject_hash,
            "recorded_event_sequence": recorded_event_sequence,
            "recorded_by": recorded_by,
            "decided_at": _time_text(decided_at),
            "supersedes_signal_id": supersedes_signal_id,
        }
        result = cls(
            signal_id=signal_id,
            run_id=run_id,
            firm_id=firm_id,
            matter_id=matter_id,
            graph_id=graph_id,
            task_id=task_id,
            signal_version=signal_version,
            decision_code=decision_code,
            category=policy.category,
            status=policy.status,
            summary=summary,
            source_ref_ids=normalized_refs,
            task_input_hash=task_input_hash,
            graph_hash=graph_hash,
            subject_hash=subject_hash,
            decision_hash=_canonical_hash(payload),
            recorded_event_sequence=recorded_event_sequence,
            recorded_by=recorded_by,
            decided_at=decided_at,
            supersedes_signal_id=supersedes_signal_id,
        )
        result.validate()
        return result

    def validate(self) -> None:
        for value, label in (
            (self.signal_id, "signal_id"),
            (self.run_id, "run_id"),
            (self.firm_id, "firm_id"),
            (self.matter_id, "matter_id"),
            (self.graph_id, "graph_id"),
            (self.task_id, "task_id"),
            (self.recorded_by, "recorded_by"),
        ):
            _uuid(value, label)
        if self.supersedes_signal_id is not None:
            _uuid(self.supersedes_signal_id, "supersedes_signal_id")
            if self.supersedes_signal_id == self.signal_id:
                raise LawyerPlanningDecisionBlocked("a lawyer decision cannot supersede itself")
        if type(self.signal_version) is not int or self.signal_version < 1:
            raise LawyerPlanningDecisionBlocked("signal_version must be positive")
        if type(self.recorded_event_sequence) is not int or self.recorded_event_sequence < 2:
            raise LawyerPlanningDecisionBlocked("recorded event sequence is invalid")
        if not isinstance(self.decision_code, LawyerPlanningDecisionCode):
            raise LawyerPlanningDecisionBlocked("lawyer decision code is invalid")
        policy = _POLICIES[self.decision_code]
        if self.category is not policy.category or self.status is not policy.status:
            raise LawyerPlanningDecisionBlocked("lawyer decision policy binding differs")
        _business_text(self.summary, "lawyer decision summary", maximum=1_000)
        if not self.source_ref_ids or len(self.source_ref_ids) > 100:
            raise LawyerPlanningDecisionBlocked("lawyer decision requires bounded task sources")
        if tuple(sorted(set(self.source_ref_ids))) != self.source_ref_ids:
            raise LawyerPlanningDecisionBlocked("lawyer decision sources are not canonical")
        for value in self.source_ref_ids:
            _source_ref(value)
        for value, label in (
            (self.task_input_hash, "task_input_hash"),
            (self.graph_hash, "graph_hash"),
            (self.subject_hash, "subject_hash"),
            (self.decision_hash, "decision_hash"),
        ):
            _sha256(value, label)
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise LawyerPlanningDecisionBlocked("decided_at must include a timezone")
        expected_subject = _canonical_hash(
            {
                "schema_version": "case-agent-lawyer-decision-subject-v1",
                "matter_id": self.matter_id,
                "category": self.category.value,
                "source_ref_ids": list(self.source_ref_ids),
            }
        )
        if self.subject_hash != expected_subject:
            raise LawyerPlanningDecisionBlocked("lawyer decision subject hash differs")
        payload = {
            "schema_version": "case-agent-lawyer-planning-decision-v1",
            "signal_id": self.signal_id,
            "run_id": self.run_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "graph_id": self.graph_id,
            "task_id": self.task_id,
            "signal_version": self.signal_version,
            "decision_code": self.decision_code.value,
            "category": self.category.value,
            "status": self.status.value,
            "summary": self.summary,
            "source_ref_ids": list(self.source_ref_ids),
            "task_input_hash": self.task_input_hash,
            "graph_hash": self.graph_hash,
            "subject_hash": self.subject_hash,
            "recorded_event_sequence": self.recorded_event_sequence,
            "recorded_by": self.recorded_by,
            "decided_at": _time_text(self.decided_at),
            "supersedes_signal_id": self.supersedes_signal_id,
        }
        if self.decision_hash != _canonical_hash(payload):
            raise LawyerPlanningDecisionBlocked("lawyer decision hash differs")


def lawyer_planning_decision_options() -> tuple[tuple[str, str, bool], ...]:
    """Browser-safe labels; categories and statuses remain server-owned."""

    return tuple(
        (code.value, policy.title, policy.note_required)
        for code, policy in _POLICIES.items()
    )


def lawyer_planning_decision_subject_hash(
    *,
    matter_id: str,
    decision_code: LawyerPlanningDecisionCode,
    source_ref_ids: Iterable[str],
) -> str:
    """Compute the server-owned version stream for one correction subject."""

    _uuid(matter_id, "matter_id")
    if not isinstance(decision_code, LawyerPlanningDecisionCode):
        raise LawyerPlanningDecisionBlocked("lawyer decision code is invalid")
    normalized_refs = tuple(sorted(set(source_ref_ids)))
    if not normalized_refs or len(normalized_refs) > 100:
        raise LawyerPlanningDecisionBlocked("lawyer decision requires bounded task sources")
    for value in normalized_refs:
        _source_ref(value)
    return _canonical_hash(
        {
            "schema_version": "case-agent-lawyer-decision-subject-v1",
            "matter_id": matter_id,
            "category": _POLICIES[decision_code].category.value,
            "source_ref_ids": list(normalized_refs),
        }
    )


def _normalize_note(value: str | None, *, required: bool) -> str | None:
    if value is None or not value.strip():
        if required:
            raise LawyerPlanningDecisionBlocked("this lawyer decision requires a note")
        return None
    normalized = " ".join(value.split())
    _business_text(normalized, "lawyer decision note", maximum=600)
    return normalized


def _source_ref(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,63}:[0-9a-f-]{36}", value
    ):
        raise LawyerPlanningDecisionBlocked("lawyer decision source reference is invalid")
    try:
        UUID(value.rsplit(":", 1)[1])
    except ValueError as error:
        raise LawyerPlanningDecisionBlocked("lawyer decision source UUID is invalid") from error


def _uuid(value: str, label: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise LawyerPlanningDecisionBlocked(f"{label} must be a UUID") from error
    if str(parsed) != value:
        raise LawyerPlanningDecisionBlocked(f"{label} must be canonical")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LawyerPlanningDecisionBlocked(f"{label} must be a lowercase SHA-256")


def _business_text(value: str, label: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= maximum
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise LawyerPlanningDecisionBlocked(f"{label} is invalid")


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


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LawyerPlanningDecisionBlocked("decision time must include a timezone")
    return value.isoformat()


__all__ = [
    "GovernedLawyerPlanningDecision",
    "LawyerPlanningDecisionBlocked",
    "LawyerPlanningDecisionCode",
    "LawyerPlanningDecisionPolicy",
    "lawyer_planning_decision_subject_hash",
    "lawyer_planning_decision_options",
]
