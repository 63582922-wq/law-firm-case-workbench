from __future__ import annotations

from base64 import urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from case_api.desktop_enrollment import SignedDesktopEnrollmentVerifier, TrustedEnrollmentIssuer
from case_api.desktop_enrollment_lifecycle import (
    DesktopEnrollmentLifecycle,
    DesktopEnrollmentLifecycleBlocked,
    EnrollmentOperationRemoteStatus,
    EnrollmentOperationStatusRequest,
    EnrollmentRegistrationRequest,
    EnrollmentRenewalRequest,
    EnrollmentRevocationReceipt,
    EnrollmentRevocationRequest,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
SECRET = b"k" * 32
NONCE = "n" * 64


class FakeVault:
    def __init__(self, envelope: str | None = None) -> None:
        self.envelope = envelope
        self.replace_calls: list[tuple[str | None, str]] = []
        self.delete_calls: list[str] = []

    def installation_secret(self) -> bytes:
        return SECRET

    def current_envelope(self) -> str:
        if self.envelope is None:
            raise KeyError("missing")
        return self.envelope

    def current_envelope_optional(self) -> str | None:
        return self.envelope

    def replace_enrollment(self, *, expected_sha256: str | None, envelope_text: str) -> None:
        current_hash = sha256(self.envelope.encode("utf-8")).hexdigest() if self.envelope is not None else None
        if current_hash != expected_sha256:
            raise RuntimeError("compare-and-set conflict")
        self.replace_calls.append((expected_sha256, envelope_text))
        self.envelope = envelope_text

    def delete_enrollment(self, *, expected_sha256: str) -> None:
        if self.envelope is None or sha256(self.envelope.encode("utf-8")).hexdigest() != expected_sha256:
            raise RuntimeError("compare-and-set conflict")
        self.delete_calls.append(expected_sha256)
        self.envelope = None


class FakeIssuer:
    def __init__(
        self,
        *,
        registration: str,
        renewal: str,
        receipt: EnrollmentRevocationReceipt,
        remote_status: EnrollmentOperationRemoteStatus | None = None,
    ) -> None:
        self.registration = registration
        self.renewal = renewal
        self.receipt = receipt
        self.remote_status = remote_status
        self.register_requests: list[EnrollmentRegistrationRequest] = []
        self.renew_requests: list[EnrollmentRenewalRequest] = []
        self.revoke_requests: list[EnrollmentRevocationRequest] = []
        self.query_requests: list[EnrollmentOperationStatusRequest] = []

    def register(self, request: EnrollmentRegistrationRequest) -> str:
        self.register_requests.append(request)
        return self.registration

    def renew(self, request: EnrollmentRenewalRequest) -> str:
        self.renew_requests.append(request)
        return self.renewal

    def revoke(self, request: EnrollmentRevocationRequest) -> EnrollmentRevocationReceipt:
        self.revoke_requests.append(request)
        return self.receipt

    def query_operation(
        self,
        request: EnrollmentOperationStatusRequest,
    ) -> EnrollmentOperationRemoteStatus:
        self.query_requests.append(request)
        if self.remote_status is None:
            raise AssertionError("unexpected operation status query")
        return self.remote_status


class DesktopEnrollmentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        issuer = TrustedEnrollmentIssuer(
            key_id="firm-root-2026",
            issuer="synthetic-firm-issuer",
            public_key_bytes=public_key,
        )
        self.verifier = SignedDesktopEnrollmentVerifier(
            trusted_issuers={issuer.key_id: issuer},
            clock=lambda: NOW,
        )
        self.base_credential = {
            "version": 1,
            "key_id": issuer.key_id,
            "issuer": issuer.issuer,
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER"],
            "installation_binding_sha256": sha256(SECRET).hexdigest(),
            "issued_at": "2026-08-10T11:55:00Z",
            "expires_at": "2026-08-20T11:55:00Z",
        }
        self.current = self._envelope(self.base_credential)
        renewal = deepcopy(self.base_credential)
        renewal["issued_at"] = "2026-08-10T12:00:00Z"
        renewal["expires_at"] = "2026-09-09T12:00:00Z"
        self.renewal = self._envelope(renewal)
        self.receipt = EnrollmentRevocationReceipt(
            revocation_id="44444444-4444-4444-8444-444444444444",
            enrollment_id=self.base_credential["enrollment_id"],
            issuer="synthetic-firm-issuer",
            effective_at=NOW,
            accepted=True,
        )

    def test_registration_sends_only_activation_binding_and_nonce_then_verifies_before_commit(self) -> None:
        vault = FakeVault()
        issuer = FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)
        lifecycle = self._lifecycle(vault, issuer)

        result = lifecycle.register(activation_secret="A" * 32)

        self.assertEqual(result.status, "REGISTERED")
        self.assertEqual(len(vault.replace_calls), 1)
        request = issuer.register_requests[0]
        self.assertEqual(set(vars(request)), {"activation_secret", "installation_binding_sha256", "client_nonce"})
        self.assertNotIn("actor", vars(request))
        self.assertNotIn("firm", vars(request))
        self.assertNotIn("roles", vars(request))
        self.assertNotIn("A" * 32, repr(request))
        self.assertNotIn(NONCE, repr(request))

    def test_invalid_registration_response_never_reaches_vault(self) -> None:
        vault = FakeVault()
        issuer = FakeIssuer(registration="not signed", renewal=self.renewal, receipt=self.receipt)
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "signature verification"):
            self._lifecycle(vault, issuer).register(activation_secret="A" * 32)
        self.assertEqual(vault.replace_calls, [])
        self.assertIsNone(vault.envelope)

    def test_renewal_is_compare_and_set_and_cannot_switch_identity(self) -> None:
        vault = FakeVault(self.current)
        issuer = FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)
        result = self._lifecycle(vault, issuer).renew()
        self.assertEqual(result.status, "RENEWED")
        self.assertEqual(vault.replace_calls[0][0], sha256(self.current.encode("utf-8")).hexdigest())

        switched = deepcopy(self.base_credential)
        switched["actor_id"] = "55555555-5555-4555-8555-555555555555"
        switched["issued_at"] = "2026-08-10T12:00:00Z"
        switched["expires_at"] = "2026-09-09T12:00:00Z"
        vault = FakeVault(self.current)
        issuer = FakeIssuer(registration=self.current, renewal=self._envelope(switched), receipt=self.receipt)
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "does not match"):
            self._lifecycle(vault, issuer).renew()
        self.assertEqual(vault.replace_calls, [])

    def test_remote_revocation_requires_matching_accepted_receipt_before_delete(self) -> None:
        vault = FakeVault(self.current)
        issuer = FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)
        result = self._lifecycle(vault, issuer).revoke(reason="设备交接停用")
        self.assertEqual(result.status, "REVOKED")
        self.assertTrue(result.remote_revocation_confirmed)
        self.assertIsNone(vault.envelope)

        rejected = EnrollmentRevocationReceipt(
            revocation_id=self.receipt.revocation_id,
            enrollment_id=self.receipt.enrollment_id,
            issuer=self.receipt.issuer,
            effective_at=NOW,
            accepted=False,
        )
        vault = FakeVault(self.current)
        issuer = FakeIssuer(registration=self.current, renewal=self.renewal, receipt=rejected)
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "not accepted"):
            self._lifecycle(vault, issuer).revoke(reason="律师离职停用")
        self.assertEqual(vault.delete_calls, [])
        self.assertEqual(vault.envelope, self.current)

    def test_local_disable_is_explicitly_not_remote_revocation(self) -> None:
        vault = FakeVault(self.current)
        issuer = FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)
        result = self._lifecycle(vault, issuer).disable_local()
        self.assertEqual(result.status, "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED")
        self.assertFalse(result.remote_revocation_confirmed)
        self.assertEqual(issuer.revoke_requests, [])
        self.assertIsNone(vault.envelope)

        damaged_vault = FakeVault("damaged-or-expired-envelope")
        result = self._lifecycle(damaged_vault, issuer).disable_local()
        self.assertEqual(result.status, "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED")
        self.assertIsNone(result.enrollment_id)
        self.assertIsNone(damaged_vault.envelope)

    def test_successful_activation_status_is_verified_before_empty_vault_commit(self) -> None:
        vault = FakeVault()
        remote = EnrollmentOperationRemoteStatus(
            operation_id=NONCE,
            operation_kind="ACTIVATE",
            state="SUCCEEDED",
            enrollment_envelope=self.current,
            revocation_receipt=None,
        )
        issuer = FakeIssuer(
            registration=self.current,
            renewal=self.renewal,
            receipt=self.receipt,
            remote_status=remote,
        )

        resolution = self._lifecycle(vault, issuer).resolve_remote_operation(
            operation_id=NONCE,
            operation_kind="ACTIVATE",
        )

        self.assertEqual(resolution.state, "SUCCEEDED")
        self.assertEqual(resolution.result.status, "REGISTERED")
        self.assertEqual(vault.replace_calls, [(None, self.current)])
        request = issuer.query_requests[0]
        self.assertIsNone(request.current_envelope_sha256)
        self.assertEqual(request.installation_binding_sha256, sha256(SECRET).hexdigest())

    def test_successful_renewal_and_revocation_status_use_current_compare_and_set(self) -> None:
        renewal_remote = EnrollmentOperationRemoteStatus(
            operation_id=NONCE,
            operation_kind="RENEW",
            state="SUCCEEDED",
            enrollment_envelope=self.renewal,
            revocation_receipt=None,
        )
        vault = FakeVault(self.current)
        issuer = FakeIssuer(
            registration=self.current,
            renewal=self.renewal,
            receipt=self.receipt,
            remote_status=renewal_remote,
        )
        resolution = self._lifecycle(vault, issuer).resolve_remote_operation(
            operation_id=NONCE,
            operation_kind="RENEW",
        )
        current_hash = sha256(self.current.encode("utf-8")).hexdigest()
        self.assertEqual(resolution.result.status, "RENEWED")
        self.assertEqual(vault.replace_calls, [(current_hash, self.renewal)])
        self.assertEqual(issuer.query_requests[0].current_envelope_sha256, current_hash)

        revocation_remote = EnrollmentOperationRemoteStatus(
            operation_id=NONCE,
            operation_kind="REVOKE",
            state="SUCCEEDED",
            enrollment_envelope=None,
            revocation_receipt=self.receipt,
        )
        vault = FakeVault(self.current)
        issuer = FakeIssuer(
            registration=self.current,
            renewal=self.renewal,
            receipt=self.receipt,
            remote_status=revocation_remote,
        )
        resolution = self._lifecycle(vault, issuer).resolve_remote_operation(
            operation_id=NONCE,
            operation_kind="REVOKE",
        )
        self.assertEqual(resolution.result.status, "REVOKED")
        self.assertTrue(resolution.result.remote_revocation_confirmed)
        self.assertEqual(vault.delete_calls, [current_hash])

    def test_pending_and_rejected_status_never_mutate_the_vault(self) -> None:
        for state in ("PENDING", "REJECTED"):
            with self.subTest(state=state):
                remote = EnrollmentOperationRemoteStatus(
                    operation_id=NONCE,
                    operation_kind="RENEW",
                    state=state,
                    enrollment_envelope=None,
                    revocation_receipt=None,
                )
                vault = FakeVault(self.current)
                issuer = FakeIssuer(
                    registration=self.current,
                    renewal=self.renewal,
                    receipt=self.receipt,
                    remote_status=remote,
                )
                resolution = self._lifecycle(vault, issuer).resolve_remote_operation(
                    operation_id=NONCE,
                    operation_kind="RENEW",
                )
                self.assertEqual(resolution.state, state)
                self.assertIsNone(resolution.result)
                self.assertEqual(vault.replace_calls, [])
                self.assertEqual(vault.delete_calls, [])

    def test_operation_status_mismatch_or_unfinished_result_material_fails_closed(self) -> None:
        invalid_statuses = (
            EnrollmentOperationRemoteStatus(
                operation_id="x" * 64,
                operation_kind="RENEW",
                state="PENDING",
                enrollment_envelope=None,
                revocation_receipt=None,
            ),
            EnrollmentOperationRemoteStatus(
                operation_id=NONCE,
                operation_kind="RENEW",
                state="PENDING",
                enrollment_envelope=self.renewal,
                revocation_receipt=None,
            ),
        )
        for remote in invalid_statuses:
            with self.subTest(remote=remote):
                vault = FakeVault(self.current)
                issuer = FakeIssuer(
                    registration=self.current,
                    renewal=self.renewal,
                    receipt=self.receipt,
                    remote_status=remote,
                )
                with self.assertRaises(DesktopEnrollmentLifecycleBlocked):
                    self._lifecycle(vault, issuer).resolve_remote_operation(
                        operation_id=NONCE,
                        operation_kind="RENEW",
                    )
                self.assertEqual(vault.replace_calls, [])

    def test_secrets_invalid_reason_and_transport_failures_are_redacted(self) -> None:
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "activation secret"):
            self._lifecycle(FakeVault(), FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)).register(
                activation_secret="short"
            )
        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "reason"):
            self._lifecycle(FakeVault(self.current), FakeIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)).revoke(
                reason="x"
            )

        class FailingIssuer(FakeIssuer):
            def register(self, request: EnrollmentRegistrationRequest) -> str:
                del request
                raise RuntimeError("A" * 32)

        with self.assertRaisesRegex(DesktopEnrollmentLifecycleBlocked, "service is unavailable") as context:
            self._lifecycle(FakeVault(), FailingIssuer(registration=self.current, renewal=self.renewal, receipt=self.receipt)).register(
                activation_secret="A" * 32
            )
        self.assertNotIn("A" * 32, str(context.exception))

    def _lifecycle(self, vault: FakeVault, issuer: FakeIssuer) -> DesktopEnrollmentLifecycle:
        return DesktopEnrollmentLifecycle(
            issuer=issuer,
            vault=vault,
            verifier=self.verifier,
            clock=lambda: NOW,
            nonce_factory=lambda: NONCE,
        )

    def _envelope(self, credential: dict[str, object]) -> str:
        canonical = json.dumps(
            credential,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = urlsafe_b64encode(self.private_key.sign(canonical)).decode("ascii").rstrip("=")
        return json.dumps({"credential": credential, "signature": signature}, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
