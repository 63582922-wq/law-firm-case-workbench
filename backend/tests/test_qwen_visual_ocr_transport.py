from __future__ import annotations

import base64
from io import BytesIO
import json
import unittest
from uuid import uuid4

from PIL import Image

from case_kernel.qwen_visual_ocr_adapter import (
    QwenVisualOcrRequest,
    RecoveredVisualOcrStatus,
)
from case_kernel.qwen_visual_ocr_transport import (
    PinnedQwenVisualOcrHttpsBroker,
    QwenVisualOcrCredentials,
    QwenVisualOcrKnownFailure,
    QwenVisualOcrNetworkBlocked,
    QwenVisualOcrRecoverableExchange,
    QwenVisualOcrTransportRequest,
    QwenVisualOcrTransportFailure,
    QwenVisualOcrTransportResult,
)
from hashlib import sha256


def candidate() -> str:
    return json.dumps(
        {
            "schema_version": "visual-page-understanding-v1",
            "request_hash": "a" * 64,
            "matter_id": str(uuid4()),
            "evidence_page_id": str(uuid4()),
            "source_file_sha256": "b" * 64,
            "source_page_sha256": "c" * 64,
            "rendered_page_sha256": "d" * 64,
            "projection_hash": "e" * 64,
            "provider_id": "qwen", "model_id": "qwen3.5-ocr",
            "text_blocks": [], "tables": [], "fields": [], "quality_risks": [],
        },
        sort_keys=True, separators=(",", ":"),
    )


class Egress:
    def __init__(self):
        self.calls = 0
        self.last = None

    def send(self, *, request):
        self.calls += 1
        self.last = request
        return transport_result(request.external_request_id, request.request_hash)


class Recovery:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def lookup(self, **kwargs):
        self.calls += 1
        return self.result


def response(*, provider_request_id="chatcmpl-provider-1") -> bytes:
    return json.dumps(
        {
            "id": provider_request_id,
            "model": "qwen3.5-ocr",
            "choices": [{"message": {"content": candidate()}}],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()


def transport_result(external, request_hash):
    return QwenVisualOcrTransportResult(
        external_request_id=external,
        request_hash=request_hash,
        provider_request_id="chatcmpl-provider-1",
        response_body=response(),
    )


def request():
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, "PNG")
    image = output.getvalue()
    rendered_hash = sha256(image).hexdigest()
    projection_hash = "e" * 64
    body = json.dumps(
        {
            "model": "qwen3.5-ocr",
            "stream": False,
            "max_tokens": 16_384,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                            "min_pixels": 3_072,
                            "max_pixels": 30_720_000,
                        },
                        {
                            "type": "text",
                            "text": (
                                "这是中国律师案件材料的单页OCR。"
                                "只返回页面中可见文字，不要返回JSON。"
                            ),
                        },
                    ],
                }
            ],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return QwenVisualOcrRequest(
        external_request_id=str(uuid4()), request_hash=sha256(body).hexdigest(),
        provider_id="qwen", service_id="qwen-visual-ocr",
        model_id="qwen3.5-ocr", processor_region="cn-beijing",
        endpoint_host="ws-legal.cn-beijing.maas.aliyuncs.com",
        projection_hash=projection_hash,
        rendered_page_sha256=rendered_hash,
        body=body,
    )


class FakeSocket:
    def __init__(
        self,
        response_bytes,
        *,
        peer="8.8.8.8",
        send_error=None,
        read_error=None,
    ):
        self.buffer = bytearray(response_bytes)
        self.peer = peer
        self.sent = b""
        self.send_error = send_error
        self.read_error = read_error

    def getpeername(self): return (self.peer, 443)
    def sendall(self, content):
        if self.send_error is not None:
            raise self.send_error
        self.sent += content

    def makefile(self, *_args, **_kwargs):
        sock = self

        class File:
            def readline(self, limit=-1):
                if not sock.buffer: return b""
                index = sock.buffer.find(b"\n") + 1
                if index <= 0: index = len(sock.buffer)
                value = bytes(sock.buffer[:index])
                del sock.buffer[:index]
                return value

            def read(self, amount=-1):
                if sock.read_error is not None:
                    raise sock.read_error
                if amount < 0: amount = len(sock.buffer)
                value = bytes(sock.buffer[:amount])
                del sock.buffer[:amount]
                return value

            def close(self): pass

        return File()

    def close(self): pass


