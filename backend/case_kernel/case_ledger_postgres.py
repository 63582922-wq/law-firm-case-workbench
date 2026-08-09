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
from .fact_claim_ledger import (
    AssertionOrigin,
    ClaimResponsePosition,
    ClaimStatus,
    FactAssertion,
    FactStatus,
    IssueStatus,
)
from .models import Actor, Role
from .transaction_ledger import (
    ClassificationOrigin,
    ClassificationStatus,
    DatePrecision,
    DuplicateStatus,
    ObligationAllocation,
    PaymentNature,
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

    def create_claim_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        original_claim_text: str,
        claimed_amount: Decimal | None,
        currency: str | None,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(original_claim_text, "claim original text")
        _validate_optional_money(claimed_amount, currency, "claim")
        validate_evidence_links(evidence_links)
        normalized_currency = currency.strip().upper() if currency else None
        command_name = "CREATE_CLAIM_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "original_claim_text": original_claim_text.strip(),
            "claimed_amount": claimed_amount,
            "currency": normalized_currency,
            "evidence_links": _evidence_payload(evidence_links),
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
                allowed_roles=self._CANDIDATE_ROLES,
            )
            claim_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_claims (
                    claim_id, firm_id, matter_id, original_claim_text, claimed_amount,
                    currency, status, evidence_links
                ) VALUES (%s, %s, %s, %s, %s, %s, 'CANDIDATE', %s)
                """,
                (
                    claim_id,
                    actor.firm_id,
                    matter_id,
                    original_claim_text.strip(),
                    claimed_amount,
                    normalized_currency,
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
                event_type="CLAIM_CANDIDATE_CREATED",
                object_type="CLAIM",
                object_id=claim_id,
                audit_payload={"claim_id": claim_id, "status": ClaimStatus.CANDIDATE.value},
                stale_submission=False,
            )

    def confirm_claim_scope(
        self,
        *,
        matter_id: str,
        claim_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        confirmation_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("claim_id", claim_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("claim confirmation_hash", confirmation_hash)
        command_name = "CONFIRM_CLAIM_SCOPE"
        payload = {
            "matter_id": matter_id,
            "claim_id": claim_id,
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
                UPDATE case_claims
                SET status = 'CONFIRMED_SCOPE', confirmation_hash = %s,
                    confirmed_by = %s, updated_at = now()
                WHERE claim_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (confirmation_hash, actor.actor_id, claim_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                _raise_missing_or_inactive(
                    connection,
                    table="case_claims",
                    id_column="claim_id",
                    object_id=claim_id,
                    matter_id=matter_id,
                    firm_id=actor.firm_id,
                    inactive_message="only an active claim candidate can be confirmed",
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="CLAIM_SCOPE_CONFIRMED",
                object_type="CLAIM",
                object_id=claim_id,
                audit_payload={"claim_id": claim_id, "status": ClaimStatus.CONFIRMED_SCOPE.value, "confirmation_hash": confirmation_hash},
                stale_submission=True,
            )

    def set_claim_response(
        self,
        *,
        matter_id: str,
        claim_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        position: ClaimResponsePosition,
        confirmed_fact_ids: tuple[str, ...],
        partial_amount: Decimal | None,
        currency: str | None,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("claim_id", claim_id)
        normalized_fact_ids = _validate_uuid_set("confirmed_fact_ids", confirmed_fact_ids, minimum=1)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("claim response approval_hash", approval_hash)
        if position is ClaimResponsePosition.PARTIALLY_ADMIT:
            _validate_optional_money(partial_amount, currency, "partial response", require_amount=True)
        elif partial_amount is not None or currency is not None:
            raise CaseLedgerPersistenceBlocked("only a partial response may contain an amount or currency")
        normalized_currency = currency.strip().upper() if currency else None
        command_name = "SET_CLAIM_RESPONSE"
        payload = {
            "matter_id": matter_id,
            "claim_id": claim_id,
            "expected_version": expected_version,
            "position": position.value,
            "confirmed_fact_ids": normalized_fact_ids,
            "partial_amount": partial_amount,
            "currency": normalized_currency,
            "approval_hash": approval_hash,
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
            claim = connection.execute(
                """
                SELECT status, claimed_amount, currency
                FROM case_claims
                WHERE claim_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (claim_id, matter_id, actor.firm_id),
            ).fetchone()
            if claim is None:
                raise KeyError(claim_id)
            if claim["status"] != ClaimStatus.CONFIRMED_SCOPE.value:
                raise CaseLedgerPersistenceBlocked("claim scope must be confirmed before a response can be recorded")
            _require_confirmed_fact_rows(connection, fact_ids=normalized_fact_ids, matter_id=matter_id, firm_id=actor.firm_id)
            if position is ClaimResponsePosition.PARTIALLY_ADMIT:
                if claim["claimed_amount"] is not None and partial_amount is not None and partial_amount > claim["claimed_amount"]:
                    raise CaseLedgerPersistenceBlocked("partial response amount cannot exceed the confirmed claim amount")
                if claim["currency"] and normalized_currency != claim["currency"]:
                    raise CaseLedgerPersistenceBlocked("partial response currency must match the confirmed claim currency")
            connection.execute(
                "DELETE FROM case_claim_responses WHERE claim_id = %s AND matter_id = %s AND firm_id = %s",
                (claim_id, matter_id, actor.firm_id),
            )
            response_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_claim_responses (
                    claim_response_id, firm_id, matter_id, claim_id, position,
                    partial_amount, currency, approval_hash, approved_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    response_id,
                    actor.firm_id,
                    matter_id,
                    claim_id,
                    position.value,
                    partial_amount,
                    normalized_currency,
                    approval_hash,
                    actor.actor_id,
                ),
            )
            for fact_id in normalized_fact_ids:
                connection.execute(
                    """
                    INSERT INTO case_claim_response_facts (
                        claim_response_id, fact_id, firm_id, matter_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (response_id, fact_id, actor.firm_id, matter_id),
                )
            connection.execute(
                """
                UPDATE case_dispute_issues
                SET status = 'INVALIDATED', approval_hash = NULL, approved_by = NULL, updated_at = now()
                WHERE issue_id IN (
                    SELECT issue_id FROM case_dispute_issue_claims
                    WHERE claim_id = %s AND matter_id = %s AND firm_id = %s
                ) AND matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                """,
                (claim_id, matter_id, actor.firm_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="CLAIM_RESPONSE_SET",
                object_type="CLAIM_RESPONSE",
                object_id=response_id,
                audit_payload={"claim_response_id": response_id, "claim_id": claim_id, "position": position.value, "approval_hash": approval_hash},
                stale_submission=True,
            )

    def create_dispute_issue_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        question: str,
        claim_ids: tuple[str, ...],
        confirmed_fact_ids: tuple[str, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        normalized_claim_ids = _validate_uuid_set("claim_ids", claim_ids, minimum=1)
        normalized_fact_ids = _validate_uuid_set("confirmed_fact_ids", confirmed_fact_ids, minimum=1)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(question, "issue question")
        command_name = "CREATE_DISPUTE_ISSUE_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "question": question.strip(),
            "claim_ids": normalized_claim_ids,
            "confirmed_fact_ids": normalized_fact_ids,
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
                allowed_roles=self._CANDIDATE_ROLES,
            )
            _require_claim_rows(connection, claim_ids=normalized_claim_ids, matter_id=matter_id, firm_id=actor.firm_id, confirmed=False)
            _require_confirmed_fact_rows(connection, fact_ids=normalized_fact_ids, matter_id=matter_id, firm_id=actor.firm_id)
            issue_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_dispute_issues (issue_id, firm_id, matter_id, question, status)
                VALUES (%s, %s, %s, %s, 'CANDIDATE')
                """,
                (issue_id, actor.firm_id, matter_id, question.strip()),
            )
            for claim_id_value in normalized_claim_ids:
                connection.execute(
                    """
                    INSERT INTO case_dispute_issue_claims (issue_id, claim_id, firm_id, matter_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (issue_id, claim_id_value, actor.firm_id, matter_id),
                )
            for fact_id in normalized_fact_ids:
                connection.execute(
                    """
                    INSERT INTO case_dispute_issue_facts (issue_id, fact_id, firm_id, matter_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (issue_id, fact_id, actor.firm_id, matter_id),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="DISPUTE_ISSUE_CANDIDATE_CREATED",
                object_type="DISPUTE_ISSUE",
                object_id=issue_id,
                audit_payload={"issue_id": issue_id, "status": IssueStatus.CANDIDATE.value},
                stale_submission=False,
            )

    def confirm_dispute_issue(
        self,
        *,
        matter_id: str,
        issue_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("issue_id", issue_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("issue approval_hash", approval_hash)
        command_name = "CONFIRM_DISPUTE_ISSUE"
        payload = {
            "matter_id": matter_id,
            "issue_id": issue_id,
            "expected_version": expected_version,
            "approval_hash": approval_hash,
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
            issue = connection.execute(
                """
                SELECT status FROM case_dispute_issues
                WHERE issue_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (issue_id, matter_id, actor.firm_id),
            ).fetchone()
            if issue is None:
                raise KeyError(issue_id)
            if issue["status"] != IssueStatus.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active dispute issue candidate can be confirmed")
            claim_rows = connection.execute(
                """
                SELECT c.claim_id, c.status
                FROM case_dispute_issue_claims link
                JOIN case_claims c
                  ON c.claim_id = link.claim_id AND c.firm_id = link.firm_id AND c.matter_id = link.matter_id
                WHERE link.issue_id = %s AND link.matter_id = %s AND link.firm_id = %s
                """,
                (issue_id, matter_id, actor.firm_id),
            ).fetchall()
            if not claim_rows or any(row["status"] != ClaimStatus.CONFIRMED_SCOPE.value for row in claim_rows):
                raise CaseLedgerPersistenceBlocked("all claims for an issue must have confirmed scope")
            fact_rows = connection.execute(
                """
                SELECT f.fact_id, f.status
                FROM case_dispute_issue_facts link
                JOIN case_facts f
                  ON f.fact_id = link.fact_id AND f.firm_id = link.firm_id AND f.matter_id = link.matter_id
                WHERE link.issue_id = %s AND link.matter_id = %s AND link.firm_id = %s
                """,
                (issue_id, matter_id, actor.firm_id),
            ).fetchall()
            if not fact_rows or any(row["status"] != FactStatus.CONFIRMED.value for row in fact_rows):
                raise CaseLedgerPersistenceBlocked("all issue facts must still be lawyer-confirmed")
            connection.execute(
                """
                UPDATE case_dispute_issues
                SET status = 'CONFIRMED', approval_hash = %s, approved_by = %s, updated_at = now()
                WHERE issue_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (approval_hash, actor.actor_id, issue_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="DISPUTE_ISSUE_CONFIRMED",
                object_type="DISPUTE_ISSUE",
                object_id=issue_id,
                audit_payload={"issue_id": issue_id, "status": IssueStatus.CONFIRMED.value, "approval_hash": approval_hash},
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

    def create_payment_classification_candidate(
        self,
        *,
        matter_id: str,
        transaction_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        origin: ClassificationOrigin,
        nature: PaymentNature,
        allocations: tuple[ObligationAllocation, ...],
        same_day_sequence: int | None,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("transaction_id", transaction_id)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        if same_day_sequence is not None and same_day_sequence < 1:
            raise CaseLedgerPersistenceBlocked("same-day sequence must be a positive integer")
        validate_evidence_links(evidence_links)
        normalized_allocations = tuple(sorted(allocations, key=lambda item: item.obligation_id))
        command_name = "CREATE_PAYMENT_CLASSIFICATION_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "transaction_id": transaction_id,
            "expected_version": expected_version,
            "origin": origin.value,
            "nature": nature.value,
            "allocations": [asdict(item) for item in normalized_allocations],
            "same_day_sequence": same_day_sequence,
            "evidence_links": _evidence_payload(evidence_links),
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
                allowed_roles=self._CANDIDATE_ROLES,
            )
            transaction = connection.execute(
                """
                SELECT amount, currency, status
                FROM case_transactions
                WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (transaction_id, matter_id, actor.firm_id),
            ).fetchone()
            if transaction is None:
                raise KeyError(transaction_id)
            if transaction["status"] == TransactionStatus.INVALIDATED.value:
                raise CaseLedgerPersistenceBlocked("a classification cannot use an invalidated transaction")
            _validate_persistent_classification(
                transaction_amount=Decimal(transaction["amount"]),
                transaction_currency=transaction["currency"],
                nature=nature,
                allocations=normalized_allocations,
            )
            classification_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_payment_classifications (
                    classification_id, firm_id, matter_id, transaction_id, origin,
                    nature, same_day_sequence, evidence_links, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'CANDIDATE')
                """,
                (
                    classification_id,
                    actor.firm_id,
                    matter_id,
                    transaction_id,
                    origin.value,
                    nature.value,
                    same_day_sequence,
                    Jsonb(_evidence_payload(evidence_links)),
                ),
            )
            for allocation in normalized_allocations:
                connection.execute(
                    """
                    INSERT INTO case_payment_allocations (
                        classification_id, obligation_id, firm_id, matter_id, amount, currency
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        classification_id,
                        allocation.obligation_id.strip(),
                        actor.firm_id,
                        matter_id,
                        allocation.amount,
                        allocation.currency.strip().upper(),
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
                event_type="PAYMENT_CLASSIFICATION_CANDIDATE_CREATED",
                object_type="PAYMENT_CLASSIFICATION",
                object_id=classification_id,
                audit_payload={
                    "classification_id": classification_id,
                    "transaction_id": transaction_id,
                    "nature": nature.value,
                    "status": ClassificationStatus.CANDIDATE.value,
                },
                stale_submission=False,
            )

    def approve_payment_classification(
        self,
        *,
        matter_id: str,
        classification_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("classification_id", classification_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("payment classification approval_hash", approval_hash)
        command_name = "APPROVE_PAYMENT_CLASSIFICATION"
        payload = {
            "matter_id": matter_id,
            "classification_id": classification_id,
            "expected_version": expected_version,
            "approval_hash": approval_hash,
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
                SELECT pc.status, pc.nature, pc.transaction_id,
                       t.amount, t.currency, t.status AS transaction_status
                FROM case_payment_classifications pc
                JOIN case_transactions t
                  ON t.transaction_id = pc.transaction_id
                 AND t.firm_id = pc.firm_id AND t.matter_id = pc.matter_id
                WHERE pc.classification_id = %s AND pc.matter_id = %s AND pc.firm_id = %s
                FOR UPDATE OF pc, t
                """,
                (classification_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(classification_id)
            if row["status"] != ClassificationStatus.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active payment classification candidate can be approved")
            if row["transaction_status"] != TransactionStatus.CONFIRMED.value:
                raise CaseLedgerPersistenceBlocked("source transaction must be lawyer-confirmed before classification approval")
            allocation_rows = connection.execute(
                """
                SELECT obligation_id, amount, currency
                FROM case_payment_allocations
                WHERE classification_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY obligation_id ASC
                """,
                (classification_id, matter_id, actor.firm_id),
            ).fetchall()
            allocations = tuple(
                ObligationAllocation(
                    obligation_id=allocation["obligation_id"],
                    amount=Decimal(allocation["amount"]),
                    currency=allocation["currency"],
                )
                for allocation in allocation_rows
            )
            _validate_persistent_classification(
                transaction_amount=Decimal(row["amount"]),
                transaction_currency=row["currency"],
                nature=PaymentNature(row["nature"]),
                allocations=allocations,
            )
            connection.execute(
                """
                UPDATE case_payment_classifications
                SET status = 'INVALIDATED', approval_hash = NULL, approved_by = NULL, updated_at = now()
                WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'APPROVED' AND classification_id <> %s
                """,
                (row["transaction_id"], matter_id, actor.firm_id, classification_id),
            )
            updated = connection.execute(
                """
                UPDATE case_payment_classifications
                SET status = 'APPROVED', approval_hash = %s, approved_by = %s, updated_at = now()
                WHERE classification_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (approval_hash, actor.actor_id, classification_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise VersionConflict("payment classification changed before approval could be persisted")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="PAYMENT_CLASSIFICATION_APPROVED",
                object_type="PAYMENT_CLASSIFICATION",
                object_id=classification_id,
                audit_payload={
                    "classification_id": classification_id,
                    "transaction_id": str(row["transaction_id"]),
                    "nature": row["nature"],
                    "status": ClassificationStatus.APPROVED.value,
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def create_duplicate_group_candidate(
        self,
        *,
        matter_id: str,
        transaction_ids: tuple[str, ...],
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        normalized_transaction_ids = _validate_uuid_set("transaction_ids", transaction_ids, minimum=2)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        command_name = "CREATE_DUPLICATE_GROUP_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "transaction_ids": normalized_transaction_ids,
            "expected_version": expected_version,
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
                allowed_roles=self._CANDIDATE_ROLES,
            )
            rows = connection.execute(
                """
                SELECT transaction_id, status
                FROM case_transactions
                WHERE transaction_id = ANY(%s) AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (list(normalized_transaction_ids), matter_id, actor.firm_id),
            ).fetchall()
            if len(rows) != len(normalized_transaction_ids):
                raise CaseLedgerPersistenceBlocked("one or more duplicate candidates do not belong to this matter")
            if any(row["status"] == TransactionStatus.INVALIDATED.value for row in rows):
                raise CaseLedgerPersistenceBlocked("an invalidated transaction cannot enter a duplicate group")
            active = connection.execute(
                """
                SELECT member.transaction_id
                FROM case_transaction_duplicate_members member
                JOIN case_transaction_duplicate_groups duplicate_group
                  ON duplicate_group.duplicate_group_id = member.duplicate_group_id
                 AND duplicate_group.firm_id = member.firm_id
                 AND duplicate_group.matter_id = member.matter_id
                WHERE member.transaction_id = ANY(%s)
                  AND member.matter_id = %s AND member.firm_id = %s
                  AND duplicate_group.status <> 'INVALIDATED'
                LIMIT 1
                """,
                (list(normalized_transaction_ids), matter_id, actor.firm_id),
            ).fetchone()
            if active is not None:
                raise CaseLedgerPersistenceBlocked("a transaction already belongs to an active duplicate group")
            group_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_transaction_duplicate_groups (
                    duplicate_group_id, firm_id, matter_id, status
                ) VALUES (%s, %s, %s, 'CANDIDATE')
                """,
                (group_id, actor.firm_id, matter_id),
            )
            for transaction_id_value in normalized_transaction_ids:
                connection.execute(
                    """
                    INSERT INTO case_transaction_duplicate_members (
                        duplicate_group_id, transaction_id, firm_id, matter_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (group_id, transaction_id_value, actor.firm_id, matter_id),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="DUPLICATE_GROUP_CANDIDATE_CREATED",
                object_type="DUPLICATE_GROUP",
                object_id=group_id,
                audit_payload={"duplicate_group_id": group_id, "status": DuplicateStatus.CANDIDATE.value},
                stale_submission=False,
            )

    def resolve_duplicate_group(
        self,
        *,
        matter_id: str,
        duplicate_group_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        same_economic_event: bool,
        canonical_transaction_id: str | None,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("duplicate_group_id", duplicate_group_id)
        if canonical_transaction_id is not None:
            _validate_uuid("canonical_transaction_id", canonical_transaction_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("duplicate resolution approval_hash", approval_hash)
        if same_economic_event and canonical_transaction_id is None:
            raise CaseLedgerPersistenceBlocked("a same-event duplicate group requires a canonical transaction")
        if not same_economic_event and canonical_transaction_id is not None:
            raise CaseLedgerPersistenceBlocked("a distinct-events resolution cannot choose a canonical transaction")
        command_name = "RESOLVE_DUPLICATE_GROUP"
        payload = {
            "matter_id": matter_id,
            "duplicate_group_id": duplicate_group_id,
            "expected_version": expected_version,
            "same_economic_event": same_economic_event,
            "canonical_transaction_id": canonical_transaction_id,
            "approval_hash": approval_hash,
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
            group = connection.execute(
                """
                SELECT status
                FROM case_transaction_duplicate_groups
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (duplicate_group_id, matter_id, actor.firm_id),
            ).fetchone()
            if group is None:
                raise KeyError(duplicate_group_id)
            if group["status"] != DuplicateStatus.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active duplicate candidate group can be resolved")
            members = connection.execute(
                """
                SELECT transaction_id
                FROM case_transaction_duplicate_members
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY transaction_id ASC
                """,
                (duplicate_group_id, matter_id, actor.firm_id),
            ).fetchall()
            member_ids = {str(row["transaction_id"]) for row in members}
            if len(member_ids) < 2:
                raise CaseLedgerPersistenceBlocked("a duplicate group must retain at least two source transactions")
            if same_economic_event and canonical_transaction_id not in member_ids:
                raise CaseLedgerPersistenceBlocked("canonical transaction must be a member of the duplicate group")
            status = DuplicateStatus.SAME_ECONOMIC_EVENT if same_economic_event else DuplicateStatus.DISTINCT_EVENTS
            updated = connection.execute(
                """
                UPDATE case_transaction_duplicate_groups
                SET status = %s, canonical_transaction_id = %s, approval_hash = %s,
                    approved_by = %s, updated_at = now()
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (
                    status.value,
                    canonical_transaction_id,
                    approval_hash,
                    actor.actor_id,
                    duplicate_group_id,
                    matter_id,
                    actor.firm_id,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("duplicate group changed before resolution could be persisted")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="DUPLICATE_GROUP_RESOLVED",
                object_type="DUPLICATE_GROUP",
                object_id=duplicate_group_id,
                audit_payload={
                    "duplicate_group_id": duplicate_group_id,
                    "status": status.value,
                    "canonical_transaction_id": canonical_transaction_id,
                    "approval_hash": approval_hash,
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


def _validate_optional_money(
    amount: Decimal | None,
    currency: str | None,
    label: str,
    *,
    require_amount: bool = False,
) -> None:
    if amount is None:
        if require_amount or currency is not None:
            raise CaseLedgerPersistenceBlocked(f"{label} requires both amount and currency")
        return
    if amount < 0 or not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise CaseLedgerPersistenceBlocked(f"{label} amount must be non-negative with at most two decimal places")
    normalized_currency = (currency or "").strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise CaseLedgerPersistenceBlocked(f"{label} currency must use a three-letter code")


def _validate_persistent_classification(
    *,
    transaction_amount: Decimal,
    transaction_currency: str,
    nature: PaymentNature,
    allocations: tuple[ObligationAllocation, ...],
) -> None:
    financial_natures = {
        PaymentNature.DISBURSEMENT,
        PaymentNature.REPAYMENT_UNSPECIFIED,
        PaymentNature.INTEREST_PAYMENT,
        PaymentNature.PRINCIPAL_REPAYMENT,
    }
    if nature not in financial_natures:
        if allocations:
            raise CaseLedgerPersistenceBlocked("a non-calculation payment nature cannot allocate an obligation")
        return
    if not allocations:
        raise CaseLedgerPersistenceBlocked("a calculation-relevant payment nature requires an obligation allocation")
    normalized_ids = [allocation.obligation_id.strip() for allocation in allocations]
    if any(not obligation_id for obligation_id in normalized_ids):
        raise CaseLedgerPersistenceBlocked("allocation obligation_id is required")
    if len(set(normalized_ids)) != len(normalized_ids):
        raise CaseLedgerPersistenceBlocked("an obligation can appear only once in one payment classification")
    total = Decimal("0")
    normalized_transaction_currency = transaction_currency.strip().upper()
    for allocation in allocations:
        if allocation.amount <= 0 or not allocation.amount.is_finite():
            raise CaseLedgerPersistenceBlocked("allocation amount must be a positive finite decimal")
        normalized_currency = allocation.currency.strip().upper()
        if normalized_currency != normalized_transaction_currency:
            raise CaseLedgerPersistenceBlocked("every allocation currency must match the source transaction currency")
        total += allocation.amount
    if total != transaction_amount:
        raise CaseLedgerPersistenceBlocked("classification allocations must equal the full source transaction amount")


def _validate_uuid_set(label: str, values: tuple[str, ...], *, minimum: int) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if len(normalized) < minimum:
        raise CaseLedgerPersistenceBlocked(f"{label} requires at least {minimum} distinct value(s)")
    for value in normalized:
        _validate_uuid(label, value)
    return normalized


def _require_confirmed_fact_rows(
    connection: psycopg.Connection,
    *,
    fact_ids: tuple[str, ...],
    matter_id: str,
    firm_id: str,
) -> None:
    rows = connection.execute(
        """
        SELECT fact_id, status
        FROM case_facts
        WHERE fact_id = ANY(%s) AND matter_id = %s AND firm_id = %s
        FOR UPDATE
        """,
        (list(fact_ids), matter_id, firm_id),
    ).fetchall()
    if len(rows) != len(fact_ids) or any(row["status"] != FactStatus.CONFIRMED.value for row in rows):
        raise CaseLedgerPersistenceBlocked("responses and issues may only use lawyer-confirmed facts in this matter")


def _require_claim_rows(
    connection: psycopg.Connection,
    *,
    claim_ids: tuple[str, ...],
    matter_id: str,
    firm_id: str,
    confirmed: bool,
) -> None:
    rows = connection.execute(
        """
        SELECT claim_id, status
        FROM case_claims
        WHERE claim_id = ANY(%s) AND matter_id = %s AND firm_id = %s
        FOR UPDATE
        """,
        (list(claim_ids), matter_id, firm_id),
    ).fetchall()
    if len(rows) != len(claim_ids):
        raise CaseLedgerPersistenceBlocked("one or more claims do not belong to this matter")
    if confirmed and any(row["status"] != ClaimStatus.CONFIRMED_SCOPE.value for row in rows):
        raise CaseLedgerPersistenceBlocked("all referenced claims must have confirmed scope")


def _raise_missing_or_inactive(
    connection: psycopg.Connection,
    *,
    table: str,
    id_column: str,
    object_id: str,
    matter_id: str,
    firm_id: str,
    inactive_message: str,
) -> None:
    allowed = {
        ("case_claims", "claim_id"),
        ("case_payment_classifications", "classification_id"),
        ("case_transaction_duplicate_groups", "duplicate_group_id"),
    }
    if (table, id_column) not in allowed:
        raise ValueError("unsupported persistent object lookup")
    current = connection.execute(
        f"SELECT status FROM {table} WHERE {id_column} = %s AND matter_id = %s AND firm_id = %s",
        (object_id, matter_id, firm_id),
    ).fetchone()
    if current is None:
        raise KeyError(object_id)
    raise CaseLedgerPersistenceBlocked(inactive_message)


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
