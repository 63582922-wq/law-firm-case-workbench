from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest

from reportlab.pdfgen import canvas

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_material_upload import (
    PostObjectStoreReconciliationRequired,
    UploadSlotFailureCode,
    UploadSlotStatus,
    WebMaterialUploadService,
    WebMaterialUploadSlot,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.errors import VersionConflict
from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.evidence_manifest_postgres import RegisteredWebEvidenceOriginal
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_upload_staging import AdmittedWebPdfUpload, WebUploadStagingArea


class _CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        del path
        return FileSafetyScanReceipt("test-scanner", "definitions-1", expected_sha256, "CLEAN")


class _MemorySlotStore:
    def __init__(self) -> None:
        self.slots: dict[str, WebMaterialUploadSlot] = {}
        self.fail_object_persistence = False

    def reserve_slot(self, **kwargs) -> WebMaterialUploadSlot:
        identity = kwargs["identity"]
        slot = WebMaterialUploadSlot(
            upload_id=kwargs["upload_id"],
            firm_id=identity.actor.firm_id,
            matter_id=kwargs["matter_id"],
            actor_id=identity.actor.actor_id,
            session_id=identity.session_id,
            expected_matter_version=kwargs["expected_matter_version"],
            display_name=kwargs["display_name"],
            declared_content_length=kwargs["declared_content_length"],
            status=UploadSlotStatus.RESERVED,
            created_at=kwargs["created_at"],
            expires_at=kwargs["expires_at"],
            updated_at=kwargs["created_at"],
        )
        self.slots[slot.upload_id] = slot
        return slot

    def claim_or_resume(self, **kwargs) -> WebMaterialUploadSlot | None:
        identity = kwargs["identity"]
        slot = self.slots.get(kwargs["upload_id"])
        if slot is None or not self._owns(slot, identity, kwargs["matter_id"]):
            return None
        if slot.status is UploadSlotStatus.RESERVED:
            if slot.expires_at <= kwargs["now"]:
                return None
            claimed = replace(
                slot,
                status=UploadSlotStatus.CLAIMED,
                attempt_id=str(uuid4()),
                attempt_count=1,
                claimed_at=kwargs["now"],
                updated_at=kwargs["now"],
            )
            self.slots[slot.upload_id] = claimed
            return claimed
        if slot.status in {
            UploadSlotStatus.OBJECT_STORED,
            UploadSlotStatus.COMPLETED,
            UploadSlotStatus.RECONCILIATION_REQUIRED,
        }:
            return slot
        return None

    def record_object_stored(self, **kwargs) -> WebMaterialUploadSlot:
        if self.fail_object_persistence:
            raise RuntimeError("database result unknown")
        identity, slot, admitted, stored, now = (
            kwargs["identity"],
            kwargs["slot"],
            kwargs["admitted"],
            kwargs["stored_object"],
            kwargs["now"],
        )
        current = self.slots[slot.upload_id]
        if current.status is not UploadSlotStatus.CLAIMED or not self._owns(current, identity, slot.matter_id):
            raise RuntimeError("not claim owner")
        persisted = replace(
            current,
            status=UploadSlotStatus.OBJECT_STORED,
            admitted_upload_id=admitted.upload_id,
            admitted_content_sha256=admitted.content_sha256,
            admitted_byte_size=admitted.byte_size,
            admitted_page_count=admitted.page_count,
            admitted_inspection_hash=admitted.inspection_hash,
            scanner_name=admitted.scanner_name,
            scanner_definitions_version=admitted.scanner_definitions_version,
            source_object_key=stored.object_key,
            source_object_version_id=stored.object_version_id,
            source_reference_hash=sha256(stored.object_key.encode("ascii")).hexdigest(),
            object_stored_at=now,
            updated_at=now,
        )
        self.slots[slot.upload_id] = persisted
        return persisted

    def complete_slot(self, **kwargs) -> WebMaterialUploadSlot:
        identity, slot, registered, now = kwargs["identity"], kwargs["slot"], kwargs["registered"], kwargs["now"]
        current = self.slots[slot.upload_id]
        if current.status is not UploadSlotStatus.OBJECT_STORED or not self._owns(current, identity, slot.matter_id):
            raise RuntimeError("not object owner")
        receipt = registered.receipt
        completed = replace(
            current,
            status=UploadSlotStatus.COMPLETED,
            evidence_file_id=receipt.object_id,
            evidence_matter_version=receipt.matter_version,
            evidence_audit_event_id=receipt.audit_event_id,
            completed_at=now,
            updated_at=now,
        )
        self.slots[slot.upload_id] = completed
        return completed

    def fail_slot(self, **kwargs) -> None:
        self._terminal(kwargs, status=UploadSlotStatus.FAILED)

    def require_reconciliation(self, **kwargs) -> None:
        self._terminal(kwargs, status=UploadSlotStatus.RECONCILIATION_REQUIRED)

    def get_slot(self, **kwargs) -> WebMaterialUploadSlot | None:
        slot = self.slots.get(kwargs["upload_id"])
        if slot is None or not self._owns(slot, kwargs["identity"], kwargs["matter_id"]):
            return None
        return slot

    def _terminal(self, kwargs, *, status: UploadSlotStatus) -> None:
        identity, slot, code, now = kwargs["identity"], kwargs["slot"], kwargs["code"], kwargs["now"]
        current = self.slots[slot.upload_id]
        if not self._owns(current, identity, slot.matter_id):
            raise RuntimeError("not slot owner")
        self.slots[slot.upload_id] = replace(
            current,
            status=status,
            failure_code=code,
            failed_at=now if status is UploadSlotStatus.FAILED else None,
            reconciliation_required_at=now if status is UploadSlotStatus.RECONCILIATION_REQUIRED else None,
            updated_at=now,
        )

    @staticmethod
    def _owns(slot: WebMaterialUploadSlot, identity: ServerIdentityContext, matter_id: str) -> bool:
        return (
            slot.firm_id == identity.actor.firm_id
            and slot.actor_id == identity.actor.actor_id
            and slot.session_id == identity.session_id
            and slot.matter_id == matter_id
        )


class _ObjectStore:
    def __init__(self) -> None:
        self.stored: list[StoredWebEvidenceOriginal] = []
        self.deleted: list[StoredWebEvidenceOriginal] = []

    def put_verified_pdf(self, upload: AdmittedWebPdfUpload, *, firm_id: str, matter_id: str) -> StoredWebEvidenceOriginal:
        stored = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{firm_id}/{matter_id}/{upload.content_sha256[:2]}/"
                f"{upload.content_sha256}/{uuid4()}.pdf"
            ),
            content_sha256=upload.content_sha256,
            byte_size=upload.byte_size,
            object_version_id="test-version-1",
        )
        self.stored.append(stored)
        return stored

    def delete_unbound_upload_object(self, stored: StoredWebEvidenceOriginal) -> None:
        self.deleted.append(stored)


