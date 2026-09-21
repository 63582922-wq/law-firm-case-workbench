from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from subprocess import CompletedProcess
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from case_api.desktop_enrollment import DesktopEnrollmentBlocked, TrustedEnrollmentIssuer
from case_api.desktop_identity_runtime import load_desktop_identity
from case_api.desktop_trust_bootstrap import DesktopEnrollmentTrustRuntime


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
INSTALLATION_SECRET = b"i" * 32


class SequenceRunner:
    def __init__(self, responses: list[CompletedProcess[str]]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, args: list[str], **kwargs) -> CompletedProcess[str]:
        del args, kwargs
        self.calls += 1
        return self.responses.pop(0)


class SyntheticCatalog:
    def __init__(self, issuer: TrustedEnrollmentIssuer) -> None:
        self.issuer = issuer

    def resolve(self, key_id: str, *, now: datetime) -> TrustedEnrollmentIssuer:
        del now
        if key_id != self.issuer.key_id:
            raise DesktopEnrollmentBlocked("unknown issuer")
        return self.issuer

    def validate_enrollment_window(
        self,
        *,
        key_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        if key_id != self.issuer.key_id or expires_at <= issued_at:
            raise DesktopEnrollmentBlocked("invalid issuer window")


class DesktopIdentityRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.issuer = TrustedEnrollmentIssuer(
            key_id="synthetic-issuer-a",
            issuer="synthetic-law-firm-admin",
            public_key_bytes=public,
        )
        self.catalog = SyntheticCatalog(self.issuer)
        self.trust = DesktopEnrollmentTrustRuntime(
            phase="READY",
            message="synthetic trust",
            catalog=self.catalog,  # type: ignore[arg-type]
        )

    def envelope(self, *, expires_at: datetime | None = None) -> str:
        credential = {
            "version": 1,
            "key_id": self.issuer.key_id,
            "issuer": self.issuer.issuer,
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER"],
            "installation_binding_sha256": sha256(INSTALLATION_SECRET).hexdigest(),
            "issued_at": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (expires_at or NOW + timedelta(days=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        canonical = json.dumps(
            credential,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return json.dumps(
            {
                "credential": credential,
                "signature": urlsafe_b64encode(self.private.sign(canonical))
                .decode("ascii")
                .rstrip("="),
            },
            ensure_ascii=False,
        )

    def runner(self, envelope: str | None = None) -> SequenceRunner:
        if envelope is None:
            return SequenceRunner([CompletedProcess([], 44, "", "not found")])
        return SequenceRunner(
            [
                CompletedProcess([], 0, envelope, ""),
                CompletedProcess([], 0, b64encode(INSTALLATION_SECRET).decode("ascii"), ""),
            ]
        )

    def load(self, runner: SequenceRunner):
        return load_desktop_identity(
            trust=self.trust,
            bootstrap_token="b" * 64,
            clock=lambda: NOW,
            platform_name="darwin",
            runner=runner,
            token_factory=lambda: "s" * 64,
        )

    def test_valid_saved_enrollment_reverifies_and_builds_process_session(self) -> None:
        runner = self.runner(self.envelope())
        runtime = self.load(runner)
        self.assertEqual(runtime.phase, "ENROLLED")
        self.assertEqual(runtime.enrollment_id, "11111111-1111-4111-8111-111111111111")
        self.assertIsNotNone(runtime.session_authority)
        self.assertEqual(runner.calls, 2)
        self.assertNotIn("22222222-2222-4222-8222-222222222222", repr(runtime))
        self.assertNotIn("b" * 64, repr(runtime))

    def test_missing_enrollment_is_not_enrolled_but_invalid_is_blocked(self) -> None:
        missing = self.runner()
        self.assertEqual(self.load(missing).phase, "NOT_ENROLLED")
        self.assertEqual(missing.calls, 1)

        parsed = json.loads(self.envelope())
        parsed["signature"] = "A" * 86
        invalid = self.load(self.runner(json.dumps(parsed)))
        self.assertEqual(invalid.phase, "BLOCKED")
        self.assertIsNone(invalid.session_authority)

    def test_unconfigured_or_blocked_trust_never_reads_keychain(self) -> None:
        runner = SequenceRunner([])
        unconfigured = load_desktop_identity(
            trust=DesktopEnrollmentTrustRuntime(
                phase="NOT_CONFIGURED",
                message="disabled",
            ),
            bootstrap_token="b" * 64,
            runner=runner,
        )
        self.assertEqual(unconfigured.phase, "NOT_ENROLLED")
        self.assertEqual(runner.calls, 0)

        blocked = load_desktop_identity(
            trust=DesktopEnrollmentTrustRuntime(phase="BLOCKED", message="blocked"),
            bootstrap_token="b" * 64,
            runner=runner,
        )
        self.assertEqual(blocked.phase, "BLOCKED")
        self.assertEqual(runner.calls, 0)

    def test_credential_too_close_to_expiry_does_not_create_session(self) -> None:
        runtime = self.load(
            self.runner(self.envelope(expires_at=NOW + timedelta(seconds=30)))
        )
        self.assertEqual(runtime.phase, "BLOCKED")
        self.assertIsNone(runtime.session_authority)


if __name__ == "__main__":
    unittest.main()
