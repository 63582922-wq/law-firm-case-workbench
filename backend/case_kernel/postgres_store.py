"""PostgreSQL 16+ implementation of the matter persistence port.

This adapter deliberately rejects the Alpha string identifiers: production
persistence uses UUID tenant, actor, matter, audit, and submission identifiers.
Every command is executed in one transaction after `app.firm_id` is set locally,
so row-level security, optimistic versioning, idempotency, audit, and outbox
records share one commit boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from typing import Callable, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import IdempotencyConflict, VersionConflict
from .models import (
    Actor,
    ApprovalBinding,
    AuditEvent,
    CommandReceipt,
    Matter,
    MatterStage,
    SubmissionBundle,
    SubmissionLifecycle,
    SubmissionValidity,
    Role,
)
from .store import StoredCommand


class PostgresMatterStore:
    """Synchronous repository adapter for provisioned UUID identities and PostgreSQL 16+."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def create(self, *, matter: Matter, actor: Actor, idempotency_key: str) -> CommandReceipt:
        _validate_identifiers(matter_id=matter.matter_id, firm_id=actor.firm_id, actor_id=actor.actor_id)
        if matter.firm_id != actor.firm_id:
            raise ValueError("matter firm must match actor firm")
        if Role.LEAD_LAWYER not in actor.roles:
            raise PermissionError("only a lead lawyer can create a matter")
        _require_key(idempotency_key)
        payload = {"command": "CREATE_MATTER", "title": matter.title}
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, _command_scope(actor.actor_id, matter.matter_id, "CREATE_MATTER", idempotency_key))
            prior = self._prior_command(
                connection,
                actor=actor,
                matter_id=matter.matter_id,
                command_name="CREATE_MATTER",
                idempotency_key=idempotency_key,
            )
            if prior is not None:
                return _resolve_idempotency(prior, payload_hash)

            connection.execute(
                """
                INSERT INTO matters (matter_id, firm_id, title, stage, version)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (matter.matter_id, actor.firm_id, matter.title, matter.stage.value, matter.version),
            )
            # A new case without an active owner is unusable: every subsequent
            # evidence or ledger command performs database role checks.  Grant
            # the creator the same lead role that the workflow required for
            # creation, in the very transaction that creates the matter.
            connection.execute(
                """
                INSERT INTO matter_actor_roles (matter_id, firm_id, user_id, role)
                VALUES (%s, %s, %s, 'LEAD_LAWYER')
                """,
                (matter.matter_id, actor.firm_id, actor.actor_id),
            )
            event = AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="MATTER_CREATED",
                input_version=0,
                payload={"title": matter.title},
            )
            self._insert_audit_and_outbox(connection, event)
            receipt = CommandReceipt(
                command_name="CREATE_MATTER",
                idempotency_key=idempotency_key,
                matter_id=matter.matter_id,
                matter_version=matter.version,
                audit_event_id=event.event_id,
            )
            self._record_command(
                connection,
                actor=actor,
                matter_id=matter.matter_id,
                command_name="CREATE_MATTER",
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                receipt=receipt,
            )
            return receipt

    def get(self, matter_id: str, *, firm_id: str | None = None) -> Matter:
        if firm_id is None:
            raise ValueError("firm_id is required for PostgreSQL matter reads")
        _validate_identifiers(matter_id=matter_id, firm_id=firm_id, actor_id=None)
        with self._transaction(firm_id) as connection:
            return self._load_matter(connection, matter_id=matter_id, lock=False)

    def audit_events(self, matter_id: str, *, firm_id: str | None = None) -> list[AuditEvent]:
        if firm_id is None:
            raise ValueError("firm_id is required for PostgreSQL audit reads")
        _validate_identifiers(matter_id=matter_id, firm_id=firm_id, actor_id=None)
        with self._transaction(firm_id) as connection:
            rows = connection.execute(
                """
                SELECT event_id, matter_id, firm_id, actor_id, event_type,
                       input_version, output_version, occurred_at, payload
                FROM audit_events
                WHERE matter_id = %s
                ORDER BY occurred_at ASC, event_id ASC
                """,
                (matter_id,),
            ).fetchall()
        return [
            AuditEvent(
                event_id=str(row["event_id"]),
                matter_id=str(row["matter_id"]),
                firm_id=str(row["firm_id"]),
                actor_id=str(row["actor_id"]),
                event_type=row["event_type"],
                input_version=row["input_version"],
                output_version=row["output_version"],
                occurred_at=row["occurred_at"],
                payload=row["payload"],
            )
            for row in rows
        ]

    def mutate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        command_name: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, str],
        mutate_matter: Callable[[Matter], AuditEvent],
    ) -> CommandReceipt:
        _validate_identifiers(matter_id=matter_id, firm_id=actor.firm_id, actor_id=actor.actor_id)
        _require_key(idempotency_key)
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, _command_scope(actor.actor_id, matter_id, command_name, idempotency_key))
            prior = self._prior_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            if prior is not None:
                return _resolve_idempotency(prior, payload_hash)

            before = self._load_matter(connection, matter_id=matter_id, lock=True)
            if before.version != expected_version:
                raise VersionConflict(f"expected matter version {expected_version}, current version is {before.version}")
            after = deepcopy(before)
            event = mutate_matter(after)
            if event.input_version != before.version or event.output_version != after.version:
                raise ValueError("workflow emitted an audit event with inconsistent matter versions")
            self._persist_mutation(connection, before=before, after=after, event=event, actor=actor)
            receipt = CommandReceipt(
                command_name=command_name,
                idempotency_key=idempotency_key,
                matter_id=matter_id,
                matter_version=after.version,
                audit_event_id=event.event_id,
            )
            self._record_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                receipt=receipt,
            )
            return receipt

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    def _prior_command(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        command_name: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
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
        response = row["response_json"]
        return StoredCommand(
            payload_hash=row["request_hash"],
            receipt=CommandReceipt(
                command_name=response["command_name"],
                idempotency_key=response["idempotency_key"],
                matter_id=response["matter_id"],
                matter_version=response["matter_version"],
                audit_event_id=response["audit_event_id"],
            ),
        )

    def _load_matter(self, connection: psycopg.Connection, *, matter_id: str, lock: bool) -> Matter:
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            f"""
            SELECT matter_id, firm_id, title, stage, version, current_submission_bundle_id
            FROM matters
            WHERE matter_id = %s{suffix}
            """,
            (matter_id,),
        ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        bundle_rows = connection.execute(
            """
            SELECT bundle_id, lifecycle, validity, final_text_hash, approved_by, approved_matter_version
            FROM submission_bundles
            WHERE matter_id = %s
            """,
            (matter_id,),
        ).fetchall()
        approval_rows = connection.execute(
            """
            SELECT DISTINCT ON (approval_type)
                   approval_type, object_hash, approved_matter_version, approved_by
            FROM approvals
            WHERE matter_id = %s AND revoked_at IS NULL
            ORDER BY approval_type, approved_matter_version DESC
            """,
            (matter_id,),
        ).fetchall()
        return Matter(
            matter_id=str(row["matter_id"]),
            firm_id=str(row["firm_id"]),
            title=row["title"],
            stage=MatterStage(row["stage"]),
            version=row["version"],
            current_submission_bundle_id=(str(row["current_submission_bundle_id"]) if row["current_submission_bundle_id"] else None),
            bundles={
                str(bundle["bundle_id"]): SubmissionBundle(
                    bundle_id=str(bundle["bundle_id"]),
                    lifecycle=SubmissionLifecycle(bundle["lifecycle"]),
                    validity=SubmissionValidity(bundle["validity"]),
                    final_text_hash=bundle["final_text_hash"],
                    approved_by=str(bundle["approved_by"]),
                    approved_matter_version=bundle["approved_matter_version"],
                )
                for bundle in bundle_rows
            },
            approvals={
                approval["approval_type"]: ApprovalBinding(
                    approval_type=approval["approval_type"],
                    object_hash=approval["object_hash"],
                    approved_matter_version=approval["approved_matter_version"],
                    approved_by=str(approval["approved_by"]),
                )
                for approval in approval_rows
            },
        )

    def _persist_mutation(
        self,
        connection: psycopg.Connection,
        *,
        before: Matter,
        after: Matter,
        event: AuditEvent,
        actor: Actor,
    ) -> None:
        for approval_type in set(before.approvals) - set(after.approvals):
            connection.execute(
                "UPDATE approvals SET revoked_at = now() WHERE matter_id = %s AND approval_type = %s AND revoked_at IS NULL",
                (after.matter_id, approval_type),
            )
        for approval_type, approval in after.approvals.items():
            if before.approvals.get(approval_type) == approval:
                continue
            connection.execute(
                "UPDATE approvals SET revoked_at = now() WHERE matter_id = %s AND approval_type = %s AND revoked_at IS NULL",
                (after.matter_id, approval_type),
            )
            connection.execute(
                """
                INSERT INTO approvals (
                    matter_id, firm_id, approval_type, object_hash,
                    approved_matter_version, approved_by, permission_snapshot
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    after.matter_id,
                    after.firm_id,
                    approval.approval_type,
                    approval.object_hash,
                    approval.approved_matter_version,
                    approval.approved_by,
                    Jsonb({"actor_roles": sorted(role.value for role in actor.roles)}),
                ),
            )
        for bundle_id, bundle in after.bundles.items():
            prior = before.bundles.get(bundle_id)
            if prior is None:
                connection.execute(
                    """
                    INSERT INTO submission_bundles (
                        bundle_id, matter_id, firm_id, lifecycle, validity, final_text_hash,
                        approved_by, approved_matter_version, locked_at, exported_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                              CASE WHEN %s = 'LOCKED' THEN now() ELSE NULL END,
                              CASE WHEN %s = 'EXPORTED' THEN now() ELSE NULL END)
                    """,
                    (
                        bundle.bundle_id,
                        after.matter_id,
                        after.firm_id,
                        bundle.lifecycle.value,
                        bundle.validity.value,
                        bundle.final_text_hash,
                        bundle.approved_by,
                        bundle.approved_matter_version,
                        bundle.lifecycle.value,
                        bundle.lifecycle.value,
                    ),
                )
            elif prior != bundle:
                connection.execute(
                    """
                    UPDATE submission_bundles
                    SET lifecycle = %s, validity = %s,
                        exported_at = CASE WHEN %s = 'EXPORTED' AND exported_at IS NULL THEN now() ELSE exported_at END
                    WHERE bundle_id = %s AND matter_id = %s
                    """,
                    (bundle.lifecycle.value, bundle.validity.value, bundle.lifecycle.value, bundle.bundle_id, after.matter_id),
                )
        updated = connection.execute(
            """
            UPDATE matters
            SET title = %s, stage = %s, version = %s, current_submission_bundle_id = %s, updated_at = now()
            WHERE matter_id = %s AND firm_id = %s AND version = %s
            """,
            (
                after.title,
                after.stage.value,
                after.version,
                after.current_submission_bundle_id,
                after.matter_id,
                after.firm_id,
                before.version,
            ),
        )
        if updated.rowcount != 1:
            raise VersionConflict("matter changed before this transaction could be persisted")
        self._insert_audit_and_outbox(connection, event)

    def _insert_audit_and_outbox(self, connection: psycopg.Connection, event: AuditEvent) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, firm_id, matter_id, actor_id, event_type,
                input_version, output_version, occurred_at, payload, request_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.firm_id,
                event.matter_id,
                event.actor_id,
                event.event_type,
                event.input_version,
                event.output_version,
                event.occurred_at,
                Jsonb(dict(event.payload)),
                str(uuid4()),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_events (firm_id, matter_id, aggregate_version, event_type, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (event.firm_id, event.matter_id, event.output_version, event.event_type, Jsonb({"audit_event_id": event.event_id})),
        )

    def _record_command(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        command_name: str,
        idempotency_key: str,
        payload_hash: str,
        receipt: CommandReceipt,
    ) -> None:
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
                Jsonb(
                    {
                        "command_name": receipt.command_name,
                        "idempotency_key": receipt.idempotency_key,
                        "matter_id": receipt.matter_id,
                        "matter_version": receipt.matter_version,
                        "audit_event_id": receipt.audit_event_id,
                    }
                ),
            ),
        )


def _validate_identifiers(*, matter_id: str, firm_id: str, actor_id: str | None) -> None:
    for label, value in (("matter_id", matter_id), ("firm_id", firm_id), ("actor_id", actor_id)):
        if value is None:
            continue
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"PostgreSQL persistence requires UUID {label}; Alpha identifiers are not accepted") from error


def _require_key(value: str) -> None:
    if not value.strip():
        raise ValueError("idempotency_key is required")


def _payload_hash(payload: dict[str, str]) -> str:
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _command_scope(actor_id: str, matter_id: str, command_name: str, idempotency_key: str) -> str:
    return "|".join((actor_id, matter_id, command_name, idempotency_key))


def _advisory_lock(connection: psycopg.Connection, scope: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope,))


def _resolve_idempotency(prior: StoredCommand, payload_hash: str) -> CommandReceipt:
    if prior.payload_hash != payload_hash:
        raise IdempotencyConflict("idempotency key was reused with different input")
    return prior.receipt
