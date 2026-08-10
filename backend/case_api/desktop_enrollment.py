"""Trusted desktop lawyer enrollment for the local macOS application.

An operating-system account does not prove a person's professional identity or
matter authority.  This module therefore accepts only a short-lived enrollment
credential signed by a pinned law-firm issuer and bound to one installation
secret held in macOS Keychain.  The browser never supplies actor, firm or role.
"""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64decode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import re
import subprocess
import sys
from typing import Mapping, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from case_kernel.models import Actor, Role

from .persistent_identity import DesktopSessionAuthority, PersistentAuthenticationBlocked


ENROLLMENT_CREDENTIAL_VERSION = 1
MAX_ENROLLMENT_BYTES = 16_384
MAX_ENROLLMENT_LIFETIME = timedelta(days=30)
MAX_CLOCK_SKEW = timedelta(minutes=5)
INSTALLATION_SECRET_BYTES = 32
MACOS_KEYCHAIN_SERVICE = "cn.lawcase.workbench.desktop-enrollment"
MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT = "signed-enrollment-v1"
MACOS_KEYCHAIN_INSTALLATION_ACCOUNT = "installation-binding-v1"

_ENVELOPE_FIELDS = frozenset({"credential", "signature"})
_CREDENTIAL_FIELDS = frozenset(
    {
        "version",
        "key_id",
        "issuer",
        "enrollment_id",
        "actor_id",
        "firm_id",
        "roles",
        "installation_binding_sha256",
        "issued_at",
        "expires_at",
    }
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")
_PROFESSIONAL_ROLES = frozenset(role for role in Role if role is not Role.SYSTEM_WORKER)


class DesktopEnrollmentBlocked(PermissionError):
    """A trusted enrolled desktop identity cannot be established."""


@dataclass(frozen=True)
class TrustedEnrollmentIssuer:
    """One pinned issuer key distributed by a trusted application release."""

    key_id: str
    issuer: str
    public_key_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.key_id):
            raise DesktopEnrollmentBlocked("trusted enrollment key identifier is invalid")
        if not self.issuer.strip() or self.issuer != self.issuer.strip() or len(self.issuer) > 200:
            raise DesktopEnrollmentBlocked("trusted enrollment issuer is invalid")
        if len(self.public_key_bytes) != 32:
            raise DesktopEnrollmentBlocked("trusted enrollment public key is invalid")
        try:
            Ed25519PublicKey.from_public_bytes(self.public_key_bytes)
        except ValueError as error:
            raise DesktopEnrollmentBlocked("trusted enrollment public key is invalid") from error


@dataclass(frozen=True)
class DesktopEnrollment:
    enrollment_id: str
    actor: Actor
    issuer: str
    key_id: str
    installation_binding_sha256: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime


class DesktopEnrollmentProvider(Protocol):
    def load(self) -> DesktopEnrollment: ...


