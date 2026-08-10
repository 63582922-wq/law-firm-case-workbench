"""Append-only preflight and attempt ledger for external AI/OCR/MCP calls."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

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
)
from .models import Actor, Role


@dataclass(frozen=True)
class ExternalRequestPreflight:
    request_kind: str
    purpose: str
    provider_id: str
    processor_region: str
    retention_policy: str
    training_policy: str
    selected_field_ids: tuple[str, ...]
    service_id: str
    call_cap: int
    cost_cap_minor: int
    input_hash: str
    authorization_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class PersistentExternalRequestSnapshot:
    matter_id: str
    matter_version: int
    authorizations: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresExternalRequestStore:
    _AUTHORIZE_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _EXECUTE_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER})
    _KINDS = frozenset({"MODEL", "OCR", "MCP"})
    _STATUSES = frozenset({"SUBMISSION_STARTED", "SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION", "CANCELLED", "EXPIRED"})

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def authorize_external_request(
        self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str,
        preflight: ExternalRequestPreflight, now: datetime | None = None,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._AUTHORIZE_ROLES)
        _require_positive_version(expected_version)
        normalized = _validate_preflight(preflight, now=now)
        command_name = "AUTHORIZE_EXTERNAL_REQUEST"
        payload = {"matter_id": matter_id, "expected_version": expected_version, "preflight": _preflight_payload(normalized)}
        request_hash = _payload_hash(payload)
        request_id = str(uuid4())
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, idempotency_key=idempotency_key, command_name=command_name, payload_hash=request_hash, allowed_roles=self._AUTHORIZE_ROLES)
            if prior is not None:
                return prior
            connection.execute(
                """
                INSERT INTO external_request_authorizations (
                    request_id, firm_id, matter_id, authorized_by, request_kind, purpose,
                    provider_id, processor_region, retention_policy, training_policy,
                    selected_field_ids, service_id, call_cap, cost_cap_minor, input_hash,
                    authorization_hash, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                """,
                (request_id, actor.firm_id, matter_id, actor.actor_id, normalized.request_kind,
                 normalized.purpose, normalized.provider_id, normalized.processor_region,
                 normalized.retention_policy, normalized.training_policy,
                 json.dumps(normalized.selected_field_ids, ensure_ascii=False, separators=(",", ":")),
                 normalized.service_id, normalized.call_cap, normalized.cost_cap_minor,
                 normalized.input_hash, normalized.authorization_hash, normalized.expires_at),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="EXTERNAL_REQUEST_AUTHORIZED", object_type="EXTERNAL_REQUEST", object_id=request_id,
                audit_payload={"request_id": request_id, "request_kind": normalized.request_kind,
                               "provider_id": normalized.provider_id, "processor_region": normalized.processor_region,
                               "service_id": normalized.service_id, "selected_field_count": len(normalized.selected_field_ids),
                               "call_cap": normalized.call_cap, "cost_cap_minor": normalized.cost_cap_minor,
                               "input_hash": normalized.input_hash, "authorization_hash": normalized.authorization_hash,
                               "expires_at": normalized.expires_at.isoformat()},
                stale_submission=False, stale_calculations=False,
            )

    def record_external_attempt(
        self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str,
        request_id: str, status: str, provider_request_ref_hash: str | None = None,
        output_hash: str | None = None, error_code: str | None = None, now: datetime | None = None,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._EXECUTE_ROLES)
        _require_positive_version(expected_version)
        _validate_uuid("request_id", request_id)
        _validate_attempt_shape(status, provider_request_ref_hash, output_hash, error_code)
        current = _now(now)
        command_name = "RECORD_EXTERNAL_REQUEST_ATTEMPT"
        payload = {"matter_id": matter_id, "expected_version": expected_version, "request_id": request_id,
                   "status": status, "provider_request_ref_hash": provider_request_ref_hash,
                   "output_hash": output_hash, "error_code": error_code}
        request_hash = _payload_hash(payload)
        attempt_id = str(uuid4())
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, idempotency_key=idempotency_key, command_name=command_name, payload_hash=request_hash, allowed_roles=self._EXECUTE_ROLES)
            if prior is not None:
                return prior
            authorization = connection.execute(
                """
                SELECT request_id, call_cap, expires_at
                FROM external_request_authorizations
                WHERE request_id = %s AND matter_id = %s AND firm_id = %s
                FOR KEY SHARE
                """, (request_id, matter_id, actor.firm_id),
            ).fetchone()
            if authorization is None:
                raise KeyError(request_id)
            attempts = connection.execute(
                """
                SELECT sequence, status FROM external_request_attempts
                WHERE request_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY sequence DESC FOR UPDATE
                """, (request_id, matter_id, actor.firm_id),
            ).fetchall()
            latest = attempts[0] if attempts else None
            if latest is not None and latest["status"] == "UNKNOWN_SUBMISSION":
                raise CaseLedgerPersistenceBlocked("unknown external submission must be reconciled before any retry")
            if latest is not None and latest["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
                raise CaseLedgerPersistenceBlocked("external request is terminal; create a new lawyer-authorized request")
            if len(attempts) >= authorization["call_cap"]:
                raise CaseLedgerPersistenceBlocked("external request call cap is exhausted")
            if authorization["expires_at"] <= current:
                if status != "EXPIRED":
                    raise CaseLedgerPersistenceBlocked("external request authorization expired before this attempt")
            elif status == "EXPIRED":
                raise CaseLedgerPersistenceBlocked("unexpired external request cannot be recorded as expired")
            if status == "SUBMISSION_STARTED" and latest is not None:
                raise CaseLedgerPersistenceBlocked("external request already started; reconcile its result instead of resubmitting")
            if status != "SUBMISSION_STARTED" and (latest is None or latest["status"] != "SUBMISSION_STARTED"):
                raise CaseLedgerPersistenceBlocked("external request outcome requires a prior submission-started receipt")
            sequence = len(attempts) + 1
            connection.execute(
                """
                INSERT INTO external_request_attempts (
                    attempt_id, firm_id, matter_id, request_id, attempted_by, sequence, status,
                    provider_request_ref_hash, output_hash, error_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (attempt_id, actor.firm_id, matter_id, request_id, actor.actor_id, sequence, status,
                 provider_request_ref_hash, output_hash, error_code.strip() if isinstance(error_code, str) else None),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="EXTERNAL_REQUEST_ATTEMPT_RECORDED", object_type="EXTERNAL_REQUEST_ATTEMPT", object_id=attempt_id,
                audit_payload={"attempt_id": attempt_id, "request_id": request_id, "sequence": sequence,
                               "status": status, "provider_request_ref_hash": provider_request_ref_hash,
                               "output_hash": output_hash, "error_code": error_code.strip() if isinstance(error_code, str) else None},
                stale_submission=False, stale_calculations=False,
            )

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentExternalRequestSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES)
            matter = connection.execute("SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s", (matter_id, actor.firm_id)).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            authorizations = _serialize_rows(connection.execute(
                "SELECT request_id, request_kind, purpose, provider_id, processor_region, retention_policy, training_policy, selected_field_ids, service_id, call_cap, cost_cap_minor, input_hash, authorization_hash, expires_at, authorized_at FROM external_request_authorizations WHERE matter_id = %s AND firm_id = %s ORDER BY authorized_at DESC, request_id DESC",
                (matter_id, actor.firm_id),
            ).fetchall())
            attempts = _serialize_rows(connection.execute(
                "SELECT attempt_id, request_id, sequence, status, provider_request_ref_hash, output_hash, error_code, created_at FROM external_request_attempts WHERE matter_id = %s AND firm_id = %s ORDER BY created_at DESC, attempt_id DESC",
                (matter_id, actor.firm_id),
            ).fetchall())
        payload = {"matter_id": matter_id, "matter_version": matter["version"], "authorizations": authorizations, "attempts": attempts}
        return PersistentExternalRequestSnapshot(matter_id, matter["version"], tuple(authorizations), tuple(attempts), _payload_hash(payload))

    def _begin(self, connection, *, actor: Actor, matter_id: str, expected_version: int, idempotency_key: str, command_name: str, payload_hash: str, allowed_roles: frozenset[Role]) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
        prior = _prior_receipt(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash)
        if prior is not None:
            return prior
        _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, allowed_roles=allowed_roles)
        return None

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


