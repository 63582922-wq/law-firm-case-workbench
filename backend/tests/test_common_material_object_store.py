from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from uuid import uuid4

from case_kernel.common_material_object_store import (
    CommonMaterialObjectStateUnknown,
    S3CommonMaterialPrivateObjectStore,
)
from case_kernel.material_type_registry import MaterialCanonicalKind
from case_kernel.web_common_material_admission import (
    AdmittedCommonMaterial,
    CommonMaterialFormat,
    CommonMaterialRoute,
)
from case_kernel.web_object_store import S3PrivateObjectStoreConfig
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore


class _Body:
    def __init__(self, content: bytes) -> None:
        self._stream = BytesIO(content)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class _Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, object]]] = {}
        self.last_put: dict[str, object] | None = None
        self.last_head: dict[str, object] | None = None
        self.fail_after_write = False

    def put_object(self, **kwargs):
        self.last_put = dict(kwargs)
        key = str(kwargs["Key"])
        if key in self.objects:
            raise RuntimeError("precondition failed")
        body = kwargs["Body"].read()
        metadata = {
            "ContentLength": len(body),
            "ContentType": kwargs["ContentType"],
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "Metadata": kwargs["Metadata"],
            "ServerSideEncryption": kwargs["ServerSideEncryption"],
            "VersionId": "version-1",
        }
        self.objects[key] = (body, metadata)
        if self.fail_after_write:
            raise RuntimeError("lost response")
        return {"VersionId": "version-1"}

    def head_object(self, **kwargs):
        self.last_head = dict(kwargs)
        return dict(self.objects[str(kwargs["Key"])][1])

    def get_object(self, **kwargs):
        content, _ = self.objects[str(kwargs["Key"])]
        return {"Body": _Body(content)}

    def delete_object(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("immutable originals are never deleted by admission")


def _config() -> S3PrivateObjectStoreConfig:
    return S3PrivateObjectStoreConfig(
        endpoint_url="https://objects.example.invalid",
        region_name="cn-test-1",
        bucket="lawcase-private-test",
        access_key_id="test-access-key",
        secret_access_key="test-secret-value-long-enough",
    )


class CommonMaterialObjectStoreTests(unittest.TestCase):
    def _admitted(self, root: Path, content: bytes) -> AdmittedCommonMaterial:
        path = root / "upload.txt"
        path.write_bytes(content)
        path.chmod(0o600)
        return AdmittedCommonMaterial(
            upload_id=str(uuid4()),
            material_object_id=str(uuid4()),
            display_name="说明.txt",
            admitted_format=CommonMaterialFormat.TXT,
            canonical_kind=MaterialCanonicalKind.TEXT,
            media_type="text/plain",
            route=CommonMaterialRoute.COMMON_DOCUMENT_READER,
            byte_size=len(content),
            content_sha256=sha256(content).hexdigest(),
            inspection_hash="a" * 64,
            scanner_name="ClamAV",
            scanner_definitions_version="ClamAV test-db",
            review_flags=(),
            path=path,
        )

    def test_put_uses_no_overwrite_checksum_encryption_and_worker_materialization(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            admitted = self._admitted(root, "待复核内容".encode())
            client = _Client()
            store = S3CommonMaterialPrivateObjectStore(_config(), client=client)
            firm_id, matter_id = str(uuid4()), str(uuid4())
            stored = store.put_immutable_common_material(admitted, firm_id=firm_id, matter_id=matter_id)
            self.assertEqual(client.last_put["IfNoneMatch"], "*")
            self.assertEqual(
                client.last_put["ChecksumSHA256"],
                base64.b64encode(bytes.fromhex(admitted.content_sha256)).decode("ascii"),
            )
            self.assertEqual(client.last_put["ServerSideEncryption"], "AES256")
            self.assertEqual(client.last_head["ChecksumMode"], "ENABLED")
            self.assertEqual(
                stored.object_key,
                f"case-materials/v1/{firm_id}/{matter_id}/"
                f"{admitted.content_sha256[:2]}/{admitted.content_sha256}",
            )
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            destination = worker_root / "source.txt"
            materialized = store.materialize_common_material(stored, destination=destination)
            self.assertEqual(materialized.read_bytes(), admitted.path.read_bytes())
            self.assertEqual(materialized.stat().st_mode & 0o777, 0o600)

    def test_lost_put_response_is_unknown_and_recovery_is_read_only(self) -> None:
        with TemporaryDirectory() as temporary:
            admitted = self._admitted(Path(temporary), b"source")
            client = _Client()
            client.fail_after_write = True
            store = S3CommonMaterialPrivateObjectStore(_config(), client=client)
            firm_id, matter_id = str(uuid4()), str(uuid4())
            with self.assertRaises(CommonMaterialObjectStateUnknown):
                store.put_immutable_common_material(admitted, firm_id=firm_id, matter_id=matter_id)
            recovered = store.recover_immutable_common_material(
                firm_id=firm_id,
                matter_id=matter_id,
                material_object_id=admitted.material_object_id,
                content_sha256=admitted.content_sha256,
                byte_size=admitted.byte_size,
                media_type=admitted.media_type,
                admitted_format=admitted.admitted_format,
                route=admitted.route,
                inspection_hash=admitted.inspection_hash,
            )
            self.assertEqual(recovered.content_sha256, admitted.content_sha256)
            self.assertEqual(len(client.objects), 1)

    def test_native_image_key_and_metadata_are_readable_by_the_0038_binding_store(self) -> None:
        content = b"\x89PNG\r\n\x1a\n" + b"test-native-image"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "upload.png"
            path.write_bytes(content)
            path.chmod(0o600)
            admitted = AdmittedCommonMaterial(
                upload_id=str(uuid4()),
                material_object_id=str(uuid4()),
                display_name="image.png",
                admitted_format=CommonMaterialFormat.PNG,
                canonical_kind=MaterialCanonicalKind.IMAGE,
                media_type="image/png",
                route=CommonMaterialRoute.VISUAL_OCR,
                byte_size=len(content),
                content_sha256=sha256(content).hexdigest(),
                inspection_hash="b" * 64,
                scanner_name="ClamAV",
                scanner_definitions_version="ClamAV test-db",
                review_flags=(),
                path=path,
            )
            client = _Client()
            store = S3CommonMaterialPrivateObjectStore(_config(), client=client)
            firm_id, matter_id = str(uuid4()), str(uuid4())
            stored = store.put_immutable_common_material(
                admitted,
                firm_id=firm_id,
                matter_id=matter_id,
            )
            self.assertEqual(
                stored.object_key,
                f"original-images/v1/{firm_id}/{matter_id}/"
                f"{admitted.content_sha256[:2]}/{admitted.content_sha256}/"
                f"{admitted.material_object_id}.png",
            )
            metadata = client.last_put["Metadata"]
            self.assertEqual(metadata["lawcase-source-sha256"], admitted.content_sha256)
            locator = SimpleNamespace(
                firm_id=firm_id,
                matter_id=matter_id,
                evidence_file_id=admitted.material_object_id,
                content_sha256=admitted.content_sha256,
                byte_size=admitted.byte_size,
                media_type=admitted.media_type,
                source_reference_hash=sha256(stored.object_key.encode()).hexdigest(),
                object_key=stored.object_key,
                object_version_id=stored.object_version_id,
            )
            existing_0038_store = S3CompatiblePrivateObjectStore(_config(), client=client)
            self.assertEqual(existing_0038_store.read_verified_native_image(locator), content)


if __name__ == "__main__":
    unittest.main()
