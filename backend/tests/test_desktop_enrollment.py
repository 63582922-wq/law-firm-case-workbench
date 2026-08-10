from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from subprocess import CompletedProcess
import unittest
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from case_api.desktop_enrollment import (
    DesktopEnrollment,
    DesktopEnrollmentBlocked,
    MacOSKeychainDesktopEnrollmentProvider,
    SignedDesktopEnrollmentVerifier,
    TrustedEnrollmentIssuer,
    create_enrolled_desktop_session_authority,
)
from case_kernel.models import Role


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
INSTALLATION_SECRET = b"i" * 32


class SequenceRunner:
    def __init__(self, responses: list[CompletedProcess[str]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kwargs) -> CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class DesktopEnrollmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.issuer = TrustedEnrollmentIssuer(
            key_id="firm-root-2026",
            issuer="synthetic-law-firm-admin",
            public_key_bytes=public_key,
        )
        self.verifier = SignedDesktopEnrollmentVerifier(
            trusted_issuers={self.issuer.key_id: self.issuer},
            clock=lambda: NOW,
        )

    def credential(self) -> dict[str, object]:
        return {
            "version": 1,
            "key_id": self.issuer.key_id,
            "issuer": self.issuer.issuer,
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER", "REVIEWER"],
            "installation_binding_sha256": sha256(INSTALLATION_SECRET).hexdigest(),
            "issued_at": "2026-08-10T11:55:00Z",
            "expires_at": "2026-09-09T11:55:00Z",
        }

    def envelope(self, credential: dict[str, object] | None = None) -> str:
        body = credential or self.credential()
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = urlsafe_b64encode(self.private_key.sign(canonical)).decode("ascii").rstrip("=")
        return json.dumps({"credential": body, "signature": signature}, ensure_ascii=False)

    def test_valid_signed_enrollment_produces_server_actor_and_redacts_binding(self) -> None:
        enrollment = self.verifier.verify(
            envelope_text=self.envelope(),
            installation_secret=INSTALLATION_SECRET,
        )
        self.assertEqual(enrollment.actor.actor_id, "22222222-2222-4222-8222-222222222222")
        self.assertEqual(enrollment.actor.firm_id, "33333333-3333-4333-8333-333333333333")
        self.assertEqual(enrollment.actor.roles, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
        self.assertNotIn(sha256(INSTALLATION_SECRET).hexdigest(), repr(enrollment))

        native_boundary = self.verifier.verify_for_installation_binding(
            envelope_text=self.envelope(),
            installation_binding_sha256=sha256(INSTALLATION_SECRET).hexdigest(),
        )
        self.assertEqual(native_boundary.enrollment_id, enrollment.enrollment_id)
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "binding is unavailable"):
            self.verifier.verify_for_installation_binding(
                envelope_text=self.envelope(),
                installation_binding_sha256="invalid",
            )

    def test_tampering_unknown_issuer_and_wrong_installation_fail_closed(self) -> None:
        tampered = json.loads(self.envelope())
        tampered["credential"]["roles"] = ["FIRM_ADMIN"]
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "signature is invalid"):
            self.verifier.verify(
                envelope_text=json.dumps(tampered),
                installation_secret=INSTALLATION_SECRET,
            )

        unknown = self.credential()
        unknown["key_id"] = "unknown-root"
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "not trusted"):
            self.verifier.verify(
                envelope_text=self.envelope(unknown),
                installation_secret=INSTALLATION_SECRET,
            )

        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "another installation"):
            self.verifier.verify(
                envelope_text=self.envelope(),
                installation_secret=b"x" * 32,
            )

    def test_identity_roles_and_fields_cannot_be_smuggled_or_duplicated(self) -> None:
        cases: list[tuple[dict[str, object], str]] = []
        unknown_field = self.credential()
        unknown_field["browser_role"] = "LEAD_LAWYER"
        cases.append((unknown_field, "fields are invalid"))
        duplicate_roles = self.credential()
        duplicate_roles["roles"] = ["REVIEWER", "REVIEWER"]
        cases.append((duplicate_roles, "roles are invalid"))
        worker = self.credential()
        worker["roles"] = ["SYSTEM_WORKER"]
        cases.append((worker, "not available to a lawyer"))
        empty_roles = self.credential()
        empty_roles["roles"] = []
        cases.append((empty_roles, "roles are invalid"))
        noncanonical_uuid = self.credential()
        noncanonical_uuid["actor_id"] = "{" + str(UUID(str(noncanonical_uuid["actor_id"]))) + "}"
        cases.append((noncanonical_uuid, "canonical UUID"))

        for credential, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                DesktopEnrollmentBlocked, message
            ):
                self.verifier.verify(
                    envelope_text=self.envelope(credential),
                    installation_secret=INSTALLATION_SECRET,
                )

        valid_envelope = self.envelope()
        duplicate_json = valid_envelope[:-1] + ',"signature":"' + "A" * 86 + '"}'
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "duplicate fields"):
            self.verifier.verify(
                envelope_text=duplicate_json,
                installation_secret=INSTALLATION_SECRET,
            )

    def test_expiry_future_issue_and_excessive_lifetime_are_rejected(self) -> None:
        cases = []
        expired = self.credential()
        expired["expires_at"] = "2026-08-10T12:00:00Z"
        cases.append((expired, "expired"))
        future = self.credential()
        future["issued_at"] = "2026-08-10T12:05:01Z"
        cases.append((future, "future"))
        excessive = self.credential()
        excessive["expires_at"] = "2026-11-09T11:55:01Z"
        cases.append((excessive, "lifetime"))
        offset = self.credential()
        offset["issued_at"] = "2026-08-10T19:55:00+08:00"
        cases.append((offset, "must use UTC"))

        for credential, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                DesktopEnrollmentBlocked, message
            ):
                self.verifier.verify(
                    envelope_text=self.envelope(credential),
                    installation_secret=INSTALLATION_SECRET,
                )

    def test_malformed_oversized_and_bad_signature_envelopes_are_rejected(self) -> None:
        malformed_cases = [
            ("not-json", "invalid JSON"),
            ("[]", "must be an object"),
            ("{}", "fields are invalid"),
            (" " * 16_385, "size is invalid"),
        ]
        for envelope, message in malformed_cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                DesktopEnrollmentBlocked, message
            ):
                self.verifier.verify(
                    envelope_text=envelope,
                    installation_secret=INSTALLATION_SECRET,
                )

        bad_signature = json.loads(self.envelope())
        bad_signature["signature"] = "A" * 86
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "signature is invalid"):
            self.verifier.verify(
                envelope_text=json.dumps(bad_signature),
                installation_secret=INSTALLATION_SECRET,
            )

    def test_keychain_provider_is_read_only_and_never_places_secrets_in_arguments(self) -> None:
        envelope = self.envelope()
        encoded_secret = b64encode(INSTALLATION_SECRET).decode("ascii")
        runner = SequenceRunner(
            [
                CompletedProcess([], 0, envelope + "\n", ""),
                CompletedProcess([], 0, encoded_secret + "\n", ""),
            ]
        )
        provider = MacOSKeychainDesktopEnrollmentProvider(
            service="cn.lawcase.workbench.desktop-enrollment",
            enrollment_account="signed-enrollment-v1",
            installation_secret_account="installation-binding-v1",
            verifier=self.verifier,
            platform_name="darwin",
            runner=runner,
        )
        enrollment = provider.load()
        self.assertEqual(enrollment.actor.roles, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
        self.assertEqual(len(runner.calls), 2)
        for args, kwargs in runner.calls:
            self.assertEqual(args[:2], ["/usr/bin/security", "find-generic-password"])
            self.assertNotIn(envelope, args)
            self.assertNotIn(encoded_secret, args)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C"})
            self.assertEqual(kwargs["timeout"], 5)

    def test_keychain_provider_rejects_missing_invalid_oversized_and_non_macos_items(self) -> None:
        def provider(runner: SequenceRunner, platform_name: str = "darwin"):
            return MacOSKeychainDesktopEnrollmentProvider(
                service="cn.lawcase.workbench.desktop-enrollment",
                enrollment_account="signed-enrollment-v1",
                installation_secret_account="installation-binding-v1",
                verifier=self.verifier,
                platform_name=platform_name,
                runner=runner,
            )

        missing = SequenceRunner([CompletedProcess([], 44, "", "sensitive detail")])
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "enrollment is unavailable"):
            provider(missing).load()
        optional_missing = SequenceRunner(
            [CompletedProcess([], 44, "", "sensitive detail")]
        )
        self.assertIsNone(provider(optional_missing).load_optional())

        lookup_failure = SequenceRunner(
            [CompletedProcess([], 1, "", "sensitive detail")]
        )
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "enrollment is unavailable"):
            provider(lookup_failure).load_optional()

        bad_secret = SequenceRunner(
            [CompletedProcess([], 0, self.envelope(), ""), CompletedProcess([], 0, "not base64", "")]
        )
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "invalid encoding"):
            provider(bad_secret).load()

        oversized = SequenceRunner([CompletedProcess([], 0, "x" * 16_385, "")])
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "invalid size"):
            provider(oversized).load()

        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "unavailable on this platform"):
            provider(SequenceRunner([]), platform_name="linux").load()

    def test_trust_registry_rejects_inconsistent_or_empty_entries(self) -> None:
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "at least one"):
            SignedDesktopEnrollmentVerifier(trusted_issuers={})
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "inconsistent"):
            SignedDesktopEnrollmentVerifier(trusted_issuers={"other": self.issuer})

    def test_verified_enrollment_is_the_only_actor_source_for_desktop_session(self) -> None:
        enrollment = self.verifier.verify(
            envelope_text=self.envelope(),
            installation_secret=INSTALLATION_SECRET,
        )

        class Provider:
            def load(self) -> DesktopEnrollment:
                return enrollment

        authority = create_enrolled_desktop_session_authority(
            enrollment_provider=Provider(),
            bootstrap_token="b" * 64,
            clock=lambda: NOW,
            token_factory=lambda: "s" * 64,
        )
        self.assertNotIn(enrollment.actor.actor_id, repr(authority))

    def test_session_expiry_is_capped_by_enrollment_and_too_short_fails_closed(self) -> None:
        valid = self.verifier.verify(
            envelope_text=self.envelope(),
            installation_secret=INSTALLATION_SECRET,
        )

        class Provider:
            def __init__(self, enrollment: DesktopEnrollment) -> None:
                self.enrollment = enrollment

            def load(self) -> DesktopEnrollment:
                return self.enrollment

        short = DesktopEnrollment(
            enrollment_id=valid.enrollment_id,
            actor=valid.actor,
            issuer=valid.issuer,
            key_id=valid.key_id,
            installation_binding_sha256=valid.installation_binding_sha256,
            issued_at=valid.issued_at,
            expires_at=NOW + timedelta(seconds=30),
        )
        with self.assertRaisesRegex(PermissionError, "expires too soon"):
            create_enrolled_desktop_session_authority(
                enrollment_provider=Provider(short),
                bootstrap_token="b" * 64,
                clock=lambda: NOW,
            )


if __name__ == "__main__":
    unittest.main()
