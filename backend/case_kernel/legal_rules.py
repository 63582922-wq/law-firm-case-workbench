"""Approval-bound, time-aware legal rule candidates for synthetic Alpha.

This module does not interpret a statute or select a legal position. It records
the conditions that a lawyer-approved rule card needs, evaluates only declared
dates and facts, and blocks an immutable case bundle until an attorney resolves
any applicable-rule conflict and approves the exact candidate hash.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable
from uuid import uuid4


class LegalRuleBlocked(ValueError):
    """A legal rule card or case bundle lacks an auditable prerequisite."""


class LegalEventKind(str, Enum):
    CONTRACT_SIGNED = "CONTRACT_SIGNED"
    DISBURSEMENT = "DISBURSEMENT"
    PAYMENT = "PAYMENT"
    DEFAULT = "DEFAULT"
    CLAIM_FILED = "CLAIM_FILED"
    CASE_ACCEPTED = "CASE_ACCEPTED"
    JUDGMENT = "JUDGMENT"


class SourceVerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    SUPERSEDED = "SUPERSEDED"


class SourceLicenseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MISSING = "MISSING"
    REVOKED = "REVOKED"


class RuleReviewStatus(str, Enum):
    LAWYER_APPROVED = "LAWYER_APPROVED"
    CANDIDATE = "CANDIDATE"
    SUPERSEDED = "SUPERSEDED"


class CandidateStatus(str, Enum):
    MATCHED = "MATCHED"
    MISSING_EVENT = "MISSING_EVENT"
    MISSING_CONDITION = "MISSING_CONDITION"
    OUTSIDE_EFFECTIVE_PERIOD = "OUTSIDE_EFFECTIVE_PERIOD"
    SOURCE_NOT_READY = "SOURCE_NOT_READY"
    RULE_NOT_APPROVED = "RULE_NOT_APPROVED"


class LegalBundleStatus(str, Enum):
    BLOCKED = "BLOCKED"
    AWAITING_LAWYER_APPROVAL = "AWAITING_LAWYER_APPROVAL"
    APPROVED = "APPROVED"


@dataclass(frozen=True)
class OfficialSourceSnapshot:
    snapshot_id: str
    source_id: str
    official_url: str
    source_tier: str
    retrieved_at: datetime
    content_sha256: str
    verification_status: SourceVerificationStatus
    license_status: SourceLicenseStatus


@dataclass(frozen=True)
class ApprovedLegalEvent:
    event_id: str
    kind: LegalEventKind
    local_date: date | None
    source_evidence_ids: tuple[str, ...]
    approved_by: str
    approval_hash: str


@dataclass(frozen=True)
class ApplicabilityRule:
    rule_id: str
    version: str
    issue_key: str
    source_snapshot: OfficialSourceSnapshot
    effective_from: date
    effective_to: date | None
    trigger_event_kind: LegalEventKind
    required_fact_keys: tuple[str, ...]
    transition_rule_ids: tuple[str, ...]
    conflict_set: str | None
    priority: int
    review_status: RuleReviewStatus


@dataclass(frozen=True)
class RuleCandidate:
    rule_id: str
    version: str
    issue_key: str
    status: CandidateStatus
    reason: str
    trigger_event_id: str | None
    trigger_date: date | None
    source_snapshot_id: str
    source_sha256: str
    conflict_set: str | None
    priority: int


@dataclass(frozen=True)
class RuleResolution:
    issue_key: str
    rule_id: str
    version: str
    selected_by: str
    selection_reason: str
    approval_hash: str


@dataclass(frozen=True)
class RuleSelection:
    issue_key: str
    rule_id: str
    version: str
    source_snapshot_id: str
    source_sha256: str
    trigger_event_id: str
    trigger_date: date
    resolution_hash: str | None


@dataclass(frozen=True)
class CaseLegalBundleDraft:
    draft_id: str
    matter_id: str
    version: int
    status: LegalBundleStatus
    candidates: tuple[RuleCandidate, ...]
    selections: tuple[RuleSelection, ...]
    blockers: tuple[str, ...]
    input_hash: str


@dataclass(frozen=True)
class LegalBundleApproval:
    approved_by: str
    approved_object_hash: str
    approval_hash: str
    approved_at: datetime


@dataclass(frozen=True)
class CaseLegalBundle:
    bundle_id: str
    matter_id: str
    version: int
    status: LegalBundleStatus
    selections: tuple[RuleSelection, ...]
    input_hash: str
    bundle_hash: str
    approval: LegalBundleApproval

    @property
    def approved_rule_versions(self) -> tuple[str, ...]:
        return tuple(selection.version for selection in self.selections)


@dataclass(frozen=True)
class ApprovedLegalBundleReference:
    bundle_id: str
    bundle_hash: str
    approved_rule_versions: tuple[str, ...]


class InMemoryLegalBundleRegistry:
    """Alpha-only read boundary; PostgreSQL replaces it without changing its checks."""

    def __init__(self, bundles: Iterable[CaseLegalBundle] = ()) -> None:
        self._bundles: dict[str, CaseLegalBundle] = {}
        for bundle in bundles:
            self.add(bundle)

    def add(self, bundle: CaseLegalBundle) -> None:
        if bundle.status is not LegalBundleStatus.APPROVED:
            raise LegalRuleBlocked("only an approved case legal bundle can be registered")
        if bundle.bundle_id in self._bundles:
            raise LegalRuleBlocked("case legal bundle id already exists")
        self._bundles[bundle.bundle_id] = bundle

    def get_reference(self, bundle_id: str) -> ApprovedLegalBundleReference:
        bundle = self._bundles.get(bundle_id)
        if bundle is None:
            raise LegalRuleBlocked("unknown approved case legal bundle")
        return ApprovedLegalBundleReference(
            bundle_id=bundle.bundle_id,
            bundle_hash=bundle.bundle_hash,
            approved_rule_versions=bundle.approved_rule_versions,
        )


def prepare_case_legal_bundle(
    *,
    matter_id: str,
    version: int,
    required_issue_keys: tuple[str, ...],
    approved_events: tuple[ApprovedLegalEvent, ...],
    confirmed_fact_keys: frozenset[str],
    rules: tuple[ApplicabilityRule, ...],
    resolutions: tuple[RuleResolution, ...] = (),
) -> CaseLegalBundleDraft:
    """Evaluate declared prerequisites only; never turn a competing rule into a default selection."""
    _required(matter_id, "matter_id")
    if version < 1:
        raise LegalRuleBlocked("bundle version must be positive")
    if not required_issue_keys or len(set(required_issue_keys)) != len(required_issue_keys):
        raise LegalRuleBlocked("required issue keys must be non-empty and unique")
    if not rules:
        raise LegalRuleBlocked("at least one applicability rule is required")
    _validate_events(approved_events)
    _validate_rules(rules)
    normalized_issue_keys = tuple(sorted(required_issue_keys))
    normalized_events = tuple(sorted(approved_events, key=lambda item: item.event_id))
    normalized_rules = tuple(sorted(rules, key=lambda item: (item.issue_key, -item.priority, item.rule_id, item.version)))
    normalized_resolutions = tuple(sorted(resolutions, key=lambda item: item.issue_key))
    resolution_by_issue = _resolution_by_issue(normalized_resolutions)

    candidates = tuple(_evaluate_rule(rule, normalized_events, confirmed_fact_keys) for rule in normalized_rules)
    selections: list[RuleSelection] = []
    blockers: list[str] = []
    for issue_key in normalized_issue_keys:
        matches = [candidate for candidate in candidates if candidate.issue_key == issue_key and candidate.status is CandidateStatus.MATCHED]
        if not matches:
            blockers.append(f"{issue_key}: no fully supported approved rule candidate")
            continue
        resolution = resolution_by_issue.get(issue_key)
        if len(matches) == 1 and resolution is None:
            selections.append(_selection_from(matches[0], None))
            continue
        if resolution is None:
            blockers.append(f"{issue_key}: multiple rule candidates require explicit lawyer selection")
            continue
        selected = next((item for item in matches if item.rule_id == resolution.rule_id and item.version == resolution.version), None)
        if selected is None:
            blockers.append(f"{issue_key}: selected rule is not a supported candidate")
            continue
        selections.append(_selection_from(selected, resolution))

    normalized_candidates = tuple(sorted(candidates, key=lambda item: (item.issue_key, -item.priority, item.rule_id, item.version)))
    normalized_selections = tuple(sorted(selections, key=lambda item: item.issue_key))
    input_hash = _hash_payload(
        {
            "matter_id": matter_id,
            "version": version,
            "required_issue_keys": normalized_issue_keys,
            "events": normalized_events,
            "confirmed_fact_keys": tuple(sorted(confirmed_fact_keys)),
            "rules": normalized_rules,
            "resolutions": normalized_resolutions,
            "candidates": normalized_candidates,
            "selections": normalized_selections,
        }
    )
    return CaseLegalBundleDraft(
        draft_id=f"legal_bundle_draft_{uuid4().hex}",
        matter_id=matter_id,
        version=version,
        status=LegalBundleStatus.BLOCKED if blockers else LegalBundleStatus.AWAITING_LAWYER_APPROVAL,
        candidates=normalized_candidates,
        selections=normalized_selections,
        blockers=tuple(blockers),
        input_hash=input_hash,
    )


def approve_case_legal_bundle(
    draft: CaseLegalBundleDraft,
    approval: LegalBundleApproval,
    *,
    bundle_id: str | None = None,
) -> CaseLegalBundle:
    if draft.status is not LegalBundleStatus.AWAITING_LAWYER_APPROVAL:
        raise LegalRuleBlocked("a blocked legal bundle draft cannot be approved")
    _required(approval.approved_by, "approval approved_by")
    _required(approval.approval_hash, "approval hash")
    if approval.approved_object_hash != draft.input_hash:
        raise LegalRuleBlocked("legal bundle approval must bind to the current draft input hash")
    bundle_hash = _hash_payload(
        {
            "matter_id": draft.matter_id,
            "version": draft.version,
            "input_hash": draft.input_hash,
            "selections": draft.selections,
            "approval": approval,
        }
    )
    return CaseLegalBundle(
        bundle_id=bundle_id or f"case_legal_bundle_{uuid4().hex}",
        matter_id=draft.matter_id,
        version=draft.version,
        status=LegalBundleStatus.APPROVED,
        selections=draft.selections,
        input_hash=draft.input_hash,
        bundle_hash=bundle_hash,
        approval=approval,
    )


def synthetic_alpha_legal_bundle() -> CaseLegalBundle:
    """Strictly synthetic fixture; it expresses no real-law conclusion or source text."""
    snapshot = OfficialSourceSnapshot(
        snapshot_id="alpha_snapshot_interest_001",
        source_id="SYNTHETIC-LEGAL-SOURCE-001",
        official_url="https://synthetic.invalid/legal-source-001",
        source_tier="SYNTHETIC_FIXTURE",
        retrieved_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        content_sha256=sha256(b"synthetic legal source fixture").hexdigest(),
        verification_status=SourceVerificationStatus.VERIFIED,
        license_status=SourceLicenseStatus.ACTIVE,
    )
    events = (
        ApprovedLegalEvent(
            event_id="alpha_legal_event_contract",
            kind=LegalEventKind.CONTRACT_SIGNED,
            local_date=date(2020, 8, 20),
            source_evidence_ids=("alpha_evidence_contract",),
            approved_by="alpha_lead_lawyer",
            approval_hash="alpha-contract-approval",
        ),
        ApprovedLegalEvent(
            event_id="alpha_legal_event_filed",
            kind=LegalEventKind.CLAIM_FILED,
            local_date=date(2020, 9, 5),
            source_evidence_ids=("alpha_evidence_claim",),
            approved_by="alpha_lead_lawyer",
            approval_hash="alpha-filed-approval",
        ),
    )
    rules = (
        ApplicabilityRule(
            rule_id="alpha_interest_rule_segment_001",
            version="SYNTHETIC-RULE-1",
            issue_key="interest_segment_one",
            source_snapshot=snapshot,
            effective_from=date(2020, 8, 20),
            effective_to=date(2020, 9, 5),
            trigger_event_kind=LegalEventKind.CONTRACT_SIGNED,
            required_fact_keys=frozenset_to_tuple({"interest_rate_basis_confirmed"}),
            transition_rule_ids=(),
            conflict_set=None,
            priority=1,
            review_status=RuleReviewStatus.LAWYER_APPROVED,
        ),
        ApplicabilityRule(
            rule_id="alpha_interest_rule_segment_002",
            version="SYNTHETIC-RULE-2",
            issue_key="interest_segment_two",
            source_snapshot=snapshot,
            effective_from=date(2020, 9, 5),
            effective_to=None,
            trigger_event_kind=LegalEventKind.CLAIM_FILED,
            required_fact_keys=frozenset_to_tuple({"interest_rate_basis_confirmed"}),
            transition_rule_ids=("alpha_transition_reviewed",),
            conflict_set=None,
            priority=1,
            review_status=RuleReviewStatus.LAWYER_APPROVED,
        ),
    )
    draft = prepare_case_legal_bundle(
        matter_id="alpha_matter_interest_001",
        version=1,
        required_issue_keys=("interest_segment_one", "interest_segment_two"),
        approved_events=events,
        confirmed_fact_keys=frozenset({"interest_rate_basis_confirmed"}),
        rules=rules,
    )
    return approve_case_legal_bundle(
        draft,
        LegalBundleApproval(
            approved_by="alpha_lead_lawyer",
            approved_object_hash=draft.input_hash,
            approval_hash="alpha-legal-bundle-approval",
            approved_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        ),
        bundle_id="alpha_legal_bundle_interest_001",
    )


def frozenset_to_tuple(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _evaluate_rule(
    rule: ApplicabilityRule,
    events: tuple[ApprovedLegalEvent, ...],
    confirmed_fact_keys: frozenset[str],
) -> RuleCandidate:
    source = rule.source_snapshot
    if source.verification_status is not SourceVerificationStatus.VERIFIED or source.license_status is not SourceLicenseStatus.ACTIVE:
        return _candidate(rule, CandidateStatus.SOURCE_NOT_READY, "source snapshot verification or license is not active", None)
    if rule.review_status is not RuleReviewStatus.LAWYER_APPROVED:
        return _candidate(rule, CandidateStatus.RULE_NOT_APPROVED, "rule card has not been lawyer approved", None)
    missing_conditions = set(rule.required_fact_keys) - set(confirmed_fact_keys)
    if missing_conditions:
        return _candidate(rule, CandidateStatus.MISSING_CONDITION, f"missing confirmed facts: {', '.join(sorted(missing_conditions))}", None)
    matching_events = [event for event in events if event.kind is rule.trigger_event_kind]
    if not matching_events:
        return _candidate(rule, CandidateStatus.MISSING_EVENT, "required trigger event is not available", None)
    if len(matching_events) > 1:
        return _candidate(rule, CandidateStatus.MISSING_EVENT, "multiple trigger events require a lawyer-approved event selection", None)
    event = matching_events[0]
    if event.local_date is None:
        return _candidate(rule, CandidateStatus.MISSING_EVENT, "trigger event date is not approved", event)
    if event.local_date < rule.effective_from or (rule.effective_to is not None and event.local_date >= rule.effective_to):
        return _candidate(rule, CandidateStatus.OUTSIDE_EFFECTIVE_PERIOD, "trigger date falls outside the rule card effective period", event)
    return _candidate(rule, CandidateStatus.MATCHED, "declared temporal and fact conditions are satisfied", event)


def _candidate(rule: ApplicabilityRule, status: CandidateStatus, reason: str, event: ApprovedLegalEvent | None) -> RuleCandidate:
    return RuleCandidate(
        rule_id=rule.rule_id,
        version=rule.version,
        issue_key=rule.issue_key,
        status=status,
        reason=reason,
        trigger_event_id=event.event_id if event else None,
        trigger_date=event.local_date if event else None,
        source_snapshot_id=rule.source_snapshot.snapshot_id,
        source_sha256=rule.source_snapshot.content_sha256,
        conflict_set=rule.conflict_set,
        priority=rule.priority,
    )


def _selection_from(candidate: RuleCandidate, resolution: RuleResolution | None) -> RuleSelection:
    if candidate.trigger_event_id is None or candidate.trigger_date is None:
        raise LegalRuleBlocked("a selected rule candidate needs an approved trigger event and date")
    return RuleSelection(
        issue_key=candidate.issue_key,
        rule_id=candidate.rule_id,
        version=candidate.version,
        source_snapshot_id=candidate.source_snapshot_id,
        source_sha256=candidate.source_sha256,
        trigger_event_id=candidate.trigger_event_id,
        trigger_date=candidate.trigger_date,
        resolution_hash=resolution.approval_hash if resolution else None,
    )


def _validate_events(events: tuple[ApprovedLegalEvent, ...]) -> None:
    if not events:
        raise LegalRuleBlocked("at least one approved legal event is required")
    seen_ids: set[str] = set()
    for event in events:
        _required(event.event_id, "legal event id")
        _required(event.approved_by, "legal event approved_by")
        _required(event.approval_hash, "legal event approval hash")
        if not event.source_evidence_ids:
            raise LegalRuleBlocked("legal events require source evidence")
        if event.event_id in seen_ids:
            raise LegalRuleBlocked("legal event ids must be unique")
        seen_ids.add(event.event_id)


def _validate_rules(rules: tuple[ApplicabilityRule, ...]) -> None:
    seen_versions: set[tuple[str, str]] = set()
    for rule in rules:
        _required(rule.rule_id, "rule id")
        _required(rule.version, "rule version")
        _required(rule.issue_key, "rule issue key")
        snapshot = rule.source_snapshot
        _required(snapshot.snapshot_id, "source snapshot id")
        _required(snapshot.source_id, "source id")
        if not snapshot.official_url.startswith("https://"):
            raise LegalRuleBlocked("rule source URL must use HTTPS")
        if len(snapshot.content_sha256) != 64 or any(char not in "0123456789abcdef" for char in snapshot.content_sha256):
            raise LegalRuleBlocked("source snapshot requires a SHA-256 hash")
        if rule.effective_to is not None and rule.effective_from >= rule.effective_to:
            raise LegalRuleBlocked("rule effective period must be non-empty")
        key = (rule.rule_id, rule.version)
        if key in seen_versions:
            raise LegalRuleBlocked("rule id and version pairs must be unique")
        seen_versions.add(key)


def _resolution_by_issue(resolutions: tuple[RuleResolution, ...]) -> dict[str, RuleResolution]:
    values: dict[str, RuleResolution] = {}
    for resolution in resolutions:
        _required(resolution.issue_key, "resolution issue key")
        _required(resolution.rule_id, "resolution rule id")
        _required(resolution.version, "resolution rule version")
        _required(resolution.selected_by, "resolution selected_by")
        _required(resolution.selection_reason, "resolution reason")
        _required(resolution.approval_hash, "resolution approval hash")
        if resolution.issue_key in values:
            raise LegalRuleBlocked("only one lawyer rule resolution is allowed per issue")
        values[resolution.issue_key] = resolution
    return values


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise LegalRuleBlocked(f"{field_name} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(val) for val in item)
        if isinstance(item, (tuple, list)):
            return [normalize(val) for val in item]
        return item

    payload = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
