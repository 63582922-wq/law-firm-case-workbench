"""PostgreSQL command adapter for evidence-bound facts and transactions.

This module is deliberately separate from the synthetic in-memory review state.
It accepts production UUID identities only and commits each ledger mutation with
the matter version, idempotency receipt, append-only audit event, and outbox row
in one tenant-scoped PostgreSQL transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Iterable, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import IdempotencyConflict, VersionConflict
from .evidence_refs import EvidenceLink, validate_evidence_links
from .fact_claim_ledger import AssertionOrigin, FactAssertion, FactStatus
from .models import Actor, Role
from .transaction_ledger import (
    DatePrecision,
    Transaction,
    TransactionChannel,
    TransactionDirection,
    TransactionStatus,
)


class CaseLedgerPersistenceBlocked(ValueError):
    """A persistent ledger command violates an authorization or data invariant."""


@dataclass(frozen=True)
class CaseLedgerCommandReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    audit_event_id: str
    object_type: str
    object_id: str


class PostgresCaseLedgerStore:
    """UUID-only fact and transaction repository for PostgreSQL 16+."""

    _CANDIDATE_ROLES = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
    _DECISION_ROLES = frozenset({Role.LEAD_LAWYER})

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def create_fact_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        original_text: str,
        origin: AssertionOrigin,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(original_text, "fact original_text")
        validate_evidence_links(evidence_links)
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "original_text": original_text.strip(),
            "origin": origin.value,
            "evidence_links": _evidence_payload(evidence_links),
        }
        command_name = "CREATE_FACT_CANDIDATE"
        payload_hash = _payload_hash(payload)

        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
            prior = _prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._CANDIDATE_ROLES,
            )
            fact_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_facts (
                    fact_id, firm_id, matter_id, original_text, origin, status, evidence_links
                ) VALUES (%s, %s, %s, %s, %s, 'CANDIDATE', %s)
                """,
                (fact_id, actor.firm_id, matter_id, original_text.strip(), origin.value, Jsonb(_evidence_payload(evidence_links))),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="FACT_CANDIDATE_CREATED",
                object_type="FACT",
                object_id=fact_id,
                audit_payload={"fact_id": fact_id, "status": FactStatus.CANDIDATE.value},
                stale_submission=False,
            )

    def decide_fact(
        self,
        *,
        matter_id: str,
        fact_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        status: FactStatus,
        decision_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("fact_id", fact_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        if status is FactStatus.CANDIDATE:
            raise CaseLedgerPersistenceBlocked("a lawyer decision cannot leave a fact as candidate")
        _validate_sha256("fact decision_hash", decision_hash)
        command_name = "DECIDE_FACT"
        payload = {
            "matter_id": matter_id,
            "fact_id": fact_id,
            "expected_version": expected_version,
            "status": status.value,
            "decision_hash": decision_hash,
        }
        payload_hash = _payload_hash(payload)

        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
            prior = _prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._DECISION_ROLES,
            )
            row = connection.execute(
                """
                SELECT status
                FROM case_facts
                WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (fact_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(fact_id)
            if row["status"] == FactStatus.INVALIDATED.value:
                raise CaseLedgerPersistenceBlocked("an invalidated fact must be rebuilt from source evidence")

            # A changed fact cannot leave an approved response or issue silently current.
            connection.execute(
                """
                DELETE FROM case_claim_responses
                WHERE claim_response_id IN (
                    SELECT claim_response_id
                    FROM case_claim_response_facts
                    WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                )
                """,
                (fact_id, matter_id, actor.firm_id),
            )
            connection.execute(
                """
                UPDATE case_dispute_issues
                SET status = 'INVALIDATED', approval_hash = NULL, approved_by = NULL, updated_at = now()
                WHERE issue_id IN (
                    SELECT issue_id
                    FROM case_dispute_issue_facts
                    WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                ) AND matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                """,
                (fact_id, matter_id, actor.firm_id, matter_id, actor.firm_id),
            )
            updated = connection.execute(
                """
                UPDATE case_facts
                SET status = %s, decision_hash = %s, decided_by = %s, updated_at = now()
                WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (status.value, decision_hash, actor.actor_id, fact_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise VersionConflict("fact changed before this transaction could be persisted")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="FACT_DECIDED",
                object_type="FACT",
                object_id=fact_id,
                audit_payload={"fact_id": fact_id, "status": status.value, "decision_hash": decision_hash},
                stale_submission=True,
            )

    def create_transaction_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
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
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _validate_transaction_candidate(local_date, date_precision, amount, currency, evidence_links)
        normalized_currency = currency.strip().upper()
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "local_date": local_date,
            "date_precision": date_precision.value,
            "amount": amount,
            "currency": normalized_currency,
            "direction": direction.value,
            "payer_label": _normalized_optional_text(payer_label),
            "payee_label": _normalized_optional_text(payee_label),
            "channel": channel.value,
            "transaction_reference": _normalized_optional_text(transaction_reference),
            "evidence_links": _evidence_payload(evidence_links),
        }
        command_name = "CREATE_TRANSACTION_CANDIDATE"
        payload_hash = _payload_hash(payload)

        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
            prior = _prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._CANDIDATE_ROLES,
            )
            transaction_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_transactions (
                    transaction_id, firm_id, matter_id, local_date, date_precision,
                    amount, currency, direction, payer_label, payee_label, channel,
                    transaction_reference, evidence_links, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'CANDIDATE')
                """,
                (
                    transaction_id,
                    actor.firm_id,
                    matter_id,
                    local_date,
                    date_precision.value,
                    amount,
                    normalized_currency,
                    direction.value,
                    _normalized_optional_text(payer_label),
                    _normalized_optional_text(payee_label),
                    channel.value,
                    _normalized_optional_text(transaction_reference),
                    Jsonb(_evidence_payload(evidence_links)),
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="TRANSACTION_CANDIDATE_CREATED",
                object_type="TRANSACTION",
                object_id=transaction_id,
                audit_payload={"transaction_id": transaction_id, "status": TransactionStatus.CANDIDATE.value},
                stale_submission=False,
            )

    def confirm_transaction(
        self,
        *,
        matter_id: str,
        transaction_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("transaction_id", transaction_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("transaction confirmation_hash", confirmation_hash)
        command_name = "CONFIRM_TRANSACTION"
        payload = {
            "matter_id": matter_id,
            "transaction_id": transaction_id,
            "expected_version": expected_version,
            "confirmation_hash": confirmation_hash,
        }
        payload_hash = _payload_hash(payload)

        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
            prior = _prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._DECISION_ROLES,
            )
            updated = connection.execute(
                """
                UPDATE case_transactions
                SET status = 'CONFIRMED', confirmation_hash = %s, confirmed_by = %s, updated_at = now()
                WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (confirmation_hash, actor.actor_id, transaction_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                current = connection.execute(
                    """
                    SELECT status FROM case_transactions
                    WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s
                    """,
                    (transaction_id, matter_id, actor.firm_id),
                ).fetchone()
                if current is None:
                    raise KeyError(transaction_id)
                raise CaseLedgerPersistenceBlocked("only a transaction candidate can be confirmed")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="TRANSACTION_CONFIRMED",
                object_type="TRANSACTION",
                object_id=transaction_id,
                audit_payload={
                    "transaction_id": transaction_id,
                    "status": TransactionStatus.CONFIRMED.value,
                    "confirmation_hash": confirmation_hash,
                },
                stale_submission=True,
            )

    def list_facts(self, *, matter_id: str, actor: Actor) -> tuple[FactAssertion, ...]:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._CANDIDATE_ROLES)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._CANDIDATE_ROLES)
            rows = connection.execute(
                """
                SELECT fact_id, original_text, origin, status, evidence_links, decided_by, decision_hash
                FROM case_facts
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY created_at ASC, fact_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        return tuple(
            FactAssertion(
                fact_id=str(row["fact_id"]),
                original_text=row["original_text"],
                origin=AssertionOrigin(row["origin"]),
                status=FactStatus(row["status"]),
                evidence_links=_deserialize_evidence(row["evidence_links"]),
                decided_by=str(row["decided_by"]) if row["decided_by"] else None,
                decision_hash=row["decision_hash"],
            )
            for row in rows
        )

    def list_transactions(self, *, matter_id: str, actor: Actor) -> tuple[Transaction, ...]:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._CANDIDATE_ROLES)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._CANDIDATE_ROLES)
            rows = connection.execute(
                """
                SELECT transaction_id, local_date, date_precision, amount, currency,
                       direction, payer_label, payee_label, channel, transaction_reference,
                       evidence_links, status, confirmed_by, confirmation_hash
                FROM case_transactions
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY local_date ASC NULLS LAST, created_at ASC, transaction_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        return tuple(
            Transaction(
                transaction_id=str(row["transaction_id"]),
                local_date=row["local_date"],
                date_precision=DatePrecision(row["date_precision"]),
                amount=Decimal(row["amount"]),
                currency=row["currency"],
                direction=TransactionDirection(row["direction"]),
                payer_label=row["payer_label"],
                payee_label=row["payee_label"],
                channel=TransactionChannel(row["channel"]),
                transaction_reference=row["transaction_reference"],
                evidence_links=_deserialize_evidence(row["evidence_links"]),
                status=TransactionStatus(row["status"]),
                confirmed_by=str(row["confirmed_by"]) if row["confirmed_by"] else None,
                confirmation_hash=row["confirmation_hash"],
            )
            for row in rows
        )

    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        return _TenantTransaction(self._dsn, firm_id)


class _TenantTransaction:
    def __init__(self, dsn: str, firm_id: str) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._context: Any = None
        self._connection: psycopg.Connection | None = None

    def __enter__(self) -> psycopg.Connection:
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        self._connection = self._context.__enter__()
        self._connection.execute("SELECT set_config('app.firm_id', %s, true)", (self._firm_id,))
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None:
        return self._context.__exit__(exc_type, exc, traceback)


def _validate_command_identity(*, matter_id: str, actor: Actor, idempotency_key: str) -> None:
    _validate_read_identity(matter_id=matter_id, actor=actor)
    _require_text(idempotency_key, "idempotency_key")


def _validate_read_identity(*, matter_id: str, actor: Actor) -> None:
    _validate_uuid("matter_id", matter_id)
    _validate_uuid("firm_id", actor.firm_id)
    _validate_uuid("actor_id", actor.actor_id)


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"PostgreSQL persistence requires UUID {label}; Alpha identifiers are not accepted") from error


