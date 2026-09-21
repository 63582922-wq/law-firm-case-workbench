from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from case_api.isolated_document_renderer_app import (
    IsolatedDocumentRendererRuntime,
    IsolatedDocumentRendererServerSettings,
    IsolatedDocumentRendererStartupBlocked,
    ShortLivedNonceStore,
    create_isolated_document_renderer_app,
    preflight_renderer_executables,
)
from case_kernel.isolated_document_renderer import (
    DOCX_MEDIA_TYPE,
    RENDER_PATH,
    RENDER_PROTOCOL_VERSION,
    sign_render_request,
)
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf


SECRET = b"renderer-test-secret-32-bytes-minimum-value"
NOW = 1_800_000_000
NONCE = "a" * 43
CONTENT = b"generated-docx-bytes"
SOURCE_HASH = sha256(CONTENT).hexdigest()
PDF = b"%PDF-1.7\nreview-preview\n%%EOF\n"


class _Converter:
    def __init__(self) -> None:
        self.calls = []

    def convert_generated_document(self, content, **kwargs):
        self.calls.append((content, kwargs))
        return ConvertedOfficePdf(
            source_sha256=kwargs["content_sha256"],
            detected_kind=kwargs["detected_kind"],
            converter_id="libreoffice-web-worker",
            converter_version="LibreOffice 25.2.1",
            transform_hash="a" * 64,
            pdf_sha256=sha256(PDF).hexdigest(),
            pdf_bytes=len(PDF),
            page_count=1,
            render_verification_hash="b" * 64,
            pdf_content=PDF,
        )


class IsolatedDocumentRendererAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.converter = _Converter()
        self.health_calls = 0
        settings = IsolatedDocumentRendererServerSettings(
            shared_secret=SECRET,
            soffice_executable=Path("/opt/lawcase/soffice"),
            pdftoppm_executable=Path("/opt/lawcase/pdftoppm"),
            worker_root=Path("/var/lib/lawcase/document-renderer"),
        )

        def health():
            self.health_calls += 1

        runtime = IsolatedDocumentRendererRuntime(
            settings=settings,
            converter=self.converter,
            clock=lambda: NOW,
            nonce_store=ShortLivedNonceStore(
                capacity=1000, ttl_seconds=120, clock=lambda: NOW
            ),
            health_probe=health,
        )
        self.client = TestClient(create_isolated_document_renderer_app(runtime))

    def _headers(
        self,
        *,
        nonce: str = NONCE,
        timestamp: str = str(NOW),
        body: bytes = CONTENT,
        signed_body: bytes | None = None,
    ) -> dict[str, str]:
        bound = body if signed_body is None else signed_body
        bound_hash = sha256(bound).hexdigest()
        signature = sign_render_request(
            secret=SECRET,
            timestamp=timestamp,
            nonce=nonce,
            detected_kind="WORD_DOCUMENT",
            source_name="approved-draft.docx",
            content_sha256=bound_hash,
            content_length=len(body),
            media_type=DOCX_MEDIA_TYPE,
        )
        return {
            "Authorization": f"HMAC-SHA256 {signature}",
            "Content-Type": DOCX_MEDIA_TYPE,
            "Content-Length": str(len(body)),
            "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
            "X-Lawcase-Render-Timestamp": timestamp,
            "X-Lawcase-Render-Nonce": nonce,
            "X-Lawcase-Detected-Kind": "WORD_DOCUMENT",
            "X-Lawcase-Source-Name": "approved-draft.docx",
            "X-Lawcase-Source-SHA256": bound_hash,
        }

    def test_hmac_bound_request_returns_signed_hash_bound_pdf(self):
        response = self.client.post(RENDER_PATH, content=CONTENT, headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-lawcase-pdf-sha256"], sha256(PDF).hexdigest())
        self.assertEqual(response.headers["x-lawcase-pdf-page-count"], "1")
        self.assertEqual(len(response.headers["x-lawcase-response-signature"]), 64)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(len(self.converter.calls), 1)

    def test_same_authenticated_nonce_is_rejected_before_second_conversion(self):
        headers = self._headers()
        first = self.client.post(RENDER_PATH, content=CONTENT, headers=headers)
        second = self.client.post(RENDER_PATH, content=CONTENT, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.converter.calls), 1)

    def test_invalid_hmac_and_expired_timestamp_never_reach_converter(self):
        invalid = self._headers(nonce="b" * 43)
        invalid["Authorization"] = "HMAC-SHA256 " + "0" * 64
        response = self.client.post(RENDER_PATH, content=CONTENT, headers=invalid)
        self.assertEqual(response.status_code, 401)

        expired = self._headers(nonce="c" * 43, timestamp=str(NOW - 61))
        response = self.client.post(RENDER_PATH, content=CONTENT, headers=expired)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.converter.calls), 0)

    def test_body_hash_mismatch_is_known_rejection_after_authentication(self):
        other = b"x" * len(CONTENT)
        response = self.client.post(
            RENDER_PATH,
            content=other,
            headers=self._headers(nonce="d" * 43, body=other, signed_body=CONTENT),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.converter.calls), 0)

    def test_healthz_executes_runtime_preflight(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            response.headers["x-lawcase-render-protocol"], RENDER_PROTOCOL_VERSION
        )
        self.assertEqual(len(response.headers["x-lawcase-health-signature"]), 64)
        self.assertEqual(self.health_calls, 1)

    def test_environment_contract_requires_base64url_secret_and_absolute_paths(self):
        secret = urlsafe_b64encode(SECRET).decode().rstrip("=")
        settings = IsolatedDocumentRendererServerSettings.from_environment(
            {
                "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET": secret,
                "LAWCASE_DOCUMENT_RENDERER_SOFFICE_EXECUTABLE": "/usr/bin/soffice",
                "LAWCASE_DOCUMENT_RENDERER_PDFTOPPM_EXECUTABLE": "/usr/bin/pdftoppm",
                "LAWCASE_DOCUMENT_RENDERER_WORKER_ROOT": "/var/lib/lawcase/document-renderer",
                "LAWCASE_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                "LAWCASE_DOCUMENT_RENDERER_MAX_CLOCK_SKEW_SECONDS": "60",
                "LAWCASE_DOCUMENT_RENDERER_REPLAY_CAPACITY": "20000",
            }
        )
        self.assertEqual(settings.shared_secret, SECRET)
        with self.assertRaises(IsolatedDocumentRendererStartupBlocked):
            IsolatedDocumentRendererServerSettings.from_environment(
                {
                    "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET": "plaintext-secret",
                    "LAWCASE_DOCUMENT_RENDERER_SOFFICE_EXECUTABLE": "soffice",
                    "LAWCASE_DOCUMENT_RENDERER_PDFTOPPM_EXECUTABLE": "/usr/bin/pdftoppm",
                    "LAWCASE_DOCUMENT_RENDERER_WORKER_ROOT": "/tmp/renderer",
                }
            )

    def test_preflight_really_executes_soffice_and_pdftoppm(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            soffice = root / "soffice"
            pdftoppm = root / "pdftoppm"
            for executable, version in ((soffice, "LibreOffice test"), (pdftoppm, "pdftoppm test")):
                executable.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
                executable.chmod(0o700)
            preflight_renderer_executables(
                soffice_executable=soffice,
                pdftoppm_executable=pdftoppm,
            )


if __name__ == "__main__":
    unittest.main()
