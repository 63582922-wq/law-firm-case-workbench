from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from case_api.desktop_enrollment import TrustedEnrollmentIssuer
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

    def test_invalid_trust_bootstrap_is_visible_but_never_opens_case_routes(self) -> None:
        client = TestClient(
            create_desktop_sidecar_app(blocked_desktop_enrollment_trust())
        )
        self.assertEqual(client.get("/healthz").json()["enrollment_trust"], "blocked")
        self.assertEqual(client.get("/v1/matters/example/snapshot").status_code, 404)

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


if __name__ == "__main__":
    unittest.main()
