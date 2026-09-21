from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import ZipFile
import unittest

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_archive_upload import ArchiveObjectStateUnknown, ArchiveUploadStatus, WebMaterialArchiveOperation, WebMaterialArchiveUploadService
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebMaterialArchive
from case_kernel.web_zip_staging import WebZipStagingArea


def _zip_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("一.pdf", b"one")
        archive.writestr("二.pdf", b"two")
    return output.getvalue()


class _Store:
    def __init__(self) -> None:
        self.operation: WebMaterialArchiveOperation | None = None
        self.reconciled = False

    def reserve_archive(self, **kwargs):
        identity = kwargs["identity"]
        self.operation = WebMaterialArchiveOperation(
            archive_id=kwargs["archive_id"], firm_id=identity.actor.firm_id, matter_id=kwargs["matter_id"],
            actor_id=identity.actor.actor_id, session_id=identity.session_id,
            expected_matter_version=kwargs["expected_matter_version"], display_name=kwargs["display_name"],
            declared_content_length=kwargs["declared_content_length"], status=ArchiveUploadStatus.RESERVED,
            created_at=kwargs["created_at"], expires_at=kwargs["expires_at"],
        )
        return self.operation

    def claim_or_resume(self, **kwargs):
        assert self.operation is not None
        if self.operation.status is ArchiveUploadStatus.RESERVED:
            self.operation = replace(self.operation, status=ArchiveUploadStatus.CLAIMED, attempt_id=str(uuid4()), attempt_count=1)
        return self.operation

    def record_object_stored(self, **kwargs):
        assert self.operation is not None
        admitted, stored = kwargs["admitted"], kwargs["stored_object"]
        self.operation = replace(
            self.operation, status=ArchiveUploadStatus.OBJECT_STORED, archive_content_sha256=admitted.content_sha256,
            archive_byte_size=admitted.byte_size, entry_count=len(admitted.entries), expanded_byte_size=admitted.expanded_byte_size,
            inventory=tuple({"name": entry.name, "sha256": entry.content_sha256, "byte_size": entry.byte_size} for entry in admitted.entries),
            source_object_key=stored.object_key, source_object_version_id=stored.object_version_id,
        )
        return self.operation

    def require_reconciliation(self, **kwargs):
        assert self.operation is not None
        self.reconciled = True
        self.operation = replace(self.operation, status=ArchiveUploadStatus.RECONCILIATION_REQUIRED)

    def get_operation(self, **kwargs):
        return self.operation


class _ObjectStore:
    def put_verified_zip(self, archive, *, firm_id: str, matter_id: str):
        return StoredWebMaterialArchive(
            object_key=f"material-archives/v1/{firm_id}/{matter_id}/{archive.content_sha256[:2]}/{archive.content_sha256}/{uuid4()}.zip",
            content_sha256=archive.content_sha256, byte_size=archive.byte_size,
            entry_count=len(archive.entries), expanded_byte_size=archive.expanded_byte_size,
        )


class WebArchiveUploadServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_receipt_is_pending_and_replay_does_not_reupload(self) -> None:
        with TemporaryDirectory() as temporary:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
            identity = ServerIdentityContext(actor=actor, session_id=str(uuid4()), issuer="https://id.example",
                                              authentication_method=AuthenticationMethod.OIDC_MFA,
                                              authenticated_at=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=10))
            store, objects = _Store(), _ObjectStore()
            service = WebMaterialArchiveUploadService(store=store, staging=WebZipStagingArea(Path(temporary)), object_store=objects, clock=lambda: now)
            slot = service.create_slot(identity=identity, matter_id=str(uuid4()), expected_version=1,
                                       client_filename="材料.zip", declared_content_length=len(_zip_bytes()))
            receipt = await service.accept_content(identity=identity, matter_id=store.operation.matter_id, archive_id=slot.archive_id,
                                                   chunks=_chunks(_zip_bytes()))
            self.assertEqual(receipt.processing_status, "STORED_PENDING_PROCESSING")
            self.assertEqual(receipt.entry_count, 2)
            replay = await service.accept_content(identity=identity, matter_id=store.operation.matter_id, archive_id=slot.archive_id,
                                                  chunks=_chunks(b"must-not-be-read"))
            self.assertEqual(replay, receipt)

    async def test_uncertain_store_handoff_blocks_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
            identity = ServerIdentityContext(actor=actor, session_id=str(uuid4()), issuer="https://id.example",
                                              authentication_method=AuthenticationMethod.OIDC_MFA,
                                              authenticated_at=now - timedelta(minutes=1), expires_at=now + timedelta(minutes=10))
            store, objects = _Store(), _ObjectStore()
            original = store.record_object_stored
            store.record_object_stored = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("unknown"))
            service = WebMaterialArchiveUploadService(store=store, staging=WebZipStagingArea(Path(temporary)), object_store=objects, clock=lambda: now)
            slot = service.create_slot(identity=identity, matter_id=str(uuid4()), expected_version=1,
                                       client_filename="材料.zip", declared_content_length=len(_zip_bytes()))
            with self.assertRaises(ArchiveObjectStateUnknown):
                await service.accept_content(identity=identity, matter_id=store.operation.matter_id, archive_id=slot.archive_id,
                                             chunks=_chunks(_zip_bytes()))
            del original
            self.assertTrue(store.reconciled)


async def _chunks(value: bytes):
    for start in range(0, len(value), 101):
        yield value[start:start + 101]
