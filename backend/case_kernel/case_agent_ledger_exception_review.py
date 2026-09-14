"""Server-owned grouping and terminal routing for 0042 exception candidates.

An exception disposition is deliberately not a fact, transaction, evidence
decision or legal conclusion.  It only routes an immutable extraction group
to a bounded next action.  Group membership is derived from stored candidate
metadata; a browser may name the opaque group id but can never select members
or submit candidate hashes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Iterable


class LedgerExceptionReviewBlocked(ValueError):
    """An exception group or disposition violates the governed review policy."""


class LedgerExceptionReextractionSourceWindowExceeded(
    LedgerExceptionReviewBlocked
):
    """One re-extraction group exceeds the governed 64-page source window."""


class LedgerExceptionReextractionCohortCapacityExceeded(
    LedgerExceptionReviewBlocked
):
    """The matter already has the governed maximum of active source cohorts."""


class LedgerExceptionDecision(str, Enum):
    REJECT_AS_DUPLICATE = "REJECT_AS_DUPLICATE"
    REQUEST_REEXTRACTION = "REQUEST_REEXTRACTION"
    REQUEST_MORE_EVIDENCE = "REQUEST_MORE_EVIDENCE"
    DEFER_WITH_REASON = "DEFER_WITH_REASON"


class LedgerExceptionReason(str, Enum):
    DUPLICATE_CONFIRMED = "DUPLICATE_CONFIRMED"
    SOURCE_QUALITY_INSUFFICIENT = "SOURCE_QUALITY_INSUFFICIENT"
    EXTRACTION_CONFLICT = "EXTRACTION_CONFLICT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    PARTY_DATE_AMOUNT_UNCLEAR = "PARTY_DATE_AMOUNT_UNCLEAR"
    AWAITING_CLIENT_INPUT = "AWAITING_CLIENT_INPUT"
    AWAITING_EXTERNAL_RECORD = "AWAITING_EXTERNAL_RECORD"
    NEEDS_LEAD_REVIEW = "NEEDS_LEAD_REVIEW"


class LedgerExceptionSourcePolicy(str, Enum):
    NATIVE_SOURCE_REVIEW = "NATIVE_SOURCE_REVIEW"
    SOURCE_REVERIFICATION_REQUIRED = "SOURCE_REVERIFICATION_REQUIRED"


class LedgerExceptionRiskPolicy(str, Enum):
    DUPLICATE_REVIEW = "DUPLICATE_REVIEW"
    LEGAL_OR_LEDGER_CONFLICT_REVIEW = "LEGAL_OR_LEDGER_CONFLICT_REVIEW"
    MISSING_FIELDS_OR_AMBIGUITY_REVIEW = "MISSING_FIELDS_OR_AMBIGUITY_REVIEW"
    LOW_CONFIDENCE_REVIEW = "LOW_CONFIDENCE_REVIEW"


class LedgerLowRiskLaneStatus(str, Enum):
    EMPTY = "EMPTY"
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"


class LedgerExtractionBatchReviewStatus(str, Enum):
    LOW_RISK_OPEN = "LOW_RISK_OPEN"
    EXCEPTIONS_OPEN = "EXCEPTIONS_OPEN"
    LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN = "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN"
    RESOLVED = "RESOLVED"


_SOURCE_REVERIFY_CODES = frozenset(
    {
        "OCR_DERIVED",
        "UNTRUSTED_TEXT",
        "NON_NATIVE_SOURCE",
        "SOURCE_TEXT_NOT_REVERIFIED",
    }
)
_DUPLICATE_CODES = frozenset(
    {"POSSIBLE_DUPLICATE", "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE"}
)
_LEGAL_OR_CONFLICT_CODES = frozenset(
    {
        "CROSS_PAGE_CONFLICT",
        "CONTRADICTS_CASE_LEDGER",
        "LEGAL_CONCLUSION_RISK",
        "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE",
    }
)
_AMBIGUITY_CODES = frozenset(
    {
        "PARTY_AMBIGUOUS",
        "DATE_AMBIGUOUS",
        "AMOUNT_AMBIGUOUS",
        "INCOMPLETE_TRANSACTION",
    }
)

_ALLOWED_REASONS = {
    LedgerExceptionDecision.REJECT_AS_DUPLICATE: frozenset(
        {LedgerExceptionReason.DUPLICATE_CONFIRMED}
    ),
    LedgerExceptionDecision.REQUEST_REEXTRACTION: frozenset(
        {
            LedgerExceptionReason.SOURCE_QUALITY_INSUFFICIENT,
            LedgerExceptionReason.EXTRACTION_CONFLICT,
        }
    ),
    LedgerExceptionDecision.REQUEST_MORE_EVIDENCE: frozenset(
        {
            LedgerExceptionReason.EVIDENCE_GAP,
            LedgerExceptionReason.PARTY_DATE_AMOUNT_UNCLEAR,
        }
    ),
    LedgerExceptionDecision.DEFER_WITH_REASON: frozenset(
        {
            LedgerExceptionReason.AWAITING_CLIENT_INPUT,
            LedgerExceptionReason.AWAITING_EXTERNAL_RECORD,
            LedgerExceptionReason.NEEDS_LEAD_REVIEW,
        }
    ),
}


@dataclass(frozen=True)
class LedgerExceptionGroup:
    group_id: str
    extraction_batch_id: str
    candidate_kind: str
    reason_codes: tuple[str, ...]
    source_policy: LedgerExceptionSourcePolicy
    risk_policy: LedgerExceptionRiskPolicy
    candidate_count: int
    summary: str
    allowed_decisions: tuple[LedgerExceptionDecision, ...]
    decision: LedgerExceptionDecision | None = None
    decision_reason: LedgerExceptionReason | None = None


@dataclass(frozen=True)
class LedgerExceptionMemberExcerpt:
    evidence_page_id: str
    page_number: int
    text: str


@dataclass(frozen=True)
class LedgerExceptionGroupMember:
    sequence: int
    candidate_kind: str
    summary: str
    confidence: float
    reason_codes: tuple[str, ...]
    excerpts: tuple[LedgerExceptionMemberExcerpt, ...]
    extraction_candidate_id: str | None = None


@dataclass(frozen=True)
class LedgerExceptionGroupMemberPage:
    group_id: str
    total_count: int
    offset: int
    next_offset: int | None
    members: tuple[LedgerExceptionGroupMember, ...]


@dataclass(frozen=True)
class LedgerExceptionBatchState:
    extraction_batch_id: str
    run_id: str
    matter_id: str
    matter_version: int
    low_risk_lane_status: LedgerLowRiskLaneStatus
    batch_status: LedgerExtractionBatchReviewStatus
    exception_group_count: int
    decided_exception_group_count: int


def canonical_reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if not result or len(result) > 15 or any(
        not isinstance(value, str) or not value for value in result
    ):
        raise LedgerExceptionReviewBlocked("exception reason codes are invalid")
    return result


def exception_source_policy(
    reason_codes: Iterable[str],
) -> LedgerExceptionSourcePolicy:
    reasons = frozenset(canonical_reason_codes(reason_codes))
    if reasons & _SOURCE_REVERIFY_CODES:
        return LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
    return LedgerExceptionSourcePolicy.NATIVE_SOURCE_REVIEW


def exception_risk_policy(
    reason_codes: Iterable[str],
) -> LedgerExceptionRiskPolicy:
    reasons = frozenset(canonical_reason_codes(reason_codes))
    if reasons & _DUPLICATE_CODES:
        return LedgerExceptionRiskPolicy.DUPLICATE_REVIEW
    if reasons & _LEGAL_OR_CONFLICT_CODES:
        return LedgerExceptionRiskPolicy.LEGAL_OR_LEDGER_CONFLICT_REVIEW
    if reasons & _AMBIGUITY_CODES:
        return LedgerExceptionRiskPolicy.MISSING_FIELDS_OR_AMBIGUITY_REVIEW
    return LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW


def exception_group_key_hash(
    *,
    candidate_kind: str,
    reason_codes: Iterable[str],
    source_policy: LedgerExceptionSourcePolicy,
    risk_policy: LedgerExceptionRiskPolicy,
) -> str:
    if candidate_kind not in {"FACT", "TRANSACTION"}:
        raise LedgerExceptionReviewBlocked("exception candidate kind is invalid")
    canonical = "\n".join(
        (
            candidate_kind,
            ",".join(canonical_reason_codes(reason_codes)),
            source_policy.value,
            risk_policy.value,
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def exception_candidate_set_hash(candidate_hashes: Iterable[str]) -> str:
    values = tuple(sorted(candidate_hashes))
    if not values or len(values) > 500 or len(set(values)) != len(values) or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in values
    ):
        raise LedgerExceptionReviewBlocked("exception candidate hash set is invalid")
    return sha256(",".join(values).encode("ascii")).hexdigest()


def exception_decision_request_hash(
    *,
    matter_id: str,
    expected_version: int,
    exception_group_id: str,
    decision: LedgerExceptionDecision,
    reason: LedgerExceptionReason,
    reason_note: str | None,
) -> str:
    """Cross-language canonical request hash used by the 0048 DB boundary."""

    if type(expected_version) is not int or expected_version < 1:
        raise LedgerExceptionReviewBlocked("exception expected version is invalid")
    note = "" if reason_note is None else reason_note
    note_bytes = note.encode("utf-8")
    canonical = "\n".join(
        (
            "case-ledger-exception-group-request-v1",
            matter_id,
            str(expected_version),
            exception_group_id,
            decision.value,
            reason.value,
            f"{len(note_bytes)}:{note}",
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_exception_decision(
    *,
    decision: LedgerExceptionDecision,
    reason: LedgerExceptionReason,
    reason_note: str | None,
    group_reason_codes: Iterable[str],
    source_policy: LedgerExceptionSourcePolicy,
    risk_policy: LedgerExceptionRiskPolicy,
) -> str | None:
    """Validate one bounded terminal route against the immutable group policy."""

    if not isinstance(decision, LedgerExceptionDecision):
        raise LedgerExceptionReviewBlocked("exception decision is invalid")
    if reason not in _ALLOWED_REASONS[decision]:
        raise LedgerExceptionReviewBlocked("exception decision reason is not allowed")
    reasons = frozenset(canonical_reason_codes(group_reason_codes))
    if (
        decision is LedgerExceptionDecision.REJECT_AS_DUPLICATE
        and not reasons & _DUPLICATE_CODES
    ):
        raise LedgerExceptionReviewBlocked(
            "only a duplicate-risk group can be rejected as duplicate"
        )
    if (
        decision is LedgerExceptionDecision.REQUEST_REEXTRACTION
        and source_policy is not LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
        and risk_policy
        not in {
            LedgerExceptionRiskPolicy.LEGAL_OR_LEDGER_CONFLICT_REVIEW,
            LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW,
        }
    ):
        raise LedgerExceptionReviewBlocked(
            "this exception group does not support re-extraction"
        )
    if (
        decision is LedgerExceptionDecision.REQUEST_MORE_EVIDENCE
        and risk_policy
        not in {
            LedgerExceptionRiskPolicy.MISSING_FIELDS_OR_AMBIGUITY_REVIEW,
            LedgerExceptionRiskPolicy.LEGAL_OR_LEDGER_CONFLICT_REVIEW,
        }
    ):
        raise LedgerExceptionReviewBlocked(
            "this exception group does not support an evidence request"
        )

    normalized_note = None if reason_note is None else reason_note.strip()
    if normalized_note is not None and (
        not normalized_note
        or len(normalized_note) > 500
        or len(normalized_note.encode("utf-8")) > 2_000
        or any(ord(character) < 32 and character not in "\n\t" for character in normalized_note)
    ):
        raise LedgerExceptionReviewBlocked("exception decision note is invalid")
    if decision is LedgerExceptionDecision.DEFER_WITH_REASON and normalized_note is None:
        raise LedgerExceptionReviewBlocked("a deferred exception requires a bounded note")
    return normalized_note


def allowed_exception_decisions(
    *,
    reason_codes: Iterable[str],
    source_policy: LedgerExceptionSourcePolicy,
    risk_policy: LedgerExceptionRiskPolicy,
) -> tuple[LedgerExceptionDecision, ...]:
    reasons = canonical_reason_codes(reason_codes)
    allowed: list[LedgerExceptionDecision] = []
    for decision, reason in (
        (
            LedgerExceptionDecision.REJECT_AS_DUPLICATE,
            LedgerExceptionReason.DUPLICATE_CONFIRMED,
        ),
        (
            LedgerExceptionDecision.REQUEST_REEXTRACTION,
            LedgerExceptionReason.SOURCE_QUALITY_INSUFFICIENT,
        ),
        (
            LedgerExceptionDecision.REQUEST_MORE_EVIDENCE,
            LedgerExceptionReason.EVIDENCE_GAP,
        ),
        (
            LedgerExceptionDecision.DEFER_WITH_REASON,
            LedgerExceptionReason.NEEDS_LEAD_REVIEW,
        ),
    ):
        try:
            validate_exception_decision(
                decision=decision,
                reason=reason,
                reason_note=("需主办律师进一步处理"
                             if decision is LedgerExceptionDecision.DEFER_WITH_REASON
                             else None),
                group_reason_codes=reasons,
                source_policy=source_policy,
                risk_policy=risk_policy,
            )
        except LedgerExceptionReviewBlocked:
            continue
        allowed.append(decision)
    return tuple(allowed)


def exception_group_summary(
    *,
    candidate_kind: str,
    candidate_count: int,
    source_policy: LedgerExceptionSourcePolicy,
    risk_policy: LedgerExceptionRiskPolicy,
) -> str:
    if candidate_kind not in {"FACT", "TRANSACTION"}:
        raise LedgerExceptionReviewBlocked("exception candidate kind is invalid")
    if type(candidate_count) is not int or not 1 <= candidate_count <= 500:
        raise LedgerExceptionReviewBlocked("exception candidate count is invalid")
    kind = "事实候选" if candidate_kind == "FACT" else "收付款候选"
    source = (
        "需先重新核验原页"
        if source_policy is LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
        else "原页可直接核对"
    )
    risk = {
        LedgerExceptionRiskPolicy.DUPLICATE_REVIEW: "可能重复",
        LedgerExceptionRiskPolicy.LEGAL_OR_LEDGER_CONFLICT_REVIEW: "存在台账或结论冲突",
        LedgerExceptionRiskPolicy.MISSING_FIELDS_OR_AMBIGUITY_REVIEW: "字段缺失或表述不唯一",
        LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW: "未达到整组确认门槛",
    }[risk_policy]
    return f"{candidate_count}条{kind}：{risk}，{source}。"


__all__ = (
    "LedgerExceptionBatchState",
    "LedgerExceptionDecision",
    "LedgerExceptionGroup",
    "LedgerExceptionGroupMember",
    "LedgerExceptionGroupMemberPage",
    "LedgerExceptionMemberExcerpt",
    "LedgerExceptionReason",
    "LedgerExceptionReextractionCohortCapacityExceeded",
    "LedgerExceptionReextractionSourceWindowExceeded",
    "LedgerExceptionReviewBlocked",
    "LedgerExceptionRiskPolicy",
    "LedgerExceptionSourcePolicy",
    "LedgerExtractionBatchReviewStatus",
    "LedgerLowRiskLaneStatus",
    "canonical_reason_codes",
    "allowed_exception_decisions",
    "exception_candidate_set_hash",
    "exception_decision_request_hash",
    "exception_group_key_hash",
    "exception_group_summary",
    "exception_risk_policy",
    "exception_source_policy",
    "validate_exception_decision",
)
