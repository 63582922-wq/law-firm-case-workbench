"""Shadow-mode generic deterministic engine.

Implements the frozen calculation rules R0-R10 of
``docs/GOLDEN_CASE_SYNTHETIC.md`` §3 for arbitrary ledger rows (CNY only).
The engine accepts only rows, debt definitions and lawyer-level config; it
never accepts numeric parameters from an LLM.

Identity proof: the engine must reproduce the authoritative 47-row synthetic
case to the cent across all four scenarios.  ``identity_proof`` below performs
that comparison against the frozen oracle calculator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable, Mapping, Sequence

CENT = Decimal("0.01")
BOUNDARY = date(2020, 8, 20)          # frozen: new rate period starts (inclusive)
OLD_CAP = Decimal("0.02")             # frozen: protected monthly rate before boundary (24%/yr)
NEW_CAP_DEFAULT = Decimal("0.01")     # 起诉时一年期LPR(3.00%)×4 = 12%/yr = 1%/month
CEILING_36 = Decimal("0.03")          # frozen: old-period 36%/yr line (3%/month)
DAY_DIVISOR = Decimal("30")           # frozen: daily rate = monthly rate / 30

CLASS_BORROW = "本金出借"
CLASS_REPAY = "还本"
CLASS_INTEREST = "付息"
CLASS_THIRD_PARTY = "代付"
CLASS_DISPUTED = "争议"
CLASS_EXCLUDE = "排除"
CLASS_BLOCK = "阻断"


class ShadowEngineBlocked(RuntimeError):
    """Fail-closed error: invalid input or unsafe parameter source."""


def _parse_date(value: str) -> date:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (time part ignored)."""
    return date.fromisoformat(str(value).strip().split()[0])


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def canonical_amount(value: Decimal) -> str:
    """Canonical money string used by S4 byte-level checks (no separators)."""
    return f"{Decimal(value).quantize(CENT):.2f}"


@dataclass(frozen=True)
class ShadowRow:
    row_id: str
    occurred_on: date
    channel: str
    amount: Decimal
    currency: str
    direction: str
    classification: str
    debt_id: str | None
    memo: str
    source_ref: object = None

    @property
    def ordering_key(self) -> tuple:
        return (self.occurred_on, str(self.row_id))


@dataclass(frozen=True)
class ShadowDebt:
    debt_id: str
    principal: Decimal
    disbursed_on: date
    agreed_monthly_rate: Decimal
    due_on: date | None = None
    evidence_pending: bool = False


@dataclass(frozen=True)
class ShadowEngineConfig:
    boundary: date = BOUNDARY
    final_date: date = date(2025, 6, 14)
    old_cap: Decimal = OLD_CAP
    new_cap: Decimal = NEW_CAP_DEFAULT
    ceiling_36: Decimal = CEILING_36
    day_divisor: Decimal = DAY_DIVISOR


@dataclass
class ShadowTraceLine:
    ordinal: int
    row_id: str
    occurred_on: date
    touched_debt_ids: tuple[str, ...]
    payment_amount: Decimal
    accrued_interest: Decimal
    interest_paid: Decimal
    natural_debt_paid: Decimal
    excess_principal_offset: Decimal
    principal_payment: Decimal
    debt_snapshot: dict


@dataclass
class ShadowLoanResult:
    debt_id: str
    principal: Decimal
    interest_arrears: Decimal
    interest_paid: Decimal
    excess_principal_offset: Decimal
    principal_paid: Decimal
    natural_debt_paid: Decimal