class _EvidenceStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.bound = False
        self.error: Exception | None = None

    def register_web_uploaded_pdf_original(self, **kwargs) -> RegisteredWebEvidenceOriginal:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.bound = True
        upload = kwargs["upload"]
        return RegisteredWebEvidenceOriginal(
            receipt=CaseLedgerCommandReceipt(
                command_name="REGISTER_WEB_UPLOADED_EVIDENCE_ORIGINAL",
                idempotency_key=kwargs["idempotency_key"],
                matter_id=kwargs["matter_id"],
                matter_version=kwargs["expected_version"] + 1,
                audit_event_id=str(uuid4()),
                object_type="EVIDENCE_ORIGINAL",
                object_id=str(uuid4()),
            ),
            source_reference_hash=sha256(kwargs["stored_object"].object_key.encode("ascii")).hexdigest(),
        )

    def is_web_uploaded_object_bound(self, **kwargs) -> bool:
        del kwargs
        return self.bound


def _pdf_bytes(root: Path) -> bytes:
    path = root / "material.pdf"
    document = canvas.Canvas(str(path))
    document.drawString(36, 720, "synthetic material")
    document.showPage()
    document.save()
    return path.read_bytes()


class WebMaterialUploadServiceTests(unittest.TestCase):
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

    def _service(self, root: Path, *, slots: _MemorySlotStore | None = None, evidence: _EvidenceStore | None = None):
        slots = slots or _MemorySlotStore()
        objects = _ObjectStore()
        evidence = evidence or _EvidenceStore()
        worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        service = WebMaterialUploadService(
            slot_store=slots,
            staging=WebUploadStagingArea(root / "private-staging"),
            scanner=_CleanScanner(),
            object_store=objects,
            evidence_store=evidence,
            system_worker_for_firm=lambda firm_id: worker if firm_id == self.firm_id else Actor(
                str(uuid4()), firm_id, frozenset({Role.SYSTEM_WORKER})
            ),
            clock=lambda: self.now,
        )
        return service, slots, objects, evidence

    @staticmethod
    async def _chunks(content: bytes):
        yield content[:17]
        yield b""
        yield content[17:]

    def test_streams_once_through_scan_private_object_and_atomic_evidence_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, slots, objects, evidence = self._service(root)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename=r"C:\Users\lawyer\案件资料\微信转账记录.pdf",
                declared_content_length=1000,
            )
            receipt = asyncio.run(
                service.accept_content(
                    identity=self.identity,
                    matter_id=self.matter_id,
                    upload_id=slot.upload_id,
                    chunks=self._chunks(_pdf_bytes(root)),
                )
            )

            self.assertEqual(receipt.display_name, "微信转账记录.pdf")
            self.assertEqual(receipt.page_count, 1)
            self.assertEqual(receipt.matter_version, 2)
            self.assertEqual(slots.slots[slot.upload_id].status, UploadSlotStatus.COMPLETED)
            self.assertEqual(len(objects.stored), 1)
            self.assertEqual(len(evidence.calls), 1)
            self.assertEqual(evidence.calls[0]["idempotency_key"], f"web-material-upload:{slot.upload_id}:bind")
            self.assertNotIn(objects.stored[0].object_key, repr(receipt))
            self.assertNotIn(objects.stored[0].object_key, repr(slots.slots[slot.upload_id]))
            self.assertEqual(list((root / "private-staging").iterdir()), [])

    def test_completed_retry_returns_saved_receipt_without_consuming_second_body(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _, _, evidence = self._service(root)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename="材料.pdf",
                declared_content_length=None,
            )
            first = asyncio.run(
                service.accept_content(
                    identity=self.identity,
                    matter_id=self.matter_id,
                    upload_id=slot.upload_id,
                    chunks=self._chunks(_pdf_bytes(root)),
                )
            )
            consumed = False

            async def hostile_retry():
                nonlocal consumed
                consumed = True
                raise AssertionError("completed slot must not read retry bytes")
                yield b""  # pragma: no cover - makes this an async generator.

            second = asyncio.run(
                service.accept_content(
                    identity=self.identity,
                    matter_id=self.matter_id,
                    upload_id=slot.upload_id,
                    chunks=hostile_retry(),
                )
            )
            self.assertEqual(second, first)
            self.assertFalse(consumed)
            self.assertEqual(len(evidence.calls), 1)

    def test_other_session_cannot_claim_or_replay_an_upload_slot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _, objects, _ = self._service(root)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename="材料.pdf",
                declared_content_length=None,
            )
            other = replace(self.identity, session_id=str(uuid4()))
            with self.assertRaisesRegex(Exception, "unavailable"):
                asyncio.run(
                    service.accept_content(
                        identity=other,
                        matter_id=self.matter_id,
                        upload_id=slot.upload_id,
                        chunks=self._chunks(_pdf_bytes(root)),
                    )
                )
            self.assertEqual(objects.stored, [])

    def test_known_unbound_version_conflict_deletes_only_after_worker_check_and_preserves_409_semantics(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _EvidenceStore()
            evidence.error = VersionConflict("expected version changed")
            service, slots, objects, _ = self._service(root, evidence=evidence)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename="材料.pdf",
                declared_content_length=None,
            )
            with self.assertRaises(VersionConflict):
                asyncio.run(
                    service.accept_content(
                        identity=self.identity,
                        matter_id=self.matter_id,
                        upload_id=slot.upload_id,
                        chunks=self._chunks(_pdf_bytes(root)),
                    )
                )
            self.assertEqual(len(objects.deleted), 1)
            self.assertEqual(slots.slots[slot.upload_id].status, UploadSlotStatus.FAILED)
            self.assertEqual(slots.slots[slot.upload_id].failure_code, UploadSlotFailureCode.LEDGER_REJECTED)

    def test_ambiguous_binding_never_deletes_the_private_object(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _EvidenceStore()
            evidence.error = RuntimeError("lost database response")
            evidence.bound = True
            service, slots, objects, _ = self._service(root, evidence=evidence)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename="材料.pdf",
                declared_content_length=None,
            )
            with self.assertRaises(PostObjectStoreReconciliationRequired):
                asyncio.run(
                    service.accept_content(
                        identity=self.identity,
                        matter_id=self.matter_id,
                        upload_id=slot.upload_id,
                        chunks=self._chunks(_pdf_bytes(root)),
                    )
                )
            self.assertEqual(objects.deleted, [])
            self.assertEqual(slots.slots[slot.upload_id].status, UploadSlotStatus.RECONCILIATION_REQUIRED)

    def test_object_persist_failure_is_unknown_and_never_triggers_delete(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            slots = _MemorySlotStore()
            slots.fail_object_persistence = True
            service, slots, objects, _ = self._service(root, slots=slots)
            slot = service.create_slot(
                identity=self.identity,
                matter_id=self.matter_id,
                expected_version=1,
                client_filename="材料.pdf",
                declared_content_length=None,
            )
            with self.assertRaises(PostObjectStoreReconciliationRequired):
                asyncio.run(
                    service.accept_content(
                        identity=self.identity,
                        matter_id=self.matter_id,
                        upload_id=slot.upload_id,
                        chunks=self._chunks(_pdf_bytes(root)),
                    )
                )
            self.assertEqual(len(objects.stored), 1)
            self.assertEqual(objects.deleted, [])
            self.assertEqual(slots.slots[slot.upload_id].status, UploadSlotStatus.RECONCILIATION_REQUIRED)
            self.assertEqual(slots.slots[slot.upload_id].failure_code, UploadSlotFailureCode.OBJECT_STATE_UNKNOWN)

            consumed = False

            async def hostile_retry():
                nonlocal consumed
                consumed = True
                raise AssertionError("reconciliation-required slot must not read retry bytes")
                yield b""  # pragma: no cover - makes this an async generator.

            with self.assertRaises(PostObjectStoreReconciliationRequired):
                asyncio.run(
                    service.accept_content(
                        identity=self.identity,
                        matter_id=self.matter_id,
                        upload_id=slot.upload_id,
                        chunks=hostile_retry(),
                    )
                )
            self.assertFalse(consumed)


if __name__ == "__main__":
    unittest.main()
