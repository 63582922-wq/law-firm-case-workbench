from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.evidence_manifest import PageDisposition
from case_kernel.evidence_manifest_postgres import PostgresEvidenceManifestStore
from case_kernel.models import Actor, Role


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None
    rowcount: int = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeEvidenceConnection:
    def __init__(
        self,
        *,
        permitted: bool = True,
        prior_receipt: dict | None = None,
        page_decision: dict | None = None,
        lock_pages: list[dict] | None = None,
        duplicate_groups: list[dict] | None = None,
        duplicate_members: list[dict] | None = None,
        annotations: list[dict] | None = None,
        manifest_row: dict | None = None,
        derivative_row: dict | None = None,
        verified_locator_row: dict | None = None,
        derivative_run_row: dict | None = None,
        derivative_run_existing: dict | None = None,
        derivative_run_claimed: dict | None = None,
        derivative_run_artifacts: list[dict] | None = None,
        clock_comparison: bool = True,
        heartbeat_expires_at: datetime | None = None,
    ) -> None:
        self.permitted = permitted
        self.prior_receipt = prior_receipt
        self.page_decision = page_decision
        self.lock_pages = lock_pages or []
        self.duplicate_groups = duplicate_groups or []
        self.duplicate_members = duplicate_members or []
        self.annotations = annotations or []
        self.manifest_row = manifest_row
        self.derivative_row = derivative_row
        self.verified_locator_row = verified_locator_row
        self.derivative_run_row = derivative_run_row
        self.derivative_run_existing = derivative_run_existing
        self.derivative_run_claimed = derivative_run_claimed
        self.derivative_run_artifacts = derivative_run_artifacts or []
        self.clock_comparison = clock_comparison
        self.heartbeat_expires_at = heartbeat_expires_at
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            if self.prior_receipt is None:
                return FakeResult(row=None)
            return FakeResult(
                row={"request_hash": self.prior_receipt["request_hash"], "response_json": self.prior_receipt["response_json"]}
            )
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": self.permitted})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"permitted": 1} if self.permitted else None)
        if normalized.startswith("SELECT 1 FROM evidence_original_files"):
            return FakeResult(row={"exists": 1})
        if normalized.startswith("SELECT 1 FROM evidence_pages"):
            return FakeResult(row={"exists": 1})
        if "SELECT evidence_page_id, disposition, status FROM evidence_page_decisions" in normalized:
            return FakeResult(
                row=self.page_decision
                or {"evidence_page_id": str(uuid4()), "disposition": "INCLUDE", "status": "CANDIDATE"}
            )
        if "SELECT manifest_id FROM evidence_manifests" in normalized:
            return FakeResult(row=None)
        if "SELECT page.evidence_page_id, page.evidence_file_id" in normalized:
            return FakeResult(rows=self.lock_pages)
        if "SELECT duplicate_group_id, status, canonical_page_id" in normalized:
            return FakeResult(rows=self.duplicate_groups)
        if "SELECT duplicate_group_id, evidence_page_id FROM evidence_page_duplicate_members" in normalized:
            return FakeResult(rows=self.duplicate_members)
        if "SELECT annotation_id, evidence_page_id, purpose" in normalized:
            return FakeResult(rows=self.annotations)
        if "SELECT content_hash, included_pages, status FROM evidence_manifests" in normalized:
            return FakeResult(row=self.manifest_row)
        if "SELECT status, content_hash FROM evidence_manifests" in normalized:
            return FakeResult(row=self.manifest_row)
        if "SELECT status FROM evidence_derivative_runs" in normalized:
            return FakeResult(row=self.derivative_run_existing)
        if "SELECT run.status, run.attempt_count" in normalized or "SELECT run.status, run.lease_id" in normalized:
            return FakeResult(row=self.derivative_run_row)
        if normalized.startswith("SELECT %s <= now()") or normalized.startswith("SELECT %s > now()"):
            key = "expired" if "expired" in normalized else "active"
            return FakeResult(row={key: self.clock_comparison})
        if "UPDATE evidence_derivative_runs SET status = 'RUNNING'" in normalized:
            return FakeResult(row=self.derivative_run_claimed)
        if "SET lease_expires_at = now()" in normalized:
            return FakeResult(
                row={"lease_expires_at": self.heartbeat_expires_at}
                if self.heartbeat_expires_at is not None
                else None
            )
        if "SELECT derivative_id, artifact_type, status, manifest_id" in normalized:
            return FakeResult(rows=self.derivative_run_artifacts)
        if "SELECT derivative.status, derivative.manifest_id" in normalized:
            return FakeResult(row=self.derivative_row)
        if "SELECT derivative.derivative_id, derivative.manifest_id" in normalized:
            return FakeResult(row=self.verified_locator_row)
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeEvidenceConnection) -> None:
        self.connection = connection

    def __enter__(self) -> FakeEvidenceConnection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class PostgresEvidenceManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresEvidenceManifestStore("postgresql://not-used.invalid/lawcase_workbench_test")

    def test_alpha_identity_is_rejected_before_database_connection(self) -> None:
        actor = Actor("alpha-lead", "alpha-firm", frozenset({Role.LEAD_LAWYER}))
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "requires UUID"):
                self.store.register_original_file(
                    matter_id="alpha-matter",
                    actor=actor,
                    expected_version=1,
                    idempotency_key="evidence-original-001",
                    original_label="[合成] 微信账单.pdf",
                    original_file_sha256="a" * 64,
                    byte_size=4096,
                    media_type="application/pdf",
                    page_count=2,
                    source_scan_fingerprint="b" * 64,
                )
        connect.assert_not_called()

    def test_original_registration_is_append_only_versioned_audited_and_stales_outputs(self) -> None:
        connection = FakeEvidenceConnection()
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.register_original_file(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-original-001",
                original_label="[合成] 微信账单.pdf",
                original_file_sha256="a" * 64,
                byte_size=4096,
                media_type="application/pdf",
                page_count=2,
                source_scan_fingerprint="b" * 64,
            )

        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO evidence_original_files", sql)
        self.assertEqual(sql.count("INSERT INTO evidence_pages"), 2)
        self.assertIn("UPDATE evidence_derivative_artifacts", sql)
        self.assertIn("UPDATE evidence_manifests", sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", sql)
        self.assertIn("INSERT INTO audit_events", sql)
        self.assertIn("INSERT INTO outbox_events", sql)
        self.assertIn("INSERT INTO command_idempotency", sql)
        self.assertNotIn("DELETE FROM evidence_", sql)

    def test_idempotent_replay_is_resolved_before_stale_expected_version_check(self) -> None:
        original_payload = {
            "matter_id": self.matter_id,
            "expected_version": 1,
            "original_label": "[合成] 微信账单.pdf",
            "original_file_sha256": "a" * 64,
            "byte_size": 4096,
            "media_type": "application/pdf",
            "page_count": 2,
            "source_scan_fingerprint": "b" * 64,
            "supersedes_file_id": None,
        }
        from case_kernel.case_ledger_postgres import _payload_hash

        response = {
            "command_name": "REGISTER_EVIDENCE_ORIGINAL",
            "idempotency_key": "evidence-original-replay",
            "matter_id": self.matter_id,
            "matter_version": 2,
            "audit_event_id": str(uuid4()),
            "object_type": "EVIDENCE_ORIGINAL",
            "object_id": str(uuid4()),
        }
        connection = FakeEvidenceConnection(
            prior_receipt={"request_hash": _payload_hash(original_payload), "response_json": response}
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.register_original_file(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-original-replay",
                original_label="[合成] 微信账单.pdf",
                original_file_sha256="a" * 64,
                byte_size=4096,
                media_type="application/pdf",
                page_count=2,
                source_scan_fingerprint="b" * 64,
            )
        self.assertEqual(receipt.object_id, response["object_id"])
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("SELECT m.version", sql)
        self.assertNotIn("INSERT INTO evidence_original_files", sql)

    def test_approved_page_decision_invalidates_manifest_derivative_and_submission(self) -> None:
        decision_id = str(uuid4())
        page_id = str(uuid4())
        connection = FakeEvidenceConnection(
            page_decision={"evidence_page_id": page_id, "disposition": "INCLUDE", "status": "CANDIDATE"}
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.approve_page_decision(
                matter_id=self.matter_id,
                decision_id=decision_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-page-decision-approve",
                approval_hash="c" * 64,
            )
        self.assertEqual(receipt.object_id, decision_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("SET status = 'INVALIDATED', approval_hash = NULL", sql)
        self.assertIn("SET status = 'APPROVED', approval_hash", sql)
        self.assertIn("UPDATE evidence_derivative_artifacts", sql)
        self.assertIn("UPDATE evidence_manifests", sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", sql)

    def test_manifest_lock_blocks_any_source_page_without_approved_decision(self) -> None:
        page_id = str(uuid4())
        file_id = str(uuid4())
        connection = FakeEvidenceConnection(
            lock_pages=[
                {
                    "evidence_page_id": page_id,
                    "evidence_file_id": file_id,
                    "page_number": 1,
                    "original_label": "[合成] 微信账单.pdf",
                    "original_file_sha256": "a" * 64,
                    "decision_id": None,
                    "disposition": None,
                }
            ]
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "unresolved pages: 1"):
                self.store.lock_manifest(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="evidence-manifest-lock-missing",
                    approval_hash="d" * 64,
                )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO evidence_manifests", sql)

    def test_manifest_lock_materializes_page_order_and_approved_red_box_lineage(self) -> None:
        file_id = str(uuid4())
        first_page_id = str(uuid4())
        second_page_id = str(uuid4())
        first_decision_id = str(uuid4())
        second_decision_id = str(uuid4())
        annotation_id = str(uuid4())
        connection = FakeEvidenceConnection(
            lock_pages=[
                {
                    "evidence_page_id": first_page_id,
                    "evidence_file_id": file_id,
                    "page_number": 1,
                    "original_label": "[合成] 微信账单.pdf",
                    "original_file_sha256": "a" * 64,
                    "decision_id": first_decision_id,
                    "disposition": "INCLUDE",
                },
                {
                    "evidence_page_id": second_page_id,
                    "evidence_file_id": file_id,
                    "page_number": 2,
                    "original_label": "[合成] 微信账单.pdf",
                    "original_file_sha256": "a" * 64,
                    "decision_id": second_decision_id,
                    "disposition": "EXCLUDE",
                },
            ],
            annotations=[
                {
                    "annotation_id": annotation_id,
                    "evidence_page_id": first_page_id,
                    "purpose": "HIGHLIGHT_RELEVANT_REGION",
                    "x0": Decimal("0.1"),
                    "y0": Decimal("0.2"),
                    "x1": Decimal("0.8"),
                    "y1": Decimal("0.4"),
                    "label": "[合成] 与原告相关交易行",
                    "approval_hash": "e" * 64,
                }
            ],
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.lock_manifest(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-manifest-lock",
                approval_hash="f" * 64,
            )
        UUID(receipt.object_id)
        manifest_params = next(
            params for sql, params in connection.executed if "INSERT INTO evidence_manifests" in sql
        )
        self.assertEqual(manifest_params[5:8], (2, 1, 1))
        page_params = [
            params for sql, params in connection.executed if "INSERT INTO evidence_manifest_pages" in sql
        ]
        self.assertEqual([params[-1] for params in page_params], [1, None])
        annotation_params = next(
            params
            for sql, params in connection.executed
            if "INSERT INTO evidence_manifest_page_annotations" in sql
        )
        self.assertEqual(annotation_params[1:3], (first_page_id, annotation_id))

    def test_system_worker_registers_only_hash_bound_current_manifest_derivative(self) -> None:
        manifest_id = str(uuid4())
        artifact_hash = "9" * 64
        object_key = f"{artifact_hash[:2]}/{artifact_hash[2:4]}/{artifact_hash}.lca"
        system_actor = Actor(self.actor_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        connection = FakeEvidenceConnection(
            manifest_row={"content_hash": "8" * 64, "included_pages": 2, "status": "LOCKED"}
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.register_derivative_candidate(
                matter_id=self.matter_id,
                manifest_id=manifest_id,
                actor=system_actor,
                expected_version=1,
                idempotency_key="evidence-derivative-register",
                manifest_content_hash="8" * 64,
                artifact_type="ANNOTATED_RELATED_PAGES_PDF",
                storage_object_key=object_key,
                artifact_sha256=artifact_hash,
                page_count=2,
            )
        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO evidence_derivative_artifacts", sql)
        self.assertIn("EVIDENCE_DERIVATIVE_CANDIDATE_REGISTERED", str(connection.executed))
        self.assertNotIn("UPDATE submission_bundles SET validity = 'STALE'", sql)

        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "must match the artifact SHA-256"):
            self.store.register_derivative_candidate(
                matter_id=self.matter_id,
                manifest_id=manifest_id,
                actor=system_actor,
                expected_version=1,
                idempotency_key="evidence-derivative-wrong-key",
                manifest_content_hash="8" * 64,
                artifact_type="RELATED_PAGES_PDF",
                storage_object_key=f"aa/bb/{artifact_hash}.lca",
                artifact_sha256=artifact_hash,
                page_count=2,
            )

    def test_derivative_verification_preserves_manifest_and_artifact_hash_binding(self) -> None:
        derivative_id = str(uuid4())
        manifest_id = str(uuid4())
        connection = FakeEvidenceConnection(
            derivative_row={
                "status": "CANDIDATE",
                "manifest_id": manifest_id,
                "artifact_type": "RELATED_PAGES_PDF",
                "artifact_sha256": "7" * 64,
                "manifest_status": "LOCKED",
            }
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.verify_derivative(
                matter_id=self.matter_id,
                derivative_id=derivative_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-derivative-verify",
                verification_hash="6" * 64,
            )
        self.assertEqual(receipt.object_id, derivative_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("SET status = 'VERIFIED', verification_hash", sql)
        self.assertIn("verified_at = now()", sql)

    def test_verified_derivative_locator_is_server_only_current_and_matter_scoped(self) -> None:
        derivative_id = str(uuid4())
        manifest_id = str(uuid4())
        artifact_hash = "5" * 64
        object_key = f"{artifact_hash[:2]}/{artifact_hash[2:4]}/{artifact_hash}.lca"
        connection = FakeEvidenceConnection(
            verified_locator_row={
                "derivative_id": derivative_id,
                "manifest_id": manifest_id,
                "artifact_type": "RELATED_PAGES_PDF",
                "storage_object_key": object_key,
                "artifact_sha256": artifact_hash,
                "page_count": 2,
                "status": "VERIFIED",
            }
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            locator = self.store.get_verified_derivative_locator(
                matter_id=self.matter_id,
                derivative_id=derivative_id,
                actor=self.actor,
            )
        self.assertEqual(locator.object_key, object_key)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("derivative.status = 'VERIFIED'", sql)
        self.assertIn("manifest.status = 'LOCKED'", sql)
        self.assertNotIn(object_key, repr(locator))

    def test_lead_queues_only_one_hash_bound_manifest_run(self) -> None:
        manifest_id = str(uuid4())
        connection = FakeEvidenceConnection(
            manifest_row={"status": "LOCKED", "content_hash": "4" * 64},
        )
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.enqueue_derivative_run(
                matter_id=self.matter_id,
                manifest_id=manifest_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="evidence-run-enqueue",
                manifest_content_hash="4" * 64,
                approval_hash="3" * 64,
            )
        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO evidence_derivative_runs", sql)
        self.assertIn("EVIDENCE_DERIVATIVE_RUN_QUEUED", str(connection.executed))

    def test_worker_claims_with_a_bounded_recoverable_lease(self) -> None:
        run_id = str(uuid4())
        manifest_id = str(uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        connection = FakeEvidenceConnection(
            derivative_run_row={
                "status": "QUEUED",
                "attempt_count": 0,
                "lease_id": None,
                "lease_expires_at": None,
                "manifest_id": manifest_id,
                "manifest_content_hash": "2" * 64,
                "manifest_status": "LOCKED",
                "current_manifest_hash": "2" * 64,
            },
            derivative_run_claimed={
                "attempt_count": 1,
                "lease_expires_at": expires_at,
                "manifest_id": manifest_id,
                "manifest_content_hash": "2" * 64,
            },
        )
        worker = Actor(self.actor_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            lease = self.store.claim_derivative_run(
                matter_id=self.matter_id,
                run_id=run_id,
                actor=worker,
                expected_version=1,
                idempotency_key="evidence-run-claim",
                lease_seconds=120,
            )
        UUID(lease.lease_id)
        self.assertEqual(lease.attempt_count, 1)
        self.assertEqual(lease.lease_expires_at, expires_at)
        self.assertEqual(lease.matter_version, 2)

    def test_worker_completes_only_with_current_lease_and_both_verified_outputs(self) -> None:
        run_id = str(uuid4())
        lease_id = str(uuid4())
        manifest_id = str(uuid4())
        related_id = str(uuid4())
        annotated_id = str(uuid4())
        connection = FakeEvidenceConnection(
            derivative_run_row={
                "status": "RUNNING",
                "lease_id": lease_id,
                "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
                "manifest_id": manifest_id,
                "manifest_content_hash": "1" * 64,
                "manifest_status": "LOCKED",
                "current_manifest_hash": "1" * 64,
            },
            derivative_run_artifacts=[
                {"derivative_id": related_id, "artifact_type": "RELATED_PAGES_PDF", "status": "VERIFIED", "manifest_id": manifest_id},
                {"derivative_id": annotated_id, "artifact_type": "ANNOTATED_RELATED_PAGES_PDF", "status": "VERIFIED", "manifest_id": manifest_id},
            ],
        )
        worker = Actor(self.actor_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.complete_derivative_run(
                matter_id=self.matter_id,
                run_id=run_id,
                lease_id=lease_id,
                related_derivative_id=related_id,
                annotated_derivative_id=annotated_id,
                actor=worker,
                expected_version=1,
                idempotency_key="evidence-run-complete",
            )
        self.assertEqual(receipt.object_id, run_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("SET status = 'SUCCEEDED'", sql)

    def test_worker_heartbeat_renews_only_the_current_unexpired_lease_without_changing_matter_version(self) -> None:
        run_id = str(uuid4())
        lease_id = str(uuid4())
        renewed_until = datetime.now(timezone.utc) + timedelta(minutes=2)
        connection = FakeEvidenceConnection(heartbeat_expires_at=renewed_until)
        worker = Actor(self.actor_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            result = self.store.renew_derivative_run_lease(
                matter_id=self.matter_id,
                run_id=run_id,
                lease_id=lease_id,
                actor=worker,
                lease_seconds=120,
            )
        self.assertEqual(result, renewed_until)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("lease_expires_at > now()", sql)
        self.assertNotIn("UPDATE matters SET version", sql)


if __name__ == "__main__":
    unittest.main()
