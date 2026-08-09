"""Deterministic, approval-bound interest and repayment calculation for synthetic Alpha.

This module deliberately does not choose a legal rule, infer a payment's nature,
or fetch a rate. Callers must supply approved CNY events and fully covered,
approved rate segments. The internal time model is [start_date, end_date): events
take effect at the start of their date, then that date's interest accrues.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable
from uuid import uuid4

from case_kernel.legal_rules import ApprovedLegalBundleReference


MONEY_UNIT = Decimal("0.01")
YEAR_DAY_COUNT = Decimal("365")


class CalculationBlocked(ValueError):
    """A scenario is incomplete, ambiguous, or outside the v1 calculation contract."""


class EventKind(str, Enum):
    DISBURSEMENT = "DISBURSEMENT"
    PAYMENT = "PAYMENT"


class AllocationPolicy(str, Enum):
    INTEREST_THEN_PRINCIPAL = "INTEREST_THEN_PRINCIPAL"
    PRINCIPAL_THEN_INTEREST = "PRINCIPAL_THEN_INTEREST"


@dataclass(frozen=True)
class ApprovedCalculationEvent:
    event_id: str
    effective_date: date
    sequence: int
    kind: EventKind
    amount: Decimal
    currency: str
    evidence_ids: tuple[str, ...]
    approved_by: str
    approval_hash: str


@dataclass(frozen=True)
class ApprovedRuleSegment:
    segment_id: str
    start_date: date
    end_date: date
    annual_rate: Decimal
    source_rule_version: str
    applicability_anchor: str
    approved_by: str
    approval_hash: str


@dataclass(frozen=True)
class CalculationScenario:
    scenario_id: str
    version: int
    start_date: date
    end_date: date
    events: tuple[ApprovedCalculationEvent, ...]
    rule_segments: tuple[ApprovedRuleSegment, ...]
    legal_bundle: ApprovedLegalBundleReference
    allocation_policy: AllocationPolicy
    approved_by: str
    approval_hash: str
    currency: str = "CNY"


@dataclass(frozen=True)
class PaymentAllocation:
    payment_event_id: str
    effective_date: date
    payment_amount: Decimal
    allocated_interest: Decimal
    allocated_principal: Decimal
    unapplied_amount: Decimal
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CalculationLineItem:
    period_start: date
    period_end: date
    opening_principal: Decimal
    annual_rate: Decimal
    day_count: int
    accrued_interest: Decimal
    closing_principal: Decimal
    accrued_unpaid_interest: Decimal
    rule_segment_id: str
    source_rule_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CalculationRun:
    run_id: str
    scenario_id: str
    scenario_version: int
    engine_version: str
    legal_bundle_id: str
    legal_bundle_hash: str
    input_hash: str
    output_hash: str
    generated_at: datetime
    line_items: tuple[CalculationLineItem, ...]
    payment_allocations: tuple[PaymentAllocation, ...]
    total_interest_accrued: Decimal
    total_interest_paid: Decimal
    remaining_principal: Decimal
    remaining_unpaid_interest: Decimal
    unapplied_payments: Decimal


@dataclass(frozen=True)
class IndependentCheck:
    matching: bool
    expected_interest_accrued: Decimal
    expected_interest_paid: Decimal
    expected_principal: Decimal
    expected_unpaid_interest: Decimal
    expected_unapplied_payments: Decimal


def calculate(scenario: CalculationScenario) -> CalculationRun:
    """Calculate one approved scenario. No unresolved input is silently defaulted."""
    _validate_scenario(scenario)
    events_by_date = _events_by_date(scenario.events)
    boundaries = _build_boundaries(scenario)
    principal = Decimal("0")
    unpaid_interest = Decimal("0")
    total_interest_accrued = Decimal("0")
    total_interest_paid = Decimal("0")
    unapplied_payments = Decimal("0")
    payment_allocations: list[PaymentAllocation] = []
    lines: list[CalculationLineItem] = []

    for start, end in zip(boundaries, boundaries[1:]):
        for event in events_by_date.get(start, ()):
            if event.kind is EventKind.DISBURSEMENT:
                principal = _money(principal + event.amount)
            else:
                allocation = _allocate_payment(
                    event,
                    principal=principal,
                    unpaid_interest=unpaid_interest,
                    policy=scenario.allocation_policy,
                )
                principal = _money(principal - allocation.allocated_principal)
                unpaid_interest = _money(unpaid_interest - allocation.allocated_interest)
                payment_allocations.append(allocation)
                total_interest_paid = _money(total_interest_paid + allocation.allocated_interest)
                unapplied_payments = _money(unapplied_payments + allocation.unapplied_amount)

        segment = _segment_for(scenario.rule_segments, start)
        accrued_interest = _period_interest(
            principal=principal,
            annual_rate=segment.annual_rate,
            day_count=(end - start).days,
        )
        unpaid_interest = _money(unpaid_interest + accrued_interest)
        total_interest_accrued = _money(total_interest_accrued + accrued_interest)
        line_evidence = tuple(
            evidence_id
            for event in events_by_date.get(start, ())
            for evidence_id in event.evidence_ids
        )
        lines.append(
            CalculationLineItem(
                period_start=start,
                period_end=end,
                opening_principal=principal,
                annual_rate=segment.annual_rate,
                day_count=(end - start).days,
                accrued_interest=accrued_interest,
                closing_principal=principal,
                accrued_unpaid_interest=unpaid_interest,
                rule_segment_id=segment.segment_id,
                source_rule_version=segment.source_rule_version,
                evidence_ids=line_evidence,
            )
        )

    input_hash = _hash_payload(scenario)
    provisional = {
        "legal_bundle_id": scenario.legal_bundle.bundle_id,
        "legal_bundle_hash": scenario.legal_bundle.bundle_hash,
        "line_items": lines,
        "payment_allocations": payment_allocations,
        "total_interest_accrued": total_interest_accrued,
        "total_interest_paid": total_interest_paid,
        "remaining_principal": principal,
        "remaining_unpaid_interest": unpaid_interest,
        "unapplied_payments": unapplied_payments,
    }
    return CalculationRun(
        run_id=f"calculation_run_{uuid4().hex}",
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.version,
        engine_version="synthetic-alpha-calc-1",
        legal_bundle_id=scenario.legal_bundle.bundle_id,
        legal_bundle_hash=scenario.legal_bundle.bundle_hash,
        input_hash=input_hash,
        output_hash=_hash_payload(provisional),
        generated_at=datetime.now(timezone.utc),
        line_items=tuple(lines),
        payment_allocations=tuple(payment_allocations),
        total_interest_accrued=total_interest_accrued,
        total_interest_paid=total_interest_paid,
        remaining_principal=principal,
        remaining_unpaid_interest=unpaid_interest,
        unapplied_payments=unapplied_payments,
    )


def independently_check(scenario: CalculationScenario, run: CalculationRun) -> IndependentCheck:
    """Daily-walk verifier separate from the interval-based production calculation.

    It applies the same declared rounding rule at every event/rule boundary and
    compares aggregate balances, so an implementation change cannot merely trust
    the values it emitted itself.
    """
    _validate_scenario(scenario)
    events_by_date = _events_by_date(scenario.events)
    boundary_dates = {scenario.start_date, scenario.end_date}
    boundary_dates.update(event.effective_date for event in scenario.events)
    boundary_dates.update(segment.start_date for segment in scenario.rule_segments)
    boundary_dates.update(segment.end_date for segment in scenario.rule_segments)
    principal = Decimal("0")
    unpaid_interest = Decimal("0")
    total_interest = Decimal("0")
    paid_interest = Decimal("0")
    unapplied = Decimal("0")
    unrounded_bucket = Decimal("0")

    cursor = scenario.start_date
    while cursor < scenario.end_date:
        if cursor in boundary_dates:
            bucket_interest = _money(unrounded_bucket)
            unpaid_interest = _money(unpaid_interest + bucket_interest)
            total_interest = _money(total_interest + bucket_interest)
            unrounded_bucket = Decimal("0")
            for event in events_by_date.get(cursor, ()):
                if event.kind is EventKind.DISBURSEMENT:
                    principal = _money(principal + event.amount)
                else:
                    allocation = _reference_allocate_payment(
                        event,
                        principal=principal,
                        unpaid_interest=unpaid_interest,
                        policy=scenario.allocation_policy,
                    )
                    principal = _money(principal - allocation.allocated_principal)
                    unpaid_interest = _money(unpaid_interest - allocation.allocated_interest)
                    paid_interest = _money(paid_interest + allocation.allocated_interest)
                    unapplied = _money(unapplied + allocation.unapplied_amount)
        segment = _segment_for(scenario.rule_segments, cursor)
        with localcontext() as context:
            context.prec = 38
            unrounded_bucket += principal * segment.annual_rate / YEAR_DAY_COUNT
        cursor = date.fromordinal(cursor.toordinal() + 1)

    final_bucket = _money(unrounded_bucket)
    unpaid_interest = _money(unpaid_interest + final_bucket)
    total_interest = _money(total_interest + final_bucket)
    return IndependentCheck(
        matching=(
            total_interest == run.total_interest_accrued
            and paid_interest == run.total_interest_paid
            and principal == run.remaining_principal
            and unpaid_interest == run.remaining_unpaid_interest
            and unapplied == run.unapplied_payments
        ),
        expected_interest_accrued=total_interest,
        expected_interest_paid=paid_interest,
        expected_principal=principal,
        expected_unpaid_interest=unpaid_interest,
        expected_unapplied_payments=unapplied,
    )


def _validate_scenario(scenario: CalculationScenario) -> None:
    if scenario.currency != "CNY":
        raise CalculationBlocked("v1 formal calculation only accepts CNY")
    if scenario.version < 1:
        raise CalculationBlocked("scenario version must be positive")
    if scenario.start_date >= scenario.end_date:
        raise CalculationBlocked("scenario must use a non-empty [start_date, end_date) interval")
    _required(scenario.approved_by, "scenario approved_by")
    _required(scenario.approval_hash, "scenario approval_hash")
    _required(scenario.legal_bundle.bundle_id, "legal bundle id")
    if len(scenario.legal_bundle.bundle_hash) != 64 or any(char not in "0123456789abcdef" for char in scenario.legal_bundle.bundle_hash):
        raise CalculationBlocked("calculation requires a SHA-256-bound approved legal bundle")
    if not scenario.legal_bundle.approved_rule_versions:
        raise CalculationBlocked("calculation requires at least one approved legal rule version")
    if not scenario.events:
        raise CalculationBlocked("at least one approved event is required")
    if not any(event.kind is EventKind.DISBURSEMENT for event in scenario.events):
        raise CalculationBlocked("at least one approved disbursement is required")
    if not scenario.rule_segments:
        raise CalculationBlocked("at least one approved rule segment is required")

    seen_sequences: set[tuple[date, int]] = set()
    for event in scenario.events:
        _required(event.event_id, "event_id")
        _required(event.approved_by, "event approved_by")
        _required(event.approval_hash, "event approval_hash")
        if event.currency != "CNY":
            raise CalculationBlocked(f"event {event.event_id} is not CNY")
        if event.amount <= 0 or event.amount != _money(event.amount):
            raise CalculationBlocked(f"event {event.event_id} amount must be positive CNY cents")
        if not event.evidence_ids:
            raise CalculationBlocked(f"event {event.event_id} requires source evidence")
        if not scenario.start_date <= event.effective_date < scenario.end_date:
            raise CalculationBlocked(f"event {event.event_id} falls outside the calculation interval")
        sequence_key = (event.effective_date, event.sequence)
        if sequence_key in seen_sequences:
            raise CalculationBlocked("same-day event sequence must be unique")
        seen_sequences.add(sequence_key)

    ordered_segments = sorted(scenario.rule_segments, key=lambda item: item.start_date)
    if ordered_segments[0].start_date != scenario.start_date or ordered_segments[-1].end_date != scenario.end_date:
        raise CalculationBlocked("rule segments must cover the full calculation interval")
    previous_end: date | None = None
    for segment in ordered_segments:
        _required(segment.segment_id, "segment_id")
        _required(segment.source_rule_version, "source_rule_version")
        if segment.source_rule_version not in scenario.legal_bundle.approved_rule_versions:
            raise CalculationBlocked("rule segment version is not included in the approved legal bundle")
        _required(segment.applicability_anchor, "applicability_anchor")
        _required(segment.approved_by, "segment approved_by")
        _required(segment.approval_hash, "segment approval_hash")
        if not Decimal("0") <= segment.annual_rate <= Decimal("1"):
            raise CalculationBlocked(f"segment {segment.segment_id} rate must be between 0 and 1")
        if segment.start_date >= segment.end_date:
            raise CalculationBlocked(f"segment {segment.segment_id} is empty")
        if previous_end is not None and segment.start_date != previous_end:
            raise CalculationBlocked("rule segments must be continuous without gaps or overlap")
        previous_end = segment.end_date


def _events_by_date(events: Iterable[ApprovedCalculationEvent]) -> dict[date, tuple[ApprovedCalculationEvent, ...]]:
    grouped: dict[date, list[ApprovedCalculationEvent]] = {}
    for event in events:
        grouped.setdefault(event.effective_date, []).append(event)
    return {day: tuple(sorted(day_events, key=lambda item: item.sequence)) for day, day_events in grouped.items()}


def _build_boundaries(scenario: CalculationScenario) -> tuple[date, ...]:
    boundaries = {scenario.start_date, scenario.end_date}
    boundaries.update(event.effective_date for event in scenario.events)
    boundaries.update(segment.start_date for segment in scenario.rule_segments)
    boundaries.update(segment.end_date for segment in scenario.rule_segments)
    return tuple(sorted(boundaries))


def _segment_for(segments: Iterable[ApprovedRuleSegment], day: date) -> ApprovedRuleSegment:
    for segment in segments:
        if segment.start_date <= day < segment.end_date:
            return segment
    raise CalculationBlocked(f"no approved rule segment covers {day.isoformat()}")


def _period_interest(*, principal: Decimal, annual_rate: Decimal, day_count: int) -> Decimal:
    with localcontext() as context:
        context.prec = 38
        return _money(principal * annual_rate * Decimal(day_count) / YEAR_DAY_COUNT)


def _allocate_payment(
    event: ApprovedCalculationEvent,
    *,
    principal: Decimal,
    unpaid_interest: Decimal,
    policy: AllocationPolicy,
) -> PaymentAllocation:
    remaining = event.amount
    if policy is AllocationPolicy.INTEREST_THEN_PRINCIPAL:
        allocated_interest = min(remaining, unpaid_interest)
        remaining = _money(remaining - allocated_interest)
        allocated_principal = min(remaining, principal)
    else:
        allocated_principal = min(remaining, principal)
        remaining = _money(remaining - allocated_principal)
        allocated_interest = min(remaining, unpaid_interest)
    applied = _money(allocated_interest + allocated_principal)
    return PaymentAllocation(
        payment_event_id=event.event_id,
        effective_date=event.effective_date,
        payment_amount=event.amount,
        allocated_interest=_money(allocated_interest),
        allocated_principal=_money(allocated_principal),
        unapplied_amount=_money(event.amount - applied),
        evidence_ids=event.evidence_ids,
    )


def _reference_allocate_payment(
    event: ApprovedCalculationEvent,
    *,
    principal: Decimal,
    unpaid_interest: Decimal,
    policy: AllocationPolicy,
) -> PaymentAllocation:
    """Independent verifier's allocation path; intentionally does not call _allocate_payment."""
    amount = event.amount
    if policy is AllocationPolicy.INTEREST_THEN_PRINCIPAL:
        paid_interest = min(unpaid_interest, amount)
        paid_principal = min(principal, _money(amount - paid_interest))
    else:
        paid_principal = min(principal, amount)
        paid_interest = min(unpaid_interest, _money(amount - paid_principal))
    unapplied = _money(amount - paid_interest - paid_principal)
    return PaymentAllocation(
        payment_event_id=event.event_id,
        effective_date=event.effective_date,
        payment_amount=amount,
        allocated_interest=_money(paid_interest),
        allocated_principal=_money(paid_principal),
        unapplied_amount=unapplied,
        evidence_ids=event.evidence_ids,
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)


def _required(value: str, field_name: str) -> None:
    if not value.strip():
        raise CalculationBlocked(f"{field_name} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(val) for val in item]
        return item

    payload = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