@dataclass
class ShadowEngineResult:
    config: ShadowEngineConfig
    trace: list[ShadowTraceLine] = field(default_factory=list)
    loans: dict[str, ShadowLoanResult] = field(default_factory=dict)
    excluded_row_ids: list[str] = field(default_factory=list)
    blocked_row_ids: list[str] = field(default_factory=list)

    def loan(self, debt_id: str) -> ShadowLoanResult:
        return self.loans[debt_id]

    @property
    def total_principal(self) -> Decimal:
        return sum((item.principal for item in self.loans.values()), Decimal("0"))

    @property
    def total_interest_arrears(self) -> Decimal:
        return sum((item.interest_arrears for item in self.loans.values()), Decimal("0"))

    def amount_set(self) -> set[str]:
        """Canonical derived amounts produced by this engine (for S4 checks)."""
        values: set[str] = set()
        for loan in self.loans.values():
            values.add(canonical_amount(loan.principal))
            values.add(canonical_amount(loan.interest_arrears))
            values.add(canonical_amount(loan.interest_paid))
            values.add(canonical_amount(loan.excess_principal_offset))
            values.add(canonical_amount(loan.principal_paid))
            values.add(canonical_amount(loan.natural_debt_paid))
        values.add(canonical_amount(self.total_principal))
        values.add(canonical_amount(self.total_interest_arrears))
        return values


def _protected_rates(agreed: Decimal, config: ShadowEngineConfig) -> tuple[Decimal, Decimal]:
    return min(agreed, config.old_cap), min(agreed, config.new_cap)


def _accrual(principal: Decimal, start_on: date, end_on: date, agreed: Decimal,
             config: ShadowEngineConfig) -> Decimal:
    if end_on < start_on:
        raise ShadowEngineBlocked(f"invalid accrual interval {start_on}..{end_on}")
    if principal < 0:
        raise ShadowEngineBlocked("negative principal in accrual")
    old_rate, new_rate = _protected_rates(agreed, config)

    def segment(a: date, b: date, rate: Decimal) -> Decimal:
        days = (b - a).days
        return _q(principal * rate * Decimal(days) / config.day_divisor)

    if start_on >= config.boundary:
        return segment(start_on, end_on, new_rate)
    if end_on <= config.boundary:
        return segment(start_on, end_on, old_rate)
    return segment(start_on, config.boundary, old_rate) + segment(config.boundary, end_on, new_rate)


def _debt_order(debts: Mapping[str, ShadowDebt]) -> list[str]:
    def key(item: ShadowDebt) -> tuple:
        return (item.due_on if item.due_on is not None else date.max, item.debt_id)
    return [item.debt_id for item in sorted(debts.values(), key=key)]


