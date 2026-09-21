from __future__ import annotations

from base64 import b64encode, urlsafe_b64encode
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from case_api.desktop_trust_bootstrap import (
    BOOTSTRAP_SCHEMA_VERSION,
    DesktopTrustBootstrapBlocked,
    default_bootstrap_path,
    load_desktop_enrollment_trust,
)
from case_api.trusted_enrollment_catalog import CATALOG_SCHEMA_VERSION


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


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


class DesktopTrustBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roots = {
            "catalog-root-a": Ed25519PrivateKey.generate(),
            "catalog-root-b": Ed25519PrivateKey.generate(),
        }
        issuer = Ed25519PrivateKey.generate()
        self.signed = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_version": 1,
            "previous_catalog_sha256": None,
            "issued_at": "2026-08-10T11:55:00Z",
            "expires_at": "2027-08-09T11:55:00Z",
            "enrollment_api_origin": "https://identity.synthetic-law-firm.example",
            "tls_spki_sha256": ["sha256/" + b64encode(b"p" * 32).decode("ascii")],
            "issuer_keys": [
                {
                    "key_id": "firm-issuer-2026-a",
                    "issuer": "synthetic-law-firm-admin",
                    "public_key": b64encode(raw_public_key(issuer)).decode("ascii"),
                    "status": "ACTIVE",
                    "not_before": "2026-08-01T00:00:00Z",
                    "not_after": "2027-08-01T00:00:00Z",
                    "status_changed_at": None,
                }
            ],
        }

    def bootstrap(self) -> dict[str, object]:
        signatures = []
        for key_id in sorted(self.roots):
            signature = self.roots[key_id].sign(canonical(self.signed))
            signatures.append(
                {
                    "key_id": key_id,
                    "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
                }
            )
        return {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "deployment_state": "PINNED",
            "threshold": 2,
            "roots": [
                {
                    "key_id": key_id,
                    "public_key": b64encode(raw_public_key(self.roots[key_id])).decode("ascii"),
                }
                for key_id in sorted(self.roots)
            ],
            "catalog_envelope": {"signed": self.signed, "signatures": signatures},
        }

    def load(self, payload: object):
        with TemporaryDirectory(prefix="lawcase-trust-test-") as temporary:
            path = Path(temporary) / "bootstrap.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return load_desktop_enrollment_trust(path, clock=lambda: NOW)

    def test_repository_bootstrap_is_deliberately_not_configured(self) -> None:
        runtime = load_desktop_enrollment_trust(default_bootstrap_path(), clock=lambda: NOW)
        self.assertEqual(runtime.phase, "NOT_CONFIGURED")
        self.assertIsNone(runtime.catalog)
        self.assertIsNone(runtime.enrollment_api_origin)

    def test_threshold_signed_pinned_catalog_loads_ready(self) -> None:
        runtime = self.load(self.bootstrap())
        self.assertEqual(runtime.phase, "READY")
        self.assertEqual(runtime.catalog_version, 1)
        self.assertEqual(runtime.active_issuer_key_count, 1)
        self.assertEqual(
            runtime.enrollment_api_origin,
            "https://identity.synthetic-law-firm.example",
        )
        self.assertIsNotNone(runtime.catalog)
        self.assertNotIn("private", repr(runtime).lower())

    def test_disabled_bootstrap_cannot_hide_roots_or_catalog(self) -> None:
        payload = self.bootstrap()
        payload["deployment_state"] = "NOT_CONFIGURED"
        payload["threshold"] = 0
        with self.assertRaisesRegex(DesktopTrustBootstrapBlocked, "cannot contain"):
            self.load(payload)
        invalid_boolean = {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "deployment_state": "NOT_CONFIGURED",
            "threshold": False,
            "roots": [],
            "catalog_envelope": None,
        }
        with self.assertRaisesRegex(DesktopTrustBootstrapBlocked, "cannot contain"):
            self.load(invalid_boolean)

    def test_unsorted_duplicate_or_invalid_roots_fail_closed(self) -> None:
        unsorted = self.bootstrap()
        unsorted["roots"] = list(reversed(unsorted["roots"]))
        duplicate = self.bootstrap()
        duplicate["roots"] = [duplicate["roots"][0], duplicate["roots"][0]]
        invalid = self.bootstrap()
        invalid["roots"][0]["public_key"] = "not-base64"
        for payload in (unsorted, duplicate, invalid):
            with self.subTest(payload=payload["roots"]), self.assertRaises(
                DesktopTrustBootstrapBlocked
            ):
                self.load(payload)

    def test_wrong_threshold_signature_and_expired_catalog_fail_closed(self) -> None:
        wrong_threshold = self.bootstrap()
        wrong_threshold["threshold"] = 3
        tampered = self.bootstrap()
        tampered["catalog_envelope"]["signed"]["enrollment_api_origin"] = (
            "https://attacker.example"
        )
        expired = self.bootstrap()
        expired["catalog_envelope"]["signed"]["expires_at"] = "2026-08-10T12:00:00Z"
        for payload in (wrong_threshold, tampered, expired):
            with self.assertRaises(DesktopTrustBootstrapBlocked):
                self.load(payload)

    def test_unknown_fields_and_private_key_material_are_rejected(self) -> None:
        extra = self.bootstrap()
        extra["private_key"] = "forbidden"
        nested_extra = deepcopy(self.bootstrap())
        nested_extra["roots"][0]["private_key"] = "forbidden"
        for payload in (extra, nested_extra):
            with self.assertRaises(DesktopTrustBootstrapBlocked):
                self.load(payload)


if __name__ == "__main__":
    unittest.main()