def _require_roles(actor: Actor, allowed_roles: frozenset[Role]) -> None:
    if not actor.roles & allowed_roles:
        raise PermissionError("actor does not have a permitted role for this ledger command")


def _require_positive_version(value: int) -> None:
    if value < 1:
        raise ValueError("expected_version must be positive")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} is required")


def _validate_sha256(label: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 value")


def _validate_transaction_candidate(
    local_date: date | None,
    date_precision: DatePrecision,
    amount: Decimal,
    currency: str,
    evidence_links: tuple[EvidenceLink, ...],
) -> None:
    if date_precision is DatePrecision.EXACT_DATE and local_date is None:
        raise CaseLedgerPersistenceBlocked("an exact transaction date requires local_date")
    if date_precision is not DatePrecision.EXACT_DATE and local_date is not None:
        raise CaseLedgerPersistenceBlocked("a non-exact transaction date cannot use local_date")
    if amount <= 0 or not amount.is_finite():
        raise CaseLedgerPersistenceBlocked("transaction amount must be a positive finite decimal")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise CaseLedgerPersistenceBlocked("transaction currency must use a three-letter code")
    validate_evidence_links(evidence_links)


def _normalized_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _evidence_payload(links: tuple[EvidenceLink, ...]) -> list[dict[str, Any]]:
    return [asdict(link) for link in links]


def _deserialize_evidence(value: Iterable[dict[str, Any]]) -> tuple[EvidenceLink, ...]:
    return tuple(EvidenceLink(**item) for item in value)


def _payload_hash(payload: dict[str, Any]) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        return value

    encoded = json.dumps(normalize(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _advisory_lock(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    command_name: str,
    idempotency_key: str,
) -> None:
    scope = "|".join((actor.actor_id, matter_id, command_name, idempotency_key))
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope,))


def _prior_receipt(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    command_name: str,
    idempotency_key: str,
    payload_hash: str,
) -> CaseLedgerCommandReceipt | None:
    row = connection.execute(
        """
        SELECT request_hash, response_json
        FROM command_idempotency
        WHERE firm_id = %s AND matter_id = %s AND actor_id = %s
          AND command_name = %s AND idempotency_key = %s
        """,
        (actor.firm_id, matter_id, actor.actor_id, command_name, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != payload_hash:
        raise IdempotencyConflict("idempotency key was reused with different input")
    response = row["response_json"]
    return CaseLedgerCommandReceipt(
        command_name=response["command_name"],
        idempotency_key=response["idempotency_key"],
        matter_id=response["matter_id"],
        matter_version=response["matter_version"],
        audit_event_id=response["audit_event_id"],
        object_type=response["object_type"],
        object_id=response["object_id"],
    )


def _authorize_and_lock_matter(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    expected_version: int,
    allowed_roles: frozenset[Role],
) -> None:
    row = connection.execute(
        """
        SELECT m.version,
               EXISTS (
                   SELECT 1
                   FROM matter_actor_roles mar
                   JOIN users u ON u.user_id = mar.user_id AND u.firm_id = mar.firm_id
                   WHERE mar.matter_id = m.matter_id AND mar.firm_id = m.firm_id
                     AND mar.user_id = %s AND mar.revoked_at IS NULL
                     AND mar.role = ANY(%s) AND u.status = 'ACTIVE'
               ) AS permitted
        FROM matters m
        WHERE m.matter_id = %s AND m.firm_id = %s
        FOR UPDATE
        """,
        (actor.actor_id, [role.value for role in allowed_roles], matter_id, actor.firm_id),
    ).fetchone()
    if row is None:
        raise KeyError(matter_id)
    if not row["permitted"]:
        raise PermissionError("actor lacks an active database role for this matter")
    if row["version"] != expected_version:
        raise VersionConflict(f"expected matter version {expected_version}, current version is {row['version']}")


def _authorize_matter_read(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    allowed_roles: frozenset[Role],
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM matters m
        JOIN matter_actor_roles mar
          ON mar.matter_id = m.matter_id AND mar.firm_id = m.firm_id
        JOIN users u ON u.user_id = mar.user_id AND u.firm_id = mar.firm_id
        WHERE m.matter_id = %s AND m.firm_id = %s AND mar.user_id = %s
          AND mar.revoked_at IS NULL AND mar.role = ANY(%s) AND u.status = 'ACTIVE'
        LIMIT 1
        """,
        (matter_id, actor.firm_id, actor.actor_id, [role.value for role in allowed_roles]),
    ).fetchone()
    if row is None:
        raise PermissionError("actor lacks an active database role for this matter")


def _finish_command(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    expected_version: int,
    command_name: str,
    idempotency_key: str,
    payload_hash: str,
    event_type: str,
    object_type: str,
    object_id: str,
    audit_payload: dict[str, Any],
    stale_submission: bool,
) -> CaseLedgerCommandReceipt:
    if stale_submission:
        connection.execute(
            """
            UPDATE submission_bundles
            SET validity = 'STALE'
            WHERE matter_id = %s AND firm_id = %s AND validity = 'VALID'
            """,
            (matter_id, actor.firm_id),
        )
    updated = connection.execute(
        """
        UPDATE matters
        SET version = version + 1,
            current_submission_bundle_id = CASE WHEN %s THEN NULL ELSE current_submission_bundle_id END,
            updated_at = now()
        WHERE matter_id = %s AND firm_id = %s AND version = %s
        RETURNING version
        """,
        (stale_submission, matter_id, actor.firm_id, expected_version),
    ).fetchone()
    if updated is None:
        raise VersionConflict("matter changed before this ledger command could be persisted")
    next_version = updated["version"]
    audit_event_id = str(uuid4())
    request_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO audit_events (
            event_id, firm_id, matter_id, actor_id, event_type,
            input_version, output_version, request_id, payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            audit_event_id,
            actor.firm_id,
            matter_id,
            actor.actor_id,
            event_type,
            expected_version,
            next_version,
            request_id,
            Jsonb(audit_payload),
        ),
    )
    connection.execute(
        """
        INSERT INTO outbox_events (firm_id, matter_id, aggregate_version, event_type, payload)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (actor.firm_id, matter_id, next_version, event_type, Jsonb({"audit_event_id": audit_event_id, "object_id": object_id})),
    )
    receipt = CaseLedgerCommandReceipt(
        command_name=command_name,
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=next_version,
        audit_event_id=audit_event_id,
        object_type=object_type,
        object_id=object_id,
    )
    connection.execute(
        """
        INSERT INTO command_idempotency (
            firm_id, matter_id, actor_id, command_name, idempotency_key, request_hash, response_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor.firm_id,
            matter_id,
            actor.actor_id,
            command_name,
            idempotency_key,
            payload_hash,
            Jsonb(asdict(receipt)),
        ),
    )
    return receipt