def _validate_preflight(value: ExternalRequestPreflight, *, now: datetime | None) -> ExternalRequestPreflight:
    if not isinstance(value, ExternalRequestPreflight) or value.request_kind not in PostgresExternalRequestStore._KINDS:
        raise CaseLedgerPersistenceBlocked("external request kind is invalid")
    for label, text in (("purpose", value.purpose), ("provider_id", value.provider_id), ("processor_region", value.processor_region), ("retention_policy", value.retention_policy), ("training_policy", value.training_policy), ("service_id", value.service_id)):
        _require_text(text, label)
        if len(text) > 240:
            raise CaseLedgerPersistenceBlocked(f"external request {label} exceeds boundary")
    if not 1 <= len(value.selected_field_ids) <= 30 or len(set(value.selected_field_ids)) != len(value.selected_field_ids) or any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in value.selected_field_ids):
        raise CaseLedgerPersistenceBlocked("external request selected fields are invalid")
    if not 1 <= value.call_cap <= 100 or not 0 <= value.cost_cap_minor <= 10_000_000:
        raise CaseLedgerPersistenceBlocked("external request call or cost cap is invalid")
    _validate_sha256("input_hash", value.input_hash)
    _validate_sha256("authorization_hash", value.authorization_hash)
    current = _now(now)
    if value.expires_at.tzinfo is None or not timedelta(minutes=1) <= value.expires_at - current <= timedelta(hours=24):
        raise CaseLedgerPersistenceBlocked("external request authorization expiry must be 1 minute to 24 hours")
    return value


