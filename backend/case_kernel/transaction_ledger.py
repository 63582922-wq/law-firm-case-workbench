"""Evidence-bound transaction reconciliation and payment classification for synthetic Alpha.

This ledger deliberately keeps a source transaction, party positions, a
lawyer-approved classification, and duplicate handling as separate objects.
It never treats a transfer record as a repayment merely because an amount is
present, and it never deletes a duplicate source record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .calculation_engine import ApprovedCalculationEvent, EventKind, PaymentApplication
from .evidence_refs import EvidenceLink, EvidenceReferenceBlocked, validate_evidence_links
from .models import Actor, Role


MONEY_UNIT = Decimal("0.01")


class TransactionLedgerBlocked(ValueError):
    """A transaction or payment classification cannot safely enter calculation input."""


class TransactionStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


class DatePrecision(str, Enum):
    EXACT_DATE = "EXACT_DATE"
    MONTH_ONLY = "MONTH_ONLY"
    YEAR_ONLY = "YEAR_ONLY"
    UNKNOWN = "UNKNOWN"


class TransactionDirection(str, Enum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"
    UNKNOWN = "UNKNOWN"


class TransactionChannel(str, Enum):
    WECHAT = "WECHAT"
    BANK = "BANK"
    CASH = "CASH"
    CHAT_RECORD = "CHAT_RECORD"
    LOAN_INSTRUMENT = "LOAN_INSTRUMENT"
    OTHER = "OTHER"


class ClassificationOrigin(str, Enum):
    PLAINTIFF_PLEADING = "PLAINTIFF_PLEADING"
    DEFENDANT_STATEMENT = "DEFENDANT_STATEMENT"
    AGENT_CANDIDATE = "AGENT_CANDIDATE"
    ASSISTANT_ENTRY = "ASSISTANT_ENTRY"


class PaymentNature(str, Enum):
    DISBURSEMENT = "DISBURSEMENT"
    REPAYMENT_UNSPECIFIED = "REPAYMENT_UNSPECIFIED"
    INTEREST_PAYMENT = "INTEREST_PAYMENT"
    PRINCIPAL_REPAYMENT = "PRINCIPAL_REPAYMENT"
    REFUND = "REFUND"
    FEE = "FEE"
    UNRELATED = "UNRELATED"


class ClassificationStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    INVALIDATED = "INVALIDATED"


class DuplicateStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    SAME_ECONOMIC_EVENT = "SAME_ECONOMIC_EVENT"
    DISTINCT_EVENTS = "DISTINCT_EVENTS"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    local_date: date | None
    date_precision: DatePrecision
    amount: Decimal
    currency: str
    direction: TransactionDirection
    payer_label: str | None
    payee_label: str | None
    channel: TransactionChannel
    transaction_reference: str | None
    evidence_links: tuple[EvidenceLink, ...]
    status: TransactionStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class ObligationAllocation:
    obligation_id: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PaymentClassificationProposal:
    proposal_id: str
    transaction_id: str
    origin: ClassificationOrigin
    nature: PaymentNature
    allocations: tuple[ObligationAllocation, ...]
    same_day_sequence: int | None
    evidence_links: tuple[EvidenceLink, ...]
    status: ClassificationStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class DuplicateGroup:
    group_id: str
    transaction_ids: tuple[str, ...]
    status: DuplicateStatus
    canonical_transaction_id: str | None
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class TransactionCalculationSnapshot:
    snapshot_id: str
    ledger_version: int
    obligation_id: str
    input_hash: str
    included_transaction_ids: tuple[str, ...]
    events: tuple[ApprovedCalculationEvent, ...]


class TransactionLedger:
    """In-memory Alpha ledger; callers must persist its approved state before production use."""

    def __init__(self) -> None:
        self._transactions: dict[str, Transaction] = {}
        self._classifications: dict[str, PaymentClassificationProposal] = {}
        self._duplicate_groups: dict[str, DuplicateGroup] = {}
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def add_transaction_candidate(
        self,
        actor: Actor,
        *,
        local_date: date | None,
        date_precision: DatePrecision,
        amount: Decimal,
        currency: str,
        direction: TransactionDirection,
        payer_label: str | None,
        payee_label: str | None,
        channel: TransactionChannel,
        transaction_reference: str | None,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> Transaction:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        _validate_transaction_input(local_date, date_precision, amount, currency, evidence_links)
        transaction = Transaction(
            transaction_id=f"transaction_{uuid4().hex}",
            local_date=local_date,
            date_precision=date_precision,
            amount=amount,
            currency=currency.strip().upper(),
            direction=direction,
            payer_label=_normalized_optional_text(payer_label),
            payee_label=_normalized_optional_text(payee_label),
            channel=channel,
            transaction_reference=_normalized_optional_text(transaction_reference),
            evidence_links=evidence_links,
            status=TransactionStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._transactions[transaction.transaction_id] = transaction
        self._version += 1
        return transaction

    def confirm_transaction(self, actor: Actor, *, transaction_id: str, confirmation_hash: str) -> Transaction:
        _require_lead(actor)
        _require_text(confirmation_hash, "transaction confirmation hash")
        transaction = self._require_transaction(transaction_id)
        if transaction.status is not TransactionStatus.CANDIDATE:
            raise TransactionLedgerBlocked("only a transaction candidate can be confirmed")
        confirmed = Transaction(
            transaction_id=transaction.transaction_id,
            local_date=transaction.local_date,
            date_precision=transaction.date_precision,
            amount=transaction.amount,
            currency=transaction.currency,
            direction=transaction.direction,
            payer_label=transaction.payer_label,
            payee_label=transaction.payee_label,
            channel=transaction.channel,
            transaction_reference=transaction.transaction_reference,
            evidence_links=transaction.evidence_links,
            status=TransactionStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._transactions[transaction_id] = confirmed
        self._version += 1
        return confirmed

    def invalidate_transaction(self, actor: Actor, *, transaction_id: str, reason_hash: str) -> Transaction:
        _require_lead(actor)
        _require_text(reason_hash, "transaction invalidation hash")
        transaction = self._require_transaction(transaction_id)
        if transaction.status is TransactionStatus.INVALIDATED:
            raise TransactionLedgerBlocked("an invalidated transaction must be rebuilt from its original evidence")
        invalidated = Transaction(
            transaction_id=transaction.transaction_id,
            local_date=transaction.local_date,
            date_precision=transaction.date_precision,
            amount=transaction.amount,
            currency=transaction.currency,
            direction=transaction.direction,
            payer_label=transaction.payer_label,
            payee_label=transaction.payee_label,
            channel=transaction.channel,
            transaction_reference=transaction.transaction_reference,
            evidence_links=transaction.evidence_links,
            status=TransactionStatus.INVALIDATED,
            confirmed_by=actor.actor_id,
            confirmation_hash=reason_hash,
        )
        self._transactions[transaction_id] = invalidated
        self._invalidate_dependents({transaction_id})
        self._version += 1
        return invalidated

    def add_payment_classification_candidate(
        self,
        actor: Actor,
        *,
        transaction_id: str,
        origin: ClassificationOrigin,
        nature: PaymentNature,
        allocations: tuple[ObligationAllocation, ...],
        same_day_sequence: int | None,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> PaymentClassificationProposal:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        transaction = self._require_transaction(transaction_id)
        if transaction.status is TransactionStatus.INVALIDATED:
            raise TransactionLedgerBlocked("a classification cannot use an invalidated transaction")
        _validate_classification(transaction, nature, allocations, same_day_sequence, evidence_links)
        proposal = PaymentClassificationProposal(
            proposal_id=f"payment_classification_{uuid4().hex}",
            transaction_id=transaction_id,
            origin=origin,
            nature=nature,
            allocations=tuple(sorted(allocations, key=lambda item: item.obligation_id)),
            same_day_sequence=same_day_sequence,
            evidence_links=evidence_links,
            status=ClassificationStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._classifications[proposal.proposal_id] = proposal
        self._version += 1
        return proposal

    def approve_payment_classification(
        self,
        actor: Actor,
        *,
        proposal_id: str,
        approval_hash: str,
    ) -> PaymentClassificationProposal:
        _require_lead(actor)
        _require_text(approval_hash, "payment classification approval hash")
        proposal = self._require_classification(proposal_id)
        if proposal.status is not ClassificationStatus.CANDIDATE:
            raise TransactionLedgerBlocked("only an active payment classification candidate can be approved")
        transaction = self._require_transaction(proposal.transaction_id)
        if transaction.status is not TransactionStatus.CONFIRMED:
            raise TransactionLedgerBlocked("the source transaction must be lawyer-confirmed before classification approval")
        _validate_classification(transaction, proposal.nature, proposal.allocations, proposal.same_day_sequence, proposal.evidence_links)
        self._invalidate_classifications_for_transaction(proposal.transaction_id)
        approved = PaymentClassificationProposal(
            proposal_id=proposal.proposal_id,
            transaction_id=proposal.transaction_id,
            origin=proposal.origin,
            nature=proposal.nature,
            allocations=proposal.allocations,
            same_day_sequence=proposal.same_day_sequence,
            evidence_links=proposal.evidence_links,
            status=ClassificationStatus.APPROVED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._classifications[proposal_id] = approved
        self._version += 1
        return approved

    def add_duplicate_group_candidate(
        self,
        actor: Actor,
        *,
        transaction_ids: tuple[str, ...],
    ) -> DuplicateGroup:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        normalized_ids = tuple(sorted(set(transaction_ids)))
        if len(normalized_ids) < 2:
            raise TransactionLedgerBlocked("a duplicate group needs at least two distinct transactions")
        for transaction_id in normalized_ids:
            transaction = self._require_transaction(transaction_id)
            if transaction.status is TransactionStatus.INVALIDATED:
                raise TransactionLedgerBlocked("an invalidated transaction cannot enter a duplicate group")
        active_members = {
            transaction_id
            for group in self._duplicate_groups.values()
            if group.status is not DuplicateStatus.INVALIDATED
            for transaction_id in group.transaction_ids
        }
        if active_members & set(normalized_ids):
            raise TransactionLedgerBlocked("a transaction already belongs to an active duplicate group")
        group = DuplicateGroup(
            group_id=f"duplicate_group_{uuid4().hex}",
            transaction_ids=normalized_ids,
            status=DuplicateStatus.CANDIDATE,
            canonical_transaction_id=None,
            approved_by=None,
            approval_hash=None,
        )
        self._duplicate_groups[group.group_id] = group
        self._version += 1
        return group

    def resolve_duplicate_group(
        self,
        actor: Actor,
        *,
        group_id: str,
        same_economic_event: bool,
        canonical_transaction_id: str | None,
        approval_hash: str,
    ) -> DuplicateGroup:
        _require_lead(actor)
        _require_text(approval_hash, "duplicate resolution approval hash")
        group = self._duplicate_groups.get(group_id)
        if group is None or group.status is not DuplicateStatus.CANDIDATE:
            raise TransactionLedgerBlocked("only an active duplicate candidate group can be resolved")
        if same_economic_event:
            if canonical_transaction_id not in group.transaction_ids:
                raise TransactionLedgerBlocked("a same-event duplicate group needs a canonical transaction in the group")
            status = DuplicateStatus.SAME_ECONOMIC_EVENT
        else:
            if canonical_transaction_id is not None:
                raise TransactionLedgerBlocked("a distinct-events resolution cannot choose a canonical transaction")
            status = DuplicateStatus.DISTINCT_EVENTS
        resolved = DuplicateGroup(
            group_id=group.group_id,
            transaction_ids=group.transaction_ids,
            status=status,
            canonical_transaction_id=canonical_transaction_id,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._duplicate_groups[group_id] = resolved
        self._version += 1
        return resolved

    def build_calculation_snapshot(self, actor: Actor, *, obligation_id: str) -> TransactionCalculationSnapshot:
        """Build CNY calculation events for one approved obligation without legal inference."""
        _require_lead(actor)
        _require_text(obligation_id, "obligation id")
        selected = self._approved_classifications_for_obligation(obligation_id)
        if not selected:
            raise TransactionLedgerBlocked("no approved classified transactions exist for this obligation")
        selected = self._resolve_duplicates_for_calculation(selected, obligation_id)
        events = tuple(
            sorted(
                (self._to_calculation_event(transaction, proposal, obligation_id) for transaction, proposal in selected),
                key=lambda item: (item.effective_date, item.sequence, item.event_id),
            )
        )
        sequence_keys = {(event.effective_date, event.sequence) for event in events}
        if len(sequence_keys) != len(events):
            raise TransactionLedgerBlocked("calculation-ready transactions must have unique same-day sequences")
        payload = {"ledger_version": self._version, "obligation_id": obligation_id, "events": events}
        return TransactionCalculationSnapshot(
            snapshot_id=f"transaction_calculation_snapshot_{uuid4().hex}",
            ledger_version=self._version,
            obligation_id=obligation_id,
            input_hash=_hash_payload(payload),
            included_transaction_ids=tuple(sorted(transaction.transaction_id for transaction, _ in selected)),
            events=events,
        )

    def _approved_classifications_for_obligation(
        self, obligation_id: str
    ) -> list[tuple[Transaction, PaymentClassificationProposal]]:
        selected: list[tuple[Transaction, PaymentClassificationProposal]] = []
        for proposal in self._classifications.values():
            if proposal.status is not ClassificationStatus.APPROVED:
                continue
            if not any(item.obligation_id == obligation_id for item in proposal.allocations):
                continue
            transaction = self._require_transaction(proposal.transaction_id)
            if transaction.status is not TransactionStatus.CONFIRMED:
                raise TransactionLedgerBlocked("an approved payment classification has an unconfirmed source transaction")
            selected.append((transaction, proposal))
        return selected

    def _resolve_duplicates_for_calculation(
        self,
        selected: list[tuple[Transaction, PaymentClassificationProposal]],
        obligation_id: str,
    ) -> list[tuple[Transaction, PaymentClassificationProposal]]:
        selected_by_id = {transaction.transaction_id: (transaction, proposal) for transaction, proposal in selected}
        for group in self._duplicate_groups.values():
            members = set(group.transaction_ids) & set(selected_by_id)
            if not members:
                continue
            if group.status is DuplicateStatus.CANDIDATE:
                raise TransactionLedgerBlocked("a calculation-ready transaction has an unresolved duplicate candidate")
            if group.status is DuplicateStatus.SAME_ECONOMIC_EVENT:
                canonical_id = group.canonical_transaction_id
                if canonical_id not in members:
                    raise TransactionLedgerBlocked(
                        f"duplicate group canonical transaction needs an approved classification for obligation {obligation_id}"
                    )
                for transaction_id in members - {canonical_id}:
                    del selected_by_id[transaction_id]
        return [selected_by_id[transaction_id] for transaction_id in sorted(selected_by_id)]

    def _to_calculation_event(
        self,
        transaction: Transaction,
        proposal: PaymentClassificationProposal,
        obligation_id: str,
    ) -> ApprovedCalculationEvent:
        if transaction.date_precision is not DatePrecision.EXACT_DATE or transaction.local_date is None:
            raise TransactionLedgerBlocked("formal calculation requires an exact lawyer-confirmed transaction date")
        if transaction.currency != "CNY":
            raise TransactionLedgerBlocked("v1 formal calculation only accepts CNY transactions")
        if transaction.amount != _money(transaction.amount):
            raise TransactionLedgerBlocked("formal calculation requires a transaction amount rounded to CNY cents")
        if proposal.same_day_sequence is None:
            raise TransactionLedgerBlocked("a calculation-relevant transaction requires a lawyer-approved same-day sequence")
        allocation = next((item for item in proposal.allocations if item.obligation_id == obligation_id), None)
        if allocation is None:
            raise TransactionLedgerBlocked("classification allocation does not include the selected obligation")
        if allocation.currency != "CNY" or allocation.amount != _money(allocation.amount):
            raise TransactionLedgerBlocked("formal calculation requires CNY-cent allocations")
        kind, payment_application = _calculation_mapping(proposal.nature)
        evidence_ids = tuple(
            sorted({link.evidence_id for link in transaction.evidence_links + proposal.evidence_links})
        )
        return ApprovedCalculationEvent(
            event_id=f"{transaction.transaction_id}:{obligation_id}",
            effective_date=transaction.local_date,
            sequence=proposal.same_day_sequence,
            kind=kind,
            amount=allocation.amount,
            currency="CNY",
            evidence_ids=evidence_ids,
            approved_by=proposal.approved_by or "",
            approval_hash=proposal.approval_hash or "",
            payment_application=payment_application,
        )

    def _invalidate_dependents(self, transaction_ids: set[str]) -> None:
        for proposal_id, proposal in list(self._classifications.items()):
            if proposal.transaction_id in transaction_ids and proposal.status is not ClassificationStatus.INVALIDATED:
                self._classifications[proposal_id] = PaymentClassificationProposal(
                    proposal_id=proposal.proposal_id,
                    transaction_id=proposal.transaction_id,
                    origin=proposal.origin,
                    nature=proposal.nature,
                    allocations=proposal.allocations,
                    same_day_sequence=proposal.same_day_sequence,
                    evidence_links=proposal.evidence_links,
                    status=ClassificationStatus.INVALIDATED,
                    approved_by=None,
                    approval_hash=None,
                )
        for group_id, group in list(self._duplicate_groups.items()):
            if set(group.transaction_ids) & transaction_ids and group.status is not DuplicateStatus.INVALIDATED:
                self._duplicate_groups[group_id] = DuplicateGroup(
                    group_id=group.group_id,
                    transaction_ids=group.transaction_ids,
                    status=DuplicateStatus.INVALIDATED,
                    canonical_transaction_id=None,
                    approved_by=None,
                    approval_hash=None,
                )

    def _invalidate_classifications_for_transaction(self, transaction_id: str) -> None:
        for proposal_id, proposal in list(self._classifications.items()):
            if proposal.transaction_id != transaction_id or proposal.status is not ClassificationStatus.APPROVED:
                continue
            self._classifications[proposal_id] = PaymentClassificationProposal(
                proposal_id=proposal.proposal_id,
                transaction_id=proposal.transaction_id,
                origin=proposal.origin,
                nature=proposal.nature,
                allocations=proposal.allocations,
                same_day_sequence=proposal.same_day_sequence,
                evidence_links=proposal.evidence_links,
                status=ClassificationStatus.INVALIDATED,
                approved_by=None,
                approval_hash=None,
            )

    def _require_transaction(self, transaction_id: str) -> Transaction:
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            raise TransactionLedgerBlocked("unknown transaction")
        return transaction

    def _require_classification(self, proposal_id: str) -> PaymentClassificationProposal:
        proposal = self._classifications.get(proposal_id)
        if proposal is None:
            raise TransactionLedgerBlocked("unknown payment classification proposal")
        return proposal


def _validate_transaction_input(
    local_date: date | None,
    date_precision: DatePrecision,
    amount: Decimal,
    currency: str,
    evidence_links: tuple[EvidenceLink, ...],
) -> None:
    if date_precision is DatePrecision.EXACT_DATE and local_date is None:
        raise TransactionLedgerBlocked("an exact transaction date requires local_date")
    if date_precision is not DatePrecision.EXACT_DATE and local_date is not None:
        raise TransactionLedgerBlocked("a non-exact transaction date cannot be represented as an exact local_date")
    _validate_amount(amount, "transaction")
    _validate_currency(currency, "transaction")
    _validate_evidence(evidence_links)


def _validate_classification(
    transaction: Transaction,
    nature: PaymentNature,
    allocations: tuple[ObligationAllocation, ...],
    same_day_sequence: int | None,
    evidence_links: tuple[EvidenceLink, ...],
) -> None:
    _validate_evidence(evidence_links)
    if same_day_sequence is not None and same_day_sequence < 1:
        raise TransactionLedgerBlocked("same-day sequence must be a positive integer")
    financial_natures = {
        PaymentNature.DISBURSEMENT,
        PaymentNature.REPAYMENT_UNSPECIFIED,
        PaymentNature.INTEREST_PAYMENT,
        PaymentNature.PRINCIPAL_REPAYMENT,
    }
    if nature not in financial_natures:
        if allocations:
            raise TransactionLedgerBlocked("a non-calculation payment nature cannot allocate an obligation")
        return
    if not allocations:
        raise TransactionLedgerBlocked("a calculation-relevant payment nature requires an obligation allocation")
    if len({item.obligation_id for item in allocations}) != len(allocations):
        raise TransactionLedgerBlocked("an obligation can appear only once in one payment classification")
    total = Decimal("0")
    for allocation in allocations:
        _require_text(allocation.obligation_id, "obligation id")
        _validate_amount(allocation.amount, "allocation")
        _validate_currency(allocation.currency, "allocation")
        if allocation.currency.upper() != transaction.currency:
            raise TransactionLedgerBlocked("every allocation currency must match the source transaction currency")
        total += allocation.amount
    if total != transaction.amount:
        raise TransactionLedgerBlocked("classification allocations must equal the full source transaction amount")


def _calculation_mapping(nature: PaymentNature) -> tuple[EventKind, PaymentApplication]:
    if nature is PaymentNature.DISBURSEMENT:
        return EventKind.DISBURSEMENT, PaymentApplication.BY_POLICY
    if nature is PaymentNature.REPAYMENT_UNSPECIFIED:
        return EventKind.PAYMENT, PaymentApplication.BY_POLICY
    if nature is PaymentNature.INTEREST_PAYMENT:
        return EventKind.PAYMENT, PaymentApplication.INTEREST_ONLY
    if nature is PaymentNature.PRINCIPAL_REPAYMENT:
        return EventKind.PAYMENT, PaymentApplication.PRINCIPAL_ONLY
    raise TransactionLedgerBlocked("only disbursements and approved repayment classifications can enter calculation")


def _validate_evidence(links: tuple[EvidenceLink, ...]) -> None:
    try:
        validate_evidence_links(links)
    except EvidenceReferenceBlocked as error:
        raise TransactionLedgerBlocked(str(error)) from error


def _validate_amount(amount: Decimal, label: str) -> None:
    if amount <= 0:
        raise TransactionLedgerBlocked(f"{label} amount must be positive")


def _validate_currency(value: str, label: str) -> None:
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise TransactionLedgerBlocked(f"{label} currency must use a three-letter code")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)


def _require_role(actor: Actor, allowed: set[Role]) -> None:
    if not actor.roles & allowed:
        raise TransactionLedgerBlocked("actor does not have a permitted role for this transaction-ledger action")


def _require_lead(actor: Actor) -> None:
    _require_role(actor, {Role.LEAD_LAWYER})


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise TransactionLedgerBlocked(f"{label} is required")


def _normalized_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(item) for item in item)
        if isinstance(item, (tuple, list)):
            return [normalize(item) for item in item]
        return item

    encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
