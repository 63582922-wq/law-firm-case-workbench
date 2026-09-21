from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.official_source_capture import (
    DirectHttpsOfficialSourceTransport,
    OfficialHttpResponse,
    OfficialSourceCaptureBlocked,
    capture_authorized_official_source,
)
from case_kernel.research_gateway import PublicResearchGateway


class FakeTransport:
    def __init__(self, response: OfficialHttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, int]] = []

    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse:
        self.calls.append((url, max_bytes))
        return self.response


class OfficialSourceCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="official-source-capture-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case"
        self.case_root.mkdir()
        self.store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-source-key-v1",
            encryption_key=b"s" * 32,
        )
        self.gateway = PublicResearchGateway()
        plan = self.gateway.prepare_plan(
            issue="LPR",
            proposed_query="一年期贷款市场报价利率 历史数据",
        )
        self.url = "https://www.chinamoney.com.cn/r/cms/chinese/chinamoney/html/currency/lpr-shibor-history-download.html"
        self.request = self.gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id="CFETS-LPR-HISTORY",
            target_url=self.url,
            requested_by="synthetic_lead_lawyer",
            lawyer_confirmed=True,
        )
        self.body = b"<!doctype html><html><head><title>LPR</title></head><body>synthetic only</body></html>"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def response(self, **changes) -> OfficialHttpResponse:
        values = {
            "status_code": 200,
            "final_url": self.url,
            "media_type": "text/html",
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": self.body,
            "peer_ip": "8.8.8.8",
        }
        values.update(changes)
        return OfficialHttpResponse(**values)

    def test_capture_encrypts_exact_response_and_requires_human_review(self) -> None:
        transport = FakeTransport(self.response())
        captured = capture_authorized_official_source(
            request=self.request,
            gateway=self.gateway,
            artifact_store=self.store,
            case_root=str(self.case_root),
            transport=transport,
            now=self.request.authorized_at + timedelta(seconds=1),
        )
        expected_hash = sha256(self.body).hexdigest()
        self.assertEqual(captured.source_id, "CFETS-LPR-HISTORY")
        self.assertEqual(captured.content_sha256, expected_hash)
        self.assertEqual(captured.review_status, "HUMAN_REVIEW_REQUIRED")
        self.assertEqual(captured.response_receipt.response_sha256, expected_hash)
        self.assertEqual(
            self.store.read_bytes(captured.encrypted_object.object_key, expected_sha256=expected_hash),
            self.body,
        )
        encrypted = (self.store.managed_root / captured.encrypted_object.object_key).read_bytes()
        self.assertNotIn(b"synthetic only", encrypted)

    def test_unregistered_or_changed_authorization_is_blocked_before_network(self) -> None:
        transport = FakeTransport(self.response())
        forged = replace(self.request, request_id="external_research_forged")
        with self.assertRaisesRegex(OfficialSourceCaptureBlocked, "not registered"):
            capture_authorized_official_source(
                request=forged,
                gateway=self.gateway,
                artifact_store=self.store,
                case_root=str(self.case_root),
                transport=transport,
                now=self.request.authorized_at,
            )
        self.assertEqual(transport.calls, [])

    def test_expired_authorization_is_blocked_before_network(self) -> None:
        transport = FakeTransport(self.response())
        with self.assertRaisesRegex(OfficialSourceCaptureBlocked, "expired"):
            capture_authorized_official_source(
                request=self.request,
                gateway=self.gateway,
                artifact_store=self.store,
                case_root=str(self.case_root),
                transport=transport,
                now=self.request.authorized_at + timedelta(minutes=16),
            )
        self.assertEqual(transport.calls, [])

    def test_redirect_private_peer_and_unarchiveable_media_fail_closed(self) -> None:
        cases = (
            (self.response(final_url="https://chinamoney.com.cn/redirected"), "final URL differs"),
            (self.response(peer_ip="127.0.0.1"), "not globally routable"),
            (self.response(media_type="application/octet-stream"), "media type"),
        )
        for response, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(OfficialSourceCaptureBlocked, message):
                    capture_authorized_official_source(
                        request=self.request,
                        gateway=self.gateway,
                        artifact_store=self.store,
                        case_root=str(self.case_root),
                        transport=FakeTransport(response),
                        now=self.request.authorized_at + timedelta(seconds=1),
                    )

    def test_declared_html_must_have_html_signature(self) -> None:
        with self.assertRaisesRegex(OfficialSourceCaptureBlocked, "HTML signature"):
            capture_authorized_official_source(
                request=self.request,
                gateway=self.gateway,
                artifact_store=self.store,
                case_root=str(self.case_root),
                transport=FakeTransport(self.response(body=b"not html")),
                now=self.request.authorized_at + timedelta(seconds=1),
            )

    def test_transport_configuration_rejects_unbounded_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout"):
            DirectHttpsOfficialSourceTransport(timeout_seconds=120)


if __name__ == "__main__":
    unittest.main()
