"""Threshold-signed, rollback-protected trust metadata for firm enrollment.

The application release pins only offline catalog-root public keys.  A signed
catalog then supplies the current firm enrollment endpoint, TLS SPKI pins and
Ed25519 issuer keys.  Catalog expiry, monotonic versions and an exact previous
hash prevent an untrusted transport from silently freezing, mixing or rolling
back deployment trust.
"""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64decode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .desktop_enrollment import DesktopEnrollmentBlocked, TrustedEnrollmentIssuer


CATALOG_SCHEMA_VERSION = "lawcase-enrollment-trust-catalog-v1"
MAX_CATALOG_BYTES = 65_536
MAX_CATALOG_LIFETIME = timedelta(days=366)
MAX_ISSUER_KEY_LIFETIME = timedelta(days=3 * 366)
MAX_ISSUER_KEYS = 32
MAX_TLS_PINS = 3
MAX_CLOCK_SKEW = timedelta(minutes=5)

_ENVELOPE_FIELDS = frozenset({"signed", "signatures"})
_SIGNED_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_version",
        "previous_catalog_sha256",
        "issued_at",
        "expires_at",
        "enrollment_api_origin",
        "tls_spki_sha256",
        "issuer_keys",
    }
)
_SIGNATURE_FIELDS = frozenset({"key_id", "signature"})
_ISSUER_KEY_FIELDS = frozenset(
    {
        "key_id",
        "issuer",
        "public_key",
        "status",
        "not_before",
        "not_after",
        "status_changed_at",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DNS_NAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]{86}$")


class EnrollmentTrustCatalogBlocked(PermissionError):
    """The deployment trust catalog cannot be accepted."""


class EnrollmentIssuerKeyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class TrustedCatalogRoot:
    key_id: str
    public_key_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_identifier(self.key_id, "catalog root key")
        _validate_public_key(self.public_key_bytes, "catalog root key")


@dataclass(frozen=True)
class CatalogIssuerKey:
    trusted_issuer: TrustedEnrollmentIssuer
    status: EnrollmentIssuerKeyStatus
    not_before: datetime
    not_after: datetime
    status_changed_at: datetime | None


@dataclass(frozen=True)
class VerifiedEnrollmentTrustCatalog:
    catalog_version: int
    catalog_sha256: str
    issued_at: datetime
    expires_at: datetime
    enrollment_api_origin: str
    tls_spki_sha256: tuple[str, ...]
    issuer_keys: tuple[CatalogIssuerKey, ...] = field(repr=False)

    def resolve(self, key_id: str, *, now: datetime) -> TrustedEnrollmentIssuer:
        _require_utc_runtime(now, "trust catalog clock")
        if now + MAX_CLOCK_SKEW < self.issued_at or now >= self.expires_at:
            raise DesktopEnrollmentBlocked("desktop enrollment trust catalog is not current")
        key = self._key(key_id)
        if key.status is EnrollmentIssuerKeyStatus.REVOKED:
            raise DesktopEnrollmentBlocked("desktop enrollment issuer key is revoked")
        return key.trusted_issuer

    def validate_enrollment_window(
        self,
        *,
        key_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        _require_utc_runtime(issued_at, "enrollment issue time")
        _require_utc_runtime(expires_at, "enrollment expiry")
        key = self._key(key_id)
        if key.status is EnrollmentIssuerKeyStatus.REVOKED:
            raise DesktopEnrollmentBlocked("desktop enrollment issuer key is revoked")
        if issued_at < key.not_before or expires_at > key.not_after:
            raise DesktopEnrollmentBlocked("desktop enrollment is outside issuer key validity")
        if (
            key.status is EnrollmentIssuerKeyStatus.RETIRED
            and (key.status_changed_at is None or issued_at > key.status_changed_at)
        ):
            raise DesktopEnrollmentBlocked("desktop enrollment was issued after key retirement")

    def _key(self, key_id: str) -> CatalogIssuerKey:
        for key in self.issuer_keys:
            if key.trusted_issuer.key_id == key_id:
                return key
        raise DesktopEnrollmentBlocked("desktop enrollment issuer is not trusted")


class TrustedEnrollmentCatalogVerifier:
    """Verify one catalog or the exact next version in an existing chain."""

    def __init__(
        self,
        *,
        roots: Mapping[str, TrustedCatalogRoot],
        threshold: int,
        clock=None,
    ) -> None:
        if not roots:
            raise EnrollmentTrustCatalogBlocked("at least one catalog root is required")
        self._roots = dict(roots)
        for key_id, root in self._roots.items():
            if key_id != root.key_id:
                raise EnrollmentTrustCatalogBlocked("catalog root registry is inconsistent")
        if type(threshold) is not int or threshold < 1 or threshold > len(self._roots):
            raise EnrollmentTrustCatalogBlocked("catalog root threshold is invalid")
        self._threshold = threshold
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(
        self,
        envelope_text: str,
        *,
        previous: VerifiedEnrollmentTrustCatalog | None = None,
    ) -> VerifiedEnrollmentTrustCatalog:
        parsed = _strict_json_object(envelope_text)
        if frozenset(parsed) != _ENVELOPE_FIELDS:
            raise EnrollmentTrustCatalogBlocked("trust catalog envelope fields are invalid")
        signed = parsed.get("signed")
        signatures = parsed.get("signatures")
        if not isinstance(signed, dict) or frozenset(signed) != _SIGNED_FIELDS:
            raise EnrollmentTrustCatalogBlocked("trust catalog signed fields are invalid")
        self._verify_signatures(signed, signatures)

        if signed.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise EnrollmentTrustCatalogBlocked("trust catalog schema version is unsupported")
        catalog_version = signed.get("catalog_version")
        if type(catalog_version) is not int or catalog_version < 1 or catalog_version > 2_147_483_647:
            raise EnrollmentTrustCatalogBlocked("trust catalog version is invalid")
        issued_at = _required_utc_timestamp(signed.get("issued_at"), "catalog issued_at")
        expires_at = _required_utc_timestamp(signed.get("expires_at"), "catalog expires_at")
        now = self._now()
        if issued_at > now + MAX_CLOCK_SKEW:
            raise EnrollmentTrustCatalogBlocked("trust catalog issue time is in the future")
        if expires_at <= now:
            raise EnrollmentTrustCatalogBlocked("trust catalog has expired")
        if expires_at <= issued_at or expires_at - issued_at > MAX_CATALOG_LIFETIME:
            raise EnrollmentTrustCatalogBlocked("trust catalog lifetime is invalid")

        canonical_envelope = _canonical_json_bytes(parsed)
        catalog_hash = sha256(canonical_envelope).hexdigest()
        previous_hash = signed.get("previous_catalog_sha256")
        self._verify_chain(
            catalog_version=catalog_version,
            previous_hash=previous_hash,
            issued_at=issued_at,
            previous=previous,
        )
        origin = _required_https_origin(signed.get("enrollment_api_origin"))
        tls_pins = _required_tls_pins(signed.get("tls_spki_sha256"))
        issuer_keys = _required_issuer_keys(signed.get("issuer_keys"), now=now)
        return VerifiedEnrollmentTrustCatalog(
            catalog_version=catalog_version,
            catalog_sha256=catalog_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            enrollment_api_origin=origin,
            tls_spki_sha256=tls_pins,
            issuer_keys=issuer_keys,
        )

    def _verify_signatures(self, signed: dict[str, object], signatures: object) -> None:
        if not isinstance(signatures, list) or not signatures or len(signatures) > len(self._roots):
            raise EnrollmentTrustCatalogBlocked("trust catalog signatures are invalid")
        key_ids: list[str] = []
        canonical = _canonical_json_bytes(signed)
        valid = 0
        for item in signatures:
            if not isinstance(item, dict) or frozenset(item) != _SIGNATURE_FIELDS:
                raise EnrollmentTrustCatalogBlocked("trust catalog signature entry is invalid")
            key_id = item.get("key_id")
            signature_text = item.get("signature")
            _require_identifier(key_id, "catalog signature key")
            if key_id in key_ids or not isinstance(signature_text, str) or not _SIGNATURE.fullmatch(signature_text):
                raise EnrollmentTrustCatalogBlocked("trust catalog signatures are invalid")
            key_ids.append(key_id)
            root = self._roots.get(key_id)
            if root is None:
                raise EnrollmentTrustCatalogBlocked("trust catalog signature uses an unknown root")
            try:
                signature = urlsafe_b64decode(signature_text + "==")
                Ed25519PublicKey.from_public_bytes(root.public_key_bytes).verify(signature, canonical)
            except (InvalidSignature, ValueError) as error:
                raise EnrollmentTrustCatalogBlocked("trust catalog signature is invalid") from error
            valid += 1
        if key_ids != sorted(key_ids):
            raise EnrollmentTrustCatalogBlocked("trust catalog signatures are not canonical")
        if valid < self._threshold:
            raise EnrollmentTrustCatalogBlocked("trust catalog signature threshold was not met")

    @staticmethod
    def _verify_chain(
        *,
        catalog_version: int,
        previous_hash: object,
        issued_at: datetime,
        previous: VerifiedEnrollmentTrustCatalog | None,
    ) -> None:
        if previous is None:
            if catalog_version != 1 or previous_hash is not None:
                raise EnrollmentTrustCatalogBlocked("initial trust catalog chain is invalid")
            return
        if catalog_version != previous.catalog_version + 1:
            raise EnrollmentTrustCatalogBlocked("trust catalog version rollback or fast-forward detected")
        if previous_hash != previous.catalog_sha256:
            raise EnrollmentTrustCatalogBlocked("trust catalog previous hash does not match")
        if issued_at < previous.issued_at:
            raise EnrollmentTrustCatalogBlocked("trust catalog issue time rolled back")

    def _now(self) -> datetime:
        now = self._clock()
        _require_utc_runtime(now, "trust catalog clock")
        return now


def _required_issuer_keys(value: object, *, now: datetime) -> tuple[CatalogIssuerKey, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_ISSUER_KEYS:
        raise EnrollmentTrustCatalogBlocked("trust catalog issuer keys are invalid")
    result: list[CatalogIssuerKey] = []
    key_ids: list[str] = []
    for item in value:
        if not isinstance(item, dict) or frozenset(item) != _ISSUER_KEY_FIELDS:
            raise EnrollmentTrustCatalogBlocked("trust catalog issuer key fields are invalid")
        key_id = item.get("key_id")
        issuer = item.get("issuer")
        _require_identifier(key_id, "issuer key")
        if key_id in key_ids:
            raise EnrollmentTrustCatalogBlocked("trust catalog issuer key is duplicated")
        key_ids.append(key_id)
        if not isinstance(issuer, str) or not issuer.strip() or issuer != issuer.strip() or len(issuer) > 200:
            raise EnrollmentTrustCatalogBlocked("trust catalog issuer name is invalid")
        public_key = _decode_public_key(item.get("public_key"))
        try:
            status = EnrollmentIssuerKeyStatus(item.get("status"))
        except (TypeError, ValueError) as error:
            raise EnrollmentTrustCatalogBlocked("trust catalog issuer key status is invalid") from error
        not_before = _required_utc_timestamp(item.get("not_before"), "issuer not_before")
        not_after = _required_utc_timestamp(item.get("not_after"), "issuer not_after")
        if not_after <= not_before or not_after - not_before > MAX_ISSUER_KEY_LIFETIME:
            raise EnrollmentTrustCatalogBlocked("trust catalog issuer key lifetime is invalid")
        changed_raw = item.get("status_changed_at")
        changed = None if changed_raw is None else _required_utc_timestamp(changed_raw, "issuer status_changed_at")
        if status is EnrollmentIssuerKeyStatus.ACTIVE:
            if changed is not None:
                raise EnrollmentTrustCatalogBlocked("active issuer key cannot have status_changed_at")
        elif (
            changed is None
            or changed < not_before
            or changed > not_after
            or changed > now + MAX_CLOCK_SKEW
        ):
            raise EnrollmentTrustCatalogBlocked("inactive issuer key status time is invalid")
        result.append(
            CatalogIssuerKey(
                trusted_issuer=TrustedEnrollmentIssuer(
                    key_id=key_id,
                    issuer=issuer,
                    public_key_bytes=public_key,
                ),
                status=status,
                not_before=not_before,
                not_after=not_after,
                status_changed_at=changed,
            )
        )
    if key_ids != sorted(key_ids):
        raise EnrollmentTrustCatalogBlocked("trust catalog issuer keys are not canonical")
    if not any(
        key.status is EnrollmentIssuerKeyStatus.ACTIVE
        and key.not_before <= now < key.not_after
        for key in result
    ):
        raise EnrollmentTrustCatalogBlocked("trust catalog has no currently active issuer key")
    return tuple(result)


def _required_https_origin(value: object) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise EnrollmentTrustCatalogBlocked("enrollment API origin is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise EnrollmentTrustCatalogBlocked(
            "enrollment API origin must be one canonical HTTPS DNS origin"
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
        or port not in (None, 443)
        or parsed.hostname is None
        or not _DNS_NAME.fullmatch(parsed.hostname)
        or value != f"https://{parsed.hostname}"
    ):
        raise EnrollmentTrustCatalogBlocked("enrollment API origin must be one canonical HTTPS DNS origin")
    return value


def _required_tls_pins(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TLS_PINS:
        raise EnrollmentTrustCatalogBlocked("enrollment API TLS pins are invalid")
    pins: list[str] = []
    for pin in value:
        if not isinstance(pin, str) or not pin.startswith("sha256/"):
            raise EnrollmentTrustCatalogBlocked("enrollment API TLS pin is invalid")
        try:
            decoded = b64decode(pin[7:], validate=True)
        except (ValueError, UnicodeEncodeError) as error:
            raise EnrollmentTrustCatalogBlocked("enrollment API TLS pin is invalid") from error
        if len(decoded) != 32 or pin in pins:
            raise EnrollmentTrustCatalogBlocked("enrollment API TLS pins are invalid")
        pins.append(pin)
    if pins != sorted(pins):
        raise EnrollmentTrustCatalogBlocked("enrollment API TLS pins are not canonical")
    return tuple(pins)


def _decode_public_key(value: object) -> bytes:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise EnrollmentTrustCatalogBlocked("trust catalog issuer public key is invalid")
    try:
        decoded = b64decode(value, validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise EnrollmentTrustCatalogBlocked("trust catalog issuer public key is invalid") from error
    _validate_public_key(decoded, "trust catalog issuer public key")
    return decoded


def _validate_public_key(value: bytes, label: str) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise EnrollmentTrustCatalogBlocked(f"{label} is invalid")
    try:
        Ed25519PublicKey.from_public_bytes(value)
    except ValueError as error:
        raise EnrollmentTrustCatalogBlocked(f"{label} is invalid") from error


def _strict_json_object(value: str) -> dict[str, object]:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_CATALOG_BYTES:
            raise EnrollmentTrustCatalogBlocked("trust catalog size is invalid")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise EnrollmentTrustCatalogBlocked("trust catalog contains duplicate fields")
                result[key] = item
            return result

        parsed = json.loads(value, object_pairs_hook=reject_duplicates)
    except EnrollmentTrustCatalogBlocked:
        raise
    except (UnicodeEncodeError, json.JSONDecodeError) as error:
        raise EnrollmentTrustCatalogBlocked("trust catalog is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise EnrollmentTrustCatalogBlocked("trust catalog must be an object")
    return parsed


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise EnrollmentTrustCatalogBlocked("trust catalog cannot be canonicalized") from error


def _required_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or "." in value:
        raise EnrollmentTrustCatalogBlocked(f"{label} must use whole-second UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EnrollmentTrustCatalogBlocked(f"{label} is invalid") from error
    _require_utc_runtime(parsed, label)
    return parsed


def _require_utc_runtime(value: object, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise EnrollmentTrustCatalogBlocked(f"{label} must use UTC")


def _require_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise EnrollmentTrustCatalogBlocked(f"{label} identifier is invalid")