class TrustedEnrollmentIssuerResolver(Protocol):
    """Resolve keys from current, signed deployment trust metadata."""

    def resolve(self, key_id: str, *, now: datetime) -> TrustedEnrollmentIssuer: ...

    def validate_enrollment_window(
        self,
        *,
        key_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None: ...


def create_enrolled_desktop_session_authority(
    *,
    enrollment_provider: DesktopEnrollmentProvider,
    bootstrap_token: str,
    clock=None,
    token_factory=None,
) -> DesktopSessionAuthority:
    """Build a process-local session only from a verified Keychain enrollment.

    Matter access remains independently checked against active database roles by
    every persistent store operation.  This function only establishes the actor
    identity used for those later checks.
    """

    current_clock = clock or (lambda: datetime.now(timezone.utc))
    now = current_clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise PersistentAuthenticationBlocked("desktop session clock must use UTC")
    enrollment = enrollment_provider.load()
    if enrollment.expires_at.tzinfo is None or enrollment.expires_at.utcoffset() != timedelta(0):
        raise PersistentAuthenticationBlocked("desktop enrollment expiry must use UTC")
    session_expires_at = min(now + timedelta(minutes=30), enrollment.expires_at)
    bootstrap_expires_at = min(now + timedelta(seconds=30), session_expires_at)
    if bootstrap_expires_at <= now or session_expires_at <= bootstrap_expires_at:
        raise PersistentAuthenticationBlocked("desktop enrollment expires too soon for a session")
    return DesktopSessionAuthority(
        actor=enrollment.actor,
        bootstrap_token=bootstrap_token,
        bootstrap_expires_at=bootstrap_expires_at,
        session_expires_at=session_expires_at,
        issuer=f"{enrollment.issuer}:{enrollment.enrollment_id}",
        clock=current_clock,
        token_factory=token_factory,
    )


class SignedDesktopEnrollmentVerifier:
    """Verify strict canonical JSON signed with a pinned Ed25519 public key."""

    def __init__(
        self,
        *,
        trusted_issuers: Mapping[str, TrustedEnrollmentIssuer] | None = None,
        trusted_catalog: TrustedEnrollmentIssuerResolver | None = None,
        clock=None,
    ) -> None:
        if (trusted_issuers is None) == (trusted_catalog is None):
            raise DesktopEnrollmentBlocked("exactly one enrollment trust source is required")
        if trusted_issuers is not None and not trusted_issuers:
            raise DesktopEnrollmentBlocked("at least one trusted enrollment issuer is required")
        self._trusted_issuers = dict(trusted_issuers or {})
        for key_id, issuer in self._trusted_issuers.items():
            if key_id != issuer.key_id:
                raise DesktopEnrollmentBlocked("trusted enrollment issuer registry is inconsistent")
        self._trusted_catalog = trusted_catalog
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, *, envelope_text: str, installation_secret: bytes) -> DesktopEnrollment:
        if len(installation_secret) != INSTALLATION_SECRET_BYTES:
            raise DesktopEnrollmentBlocked("desktop installation binding is unavailable")
        return self.verify_for_installation_binding(
            envelope_text=envelope_text,
            installation_binding_sha256=sha256(installation_secret).hexdigest(),
        )

    def verify_for_installation_binding(
        self,
        *,
        envelope_text: str,
        installation_binding_sha256: str,
    ) -> DesktopEnrollment:
        """Verify for a binding digest computed inside the native Keychain boundary."""

        if not _SHA256_PATTERN.fullmatch(installation_binding_sha256):
            raise DesktopEnrollmentBlocked("desktop installation binding is unavailable")
        envelope = _strict_json_object(envelope_text)
        if frozenset(envelope) != _ENVELOPE_FIELDS:
            raise DesktopEnrollmentBlocked("desktop enrollment envelope fields are invalid")

        credential = envelope.get("credential")
        signature_text = envelope.get("signature")
        if not isinstance(credential, dict) or frozenset(credential) != _CREDENTIAL_FIELDS:
            raise DesktopEnrollmentBlocked("desktop enrollment credential fields are invalid")
        if not isinstance(signature_text, str) or not _BASE64URL_PATTERN.fullmatch(signature_text):
            raise DesktopEnrollmentBlocked("desktop enrollment signature encoding is invalid")

        key_id = _required_identifier(credential, "key_id")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise DesktopEnrollmentBlocked("desktop enrollment clock must use UTC")
        trusted = (
            self._trusted_catalog.resolve(key_id, now=now)
            if self._trusted_catalog is not None
            else self._trusted_issuers.get(key_id)
        )
        if trusted is None:
            raise DesktopEnrollmentBlocked("desktop enrollment issuer is not trusted")
        issuer = _required_string(credential, "issuer", maximum=200)
        if issuer != trusted.issuer:
            raise DesktopEnrollmentBlocked("desktop enrollment issuer is not trusted")

        canonical_bytes = _canonical_json_bytes(credential)
        try:
            signature = urlsafe_b64decode(signature_text + "==")
            Ed25519PublicKey.from_public_bytes(trusted.public_key_bytes).verify(
                signature,
                canonical_bytes,
            )
        except (InvalidSignature, ValueError) as error:
            raise DesktopEnrollmentBlocked("desktop enrollment signature is invalid") from error

        if type(credential.get("version")) is not int or credential["version"] != 1:
            raise DesktopEnrollmentBlocked("desktop enrollment version is not supported")
        enrollment_id = _required_uuid(credential, "enrollment_id")
        actor_id = _required_uuid(credential, "actor_id")
        firm_id = _required_uuid(credential, "firm_id")
        roles = _required_roles(credential)
        binding = credential.get("installation_binding_sha256")
        if not isinstance(binding, str) or not _SHA256_PATTERN.fullmatch(binding):
            raise DesktopEnrollmentBlocked("desktop installation binding is invalid")
        if not compare_digest(binding, installation_binding_sha256):
            raise DesktopEnrollmentBlocked("desktop enrollment belongs to another installation")

        issued_at = _required_utc_timestamp(credential, "issued_at")
        expires_at = _required_utc_timestamp(credential, "expires_at")
        if issued_at > now + MAX_CLOCK_SKEW:
            raise DesktopEnrollmentBlocked("desktop enrollment issue time is in the future")
        if expires_at <= now:
            raise DesktopEnrollmentBlocked("desktop enrollment has expired")
        if expires_at <= issued_at or expires_at - issued_at > MAX_ENROLLMENT_LIFETIME:
            raise DesktopEnrollmentBlocked("desktop enrollment lifetime is invalid")
        if self._trusted_catalog is not None:
            self._trusted_catalog.validate_enrollment_window(
                key_id=key_id,
                issued_at=issued_at,
                expires_at=expires_at,
            )

        return DesktopEnrollment(
            enrollment_id=enrollment_id,
            actor=Actor(actor_id=actor_id, firm_id=firm_id, roles=roles),
            issuer=issuer,
            key_id=key_id,
            installation_binding_sha256=binding,
            issued_at=issued_at,
            expires_at=expires_at,
        )


