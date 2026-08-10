from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
import json
from subprocess import CompletedProcess
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from case_api.desktop_enrollment import TrustedEnrollmentIssuer
from case_api.desktop_identity_runtime import DesktopIdentityRuntime
from case_api.desktop_enrollment_lifecycle import EnrollmentRevocationReceipt
from case_api.desktop_sidecar import (
    DesktopSidecarBlocked,
    PROTOCOL,
    create_desktop_sidecar_app,
    read_parent_handshake,
)
from case_api.desktop_trust_bootstrap import (
    DesktopEnrollmentTrustRuntime,
    blocked_desktop_enrollment_trust,
)
from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.models import Actor, Role


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class SyntheticCurrentCatalog:
    def __init__(self, issuer: TrustedEnrollmentIssuer) -> None:
        self.issuer = issuer

    def resolve(self, key_id: str, *, now: datetime) -> TrustedEnrollmentIssuer:
        if key_id != self.issuer.key_id:
            raise PermissionError("unknown issuer")
        return self.issuer

    def validate_enrollment_window(
        self,
        *,
        key_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        if key_id != self.issuer.key_id or expires_at <= issued_at:
            raise PermissionError("invalid window")


class DesktopSidecarTests(unittest.TestCase):
    def test_strict_parent_handshake_accepts_only_expected_fields(self) -> None:
        payload = {
            "protocol": PROTOCOL,
            "challenge": "a" * 64,
            "parent_pid": 1234,
            "parent_api_token": "b" * 64,
        }
        self.assertEqual(
            read_parent_handshake(StringIO(json.dumps(payload) + "\n")),
            ("a" * 64, 1234, "b" * 64),
        )
        for invalid in (
            "",
            "{}\n",
            json.dumps({**payload, "actor_id": "client-controlled"}) + "\n",
            json.dumps({**payload, "challenge": "short"}) + "\n",
            json.dumps({**payload, "parent_pid": True}) + "\n",
            json.dumps({**payload, "parent_api_token": "short"}) + "\n",
            "{" + "x" * 2048 + "\n",
        ):
            with self.subTest(invalid=invalid[:60]):
                with self.assertRaises(DesktopSidecarBlocked):
                    read_parent_handshake(StringIO(invalid))

    def test_disabled_health_exposes_no_case_docs_or_identity_routes(self) -> None:
        client = TestClient(create_desktop_sidecar_app())
        health = client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.json(),
            {
                "service": "lawcase-local-api",
                "mode": "desktop-disabled",
                "persistence": "not-configured",
                "identity": "not-enrolled",
                "enrollment_trust": "not-configured",
            },
        )
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 404)
        self.assertEqual(client.get("/v1/matters/example/snapshot").status_code, 404)
        self.assertEqual(client.post("/v1/desktop-enrollment/activate").status_code, 404)
        self.assertEqual(client.post("/v1/desktop-enrollment/renew").status_code, 404)
        self.assertEqual(client.post("/v1/desktop-enrollment/revoke").status_code, 404)

    def test_invalid_trust_bootstrap_is_visible_but_never_opens_case_routes(self) -> None:
        client = TestClient(
            create_desktop_sidecar_app(blocked_desktop_enrollment_trust())
        )
        self.assertEqual(client.get("/healthz").json()["enrollment_trust"], "blocked")
        self.assertEqual(client.get("/v1/matters/example/snapshot").status_code, 404)

    def test_parent_only_activation_uses_keychain_binding_and_returns_staged_envelope(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        private = Ed25519PrivateKey.generate()
        issuer = TrustedEnrollmentIssuer(
            key_id="synthetic-issuer-a",
            issuer="synthetic-law-firm-admin",
            public_key_bytes=private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
        trust = DesktopEnrollmentTrustRuntime(
            phase="READY",
            message="synthetic test trust",
            catalog=SyntheticCurrentCatalog(issuer),  # type: ignore[arg-type]
        )
        installation_secret = b"i" * 32
        credential = {
            "version": 1,
            "key_id": issuer.key_id,
            "issuer": issuer.issuer,
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER"],
            "installation_binding_sha256": sha256(installation_secret).hexdigest(),
            "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(days=5)).isoformat().replace("+00:00", "Z"),
        }
        envelope = json.dumps(
            {
                "credential": credential,
                "signature": urlsafe_b64encode(private.sign(canonical(credential)))
                .decode("ascii")
                .rstrip("="),
            },
            ensure_ascii=False,
        )

        class Issuer:
            register_request = None

            def register(self, request):
                self.register_request = request
                return envelope

            def renew(self, request):
                raise AssertionError(request)

            def revoke(self, request):
                raise AssertionError(request)

        lifecycle_issuer = Issuer()

        def keychain_runner(command, **kwargs):
            del kwargs
            account = command[command.index("-a") + 1]
            if account == "signed-enrollment-v1":
                return CompletedProcess(command, 44, stdout="", stderr="not exposed")
            return CompletedProcess(
                command,
                0,
                stdout=b64encode(installation_secret).decode("ascii") + "\n",
                stderr="",
            )

        client = TestClient(
            create_desktop_sidecar_app(
                trust,
                parent_api_token="c" * 64,
                enrollment_issuer=lifecycle_issuer,
                keychain_runner=keychain_runner,
            )
        )
        endpoint = "/v1/desktop-enrollment/activate"
        authorization = {"Authorization": f"Bearer {'c' * 64}"}
        activation_secret = "A" * 32
        self.assertEqual(
            client.post(endpoint, json={"activation_secret": activation_secret}).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                endpoint,
                headers=authorization,
                json={"activation_secret": activation_secret, "role": "ADMIN"},
            ).status_code,
            422,
        )
        activated = client.post(
            endpoint,
            headers=authorization,
            json={"activation_secret": activation_secret},
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["status"], "REGISTERED")
        self.assertEqual(activated.json()["envelope_text"], envelope)
        self.assertEqual(
            activated.json()["installation_binding_sha256"],
            sha256(installation_secret).hexdigest(),
        )
        self.assertNotIn(activation_secret, activated.text)
        request = lifecycle_issuer.register_request
        self.assertIsNotNone(request)
        self.assertEqual(request.activation_secret, activation_secret)
        self.assertEqual(
            set(request.__dict__),
            {"activation_secret", "installation_binding_sha256", "client_nonce"},
        )
        self.assertNotIn(activation_secret, repr(request))

    def test_native_parent_token_guards_exact_signed_enrollment_verification(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        issuer = TrustedEnrollmentIssuer(
            key_id="synthetic-issuer-a",
            issuer="synthetic-law-firm-admin",
            public_key_bytes=public,
        )
        catalog = SyntheticCurrentCatalog(issuer)
        trust = DesktopEnrollmentTrustRuntime(
            phase="READY",
            message="synthetic test trust",
            catalog=catalog,  # type: ignore[arg-type]
        )
        installation_secret = b"i" * 32
        credential = {
            "version": 1,
            "key_id": issuer.key_id,
            "issuer": issuer.issuer,
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER"],
            "installation_binding_sha256": sha256(installation_secret).hexdigest(),
            "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        }
        envelope = json.dumps(
            {
                "credential": credential,
                "signature": urlsafe_b64encode(private.sign(canonical(credential)))
                .decode("ascii")
                .rstrip("="),
            },
            ensure_ascii=False,
        )
        client = TestClient(
            create_desktop_sidecar_app(trust, parent_api_token="c" * 64)
        )
        body = {
            "envelope_text": envelope,
            "installation_binding_sha256": sha256(installation_secret).hexdigest(),
        }
        endpoint = "/v1/desktop-enrollment/verify"
        self.assertEqual(client.post(endpoint, json=body).status_code, 404)
        verified = client.post(
            endpoint,
            json=body,
            headers={"Authorization": f"Bearer {'c' * 64}"},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["status"], "VERIFIED")
        self.assertEqual(
            verified.json()["envelope_sha256"],
            sha256(envelope.encode("utf-8")).hexdigest(),
        )
        body["installation_binding_sha256"] = "0" * 64
        self.assertEqual(
            client.post(
                endpoint,
                json=body,
                headers={"Authorization": f"Bearer {'c' * 64}"},
            ).status_code,
            422,
        )

    def test_reverified_identity_exchanges_one_native_session_without_case_routes(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        authority = DesktopSessionAuthority(
            actor=Actor(
                actor_id="22222222-2222-4222-8222-222222222222",
                firm_id="33333333-3333-4333-8333-333333333333",
                roles=frozenset({Role.LEAD_LAWYER}),
            ),
            bootstrap_token="d" * 64,
            bootstrap_expires_at=now + timedelta(seconds=30),
            session_expires_at=now + timedelta(minutes=30),
            clock=lambda: now,
            token_factory=lambda: "s" * 64,
        )
        identity = DesktopIdentityRuntime(
            phase="ENROLLED",
            message="synthetic enrolled identity",
            enrollment_id="11111111-1111-4111-8111-111111111111",
            expires_at=(now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            session_authority=authority,
        )
        client = TestClient(
            create_desktop_sidecar_app(identity=identity),
            client=("127.0.0.1", 50001),
        )
        endpoint = "/v1/desktop-sessions/exchange"
        headers = {
            "Origin": "tauri://localhost",
            "X-Desktop-Bootstrap": "d" * 64,
        }
        exchanged = client.post(endpoint, headers=headers)
        self.assertEqual(exchanged.status_code, 200)
        self.assertEqual(exchanged.json()["status"], "SESSION_READY")
        self.assertEqual(exchanged.json()["access_token"], "s" * 64)
        self.assertEqual(exchanged.headers["cache-control"], "no-store")
        self.assertEqual(client.post(endpoint, headers=headers).status_code, 401)
        self.assertEqual(client.get("/v1/matters/example/snapshot").status_code, 404)

    def test_parent_only_renewal_and_remote_revocation_are_staged_for_native_cas(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        private = Ed25519PrivateKey.generate()
        issuer = TrustedEnrollmentIssuer(
            key_id="synthetic-issuer-a",
            issuer="synthetic-law-firm-admin",
            public_key_bytes=private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
        trust = DesktopEnrollmentTrustRuntime(
            phase="READY",
            message="synthetic test trust",
            catalog=SyntheticCurrentCatalog(issuer),  # type: ignore[arg-type]
        )
        installation_secret = b"i" * 32

        def signed(expires_at: datetime, issued_at: datetime) -> str:
            credential = {
                "version": 1,
                "key_id": issuer.key_id,
                "issuer": issuer.issuer,
                "enrollment_id": "11111111-1111-4111-8111-111111111111",
                "actor_id": "22222222-2222-4222-8222-222222222222",
                "firm_id": "33333333-3333-4333-8333-333333333333",
                "roles": ["LEAD_LAWYER"],
                "installation_binding_sha256": sha256(installation_secret).hexdigest(),
                "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            }
            return json.dumps(
                {
                    "credential": credential,
                    "signature": urlsafe_b64encode(private.sign(canonical(credential)))
                    .decode("ascii")
                    .rstrip("="),
                },
                ensure_ascii=False,
            )

        current = signed(now + timedelta(days=5), now - timedelta(minutes=1))
        renewed = signed(now + timedelta(days=10), now)

        class Issuer:
            def register(self, request):
                raise AssertionError(request)

            def renew(self, request):
                self.renew_request = request
                return renewed

            def revoke(self, request):
                self.revoke_request = request
                return EnrollmentRevocationReceipt(
                    revocation_id="44444444-4444-4444-8444-444444444444",
                    enrollment_id="11111111-1111-4111-8111-111111111111",
                    issuer=issuer.issuer,
                    effective_at=now,
                    accepted=True,
                )

        lifecycle_issuer = Issuer()

        def keychain_runner(command, **kwargs):
            del kwargs
            account = command[command.index("-a") + 1]
            value = current if account == "signed-enrollment-v1" else b64encode(installation_secret).decode("ascii")
            return CompletedProcess(command, 0, stdout=value + "\n", stderr="")

        client = TestClient(
            create_desktop_sidecar_app(
                trust,
                parent_api_token="c" * 64,
                enrollment_issuer=lifecycle_issuer,
                keychain_runner=keychain_runner,
            )
        )
        authorization = {"Authorization": f"Bearer {'c' * 64}"}
        self.assertEqual(client.post("/v1/desktop-enrollment/renew").status_code, 404)
        renewal = client.post("/v1/desktop-enrollment/renew", headers=authorization)
        self.assertEqual(renewal.status_code, 200, renewal.text)
        self.assertEqual(renewal.json()["status"], "RENEWED")
        self.assertEqual(renewal.json()["envelope_text"], renewed)
        self.assertEqual(
            renewal.json()["expected_current_sha256"],
            sha256(current.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("installation_secret", renewal.text)

        self.assertEqual(
            client.post(
                "/v1/desktop-enrollment/revoke",
                headers=authorization,
                json={"confirmation": "wrong"},
            ).status_code,
            422,
        )
        revocation = client.post(
            "/v1/desktop-enrollment/revoke",
            headers=authorization,
            json={"confirmation": "CONFIRM_REMOTE_REVOCATION"},
        )
        self.assertEqual(revocation.status_code, 200, revocation.text)
        self.assertTrue(revocation.json()["remote_revocation_confirmed"])
        self.assertEqual(
            revocation.json()["expected_current_sha256"],
            sha256(current.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
