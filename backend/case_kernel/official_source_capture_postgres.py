"""PostgreSQL workflow for lawyer-authorized official-source capture runs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from ipaddress import ip_address
import json
import re
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
from .models import Actor, Role
from .research_gateway import PUBLIC_SOURCES


_SOURCES = {item.source_id: item for item in PUBLIC_SOURCES}
_PARSER_KINDS = frozenset(
    {
        "PRIVATE_LENDING_SECOND_REVISION",
        "PRIVATE_LENDING_FIRST_REVISION",
        "PRIVATE_LENDING_2015_ORIGINAL",
        "CIVIL_CODE_BORROWING",
        "CFETS_LPR_JSON",
        "CFETS_LPR_ANNOUNCEMENT",
    }
)
_MAX_PARSED_SUMMARY_BYTES = 64 * 1024
_CAPTURE_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/pdf",
        "application/json",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


@dataclass(frozen=True)
class OfficialSourceCaptureRunLease:
    run_id: str
    matter_id: str
    matter_version: int
    lease_id: str
    lease_expires_at: datetime
    source_id: str
    publisher: str
    source_tier: str
    target_url: str
    query_sha256: str
    authorization_hash: str
    authorized_by: str
    authorized_at: datetime
    max_response_bytes: int


@dataclass(frozen=True)
class PersistentOfficialSourceCaptureSnapshot:
    matter_id: str
    matter_version: int
    runs: tuple[dict[str, Any], ...]
    reviews: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresOfficialSourceCaptureStore:
    _REVIEW_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _WORKER_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )

    def __init__(
        self,
        dsn: str,
        *,
        artifact_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._artifact_reader = artifact_reader

    def queue_capture(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        source_id: str,
        target_url: str,
        query_sha256: str,
        authorization_hash: str,
        max_response_bytes: int = 32 * 1024 * 1024,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._REVIEW_ROLES)
        source = _source(source_id)
        _validate_source_url(target_url, allowed_domains=source.allowed_domains)
        _validate_sha256("query_sha256", query_sha256)
        _validate_sha256("authorization_hash", authorization_hash)
        if max_response_bytes < 1 or max_response_bytes > 64 * 1024 * 1024:
            raise CaseLedgerPersistenceBlocked("official source response byte limit is invalid")
        command_name = "QUEUE_OFFICIAL_SOURCE_CAPTURE"
        run_id = str(uuid4())
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "source_id": source.source_id,
            "publisher": source.publisher,
            "source_tier": source.source_tier,
            "target_url": target_url,
            "query_sha256": query_sha256,
            "authorization_hash": authorization_hash,
            "max_response_bytes": max_response_bytes,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._REVIEW_ROLES,
            )
            if prior is not None:
                return prior
            connection.execute(
                """
                INSERT INTO official_source_capture_runs (
                    run_id, firm_id, matter_id, source_id, publisher, source_tier,
                    target_url, query_sha256, authorization_hash, max_response_bytes,
                    status, attempt_count, authorized_by, authorized_at,
                    authorization_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'QUEUED', 0, %s, now(), now() + interval '15 minutes')
                """,
                (
                    run_id,
                    actor.firm_id,
                    matter_id,
                    source.source_id,
                    source.publisher,
                    source.source_tier,
                    target_url,
                    query_sha256,
                    authorization_hash,
                    max_response_bytes,
                    actor.actor_id,
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
                event_type="OFFICIAL_SOURCE_CAPTURE_QUEUED",
                object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "source_id": source.source_id,
                    "target_url": target_url,
                    "query_sha256": query_sha256,
                    "authorization_hash": authorization_hash,
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def claim_capture(
        self,
        *,
        matter_id: str,
        run_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> OfficialSourceCaptureRunLease:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._WORKER_ROLES)
        _validate_uuid("run_id", run_id)
        if lease_seconds < 30 or lease_seconds > 300:
            raise CaseLedgerPersistenceBlocked("official source capture lease must be 30 to 300 seconds")
        lease_id = str(uuid5(NAMESPACE_URL, f"lawcase:official-source:{actor.firm_id}:{matter_id}:{run_id}:{idempotency_key}"))
        command_name = "CLAIM_OFFICIAL_SOURCE_CAPTURE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "run_id": run_id,
            "lease_id": lease_id,
            "lease_seconds": lease_seconds,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._WORKER_ROLES,
            )
            if prior is not None:
                row = self._load_run(connection, actor=actor, matter_id=matter_id, run_id=run_id)
                if row["status"] != "RUNNING" or str(row["lease_id"]) != lease_id:
                    raise CaseLedgerPersistenceBlocked("prior capture claim no longer owns the lease")
                return _lease(row, matter_version=prior.matter_version)
            row = self._load_run(connection, actor=actor, matter_id=matter_id, run_id=run_id, for_update=True)
            if row["status"] != "QUEUED" or row["attempt_count"] != 0:
                raise CaseLedgerPersistenceBlocked("official source capture is not queued")
            valid = connection.execute(
                "SELECT authorization_expires_at > now() AS valid FROM official_source_capture_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()["valid"]
            if valid is not True:
                raise CaseLedgerPersistenceBlocked("official source capture authorization expired before claim")
            claimed = connection.execute(
                """
                UPDATE official_source_capture_runs
                SET status = 'RUNNING', attempt_count = 1, lease_id = %s,
                    lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                RETURNING *
                """,
                (lease_id, lease_seconds, run_id, matter_id, actor.firm_id),
            ).fetchone()
            receipt = _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="OFFICIAL_SOURCE_CAPTURE_CLAIMED",
                object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
                object_id=run_id,
                audit_payload={"run_id": run_id, "lease_id": lease_id},
                stale_submission=False,
                stale_calculations=False,
            )
            return _lease(claimed, matter_version=receipt.matter_version)

    def complete_capture(
        self,
        *,
        matter_id: str,
        run_id: str,
        lease_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        final_url: str,
        retrieved_at: datetime,
        peer_ip: str,
        content_media_type: str,
        content_sha256: str,
        content_bytes: int,
        storage_object_key: str,
        capture_verification_hash: str,
        parser_kind: str,
        parsed_output_hash: str,
        parsed_summary: dict[str, Any],
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._WORKER_ROLES)
        _validate_uuid("run_id", run_id)
        _validate_uuid("lease_id", lease_id)
        for label, value in (
            ("content_sha256", content_sha256),
            ("capture_verification_hash", capture_verification_hash),
            ("parsed_output_hash", parsed_output_hash),
        ):
            _validate_sha256(label, value)
        if parser_kind not in _PARSER_KINDS:
            raise CaseLedgerPersistenceBlocked("unsupported official source parser kind")
        if content_media_type not in _CAPTURE_MEDIA_TYPES:
            raise CaseLedgerPersistenceBlocked("unsupported official source capture media type")
        try:
            peer = ip_address(peer_ip)
        except ValueError as error:
            raise CaseLedgerPersistenceBlocked("official source capture peer IP is invalid") from error
        if not peer.is_global:
            raise CaseLedgerPersistenceBlocked("official source capture peer IP is not globally routable")
        if retrieved_at.tzinfo is None:
            raise CaseLedgerPersistenceBlocked("official source retrieval time must include timezone")
        _validate_parsed_summary(parsed_summary)
        plaintext = self._read_artifact(storage_object_key, content_sha256)
        if len(plaintext) != content_bytes:
            raise CaseLedgerPersistenceBlocked("official source encrypted object byte size differs")
        command_name = "COMPLETE_OFFICIAL_SOURCE_CAPTURE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "run_id": run_id,
            "lease_id": lease_id,
            "final_url": final_url,
            "retrieved_at": retrieved_at,
            "peer_ip": peer_ip,
            "content_media_type": content_media_type,
            "content_sha256": content_sha256,
            "content_bytes": content_bytes,
            "storage_object_key": storage_object_key,
            "capture_verification_hash": capture_verification_hash,
            "parser_kind": parser_kind,
            "parsed_output_hash": parsed_output_hash,
            "parsed_summary": parsed_summary,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._WORKER_ROLES,
            )
            if prior is not None:
                return prior
            run = self._load_run(connection, actor=actor, matter_id=matter_id, run_id=run_id, for_update=True)
            _validate_active_lease(connection, run=run, lease_id=lease_id)
            source = _source(run["source_id"])
            _validate_source_url(final_url, allowed_domains=source.allowed_domains)
            if final_url != run["target_url"]:
                raise CaseLedgerPersistenceBlocked("official source final URL differs from approved target")
            if content_bytes < 1 or content_bytes > run["max_response_bytes"]:
                raise CaseLedgerPersistenceBlocked("official source result exceeds approved byte limit")
            connection.execute(
                """
                UPDATE official_source_capture_runs
                SET status = 'REVIEW_REQUIRED', lease_id = NULL, lease_expires_at = NULL,
                    final_url = %s, retrieved_at = %s, peer_ip = %s,
                    content_media_type = %s, content_sha256 = %s, content_bytes = %s,
                    storage_object_key = %s, capture_verification_hash = %s,
                    parser_kind = %s, parsed_output_hash = %s, parsed_summary = %s,
                    completed_at = now(), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (
                    final_url,
                    retrieved_at,
                    peer_ip,
                    content_media_type,
                    content_sha256,
                    content_bytes,
                    storage_object_key,
                    capture_verification_hash,
                    parser_kind,
                    parsed_output_hash,
                    Jsonb(parsed_summary),
                    run_id,
                    matter_id,
                    actor.firm_id,
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
                event_type="OFFICIAL_SOURCE_CAPTURE_REVIEW_REQUIRED",
                object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "source_id": run["source_id"],
                    "content_sha256": content_sha256,
                    "capture_verification_hash": capture_verification_hash,
                    "parsed_output_hash": parsed_output_hash,
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def fail_capture(
        self,
        *,
        matter_id: str,
        run_id: str,
        lease_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        failure_code: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._WORKER_ROLES)
        _validate_uuid("run_id", run_id)
        _validate_uuid("lease_id", lease_id)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", failure_code):
            raise CaseLedgerPersistenceBlocked("official source capture failure code is invalid")
        command_name = "FAIL_OFFICIAL_SOURCE_CAPTURE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "run_id": run_id,
            "lease_id": lease_id,
            "failure_code": failure_code,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._WORKER_ROLES,
            )
            if prior is not None:
                return prior
            run = self._load_run(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                for_update=True,
            )
            _validate_active_lease(connection, run=run, lease_id=lease_id)
            connection.execute(
                """
                UPDATE official_source_capture_runs
                SET status = 'FAILED', lease_id = NULL, lease_expires_at = NULL,
                    failure_code = %s, completed_at = now(), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (failure_code, run_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="OFFICIAL_SOURCE_CAPTURE_FAILED",
                object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
                object_id=run_id,
                audit_payload={"run_id": run_id, "source_id": run["source_id"], "failure_code": failure_code},
                stale_submission=False,
                stale_calculations=False,
            )

    def review_capture(
        self,
        *,
        matter_id: str,
        run_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        decision: str,
        provision_locator: str,
        review_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._REVIEW_ROLES)
        _validate_uuid("run_id", run_id)
        if decision not in {"APPROVE_FOR_REGISTRATION", "REJECT"}:
            raise CaseLedgerPersistenceBlocked("unsupported official source review decision")
        _require_text(provision_locator, "provision_locator")
        _validate_sha256("review_hash", review_hash)
        command_name = "REVIEW_OFFICIAL_SOURCE_CAPTURE"
        review_id = str(uuid4())
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "run_id": run_id,
            "decision": decision,
            "provision_locator": provision_locator.strip(),
            "review_hash": review_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._REVIEW_ROLES,
            )
            if prior is not None:
                return prior
            run = self._load_run(connection, actor=actor, matter_id=matter_id, run_id=run_id, for_update=True)
            if run["status"] != "REVIEW_REQUIRED":
                raise CaseLedgerPersistenceBlocked("official source capture is not ready for lawyer review")
            connection.execute(
                """
                INSERT INTO official_source_capture_reviews (
                    review_id, run_id, firm_id, matter_id, decision,
                    provision_locator, review_hash, reviewed_by, reviewed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    review_id,
                    run_id,
                    actor.firm_id,
                    matter_id,
                    decision,
                    provision_locator.strip(),
                    review_hash,
                    actor.actor_id,
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
                event_type="OFFICIAL_SOURCE_CAPTURE_REVIEWED",
                object_type="OFFICIAL_SOURCE_CAPTURE_REVIEW",
                object_id=review_id,
                audit_payload={
                    "review_id": review_id,
                    "run_id": run_id,
                    "decision": decision,
                    "review_hash": review_hash,
                    "content_sha256": run["content_sha256"],
                    "parsed_output_hash": run["parsed_output_hash"],
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def get_snapshot(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentOfficialSourceCaptureSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
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
            runs = connection.execute(
                """
                SELECT run_id, source_id, publisher, source_tier, target_url,
                       query_sha256, max_response_bytes, status, attempt_count,
                       authorized_by, authorized_at, authorization_expires_at,
                       final_url, retrieved_at, peer_ip::text AS peer_ip,
                       content_media_type, content_sha256, content_bytes,
                       capture_verification_hash, parser_kind, parsed_output_hash,
                       parsed_summary, failure_code, completed_at, stale_at, stale_reason
                FROM official_source_capture_runs
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY created_at DESC, run_id DESC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            reviews = connection.execute(
                """
                SELECT review_id, run_id, decision, provision_locator,
                       review_hash, reviewed_by, reviewed_at
                FROM official_source_capture_reviews
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY reviewed_at DESC, review_id DESC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        serialized_runs = tuple(_serialize_row(item) for item in runs)
        serialized_reviews = tuple(_serialize_row(item) for item in reviews)
        payload = {
            "matter_id": matter_id,
            "matter_version": matter["version"],
            "runs": serialized_runs,
            "reviews": serialized_reviews,
        }
        return PersistentOfficialSourceCaptureSnapshot(
            **payload,
            snapshot_hash=_payload_hash(payload),
        )

    def _read_artifact(self, object_key: str, expected_hash: str) -> bytes:
        expected_key = f"{expected_hash[:2]}/{expected_hash[2:4]}/{expected_hash}.lca"
        if object_key != expected_key:
            raise CaseLedgerPersistenceBlocked("official source object key is not hash-bound")
        if self._artifact_reader is None:
            raise CaseLedgerPersistenceBlocked("official source capture completion requires encrypted-object verifier")
        try:
            plaintext = self._artifact_reader(object_key, expected_hash)
        except Exception as error:
            raise CaseLedgerPersistenceBlocked("official source encrypted object authentication failed") from error
        if not isinstance(plaintext, bytes) or sha256(plaintext).hexdigest() != expected_hash:
            raise CaseLedgerPersistenceBlocked("official source encrypted object hash verification failed")
        return plaintext

    @staticmethod
    def _load_run(
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            f"""
            SELECT * FROM official_source_capture_runs
            WHERE run_id = %s AND matter_id = %s AND firm_id = %s{suffix}
            """,
            (run_id, matter_id, actor.firm_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    @staticmethod
    def _validate_command(
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        roles: frozenset[Role],
    ) -> None:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, roles)
        _require_positive_version(expected_version)

    @staticmethod
    def _begin(
        connection: psycopg.Connection,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        command_name: str,
        payload_hash: str,
        allowed_roles: frozenset[Role],
    ) -> CaseLedgerCommandReceipt | None:
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
            payload_hash=payload_hash,
        )
        if prior is not None:
            return prior
        _authorize_and_lock_matter(
            connection,
            actor=actor,
            matter_id=matter_id,
            expected_version=expected_version,
            allowed_roles=allowed_roles,
        )
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


def _source(source_id: str):
    try:
        return _SOURCES[source_id]
    except KeyError as error:
        raise CaseLedgerPersistenceBlocked("unknown registered official source") from error


def _validate_source_url(url: str, *, allowed_domains: frozenset[str]) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_domains
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise CaseLedgerPersistenceBlocked("official source target must be an allowlisted canonical HTTPS URL")


def _validate_parsed_summary(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or not value:
        raise CaseLedgerPersistenceBlocked("official source parsed summary must be a non-empty object")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PARSED_SUMMARY_BYTES:
        raise CaseLedgerPersistenceBlocked("official source parsed summary exceeds the byte limit")
    forbidden = {"normalized_text", "raw_text", "response_body", "plaintext"}
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if forbidden.intersection(item):
                raise CaseLedgerPersistenceBlocked("parsed summary must not duplicate official source text")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and len(item) > 1000:
            raise CaseLedgerPersistenceBlocked("parsed summary contains an oversized text field")


def _validate_active_lease(connection: psycopg.Connection, *, run: dict[str, Any], lease_id: str) -> None:
    if run["status"] != "RUNNING" or str(run["lease_id"]) != lease_id:
        raise CaseLedgerPersistenceBlocked("official source capture lease is missing or replaced")
    active = connection.execute("SELECT %s > now() AS active", (run["lease_expires_at"],)).fetchone()["active"]
    if active is not True:
        raise CaseLedgerPersistenceBlocked("official source capture lease expired")


def _lease(row: dict[str, Any], *, matter_version: int) -> OfficialSourceCaptureRunLease:
    return OfficialSourceCaptureRunLease(
        run_id=str(row["run_id"]),
        matter_id=str(row["matter_id"]),
        matter_version=matter_version,
        lease_id=str(row["lease_id"]),
        lease_expires_at=row["lease_expires_at"],
        source_id=row["source_id"],
        publisher=row["publisher"],
        source_tier=row["source_tier"],
        target_url=row["target_url"],
        query_sha256=row["query_sha256"],
        authorization_hash=row["authorization_hash"],
        authorized_by=str(row["authorized_by"]),
        authorized_at=row["authorized_at"],
        max_response_bytes=row["max_response_bytes"],
    )


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key.endswith("_id") and value is not None:
            result[key] = str(value)
        else:
            result[key] = value
    return result