class MacOSKeychainDesktopEnrollmentProvider:
    """Read the signed enrollment and installation secret without modifying Keychain."""

    def __init__(
        self,
        *,
        service: str,
        enrollment_account: str,
        installation_secret_account: str,
        verifier: SignedDesktopEnrollmentVerifier,
        security_executable: str = "/usr/bin/security",
        platform_name: str | None = None,
        runner=None,
    ) -> None:
        normalized_values: dict[str, str] = {}
        for label, value in (
            ("service", service),
            ("enrollment account", enrollment_account),
            ("installation account", installation_secret_account),
        ):
            normalized = value.strip()
            if (
                not normalized
                or normalized != value
                or len(normalized) > 128
                or not _IDENTIFIER_PATTERN.fullmatch(normalized)
            ):
                raise DesktopEnrollmentBlocked(f"Keychain {label} is invalid")
            normalized_values[label] = normalized
        if normalized_values["enrollment account"] == normalized_values["installation account"]:
            raise DesktopEnrollmentBlocked("Keychain enrollment and installation accounts must differ")
        if security_executable != "/usr/bin/security":
            raise DesktopEnrollmentBlocked("only the fixed macOS security executable is allowed")
        self._service = normalized_values["service"]
        self._enrollment_account = normalized_values["enrollment account"]
        self._installation_secret_account = normalized_values["installation account"]
        self._verifier = verifier
        self._security_executable = security_executable
        self._platform_name = platform_name or sys.platform
        self._runner = runner or subprocess.run

    def load(self) -> DesktopEnrollment:
        enrollment = self.load_optional()
        if enrollment is None:
            raise DesktopEnrollmentBlocked(
                "desktop enrollment is unavailable in macOS Keychain"
            )
        return enrollment

    def load_optional(self) -> DesktopEnrollment | None:
        envelope_text = self._read_item(
            account=self._enrollment_account,
            maximum_bytes=MAX_ENROLLMENT_BYTES,
            unavailable_message="desktop enrollment is unavailable in macOS Keychain",
            missing_is_none=True,
        )
        if envelope_text is None:
            return None
        encoded_secret = self._read_item(
            account=self._installation_secret_account,
            maximum_bytes=128,
            unavailable_message="desktop installation binding is unavailable in macOS Keychain",
        )
        if encoded_secret is None:
            raise DesktopEnrollmentBlocked(
                "desktop installation binding is unavailable in macOS Keychain"
            )
        if any(character.isspace() for character in encoded_secret):
            raise DesktopEnrollmentBlocked("desktop installation binding has invalid encoding")
        try:
            installation_secret = b64decode(encoded_secret, validate=True)
        except (ValueError, UnicodeEncodeError) as error:
            raise DesktopEnrollmentBlocked("desktop installation binding has invalid encoding") from error
        return self._verifier.verify(
            envelope_text=envelope_text,
            installation_secret=installation_secret,
        )

    def _read_item(
        self,
        *,
        account: str,
        maximum_bytes: int,
        unavailable_message: str,
        missing_is_none: bool = False,
    ) -> str | None:
        if self._platform_name != "darwin":
            raise DesktopEnrollmentBlocked("macOS Keychain is unavailable on this platform")
        try:
            completed = self._runner(
                [
                    self._security_executable,
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopEnrollmentBlocked("macOS Keychain lookup failed") from error
        if completed.returncode == 44 and missing_is_none:
            return None
        if completed.returncode != 0:
            raise DesktopEnrollmentBlocked(unavailable_message)
        value = completed.stdout.strip()
        try:
            encoded_size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise DesktopEnrollmentBlocked("macOS Keychain item has invalid encoding") from error
        if not value or encoded_size > maximum_bytes:
            raise DesktopEnrollmentBlocked("macOS Keychain item has invalid size")
        return value


def _strict_json_object(value: str) -> dict[str, object]:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ENROLLMENT_BYTES:
            raise DesktopEnrollmentBlocked("desktop enrollment envelope size is invalid")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise DesktopEnrollmentBlocked("desktop enrollment contains duplicate fields")
                result[key] = item
            return result

        parsed = json.loads(value, object_pairs_hook=reject_duplicates)
    except DesktopEnrollmentBlocked:
        raise
    except (UnicodeEncodeError, json.JSONDecodeError) as error:
        raise DesktopEnrollmentBlocked("desktop enrollment envelope is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise DesktopEnrollmentBlocked("desktop enrollment envelope must be an object")
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
        raise DesktopEnrollmentBlocked("desktop enrollment credential is not canonicalizable") from error


def _required_string(credential: Mapping[str, object], field_name: str, *, maximum: int) -> str:
    value = credential.get(field_name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} is invalid")
    return value


def _required_identifier(credential: Mapping[str, object], field_name: str) -> str:
    value = _required_string(credential, field_name, maximum=128)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} is invalid")
    return value


def _required_uuid(credential: Mapping[str, object], field_name: str) -> str:
    value = credential.get(field_name)
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError as error:
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} must be UUID") from error
    if parsed is None or str(parsed) != value:
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} must be canonical UUID")
    return value


def _required_roles(credential: Mapping[str, object]) -> frozenset[Role]:
    raw_roles = credential.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles or len(raw_roles) > len(_PROFESSIONAL_ROLES):
        raise DesktopEnrollmentBlocked("desktop enrollment roles are invalid")
    if any(not isinstance(value, str) for value in raw_roles) or len(set(raw_roles)) != len(raw_roles):
        raise DesktopEnrollmentBlocked("desktop enrollment roles are invalid")
    try:
        roles = frozenset(Role(value) for value in raw_roles)
    except ValueError as error:
        raise DesktopEnrollmentBlocked("desktop enrollment role is not supported") from error
    if not roles or not roles.issubset(_PROFESSIONAL_ROLES):
        raise DesktopEnrollmentBlocked("desktop enrollment role is not available to a lawyer")
    return roles


def _required_utc_timestamp(credential: Mapping[str, object], field_name: str) -> datetime:
    value = credential.get(field_name)
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} must use UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} is invalid") from error
    if parsed.utcoffset() != timedelta(0):
        raise DesktopEnrollmentBlocked(f"desktop enrollment {field_name} must use UTC")
    return parsed