def transport_request(value=None):
    value = value or request()
    return QwenVisualOcrTransportRequest(
        external_request_id=value.external_request_id,
        endpoint=(
            f"https://{value.endpoint_host}/compatible-mode/v1/chat/completions"
        ),
        endpoint_host=value.endpoint_host,
        method="POST",
        headers={
            "Authorization": "Bearer " + "k" * 40,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        body=value.body,
        request_hash=value.request_hash,
        projection_hash=value.projection_hash,
        rendered_page_sha256=value.rendered_page_sha256,
        timeout_seconds=45.0,
        max_response_bytes=2 * 1024 * 1024,
    )


class QwenVisualOcrTransportTests(unittest.TestCase):
    def exchange(self, egress=None, recovery=None):
        return QwenVisualOcrRecoverableExchange(
            credentials=QwenVisualOcrCredentials("k" * 40),
            egress=egress or Egress(), recovery=recovery or Recovery(),
        )

    def test_server_credential_is_redacted_and_request_uses_no_public_image_url(self):
        credentials = QwenVisualOcrCredentials("secret" * 8)
        self.assertNotIn("secret", repr(credentials))
        egress = Egress()
        original = request()
        result = self.exchange(egress=egress).send(request=original)
        self.assertEqual(result.external_request_id, original.external_request_id)
        self.assertEqual(
            json.loads(result.response_body)["schema_version"],
            "visual-page-understanding-v1",
        )
        self.assertEqual(egress.last.endpoint_host, original.endpoint_host)
        self.assertEqual(egress.last.timeout_seconds, 120.0)
        self.assertNotIn("api_key", egress.last.body.decode())

    def test_lookup_recovery_never_calls_egress(self):
        original = request()
        egress = Egress()
        recovery = Recovery(
            transport_result(original.external_request_id, original.request_hash)
        )
        recovered = self.exchange(egress=egress, recovery=recovery).recover(
            external_request_id=original.external_request_id,
            request_hash=original.request_hash,
        )
        self.assertEqual(recovered.status, RecoveredVisualOcrStatus.SUCCEEDED)
        self.assertEqual(egress.calls, 0)
        self.assertEqual(recovery.calls, 1)

    def test_unknown_lookup_remains_unresolved_and_never_resubmits(self):
        original = request()
        egress, recovery = Egress(), Recovery(None)
        result = self.exchange(egress=egress, recovery=recovery).recover(
            external_request_id=original.external_request_id,
            request_hash=original.request_hash,
        )
        self.assertEqual(result.status, RecoveredVisualOcrStatus.UNRESOLVED)
        self.assertEqual(egress.calls, 0)

    def test_known_failure_lookup_is_terminal_and_never_resubmits(self):
        original = request()
        failure = QwenVisualOcrTransportFailure(
            external_request_id=original.external_request_id,
            request_hash=original.request_hash,
            error_code="QWEN_VISUAL_OCR_HTTP_400",
        )
        egress, recovery = Egress(), Recovery(failure)
        result = self.exchange(egress=egress, recovery=recovery).recover(
            external_request_id=original.external_request_id,
            request_hash=original.request_hash,
        )
        self.assertEqual(result.status, RecoveredVisualOcrStatus.FAILED)
        self.assertEqual(result.error_code, "QWEN_VISUAL_OCR_HTTP_400")
        self.assertEqual(egress.calls, 0)

    def test_recovered_result_misbinding_is_rejected(self):
        original = request()
        recovery = Recovery(transport_result(str(uuid4()), original.request_hash))
        with self.assertRaisesRegex(Exception, "differs"):
            self.exchange(recovery=recovery).recover(
                external_request_id=original.external_request_id,
                request_hash=original.request_hash,
            )

    def test_provider_response_id_must_match_transport_receipt(self):
        original = request()
        mismatched = QwenVisualOcrTransportResult(
            external_request_id=original.external_request_id,
            request_hash=original.request_hash,
            provider_request_id="chatcmpl-provider-1",
            response_body=response(provider_request_id="chatcmpl-other"),
        )
        with self.assertRaisesRegex(Exception, "response id differs"):
            self.exchange(recovery=Recovery(mismatched)).recover(
                external_request_id=original.external_request_id,
                request_hash=original.request_hash,
            )

    def test_pinned_https_broker_sends_exactly_one_post_to_global_peer(self):
        body = response()
        http_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        socket = FakeSocket(http_response)
        broker = PinnedQwenVisualOcrHttpsBroker(
            resolver=lambda *_args, **_kwargs: [
                (None, None, None, None, ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: socket,
        )
        result = broker.send(request=transport_request())
        self.assertEqual(result.provider_request_id, "chatcmpl-provider-1")
        self.assertTrue(
            socket.sent.startswith(b"POST /compatible-mode/v1/chat/completions ")
        )
        self.assertEqual(socket.sent.count(b"POST "), 1)

    def test_pinned_https_broker_blocks_private_dns_and_marks_redirect_terminal(self):
        broker = PinnedQwenVisualOcrHttpsBroker(
            resolver=lambda *_args, **_kwargs: [
                (None, None, None, None, ("127.0.0.1", 443))
            ],
            connection_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("socket must not open")
            ),
        )
        with self.assertRaises(QwenVisualOcrNetworkBlocked) as raised:
            broker.send(request=transport_request())
        self.assertEqual(
            raised.exception.error_code,
            "QWEN_VISUAL_OCR_UNKNOWN_DNS",
        )

        redirect = FakeSocket(
            b"HTTP/1.1 302 Found\r\nLocation: https://evil.example\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        broker = PinnedQwenVisualOcrHttpsBroker(
            resolver=lambda *_args, **_kwargs: [
                (None, None, None, None, ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: redirect,
        )
        value = transport_request()
        with self.assertRaises(QwenVisualOcrKnownFailure) as raised:
            broker.send(request=value)
        self.assertEqual(raised.exception.external_request_id, value.external_request_id)
        self.assertEqual(raised.exception.error_code, "QWEN_VISUAL_OCR_HTTP_302")

    def test_complete_http_rejection_is_known_not_indeterminate(self):
        rejected = FakeSocket(
            b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        broker = PinnedQwenVisualOcrHttpsBroker(
            resolver=lambda *_args, **_kwargs: [
                (None, None, None, None, ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: rejected,
        )
        with self.assertRaises(QwenVisualOcrKnownFailure) as raised:
            broker.send(request=transport_request())
        self.assertEqual(raised.exception.error_code, "QWEN_VISUAL_OCR_HTTP_400")

    def test_pinned_https_broker_records_controlled_unknown_network_stage(self):
        cases = (
            (
                "connect",
                lambda: PinnedQwenVisualOcrHttpsBroker(
                    resolver=lambda *_args, **_kwargs: [
                        (None, None, None, None, ("8.8.8.8", 443))
                    ],
                    connection_factory=lambda *_args: (_ for _ in ()).throw(
                        OSError("connect detail must not escape")
                    ),
                ),
                "QWEN_VISUAL_OCR_UNKNOWN_CONNECT",
            ),
            (
                "send",
                lambda: PinnedQwenVisualOcrHttpsBroker(
                    resolver=lambda *_args, **_kwargs: [
                        (None, None, None, None, ("8.8.8.8", 443))
                    ],
                    connection_factory=lambda *_args: FakeSocket(
                        b"", send_error=OSError("send detail must not escape")
                    ),
                ),
                "QWEN_VISUAL_OCR_UNKNOWN_SEND",
            ),
            (
                "response-head",
                lambda: PinnedQwenVisualOcrHttpsBroker(
                    resolver=lambda *_args, **_kwargs: [
                        (None, None, None, None, ("8.8.8.8", 443))
                    ],
                    connection_factory=lambda *_args: FakeSocket(b""),
                ),
                "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
            ),
            (
                "response-body",
                lambda: PinnedQwenVisualOcrHttpsBroker(
                    resolver=lambda *_args, **_kwargs: [
                        (None, None, None, None, ("8.8.8.8", 443))
                    ],
                    connection_factory=lambda *_args: FakeSocket(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 20\r\n\r\n",
                        read_error=OSError("body detail must not escape"),
                    ),
                ),
                "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY",
            ),
        )
        for label, broker_factory, expected in cases:
            with self.subTest(stage=label):
                value = transport_request()
                with self.assertRaises(QwenVisualOcrNetworkBlocked) as raised:
                    broker_factory().send(request=value)
                self.assertEqual(raised.exception.error_code, expected)
                self.assertNotIn("detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
