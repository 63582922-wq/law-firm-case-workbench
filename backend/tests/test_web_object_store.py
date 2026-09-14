from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from hashlib import sha256

from reportlab.pdfgen import canvas

from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.web_object_store import (
    S3CompatiblePrivateObjectStore,
    S3PrivateObjectStoreConfig,
    StoredCaseAgentMaterial,
    WebObjectStoreBlocked,
)
from case_kernel.web_upload_staging import WebUploadStagingArea


class CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        return FileSafetyScanReceipt("test scanner", "definitions-1", expected_sha256, "CLEAN")


class FakeS3:
    def __init__(self, *, corrupt_checksum: bool = False) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.corrupt_checksum = corrupt_checksum
        self.last_head: dict[str, object] | None = None

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        content = body.read()
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "content": content,
            "ContentLength": len(content),
            "ChecksumSHA256": "incorrect" if self.corrupt_checksum else kwargs["ChecksumSHA256"],
            "Metadata": dict(kwargs["Metadata"]),
            "ContentType": kwargs["ContentType"],
            "ServerSideEncryption": kwargs["ServerSideEncryption"],
        }
        return {"VersionId": "version-1"}

    def head_object(self, **kwargs):
        self.last_head = dict(kwargs)
        return self.objects[(kwargs["Bucket"], kwargs["Key"])]

    def get_object(self, **kwargs):
        item = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": BytesIO(item["content"])}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}


class _S3LookupError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}
        super().__init__("controlled fake S3 lookup error")


class LookupFailingS3(FakeS3):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def head_object(self, **kwargs):
        raise _S3LookupError(self.code)


def _pdf_bytes(root: Path) -> bytes:
    path = root / "source.pdf"
    document = canvas.Canvas(str(path))
    document.drawString(36, 720, "synthetic evidence")
    document.save()
    return path.read_bytes()


