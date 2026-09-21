from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_common_material_upload import (
    CommonMaterialUploadFailureCode,
    CommonMaterialUploadOperation,
    CommonMaterialUploadReconciliationRequired,
    CommonMaterialUploadStatus,
    WebCommonMaterialUploadService,
)
from case_kernel.common_material_object_store import (
    CommonMaterialObjectStoreBlocked,
    StoredCommonMaterialOriginal,
)
from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.models import Actor, Role
from case_kernel.web_common_material_admission import CommonMaterialStagingArea
from case_kernel.web_common_material_admission import common_material_agent_status


class _Scanner:
    def scan(self, path: Path, *, expected_sha256: str):
        assert sha256(path.read_bytes()).hexdigest() == expected_sha256
        return FileSafetyScanReceipt("ClamAV", "test-definitions", expected_sha256, "CLEAN")


class _UnavailableScanner:
    def scan(self, path: Path, *, expected_sha256: str):
        raise RuntimeError("scanner process is unavailable")


class _Store:
    def __init__(self) -> None:
        self.operation: CommonMaterialUploadOperation | None = None
        self.reconciliation_code: CommonMaterialUploadFailureCode | None = None
        self.reconciliation_admitted = None
        self.reconciliation_stored = None
        self.failed = False
        self.failure_code: CommonMaterialUploadFailureCode | None = None

    def reserve_upload(self, **kwargs):
        identity = kwargs["identity"]
        if self.operation is None:
            self.operation = CommonMaterialUploadOperation(
                upload_id=kwargs["upload_id"],
                material_object_id=kwargs["material_object_id"],
                firm_id=identity.actor.firm_id,
                matter_id=kwargs["matter_id"],
                actor_id=identity.actor.actor_id,
                session_id=identity.session_id,
                expected_matter_version=kwargs["expected_matter_version"],
                display_name=kwargs["display_name"],
                declared_byte_size=kwargs["declared_byte_size"],
                declared_media_type=kwargs["declared_media_type"],
                reserve_idempotency_key=kwargs["idempotency_key"],
                reserve_request_hash="1" * 64,
                status=CommonMaterialUploadStatus.RESERVED,
                created_at=kwargs["created_at"],
                expires_at=kwargs["expires_at"],
            )
        return self.operation

    def claim_or_resume(self, **kwargs):
        assert self.operation is not None
        if self.operation.status is CommonMaterialUploadStatus.RESERVED:
            self.operation = replace(
                self.operation,
                status=CommonMaterialUploadStatus.CLAIMED,
                content_idempotency_key=kwargs["content_idempotency_key"],
                attempt_id=str(uuid4()),
                attempt_count=1,
                claimed_at=kwargs["now"],
            )
        return self.operation

    def record_object_stored(self, **kwargs):
        assert self.operation is not None
        admitted = kwargs["admitted"]
        stored = kwargs["stored"]
        self.operation = replace(
            self.operation,
            status=CommonMaterialUploadStatus.OBJECT_STORED,
            admitted_format=admitted.admitted_format,
            canonical_kind=admitted.canonical_kind.value,
            admitted_media_type=admitted.media_type,
            route=admitted.route,
            admitted_byte_size=admitted.byte_size,
            admitted_content_sha256=admitted.content_sha256,
            admitted_inspection_hash=admitted.inspection_hash,
            scanner_name=admitted.scanner_name,
            scanner_definitions_version=admitted.scanner_definitions_version,
            review_flags=admitted.review_flags,
            source_object_key=stored.object_key,
            source_object_version_id=stored.object_version_id,
            source_reference_hash=sha256(stored.object_key.encode()).hexdigest(),
            object_stored_at=kwargs["now"],
        )
        return self.operation

    def complete_registration(self, **kwargs):
        assert self.operation is not None
        agent_status = common_material_agent_status(self.operation.admitted_format)
        agent_source_ref = (
            f"material-object:{self.operation.material_object_id}"
            if agent_status.value == "AGENT_READY"
            else None
        )
        self.operation = replace(
            self.operation,
            status=CommonMaterialUploadStatus.COMPLETED,
            result_matter_version=self.operation.expected_matter_version + 1,
            audit_event_id=str(uuid4()),
            outbox_id=str(uuid4()),
            completed_at=kwargs["now"],
            agent_status=agent_status,
            agent_source_ref=agent_source_ref,
        )
        return self.operation

    def fail_upload(self, **kwargs):
        self.failed = True
        self.failure_code = kwargs["failure_code"]
        assert self.operation is not None
        self.operation = replace(
            self.operation,
            status=CommonMaterialUploadStatus.FAILED,
            failure_code=kwargs["failure_code"],
            terminal_at=kwargs["now"],
        )

    def require_reconciliation(self, **kwargs):
        assert self.operation is not None
        self.reconciliation_code = kwargs["failure_code"]
        self.reconciliation_admitted = kwargs.get("admitted")
        self.reconciliation_stored = kwargs.get("stored")
        self.operation = replace(
            self.operation,
            status=CommonMaterialUploadStatus.RECONCILIATION_REQUIRED,
            failure_code=kwargs["failure_code"],
            terminal_at=kwargs["now"],
        )

    def get_upload(self, **kwargs):
        return self.operation


