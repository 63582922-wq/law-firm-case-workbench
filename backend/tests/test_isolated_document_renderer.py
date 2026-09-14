from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
import unittest

from case_kernel.isolated_document_renderer import (
    DOCX_MEDIA_TYPE,
    IsolatedDocumentRendererBlocked,
    IsolatedDocumentRendererClient,
    IsolatedDocumentRendererClientSettings,
    IsolatedDocumentRendererUnknown,
    RENDER_HEALTH_BODY,
    RENDER_PATH,
    RENDER_PROTOCOL_VERSION,
    RendererHttpResponse,
    sign_renderer_health,
    sign_render_response,
)


SECRET = b"renderer-test-secret-32-bytes-minimum-value"
CONTENT = b"generated-docx-bytes"
SOURCE_HASH = sha256(CONTENT).hexdigest()
PDF = b"%PDF-1.7\nreview-preview\n%%EOF\n"
PDF_HASH = sha256(PDF).hexdigest()
TRANSFORM_HASH = "a" * 64
RENDER_HASH = "b" * 64
NONCE = "n" * 43


class _ResponseTransport:
    def __init__(self, *, tamper_body: bool = False) -> None:
        self.calls = []
        self.tamper_body = tamper_body

    def post_once(self, **kwargs):
        self.calls.append(kwargs)
        headers = kwargs["headers"]
        signature = sign_render_response(
            secret=SECRET,
            request_nonce=headers["X-Lawcase-Render-Nonce"],
            source_sha256=SOURCE_HASH,
            detected_kind="WORD_DOCUMENT",
            converter_id="libreoffice-web-worker",
            converter_version="LibreOffice 25.2.1",
            transform_hash=TRANSFORM_HASH,
            pdf_sha256=PDF_HASH,
            pdf_bytes=len(PDF),
            page_count=1,
            render_verification_hash=RENDER_HASH,
        )
        response_headers = {
            "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
            "X-Lawcase-Source-SHA256": SOURCE_HASH,
            "X-Lawcase-Detected-Kind": "WORD_DOCUMENT",
            "X-Lawcase-Converter-Id": "libreoffice-web-worker",
            "X-Lawcase-Converter-Version": "LibreOffice 25.2.1",
            "X-Lawcase-Transform-SHA256": TRANSFORM_HASH,
            "X-Lawcase-PDF-SHA256": PDF_HASH,
            "X-Lawcase-PDF-Bytes": str(len(PDF)),
            "X-Lawcase-PDF-Page-Count": "1",
            "X-Lawcase-Render-Verification-SHA256": RENDER_HASH,
            "X-Lawcase-Response-Signature": signature,
        }
        return RendererHttpResponse(
            status_code=200,
            headers=response_headers,
            body=PDF + b"tampered" if self.tamper_body else PDF,
        )


class _TimeoutTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post_once(self, **_):
        self.calls += 1
        raise TimeoutError("response lost")


class _HealthTransport(_ResponseTransport):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.health_calls = 0
        self.fail = fail

    def get_once(self, **kwargs):
        self.health_calls += 1
        if self.fail:
            raise TimeoutError("health timeout")
        return RendererHttpResponse(
            status_code=200,
            headers={
                "Content-Type": "application/json",
                "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
                "X-Lawcase-Health-Signature": sign_renderer_health(secret=SECRET),
            },
            body=RENDER_HEALTH_BODY,
        )


