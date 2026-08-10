"""Recoverable PostgreSQL queue for lawyer-approved local-folder evidence intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _read_projection_version,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .evidence_manifest_postgres import PostgresEvidenceManifestStore
from .models import Actor, Role


@dataclass(frozen=True)
class EvidenceIntakeItemLease:
    run_id: str
    item_id: str
    lease_id: str
    matter_id: str
    scan_id: str
    scan_manifest_hash: str
    relative_path: str
    expected_byte_size: int
    expected_sha256: str
    detected_kind: str
    attempt_count: int
    lease_expires_at: datetime
    matter_version: int


@dataclass(frozen=True)
class PersistentEvidenceIntakeSummary:
    matter_id: str
    matter_version: int
    run: dict[str, Any] | None


class PostgresEvidenceIntakeStore(PostgresEvidenceManifestStore):
    """Evidence Store plus the approved-folder material intake queue."""

    def enqueue_evidence_intake_run(
        self,
        *,
        matter_id: str,
        scan_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        scan_manifest_hash: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("scan_id", scan_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("scan_manifest_hash", scan_manifest_hash)
        _validate_sha256("intake approval_hash", approval_hash)
        command_name = "ENQUEUE_EVIDENCE_INTAKE_RUN"
        payload = {
            "matter_id": matter_id,
            "scan_id": scan_id,
            "expected_version": expected_version,
            "scan_manifest_hash": scan_manifest_hash,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            scan = connection.execute(
                """
                SELECT scan_id, manifest_hash, status
                FROM local_folder_scans
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchone()
            if scan is None:
                raise KeyError(scan_id)
            if scan["status"] != "APPROVED" or scan["manifest_hash"] != scan_manifest_hash:
                raise CaseLedgerPersistenceBlocked("material intake requires the current approved folder scan")
            existing = connection.execute(
                """
                SELECT status FROM evidence_intake_runs
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s AND status <> 'STALE'
                LIMIT 1
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchone()
            if existing is not None:
                raise CaseLedgerPersistenceBlocked("the approved folder scan already has a material intake run")
            files = connection.execute(
                """
                SELECT relative_path, byte_size, file_sha256, detected_kind
                FROM local_folder_scan_files
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s AND present = true
                ORDER BY sort_sequence ASC
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchall()
            if not files:
                raise CaseLedgerPersistenceBlocked("the approved folder scan has no present files to receive")
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_intake_runs (
                    run_id, firm_id, matter_id, scan_id, scan_manifest_hash,
                    status, created_by, approval_hash
                ) VALUES (%s, %s, %s, %s, %s, 'QUEUED', %s, %s)
                """,
                (run_id, actor.firm_id, matter_id, scan_id, scan_manifest_hash, actor.actor_id, approval_hash),
            )
            for file in files:
                item_id = str(uuid5(NAMESPACE_URL, f"lawcase:intake:{actor.firm_id}:{matter_id}:{run_id}:{file['relative_path']}"))
                connection.execute(
                    """
                    INSERT INTO evidence_intake_items (
                        item_id, run_id, firm_id, matter_id, scan_id, relative_path,
                        expected_byte_size, expected_sha256, detected_kind, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED')
                    """,
                    (
                        item_id,
                        run_id,
                        actor.firm_id,
                        matter_id,
                        scan_id,
                        file["relative_path"],
                        file["byte_size"],
                        file["file_sha256"],
                        file["detected_kind"],
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
                event_type="EVIDENCE_INTAKE_RUN_QUEUED",
                object_type="EVIDENCE_INTAKE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "scan_id": scan_id,
                    "scan_manifest_hash": scan_manifest_hash,
                    "total_items": len(files),
                    "approval_hash": approval_hash,
                },
                stale_submission=False,
            )

    def claim_evidence_intake_item(
        self,
        *,
        matter_id: str,
        run_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> EvidenceIntakeItemLease:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("run_id", run_id)
        worker_roles = frozenset({Role.SYSTEM_WORKER})
        _require_roles(actor, worker_roles)
        _require_positive_version(expected_version)
        if lease_seconds < 30 or lease_seconds > 300:
            raise CaseLedgerPersistenceBlocked("evidence intake lease must be between 30 and 300 seconds")
        command_name = "CLAIM_EVIDENCE_INTAKE_ITEM"
        lease_id = str(uuid5(NAMESPACE_URL, f"lawcase:intake-lease:{actor.firm_id}:{matter_id}:{run_id}:{idempotency_key}"))
        payload = {
            "matter_id": matter_id,
            "run_id": run_id,
            "expected_version": expected_version,
            "lease_id": lease_id,
            "lease_seconds": lease_seconds,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=worker_roles,
            )
            if prior is not None:
                item = connection.execute(
                    """
                    SELECT item_id, scan_id, relative_path, expected_byte_size, expected_sha256,
                           detected_kind, attempt_count, lease_expires_at
                    FROM evidence_intake_items
                    WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                      AND status = 'RUNNING' AND lease_id = %s
                    """,
                    (run_id, matter_id, actor.firm_id, lease_id),
                ).fetchone()
                run = connection.execute(
                    "SELECT scan_manifest_hash FROM evidence_intake_runs WHERE run_id = %s AND matter_id = %s AND firm_id = %s",
                    (run_id, matter_id, actor.firm_id),
                ).fetchone()
                if item is None or run is None:
                    raise CaseLedgerPersistenceBlocked("the replayed intake claim has already progressed")
                return _lease_payload(item, run_id=run_id, lease_id=lease_id, matter_id=matter_id, scan_manifest_hash=run["scan_manifest_hash"], matter_version=prior.matter_version)
            run = connection.execute(
                """
                SELECT run.status, run.scan_id, run.scan_manifest_hash,
                       scan.status AS scan_status, scan.manifest_hash AS current_manifest_hash
                FROM evidence_intake_runs run
                JOIN local_folder_scans scan
                  ON scan.scan_id = run.scan_id AND scan.matter_id = run.matter_id AND scan.firm_id = run.firm_id
                WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
                FOR UPDATE OF run
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in {"QUEUED", "RUNNING"} or run["scan_status"] != "APPROVED" or run["current_manifest_hash"] != run["scan_manifest_hash"]:
                raise CaseLedgerPersistenceBlocked("evidence intake run is no longer current")
            item = connection.execute(
                """
                SELECT item_id, scan_id, relative_path, expected_byte_size, expected_sha256,
                       detected_kind, attempt_count, lease_expires_at, status
                FROM evidence_intake_items
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                  AND attempt_count < 3
                  AND (status = 'QUEUED' OR (status = 'RUNNING' AND lease_expires_at <= now()))
                ORDER BY created_at ASC, item_id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            if item is None:
                raise CaseLedgerPersistenceBlocked("evidence intake run has no claimable item")
            claimed = connection.execute(
                """
                UPDATE evidence_intake_items
                SET status = 'RUNNING', attempt_count = attempt_count + 1,
                    lease_id = %s, lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE item_id = %s AND run_id = %s AND matter_id = %s AND firm_id = %s
                RETURNING item_id, scan_id, relative_path, expected_byte_size, expected_sha256,
                          detected_kind, attempt_count, lease_expires_at
                """,
                (lease_id, lease_seconds, item["item_id"], run_id, matter_id, actor.firm_id),
            ).fetchone()
            if claimed is None:
                raise CaseLedgerPersistenceBlocked("evidence intake item changed before claim")
            connection.execute(
                "UPDATE evidence_intake_runs SET status = 'RUNNING', updated_at = now() WHERE run_id = %s AND matter_id = %s AND firm_id = %s AND status = 'QUEUED'",
                (run_id, matter_id, actor.firm_id),
            )
            receipt = _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_INTAKE_ITEM_CLAIMED",
                object_type="EVIDENCE_INTAKE_ITEM",
                object_id=str(claimed["item_id"]),
                audit_payload={
                    "run_id": run_id,
                    "item_id": str(claimed["item_id"]),
                    "scan_id": str(claimed["scan_id"]),
                    "expected_sha256": claimed["expected_sha256"],
                    "attempt_count": claimed["attempt_count"],
                    "lease_id": lease_id,
                },
                stale_submission=False,
            )
            return _lease_payload(claimed, run_id=run_id, lease_id=lease_id, matter_id=matter_id, scan_manifest_hash=run["scan_manifest_hash"], matter_version=receipt.matter_version)

    def renew_evidence_intake_item_lease(
        self,
        *,
        matter_id: str,
        run_id: str,
        item_id: str,
        lease_id: str,
        actor: Actor,
        lease_seconds: int = 120,
    ) -> datetime:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        for label, value in (("run_id", run_id), ("item_id", item_id), ("lease_id", lease_id)):
            _validate_uuid(label, value)
        worker_roles = frozenset({Role.SYSTEM_WORKER})
        _require_roles(actor, worker_roles)
        if lease_seconds < 30 or lease_seconds > 300:
            raise CaseLedgerPersistenceBlocked("evidence intake lease must be between 30 and 300 seconds")
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=worker_roles)
            renewed = connection.execute(
                """
                UPDATE evidence_intake_items
                SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE item_id = %s AND run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND lease_id = %s AND lease_expires_at > now()
                RETURNING lease_expires_at
                """,
                (lease_seconds, item_id, run_id, matter_id, actor.firm_id, lease_id),
            ).fetchone()
            if renewed is None:
                raise CaseLedgerPersistenceBlocked("evidence intake lease is missing, expired, or replaced")
            return renewed["lease_expires_at"]

    def complete_evidence_intake_item(
        self,
        *,
        matter_id: str,
        run_id: str,
        item_id: str,
        lease_id: str,
        evidence_file_id: str,
        inspection_hash: str,
        scanner_name: str,
        scanner_definitions_version: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        return self._finalize_evidence_intake_item(
            matter_id=matter_id,
            run_id=run_id,
            item_id=item_id,
            lease_id=lease_id,
            status="REGISTERED",
            evidence_file_id=evidence_file_id,
            inspection_hash=inspection_hash,
            scanner_name=scanner_name,
            scanner_definitions_version=scanner_definitions_version,
            outcome_code=None,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def finalize_evidence_intake_item(
        self,
        *,
        matter_id: str,
        run_id: str,
        item_id: str,
        lease_id: str,
        outcome: str,
        outcome_code: str,
        inspection_hash: str | None,
        scanner_name: str | None,
        scanner_definitions_version: str | None,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        if outcome not in {"REVIEW_REQUIRED", "BLOCKED", "FAILED"}:
            raise CaseLedgerPersistenceBlocked("evidence intake terminal outcome is invalid")
        return self._finalize_evidence_intake_item(
            matter_id=matter_id,
            run_id=run_id,
            item_id=item_id,
            lease_id=lease_id,
            status=outcome,
            evidence_file_id=None,
            inspection_hash=inspection_hash,
            scanner_name=scanner_name,
            scanner_definitions_version=scanner_definitions_version,
            outcome_code=outcome_code,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def _finalize_evidence_intake_item(
        self,
        *,
        matter_id: str,
        run_id: str,
        item_id: str,
        lease_id: str,
        status: str,
        evidence_file_id: str | None,
        inspection_hash: str | None,
        scanner_name: str | None,
        scanner_definitions_version: str | None,
        outcome_code: str | None,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        for label, value in (("run_id", run_id), ("item_id", item_id), ("lease_id", lease_id)):
            _validate_uuid(label, value)
        worker_roles = frozenset({Role.SYSTEM_WORKER})
        _require_roles(actor, worker_roles)
        _require_positive_version(expected_version)
        if evidence_file_id is not None:
            _validate_uuid("evidence_file_id", evidence_file_id)
        if inspection_hash is not None:
            _validate_sha256("inspection_hash", inspection_hash)
        if status in {"REGISTERED", "REVIEW_REQUIRED", "BLOCKED"}:
            if inspection_hash is None or scanner_name is None or scanner_definitions_version is None:
                raise CaseLedgerPersistenceBlocked("inspected intake outcomes require scanner provenance and inspection hash")
            _require_text(scanner_name, "scanner_name")
            _require_text(scanner_definitions_version, "scanner_definitions_version")
        if outcome_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", outcome_code):
            raise CaseLedgerPersistenceBlocked("evidence intake outcome_code is invalid")
        command_name = f"FINALIZE_EVIDENCE_INTAKE_ITEM_{status}"
        payload = {
            "matter_id": matter_id,
            "run_id": run_id,
            "item_id": item_id,
            "lease_id": lease_id,
            "status": status,
            "evidence_file_id": evidence_file_id,
            "inspection_hash": inspection_hash,
            "scanner_name": scanner_name,
            "scanner_definitions_version": scanner_definitions_version,
            "outcome_code": outcome_code,
            "expected_version": expected_version,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=worker_roles,
            )
            if prior is not None:
                return prior
            item = connection.execute(
                """
                SELECT item.status, item.lease_id, item.lease_expires_at, item.relative_path,
                       item.expected_byte_size, item.expected_sha256, run.scan_manifest_hash,
                       scan.status AS scan_status, scan.manifest_hash AS current_manifest_hash
                FROM evidence_intake_items item
                JOIN evidence_intake_runs run
                  ON run.run_id = item.run_id AND run.matter_id = item.matter_id AND run.firm_id = item.firm_id
                JOIN local_folder_scans scan
                  ON scan.scan_id = run.scan_id AND scan.matter_id = run.matter_id AND scan.firm_id = run.firm_id
                WHERE item.item_id = %s AND item.run_id = %s AND item.matter_id = %s AND item.firm_id = %s
                FOR UPDATE OF item, run
                """,
                (item_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if item is None:
                raise KeyError(item_id)
            active = connection.execute("SELECT %s > now() AS active", (item["lease_expires_at"],)).fetchone()["active"] if item["lease_expires_at"] is not None else False
            if item["status"] != "RUNNING" or str(item["lease_id"]) != lease_id or active is not True:
                raise CaseLedgerPersistenceBlocked("evidence intake lease is missing, expired, or replaced")
            if item["scan_status"] != "APPROVED" or item["current_manifest_hash"] != item["scan_manifest_hash"]:
                raise CaseLedgerPersistenceBlocked("evidence intake source scan is no longer current")
            if status == "REGISTERED":
                original = connection.execute(
                    """
                    SELECT evidence_file_id FROM evidence_original_files
                    WHERE evidence_file_id = %s AND matter_id = %s AND firm_id = %s
                      AND original_label = %s AND original_file_sha256 = %s AND byte_size = %s
                    """,
                    (evidence_file_id, matter_id, actor.firm_id, item["relative_path"], item["expected_sha256"], item["expected_byte_size"]),
                ).fetchone()
                if original is None:
                    raise CaseLedgerPersistenceBlocked("registered evidence original does not match the claimed intake item")
            updated = connection.execute(
                """
                UPDATE evidence_intake_items
                SET status = %s, lease_id = NULL, lease_expires_at = NULL,
                    evidence_file_id = %s, inspection_hash = %s, scanner_name = %s,
                    scanner_definitions_version = %s, outcome_code = %s,
                    completed_at = now(), updated_at = now()
                WHERE item_id = %s AND run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND lease_id = %s
                """,
                (
                    status,
                    evidence_file_id,
                    inspection_hash,
                    scanner_name,
                    scanner_definitions_version,
                    outcome_code,
                    item_id,
                    run_id,
                    matter_id,
                    actor.firm_id,
                    lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("evidence intake item changed before completion")
            final_run_status = _refresh_run_status(connection, run_id=run_id, matter_id=matter_id, firm_id=actor.firm_id)
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type=f"EVIDENCE_INTAKE_ITEM_{status}",
                object_type="EVIDENCE_INTAKE_ITEM",
                object_id=item_id,
                audit_payload={
                    "run_id": run_id,
                    "item_id": item_id,
                    "status": status,
                    "evidence_file_id": evidence_file_id,
                    "inspection_hash": inspection_hash,
                    "outcome_code": outcome_code,
                    "run_status": final_run_status,
                },
                stale_submission=False,
            )

    def get_current_evidence_intake_summary(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentEvidenceIntakeSummary:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES)
            matter_version = _read_projection_version(connection, matter_id=matter_id, firm_id=actor.firm_id)
            row = connection.execute(
                """
                SELECT run.run_id, run.scan_id, run.scan_manifest_hash, run.status,
                       run.created_at, run.completed_at,
                       COUNT(item.item_id) AS total_items,
                       COUNT(*) FILTER (WHERE item.status = 'QUEUED') AS queued_items,
                       COUNT(*) FILTER (WHERE item.status = 'RUNNING') AS running_items,
                       COUNT(*) FILTER (WHERE item.status = 'REGISTERED') AS registered_items,
                       COUNT(*) FILTER (WHERE item.status = 'REVIEW_REQUIRED') AS review_required_items,
                       COUNT(*) FILTER (WHERE item.status = 'BLOCKED') AS blocked_items,
                       COUNT(*) FILTER (WHERE item.status = 'FAILED') AS failed_items
                FROM evidence_intake_runs run
                JOIN local_folder_scans scan
                  ON scan.scan_id = run.scan_id AND scan.matter_id = run.matter_id AND scan.firm_id = run.firm_id
                LEFT JOIN evidence_intake_items item
                  ON item.run_id = run.run_id AND item.matter_id = run.matter_id AND item.firm_id = run.firm_id
                WHERE run.matter_id = %s AND run.firm_id = %s
                  AND scan.status = 'APPROVED' AND run.status <> 'STALE'
                GROUP BY run.run_id
                ORDER BY run.created_at DESC, run.run_id DESC
                LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
        return PersistentEvidenceIntakeSummary(
            matter_id=matter_id,
            matter_version=matter_version,
            run=_run_summary_payload(row),
        )

    def reap_exhausted_evidence_intake_items(
        self,
        *,
        matter_id: str,
        run_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("run_id", run_id)
        worker_roles = frozenset({Role.SYSTEM_WORKER})
        _require_roles(actor, worker_roles)
        _require_positive_version(expected_version)
        command_name = "REAP_EXHAUSTED_EVIDENCE_INTAKE_ITEMS"
        payload = {"matter_id": matter_id, "run_id": run_id, "expected_version": expected_version}
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=worker_roles,
            )
            if prior is not None:
                return prior
            run = connection.execute(
                """
                SELECT run.scan_manifest_hash, scan.status AS scan_status,
                       scan.manifest_hash AS current_manifest_hash
                FROM evidence_intake_runs run
                JOIN local_folder_scans scan
                  ON scan.scan_id = run.scan_id AND scan.matter_id = run.matter_id AND scan.firm_id = run.firm_id
                WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
                FOR UPDATE OF run
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["scan_status"] != "APPROVED" or run["current_manifest_hash"] != run["scan_manifest_hash"]:
                raise CaseLedgerPersistenceBlocked("evidence intake source scan is no longer current")
            exhausted = connection.execute(
                """
                UPDATE evidence_intake_items
                SET status = 'FAILED', lease_id = NULL, lease_expires_at = NULL,
                    outcome_code = 'RECOVERY_ATTEMPTS_EXHAUSTED',
                    completed_at = now(), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND attempt_count >= 3 AND lease_expires_at <= now()
                RETURNING item_id
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchall()
            if not exhausted:
                raise CaseLedgerPersistenceBlocked("evidence intake run has no exhausted expired items")
            final_run_status = _refresh_run_status(connection, run_id=run_id, matter_id=matter_id, firm_id=actor.firm_id)
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_INTAKE_ITEMS_RECOVERY_EXHAUSTED",
                object_type="EVIDENCE_INTAKE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "failed_item_count": len(exhausted),
                    "failed_item_ids": [str(row["item_id"]) for row in exhausted],
                    "run_status": final_run_status,
                },
                stale_submission=False,
            )


def _lease_payload(
    row: dict[str, Any],
    *,
    run_id: str,
    lease_id: str,
    matter_id: str,
    scan_manifest_hash: str,
    matter_version: int,
) -> EvidenceIntakeItemLease:
    return EvidenceIntakeItemLease(
        run_id=run_id,
        item_id=str(row["item_id"]),
        lease_id=lease_id,
        matter_id=matter_id,
        scan_id=str(row["scan_id"]),
        scan_manifest_hash=scan_manifest_hash,
        relative_path=row["relative_path"],
        expected_byte_size=row["expected_byte_size"],
        expected_sha256=row["expected_sha256"],
        detected_kind=row["detected_kind"],
        attempt_count=row["attempt_count"],
        lease_expires_at=row["lease_expires_at"],
        matter_version=matter_version,
    )


def _refresh_run_status(connection: Any, *, run_id: str, matter_id: str, firm_id: str) -> str:
    counts = connection.execute(
        """
        SELECT COUNT(*) FILTER (WHERE status IN ('QUEUED', 'RUNNING')) AS active_count,
               COUNT(*) FILTER (WHERE status = 'REGISTERED') AS registered_count,
               COUNT(*) AS total_count
        FROM evidence_intake_items
        WHERE run_id = %s AND matter_id = %s AND firm_id = %s
        """,
        (run_id, matter_id, firm_id),
    ).fetchone()
    if counts["active_count"] > 0:
        status = "RUNNING"
        connection.execute(
            "UPDATE evidence_intake_runs SET status = %s, completed_at = NULL, updated_at = now() WHERE run_id = %s AND matter_id = %s AND firm_id = %s AND status <> 'STALE'",
            (status, run_id, matter_id, firm_id),
        )
    else:
        status = "SUCCEEDED" if counts["registered_count"] == counts["total_count"] else "PARTIAL"
        connection.execute(
            "UPDATE evidence_intake_runs SET status = %s, completed_at = now(), updated_at = now() WHERE run_id = %s AND matter_id = %s AND firm_id = %s AND status <> 'STALE'",
            (status, run_id, matter_id, firm_id),
        )
    return status


def _run_summary_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": str(row["run_id"]),
        "scan_id": str(row["scan_id"]),
        "scan_manifest_hash": row["scan_manifest_hash"],
        "status": row["status"],
        "total_items": row["total_items"],
        "queued_items": row["queued_items"],
        "running_items": row["running_items"],
        "registered_items": row["registered_items"],
        "review_required_items": row["review_required_items"],
        "blocked_items": row["blocked_items"],
        "failed_items": row["failed_items"],
        "created_at": row["created_at"].isoformat(),
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }
