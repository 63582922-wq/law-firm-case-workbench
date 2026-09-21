"""Fact-snapshot-bound timeline and issue-to-evidence matrix for synthetic Alpha."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .evidence_refs import EvidenceLink, EvidenceReferenceBlocked, validate_evidence_links
from .fact_claim_ledger import FactClaimSnapshot
from .models import Actor, Role


class TimelineMatrixBlocked(ValueError):
    """A time event or evidentiary use is not tied to the approved fact snapshot."""


class ReviewStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


class TimelineEventType(str, Enum):
    CONTRACT_SIGNED = "CONTRACT_SIGNED"
    DISBURSEMENT = "DISBURSEMENT"
    PAYMENT = "PAYMENT"
    INTEREST_PAYMENT = "INTEREST_PAYMENT"
    DEFAULT = "DEFAULT"
    CLAIM_FILED = "CLAIM_FILED"
    CASE_ACCEPTED = "CASE_ACCEPTED"
    SERVICE = "SERVICE"
    COMMUNICATION = "COMMUNICATION"
    OTHER = "OTHER"


class EventDatePrecision(str, Enum):
    EXACT_DATE = "EXACT_DATE"
    DATE_RANGE = "DATE_RANGE"
    UNKNOWN = "UNKNOWN"


class EvidencePurpose(str, Enum):
    PROVE_FACT = "PROVE_FACT"
    SUPPORT_CLAIM_RESPONSE = "SUPPORT_CLAIM_RESPONSE"
    PROVE_PAYMENT = "PROVE_PAYMENT"
    PROVE_IDENTITY = "PROVE_IDENTITY"
    PROVE_PROCEDURE = "PROVE_PROCEDURE"
    REBUT_OPPOSING_POSITION = "REBUT_OPPOSING_POSITION"


class BurdenParty(str, Enum):
    PLAINTIFF = "PLAINTIFF"
    DEFENDANT = "DEFENDANT"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    event_type: TimelineEventType
    date_precision: EventDatePrecision
    event_date: date | None
    range_end_date: date | None
    label: str
    fact_ids: tuple[str, ...]
    evidence_links: tuple[EvidenceLink, ...]
    fact_snapshot_hash: str | None
    status: ReviewStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class EvidenceMatrixItem:
    matrix_item_id: str
    fact_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    issue_ids: tuple[str, ...]
    purpose: EvidencePurpose
    burden_party: BurdenParty
    evidence_links: tuple[EvidenceLink, ...]
    fact_snapshot_hash: str
    status: ReviewStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class TimelineEvidenceSnapshot:
    snapshot_id: str
    ledger_version: int
    fact_snapshot_hash: str
    input_hash: str
    timeline_events: tuple[TimelineEvent, ...]
    evidence_matrix: tuple[EvidenceMatrixItem, ...]


class TimelineEvidenceLedger:
    """Requires a current FactClaimSnapshot before anything becomes formal."""

    def __init__(self) -> None:
        self._events: dict[str, TimelineEvent] = {}
        self._matrix_items: dict[str, EvidenceMatrixItem] = {}
        self._version = 1

    def add_timeline_event_candidate(
        self,
        actor: Actor,
        *,
        event_type: TimelineEventType,
        date_precision: EventDatePrecision,
        event_date: date | None,
        range_end_date: date | None,
        label: str,
        fact_ids: tuple[str, ...],
        evidence_links: tuple[EvidenceLink, ...],
    ) -> TimelineEvent:
        _require_candidate_role(actor)
        _validate_date_shape(date_precision, event_date, range_end_date)
        _require_text(label, "timeline label")
        if not fact_ids:
            raise TimelineMatrixBlocked("a timeline event requires at least one related fact")
        _validate_evidence(evidence_links)
        event = TimelineEvent(
            event_id=f"timeline_event_{uuid4().hex}",
            event_type=event_type,
            date_precision=date_precision,
            event_date=event_date,
            range_end_date=range_end_date,
            label=label.strip(),
            fact_ids=tuple(sorted(set(fact_ids))),
            evidence_links=evidence_links,
            fact_snapshot_hash=None,
            status=ReviewStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._events[event.event_id] = event
        self._version += 1
        return event

    def confirm_timeline_event(
        self,
        actor: Actor,
        *,
        event_id: str,
        fact_snapshot: FactClaimSnapshot,
        confirmation_hash: str,
    ) -> TimelineEvent:
        _require_lead(actor)
        _require_text(confirmation_hash, "timeline confirmation hash")
        event = self._events.get(event_id)
        if event is None or event.status is not ReviewStatus.CANDIDATE:
            raise TimelineMatrixBlocked("only a timeline candidate can be confirmed")
        _require_fact_ids(fact_snapshot, event.fact_ids)
        confirmed = TimelineEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            date_precision=event.date_precision,
            event_date=event.event_date,
            range_end_date=event.range_end_date,
            label=event.label,
            fact_ids=event.fact_ids,
            evidence_links=event.evidence_links,
            fact_snapshot_hash=fact_snapshot.input_hash,
            status=ReviewStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._events[event_id] = confirmed
        self._version += 1
        return confirmed

    def add_evidence_matrix_candidate(
        self,
        actor: Actor,
        *,
        fact_snapshot: FactClaimSnapshot,
        fact_ids: tuple[str, ...],
        claim_ids: tuple[str, ...],
        issue_ids: tuple[str, ...],
        purpose: EvidencePurpose,
        burden_party: BurdenParty,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> EvidenceMatrixItem:
        _require_candidate_role(actor)
        if not (fact_ids or claim_ids or issue_ids):
            raise TimelineMatrixBlocked("an evidence matrix item requires a fact, claim, or dispute issue")
        _require_fact_ids(fact_snapshot, fact_ids)
        _require_claim_ids(fact_snapshot, claim_ids)
        _require_issue_ids(fact_snapshot, issue_ids)
        _validate_evidence(evidence_links)
        item = EvidenceMatrixItem(
            matrix_item_id=f"evidence_matrix_{uuid4().hex}",
            fact_ids=tuple(sorted(set(fact_ids))),
            claim_ids=tuple(sorted(set(claim_ids))),
            issue_ids=tuple(sorted(set(issue_ids))),
            purpose=purpose,
            burden_party=burden_party,
            evidence_links=evidence_links,
            fact_snapshot_hash=fact_snapshot.input_hash,
            status=ReviewStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._matrix_items[item.matrix_item_id] = item
        self._version += 1
        return item

    def confirm_evidence_matrix_item(self, actor: Actor, *, matrix_item_id: str, confirmation_hash: str) -> EvidenceMatrixItem:
        _require_lead(actor)
        _require_text(confirmation_hash, "evidence matrix confirmation hash")
        item = self._matrix_items.get(matrix_item_id)
        if item is None or item.status is not ReviewStatus.CANDIDATE:
            raise TimelineMatrixBlocked("only an evidence matrix candidate can be confirmed")
        confirmed = EvidenceMatrixItem(
            matrix_item_id=item.matrix_item_id,
            fact_ids=item.fact_ids,
            claim_ids=item.claim_ids,
            issue_ids=item.issue_ids,
            purpose=item.purpose,
            burden_party=item.burden_party,
            evidence_links=item.evidence_links,
            fact_snapshot_hash=item.fact_snapshot_hash,
            status=ReviewStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._matrix_items[matrix_item_id] = confirmed
        self._version += 1
        return confirmed

    def build_formal_snapshot(self, actor: Actor, *, fact_snapshot: FactClaimSnapshot) -> TimelineEvidenceSnapshot:
        _require_lead(actor)
        events = tuple(sorted((item for item in self._events.values() if item.status is ReviewStatus.CONFIRMED), key=lambda item: (item.event_date or date.max, item.event_id)))
        matrix_items = tuple(sorted((item for item in self._matrix_items.values() if item.status is ReviewStatus.CONFIRMED), key=lambda item: item.matrix_item_id))
        if any(item.fact_snapshot_hash != fact_snapshot.input_hash for item in events):
            raise TimelineMatrixBlocked("a confirmed timeline event is stale against the current fact snapshot")
        if any(item.fact_snapshot_hash != fact_snapshot.input_hash for item in matrix_items):
            raise TimelineMatrixBlocked("a confirmed evidence matrix item is stale against the current fact snapshot")
        required_issues = {item.issue_id for item in fact_snapshot.issues}
        covered_issues = {issue_id for item in matrix_items for issue_id in item.issue_ids}
        if required_issues - covered_issues:
            raise TimelineMatrixBlocked("every confirmed dispute issue requires an evidence matrix entry")
        payload = {"version": self._version, "fact_snapshot_hash": fact_snapshot.input_hash, "events": events, "matrix_items": matrix_items}
        return TimelineEvidenceSnapshot(
            snapshot_id=f"timeline_evidence_snapshot_{uuid4().hex}",
            ledger_version=self._version,
            fact_snapshot_hash=fact_snapshot.input_hash,
            input_hash=_hash_payload(payload),
            timeline_events=events,
            evidence_matrix=matrix_items,
        )


def _validate_date_shape(precision: EventDatePrecision, event_date: date | None, range_end_date: date | None) -> None:
    if precision is EventDatePrecision.EXACT_DATE:
        if event_date is None or range_end_date is not None:
            raise TimelineMatrixBlocked("an exact timeline date requires one date and no range end")
    elif precision is EventDatePrecision.DATE_RANGE:
        if event_date is None or range_end_date is None or event_date > range_end_date:
            raise TimelineMatrixBlocked("a timeline date range requires an ordered start and end")
    elif event_date is not None or range_end_date is not None:
        raise TimelineMatrixBlocked("an unknown timeline date cannot be represented as a precise date")


def _require_fact_ids(snapshot: FactClaimSnapshot, fact_ids: tuple[str, ...]) -> None:
    confirmed_ids = {item.fact_id for item in snapshot.facts}
    if not set(fact_ids) <= confirmed_ids:
        raise TimelineMatrixBlocked("timeline and evidence matrix objects may only use confirmed facts")


def _require_claim_ids(snapshot: FactClaimSnapshot, claim_ids: tuple[str, ...]) -> None:
    confirmed_ids = {item.claim_id for item in snapshot.claims}
    if not set(claim_ids) <= confirmed_ids:
        raise TimelineMatrixBlocked("evidence matrix objects may only use confirmed claim scope")


def _require_issue_ids(snapshot: FactClaimSnapshot, issue_ids: tuple[str, ...]) -> None:
    confirmed_ids = {item.issue_id for item in snapshot.issues}
    if not set(issue_ids) <= confirmed_ids:
        raise TimelineMatrixBlocked("evidence matrix objects may only use confirmed dispute issues")


def _validate_evidence(links: tuple[EvidenceLink, ...]) -> None:
    try:
        validate_evidence_links(links)
    except EvidenceReferenceBlocked as error:
        raise TimelineMatrixBlocked(str(error)) from error


def _require_candidate_role(actor: Actor) -> None:
    if not actor.roles & {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER}:
        raise TimelineMatrixBlocked("actor does not have a permitted role for this timeline/evidence action")


def _require_lead(actor: Actor) -> None:
    if Role.LEAD_LAWYER not in actor.roles:
        raise TimelineMatrixBlocked("lead lawyer role is required")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise TimelineMatrixBlocked(f"{label} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(value) for value in item)
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        return item

    encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