def run_engine(
    rows: Iterable[ShadowRow],
    debts: Mapping[str, ShadowDebt],
    config: ShadowEngineConfig,
    *,
    include_disputed: Sequence[str] | None = None,
) -> ShadowEngineResult:
    """Deterministic calculation under frozen rules R1-R9.

    - ``本金出借`` rows define disbursements and must match ``debts``.
    - ``排除``/``并入`` rows are excluded; ``阻断`` rows (incl. non-CNY) are
      excluded and reported; ``争议`` rows enter only via ``include_disputed``.
    - Undesignated repayments (``debt_id is None``) are allocated by statutory
      order (R8): debts with the earliest due date first; within each debt the
      order is interest arrears then principal (R3, fees=0).
    - Old-period interest payments on a debt whose agreed rate exceeds the old
      cap apply the 24%-36% natural-debt band and offset the excess beyond 36%.
    """
    include = set(include_disputed or ())
    ordered = sorted(rows, key=lambda item: item.ordering_key)
    for debt in debts.values():
        if debt.principal <= 0 or debt.agreed_monthly_rate < 0:
            raise ShadowEngineBlocked(f"debt {debt.debt_id} has invalid principal or rate")

    borrow_rows = [row for row in ordered if row.classification == CLASS_BORROW]
    totals: dict[str, Decimal] = {}
    for row in borrow_rows:
        if row.currency != "CNY":
            raise ShadowEngineBlocked(f"borrow row {row.row_id} is not CNY")
        if row.debt_id not in debts:
            raise ShadowEngineBlocked(f"borrow row {row.row_id} references unknown debt {row.debt_id}")
        if debts[row.debt_id].evidence_pending:
            raise ShadowEngineBlocked(
                f"borrow row {row.row_id} targets debt {row.debt_id} which is "
                "evidence_pending (no supported disbursement evidence)"
            )
        if row.amount <= 0:
            raise ShadowEngineBlocked(
                f"borrow row {row.row_id} amount {row.amount} is not positive: "
                "negative outgoing transfer cannot be a 本金出借 (direction/classification mismatch)"
            )
        if row.occurred_on < debts[row.debt_id].disbursed_on:
            raise ShadowEngineBlocked(
                f"borrow row {row.row_id} date precedes the debt disbursed date"
            )
        totals[row.debt_id] = totals.get(row.debt_id, Decimal("0")) + row.amount
    for debt_id, debt in debts.items():
        if debt.evidence_pending:
            continue
        total = totals.get(debt_id, Decimal("0"))
        if total != debt.principal:
            raise ShadowEngineBlocked(
                f"debt {debt_id} cumulative borrow {total} differs from principal {debt.principal}"
            )

    active_debts = {debt_id: debt for debt_id, debt in debts.items()
                    if not debt.evidence_pending}
    state: dict[str, dict] = {
        debt_id: {
            "principal": debt.principal,
            "arrears": Decimal("0"),
            "last": debt.disbursed_on,
            "interest_paid": Decimal("0"),
            "offset": Decimal("0"),
            "principal_paid": Decimal("0"),
            "natural": Decimal("0"),
        }
        for debt_id, debt in active_debts.items()
    }

    events: list[ShadowRow] = []
    result = ShadowEngineResult(config=config)
    for row in ordered:
        classification = row.classification
        if classification.startswith(CLASS_EXCLUDE) or classification.startswith("并入"):
            result.excluded_row_ids.append(row.row_id)
            continue
        if classification.startswith(CLASS_BLOCK) or row.currency != "CNY":
            result.blocked_row_ids.append(row.row_id)
            continue
        if classification == CLASS_BORROW:
            continue
        if classification == CLASS_DISPUTED:
            if row.row_id in include:
                events.append(row)
            else:
                result.excluded_row_ids.append(row.row_id)
            continue
        if classification not in (CLASS_REPAY, CLASS_INTEREST, CLASS_THIRD_PARTY):
            raise ShadowEngineBlocked(
                f"row {row.row_id} classification {classification!r} has no calculation mapping"
            )
        events.append(row)
    events.sort(key=lambda item: item.ordering_key)

    order = _debt_order(debts)
    trace: list[ShadowTraceLine] = []
    for ordinal, row in enumerate(events, start=1):
        payment = row.amount
        if row.debt_id is not None:
            targets = [row.debt_id]
        else:
            targets = order
        touched: list[str] = []
        snapshot: dict = {}
        total_accrued = Decimal("0")
        total_interest_paid = Decimal("0")
        total_natural = Decimal("0")
        total_offset = Decimal("0")
        total_principal_paid = Decimal("0")
        for debt_id in targets:
            if payment <= 0:
                break
            st = state[debt_id]
            principal = Decimal(st["principal"])
            agreed = debts[debt_id].agreed_monthly_rate
            accrued = _accrual(principal, st["last"], row.occurred_on, agreed, config)
            st["arrears"] = Decimal(st["arrears"]) + accrued
            st["last"] = row.occurred_on
            total_accrued += accrued
            touched.append(debt_id)
            interest_paid = min(payment, Decimal(st["arrears"]))
            payment -= interest_paid
            st["arrears"] = Decimal(st["arrears"]) - interest_paid
            st["interest_paid"] = Decimal(st["interest_paid"]) + interest_paid
            total_interest_paid += interest_paid
            natural = Decimal("0")
            offset = Decimal("0")
            principal_payment = Decimal("0")
            is_interest_event = row.classification == CLASS_INTEREST
            if (
                is_interest_event
                and agreed > config.old_cap
                and row.occurred_on < config.boundary
            ):
                ceiling_effective = min(agreed, config.ceiling_36)
                band_rate = ceiling_effective - config.old_cap
                natural_capacity = _q(principal * band_rate) if band_rate > 0 else Decimal("0")
                natural = min(payment, natural_capacity)
                payment -= natural
                if payment > 0:
                    offset = payment
                    payment = Decimal("0")
                    principal -= offset
                    st["offset"] = Decimal(st["offset"]) + offset
                    st["natural"] = Decimal(st["natural"]) + natural
            elif payment > 0:
                principal_payment = payment
                payment = Decimal("0")
                principal -= principal_payment
                st["principal_paid"] = Decimal(st["principal_paid"]) + principal_payment
            if principal < 0:
                raise ShadowEngineBlocked(
                    f"row {row.row_id} payment reduced debt {debt_id} principal below zero"
                )
            st["principal"] = principal
            snapshot[debt_id] = {
                "closing_principal": principal,
                "closing_interest_arrears": Decimal(st["arrears"]),
            }
            total_natural += natural
            total_offset += offset
            total_principal_paid += principal_payment
        if payment > 0:
            raise ShadowEngineBlocked(
                f"row {row.row_id} payment {row.amount} exceeds every target debt balance"
            )
        trace.append(
            ShadowTraceLine(
                ordinal=ordinal,
                row_id=row.row_id,
                occurred_on=row.occurred_on,
                touched_debt_ids=tuple(touched),
                payment_amount=row.amount,
                accrued_interest=total_accrued,
                interest_paid=total_interest_paid,
                natural_debt_paid=total_natural,
                excess_principal_offset=total_offset,
                principal_payment=total_principal_paid,
                debt_snapshot=snapshot,
            )
        )
    result.trace = trace

    for debt_id in sorted(active_debts):
        st = state[debt_id]
        agreed = debts[debt_id].agreed_monthly_rate
        final_accrual = _accrual(
            Decimal(st["principal"]), st["last"], config.final_date, agreed, config
        )
        arrears = Decimal(st["arrears"]) + final_accrual
        result.loans[debt_id] = ShadowLoanResult(
            debt_id=debt_id,
            principal=Decimal(st["principal"]),
            interest_arrears=arrears,
            interest_paid=Decimal(st["interest_paid"]),
            excess_principal_offset=Decimal(st["offset"]),
            principal_paid=Decimal(st["principal_paid"]),
            natural_debt_paid=Decimal(st["natural"]),
        )
    return result