class WebObjectStoreTests(unittest.TestCase):
    def _config(self) -> S3PrivateObjectStoreConfig:
        return S3PrivateObjectStoreConfig(
            endpoint_url="http://object-storage:9000",
            region_name="us-east-1",
            bucket="lawcase-private",
            access_key_id="lawcase-app-user",
            secret_access_key="x" * 32,
            allow_insecure_internal_endpoint=True,
        )

    def _admitted(self, root: Path):
        staging = WebUploadStagingArea(root / "staging")
        staged = staging.stage_stream(BytesIO(_pdf_bytes(root)), client_filename="微信记录.pdf")
        return staging, staging.inspect_pdf(staged, scanner=CleanScanner())

    def test_puts_admitted_pdf_under_opaque_private_key_and_verifies_checksum(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, admitted = self._admitted(root)
            client = FakeS3()
            stored = S3CompatiblePrivateObjectStore(self._config(), client=client).put_verified_pdf(
                admitted,
                firm_id=str(uuid4()),
                matter_id=str(uuid4()),
            )
            self.assertEqual(stored.content_sha256, admitted.content_sha256)
            self.assertEqual(stored.byte_size, admitted.byte_size)
            self.assertTrue(stored.object_key.endswith(".pdf"))
            self.assertNotIn(admitted.display_name, stored.object_key)
            self.assertNotIn(str(admitted.path), repr(stored))
            stored_remote = client.objects[(self._config().bucket, stored.object_key)]
            self.assertEqual(stored_remote["ContentType"], "application/pdf")
            self.assertEqual(stored_remote["ServerSideEncryption"], "AES256")
            self.assertEqual(client.last_head["ChecksumMode"], "ENABLED")
            staging.discard(admitted)

    def test_object_checksum_mismatch_is_deleted_and_never_returned(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, admitted = self._admitted(root)
            client = FakeS3(corrupt_checksum=True)
            store = S3CompatiblePrivateObjectStore(self._config(), client=client)
            with self.assertRaisesRegex(WebObjectStoreBlocked, "checksum"):
                store.put_verified_pdf(admitted, firm_id=str(uuid4()), matter_id=str(uuid4()))
            self.assertEqual(client.objects, {})
            staging.discard(admitted)

    def test_explicit_cleanup_only_accepts_a_store_created_opaque_handle(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, admitted = self._admitted(root)
            client = FakeS3()
            store = S3CompatiblePrivateObjectStore(self._config(), client=client)
            stored = store.put_verified_pdf(admitted, firm_id=str(uuid4()), matter_id=str(uuid4()))
            store.delete_unbound_upload_object(stored)
            self.assertEqual(client.objects, {})
            staging.discard(admitted)

    def test_materializes_verified_object_only_into_private_worker_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, admitted = self._admitted(root)
            client = FakeS3()
            store = S3CompatiblePrivateObjectStore(self._config(), client=client)
            stored = store.put_verified_pdf(admitted, firm_id=str(uuid4()), matter_id=str(uuid4()))
            worker = root / "worker"
            worker.mkdir(mode=0o700)
            worker.chmod(0o700)
            materialized = store.materialize_verified_pdf(stored, destination=worker / "source.pdf")
            self.assertEqual(materialized.read_bytes(), admitted.path.read_bytes())
            self.assertNotIn(str(worker), repr(stored))
            staging.discard(admitted)

    def test_reads_server_bound_native_image_with_tenant_and_hash_rechecks(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id, evidence_file_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        content = b"\x89PNG\r\n\x1a\nserver-bound-image"
        digest = sha256(content).hexdigest()
        key = (
            f"original-images/v1/{firm_id}/{matter_id}/{digest[:2]}/"
            f"{digest}/{uuid4()}.png"
        )
        checksum = base64.b64encode(bytes.fromhex(digest)).decode()
        client.objects[(self._config().bucket, key)] = {
            "content": content,
            "ContentLength": len(content),
            "ChecksumSHA256": checksum,
            "ContentType": "image/png",
            "Metadata": {
                "lawcase-source-sha256": digest,
                "lawcase-source-bytes": str(len(content)),
                "lawcase-source-media-type": "image/png",
            },
        }
        locator = type("Locator", (), {
            "firm_id": firm_id, "matter_id": matter_id,
            "evidence_file_id": evidence_file_id, "content_sha256": digest,
            "byte_size": len(content), "media_type": "image/png",
            "object_version_id": None,
            "source_reference_hash": sha256(key.encode()).hexdigest(),
            "object_key": key,
        })()
        self.assertEqual(store.read_verified_native_image(locator), content)
        locator.firm_id = str(uuid4())
        with self.assertRaisesRegex(WebObjectStoreBlocked, "locator"):
            store.read_verified_native_image(locator)

    def test_stores_canonical_case_agent_candidate_without_public_locator(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id, artifact_id = str(uuid4()), str(uuid4()), str(uuid4())
        content = b'{"review_status":"NEEDS_LAWYER_REVIEW","schema_version":"candidate-v1"}'
        from hashlib import sha256

        content_hash = sha256(content).hexdigest()
        stored = store.put_case_agent_review_candidate(
            content,
            firm_id=firm_id,
            matter_id=matter_id,
            artifact_id=artifact_id,
            content_sha256=content_hash,
        )
        self.assertNotIn(stored.object_key, repr(stored))
        self.assertTrue(stored.object_key.startswith("case-agent-candidates/v1/"))
        store.verify_case_agent_review_candidate(stored, artifact_id=artifact_id)
        self.assertEqual(
            store.read_case_agent_review_candidate(stored, artifact_id=artifact_id),
            content,
        )

    def test_rejects_noncanonical_or_duplicate_candidate_json(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        from hashlib import sha256

        for content in (b'{"b":2, "a":1}', b'{"a":1,"a":2}'):
            with self.subTest(content=content):
                with self.assertRaisesRegex(WebObjectStoreBlocked, "canonical|invalid"):
                    store.put_case_agent_review_candidate(
                        content,
                        firm_id=str(uuid4()),
                        matter_id=str(uuid4()),
                        artifact_id=str(uuid4()),
                        content_sha256=sha256(content).hexdigest(),
                    )

    def test_archives_and_recovers_exact_case_agent_research_response(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
        from hashlib import sha256

        raw = b'{"type":"search","web":{"results":[]}}'
        request_hash = sha256(b"exact request").hexdigest()
        receipt = {
            "schema_version": "case-agent-public-search-egress-receipt-v1",
            "request_id": request_id,
            "response_sha256": sha256(raw).hexdigest(),
        }
        stored = store.put_case_agent_research_response(
            raw,
            receipt_payload=receipt,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
            request_hash=request_hash,
        )
        self.assertNotIn(stored.object_key, repr(stored))
        body, restored_receipt = store.read_case_agent_research_response(
            stored,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
        )
        self.assertEqual(body, raw)
        self.assertEqual(restored_receipt, receipt)
        recovered, recovered_body, recovered_receipt = (
            store.recover_case_agent_research_response(
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=request_id,
                request_hash=request_hash,
            )
        )
        self.assertEqual(recovered.object_key, stored.object_key)
        self.assertEqual(recovered.request_hash, stored.request_hash)
        self.assertEqual(recovered.response_sha256, stored.response_sha256)
        self.assertEqual(recovered.archive_sha256, stored.archive_sha256)
        self.assertEqual(recovered_body, raw)
        self.assertEqual(recovered_receipt, receipt)

    def test_archives_and_lookup_only_recovers_lawyer_analysis_response(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
        raw = b'{"choices":[],"id":"chatcmpl-controlled","model":"qwen3.7-plus"}'
        request_hash = sha256(b"strict lawyer analysis request").hexdigest()
        receipt = {
            "schema_version": "case-agent-lawyer-analysis-transport-receipt-v1",
            "external_request_id": request_id,
            "request_hash": request_hash,
            "response_sha256": sha256(raw).hexdigest(),
            "provider_response_id_hash": sha256(b"chatcmpl-controlled").hexdigest(),
        }
        stored = store.put_case_agent_lawyer_analysis_response(
            raw,
            receipt_payload=receipt,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
            request_hash=request_hash,
        )
        self.assertNotIn(stored.object_key, repr(stored))
        self.assertTrue(
            stored.object_key.startswith("case-agent-lawyer-analysis/v1/")
        )
        body, restored_receipt = store.read_case_agent_lawyer_analysis_response(
            stored,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
        )
        self.assertEqual(body, raw)
        self.assertEqual(restored_receipt, receipt)
        recovered = store.recover_case_agent_lawyer_analysis_response(
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
            request_hash=request_hash,
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        recovered_stored, recovered_body, recovered_receipt = recovered
        self.assertEqual(recovered_stored.archive_sha256, stored.archive_sha256)
        self.assertEqual(recovered_body, raw)
        self.assertEqual(recovered_receipt, receipt)

    def test_lawyer_analysis_recovery_distinguishes_absent_from_denied(self) -> None:
        identifiers = {
            "firm_id": str(uuid4()),
            "matter_id": str(uuid4()),
            "external_request_id": str(uuid4()),
            "request_hash": sha256(b"lookup-only").hexdigest(),
        }
        absent = S3CompatiblePrivateObjectStore(
            self._config(), client=LookupFailingS3("NoSuchKey")
        )
        self.assertIsNone(
            absent.recover_case_agent_lawyer_analysis_response(**identifiers)
        )
        denied = S3CompatiblePrivateObjectStore(
            self._config(), client=LookupFailingS3("AccessDenied")
        )
        with self.assertRaisesRegex(WebObjectStoreBlocked, "failed closed"):
            denied.recover_case_agent_lawyer_analysis_response(**identifiers)

    def test_lawyer_analysis_archive_rejects_corrupt_remote_metadata(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id, request_id = str(uuid4()), str(uuid4()), str(uuid4())
        raw = b'{"id":"chatcmpl-controlled","model":"qwen3.7-plus"}'
        request_hash = sha256(b"bound request").hexdigest()
        stored = store.put_case_agent_lawyer_analysis_response(
            raw,
            receipt_payload={"schema_version": "transport-v1"},
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
            request_hash=request_hash,
        )
        client.objects[(self._config().bucket, stored.object_key)]["Metadata"][
            "lawcase-lawyer-request-hash"
        ] = sha256(b"different request").hexdigest()
        with self.assertRaisesRegex(WebObjectStoreBlocked, "metadata differs"):
            store.read_case_agent_lawyer_analysis_response(
                stored,
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=request_id,
            )

    def test_materializes_hash_bound_case_agent_office_source(self) -> None:
        client = FakeS3()
        store = S3CompatiblePrivateObjectStore(self._config(), client=client)
        firm_id, matter_id = str(uuid4()), str(uuid4())
        content = b"PK\x03\x04synthetic-docx"
        from hashlib import sha256
        import base64

        content_hash = sha256(content).hexdigest()
        object_key = (
            f"case-materials/v1/{firm_id}/{matter_id}/"
            f"{content_hash[:2]}/{content_hash}"
        )
        client.objects[(self._config().bucket, object_key)] = {
            "content": content,
            "ContentLength": len(content),
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(content_hash)).decode("ascii"),
            "Metadata": {
                "lawcase-material-sha256": content_hash,
                "lawcase-material-bytes": str(len(content)),
                "lawcase-material-media-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "lawcase-material-format": "DOCX",
            },
        }
        stored = StoredCaseAgentMaterial(
            object_key=object_key,
            content_sha256=content_hash,
            byte_size=len(content),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            admitted_format="DOCX",
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worker = root / "worker"
            worker.mkdir(mode=0o700)
            worker.chmod(0o700)
            path = store.materialize_case_agent_material(
                stored, destination=worker / "source.docx"
            )
            self.assertEqual(path.read_bytes(), content)

    def test_materialization_refuses_nonprivate_destination_and_corrupt_download(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging, admitted = self._admitted(root)
            client = FakeS3()
            store = S3CompatiblePrivateObjectStore(self._config(), client=client)
            stored = store.put_verified_pdf(admitted, firm_id=str(uuid4()), matter_id=str(uuid4()))
            public_worker = root / "public-worker"
            public_worker.mkdir(mode=0o755)
            with self.assertRaisesRegex(WebObjectStoreBlocked, "must be private"):
                store.materialize_verified_pdf(stored, destination=public_worker / "source.pdf")
            client.objects[(self._config().bucket, stored.object_key)]["content"] = b"corrupt"
            private_worker = root / "worker"
            private_worker.mkdir(mode=0o700)
            private_worker.chmod(0o700)
            target = private_worker / "source.pdf"
            with self.assertRaisesRegex(WebObjectStoreBlocked, "bytes differ"):
                store.materialize_verified_pdf(stored, destination=target)
            self.assertFalse(target.exists())
            staging.discard(admitted)

    def test_external_plain_http_endpoint_is_refused_without_explicit_internal_acknowledgement(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            S3PrivateObjectStoreConfig(
                endpoint_url="http://storage.example.test",
                region_name="us-east-1",
                bucket="lawcase-private",
                access_key_id="lawcase-app-user",
                secret_access_key="x" * 32,
            )


if __name__ == "__main__":
    unittest.main()
