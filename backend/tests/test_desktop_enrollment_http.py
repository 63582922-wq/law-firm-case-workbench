from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from case_api.desktop_enrollment_http import JsonFirmEnrollmentIssuer, PinnedHttpsJsonTransport
from case_api.desktop_enrollment_lifecycle import (
    DesktopEnrollmentLifecycleBlocked,
    EnrollmentRegistrationRequest,
    EnrollmentRenewalRequest,
    EnrollmentRevocationRequest,
)


class FakeTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((path, payload))
        return self.response


class FakeSocket:
    def __init__(self, certificate_der: bytes) -> None:
        self.certificate_der = certificate_der

    def getpeercert(self, *, binary_form: bool):
        if not binary_form:
            raise AssertionError("binary certificate required")
        return self.certificate_der


class FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]

    def getheader(self, name: str, default: str = "") -> str:
        return "application/json" if name == "Content-Type" else default


class FakeConnection:
    def __init__(self, certificate_der: bytes, response_body: bytes) -> None:
        self.sock = FakeSocket(certificate_der)
        self.response = FakeResponse(response_body)
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        return None

    def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FirmEnrollmentIssuerTests(unittest.TestCase):
    def test_https_transport_verifies_spki_and_never_retries(self) -> None:
        private = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "enroll.synthetic.example")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(private, algorithm=None)
        )
        certificate_der = certificate.public_bytes(serialization.Encoding.DER)
        spki = private.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pin = "sha256/" + b64encode(sha256(spki).digest()).decode("ascii")
        connection = FakeConnection(
            certificate_der,
            b'{"enrollment_envelope":"signed-envelope"}',
        )
        factory_calls = []

        def factory(host, port, **kwargs):
            factory_calls.append((host, port, kwargs))
            return connection

        transport = PinnedHttpsJsonTransport(
            origin="https://enroll.synthetic.example",
            tls_spki_sha256=(pin,),
            connection_factory=factory,
        )
        payload = transport.post_json(
            "/v1/desktop-enrollments/renew",
            {"current_envelope_sha256": "a" * 64},
        )
        self.assertEqual(payload["enrollment_envelope"], "signed-envelope")
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(connection.requests[0][0:2], ("POST", "/v1/desktop-enrollments/renew"))
        self.assertTrue(connection.closed)
        self.assertNotIn("Authorization", connection.requests[0][3])

        mismatch = PinnedHttpsJsonTransport(
            origin="https://enroll.synthetic.example",
            tls_spki_sha256=("sha256/" + b64encode(b"0" * 32).decode("ascii"),),
            connection_factory=lambda *args, **kwargs: FakeConnection(
                certificate_der,
                b'{"enrollment_envelope":"signed-envelope"}',
            ),
        )
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "pin"):
            mismatch.post_json("/v1/desktop-enrollments/renew", {})

    def test_https_transport_rejects_unpinned_path_and_unsafe_origin(self) -> None:
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "origin"):
            PinnedHttpsJsonTransport(
                origin="http://enroll.synthetic.example",
                tls_spki_sha256=("sha256/" + b64encode(b"a" * 32).decode("ascii"),),
            )
        transport = PinnedHttpsJsonTransport(
            origin="https://enroll.synthetic.example",
            tls_spki_sha256=("sha256/" + b64encode(b"a" * 32).decode("ascii"),),
        )
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "path"):
            transport.post_json("/v1/arbitrary", {})

    def test_registration_sends_only_secret_binding_and_nonce(self) -> None:
        transport = FakeTransport({"enrollment_envelope": json.dumps({"credential": {}, "signature": "x"})})
        issuer = JsonFirmEnrollmentIssuer(transport)
        envelope = issuer.register(
            EnrollmentRegistrationRequest(
                activation_secret="A" * 32,
                installation_binding_sha256="a" * 64,
                client_nonce="n" * 64,
            )
        )
        self.assertIn("credential", envelope)
        path, payload = transport.calls[0]
        self.assertEqual(path, "/v1/desktop-enrollments/activate")
        self.assertEqual(
            set(payload),
            {"activation_secret", "installation_binding_sha256", "client_nonce"},
        )
        self.assertNotIn("actor_id", payload)
        self.assertNotIn("firm_id", payload)
        self.assertNotIn("roles", payload)

    def test_renewal_uses_hash_bound_current_enrollment(self) -> None:
        transport = FakeTransport({"enrollment_envelope": "signed-envelope"})
        issuer = JsonFirmEnrollmentIssuer(transport)
        issuer.renew(
            EnrollmentRenewalRequest(
                enrollment_id="11111111-1111-4111-8111-111111111111",
                installation_binding_sha256="a" * 64,
                current_envelope_sha256="b" * 64,
                client_nonce="n" * 64,
            )
        )
        path, payload = transport.calls[0]
        self.assertEqual(path, "/v1/desktop-enrollments/renew")
        self.assertEqual(payload["current_envelope_sha256"], "b" * 64)

    def test_revocation_receipt_is_strict_and_typed(self) -> None:
        transport = FakeTransport(
            {
                "revocation_id": "44444444-4444-4444-8444-444444444444",
                "enrollment_id": "11111111-1111-4111-8111-111111111111",
                "issuer": "synthetic-firm-issuer",
                "effective_at": "2026-08-10T12:00:00Z",
                "accepted": True,
            }
        )
        issuer = JsonFirmEnrollmentIssuer(transport)
        receipt = issuer.revoke(
            EnrollmentRevocationRequest(
                enrollment_id="11111111-1111-4111-8111-111111111111",
                installation_binding_sha256="a" * 64,
                current_envelope_sha256="b" * 64,
                reason="用户在本机主动请求撤销登记",
                client_nonce="n" * 64,
            )
        )
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.effective_at, datetime(2026, 8, 10, 12, tzinfo=timezone.utc))

        transport.response = {**transport.response, "role": "ADMIN"}
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "fields"):
            issuer.revoke(
                EnrollmentRevocationRequest(
                    enrollment_id="11111111-1111-4111-8111-111111111111",
                    installation_binding_sha256="a" * 64,
                    current_envelope_sha256="b" * 64,
                    reason="用户在本机主动请求撤销登记",
                    client_nonce="n" * 64,
                )
            )


if __name__ == "__main__":
    unittest.main()