def golden_rows_and_debts(project_root: str | Path) -> tuple[list[ShadowRow], dict[str, ShadowDebt]]:
    """Map the authoritative 47 rows (parsed from the frozen spec) into engine inputs."""
    from case_kernel.golden_case_source import load_authoritative_case  # deferred import

    spec = load_authoritative_case(project_root)
    debts: dict[str, ShadowDebt] = {
        "L1": ShadowDebt("L1", Decimal("300000.00"), date(2019, 6, 3), Decimal("0.02"),
                         due_on=date(2020, 6, 2)),
        "L2": ShadowDebt("L2", Decimal("200000.00"), date(2019, 11, 15), Decimal("0.035"),
                         due_on=None),
    }
    rows: list[ShadowRow] = []
    for row in spec.transactions:
        classification = row.gold_classification
        debt_id = row.debt
        if classification == "本金":
            engine_class = CLASS_BORROW
        elif classification.startswith("息"):
            engine_class = CLASS_INTEREST
        elif classification.startswith("代付"):
            engine_class = CLASS_THIRD_PARTY
        elif classification.startswith("还本"):
            engine_class = CLASS_REPAY
            if "法定顺序" in classification:
                debt_id = None
        elif classification.startswith("争议"):
            engine_class = CLASS_DISPUTED
        elif classification.startswith(("排除", "并入", "阻断")):
            engine_class = classification
        else:
            raise ShadowEngineBlocked(f"unmapped golden classification: {classification}")
        rows.append(
            ShadowRow(
                row_id=str(row.row_number),
                occurred_on=_parse_date(row.occurred_at),
                channel=row.channel,
                amount=Decimal(row.amount),
                currency=row.currency,
                direction=row.direction,
                classification=engine_class,
                debt_id=debt_id,
                memo=row.summary,
            )
        )
    return rows, debts


