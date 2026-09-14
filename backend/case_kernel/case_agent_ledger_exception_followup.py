"""Governed lifecycle for lawyer-routed ledger extraction exceptions.

0047 records that a complete exception group has been *routed*.  Three of
those routes create work which remains active after routing; they are not a
fact, transaction, legal conclusion, or completed review.  This module keeps
the public command vocabulary deliberately small and supplies the canonical
request hash shared with migration 0049.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from re import fullmatch
from uuid import UUID


class LedgerExceptionFollowupBlocked(ValueError):
    """A follow-up command violates its immutable origin or lifecycle."""


class LedgerExceptionFollowupKind(str, Enum):
    REEXTRACTION = "REEXTRACTION"
    MORE_EVIDENCE = "MORE_EVIDENCE"
    DEFERRED_REVIEW = "DEFERRED_REVIEW"


class LedgerExceptionFollowupState(str, Enum):
    ACTIVE = "ACTIVE"
    SATISFIED = "SATISFIED"
    RESUMED = "RESUMED"
    WITHDRAWN = "WITHDRAWN"
    SUPERSEDED = "SUPERSEDED"


class LedgerExceptionControlHealth(str, Enum):
    HEALTHY = "HEALTHY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class LedgerExceptionFollowupAction(str, Enum):
    CONFIRM_MORE_EVIDENCE = "CONFIRM_MORE_EVIDENCE"
    RESUME = "RESUME"
    WITHDRAW = "WITHDRAW"
    SUPERSEDE = "SUPERSEDE"


class ManagedEvidenceSourceType(str, Enum):
    EVIDENCE_FILE = "EVIDENCE_FILE"
    MATERIAL_OBJECT = "MATERIAL_OBJECT"


@dataclass(frozen=True, order=True)
class ManagedEvidenceSourceRef:
    object_type: ManagedEvidenceSourceType
    object_id: str

    def validate(self) -> None:
        if not isinstance(self.object_type, ManagedEvidenceSourceType):
            raise LedgerExceptionFollowupBlocked(
                "managed evidence source type is invalid"
            )
        _uuid(self.object_id, "managed evidence source object_id")

    @property
    def canonical_ref(self) -> str:
        self.validate()
        return f"{self.object_type.value}:{UUID(self.object_id)}"


@dataclass(frozen=True)
class ManagedEvidenceSourceCandidate:
    object_type: ManagedEvidenceSourceType
    object_id: str
    display_label: str
    created_at: datetime

    def validate(self) -> None:
        if not isinstance(self.object_type, ManagedEvidenceSourceType):
            raise LedgerExceptionFollowupBlocked(
                "managed evidence source type is invalid"
            )
        _uuid(self.object_id, "managed evidence source object_id")
        if (
            not isinstance(self.display_label, str)
            or not self.display_label
            or self.display_label != self.display_label.strip()
            or len(self.display_label) > 500
            or len(self.display_label.encode("utf-8")) > 2_000
            or any(ord(character) < 32 for character in self.display_label)
        ):
            raise LedgerExceptionFollowupBlocked(
                "managed evidence source label is invalid"
            )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise LedgerExceptionFollowupBlocked(
                "managed evidence source creation time is invalid"
            )


@dataclass(frozen=True)
class LedgerExceptionFollowup:
    followup_id: str
    origin_exception_decision_id: str
    matter_id: str
    kind: LedgerExceptionFollowupKind
    state: LedgerExceptionFollowupState
    subject_hash: str
    head_sequence: int
    managed_evidence_request_id: str | None = None

    def validate(self) -> None:
        for label, value in (
            ("followup_id", self.followup_id),
            ("origin_exception_decision_id", self.origin_exception_decision_id),
            ("matter_id", self.matter_id),
        ):
            _uuid(value, label)
        if not isinstance(self.kind, LedgerExceptionFollowupKind):
            raise LedgerExceptionFollowupBlocked("follow-up kind is invalid")
        if not isinstance(self.state, LedgerExceptionFollowupState):
            raise LedgerExceptionFollowupBlocked("follow-up state is invalid")
        _sha256(self.subject_hash, "subject_hash")
        if type(self.head_sequence) is not int or self.head_sequence < 1:
            raise LedgerExceptionFollowupBlocked("follow-up head sequence is invalid")
        if self.kind is LedgerExceptionFollowupKind.MORE_EVIDENCE:
            if self.managed_evidence_request_id is None:
                raise LedgerExceptionFollowupBlocked(
                    "more-evidence follow-up lacks its managed request"
                )
            _uuid(
                self.managed_evidence_request_id,
                "managed_evidence_request_id",
            )
        elif self.managed_evidence_request_id is not None:
            raise LedgerExceptionFollowupBlocked(
                "only a more-evidence follow-up may expose a managed request"
            )


@dataclass(frozen=True)
class LedgerExceptionFollowupSnapshot:
    """Lawyer-readable ACTIVE follow-up without internal semantic hashes.

    ``evidence_page_ids`` is the bounded page projection carried by this
    snapshot.  ``evidence_page_count`` records the complete server-side set
    size when the projection is paged; ``None`` retains the historical
    complete-snapshot contract for worker and unit-test callers.
    """

    followup_id: str
    kind: LedgerExceptionFollowupKind
    state: LedgerExceptionFollowupState
    head_sequence: int
    origin_exception_decision_id: str
    origin_exception_group_id: str
    origin_extraction_batch_id: str
    created_matter_version: int
    created_at: datetime
    reason_code: str
    reason_note: str | None
    candidate_count: int
    canonical_reason_codes: tuple[str, ...]
    evidence_page_ids: tuple[str, ...]
    control_health: LedgerExceptionControlHealth
    managed_evidence_request_id: str | None = None
    acceptance_criteria: Mapping[str, object] | None = None
    automation_status: str | None = None
    evidence_page_count: int | None = None

    def validate(self) -> None:
        for label, value in (
            ("followup_id", self.followup_id),
            ("origin_exception_decision_id", self.origin_exception_decision_id),
            ("origin_exception_group_id", self.origin_exception_group_id),
            ("origin_extraction_batch_id", self.origin_extraction_batch_id),
        ):
            _uuid(value, label)
        if not isinstance(self.kind, LedgerExceptionFollowupKind):
            raise LedgerExceptionFollowupBlocked("follow-up kind is invalid")
        if not isinstance(self.control_health, LedgerExceptionControlHealth):
            raise LedgerExceptionFollowupBlocked(
                "follow-up control health is invalid"
            )
        if self.state is not LedgerExceptionFollowupState.ACTIVE:
            raise LedgerExceptionFollowupBlocked(
                "active follow-up projection is not active"
            )
        if type(self.head_sequence) is not int or self.head_sequence < 1:
            raise LedgerExceptionFollowupBlocked(
                "follow-up head sequence is invalid"
            )
        if (
            type(self.created_matter_version) is not int
            or self.created_matter_version < 1
            or not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
        ):
            raise LedgerExceptionFollowupBlocked(
                "follow-up activation metadata is invalid"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise LedgerExceptionFollowupBlocked(
                "follow-up reason code is invalid"
            )
        if self.reason_note is not None:
            normalized = self.reason_note.strip()
            if (
                normalized != self.reason_note
                or not normalized
                or len(normalized) > 500
                or len(normalized.encode("utf-8")) > 2_000
            ):
                raise LedgerExceptionFollowupBlocked(
                    "follow-up reason note is invalid"
                )
        if type(self.candidate_count) is not int or not 1 <= self.candidate_count <= 500:
            raise LedgerExceptionFollowupBlocked(
                "follow-up candidate count is invalid"
            )
        if (
            not self.canonical_reason_codes
            or len(self.canonical_reason_codes) > 15
            or len(set(self.canonical_reason_codes)) !=
                len(self.canonical_reason_codes)
            or any(not isinstance(code, str) or not code for code in self.canonical_reason_codes)
        ):
            raise LedgerExceptionFollowupBlocked(
                "follow-up canonical reason codes are invalid"
            )
        page_count = (
            len(self.evidence_page_ids)
            if self.evidence_page_count is None
            else self.evidence_page_count
        )
        if (
            not self.evidence_page_ids
            or len(self.evidence_page_ids) > 500
            or len(set(self.evidence_page_ids)) != len(self.evidence_page_ids)
            or type(page_count) is not int
            or page_count < len(self.evidence_page_ids)
        ):
            raise LedgerExceptionFollowupBlocked(
                "follow-up evidence page set is invalid"
            )
        for page_id in self.evidence_page_ids:
            _uuid(page_id, "evidence_page_id")
        if self.kind is LedgerExceptionFollowupKind.MORE_EVIDENCE:
            if self.managed_evidence_request_id is None:
                raise LedgerExceptionFollowupBlocked(
                    "more-evidence follow-up lacks its managed request"
                )
            _uuid(self.managed_evidence_request_id, "managed_evidence_request_id")
            if (
                not isinstance(self.acceptance_criteria, Mapping)
                or self.acceptance_criteria.get("new_source_required") is not True
            ):
                raise LedgerExceptionFollowupBlocked(
                    "managed evidence acceptance criteria are invalid"
                )
        elif (
            self.managed_evidence_request_id is not None
            or self.acceptance_criteria is not None
        ):
            raise LedgerExceptionFollowupBlocked(
                "only more-evidence work may expose an evidence request"
            )
        allowed_automation_statuses = {
            "WAITING_FOR_PLAN",
            "WAITING_FOR_REPLAN",
            "QUEUED",
            "RUNNING",
            "VERIFYING",
            "BLOCKED",
            "RECOVERY_REQUIRED",
        }
        if self.kind is LedgerExceptionFollowupKind.REEXTRACTION:
            if self.automation_status not in allowed_automation_statuses:
                raise LedgerExceptionFollowupBlocked(
                    "re-extraction automation status is invalid"
                )
        elif self.automation_status is not None:
            raise LedgerExceptionFollowupBlocked(
                "only re-extraction work exposes automation status"
            )


def validate_followup_action(
    *,
    kind: LedgerExceptionFollowupKind,
    action: LedgerExceptionFollowupAction,
    managed_evidence_request_id: str | None,
    reextraction_batch_id: str | None,
    reason_note: str | None,
    managed_evidence_sources: Sequence[ManagedEvidenceSourceRef] = (),
) -> str | None:
    """Validate the exact bounded transition without inferring satisfaction."""

    if not isinstance(kind, LedgerExceptionFollowupKind):
        raise LedgerExceptionFollowupBlocked("follow-up kind is invalid")
    if not isinstance(action, LedgerExceptionFollowupAction):
        raise LedgerExceptionFollowupBlocked("follow-up action is invalid")
    if action is LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE:
        if kind is not LedgerExceptionFollowupKind.MORE_EVIDENCE:
            raise LedgerExceptionFollowupBlocked(
                "only a more-evidence follow-up can be confirmed"
            )
        if managed_evidence_request_id is None or reextraction_batch_id is not None:
            raise LedgerExceptionFollowupBlocked(
                "evidence confirmation requires its exact managed request"
            )
        _uuid(managed_evidence_request_id, "managed_evidence_request_id")
        _canonical_managed_evidence_source_set(
            managed_evidence_sources,
            allow_empty=False,
        )
    elif action is LedgerExceptionFollowupAction.RESUME:
        if kind is not LedgerExceptionFollowupKind.DEFERRED_REVIEW:
            raise LedgerExceptionFollowupBlocked(
                "only a deferred follow-up can be resumed"
            )
        if (
            managed_evidence_request_id is not None
            or managed_evidence_sources
            or reextraction_batch_id is not None
        ):
            raise LedgerExceptionFollowupBlocked(
                "a deferred transition cannot bind an unrelated batch or request"
            )
    elif action in {
        LedgerExceptionFollowupAction.WITHDRAW,
        LedgerExceptionFollowupAction.SUPERSEDE,
    }:
        if (
            managed_evidence_request_id is not None
            or managed_evidence_sources
            or reextraction_batch_id is not None
        ):
            raise LedgerExceptionFollowupBlocked(
                "a manual closure cannot bind an unrelated batch or request"
            )
    else:  # pragma: no cover - the enum makes this defensive only
        raise LedgerExceptionFollowupBlocked("follow-up action is unsupported")

    normalized = None if reason_note is None else reason_note.strip()
    if normalized is not None and (
        not normalized
        or len(normalized) > 500
        or len(normalized.encode("utf-8")) > 2_000
        or any(
            ord(character) < 32 and character not in "\n\t"
            for character in normalized
        )
    ):
        raise LedgerExceptionFollowupBlocked("follow-up reason note is invalid")
    if action in {
        LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
        LedgerExceptionFollowupAction.RESUME,
        LedgerExceptionFollowupAction.WITHDRAW,
        LedgerExceptionFollowupAction.SUPERSEDE,
    } and normalized is None:
        raise LedgerExceptionFollowupBlocked(
            "a lawyer follow-up transition requires a bounded reason"
        )
    return normalized


def followup_request_hash(
    *,
    matter_id: str,
    expected_version: int,
    followup_id: str,
    action: LedgerExceptionFollowupAction,
    managed_evidence_request_id: str | None = None,
    managed_evidence_sources: Sequence[ManagedEvidenceSourceRef] = (),
    reextraction_batch_id: str | None = None,
    reason_note: str | None = None,
) -> str:
    """Return the cross-language 0049 command hash.

    The optional UUID slots are always present as empty strings when unused;
    the UTF-8 byte length prevents ambiguous notes across Python/PostgreSQL.
    """

    _uuid(matter_id, "matter_id")
    _uuid(followup_id, "followup_id")
    matter_id = str(UUID(matter_id))
    followup_id = str(UUID(followup_id))
    if type(expected_version) is not int or expected_version < 1:
        raise LedgerExceptionFollowupBlocked("expected matter version is invalid")
    if not isinstance(action, LedgerExceptionFollowupAction):
        raise LedgerExceptionFollowupBlocked("follow-up action is invalid")
    if managed_evidence_request_id is not None:
        _uuid(managed_evidence_request_id, "managed_evidence_request_id")
        managed_evidence_request_id = str(UUID(managed_evidence_request_id))
    if reextraction_batch_id is not None:
        _uuid(reextraction_batch_id, "reextraction_batch_id")
        reextraction_batch_id = str(UUID(reextraction_batch_id))
    source_refs = _canonical_managed_evidence_source_set(
        managed_evidence_sources,
        allow_empty=True,
    )
    note = "" if reason_note is None else reason_note
    canonical = "\n".join(
        (
            "case-ledger-exception-followup-request-v2",
            matter_id,
            str(expected_version),
            followup_id,
            action.value,
            managed_evidence_request_id or "",
            reextraction_batch_id or "",
            f"{len(source_refs.encode('utf-8'))}:{source_refs}",
            f"{len(note.encode('utf-8'))}:{note}",
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def reextraction_task_binding_request_hash(
    *,
    matter_id: str,
    expected_version: int,
    followup_id: str,
    run_id: str,
    graph_id: str,
    task_id: str,
) -> str:
    """Hash the non-mutating server binding for one exact extraction task."""

    for label, value in (
        ("matter_id", matter_id),
        ("followup_id", followup_id),
        ("run_id", run_id),
        ("graph_id", graph_id),
        ("task_id", task_id),
    ):
        _uuid(value, label)
    matter_id = str(UUID(matter_id))
    followup_id = str(UUID(followup_id))
    run_id = str(UUID(run_id))
    graph_id = str(UUID(graph_id))
    task_id = str(UUID(task_id))
    if type(expected_version) is not int or expected_version < 1:
        raise LedgerExceptionFollowupBlocked("expected matter version is invalid")
    canonical = "\n".join(
        (
            "case-ledger-exception-reextraction-task-binding-v1",
            matter_id,
            str(expected_version),
            followup_id,
            run_id,
            graph_id,
            task_id,
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def control_transfer_request_hash(
    *,
    matter_id: str,
    expected_version: int,
    replacement_run_id: str,
) -> str:
    """Hash one server-selected replacement control run."""

    _uuid(matter_id, "matter_id")
    _uuid(replacement_run_id, "replacement_run_id")
    if type(expected_version) is not int or expected_version < 1:
        raise LedgerExceptionFollowupBlocked("expected matter version is invalid")
    canonical = "\n".join(
        (
            "case-ledger-exception-control-transfer-v1",
            str(UUID(matter_id)),
            str(expected_version),
            str(UUID(replacement_run_id)),
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def reextraction_set_satisfaction_request_hash(
    *,
    matter_id: str,
    expected_version: int,
    run_id: str,
    graph_id: str,
) -> str:
    """Hash one atomic, server-discovered re-extraction cohort.

    A caller cannot name a follow-up or a batch.  PostgreSQL discovers every
    active re-extraction follow-up for the run and verifies the current task
    binding and staged batch for each member before closing the set once.
    """

    for label, value in (
        ("matter_id", matter_id),
        ("run_id", run_id),
        ("graph_id", graph_id),
    ):
        _uuid(value, label)
    if type(expected_version) is not int or expected_version < 1:
        raise LedgerExceptionFollowupBlocked("expected matter version is invalid")
    canonical = "\n".join(
        (
            "case-ledger-exception-reextraction-set-v1",
            str(UUID(matter_id)),
            str(expected_version),
            str(UUID(run_id)),
            str(UUID(graph_id)),
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or fullmatch(r"[A-Za-z0-9._~-]{16,128}", value) is None:
        raise LedgerExceptionFollowupBlocked("idempotency key is invalid")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise LedgerExceptionFollowupBlocked(f"{label} is invalid") from error


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LedgerExceptionFollowupBlocked(f"{label} is invalid")


def _canonical_managed_evidence_source_set(
    sources: Sequence[ManagedEvidenceSourceRef],
    *,
    allow_empty: bool,
) -> str:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise LedgerExceptionFollowupBlocked(
            "managed evidence sources must be a bounded sequence"
        )
    if (not allow_empty and not sources) or len(sources) > 100:
        raise LedgerExceptionFollowupBlocked(
            "managed evidence sources must contain between 1 and 100 objects"
        )
    canonical: list[str] = []
    for source in sources:
        if not isinstance(source, ManagedEvidenceSourceRef):
            raise LedgerExceptionFollowupBlocked(
                "managed evidence source is invalid"
            )
        canonical.append(source.canonical_ref)
    if len(set(canonical)) != len(canonical):
        raise LedgerExceptionFollowupBlocked(
            "managed evidence sources contain duplicates"
        )
    return "\n".join(sorted(canonical))


__all__ = (
    "LedgerExceptionControlHealth",
    "LedgerExceptionFollowup",
    "LedgerExceptionFollowupAction",
    "LedgerExceptionFollowupBlocked",
    "LedgerExceptionFollowupKind",
    "LedgerExceptionFollowupState",
    "LedgerExceptionFollowupSnapshot",
    "ManagedEvidenceSourceRef",
    "ManagedEvidenceSourceCandidate",
    "ManagedEvidenceSourceType",
    "control_transfer_request_hash",
    "followup_request_hash",
    "reextraction_set_satisfaction_request_hash",
    "reextraction_task_binding_request_hash",
    "validate_followup_action",
    "validate_idempotency_key",
)
