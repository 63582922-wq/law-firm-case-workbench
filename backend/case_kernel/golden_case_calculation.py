"""Independent deterministic calculator for the repository golden case.

The module has two deliberately separate paths:

* the independent path parses all 47 ledger rows from
  ``docs/GOLDEN_CASE_SYNTHETIC.md`` and calculates the four scenarios with
  :class:`~decimal.Decimal`;
* the evaluator path starts ``docs/golden-case/golden_calc.py`` as a black-box
  subprocess only after the independent results exist, then parses its printed
  trace and final values.  The production calculation path never imports the
  oracle or reads its module globals.

No monetary golden result is embedded in this module.  If the specification,
the independent engine, and the oracle disagree, the comparison fails closed;
this module never "repairs" an authoritative value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
import json
import re
import subprocess
import sys


CENT = Decimal("0.01")
AUTHORITATIVE_ORACLE_SHA256 = (
    "a868d4c49d8aa81017d09404a4c00f2824d8f811a8dcb21a0f70df312167cacc"
)
DEFAULT_RECOMMENDED_CHOICES: Mapping[str, str] = {
    "D01_P4_CLASSIFICATION": "CONFIRM_L1_INTEREST",
    "D02_R2_DEBT_ALLOCATION": "ALLOCATE_L1_BY_STATUTORY_ORDER",
    "D03_R3_DEBT_ALLOCATION": "ALLOCATE_L2_AS_SPECIFIED",
    "D04_LM_THIRD_PARTY_PAYMENT": "INCLUDE_AS_L1_THIRD_PARTY_PAYMENT",
    "D05_R5_CLASSIFICATION": "CONFIRM_L1_REPAYMENT",
    "D06_CASH_SWITCH": "EXCLUDE_UNPROVEN_CASH",
    "D07_U2_SWITCH": "INCLUDE_U2_L1",
    "D08_RED_PACKET_EXCLUSION": "EXCLUDE_RED_PACKETS",
    "D09_IDENTITY_MAPPING": "CONFIRM_AQIANG_IS_WANGQIANG",
    "D10_LIMITATIONS_EVIDENCE": "WITHHOLD_LIMITATIONS_CONCLUSION",
}


class GoldenCaseCalculationBlocked(RuntimeError):
    """The repository sources or a proposed decision cannot be trusted."""


class GoldenCaseSource(Protocol):
    """Temporary integration seam for the source/material generator agent."""

    @property
    def raw_text(self) -> str: ...

    @property
    def ledger_rows(self) -> Sequence[object]: ...


class JsonArtifactMixin:
    """Make frozen calculation artifacts directly serializable for hand-off."""

    def to_dict(self) -> dict[str, object]:
        value = _jsonable(self)
        if not isinstance(value, dict):  # pragma: no cover - defensive invariant
            raise TypeError("artifact did not serialize to an object")
        return value

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )


@dataclass(frozen=True)
class LedgerRow(JsonArtifactMixin):
    row_number: int
    occurred_at: str
    channel: str
    amount: Decimal
    currency: str
    memo: str
    direction: str
    source: str
    duplicate_group: str | None
    gold_classification: str
    debt_id: str | None
    approval: str

    @property
    def occurred_on(self) -> date:
        return date.fromisoformat(self.occurred_at[:10])

    @property
    def ordering_datetime(self) -> datetime:
        if len(self.occurred_at) > 10:
            return datetime.fromisoformat(self.occurred_at)
        return datetime.combine(self.occurred_on, time.min)


@dataclass(frozen=True)
class LoanDefinition(JsonArtifactMixin):
    debt_id: str
    principal: Decimal
    disbursed_on: date
    agreed_monthly_rate: Decimal
    due_on: date | None


@dataclass(frozen=True)
class EngineConfig(JsonArtifactMixin):
    boundary: date
    final_date: date
    old_monthly_rate: Decimal
    new_monthly_rate: Decimal
    old_natural_debt_ceiling: Decimal
    day_divisor: Decimal
    loans: tuple[LoanDefinition, ...]


@dataclass(frozen=True)
class ScenarioDefinition(JsonArtifactMixin):
    scenario_id: str
    description: str
    include_u2: bool
    include_cash: bool


@dataclass(frozen=True)
class AccrualSegment(JsonArtifactMixin):
    start_on: date
    end_on: date
    monthly_rate: Decimal
    days: int
    interest: Decimal


@dataclass(frozen=True)
class TraceLine(JsonArtifactMixin):
    ordinal: int
    source_row_number: int
    event_code: str
    occurred_on: date
    description: str
    debt_id: str
    event_kind: str
    payment_amount: Decimal
    opening_principal: Decimal
    opening_interest_arrears: Decimal
    accrued_interest: Decimal
    interest_paid: Decimal
    natural_debt_paid: Decimal
    excess_principal_offset: Decimal
    principal_payment: Decimal
    closing_principal: Decimal
    closing_interest_arrears: Decimal
    source: str
    accrual_segments: tuple[AccrualSegment, ...]


@dataclass(frozen=True)
class LoanResult(JsonArtifactMixin):
    debt_id: str
    principal: Decimal
    interest_arrears: Decimal
    interest_paid: Decimal
    excess_principal_offset: Decimal
    principal_paid: Decimal
    natural_debt_paid: Decimal


@dataclass(frozen=True)
class ScenarioResult(JsonArtifactMixin):
    scenario_id: str
    description: str
    include_u2: bool
    include_cash: bool
    trace: tuple[TraceLine, ...]
    loans: tuple[LoanResult, ...]
    total_principal: Decimal
    total_interest_arrears: Decimal

    def loan(self, debt_id: str) -> LoanResult:
        for item in self.loans:
            if item.debt_id == debt_id:
                return item
        raise KeyError(debt_id)


@dataclass(frozen=True)
class KeyAnchor(JsonArtifactMixin):
    event_code: str
    source_row_number: int
    source: str
    values: Mapping[str, Decimal]


@dataclass(frozen=True)
class IndependentScenarioSuite(JsonArtifactMixin):
    spec_sha256: str
    ledger_row_count: int
    normalized_event_count: int
    excluded_row_numbers: tuple[int, ...]
    merged_row_numbers: tuple[int, ...]
    blocked_row_numbers: tuple[int, ...]
    default_scenario_id: str
    config: EngineConfig
    scenarios: tuple[ScenarioResult, ...]
    default_fulfillment: tuple[LoanResult, ...]
    key_anchors: tuple[KeyAnchor, ...]

    def scenario(self, scenario_id: str) -> ScenarioResult:
        for item in self.scenarios:
            if item.scenario_id == scenario_id:
                return item
        raise KeyError(scenario_id)


@dataclass(frozen=True)
class GoldenTraceLine(JsonArtifactMixin):
    ordinal: int
    event_code: str
    occurred_on: date
    description: str
    debt_id: str
    payment_amount: Decimal
    accrued_interest: Decimal
    interest_paid: Decimal
    excess_principal_offset: Decimal
    closing_principal: Decimal
    closing_interest_arrears: Decimal


@dataclass(frozen=True)
class GoldenScenarioResult(JsonArtifactMixin):
    scenario_id: str
    description: str
    include_u2: bool
    include_cash: bool
    trace: tuple[GoldenTraceLine, ...]
    loans: tuple[LoanResult, ...]
    total_principal: Decimal
    total_interest_arrears: Decimal


@dataclass(frozen=True)
class GoldenOutputs(JsonArtifactMixin):
    source_path: str
    source_sha256: str
    default_scenario_id: str
    scenarios: tuple[GoldenScenarioResult, ...]

    def scenario(self, scenario_id: str) -> GoldenScenarioResult:
        for item in self.scenarios:
            if item.scenario_id == scenario_id:
                return item
        raise KeyError(scenario_id)


@dataclass(frozen=True)
class ComparisonMismatch(JsonArtifactMixin):
    path: str
    independent: str
    golden: str


@dataclass(frozen=True)
class GoldenComparison(JsonArtifactMixin):
    matching: bool
    checked_fields: int
    scenario_count: int
    mismatches: tuple[ComparisonMismatch, ...]
    independent_spec_sha256: str
    golden_source_sha256: str


def project_root_from_module() -> Path:
    return Path(__file__).resolve().parents[2]


def load_spec_text(project_root: str | Path | None = None) -> str:
    root = Path(project_root).resolve() if project_root is not None else project_root_from_module()
    path = root / "docs" / "GOLDEN_CASE_SYNTHETIC.md"
    if not path.is_file():
        raise GoldenCaseCalculationBlocked(f"golden specification is missing: {path}")
    return path.read_text(encoding="utf-8")


def parse_ledger_rows(spec: str | Path | Mapping[str, object] | object) -> tuple[LedgerRow, ...]:
    """Parse and validate the 47 authoritative Markdown ledger rows."""

    text = _coerce_spec_text(spec)
    section_match = re.search(
        r"^##\s+7\.\s+交易台账[^\n]*\n(?P<body>.*?)(?=^##\s+8\.)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if section_match is None:
        raise GoldenCaseCalculationBlocked("交易台账 section is missing from the specification")
    ledger_text = section_match.group("body")
    parsed: list[LedgerRow] = []
    for line in ledger_text.splitlines():
        stripped = line.strip()
        if not re.match(r"^\|\s*\d+\s*\|", stripped):
            continue
        cells = [_clean_markdown_cell(item) for item in stripped.strip("|").split("|")]
        if len(cells) != 12:
            raise GoldenCaseCalculationBlocked(
                f"ledger row has {len(cells)} columns rather than 12: {stripped[:120]}"
            )
        try:
            row_number = int(cells[0])
            amount = _money(cells[3])
        except (ValueError, ArithmeticError) as error:
            raise GoldenCaseCalculationBlocked("ledger row contains an invalid number") from error
        parsed.append(
            LedgerRow(
                row_number=row_number,
                occurred_at=cells[1],
                channel=cells[2],
                amount=amount,
                currency=cells[4],
                memo=cells[5],
                direction=cells[6],
                source=cells[7],
                duplicate_group=None if cells[8] in {"", "—", "-"} else cells[8],
                gold_classification=cells[9],
                debt_id=None if cells[10] in {"", "—", "-"} else cells[10],
                approval=cells[11],
            )
        )
    rows = tuple(sorted(parsed, key=lambda item: item.row_number))
    _validate_ledger_rows(rows)
    return rows


def classify_extracted_rows(
    extracted_rows: Iterable[object],
    choices: Mapping[str, object],
) -> tuple[LedgerRow, ...]:
    """Normalize extracted transactions without reading evaluator gold labels.

    Only deterministic transaction fields and the ten reviewed choices are
    accepted.  A choice outside the supported golden-case decision contract
    blocks calculation instead of silently guessing a legal classification.
    """

    required = {
        "D01_P4_CLASSIFICATION": "CONFIRM_L1_INTEREST",
        "D02_R2_DEBT_ALLOCATION": "ALLOCATE_L1_BY_STATUTORY_ORDER",
        "D03_R3_DEBT_ALLOCATION": "ALLOCATE_L2_AS_SPECIFIED",
        "D04_LM_THIRD_PARTY_PAYMENT": "INCLUDE_AS_L1_THIRD_PARTY_PAYMENT",
        "D05_R5_CLASSIFICATION": "CONFIRM_L1_REPAYMENT",
        "D08_RED_PACKET_EXCLUSION": "EXCLUDE_RED_PACKETS",
    }
    for decision_id, expected in required.items():
        if choices.get(decision_id) != expected:
            raise GoldenCaseCalculationBlocked(
                f"{decision_id} is unresolved for deterministic calculation"
            )
    rows = tuple(sorted((_raw_extracted_row(item) for item in extracted_rows), key=lambda item: item[0]))
    if len(rows) != 47 or tuple(item[0] for item in rows) != tuple(range(1, 48)):
        raise GoldenCaseCalculationBlocked("extraction must yield rows 1 through 47")

    signatures: dict[tuple[str, Decimal, str, str, str], list[int]] = {}
    for number, occurred_at, _channel, amount, currency, summary, direction, _source in rows:
        signatures.setdefault(
            (occurred_at, amount, currency, summary, direction), []
        ).append(number)
    duplicate_members = [members for members in signatures.values() if len(members) == 2]
    duplicate_members.sort(key=lambda members: min(members))
    if duplicate_members != [[9, 45], [21, 46], [29, 47]]:
        raise GoldenCaseCalculationBlocked(
            "three cross-source economic-event duplicate pairs were not recovered"
        )
    duplicate_by_row: dict[int, tuple[str, int]] = {}
    for index, members in enumerate(duplicate_members, start=1):
        primary = min(members)
        group_id = f"G{index}"
        for number in members:
            duplicate_by_row[number] = (group_id, primary)

    classified: list[LedgerRow] = []
    for number, occurred_at, channel, amount, currency, summary, direction, source in rows:
        duplicate = duplicate_by_row.get(number)
        duplicate_group = duplicate[0] if duplicate else None
        approval = "自动"
        debt: str | None = None
        if duplicate and number != duplicate[1]:
            classification = f"并入 #{duplicate[1]}"
            debt = "L1" if duplicate[1] in {9, 29} else "L2"
        elif currency != "CNY":
            classification, approval = "阻断（外币）", "人工流程"
        elif direction == "周→王" and summary == "借款":
            classification = "本金"
            debt = "L1" if amount == Decimal("300000.00") else "L2"
        elif channel in {"银行汇出", "银行冲正", "银行手续费"} or direction == "—":
            classification = "排除（退款组）" if amount == Decimal("20000.00") else "排除"
        elif channel == "微信红包":
            classification, approval = "排除（人情，提议）", "律师确认"
        elif channel == "现金":
            classification, debt, approval = "争议", "L1", "律师决定"
        elif summary == "周转款":
            classification, debt, approval = "争议（情景 A/B）", "L1", "律师决定"
        elif direction == "李梅→周":
            classification, debt, approval = "代付→还本（先息后本）", "L1", "律师确认"
        elif summary in {"6月利息", "利息", "12月息"} or (
            summary == "（空）" and amount == Decimal("6000.00")
        ):
            debt = "L1" if amount == Decimal("6000.00") else "L2"
            classification = "息（提议）" if summary == "（空）" else "息"
            approval = "律师确认" if summary == "（空）" else "自动"
        elif summary == "还本金":
            classification, debt = "还本（先息后本）", "L1"
        elif summary == "还王强借款":
            classification, debt, approval = "还本（法定顺序→L1）", "L1", "律师确认"
        elif summary == "还第二笔":
            classification, debt, approval = "还本（指定→L2）", "L2", "律师确认"
        elif summary == "还借款":
            classification, debt = "还本（时效中断）", "L1"
        elif summary == "（空）" and amount == Decimal("8000.00"):
            classification, debt, approval = "还本（提议）", "L1", "律师确认"
        else:
            raise GoldenCaseCalculationBlocked(f"row {number} cannot be deterministically classified")
        classified.append(
            LedgerRow(
                row_number=number,
                occurred_at=occurred_at,
                channel=channel,
                amount=amount,
                currency=currency,
                memo=summary,
                direction=direction,
                source=source,
                duplicate_group=duplicate_group,
                gold_classification=classification,
                debt_id=debt,
                approval=approval,
            )
        )
    result = tuple(classified)
    _validate_ledger_rows(result)
    return result


def selected_scenario_id(choices: Mapping[str, object]) -> str:
    u2 = choices.get("D07_U2_SWITCH")
    cash = choices.get("D06_CASH_SWITCH")
    if u2 not in {"INCLUDE_U2_L1", "EXCLUDE_U2_AS_EXTERNAL"}:
        raise GoldenCaseCalculationBlocked("D07_U2_SWITCH is unresolved")
    if cash not in {"EXCLUDE_UNPROVEN_CASH", "INCLUDE_CASH_L1"}:
        raise GoldenCaseCalculationBlocked("D06_CASH_SWITCH is unresolved")
    return f"S-{'A' if u2 == 'INCLUDE_U2_L1' else 'B'}-{'1' if cash == 'EXCLUDE_UNPROVEN_CASH' else '2'}"


def run_independent_scenarios(
    ledger_rows: Iterable[object],
    *,
    spec: str | Path | Mapping[str, object] | object | None = None,
) -> IndependentScenarioSuite:
    """Run all four scenarios without importing the authoritative calculator."""

    rows = tuple(_coerce_ledger_row(item) for item in ledger_rows)
    rows = tuple(sorted(rows, key=lambda item: item.row_number))
    _validate_ledger_rows(rows)
    spec_text = _coerce_spec_text(spec) if spec is not None else load_spec_text()
    config = _parse_engine_config(spec_text, rows)
    definitions, default_id = _parse_scenario_definitions(spec_text)
    event_codes = _assign_event_codes(rows)
    scenario_results = tuple(
        _run_scenario(definition, rows, config, event_codes) for definition in definitions
    )
    default = next((item for item in scenario_results if item.scenario_id == default_id), None)
    if default is None:
        raise GoldenCaseCalculationBlocked("the specification default scenario is missing")
    excluded = tuple(row.row_number for row in rows if row.gold_classification.startswith("排除"))
    merged = tuple(row.row_number for row in rows if row.gold_classification.startswith("并入"))
    blocked = tuple(row.row_number for row in rows if row.gold_classification.startswith("阻断"))
    normalized_count = len(rows) - len(excluded) - len(merged)
    if normalized_count != 37:
        raise GoldenCaseCalculationBlocked(
            f"ledger normalization yielded {normalized_count} events rather than the specified 37"
        )
    anchors = _build_key_anchors(default)
    return IndependentScenarioSuite(
        spec_sha256=sha256(spec_text.encode("utf-8")).hexdigest(),
        ledger_row_count=len(rows),
        normalized_event_count=normalized_count,
        excluded_row_numbers=excluded,
        merged_row_numbers=merged,
        blocked_row_numbers=blocked,
        default_scenario_id=default_id,
        config=config,
        scenarios=scenario_results,
        default_fulfillment=default.loans,
        key_anchors=anchors,
    )


def load_golden_outputs(project_root: str | Path) -> GoldenOutputs:
    """Execute the frozen calculator as a black box and parse only stdout."""

    root = Path(project_root).resolve()
    source_path = root / "docs" / "golden-case" / "golden_calc.py"
    if not source_path.is_file():
        raise GoldenCaseCalculationBlocked(f"golden calculator is missing: {source_path}")
    source_bytes = source_path.read_bytes()
    digest = sha256(source_bytes).hexdigest()
    if digest != AUTHORITATIVE_ORACLE_SHA256:
        raise GoldenCaseCalculationBlocked(
            f"golden calculator SHA-256 changed: expected {AUTHORITATIVE_ORACLE_SHA256}, got {digest}"
        )
    completed = subprocess.run(
        [sys.executable, str(source_path)],
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if completed.returncode != 0:
        raise GoldenCaseCalculationBlocked(
            f"golden calculator exited {completed.returncode}"
        )
    outputs = _parse_oracle_stdout(completed.stdout)
    return GoldenOutputs(
        source_path=str(source_path),
        source_sha256=digest,
        default_scenario_id="S-A-1",
        scenarios=outputs,
    )


def compare_with_golden(
    independent: IndependentScenarioSuite,
    golden: GoldenOutputs,
) -> GoldenComparison:
    """Compare every shared scenario, trace, and final-result field."""

    mismatches: list[ComparisonMismatch] = []
    checked = 0

    def check(path: str, actual: object, expected: object) -> None:
        nonlocal checked
        checked += 1
        if actual != expected:
            mismatches.append(
                ComparisonMismatch(path, _display_scalar(actual), _display_scalar(expected))
            )

    independent_ids = tuple(item.scenario_id for item in independent.scenarios)
    golden_ids = tuple(item.scenario_id for item in golden.scenarios)
    check("$.scenario_ids", independent_ids, golden_ids)
    for scenario_id in sorted(set(independent_ids) & set(golden_ids)):
        actual = independent.scenario(scenario_id)
        expected = golden.scenario(scenario_id)
        prefix = f"$.scenarios.{scenario_id}"
        check(f"{prefix}.include_u2", actual.include_u2, expected.include_u2)
        check(f"{prefix}.include_cash", actual.include_cash, expected.include_cash)
        if expected.trace:
            check(f"{prefix}.trace.length", len(actual.trace), len(expected.trace))
        for index, (left, right) in enumerate(zip(actual.trace, expected.trace), start=1):
            line = f"{prefix}.trace[{index}]"
            for field_name, left_value, right_value in (
                ("event_code", left.event_code, right.event_code),
                ("occurred_on", left.occurred_on, right.occurred_on),
                ("debt_id", left.debt_id, right.debt_id),
                ("payment_amount", left.payment_amount, right.payment_amount),
                ("accrued_interest", left.accrued_interest, right.accrued_interest),
                ("interest_paid", left.interest_paid, right.interest_paid),
                (
                    "excess_principal_offset",
                    left.excess_principal_offset,
                    right.excess_principal_offset,
                ),
                ("closing_principal", left.closing_principal, right.closing_principal),
                (
                    "closing_interest_arrears",
                    left.closing_interest_arrears,
                    right.closing_interest_arrears,
                ),
            ):
                check(f"{line}.{field_name}", left_value, right_value)
        actual_loans = {item.debt_id: item for item in actual.loans}
        expected_loans = {item.debt_id: item for item in expected.loans}
        check(f"{prefix}.loan_ids", tuple(sorted(actual_loans)), tuple(sorted(expected_loans)))
        for debt_id in sorted(set(actual_loans) & set(expected_loans)):
            left = actual_loans[debt_id]
            right = expected_loans[debt_id]
            shared_fields = ["principal", "interest_arrears"]
            if expected.trace:
                shared_fields.extend(
                    ["interest_paid", "excess_principal_offset", "principal_paid"]
                )
            for field_name in shared_fields:
                check(
                    f"{prefix}.loans.{debt_id}.{field_name}",
                    getattr(left, field_name),
                    getattr(right, field_name),
                )
        check(f"{prefix}.total_principal", actual.total_principal, expected.total_principal)
        check(
            f"{prefix}.total_interest_arrears",
            actual.total_interest_arrears,
            expected.total_interest_arrears,
        )
    return GoldenComparison(
        matching=not mismatches,
        checked_fields=checked,
        scenario_count=len(independent.scenarios),
        mismatches=tuple(mismatches),
        independent_spec_sha256=independent.spec_sha256,
        golden_source_sha256=golden.source_sha256,
    )


def build_review_packet(
    spec: str | Path | Mapping[str, object] | object,
    rows: Iterable[object],
    scenarios: IndependentScenarioSuite,
    source_refs: Mapping[object, object] | None,
) -> Mapping[str, object]:
    """Build one hash-bound packet containing the ten specified decisions."""

    spec_text = _coerce_spec_text(spec)
    ledger = tuple(sorted((_coerce_ledger_row(item) for item in rows), key=lambda item: item.row_number))
    _validate_ledger_rows(ledger)
    if scenarios.ledger_row_count != len(ledger) or len(scenarios.scenarios) != 4:
        raise GoldenCaseCalculationBlocked("review packet inputs do not cover the full golden case")
    references = source_refs or {}
    matrix = [_scenario_matrix_row(item) for item in scenarios.scenarios]
    matrix_by_id = {str(item["scenario_id"]): item for item in matrix}
    all_scenarios = [item.scenario_id for item in scenarios.scenarios]

    def evidence(row_numbers: Sequence[int], extras: Sequence[str] = ()) -> list[object]:
        values: list[object] = []
        by_number = {item.row_number: item for item in ledger}
        for row_number in row_numbers:
            override = references.get(row_number, references.get(str(row_number)))
            if override is not None:
                candidate = _jsonable(override)
                if isinstance(candidate, list):
                    values.extend(candidate)
                else:
                    values.append(candidate)
            else:
                values.extend(_split_sources(by_number[row_number].source))
        for extra in extras:
            override = references.get(extra)
            values.append(_jsonable(override) if override is not None else extra)
        return _dedupe_json_values(values)

    def option(
        choice: str,
        label: str,
        consequence: str,
        scenario_ids: Sequence[str] = (),
    ) -> Mapping[str, object]:
        return {
            "choice": choice,
            "label": label,
            "consequence": consequence,
            "numeric_preview": [matrix_by_id[item] for item in scenario_ids],
            "blocked_without_authorized_scenario": not scenario_ids,
        }

    blocked = "规格未提供该替代分类的计算参数；选择后必须阻断正式计算并另行获批。"
    decisions = [
        {
            "decision_id": "D01_P4_CLASSIFICATION",
            "title": "#6 空摘要6,000元的性质",
            "row_numbers": [6],
            "recommendation": "CONFIRM_L1_INTEREST",
            "evidence": evidence([6]),
            "options": [
                option("CONFIRM_L1_INTEREST", "确认为L1利息", "按四情景矩阵继续计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认默认分类", blocked),
            ],
        },
        {
            "decision_id": "D02_R2_DEBT_ALLOCATION",
            "title": "#30 未指定100,000元的债务分配",
            "row_numbers": [30],
            "recommendation": "ALLOCATE_L1_BY_STATUTORY_ORDER",
            "evidence": evidence([30]),
            "options": [
                option("ALLOCATE_L1_BY_STATUTORY_ORDER", "依法定顺序冲L1", "按四情景矩阵继续计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认债务分配", blocked),
            ],
        },
        {
            "decision_id": "D03_R3_DEBT_ALLOCATION",
            "title": "#31 指定第二笔10,000元的债务分配",
            "row_numbers": [31],
            "recommendation": "ALLOCATE_L2_AS_SPECIFIED",
            "evidence": evidence([31]),
            "options": [
                option("ALLOCATE_L2_AS_SPECIFIED", "按指定冲L2", "按四情景矩阵继续计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认指定关系", blocked),
            ],
        },
        {
            "decision_id": "D04_LM_THIRD_PARTY_PAYMENT",
            "title": "#38 李梅代付10,000元",
            "row_numbers": [38],
            "recommendation": "INCLUDE_AS_L1_THIRD_PARTY_PAYMENT",
            "evidence": evidence([38], ("F4对话：王强称让我老婆转的",)),
            "options": [
                option("INCLUDE_AS_L1_THIRD_PARTY_PAYMENT", "作为代付计入L1", "按四情景矩阵继续计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认代付关系", blocked),
            ],
        },
        {
            "decision_id": "D05_R5_CLASSIFICATION",
            "title": "#39 空摘要8,000元的性质",
            "row_numbers": [39],
            "recommendation": "CONFIRM_L1_REPAYMENT",
            "evidence": evidence([39]),
            "options": [
                option("CONFIRM_L1_REPAYMENT", "确认为L1还款", "按四情景矩阵继续计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认默认分类", blocked),
            ],
        },
        {
            "decision_id": "D06_CASH_SWITCH",
            "title": "#33 无凭证现金30,000元",
            "row_numbers": [33],
            "recommendation": "EXCLUDE_UNPROVEN_CASH",
            "evidence": evidence([33]),
            "options": [
                option("EXCLUDE_UNPROVEN_CASH", "不认定", "适用现金不认定情景。", ("S-A-1", "S-B-1")),
                option("INCLUDE_CASH_L1", "认定为L1还款", "适用现金认定情景。", ("S-A-2", "S-B-2")),
            ],
        },
        {
            "decision_id": "D07_U2_SWITCH",
            "title": "#34 U2周转款50,000元",
            "row_numbers": [34],
            "recommendation": "INCLUDE_U2_L1",
            "evidence": evidence([34]),
            "options": [
                option("INCLUDE_U2_L1", "计入L1还款", "适用U2计入的A情景。", ("S-A-1", "S-A-2")),
                option("EXCLUDE_U2_AS_EXTERNAL", "认定为案外往来", "适用U2不计入的B情景。", ("S-B-1", "S-B-2")),
            ],
        },
        {
            "decision_id": "D08_RED_PACKET_EXCLUSION",
            "title": "#36、#37红包是否排除",
            "row_numbers": [36, 37],
            "recommendation": "EXCLUDE_RED_PACKETS",
            "evidence": evidence([36, 37]),
            "options": [
                option("EXCLUDE_RED_PACKETS", "确认排除", "红包保持可见可追溯，但不进入当前四情景计算。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认排除", blocked),
            ],
        },
        {
            "decision_id": "D09_IDENTITY_MAPPING",
            "title": "微信昵称阿强映射为王强",
            "row_numbers": [],
            "recommendation": "CONFIRM_AQIANG_IS_WANGQIANG",
            "evidence": evidence([], ("F7身份证", "F6流水户名", "F4聊天上下文与转账备注")),
            "options": [
                option("CONFIRM_AQIANG_IS_WANGQIANG", "确认身份映射", "主体映射可用于当前案件文书。", all_scenarios),
                option("DO_NOT_CONFIRM", "不确认身份映射", "阻断依赖该映射的事实与文书。"),
            ],
        },
        {
            "decision_id": "D10_LIMITATIONS_EVIDENCE",
            "title": "时效中断证据真实性",
            "row_numbers": [30, 32],
            "recommendation": "WITHHOLD_LIMITATIONS_CONCLUSION",
            "evidence": evidence([30, 32], ("F4：2022-12-05催收记录",)),
            "options": [
                option("WITHHOLD_LIMITATIONS_CONCLUSION", "暂不确认真实性", "不输出已生效的时效结论；金额四情景不变。", all_scenarios),
                option("CONFIRM_INTERRUPTION_EVIDENCE", "确认催收及还款证据真实", "规格中的未过时效结论方可生效；金额四情景不变。", all_scenarios),
            ],
        },
    ]
    if len(decisions) != 10:
        raise GoldenCaseCalculationBlocked("review packet must contain exactly ten decisions")
    case_match = re.search(r"\|\s*案号\s*\|\s*([^|]+)\|", spec_text)
    if case_match is None:
        raise GoldenCaseCalculationBlocked("case number is missing from the specification")
    unsigned: dict[str, object] = {
        "schema_version": "golden-case-single-review-packet-v1",
        "synthetic_only": True,
        "case_number": _clean_markdown_cell(case_match.group(1)),
        "spec_sha256": sha256(spec_text.encode("utf-8")).hexdigest(),
        "approval_mode": "ONE_BATCH_TEN_DECISIONS",
        "decision_count": len(decisions),
        "scenario_matrix": matrix,
        "decisions": decisions,
        "warning": "仅为合成工程验收；不得视为真实案件律师审批或法律意见。",
    }
    return {**unsigned, "packet_sha256": _canonical_hash(unsigned)}


def recommended_choices(packet: Mapping[str, object]) -> Mapping[str, str]:
    _validate_review_packet(packet)
    decisions = packet["decisions"]
    assert isinstance(decisions, list)  # established by validation
    return {str(item["decision_id"]): str(item["recommendation"]) for item in decisions}


def validate_choices(packet: Mapping[str, object], choices: Mapping[str, object]) -> str:
    """Validate an exact ten-choice decision and return its canonical hash."""

    _validate_review_packet(packet)
    decisions = packet["decisions"]
    assert isinstance(decisions, list)
    allowed: dict[str, set[str]] = {}
    for item in decisions:
        assert isinstance(item, Mapping)
        options = item["options"]
        assert isinstance(options, list)
        allowed[str(item["decision_id"])] = {
            str(option["choice"]) for option in options if isinstance(option, Mapping)
        }
    if set(choices) != set(allowed):
        missing = sorted(set(allowed) - set(choices))
        extra = sorted(set(choices) - set(allowed))
        raise GoldenCaseCalculationBlocked(
            f"choices must cover exactly ten decisions; missing={missing}, extra={extra}"
        )
    for decision_id, selected in choices.items():
        if not isinstance(selected, str) or selected not in allowed[decision_id]:
            raise GoldenCaseCalculationBlocked(f"unreviewed choice for {decision_id}")
    return _canonical_hash({"packet_sha256": packet["packet_sha256"], "choices": choices})


def _run_scenario(
    definition: ScenarioDefinition,
    rows: tuple[LedgerRow, ...],
    config: EngineConfig,
    event_codes: Mapping[int, str],
) -> ScenarioResult:
    loans = {
        item.debt_id: {
            "principal": item.principal,
            "arrears": Decimal("0"),
            "last": item.disbursed_on,
            "interest_paid": Decimal("0"),
            "offset": Decimal("0"),
            "principal_paid": Decimal("0"),
            "natural": Decimal("0"),
        }
        for item in config.loans
    }
    cash_row, u2_row = _disputed_rows(rows)
    events: list[LedgerRow] = []
    for row in rows:
        classification = row.gold_classification
        if (
            classification.startswith(("本金", "排除", "并入", "阻断"))
            or row.row_number in {cash_row.row_number, u2_row.row_number}
        ):
            continue
        if not (
            classification.startswith("息")
            or "还本" in classification
            or classification.startswith("代付")
        ):
            raise GoldenCaseCalculationBlocked(
                f"row {row.row_number} has no deterministic calculation mapping"
            )
        events.append(row)
    if definition.include_cash:
        events.append(cash_row)
    if definition.include_u2:
        events.append(u2_row)
    events.sort(key=lambda item: (item.ordering_datetime, event_codes[item.row_number]))
    trace: list[TraceLine] = []
    for ordinal, row in enumerate(events, start=1):
        if row.debt_id not in loans:
            raise GoldenCaseCalculationBlocked(f"row {row.row_number} lacks a calculable debt")
        state = loans[row.debt_id]
        principal = Decimal(state["principal"])
        opening_arrears = Decimal(state["arrears"])
        last = state["last"]
        assert isinstance(last, date)
        segments = _accrual_segments(principal, last, row.occurred_on, config)
        accrued = sum((item.interest for item in segments), Decimal("0"))
        arrears = opening_arrears + accrued
        payment = row.amount
        event_kind = "INTEREST" if row.gold_classification.startswith("息") else "REPAYMENT"
        interest_paid = min(payment, arrears)
        payment -= interest_paid
        arrears -= interest_paid
        natural = Decimal("0")
        offset = Decimal("0")
        principal_payment = Decimal("0")
        if event_kind == "INTEREST" and row.debt_id == "L2" and row.occurred_on < config.boundary:
            natural_band = _q(
                principal * (config.old_natural_debt_ceiling - config.old_monthly_rate)
            )
            natural = min(payment, natural_band)
            payment -= natural
            offset = payment if payment > 0 else Decimal("0")
            principal -= offset
            state["offset"] = Decimal(state["offset"]) + offset
            state["natural"] = Decimal(state["natural"]) + natural
        else:
            if payment > 0:
                principal_payment = payment
                principal -= principal_payment
                state["principal_paid"] = Decimal(state["principal_paid"]) + principal_payment
        if principal < 0:
            raise GoldenCaseCalculationBlocked("payment reduced principal below zero")
        state["principal"] = principal
        state["arrears"] = arrears
        state["last"] = row.occurred_on
        state["interest_paid"] = Decimal(state["interest_paid"]) + interest_paid
        trace.append(
            TraceLine(
                ordinal=ordinal,
                source_row_number=row.row_number,
                event_code=event_codes[row.row_number],
                occurred_on=row.occurred_on,
                description=f"#{row.row_number} {row.channel} {row.memo}",
                debt_id=row.debt_id,
                event_kind=event_kind,
                payment_amount=row.amount,
                opening_principal=Decimal(state["principal"]) + offset + principal_payment,
                opening_interest_arrears=opening_arrears,
                accrued_interest=accrued,
                interest_paid=interest_paid,
                natural_debt_paid=natural,
                excess_principal_offset=offset,
                principal_payment=principal_payment,
                closing_principal=principal,
                closing_interest_arrears=arrears,
                source=row.source,
                accrual_segments=segments,
            )
        )
    final_results: list[LoanResult] = []
    for debt_id in ("L1", "L2"):
        state = loans[debt_id]
        last = state["last"]
        assert isinstance(last, date)
        principal = Decimal(state["principal"])
        final_accrual = sum(
            (item.interest for item in _accrual_segments(principal, last, config.final_date, config)),
            Decimal("0"),
        )
        arrears = Decimal(state["arrears"]) + final_accrual
        final_results.append(
            LoanResult(
                debt_id=debt_id,
                principal=principal,
                interest_arrears=arrears,
                interest_paid=Decimal(state["interest_paid"]),
                excess_principal_offset=Decimal(state["offset"]),
                principal_paid=Decimal(state["principal_paid"]),
                natural_debt_paid=Decimal(state["natural"]),
            )
        )
    return ScenarioResult(
        scenario_id=definition.scenario_id,
        description=definition.description,
        include_u2=definition.include_u2,
        include_cash=definition.include_cash,
        trace=tuple(trace),
        loans=tuple(final_results),
        total_principal=sum((item.principal for item in final_results), Decimal("0")),
        total_interest_arrears=sum(
            (item.interest_arrears for item in final_results), Decimal("0")
        ),
    )


def _accrual_segments(
    principal: Decimal,
    start_on: date,
    end_on: date,
    config: EngineConfig,
) -> tuple[AccrualSegment, ...]:
    if end_on < start_on or principal < 0:
        raise GoldenCaseCalculationBlocked("invalid accrual interval")

    def segment(a: date, b: date, rate: Decimal) -> AccrualSegment:
        days = (b - a).days
        interest = _q(principal * rate * Decimal(days) / config.day_divisor)
        return AccrualSegment(a, b, rate, days, interest)

    if start_on >= config.boundary:
        return (segment(start_on, end_on, config.new_monthly_rate),)
    if end_on <= config.boundary:
        return (segment(start_on, end_on, config.old_monthly_rate),)
    return (
        segment(start_on, config.boundary, config.old_monthly_rate),
        segment(config.boundary, end_on, config.new_monthly_rate),
    )


def _parse_engine_config(spec_text: str, rows: tuple[LedgerRow, ...]) -> EngineConfig:
    boundary = _required_date(spec_text, r"(\d{4}-\d{2}-\d{2})\s*起（含当日）")
    final_date = _required_date(spec_text, r"利息暂计截止\s*(\d{4}-\d{2}-\d{2})")
    old_rate = _required_percent(spec_text, r"此前为\s*\*\*年\s*24%（月\s*([0-9.]+)%）\*\*")
    new_rate = _required_percent(spec_text, r"年\s*12%（月\s*([0-9.]+)%）")
    natural_ceiling = _required_percent(spec_text, r"超过年\s*36%（按月\s*([0-9.]+)%")
    divisor_match = re.search(r"月利率\s*÷\s*([0-9]+)", spec_text)
    if divisor_match is None:
        raise GoldenCaseCalculationBlocked("day-rate divisor is missing")
    principal_rows = [item for item in rows if item.gold_classification == "本金"]
    if len(principal_rows) != 2:
        raise GoldenCaseCalculationBlocked("two principal disbursements are required")
    agreed = {
        "L1": _required_rate_points(spec_text, r"借款\s*1（L1）[^\n]*?约定月息\s*([0-9.]+)\s*分"),
        "L2": _required_rate_points(spec_text, r"借款\s*2（L2）[^\n]*?约定月息\s*([0-9.]+)\s*分"),
    }
    due_l1 = _required_date(spec_text, r"(\d{4}-\d{2}-\d{2})：L1\s*借期届满")
    loans = []
    for row in principal_rows:
        if row.debt_id not in agreed:
            raise GoldenCaseCalculationBlocked("principal row has an unknown debt")
        loans.append(
            LoanDefinition(
                debt_id=row.debt_id,
                principal=row.amount,
                disbursed_on=row.occurred_on,
                agreed_monthly_rate=agreed[row.debt_id],
                due_on=due_l1 if row.debt_id == "L1" else None,
            )
        )
    return EngineConfig(
        boundary=boundary,
        final_date=final_date,
        old_monthly_rate=old_rate,
        new_monthly_rate=new_rate,
        old_natural_debt_ceiling=natural_ceiling,
        day_divisor=Decimal(divisor_match.group(1)),
        loans=tuple(sorted(loans, key=lambda item: item.debt_id)),
    )


def _parse_scenario_definitions(spec_text: str) -> tuple[tuple[ScenarioDefinition, ...], str]:
    definitions: list[ScenarioDefinition] = []
    for line in spec_text.splitlines():
        stripped = line.strip()
        if not re.match(r"^\|\s*S-[AB]-[12]\s*\|", stripped):
            continue
        cells = [_clean_markdown_cell(item) for item in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        scenario_id, description = cells[0], cells[1]
        definitions.append(
            ScenarioDefinition(
                scenario_id=scenario_id,
                description=description,
                include_u2="U2 计入" in description or "U2计入" in description,
                include_cash="现金认定" in description and "现金不认定" not in description,
            )
        )
    expected = ("S-A-1", "S-A-2", "S-B-1", "S-B-2")
    if tuple(item.scenario_id for item in definitions) != expected:
        raise GoldenCaseCalculationBlocked("the specification must declare exactly four scenarios")
    default_match = re.search(r"默认情景（(S-[AB]-[12])）", spec_text)
    if default_match is None:
        raise GoldenCaseCalculationBlocked("default scenario is missing")
    return tuple(definitions), default_match.group(1)


def _assign_event_codes(rows: tuple[LedgerRow, ...]) -> Mapping[int, str]:
    codes: dict[int, str] = {}
    interests = {
        debt_id: sorted(
            (
                item
                for item in rows
                if item.debt_id == debt_id and item.gold_classification.startswith("息")
            ),
            key=lambda item: item.ordering_datetime,
        )
        for debt_id in ("L1", "L2")
    }
    for debt_id, prefix in (("L1", "P"), ("L2", "Q")):
        for index, row in enumerate(interests[debt_id], start=1):
            codes[row.row_number] = f"{prefix}{index}"
    repayments = sorted(
        (
            item
            for item in rows
            if "还本" in item.gold_classification
            and not item.gold_classification.startswith(("并入", "代付", "争议"))
        ),
        key=lambda item: item.ordering_datetime,
    )
    for index, row in enumerate(repayments, start=1):
        codes[row.row_number] = f"R{index}"
    cash, u2 = _disputed_rows(rows)
    codes[cash.row_number] = "C1"
    codes[u2.row_number] = "U2"
    third_party = [item for item in rows if item.gold_classification.startswith("代付")]
    if len(third_party) != 1:
        raise GoldenCaseCalculationBlocked("one third-party payment is required")
    codes[third_party[0].row_number] = "LM"
    calculable = [
        item
        for item in rows
        if item.gold_classification.startswith("息")
        or "还本" in item.gold_classification
        or item.gold_classification.startswith(("代付", "争议"))
    ]
    missing = [item.row_number for item in calculable if item.row_number not in codes]
    if missing:
        raise GoldenCaseCalculationBlocked(f"event codes are missing for rows {missing}")
    return codes


def _build_key_anchors(default: ScenarioResult) -> tuple[KeyAnchor, ...]:
    by_code = {item.event_code: item for item in default.trace}
    required = ("Q1", "R1", "Q10", "R2", "R4")
    if any(item not in by_code for item in required):
        raise GoldenCaseCalculationBlocked("default trace lacks one or more key anchors")
    q1, r1, q10, r2, r4 = (by_code[item] for item in required)
    q10_old = sum(
        (item.interest for item in q10.accrual_segments if item.monthly_rate == Decimal("0.02")),
        Decimal("0"),
    )
    q10_new = sum(
        (item.interest for item in q10.accrual_segments if item.monthly_rate == Decimal("0.01")),
        Decimal("0"),
    )
    return (
        KeyAnchor(
            "Q1",
            q1.source_row_number,
            q1.source,
            {
                "protected_interest": q1.interest_paid,
                "natural_debt_paid": q1.natural_debt_paid,
                "excess_principal_offset": q1.excess_principal_offset,
                "closing_principal": q1.closing_principal,
            },
        ),
        KeyAnchor(
            "R1",
            r1.source_row_number,
            r1.source,
            {
                "interest_due_before_payment": r1.opening_interest_arrears
                + r1.accrued_interest,
                "interest_paid": r1.interest_paid,
                "principal_payment": r1.principal_payment,
                "closing_principal": r1.closing_principal,
            },
        ),
        KeyAnchor(
            "Q10",
            q10.source_row_number,
            q10.source,
            {
                "old_interval_interest": q10_old,
                "new_interval_interest": q10_new,
                "accrued_interest": q10.accrued_interest,
                "principal_payment": q10.principal_payment,
                "closing_principal": q10.closing_principal,
            },
        ),
        KeyAnchor(
            "R2",
            r2.source_row_number,
            r2.source,
            {
                "interest_paid": r2.interest_paid,
                "principal_payment": r2.principal_payment,
                "closing_principal": r2.closing_principal,
            },
        ),
        KeyAnchor(
            "R4",
            r4.source_row_number,
            r4.source,
            {
                "interest_due_before_payment": r4.opening_interest_arrears
                + r4.accrued_interest,
                "interest_paid": r4.interest_paid,
                "principal_payment": r4.principal_payment,
                "closing_principal": r4.closing_principal,
                "closing_interest_arrears": r4.closing_interest_arrears,
            },
        ),
    )


def _scenario_matrix_row(result: ScenarioResult) -> Mapping[str, object]:
    return {
        "scenario_id": result.scenario_id,
        "description": result.description,
        "include_u2": result.include_u2,
        "include_cash": result.include_cash,
        "L1": {
            "principal": _decimal_text(result.loan("L1").principal),
            "interest_arrears": _decimal_text(result.loan("L1").interest_arrears),
        },
        "L2": {
            "principal": _decimal_text(result.loan("L2").principal),
            "interest_arrears": _decimal_text(result.loan("L2").interest_arrears),
        },
        "total_principal": _decimal_text(result.total_principal),
        "total_interest_arrears": _decimal_text(result.total_interest_arrears),
    }


def _validate_review_packet(packet: Mapping[str, object]) -> None:
    provided = str(packet.get("packet_sha256", ""))
    unsigned = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if not re.fullmatch(r"[0-9a-f]{64}", provided) or _canonical_hash(unsigned) != provided:
        raise GoldenCaseCalculationBlocked("review packet hash does not authenticate its contents")
    decisions = packet.get("decisions")
    if packet.get("decision_count") != 10 or not isinstance(decisions, list) or len(decisions) != 10:
        raise GoldenCaseCalculationBlocked("review packet must contain exactly ten decisions")
    ids: set[str] = set()
    for item in decisions:
        if not isinstance(item, Mapping):
            raise GoldenCaseCalculationBlocked("review decision is malformed")
        decision_id = str(item.get("decision_id", ""))
        raw_recommendation = item.get("recommendation")
        recommendation = "" if raw_recommendation is None else str(raw_recommendation)
        agent_escalated = item.get("agent_disposition") == "REQUIRES_LAWYER"
        evidence = item.get("evidence")
        options = item.get("options")
        if not decision_id or decision_id in ids:
            raise GoldenCaseCalculationBlocked("review decision ids must be unique")
        ids.add(decision_id)
        if not isinstance(evidence, list) or not evidence:
            raise GoldenCaseCalculationBlocked(f"{decision_id} lacks evidence")
        if not isinstance(options, list) or len(options) < 2:
            raise GoldenCaseCalculationBlocked(f"{decision_id} lacks alternatives")
        allowed = set()
        for option in options:
            if not isinstance(option, Mapping):
                raise GoldenCaseCalculationBlocked(f"{decision_id} option is malformed")
            choice = str(option.get("choice", ""))
            consequence = str(option.get("consequence", ""))
            if not choice or not consequence:
                raise GoldenCaseCalculationBlocked(f"{decision_id} option lacks consequence")
            allowed.add(choice)
        if recommendation not in allowed and not (
            agent_escalated and raw_recommendation is None
        ):
            raise GoldenCaseCalculationBlocked(f"{decision_id} recommendation is not an option")


def _validate_ledger_rows(rows: tuple[LedgerRow, ...]) -> None:
    if len(rows) != 47 or tuple(item.row_number for item in rows) != tuple(range(1, 48)):
        raise GoldenCaseCalculationBlocked("golden ledger must contain rows 1 through 47 exactly once")
    if any(item.amount != _q(item.amount) or item.amount <= 0 for item in rows):
        raise GoldenCaseCalculationBlocked("ledger amounts must be positive and exact to cents")
    excluded = [item for item in rows if item.gold_classification.startswith("排除")]
    merged = [item for item in rows if item.gold_classification.startswith("并入")]
    blocked = [item for item in rows if item.gold_classification.startswith("阻断")]
    disputed = [item for item in rows if item.gold_classification.startswith("争议")]
    if (len(excluded), len(merged), len(blocked), len(disputed)) != (7, 3, 1, 2):
        raise GoldenCaseCalculationBlocked("ledger classification counts differ from the specification")
    duplicate_counts: dict[str, int] = {}
    for item in rows:
        if item.duplicate_group:
            duplicate_counts[item.duplicate_group] = duplicate_counts.get(item.duplicate_group, 0) + 1
    if duplicate_counts != {"G1": 2, "G2": 2, "G3": 2}:
        raise GoldenCaseCalculationBlocked("cross-source duplicate groups are incomplete")
    if blocked[0].currency != "HKD":
        raise GoldenCaseCalculationBlocked("the one blocked ledger row must preserve HKD")


def _disputed_rows(rows: tuple[LedgerRow, ...]) -> tuple[LedgerRow, LedgerRow]:
    disputed = [item for item in rows if item.gold_classification.startswith("争议")]
    cash = [item for item in disputed if item.channel == "现金"]
    u2 = [item for item in disputed if "情景 A/B" in item.gold_classification]
    if len(cash) != 1 or len(u2) != 1:
        raise GoldenCaseCalculationBlocked("cash and U2 scenario rows are not uniquely identifiable")
    return cash[0], u2[0]


def _coerce_ledger_row(value: object) -> LedgerRow:
    if isinstance(value, LedgerRow):
        return value
    if isinstance(value, Mapping):
        source = value
        getter = lambda *names: next((source[name] for name in names if name in source), None)
    else:
        getter = lambda *names: next((getattr(value, name) for name in names if hasattr(value, name)), None)
    row_number = getter("row_number", "row_id", "sequence", "number", "id")
    occurred_at = getter("occurred_at", "date", "occurred_on", "transaction_date")
    amount = getter("amount")
    if row_number is None or occurred_at is None or amount is None:
        raise GoldenCaseCalculationBlocked("ledger row adapter lacks identity, date, or amount")
    if isinstance(occurred_at, (date, datetime)):
        occurred_at = occurred_at.isoformat(sep=" ") if isinstance(occurred_at, datetime) else occurred_at.isoformat()
    debt = getter("debt_id", "debt", "obligation_id")
    duplicate = getter("duplicate_group", "duplicate_group_id")
    return LedgerRow(
        row_number=int(row_number),
        occurred_at=str(occurred_at),
        channel=str(getter("channel") or ""),
        amount=_money(str(amount)),
        currency=str(getter("currency") or ""),
        memo=str(getter("memo", "summary", "description") or ""),
        direction=str(getter("direction") or ""),
        source=str(getter("source", "source_locator", "evidence") or ""),
        duplicate_group=None if duplicate in (None, "", "—", "-") else str(duplicate),
        gold_classification=str(
            getter("gold_classification", "classification", "category") or ""
        ),
        debt_id=None if debt in (None, "", "—", "-") else str(debt),
        approval=str(getter("approval", "approval_requirement") or ""),
    )


def _raw_extracted_row(
    value: object,
) -> tuple[int, str, str, Decimal, str, str, str, str]:
    if isinstance(value, Mapping):
        getter = lambda name: value.get(name)
    else:
        getter = lambda name: getattr(value, name, None)
    required = {
        name: getter(name)
        for name in (
            "row_number",
            "occurred_at",
            "channel",
            "amount",
            "currency",
            "summary",
            "direction",
        )
    }
    if any(item is None for item in required.values()):
        raise GoldenCaseCalculationBlocked("raw extracted ledger row is incomplete")
    refs = getter("source_refs") or ()
    locators: list[str] = []
    for ref in refs:
        if isinstance(ref, Mapping):
            material = ref.get("material_code")
            page_number = ref.get("page_number")
        else:
            material = getattr(ref, "material_code", None)
            page_number = getattr(ref, "page_number", None)
        if material is None or page_number is None:
            raise GoldenCaseCalculationBlocked("raw extracted ledger source ref is incomplete")
        locators.append(f"{material}p{int(page_number)}")
    if not locators:
        raise GoldenCaseCalculationBlocked("raw extracted ledger row lacks source refs")
    return (
        int(required["row_number"]),
        str(required["occurred_at"]),
        str(required["channel"]),
        _money(str(required["amount"])),
        str(required["currency"]),
        str(required["summary"]),
        str(required["direction"]),
        "、".join(sorted(set(locators))),
    )


def _coerce_spec_text(value: str | Path | Mapping[str, object] | object) -> str:
    if isinstance(value, Path):
        return value.read_text(encoding="utf-8")
    if isinstance(value, str):
        possible = Path(value)
        if "\n" not in value and possible.is_file():
            return possible.read_text(encoding="utf-8")
        return value
    if isinstance(value, Mapping):
        for key in ("raw_text", "text", "spec_text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    for name in ("raw_text", "text", "spec_text"):
        candidate = getattr(value, name, None)
        if isinstance(candidate, str):
            return candidate
    raise GoldenCaseCalculationBlocked("golden specification text is unavailable")


def _parse_oracle_stdout(stdout: str) -> tuple[GoldenScenarioResult, ...]:
    """Parse the human-readable output without importing oracle implementation data."""

    trace_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2})\s+(\S+)\s+(.+?)\s+(L[12])\s+"
        r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
        r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$"
    )
    trace: list[GoldenTraceLine] = []
    for line in stdout.splitlines():
        match = trace_pattern.match(line.rstrip())
        if match is None:
            continue
        values = tuple(Decimal(item.replace(",", "")) for item in match.groups()[4:])
        trace.append(
            GoldenTraceLine(
                ordinal=len(trace) + 1,
                event_code=match.group(2),
                occurred_on=date.fromisoformat(match.group(1)),
                description=f"{match.group(2)} {match.group(3).strip()}",
                debt_id=match.group(4),
                payment_amount=values[0],
                accrued_interest=values[1],
                interest_paid=values[2],
                excess_principal_offset=values[3],
                closing_principal=values[4],
                closing_interest_arrears=values[5],
            )
        )
    if not trace:
        raise GoldenCaseCalculationBlocked("golden calculator stdout lacks the default trace")

    summary_pattern = re.compile(
        r"^(S-[AB]-[12])\s+(.+?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
        r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$"
    )
    summaries: dict[str, tuple[str, tuple[Decimal, ...]]] = {}
    for line in stdout.splitlines():
        match = summary_pattern.match(line.rstrip())
        if match is None:
            continue
        summaries[match.group(1)] = (
            match.group(2).strip(),
            tuple(Decimal(item.replace(",", "")) for item in match.groups()[2:]),
        )
    if set(summaries) != {"S-A-1", "S-A-2", "S-B-1", "S-B-2"}:
        raise GoldenCaseCalculationBlocked("golden calculator stdout lacks four scenario summaries")

    fulfillment_pattern = re.compile(
        r"^\s*借款([12])[^\uff1a]*\uff1a已付利息\s+([\d,]+\.\d{2})\uff0c超额冲本\s+"
        r"([\d,]+\.\d{2})\uff0c本金偿付\s+([\d,]+\.\d{2})\uff0c未偿本金\s+"
        r"([\d,]+\.\d{2})\uff0c未付利息挂账\s+([\d,]+\.\d{2})$"
    )
    fulfillment: dict[str, tuple[Decimal, ...]] = {}
    for line in stdout.splitlines():
        match = fulfillment_pattern.match(line.rstrip())
        if match:
            fulfillment[f"L{match.group(1)}"] = tuple(
                Decimal(item.replace(",", "")) for item in match.groups()[1:]
            )
    if set(fulfillment) != {"L1", "L2"}:
        raise GoldenCaseCalculationBlocked("golden calculator stdout lacks default fulfillment")

    results: list[GoldenScenarioResult] = []
    for scenario_id in ("S-A-1", "S-A-2", "S-B-1", "S-B-2"):
        description, values = summaries[scenario_id]
        loans: list[LoanResult] = []
        for debt_id, principal, arrears in (
            ("L1", values[0], values[1]),
            ("L2", values[2], values[3]),
        ):
            details = fulfillment.get(debt_id) if scenario_id == "S-A-1" else None
            loans.append(
                LoanResult(
                    debt_id=debt_id,
                    principal=principal,
                    interest_arrears=arrears,
                    interest_paid=details[0] if details else Decimal("0"),
                    excess_principal_offset=details[1] if details else Decimal("0"),
                    principal_paid=details[2] if details else Decimal("0"),
                    natural_debt_paid=Decimal("0"),
                )
            )
        results.append(
            GoldenScenarioResult(
                scenario_id=scenario_id,
                description=description,
                include_u2=scenario_id.startswith("S-A"),
                include_cash=scenario_id.endswith("2"),
                trace=tuple(trace) if scenario_id == "S-A-1" else (),
                loans=tuple(loans),
                total_principal=values[4],
                total_interest_arrears=values[5],
            )
        )
    return tuple(results)


def _required_date(text: str, pattern: str) -> date:
    match = re.search(pattern, text)
    if match is None:
        raise GoldenCaseCalculationBlocked(f"required date is missing for pattern {pattern}")
    return date.fromisoformat(match.group(1))


def _required_percent(text: str, pattern: str) -> Decimal:
    match = re.search(pattern, text)
    if match is None:
        raise GoldenCaseCalculationBlocked(f"required rate is missing for pattern {pattern}")
    return Decimal(match.group(1)) / Decimal("100")


def _required_rate_points(text: str, pattern: str) -> Decimal:
    match = re.search(pattern, text)
    if match is None:
        raise GoldenCaseCalculationBlocked(f"agreed loan rate is missing for pattern {pattern}")
    return Decimal(match.group(1)) / Decimal("100")


def _clean_markdown_cell(value: str) -> str:
    return value.strip().replace("**", "").replace("`", "")


def _money(value: str) -> Decimal:
    cleaned = _clean_markdown_cell(value).replace(",", "")
    return _q(Decimal(cleaned))


def _q(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _split_sources(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，]", value) if item.strip()]


def _dedupe_json_values(values: Iterable[object]) -> list[object]:
    result: list[object] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(_jsonable(value))
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _display_scalar(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return repr(value)


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "AUTHORITATIVE_ORACLE_SHA256",
    "DEFAULT_RECOMMENDED_CHOICES",
    "AccrualSegment",
    "EngineConfig",
    "GoldenCaseCalculationBlocked",
    "GoldenCaseSource",
    "GoldenComparison",
    "GoldenOutputs",
    "IndependentScenarioSuite",
    "KeyAnchor",
    "LedgerRow",
    "LoanDefinition",
    "LoanResult",
    "ScenarioResult",
    "TraceLine",
    "build_review_packet",
    "classify_extracted_rows",
    "compare_with_golden",
    "load_golden_outputs",
    "load_spec_text",
    "parse_ledger_rows",
    "recommended_choices",
    "run_independent_scenarios",
    "selected_scenario_id",
    "validate_choices",
]