def identity_proof(project_root: str | Path) -> dict:
    """§4.2 engine identity proof: the generic engine reproduces the frozen
    oracle calculator to the cent for every field the oracle actually emits:

    - all four scenarios: per-loan principal + interest arrears + totals
      (the oracle's summary table);
    - S-A-1 (the only scenario whose trace the oracle prints): trace length
      and per-event closing principal / closing interest arrears.

    Fields the oracle does not print (e.g. interest_paid bookkeeping of
    non-default scenarios) are excluded from comparison by design.
    """
    from case_kernel.golden_case_calculation import load_golden_outputs  # deferred import

    root = Path(project_root).resolve()
    rows, debts = golden_rows_and_debts(root)
    oracle = load_golden_outputs(root)
    config = ShadowEngineConfig(
        boundary=BOUNDARY,
        final_date=date(2025, 6, 14),
        old_cap=OLD_CAP,
        new_cap=NEW_CAP_DEFAULT,
        ceiling_36=CEILING_36,
        day_divisor=DAY_DIVISOR,
    )
    scenario_toggles = {
        "S-A-1": {"34"},
        "S-A-2": {"33", "34"},
        "S-B-1": set(),
        "S-B-2": {"33"},
    }
    checked = 0
    mismatches: list[str] = []
    for scenario_id, toggle in scenario_toggles.items():
        engine_result = run_engine(rows, debts, config, include_disputed=toggle)
        golden = oracle.scenario(scenario_id)
        golden_by_debt = {item.debt_id: item for item in golden.loans}
        for debt_id in ("L1", "L2"):
            actual = engine_result.loan(debt_id)
            expected = golden_by_debt[debt_id]
            for field_name in ("principal", "interest_arrears"):
                checked += 1
                if getattr(actual, field_name) != getattr(expected, field_name):
                    mismatches.append(
                        f"{scenario_id}.{debt_id}.{field_name}: "
                        f"engine={getattr(actual, field_name)} oracle={getattr(expected, field_name)}"
                    )
        checked += 1
        if engine_result.total_principal != golden.total_principal:
            mismatches.append(
                f"{scenario_id}.total_principal: engine={engine_result.total_principal} "
                f"oracle={golden.total_principal}"
            )
        checked += 1
        if engine_result.total_interest_arrears != golden.total_interest_arrears:
            mismatches.append(
                f"{scenario_id}.total_interest_arrears: "
                f"engine={engine_result.total_interest_arrears} "
                f"oracle={golden.total_interest_arrears}"
            )
        if scenario_id == "S-A-1":
            checked += 1
            if len(engine_result.trace) != len(golden.trace):
                mismatches.append(
                    f"S-A-1.trace.length: engine={len(engine_result.trace)} "
                    f"oracle={len(golden.trace)}"
                )
            for engine_line, golden_line in zip(engine_result.trace, golden.trace):
                for debt_id in (golden_line.debt_id,):
                    checked += 1
                    closing = engine_line.debt_snapshot[debt_id]
                    if closing["closing_principal"] != golden_line.closing_principal:
                        mismatches.append(
                            f"S-A-1.trace[{engine_line.ordinal}].{debt_id}.closing_principal: "
                            f"engine={closing['closing_principal']} "
                            f"oracle={golden_line.closing_principal}"
                        )
                    checked += 1
                    if closing["closing_interest_arrears"] != golden_line.closing_interest_arrears:
                        mismatches.append(
                            f"S-A-1.trace[{engine_line.ordinal}].{debt_id}.closing_arrears: "
                            f"engine={closing['closing_interest_arrears']} "
                            f"oracle={golden_line.closing_interest_arrears}"
                        )
    return {
        "identity_proof": "PASS" if not mismatches else "FAIL",
        "checked_fields": checked,
        "mismatches": mismatches,
        "oracle_sha256": oracle.source_sha256,
        "scenarios_checked": sorted(scenario_toggles),
    }