def _validate_attempt_shape(status: str, provider_ref: str | None, output_hash: str | None, error_code: str | None) -> None:
    if status not in PostgresExternalRequestStore._STATUSES:
        raise CaseLedgerPersistenceBlocked("external request attempt status is invalid")
    if status == "SUBMISSION_STARTED":
        if provider_ref is None or output_hash is not None or error_code is not None:
            raise CaseLedgerPersistenceBlocked("submission-started receipt requires only provider request reference hash")
        _validate_sha256("provider_request_ref_hash", provider_ref)
    elif status == "SUCCEEDED":
        if output_hash is None or provider_ref is not None or error_code is not None:
            raise CaseLedgerPersistenceBlocked("successful external request receipt requires only output hash")
        _validate_sha256("output_hash", output_hash)
    elif provider_ref is not None or output_hash is not None or not isinstance(error_code, str) or not error_code.strip() or len(error_code) > 160:
        raise CaseLedgerPersistenceBlocked("unsuccessful external request receipt requires only stable error code")


def _preflight_payload(value: ExternalRequestPreflight) -> dict[str, Any]:
    return {"request_kind": value.request_kind, "purpose": value.purpose, "provider_id": value.provider_id, "processor_region": value.processor_region, "retention_policy": value.retention_policy, "training_policy": value.training_policy, "selected_field_ids": list(value.selected_field_ids), "service_id": value.service_id, "call_cap": value.call_cap, "cost_cap_minor": value.cost_cap_minor, "input_hash": value.input_hash, "authorization_hash": value.authorization_hash, "expires_at": value.expires_at.isoformat()}


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise CaseLedgerPersistenceBlocked("external request time must be timezone-aware")
    return current


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise CaseLedgerPersistenceBlocked(f"{label} must be a UUID") from error


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value.isoformat() if hasattr(value, "isoformat") else str(value) if key.endswith("_id") and value is not None else value for key, value in row.items()} for row in rows]
