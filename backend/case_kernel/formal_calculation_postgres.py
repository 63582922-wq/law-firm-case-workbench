"""PostgreSQL adapter for approval-bound deterministic formal calculations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import json
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .calculation_engine import (
    AllocationPolicy,
    ApprovedCalculationEvent,
    ApprovedRuleSegment,
    CalculationBlocked,
    CalculationScenario,
    EventKind,
    PaymentApplication,
    calculate,
    independently_check,
)
from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .legal_rules import ApprovedLegalBundleReference
from .models import Actor, Role


@dataclass(frozen=True)
class PersistentFormalCalculationSnapshot:
    matter_id: str
    matter_version: int
    scenario: dict[str, Any] | None
    run: dict[str, Any] | None
    snapshot_hash: str


class PostgresFormalCalculationStore:
    _LEAD_ROLES = frozenset({Role.LEAD_LAWYER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def create_formal_calculation(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        obligation_id: str,
        start_date: date,
        end_date: date,
        legal_bundle_id: str,
        legal_bundle_hash: str,
        allocation_policy: AllocationPolicy,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._LEAD_ROLES)
        _require_positive_version(expected_version)
        _require_text(obligation_id, "obligation_id")
        _validate_uuid("legal_bundle_id", legal_bundle_id)
        _validate_sha256("legal_bundle_hash", legal_bundle_hash)
        _validate_sha256("approval_hash", approval_hash)
        if start_date >= end_date:
            raise CaseLedgerPersistenceBlocked("formal calculation interval must be non-empty")
        command_name = "CREATE_FORMAL_CALCULATION"
        request_payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "obligation_id": obligation_id.strip(),
            "start_date": start_date,
            "end_date": end_date,
            "legal_bundle_id": legal_bundle_id,
            "legal_bundle_hash": legal_bundle_hash,
            "allocation_policy": allocation_policy,
            "approval_hash": approval_hash,
        }
        request_hash = _payload_hash(request_payload)
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            prior = _prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._LEAD_ROLES,
            )
            bundle = connection.execute(
                """
                SELECT bundle_id, bundle_hash, status
                FROM case_legal_bundles
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (legal_bundle_id, matter_id, actor.firm_id),
            ).fetchone()
            if bundle is None:
                raise KeyError(legal_bundle_id)
            if bundle["status"] != "APPROVED" or bundle["bundle_hash"] != legal_bundle_hash:
                raise CaseLedgerPersistenceBlocked("formal calculation requires the current approved legal bundle")
            rule_rows = connection.execute(
                """
                SELECT rule_version FROM case_legal_bundle_rule_versions
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY issue_key, rule_version
                """,
                (legal_bundle_id, matter_id, actor.firm_id),
            ).fetchall()
            approved_rule_versions = tuple(row["rule_version"] for row in rule_rows)
            if not approved_rule_versions:
                raise CaseLedgerPersistenceBlocked("approved legal bundle contains no rule versions")
            segment_rows = connection.execute(
                """
                SELECT segment_id, start_date, end_date, annual_rate,
                       rule_version AS source_rule_version, applicability_anchor,
                       approval_hash, trigger_event_id
                FROM case_legal_bundle_segments
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY start_date, segment_id
                """,
                (legal_bundle_id, matter_id, actor.firm_id),
            ).fetchall()
            if not segment_rows:
                raise CaseLedgerPersistenceBlocked(
                    "approved legal bundle contains no server-derived calculation segments"
                )
            transaction_rows = connection.execute(
                """
                SELECT transaction.transaction_id, transaction.local_date,
                       transaction.date_precision, transaction.status AS transaction_status,
                       transaction.evidence_links AS transaction_evidence_links,
                       classification.classification_id, classification.nature,
                       classification.same_day_sequence,
                       classification.evidence_links AS classification_evidence_links,
                       classification.status AS classification_status,
                       classification.approval_hash, classification.approved_by,
                       allocation.amount, allocation.currency
                FROM case_payment_allocations allocation
                JOIN case_payment_classifications classification
                  ON classification.classification_id = allocation.classification_id
                 AND classification.matter_id = allocation.matter_id
                 AND classification.firm_id = allocation.firm_id
                JOIN case_transactions transaction
                  ON transaction.transaction_id = classification.transaction_id
                 AND transaction.matter_id = classification.matter_id
                 AND transaction.firm_id = classification.firm_id
                WHERE allocation.obligation_id = %s
                  AND allocation.matter_id = %s AND allocation.firm_id = %s
                  AND classification.status = 'APPROVED'
                  AND transaction.status = 'CONFIRMED'
                ORDER BY transaction.local_date, classification.same_day_sequence, transaction.transaction_id
                """,
                (obligation_id.strip(), matter_id, actor.firm_id),
            ).fetchall()
            if not transaction_rows:
                raise CaseLedgerPersistenceBlocked("formal calculation has no approved classified transactions")
            transaction_rows = self._remove_approved_duplicates(
                connection,
                transaction_rows,
                matter_id=matter_id,
                firm_id=actor.firm_id,
            )
            events = tuple(self._event_from_row(row) for row in transaction_rows)
            transaction_snapshot_hash = _payload_hash(
                {
                    "obligation_id": obligation_id.strip(),
                    "events": tuple(asdict(event) for event in events),
                    "source_transaction_ids": tuple(str(row["transaction_id"]) for row in transaction_rows),
                }
            )
            scenario_version_row = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                FROM calculation_scenarios
                WHERE matter_id = %s AND firm_id = %s AND obligation_id = %s
                """,
                (matter_id, actor.firm_id, obligation_id.strip()),
            ).fetchone()
            scenario_version = scenario_version_row["next_version"]
            scenario_id = str(uuid4())
            scenario = CalculationScenario(
                scenario_id=scenario_id,
                version=scenario_version,
                start_date=start_date,
                end_date=end_date,
                events=events,
                rule_segments=tuple(
                    ApprovedRuleSegment(
                        segment_id=str(segment["segment_id"]),
                        start_date=segment["start_date"],
                        end_date=segment["end_date"],
                        annual_rate=Decimal(segment["annual_rate"]),
                        source_rule_version=segment["source_rule_version"],
                        applicability_anchor=segment["applicability_anchor"],
                        approved_by=actor.actor_id,
                        approval_hash=segment["approval_hash"],
                    )
                    for segment in segment_rows
                ),
                legal_bundle=ApprovedLegalBundleReference(
                    bundle_id=legal_bundle_id,
                    bundle_hash=legal_bundle_hash,
                    approved_rule_versions=approved_rule_versions,
                ),
                allocation_policy=allocation_policy,
                approved_by=actor.actor_id,
                approval_hash=approval_hash,
                currency="CNY",
            )
            try:
                run = calculate(scenario)
                independent = independently_check(scenario, run)
            except CalculationBlocked as error:
                raise CaseLedgerPersistenceBlocked(str(error)) from error
            if not independent.matching:
                raise CaseLedgerPersistenceBlocked("independent formal calculation check did not match")
            independent_hash = _payload_hash(asdict(independent))
            stale_reason = "新的正式计算情景已获批准。"
            connection.execute(
                """
                UPDATE calculation_runs run
                SET status = 'STALE', stale_at = now(), stale_reason = %s
                WHERE run.matter_id = %s AND run.firm_id = %s AND run.status = 'VERIFIED'
                  AND EXISTS (
                      SELECT 1 FROM calculation_scenarios scenario
                      WHERE scenario.scenario_id = run.scenario_id
                        AND scenario.obligation_id = %s
                  )
                """,
                (stale_reason, matter_id, actor.firm_id, obligation_id.strip()),
            )
            connection.execute(
                """
                UPDATE calculation_scenarios
                SET status = 'STALE', stale_at = now(), stale_reason = %s
                WHERE matter_id = %s AND firm_id = %s AND obligation_id = %s AND status = 'APPROVED'
                """,
                (stale_reason, matter_id, actor.firm_id, obligation_id.strip()),
            )
            connection.execute(
                """
                INSERT INTO calculation_scenarios (
                    scenario_id, firm_id, matter_id, obligation_id, version, status,
                    start_date, end_date, currency, allocation_policy,
                    legal_bundle_id, legal_bundle_hash, transaction_snapshot_hash,
                    input_hash, approved_by, approval_hash
                ) VALUES (%s, %s, %s, %s, %s, 'APPROVED', %s, %s, 'CNY', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    scenario_id,
                    actor.firm_id,
                    matter_id,
                    obligation_id.strip(),
                    scenario_version,
                    start_date,
                    end_date,
                    allocation_policy.value,
                    legal_bundle_id,
                    legal_bundle_hash,
                    transaction_snapshot_hash,
                    run.input_hash,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            for event, row in zip(events, transaction_rows, strict=True):
                connection.execute(
                    """
                    INSERT INTO calculation_scenario_events (
                        scenario_id, transaction_id, classification_id, firm_id, matter_id,
                        effective_date, same_day_sequence, event_kind, amount, currency,
                        payment_application, evidence_ids, approved_by, approval_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'CNY', %s, %s, %s, %s)
                    """,
                    (
                        scenario_id,
                        str(row["transaction_id"]),
                        str(row["classification_id"]),
                        actor.firm_id,
                        matter_id,
                        event.effective_date,
                        event.sequence,
                        event.kind.value,
                        event.amount,
                        event.payment_application.value,
                        json.dumps(event.evidence_ids, ensure_ascii=False),
                        event.approved_by,
                        event.approval_hash,
                    ),
                )
            for segment in scenario.rule_segments:
                connection.execute(
                    """
                    INSERT INTO calculation_rule_segments (
                        segment_id, scenario_id, firm_id, matter_id, start_date, end_date,
                        annual_rate, source_rule_version, applicability_anchor,
                        approved_by, approval_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        segment.segment_id,
                        scenario_id,
                        actor.firm_id,
                        matter_id,
                        segment.start_date,
                        segment.end_date,
                        segment.annual_rate,
                        segment.source_rule_version,
                        segment.applicability_anchor,
                        segment.approved_by,
                        segment.approval_hash,
                    ),
                )
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO calculation_runs (
                    run_id, firm_id, matter_id, scenario_id, scenario_version, status,
                    engine_version, legal_bundle_id, legal_bundle_hash, input_hash,
                    output_hash, independent_check_hash, total_interest_accrued,
                    total_interest_paid, remaining_principal, remaining_unpaid_interest,
                    unapplied_payments, generated_at
                ) VALUES (%s, %s, %s, %s, %s, 'VERIFIED', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    actor.firm_id,
                    matter_id,
                    scenario_id,
                    scenario_version,
                    run.engine_version,
                    legal_bundle_id,
                    legal_bundle_hash,
                    run.input_hash,
                    run.output_hash,
                    independent_hash,
                    run.total_interest_accrued,
                    run.total_interest_paid,
                    run.remaining_principal,
                    run.remaining_unpaid_interest,
                    run.unapplied_payments,
                    run.generated_at,
                ),
            )
            for sequence, line in enumerate(run.line_items, start=1):
                connection.execute(
                    """
                    INSERT INTO calculation_line_items (
                        run_id, scenario_id, line_sequence, firm_id, matter_id,
                        period_start, period_end, opening_principal, annual_rate,
                        day_count, accrued_interest, closing_principal,
                        accrued_unpaid_interest, rule_segment_id,
                        source_rule_version, evidence_ids
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        scenario_id,
                        sequence,
                        actor.firm_id,
                        matter_id,
                        line.period_start,
                        line.period_end,
                        line.opening_principal,
                        line.annual_rate,
                        line.day_count,
                        line.accrued_interest,
                        line.closing_principal,
                        line.accrued_unpaid_interest,
                        line.rule_segment_id,
                        line.source_rule_version,
                        json.dumps(line.evidence_ids, ensure_ascii=False),
                    ),
                )
            for sequence, allocation in enumerate(run.payment_allocations, start=1):
                connection.execute(
                    """
                    INSERT INTO calculation_payment_allocations (
                        run_id, allocation_sequence, firm_id, matter_id,
                        payment_event_id, effective_date, payment_amount,
                        allocated_interest, allocated_principal, unapplied_amount,
                        payment_application, evidence_ids
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        sequence,
                        actor.firm_id,
                        matter_id,
                        allocation.payment_event_id,
                        allocation.effective_date,
                        allocation.payment_amount,
                        allocation.allocated_interest,
                        allocation.allocated_principal,
                        allocation.unapplied_amount,
                        allocation.payment_application.value,
                        json.dumps(allocation.evidence_ids, ensure_ascii=False),
                    ),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                event_type="FORMAL_CALCULATION_VERIFIED",
                object_type="CALCULATION_RUN",
                object_id=run_id,
                audit_payload={
                    "scenario_id": scenario_id,
                    "run_id": run_id,
                    "obligation_id": obligation_id.strip(),
                    "legal_bundle_id": legal_bundle_id,
                    "legal_bundle_hash": legal_bundle_hash,
                    "transaction_snapshot_hash": transaction_snapshot_hash,
                    "input_hash": run.input_hash,
                    "output_hash": run.output_hash,
                    "independent_check_hash": independent_hash,
                },
                stale_submission=True,
                stale_calculations=False,
            )

    def get_current_calculation(
        self,
        *,
        matter_id: str,
        obligation_id: str,
        actor: Actor,
    ) -> PersistentFormalCalculationSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        _require_text(obligation_id, "obligation_id")
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter = connection.execute(
                "SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            scenario = connection.execute(
                """
                SELECT scenario_id, obligation_id, version, start_date, end_date,
                       currency, allocation_policy, legal_bundle_id, legal_bundle_hash,
                       transaction_snapshot_hash, input_hash, approved_by, approval_hash
                FROM calculation_scenarios
                WHERE matter_id = %s AND firm_id = %s AND obligation_id = %s
                  AND status = 'APPROVED'
                """,
                (matter_id, actor.firm_id, obligation_id.strip()),
            ).fetchone()
            if scenario is None:
                payload = {"matter_id": matter_id, "matter_version": matter["version"], "scenario": None, "run": None}
                return PersistentFormalCalculationSnapshot(snapshot_hash=_payload_hash(payload), **payload)
            run = connection.execute(
                """
                SELECT run_id, scenario_id, scenario_version, engine_version,
                       legal_bundle_id, legal_bundle_hash, input_hash, output_hash,
                       independent_check_hash, total_interest_accrued,
                       total_interest_paid, remaining_principal,
                       remaining_unpaid_interest, unapplied_payments, generated_at
                FROM calculation_runs
                WHERE scenario_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'VERIFIED'
                """,
                (scenario["scenario_id"], matter_id, actor.firm_id),
            ).fetchone()
            if run is None:
                raise CaseLedgerPersistenceBlocked(
                    "approved formal calculation scenario has no current verified run"
                )
            line_rows = connection.execute(
                """
                SELECT line_sequence, period_start, period_end, opening_principal,
                       annual_rate, day_count, accrued_interest, closing_principal,
                       accrued_unpaid_interest, rule_segment_id,
                       source_rule_version, evidence_ids
                FROM calculation_line_items
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY line_sequence
                """,
                (run["run_id"], matter_id, actor.firm_id),
            ).fetchall()
            allocation_rows = connection.execute(
                """
                SELECT allocation_sequence, payment_event_id, effective_date,
                       payment_amount, allocated_interest, allocated_principal,
                       unapplied_amount, payment_application, evidence_ids
                FROM calculation_payment_allocations
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY allocation_sequence
                """,
                (run["run_id"], matter_id, actor.firm_id),
            ).fetchall()
        scenario_payload = _serialize_row(scenario)
        run_payload = {**_serialize_row(run), "line_items": tuple(_serialize_row(row) for row in line_rows), "payment_allocations": tuple(_serialize_row(row) for row in allocation_rows)}
        payload = {
            "matter_id": matter_id,
            "matter_version": matter["version"],
            "scenario": scenario_payload,
            "run": run_payload,
        }
        return PersistentFormalCalculationSnapshot(snapshot_hash=_payload_hash(payload), **payload)

    def _remove_approved_duplicates(
        self,
        connection: psycopg.Connection,
        rows: list[dict[str, Any]],
        *,
        matter_id: str,
        firm_id: str,
    ) -> list[dict[str, Any]]:
        selected = {str(row["transaction_id"]): row for row in rows}
        duplicate_rows = connection.execute(
            """
            SELECT group_row.duplicate_group_id, group_row.status,
                   group_row.canonical_transaction_id, member.transaction_id
            FROM case_transaction_duplicate_groups group_row
            JOIN case_transaction_duplicate_members member
              ON member.duplicate_group_id = group_row.duplicate_group_id
             AND member.matter_id = group_row.matter_id AND member.firm_id = group_row.firm_id
            WHERE member.transaction_id = ANY(%s)
              AND group_row.matter_id = %s AND group_row.firm_id = %s
              AND group_row.status <> 'INVALIDATED'
            ORDER BY group_row.duplicate_group_id, member.transaction_id
            """,
            (list(selected), matter_id, firm_id),
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in duplicate_rows:
            groups.setdefault(str(row["duplicate_group_id"]), []).append(row)
        for members in groups.values():
            status = members[0]["status"]
            if status == "CANDIDATE":
                raise CaseLedgerPersistenceBlocked("formal calculation includes an unresolved duplicate transaction")
            if status == "SAME_ECONOMIC_EVENT":
                canonical_id = str(members[0]["canonical_transaction_id"])
                if canonical_id not in selected:
                    raise CaseLedgerPersistenceBlocked("approved duplicate canonical transaction is not calculation-ready")
                for member in members:
                    transaction_id = str(member["transaction_id"])
                    if transaction_id != canonical_id:
                        selected.pop(transaction_id, None)
        return [selected[key] for key in sorted(selected, key=lambda value: (selected[value]["local_date"], selected[value]["same_day_sequence"], value))]

    @staticmethod
    def _event_from_row(row: dict[str, Any]) -> ApprovedCalculationEvent:
        if row["date_precision"] != "EXACT_DATE" or row["local_date"] is None:
            raise CaseLedgerPersistenceBlocked("formal calculation requires exact approved transaction dates")
        amount = Decimal(row["amount"])
        if row["currency"] != "CNY" or amount.quantize(Decimal("0.01")) != amount:
            raise CaseLedgerPersistenceBlocked("formal calculation requires CNY-cent obligation allocations")
        if row["same_day_sequence"] is None or row["same_day_sequence"] < 1:
            raise CaseLedgerPersistenceBlocked("formal calculation requires approved same-day ordering")
        mapping = {
            "DISBURSEMENT": (EventKind.DISBURSEMENT, PaymentApplication.BY_POLICY),
            "REPAYMENT_UNSPECIFIED": (EventKind.PAYMENT, PaymentApplication.BY_POLICY),
            "INTEREST_PAYMENT": (EventKind.PAYMENT, PaymentApplication.INTEREST_ONLY),
            "PRINCIPAL_REPAYMENT": (EventKind.PAYMENT, PaymentApplication.PRINCIPAL_ONLY),
        }
        if row["nature"] not in mapping:
            raise CaseLedgerPersistenceBlocked("a non-calculation payment nature entered the obligation allocation")
        evidence_ids = _evidence_ids(row["transaction_evidence_links"], row["classification_evidence_links"])
        kind, application = mapping[row["nature"]]
        return ApprovedCalculationEvent(
            event_id=str(row["transaction_id"]),
            effective_date=row["local_date"],
            sequence=row["same_day_sequence"],
            kind=kind,
            amount=amount,
            currency="CNY",
            evidence_ids=evidence_ids,
            approved_by=str(row["approved_by"]),
            approval_hash=row["approval_hash"],
            payment_application=application,
        )

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _evidence_ids(*collections: Any) -> tuple[str, ...]:
    values: set[str] = set()
    for collection in collections:
        parsed = json.loads(collection) if isinstance(collection, str) else collection
        if not isinstance(parsed, list):
            raise CaseLedgerPersistenceBlocked("transaction evidence links are not a JSON array")
        for item in parsed:
            if not isinstance(item, dict) or not str(item.get("evidence_id", "")).strip():
                raise CaseLedgerPersistenceBlocked("transaction evidence link is missing evidence_id")
            values.add(str(item["evidence_id"]))
    if not values:
        raise CaseLedgerPersistenceBlocked("formal calculation event has no source evidence")
    return tuple(sorted(values))


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            serialized[key] = format(value, "f")
        elif isinstance(value, date):
            serialized[key] = value.isoformat()
        elif hasattr(value, "isoformat"):
            serialized[key] = value.isoformat()
        elif isinstance(value, list):
            serialized[key] = tuple(value)
        else:
            serialized[key] = str(value) if key.endswith("_id") and value is not None else value
    return serialized