class IsolatedDocumentRendererClientTests(unittest.TestCase):
    def _settings(self):
        return IsolatedDocumentRendererClientSettings(
            endpoint="http://document-renderer:8090",
            shared_secret=SECRET,
            timeout_seconds=90,
        )

    def test_client_implements_byte_only_review_converter_and_verifies_response(self):
        transport = _ResponseTransport()
        client = IsolatedDocumentRendererClient(
            settings=self._settings(),
            transport=transport,
            clock=lambda: 1_800_000_000,
            nonce_factory=lambda: NONCE,
        )

        converted = client.convert_generated_document(
            CONTENT,
            content_sha256=SOURCE_HASH,
            source_name="approved-draft.docx",
            detected_kind="WORD_DOCUMENT",
        )

        self.assertEqual(converted.pdf_content, PDF)
        self.assertEqual(converted.source_sha256, SOURCE_HASH)
        self.assertEqual(converted.render_verification_hash, RENDER_HASH)
        self.assertEqual(len(transport.calls), 1)
        request = transport.calls[0]
        self.assertEqual(
            request["endpoint"],
            "http://document-renderer:8090" + RENDER_PATH,
        )
        self.assertEqual(request["body"], CONTENT)
        self.assertEqual(request["headers"]["Content-Type"], DOCX_MEDIA_TYPE)
        self.assertTrue(request["headers"]["Authorization"].startswith("HMAC-SHA256 "))
        self.assertNotIn("path", request["headers"])
        self.assertNotIn("url", request["headers"])
        self.assertNotIn("command", request["headers"])

    def test_timeout_is_unknown_and_client_never_retries(self):
        transport = _TimeoutTransport()
        client = IsolatedDocumentRendererClient(
            settings=self._settings(), transport=transport, nonce_factory=lambda: NONCE
        )
        with self.assertRaisesRegex(IsolatedDocumentRendererUnknown, "not retransmitted"):
            client.convert_generated_document(
                CONTENT,
                content_sha256=SOURCE_HASH,
                source_name="approved-draft.docx",
                detected_kind="WORD_DOCUMENT",
            )
        self.assertEqual(transport.calls, 1)

    def test_startup_preflight_is_one_authenticated_health_request_without_retry(self):
        transport = _HealthTransport()
        client = IsolatedDocumentRendererClient(
            settings=self._settings(), transport=transport
        )
        client.preflight()
        self.assertEqual(transport.health_calls, 1)

        failed = _HealthTransport(fail=True)
        client = IsolatedDocumentRendererClient(
            settings=self._settings(), transport=failed
        )
        with self.assertRaisesRegex(IsolatedDocumentRendererBlocked, "health preflight"):
            client.preflight()
        self.assertEqual(failed.health_calls, 1)

    def test_unverifiable_or_tampered_success_is_unknown_not_a_retry(self):
        transport = _ResponseTransport(tamper_body=True)
        client = IsolatedDocumentRendererClient(
            settings=self._settings(),
            transport=transport,
            nonce_factory=lambda: NONCE,
        )
        with self.assertRaises(IsolatedDocumentRendererUnknown):
            client.convert_generated_document(
                CONTENT,
                content_sha256=SOURCE_HASH,
                source_name="approved-draft.docx",
                detected_kind="WORD_DOCUMENT",
            )
        self.assertEqual(len(transport.calls), 1)

    def test_only_fixed_docx_xlsx_names_and_hash_bound_bytes_are_allowed(self):
        client = IsolatedDocumentRendererClient(
            settings=self._settings(),
            transport=_ResponseTransport(),
            nonce_factory=lambda: NONCE,
        )
        for changes in (
            {"source_name": "../../case.docx"},
            {"detected_kind": "PRESENTATION"},
            {"content_sha256": "0" * 64},
        ):
            values = {
                "content_sha256": SOURCE_HASH,
                "source_name": "approved-draft.docx",
                "detected_kind": "WORD_DOCUMENT",
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(IsolatedDocumentRendererBlocked):
                client.convert_generated_document(CONTENT, **values)

    def test_endpoint_is_one_fixed_internal_origin(self):
        for endpoint in (
            "https://document-renderer:8090",
            "http://user@document-renderer:8090",
            "http://document-renderer:8090/arbitrary",
            "http://document-renderer:8090?url=https://example.com",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(IsolatedDocumentRendererBlocked):
                IsolatedDocumentRendererClientSettings(
                    endpoint=endpoint,
                    shared_secret=SECRET,
                )

    def test_worker_environment_contract_decodes_secret_without_exposing_it_in_repr(self):
        encoded = urlsafe_b64encode(SECRET).decode().rstrip("=")
        settings = IsolatedDocumentRendererClientSettings.from_worker_environment(
            {
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT": "http://document-renderer:8090",
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET": encoded,
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
            }
        )
        self.assertEqual(settings.shared_secret, SECRET)
        self.assertEqual(settings.health_endpoint, "http://document-renderer:8090/healthz")
        self.assertNotIn(encoded, repr(settings))

        with self.assertRaisesRegex(
            IsolatedDocumentRendererBlocked, "between 10 and 180"
        ):
            IsolatedDocumentRendererClientSettings.from_worker_environment(
                {
                    "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT": (
                        "http://document-renderer:8090"
                    ),
                    "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET": encoded,
                    "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "181",
                }
            )


if __name__ == "__main__":
    unittest.main()