class _Objects:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def put_immutable_common_material(self, admitted, *, firm_id: str, matter_id: str):
        self.calls += 1
        if self.fail:
            raise RuntimeError("remote outcome unknown")
        return StoredCommonMaterialOriginal(
            object_key=(
                f"case-materials/v1/{firm_id}/{matter_id}/"
                f"{admitted.content_sha256[:2]}/{admitted.content_sha256}"
            ),
            material_object_id=admitted.material_object_id,
            content_sha256=admitted.content_sha256,
            byte_size=admitted.byte_size,
            media_type=admitted.media_type,
            admitted_format=admitted.admitted_format,
            route=admitted.route,
            inspection_hash=admitted.inspection_hash,
            object_version_id="version-1",
        )


class _KnownUnavailableObjects:
    def put_immutable_common_material(self, admitted, *, firm_id: str, matter_id: str):
        raise CommonMaterialObjectStoreBlocked("storage configuration is unavailable")


async def _chunks(content: bytes, counter: list[int] | None = None):
    if counter is not None:
        counter.append(1)
    for offset in range(0, len(content), 7):
        yield content[offset : offset + 7]


class WebCommonMaterialUploadServiceTests(unittest.IsolatedAsyncioTestCase):
    def _identity(self, now: datetime) -> ServerIdentityContext:
        actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.ASSISTANT}))
        return ServerIdentityContext(
            actor=actor,
            session_id=str(uuid4()),
            issuer="https://identity.example.invalid",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
        )

    async def test_complete_receipt_is_review_only_and_replay_does_not_consume_second_body(self) -> None:
        content = "案情材料，仅供律师复核。".encode()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        identity = self._identity(now)
        matter_id = str(uuid4())
        with TemporaryDirectory() as temporary:
            store, objects = _Store(), _Objects()
            service = WebCommonMaterialUploadService(
                store=store,
                staging=CommonMaterialStagingArea(Path(temporary)),
                scanner=_Scanner(),
                object_store=objects,
                clock=lambda: now,
            )
            slot = service.create_slot(
                identity=identity,
                matter_id=matter_id,
                expected_version=7,
                client_filename="案情.txt",
                declared_byte_size=len(content),
                declared_media_type="text/plain",
                idempotency_key="reserve-common-001",
            )
            receipt = await service.accept_content(
                identity=identity,
                matter_id=matter_id,
                upload_id=slot.upload_id,
                idempotency_key="content-common-001",
                chunks=_chunks(content),
            )
            self.assertEqual(receipt.matter_version, 8)
            self.assertEqual(receipt.review_status.value, "NEEDS_LAWYER_REVIEW")
            self.assertFalse(receipt.formal_fact)
            self.assertFalse(receipt.formal_transaction)
            self.assertFalse(receipt.legal_conclusion)
            self.assertFalse(receipt.evidence_decision)
            self.assertFalse(receipt.court_ready)
            self.assertEqual(receipt.agent_status.value, "INGESTED_PENDING_ADAPTER")
            self.assertIsNone(receipt.agent_source_ref)
            second_body_counter: list[int] = []
            replay = await service.accept_content(
                identity=identity,
                matter_id=matter_id,
                upload_id=slot.upload_id,
                idempotency_key="content-common-001",
                chunks=_chunks(b"must not be read", second_body_counter),
            )
            self.assertEqual(replay, receipt)
            self.assertEqual(second_body_counter, [])
            self.assertEqual(objects.calls, 1)

    async def test_unknown_object_result_is_reconciled_and_never_retried(self) -> None:
        content = b"review only"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        identity = self._identity(now)
        matter_id = str(uuid4())
        with TemporaryDirectory() as temporary:
            store, objects = _Store(), _Objects(fail=True)
            service = WebCommonMaterialUploadService(
                store=store,
                staging=CommonMaterialStagingArea(Path(temporary)),
                scanner=_Scanner(),
                object_store=objects,
                clock=lambda: now,
            )
            slot = service.create_slot(
                identity=identity,
                matter_id=matter_id,
                expected_version=1,
                client_filename="note.txt",
                declared_byte_size=len(content),
                declared_media_type="text/plain",
                idempotency_key="reserve-common-002",
            )
            with self.assertRaises(CommonMaterialUploadReconciliationRequired):
                await service.accept_content(
                    identity=identity,
                    matter_id=matter_id,
                    upload_id=slot.upload_id,
                    idempotency_key="content-common-002",
                    chunks=_chunks(content),
                )
            self.assertEqual(store.reconciliation_code, CommonMaterialUploadFailureCode.OBJECT_STATE_UNKNOWN)
            self.assertIsNotNone(store.reconciliation_admitted)
            self.assertIsNone(store.reconciliation_stored)
            second_body_counter: list[int] = []
            with self.assertRaises(CommonMaterialUploadReconciliationRequired):
                await service.accept_content(
                    identity=identity,
                    matter_id=matter_id,
                    upload_id=slot.upload_id,
                    idempotency_key="content-common-002",
                    chunks=_chunks(b"second body", second_body_counter),
                )
            self.assertEqual(second_body_counter, [])
            self.assertEqual(objects.calls, 1)

    async def test_scanner_outage_is_not_mislabeled_as_rejected_content(self) -> None:
        content = b"review only"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        identity = self._identity(now)
        matter_id = str(uuid4())
        with TemporaryDirectory() as temporary:
            store = _Store()
            service = WebCommonMaterialUploadService(
                store=store,
                staging=CommonMaterialStagingArea(Path(temporary)),
                scanner=_UnavailableScanner(),
                object_store=_Objects(),
                clock=lambda: now,
            )
            slot = service.create_slot(
                identity=identity,
                matter_id=matter_id,
                expected_version=1,
                client_filename="note.txt",
                declared_byte_size=len(content),
                declared_media_type="text/plain",
                idempotency_key="reserve-common-003",
            )
            with self.assertRaisesRegex(ValueError, "admission service is unavailable"):
                await service.accept_content(
                    identity=identity,
                    matter_id=matter_id,
                    upload_id=slot.upload_id,
                    idempotency_key="content-common-003",
                    chunks=_chunks(content),
                )
            self.assertEqual(
                store.failure_code,
                CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE,
            )
            status = service.read_status(
                identity=identity,
                matter_id=matter_id,
                upload_id=slot.upload_id,
            )
            self.assertEqual(status.status, "ADMISSION_UNAVAILABLE")
            self.assertFalse(status.retry_allowed)

    async def test_known_object_store_failure_is_not_mislabeled_as_unknown_remote_state(self) -> None:
        content = b"review only"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        identity = self._identity(now)
        matter_id = str(uuid4())
        with TemporaryDirectory() as temporary:
            store = _Store()
            service = WebCommonMaterialUploadService(
                store=store,
                staging=CommonMaterialStagingArea(Path(temporary)),
                scanner=_Scanner(),
                object_store=_KnownUnavailableObjects(),
                clock=lambda: now,
            )
            slot = service.create_slot(
                identity=identity,
                matter_id=matter_id,
                expected_version=1,
                client_filename="note.txt",
                declared_byte_size=len(content),
                declared_media_type="text/plain",
                idempotency_key="reserve-common-004",
            )
            with self.assertRaisesRegex(ValueError, "object service is unavailable"):
                await service.accept_content(
                    identity=identity,
                    matter_id=matter_id,
                    upload_id=slot.upload_id,
                    idempotency_key="content-common-004",
                    chunks=_chunks(content),
                )
            self.assertEqual(
                store.failure_code,
                CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE,
            )
            self.assertIsNone(store.reconciliation_code)


if __name__ == "__main__":
    unittest.main()
