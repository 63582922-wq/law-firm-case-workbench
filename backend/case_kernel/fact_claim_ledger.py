"""Evidence-linked facts, claim scope, and dispute issues for synthetic Alpha.

The ledger records positions; it never turns OCR, a plaintiff's pleading, or an
agent proposal into a lawyer-confirmed fact. Formal snapshots expose only the
lawyer-approved objects and their evidence links.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .evidence_refs import EvidenceLink, EvidenceReferenceBlocked, validate_evidence_links
from .models import Actor, Role


class FactLedgerBlocked(ValueError):
    """A proposed fact, claim response, or issue cannot safely enter a formal snapshot."""


class AssertionOrigin(str, Enum):
    PLAINTIFF_PLEADING = "PLAINTIFF_PLEADING"
    DEFENDANT_STATEMENT = "DEFENDANT_STATEMENT"
    AGENT_CANDIDATE = "AGENT_CANDIDATE"
    ASSISTANT_ENTRY = "ASSISTANT_ENTRY"


class FactStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    DENIED = "DENIED"
    INVALIDATED = "INVALIDATED"


class ClaimStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED_SCOPE = "CONFIRMED_SCOPE"
    INVALIDATED = "INVALIDATED"


class ClaimResponsePosition(str, Enum):
    ADMIT = "ADMIT"
    PARTIALLY_ADMIT = "PARTIALLY_ADMIT"
    DISPUTE = "DISPUTE"
    OUTSIDE_SCOPE = "OUTSIDE_SCOPE"


class IssueStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class FactAssertion:
    fact_id: str
    original_text: str
    origin: AssertionOrigin
    status: FactStatus
    evidence_links: tuple[EvidenceLink, ...]
    decided_by: str | None
    decision_hash: str | None


@dataclass(frozen=True)
class ClaimItem:
    claim_id: str
    original_claim_text: str
    claimed_amount: Decimal | None
    currency: str | None
    status: ClaimStatus
    evidence_links: tuple[EvidenceLink, ...]
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class ClaimResponse:
    claim_id: str
    position: ClaimResponsePosition
    confirmed_fact_ids: tuple[str, ...]
    partial_amount: Decimal | None
    currency: str | None
    approved_by: str
    approval_hash: str


@dataclass(frozen=True)
class DisputeIssue:
    issue_id: str
    question: str
    claim_ids: tuple[str, ...]
    confirmed_fact_ids: tuple[str, ...]
    status: IssueStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class FactClaimSnapshot:
    snapshot_id: str
    ledger_version: int
    input_hash: str
    facts: tuple[FactAssertion, ...]
    claims: tuple[ClaimItem, ...]
    responses: tuple[ClaimResponse, ...]
    issues: tuple[DisputeIssue, ...]


class FactClaimLedger:
    """In-memory domain ledger. Persistent projections are added after the fact schema migration."""

    def __init__(self) -> None:
        self._facts: dict[str, FactAssertion] = {}
        self._claims: dict[str, ClaimItem] = {}
        self._responses: dict[str, ClaimResponse] = {}
        self._issues: dict[str, DisputeIssue] = {}
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def add_fact_candidate(
        self,
        actor: Actor,
        *,
        original_text: str,
        origin: AssertionOrigin,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> FactAssertion:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        _require_text(original_text, "fact original_text")
        _validate_evidence(evidence_links)
        fact = FactAssertion(
            fact_id=f"fact_{uuid4().hex}",
            original_text=original_text.strip(),
            origin=origin,
            status=FactStatus.CANDIDATE,
            evidence_links=evidence_links,
            decided_by=None,
            decision_hash=None,
        )
        self._facts[fact.fact_id] = fact
        self._version += 1
        return fact

    def decide_fact(
        self,
        actor: Actor,
        *,
        fact_id: str,
        status: FactStatus,
        decision_hash: str,
    ) -> FactAssertion:
        _require_lead(actor)
        if status is FactStatus.CANDIDATE:
            raise FactLedgerBlocked("a lawyer decision cannot leave a fact as candidate")
        _require_text(decision_hash, "fact decision hash")
        fact = self._facts.get(fact_id)
        if fact is None:
            raise FactLedgerBlocked("unknown fact")
        if fact.status is FactStatus.INVALIDATED:
            raise FactLedgerBlocked("an invalidated fact must be rebuilt from source evidence")
        decided = FactAssertion(
            fact_id=fact.fact_id,
            original_text=fact.original_text,
            origin=fact.origin,
            status=status,
            evidence_links=fact.evidence_links,
            decided_by=actor.actor_id,
            decision_hash=decision_hash,
        )
        self._facts[fact_id] = decided
        self._version += 1
        self._invalidate_dependents({fact_id})
        return decided

    def add_claim_candidate(
        self,
        actor: Actor,
        *,
        original_claim_text: str,
        claimed_amount: Decimal | None,
        currency: str | None,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> ClaimItem:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        _require_text(original_claim_text, "claim original text")
        _validate_evidence(evidence_links)
        _validate_monetary_value(claimed_amount, currency, "claim")
        claim = ClaimItem(
            claim_id=f"claim_{uuid4().hex}",
            original_claim_text=original_claim_text.strip(),
            claimed_amount=claimed_amount,
            currency=currency,
            status=ClaimStatus.CANDIDATE,
            evidence_links=evidence_links,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._claims[claim.claim_id] = claim
        self._version += 1
        return claim

    def confirm_claim_scope(self, actor: Actor, *, claim_id: str, confirmation_hash: str) -> ClaimItem:
        _require_lead(actor)
        _require_text(confirmation_hash, "claim confirmation hash")
        claim = self._claims.get(claim_id)
        if claim is None:
            raise FactLedgerBlocked("unknown claim")
        if claim.status is ClaimStatus.INVALIDATED:
            raise FactLedgerBlocked("an invalidated claim must be rebuilt from its original source")
        confirmed = ClaimItem(
            claim_id=claim.claim_id,
            original_claim_text=claim.original_claim_text,
            claimed_amount=claim.claimed_amount,
            currency=claim.currency,
            status=ClaimStatus.CONFIRMED_SCOPE,
            evidence_links=claim.evidence_links,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._claims[claim_id] = confirmed
        self._version += 1
        self._invalidate_dependents(set(), {claim_id})
        return confirmed

    def set_claim_response(
        self,
        actor: Actor,
        *,
        claim_id: str,
        position: ClaimResponsePosition,
        confirmed_fact_ids: tuple[str, ...],
        partial_amount: Decimal | None,
        currency: str | None,
        approval_hash: str,
    ) -> ClaimResponse:
        _require_lead(actor)
        _require_text(approval_hash, "claim response approval hash")
        claim = self._claims.get(claim_id)
        if claim is None or claim.status is not ClaimStatus.CONFIRMED_SCOPE:
            raise FactLedgerBlocked("claim scope must be confirmed before a response can be recorded")
        _require_confirmed_facts(self._facts, confirmed_fact_ids)
        if position is ClaimResponsePosition.PARTIALLY_ADMIT:
            _validate_monetary_value(partial_amount, currency, "partial response")
            if claim.claimed_amount is not None and partial_amount is not None and partial_amount > claim.claimed_amount:
                raise FactLedgerBlocked("partial response amount cannot exceed the confirmed claim amount")
            if claim.currency and currency != claim.currency:
                raise FactLedgerBlocked("partial response currency must match the confirmed claim currency")
        elif partial_amount is not None or currency is not None:
            raise FactLedgerBlocked("only a partial response may contain a partial amount or currency")
        response = ClaimResponse(
            claim_id=claim_id,
            position=position,
            confirmed_fact_ids=tuple(sorted(set(confirmed_fact_ids))),
            partial_amount=partial_amount,
            currency=currency,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._responses[claim_id] = response
        self._version += 1
        # Replacing a response must invalidate downstream issues, but it must
        # not delete the response just approved in this command.
        self._invalidate_issues(set(), {claim_id})
        return response

    def add_dispute_issue_candidate(
        self,
        actor: Actor,
        *,
        question: str,
        claim_ids: tuple[str, ...],
        confirmed_fact_ids: tuple[str, ...],
    ) -> DisputeIssue:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        _require_text(question, "issue question")
        if not claim_ids:
            raise FactLedgerBlocked("a dispute issue requires at least one claim")
        if not all(claim_id in self._claims for claim_id in claim_ids):
            raise FactLedgerBlocked("dispute issue references an unknown claim")
        _require_confirmed_facts(self._facts, confirmed_fact_ids)
        issue = DisputeIssue(
            issue_id=f"issue_{uuid4().hex}",
            question=question.strip(),
            claim_ids=tuple(sorted(set(claim_ids))),
            confirmed_fact_ids=tuple(sorted(set(confirmed_fact_ids))),
            status=IssueStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._issues[issue.issue_id] = issue
        self._version += 1
        return issue

    def confirm_dispute_issue(self, actor: Actor, *, issue_id: str, approval_hash: str) -> DisputeIssue:
        _require_lead(actor)
        _require_text(approval_hash, "issue approval hash")
        issue = self._issues.get(issue_id)
        if issue is None or issue.status is IssueStatus.INVALIDATED:
            raise FactLedgerBlocked("only an active issue candidate can be confirmed")
        if any(self._claims[claim_id].status is not ClaimStatus.CONFIRMED_SCOPE for claim_id in issue.claim_ids):
            raise FactLedgerBlocked("all claims for an issue must have confirmed scope")
        _require_confirmed_facts(self._facts, issue.confirmed_fact_ids)
        confirmed = DisputeIssue(
            issue_id=issue.issue_id,
            question=issue.question,
            claim_ids=issue.claim_ids,
            confirmed_fact_ids=issue.confirmed_fact_ids,
            status=IssueStatus.CONFIRMED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._issues[issue_id] = confirmed
        self._version += 1
        return confirmed

    def build_formal_snapshot(self, actor: Actor) -> FactClaimSnapshot:
        _require_lead(actor)
        responses = tuple(sorted(self._responses.values(), key=lambda item: item.claim_id))
        confirmed_claims = tuple(sorted((item for item in self._claims.values() if item.status is ClaimStatus.CONFIRMED_SCOPE), key=lambda item: item.claim_id))
        confirmed_issues = tuple(sorted((item for item in self._issues.values() if item.status is IssueStatus.CONFIRMED), key=lambda item: item.issue_id))
        if not confirmed_claims:
            raise FactLedgerBlocked("a formal snapshot requires at least one confirmed claim scope")
        if {item.claim_id for item in confirmed_claims} - {item.claim_id for item in responses}:
            raise FactLedgerBlocked("every confirmed claim scope requires an explicit lawyer response")
        if not confirmed_issues:
            raise FactLedgerBlocked("a formal snapshot requires at least one confirmed dispute issue")
        facts = tuple(sorted((item for item in self._facts.values() if item.status is FactStatus.CONFIRMED), key=lambda item: item.fact_id))
        payload = {"facts": facts, "claims": confirmed_claims, "responses": responses, "issues": confirmed_issues, "ledger_version": self._version}
        return FactClaimSnapshot(
            snapshot_id=f"fact_claim_snapshot_{uuid4().hex}",
            ledger_version=self._version,
            input_hash=_hash_payload(payload),
            facts=facts,
            claims=confirmed_claims,
            responses=responses,
            issues=confirmed_issues,
        )

    def _invalidate_dependents(self, fact_ids: set[str], claim_ids: set[str] | None = None) -> None:
        impacted_claims = claim_ids or set()
        for claim_id, response in list(self._responses.items()):
            if claim_id in impacted_claims or set(response.confirmed_fact_ids) & fact_ids:
                del self._responses[claim_id]
        self._invalidate_issues(fact_ids, impacted_claims)

    def _invalidate_issues(self, fact_ids: set[str], claim_ids: set[str]) -> None:
        for issue_id, issue in list(self._issues.items()):
            if set(issue.claim_ids) & claim_ids or set(issue.confirmed_fact_ids) & fact_ids:
                self._issues[issue_id] = DisputeIssue(
                    issue_id=issue.issue_id,
                    question=issue.question,
                    claim_ids=issue.claim_ids,
                    confirmed_fact_ids=issue.confirmed_fact_ids,
                    status=IssueStatus.INVALIDATED,
                    approved_by=None,
                    approval_hash=None,
                )


def _require_role(actor: Actor, allowed: set[Role]) -> None:
    if not actor.roles & allowed:
        raise FactLedgerBlocked("actor does not have a permitted role for this fact-ledger action")


def _require_lead(actor: Actor) -> None:
    _require_role(actor, {Role.LEAD_LAWYER})


def _validate_evidence(links: tuple[EvidenceLink, ...]) -> None:
    try:
        validate_evidence_links(links)
    except EvidenceReferenceBlocked as error:
        raise FactLedgerBlocked(str(error)) from error


def _validate_monetary_value(amount: Decimal | None, currency: str | None, label: str) -> None:
    if amount is None:
        if currency is not None:
            raise FactLedgerBlocked(f"{label} currency cannot exist without an amount")
        return
    if amount < 0 or amount.as_tuple().exponent < -2:
        raise FactLedgerBlocked(f"{label} amount must be non-negative with at most two decimal places")
    _require_text(currency or "", f"{label} currency")


def _require_confirmed_facts(facts: dict[str, FactAssertion], fact_ids: tuple[str, ...]) -> None:
    if not fact_ids:
        raise FactLedgerBlocked("at least one lawyer-confirmed fact is required")
    for fact_id in fact_ids:
        fact = facts.get(fact_id)
        if fact is None or fact.status is not FactStatus.CONFIRMED:
            raise FactLedgerBlocked("responses and issues may only use lawyer-confirmed facts")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise FactLedgerBlocked(f"{label} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(value) for key, value in item.__dict__.items()}
        if isinstance(item, dict):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(value) for value in item)
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        return item

    return sha256(json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
