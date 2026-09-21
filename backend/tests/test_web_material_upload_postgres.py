from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4
import unittest

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_material_upload import UploadSlotFailureCode, UploadSlotStatus
from case_api.web_material_upload_postgres import (
    PostgresWebMaterialUploadSlotStore,
    WebMaterialUploadPersistenceBlocked,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_manifest_postgres import RegisteredWebEvidenceOriginal
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_upload_staging import AdmittedWebPdfUpload


@dataclass
class _Result:
    row: dict[str, Any] | None = None

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, error_marker: str | None = None) -> None:
        self.error_marker = error_marker
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.slot: dict[str, Any] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if self.error_marker is not None and self.error_marker in normalized:
            raise RuntimeError("private PostgreSQL fault detail")
        if normalized.startswith("SELECT m.version,"):
            return _Result({"version": 1, "permitted": True})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return _Result({"permitted": 1})
        if normalized.startswith("INSERT INTO web_material_upload_slots"):
            assert params is not None
            self.slot = _base_slot_from_reservation(params)
            return _Result(dict(self.slot))
        if "UPDATE web_material_upload_slots SET status = 'CLAIMED'" in normalized:
            assert params is not None and self.slot is not None
            # PostgreSQL's WHERE status = 'RESERVED' CAS must be reflected in
            # the fake connection too; a terminal reconciliation state must
            # be returned by the follow-up SELECT, never re-claimed.
            if self.slot["status"] != "RESERVED":
                return _Result(None)
            self.slot.update(
                status="CLAIMED",
                attempt_id=params[0],
                attempt_count=1,
                claimed_at=params[1],
                updated_at=params[2],
            )
            return _Result(dict(self.slot))
        if "UPDATE web_material_upload_slots SET status = 'OBJECT_STORED'" in normalized:
            assert params is not None and self.slot is not None
            self.slot.update(
                status="OBJECT_STORED",
                admitted_upload_id=params[0],
                admitted_content_sha256=params[1],
                admitted_byte_size=params[2],
                admitted_page_count=params[3],
                admitted_inspection_hash=params[4],
                scanner_name=params[5],
                scanner_definitions_version=params[6],
                source_object_key=params[7],
                source_object_version_id=params[8],
                source_reference_hash=params[9],
                object_stored_at=params[10],
                updated_at=params[11],
            )
            return _Result(dict(self.slot))
        if "UPDATE web_material_upload_slots SET status = 'COMPLETED'" in normalized:
            assert params is not None and self.slot is not None
            self.slot.update(
                status="COMPLETED",
                evidence_file_id=params[0],
                evidence_matter_version=params[1],
                evidence_audit_event_id=params[2],
                completed_at=params[3],
                updated_at=params[4],
            )
            return _Result(dict(self.slot))
        if "UPDATE web_material_upload_slots SET status = 'FAILED'" in normalized:
            assert params is not None and self.slot is not None
            self.slot.update(
                status="FAILED",
                failure_code=params[0],
                failed_at=params[1],
                updated_at=params[2],
            )
            return _Result(dict(self.slot))
        if "UPDATE web_material_upload_slots SET status = 'RECONCILIATION_REQUIRED'" in normalized:
            assert params is not None and self.slot is not None
            self.slot.update(
                status="RECONCILIATION_REQUIRED",
                failure_code=params[0],
                reconciliation_required_at=params[1],
                updated_at=params[2],
            )
            return _Result(dict(self.slot))
        if normalized.startswith("SELECT upload_id,") and "FROM web_material_upload_slots" in normalized:
            return _Result(dict(self.slot) if self.slot is not None else None)
        return _Result()


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_: object) -> bool:
        return False


def _base_slot_from_reservation(params: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "upload_id": params[0],
        "firm_id": params[1],
        "matter_id": params[2],
        "actor_id": params[3],
        "session_id": params[4],
        "expected_matter_version": params[5],
        "display_name": params[6],
        "declared_content_length": params[7],
        "status": "RESERVED",
        "attempt_id": None,
        "attempt_count": 0,
        "created_at": params[8],
        "expires_at": params[9],
        "claimed_at": None,
        "admitted_upload_id": None,
        "admitted_content_sha256": None,
        "admitted_byte_size": None,
        "admitted_page_count": None,
        "admitted_inspection_hash": None,
        "scanner_name": None,
        "scanner_definitions_version": None,
        "source_object_key": None,
        "source_object_version_id": None,
        "source_reference_hash": None,
        "object_stored_at": None,
        "evidence_file_id": None,
        "evidence_matter_version": None,
        "evidence_audit_event_id": None,
        "completed_at": None,
        "failure_code": None,
        "failed_at": None,
        "reconciliation_required_at": None,
        "updated_at": params[10],
    }


class PostgresWebMaterialUploadSlotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.identity = ServerIdentityContext(
            actor=self.actor,
            session_id=str(uuid4()),
            issuer="https://login.example.test/oidc",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=30),
        )

    @staticmethod
    def _store(connection: _Connection) -> PostgresWebMaterialUploadSlotStore:
        return PostgresWebMaterialUploadSlotStore(
            "postgresql://not-used.invalid/lawcase_upload_test",
            connection_factory=lambda: _ConnectionContext(connection),
        )

    def test_reserve_claim_persist_and_complete_are_tenant_scoped_and_keep_object_key_private(self) -> None:
        connection = _Connection()
        store = self._store(connection)
        reserved = store.reserve_slot(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=1,
            upload_id=str(uuid4()),
            display_name="微信转账记录.pdf",
            declared_content_length=4096,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=10),
        )
        self.assertEqual(reserved.status, UploadSlotStatus.RESERVED)
        statements = [statement for statement, _ in connection.executed]
        context = next(index for index, statement in enumerate(statements) if "app.firm_id" in statement)
        membership = next(index for index, statement in enumerate(statements) if statement.startswith("SELECT m.version,"))
        self.assertLess(context, membership)
        self.assertEqual(connection.executed[context][1], (self.firm_id,))

        claimed = store.claim_or_resume(
            identity=self.identity,
            matter_id=self.matter_id,
            upload_id=reserved.upload_id,
            now=self.now + timedelta(seconds=1),
        )
        assert claimed is not None
        self.assertEqual(claimed.status, UploadSlotStatus.CLAIMED)
        claim_parameters = next(
            parameters
            for statement, parameters in connection.executed
            if "UPDATE web_material_upload_slots SET status = 'CLAIMED'" in statement
        )
        self.assertIsNotNone(claim_parameters)
        self.assertIn(self.identity.session_id, claim_parameters or ())

        admitted = AdmittedWebPdfUpload(
            upload_id=str(uuid4()),
            display_name=reserved.display_name,
            byte_size=4096,
            content_sha256="a" * 64,
            media_type="application/pdf",
            page_count=2,
            inspection_hash="b" * 64,
            scanner_name="test-scanner",
            scanner_definitions_version="definitions-1",
            path=Path("/private/staging/upload.part"),
        )
        stored = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{self.firm_id}/{self.matter_id}/{admitted.content_sha256[:2]}/"
                f"{admitted.content_sha256}/{uuid4()}.pdf"
            ),
            content_sha256=admitted.content_sha256,
            byte_size=admitted.byte_size,
            object_version_id="version-1",
        )
        object_stored = store.record_object_stored(
            identity=self.identity,
            slot=claimed,
            admitted=admitted,
            stored_object=stored,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(object_stored.status, UploadSlotStatus.OBJECT_STORED)
        self.assertNotIn(stored.object_key, repr(object_stored))
        for statement, parameters in connection.executed:
            if parameters is not None and stored.object_key in parameters:
                self.assertIn("UPDATE web_material_upload_slots", statement)
                self.assertNotIn("web_material_upload_slot_events", statement)

        registered = RegisteredWebEvidenceOriginal(
            receipt=CaseLedgerCommandReceipt(
                command_name="REGISTER_WEB_UPLOADED_EVIDENCE_ORIGINAL",
                idempotency_key=f"web-material-upload:{reserved.upload_id}:bind",
                matter_id=self.matter_id,
                matter_version=2,
                audit_event_id=str(uuid4()),
                object_type="EVIDENCE_ORIGINAL",
                object_id=str(uuid4()),
            ),
            source_reference_hash=sha256(stored.object_key.encode("ascii")).hexdigest(),
        )
        completed = store.complete_slot(
            identity=self.identity,
            slot=object_stored,
            registered=registered,
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(completed.status, UploadSlotStatus.COMPLETED)
        self.assertEqual(completed.completed_receipt().matter_version, 2)

    def test_database_faults_and_owner_mismatch_fail_closed_without_internal_details(self) -> None:
        faulty = _Connection(error_marker="INSERT INTO web_material_upload_slots")
        with self.assertRaises(WebMaterialUploadPersistenceBlocked) as blocked:
            self._store(faulty).reserve_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_matter_version=1,
                upload_id=str(uuid4()),
                display_name="材料.pdf",
                declared_content_length=None,
                created_at=self.now,
                expires_at=self.now + timedelta(minutes=10),
            )
        self.assertNotIn("private PostgreSQL fault detail", str(blocked.exception))

        connection = _Connection()
        store = self._store(connection)
        slot = store.reserve_slot(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=1,
            upload_id=str(uuid4()),
            display_name="材料.pdf",
            declared_content_length=None,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=10),
        )
        wrong_identity = ServerIdentityContext(
            actor=self.actor,
            session_id=str(uuid4()),
            issuer=self.identity.issuer,
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=self.identity.authenticated_at,
            expires_at=self.identity.expires_at,
        )
        with self.assertRaises(WebMaterialUploadPersistenceBlocked):
            store.record_object_stored(
                identity=wrong_identity,
                slot=slot,
                admitted=AdmittedWebPdfUpload(
                    upload_id=str(uuid4()),
                    display_name="材料.pdf",
                    byte_size=1,
                    content_sha256="a" * 64,
                    media_type="application/pdf",
                    page_count=1,
                    inspection_hash="b" * 64,
                    scanner_name="scanner",
                    scanner_definitions_version="defs",
                    path=Path("/private/not-used.pdf"),
                ),
                stored_object=StoredWebEvidenceOriginal(
                    object_key=(
                        f"originals/v1/{self.firm_id}/{self.matter_id}/aa/"
                        f"{'a' * 64}/{uuid4()}.pdf"
                    ),
                    content_sha256="a" * 64,
                    byte_size=1,
                ),
                now=self.now,
            )

    def test_terminal_failure_uses_the_exact_owner_session_attempt_cas(self) -> None:
        connection = _Connection()
        store = self._store(connection)
        reserved = store.reserve_slot(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=1,
            upload_id=str(uuid4()),
            display_name="材料.pdf",
            declared_content_length=None,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=10),
        )
        claimed = store.claim_or_resume(
            identity=self.identity,
            matter_id=self.matter_id,
            upload_id=reserved.upload_id,
            now=self.now + timedelta(seconds=1),
        )
        assert claimed is not None
        store.fail_slot(
            identity=self.identity,
            slot=claimed,
            code=UploadSlotFailureCode.CONTENT_REJECTED,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(connection.slot["status"], "FAILED")
        parameters = next(
            params
            for statement, params in connection.executed
            if "UPDATE web_material_upload_slots SET status = 'FAILED'" in statement
        )
        self.assertEqual(len(parameters or ()), 9)
        self.assertEqual((parameters or ())[3:], (
            reserved.upload_id,
            self.firm_id,
            self.matter_id,
            self.actor.actor_id,
            self.identity.session_id,
            claimed.attempt_id,
        ))

    def test_same_session_replay_of_reconciliation_state_is_returned_without_a_second_claim(self) -> None:
        connection = _Connection()
        store = self._store(connection)
        reserved = store.reserve_slot(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=1,
            upload_id=str(uuid4()),
            display_name="材料.pdf",
            declared_content_length=None,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=10),
        )
        claimed = store.claim_or_resume(
            identity=self.identity,
            matter_id=self.matter_id,
            upload_id=reserved.upload_id,
            now=self.now + timedelta(seconds=1),
        )
        assert claimed is not None
        store.require_reconciliation(
            identity=self.identity,
            slot=claimed,
            code=UploadSlotFailureCode.OBJECT_STATE_UNKNOWN,
            now=self.now + timedelta(seconds=2),
        )

        replay = store.claim_or_resume(
            identity=self.identity,
            matter_id=self.matter_id,
            upload_id=reserved.upload_id,
            now=self.now + timedelta(seconds=3),
        )

        assert replay is not None
        self.assertEqual(replay.status, UploadSlotStatus.RECONCILIATION_REQUIRED)
        claims = [
            statement
            for statement, _ in connection.executed
            if "UPDATE web_material_upload_slots SET status = 'CLAIMED'" in statement
        ]
        self.assertEqual(len(claims), 2)


if __name__ == "__main__":
    unittest.main()
