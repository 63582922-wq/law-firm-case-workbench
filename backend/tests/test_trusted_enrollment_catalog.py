from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from case_api.desktop_enrollment import (
    DesktopEnrollmentBlocked,
    SignedDesktopEnrollmentVerifier,
)
from case_api.trusted_enrollment_catalog import (
    CATALOG_SCHEMA_VERSION,
    EnrollmentTrustCatalogBlocked,
    TrustedCatalogRoot,
    TrustedEnrollmentCatalogVerifier,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
INSTALLATION_SECRET = b"i" * 32


def raw_public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class TrustedEnrollmentCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_private = {
            "catalog-root-a": Ed25519PrivateKey.generate(),
            "catalog-root-b": Ed25519PrivateKey.generate(),
            "catalog-root-c": Ed25519PrivateKey.generate(),
        }
        roots = {
            key_id: TrustedCatalogRoot(key_id=key_id, public_key_bytes=raw_public_key(private))
            for key_id, private in self.root_private.items()
        }
        self.catalog_verifier = TrustedEnrollmentCatalogVerifier(
            roots=roots,
            threshold=2,
            clock=lambda: NOW,
        )
        self.issuer_private = {
            "firm-issuer-2026-a": Ed25519PrivateKey.generate(),
            "firm-issuer-2026-b": Ed25519PrivateKey.generate(),
        }

    def issuer_key(
        self,
        key_id: str,
        *,
        status: str = "ACTIVE",
        status_changed_at: str | None = None,
    ) -> dict[str, object]:
        return {
            "key_id": key_id,
            "issuer": "synthetic-law-firm-admin",
            "public_key": b64encode(raw_public_key(self.issuer_private[key_id])).decode("ascii"),
            "status": status,
            "not_before": "2026-08-01T00:00:00Z",
            "not_after": "2027-08-01T00:00:00Z",
            "status_changed_at": status_changed_at,
        }

    def signed_catalog(
        self,
        *,
        version: int = 1,
        previous_hash: str | None = None,
        issuer_keys: list[dict[str, object]] | None = None,
        root_signers: tuple[str, ...] = ("catalog-root-a", "catalog-root-b"),
        issued_at: str = "2026-08-10T11:55:00Z",
        expires_at: str = "2027-08-09T11:55:00Z",
        origin: str = "https://identity.synthetic-law-firm.example",
        pins: list[str] | None = None,
        extra_signed: dict[str, object] | None = None,
    ) -> str:
        signed: dict[str, object] = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_version": version,
            "previous_catalog_sha256": previous_hash,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "enrollment_api_origin": origin,
            "tls_spki_sha256": pins or ["sha256/" + b64encode(b"p" * 32).decode("ascii")],
            "issuer_keys": issuer_keys or [self.issuer_key("firm-issuer-2026-a")],
        }
        signed.update(extra_signed or {})
        signatures = []
        for key_id in root_signers:
            signature = self.root_private[key_id].sign(canonical(signed))
            signatures.append(
                {
                    "key_id": key_id,
                    "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
                }
            )
        return json.dumps(
            {"signed": signed, "signatures": signatures},
            ensure_ascii=False,
        )

    def enrollment_envelope(
        self,
        *,
        key_id: str = "firm-issuer-2026-a",
        issued_at: str = "2026-08-10T11:55:00Z",
        expires_at: str = "2026-09-09T11:55:00Z",
    ) -> str:
        credential = {
            "version": 1,
            "key_id": key_id,
            "issuer": "synthetic-law-firm-admin",
            "enrollment_id": "11111111-1111-4111-8111-111111111111",
            "actor_id": "22222222-2222-4222-8222-222222222222",
            "firm_id": "33333333-3333-4333-8333-333333333333",
            "roles": ["LEAD_LAWYER"],
            "installation_binding_sha256": sha256(INSTALLATION_SECRET).hexdigest(),
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        signature = self.issuer_private[key_id].sign(canonical(credential))
        return json.dumps(
            {
                "credential": credential,
                "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            },
            ensure_ascii=False,
        )

    def test_threshold_signed_catalog_drives_enrollment_verification(self) -> None:
        catalog = self.catalog_verifier.verify(self.signed_catalog())
        self.assertEqual(catalog.catalog_version, 1)
        self.assertEqual(catalog.enrollment_api_origin, "https://identity.synthetic-law-firm.example")
        verifier = SignedDesktopEnrollmentVerifier(
            trusted_catalog=catalog,
            clock=lambda: NOW,
        )
        enrollment = verifier.verify(
            envelope_text=self.enrollment_envelope(),
            installation_secret=INSTALLATION_SECRET,
        )
        self.assertEqual(enrollment.key_id, "firm-issuer-2026-a")

    def test_signature_threshold_unknown_root_and_tampering_fail_closed(self) -> None:
        with self.assertRaisesRegex(EnrollmentTrustCatalogBlocked, "threshold"):
            self.catalog_verifier.verify(
                self.signed_catalog(root_signers=("catalog-root-a",))
            )

        parsed = json.loads(self.signed_catalog())
        parsed["signatures"][0]["key_id"] = "unknown-root"
        with self.assertRaisesRegex(EnrollmentTrustCatalogBlocked, "unknown root"):
            self.catalog_verifier.verify(json.dumps(parsed))

        parsed = json.loads(self.signed_catalog())
        parsed["signed"]["enrollment_api_origin"] = "https://attacker.example"
        with self.assertRaisesRegex(EnrollmentTrustCatalogBlocked, "signature is invalid"):
            self.catalog_verifier.verify(json.dumps(parsed))

    def test_strict_schema_https_origin_tls_pins_and_canonical_order(self) -> None:
        invalid = (
            (self.signed_catalog(origin="http://identity.synthetic-law-firm.example"), "HTTPS DNS origin"),
            (self.signed_catalog(origin="https://127.0.0.1"), "HTTPS DNS origin"),
            (self.signed_catalog(origin="https://identity.synthetic-law-firm.example/path"), "HTTPS DNS origin"),
            (self.signed_catalog(origin="https://identity.synthetic-law-firm.example:bad"), "HTTPS DNS origin"),
            (self.signed_catalog(pins=["sha256/not-base64"]), "TLS pin"),
            (
                self.signed_catalog(
                    issuer_keys=[
                        self.issuer_key("firm-issuer-2026-b"),
                        self.issuer_key("firm-issuer-2026-a"),
                    ]
                ),
                "not canonical",
            ),
            (self.signed_catalog(extra_signed={"browser_role": "FIRM_ADMIN"}), "signed fields"),
        )
        for payload, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                EnrollmentTrustCatalogBlocked, message
            ):
                self.catalog_verifier.verify(payload)

        duplicate = self.signed_catalog()[:-1] + ',"signed":{}}'
        with self.assertRaisesRegex(EnrollmentTrustCatalogBlocked, "duplicate fields"):
            self.catalog_verifier.verify(duplicate)

    def test_exact_next_version_and_previous_hash_block_rollback_and_mix(self) -> None:
        first = self.catalog_verifier.verify(self.signed_catalog())
        second = self.catalog_verifier.verify(
            self.signed_catalog(version=2, previous_hash=first.catalog_sha256),
            previous=first,
        )
        self.assertEqual(second.catalog_version, 2)

        for version, previous_hash, message in (
            (1, first.catalog_sha256, "rollback or fast-forward"),
            (3, first.catalog_sha256, "rollback or fast-forward"),
            (2, "0" * 64, "previous hash"),
        ):
            with self.subTest(version=version), self.assertRaisesRegex(
                EnrollmentTrustCatalogBlocked, message
            ):
                self.catalog_verifier.verify(
                    self.signed_catalog(version=version, previous_hash=previous_hash),
                    previous=first,
                )

    def test_expired_future_and_excessive_lifetime_catalogs_fail_closed(self) -> None:
        cases = (
            (self.signed_catalog(expires_at="2026-08-10T12:00:00Z"), "expired"),
            (self.signed_catalog(issued_at="2026-08-10T12:05:01Z"), "future"),
            (self.signed_catalog(expires_at="2027-08-12T11:55:00Z"), "lifetime"),
        )
        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                EnrollmentTrustCatalogBlocked, message
            ):
                self.catalog_verifier.verify(payload)

    def test_retired_key_accepts_pre_retirement_issue_only_and_revoked_key_never_resolves(self) -> None:
        retired_keys = [
            self.issuer_key(
                "firm-issuer-2026-a",
                status="RETIRED",
                status_changed_at="2026-08-10T11:58:00Z",
            ),
            self.issuer_key("firm-issuer-2026-b"),
        ]
        catalog = self.catalog_verifier.verify(self.signed_catalog(issuer_keys=retired_keys))
        verifier = SignedDesktopEnrollmentVerifier(trusted_catalog=catalog, clock=lambda: NOW)
        verifier.verify(
            envelope_text=self.enrollment_envelope(issued_at="2026-08-10T11:55:00Z"),
            installation_secret=INSTALLATION_SECRET,
        )
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "after key retirement"):
            verifier.verify(
                envelope_text=self.enrollment_envelope(issued_at="2026-08-10T11:59:00Z"),
                installation_secret=INSTALLATION_SECRET,
            )

        revoked_keys = deepcopy(retired_keys)
        revoked_keys[0]["status"] = "REVOKED"
        revoked = self.catalog_verifier.verify(self.signed_catalog(issuer_keys=revoked_keys))
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "revoked"):
            SignedDesktopEnrollmentVerifier(
                trusted_catalog=revoked,
                clock=lambda: NOW,
            ).verify(
                envelope_text=self.enrollment_envelope(),
                installation_secret=INSTALLATION_SECRET,
            )

        future_retired = deepcopy(retired_keys)
        future_retired[0]["status_changed_at"] = "2026-08-10T12:05:01Z"
        with self.assertRaisesRegex(EnrollmentTrustCatalogBlocked, "status time"):
            self.catalog_verifier.verify(self.signed_catalog(issuer_keys=future_retired))

    def test_catalog_expiry_is_rechecked_for_long_running_process(self) -> None:
        catalog = self.catalog_verifier.verify(self.signed_catalog())
        verifier = SignedDesktopEnrollmentVerifier(
            trusted_catalog=catalog,
            clock=lambda: NOW + timedelta(days=366),
        )
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "catalog is not current"):
            verifier.verify(
                envelope_text=self.enrollment_envelope(),
                installation_secret=INSTALLATION_SECRET,
            )

    def test_verifier_requires_exactly_one_trust_source(self) -> None:
        catalog = self.catalog_verifier.verify(self.signed_catalog())
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "exactly one"):
            SignedDesktopEnrollmentVerifier()
        with self.assertRaisesRegex(DesktopEnrollmentBlocked, "exactly one"):
            SignedDesktopEnrollmentVerifier(trusted_issuers={}, trusted_catalog=catalog)


if __name__ == "__main__":
    unittest.main()
