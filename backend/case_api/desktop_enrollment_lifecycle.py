"""Enrollment, renewal and revocation orchestration for a desktop installation.

The browser never submits actor, firm or roles.  A one-time activation secret is
exchanged with an authenticated law-firm issuer, and the returned signed
credential is verified before a compare-and-set Keychain write.  Concrete
network and writable-Keychain adapters remain deployment responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from secrets import token_urlsafe
from typing import Protocol
from uuid import UUID

from .desktop_enrollment import (
    INSTALLATION_SECRET_BYTES,
    DesktopEnrollment,
    DesktopEnrollmentBlocked,
    SignedDesktopEnrollmentVerifier,
)


class DesktopEnrollmentLifecycleBlocked(PermissionError):
    """A registration, renewal or revocation operation failed closed."""


_OPAQUE_SECRET = re.compile(r"^[!-~]{24,256}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,160}$")
_REASON = re.compile(r"^[^\x00-\x1f\x7f]{2,200}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPERATION_KINDS = frozenset({"ACTIVATE", "RENEW", "REVOKE"})
_REMOTE_STATES = frozenset({"PENDING", "REJECTED", "SUCCEEDED"})


@dataclass(frozen=True)
class EnrollmentRegistrationRequest:
    activation_secret: str = field(repr=False)
    installation_binding_sha256: str
    client_nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_opaque_secret(self.activation_secret)
        _require_sha256(self.installation_binding_sha256)
        _require_nonce(self.client_nonce)


@dataclass(frozen=True)
class EnrollmentRenewalRequest:
    enrollment_id: str
    installation_binding_sha256: str
    current_envelope_sha256: str
    client_nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_uuid(self.enrollment_id, "enrollment_id")
        _require_sha256(self.installation_binding_sha256)
        _require_sha256(self.current_envelope_sha256)
        _require_nonce(self.client_nonce)


@dataclass(frozen=True)
class EnrollmentRevocationRequest:
    enrollment_id: str
    installation_binding_sha256: str
    current_envelope_sha256: str
    reason: str
    client_nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_uuid(self.enrollment_id, "enrollment_id")
        _require_sha256(self.installation_binding_sha256)
        _require_sha256(self.current_envelope_sha256)
        if not isinstance(self.reason, str) or not _REASON.fullmatch(self.reason) or self.reason != self.reason.strip():
            raise DesktopEnrollmentLifecycleBlocked("desktop enrollment revocation reason is invalid")
        _require_nonce(self.client_nonce)


@dataclass(frozen=True)
class EnrollmentRevocationReceipt:
    revocation_id: str
    enrollment_id: str
    issuer: str
    effective_at: datetime
    accepted: bool

    def validate(self, *, expected_enrollment_id: str, now: datetime) -> None:
        _require_uuid(self.revocation_id, "revocation_id")
        _require_uuid(self.enrollment_id, "enrollment_id")
        if self.enrollment_id != expected_enrollment_id:
            raise DesktopEnrollmentLifecycleBlocked("revocation receipt enrollment does not match")
        if not isinstance(self.issuer, str) or not _IDENTIFIER.fullmatch(self.issuer):
            raise DesktopEnrollmentLifecycleBlocked("revocation receipt issuer is invalid")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() != timedelta(0):
            raise DesktopEnrollmentLifecycleBlocked("revocation receipt time must use UTC")
        if self.effective_at > now + timedelta(minutes=5):
            raise DesktopEnrollmentLifecycleBlocked("revocation receipt time is in the future")
        if self.accepted is not True:
            raise DesktopEnrollmentLifecycleBlocked("remote revocation was not accepted")


@dataclass(frozen=True)
class EnrollmentOperationStatusRequest:
    operation_id: str
    operation_kind: str
    installation_binding_sha256: str
    current_envelope_sha256: str | None

    def __post_init__(self) -> None:
        _require_nonce(self.operation_id)
        _require_operation_kind(self.operation_kind)
        _require_sha256(self.installation_binding_sha256)
        if self.operation_kind == "ACTIVATE":
            if self.current_envelope_sha256 is not None:
                raise DesktopEnrollmentLifecycleBlocked(
                    "activation status cannot target an existing enrollment"
                )
        else:
            _require_sha256(self.current_envelope_sha256)


@dataclass(frozen=True)
class EnrollmentOperationRemoteStatus:
    operation_id: str
    operation_kind: str
    state: str
    enrollment_envelope: str | None
    revocation_receipt: EnrollmentRevocationReceipt | None

    def validate(self, *, request: EnrollmentOperationStatusRequest) -> None:
        _require_nonce(self.operation_id)
        _require_operation_kind(self.operation_kind)
        if self.operation_id != request.operation_id or self.operation_kind != request.operation_kind:
            raise DesktopEnrollmentLifecycleBlocked("remote operation status does not match request")
        if self.state not in _REMOTE_STATES:
            raise DesktopEnrollmentLifecycleBlocked("remote operation status is invalid")
        if self.state != "SUCCEEDED":
            if self.enrollment_envelope is not None or self.revocation_receipt is not None:
                raise DesktopEnrollmentLifecycleBlocked("unfinished operation returned result material")
            return
        if self.operation_kind in {"ACTIVATE", "RENEW"}:
            if not isinstance(self.enrollment_envelope, str) or not self.enrollment_envelope:
                raise DesktopEnrollmentLifecycleBlocked("successful operation envelope is unavailable")
            if self.revocation_receipt is not None:
                raise DesktopEnrollmentLifecycleBlocked("successful enrollment operation returned a receipt")
        elif self.enrollment_envelope is not None or not isinstance(
            self.revocation_receipt, EnrollmentRevocationReceipt
        ):
            raise DesktopEnrollmentLifecycleBlocked("successful revocation result is invalid")


@dataclass(frozen=True)
class EnrollmentOperationResult:
    status: str
    enrollment_id: str | None
    actor_id: str | None
    firm_id: str | None
    expires_at: datetime | None
    remote_revocation_confirmed: bool


@dataclass(frozen=True)
class EnrollmentOperationResolution:
    operation_id: str
    operation_kind: str
    state: str
    result: EnrollmentOperationResult | None


class AuthenticatedFirmEnrollmentIssuer(Protocol):
    """Transport configured outside the browser with firm-authenticated TLS."""

    def register(self, request: EnrollmentRegistrationRequest) -> str: ...

    def renew(self, request: EnrollmentRenewalRequest) -> str: ...

    def revoke(self, request: EnrollmentRevocationRequest) -> EnrollmentRevocationReceipt: ...

    def query_operation(
        self,
        request: EnrollmentOperationStatusRequest,
    ) -> EnrollmentOperationRemoteStatus: ...


class DesktopEnrollmentCredentialVault(Protocol):
    """Writable native vault with compare-and-set semantics.

    Implementations must keep the installation secret and signed envelope in
    separate access-controlled entries.  They must never expose either to the
    webview or command-line arguments.
    """

    def installation_secret(self) -> bytes: ...

    def current_envelope(self) -> str: ...

    def current_envelope_optional(self) -> str | None: ...

    def replace_enrollment(self, *, expected_sha256: str | None, envelope_text: str) -> None: ...

    def delete_enrollment(self, *, expected_sha256: str) -> None: ...


class DesktopEnrollmentLifecycle:
    def __init__(
        self,
        *,
        issuer: AuthenticatedFirmEnrollmentIssuer,
        vault: DesktopEnrollmentCredentialVault,
        verifier: SignedDesktopEnrollmentVerifier,
        clock=None,
        nonce_factory=None,
    ) -> None:
        self._issuer = issuer
        self._vault = vault
        self._verifier = verifier
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce_factory = nonce_factory or (lambda: token_urlsafe(48))

    def register(self, *, activation_secret: str) -> EnrollmentOperationResult:
        secret = self._installation_secret()
        request = EnrollmentRegistrationRequest(
            activation_secret=activation_secret,
            installation_binding_sha256=sha256(secret).hexdigest(),
            client_nonce=self._nonce(),
        )
        envelope = self._issuer_call(lambda: self._issuer.register(request), "registration")
        enrollment = self._verify_envelope(envelope, secret=secret)
        self._vault_call(
            lambda: self._vault.replace_enrollment(expected_sha256=None, envelope_text=envelope),
            "registration",
        )
        return _result("REGISTERED", enrollment, remote_revocation_confirmed=False)

    def renew(self) -> EnrollmentOperationResult:
        secret = self._installation_secret()
        current_envelope = self._current_envelope()
        current_hash = _envelope_hash(current_envelope)
        current = self._verify_envelope(current_envelope, secret=secret)
        request = EnrollmentRenewalRequest(
            enrollment_id=current.enrollment_id,
            installation_binding_sha256=sha256(secret).hexdigest(),
            current_envelope_sha256=current_hash,
            client_nonce=self._nonce(),
        )
        renewed_envelope = self._issuer_call(lambda: self._issuer.renew(request), "renewal")
        renewed = self._verify_envelope(renewed_envelope, secret=secret)
        if (
            renewed.enrollment_id != current.enrollment_id
            or renewed.actor.actor_id != current.actor.actor_id
            or renewed.actor.firm_id != current.actor.firm_id
        ):
            raise DesktopEnrollmentLifecycleBlocked("renewed desktop identity does not match current enrollment")
        if renewed.expires_at <= current.expires_at:
            raise DesktopEnrollmentLifecycleBlocked("renewed desktop enrollment does not extend expiry")
        self._vault_call(
            lambda: self._vault.replace_enrollment(
                expected_sha256=current_hash,
                envelope_text=renewed_envelope,
            ),
            "renewal",
        )
        return _result("RENEWED", renewed, remote_revocation_confirmed=False)

    def revoke(self, *, reason: str) -> EnrollmentOperationResult:
        secret = self._installation_secret()
        current_envelope = self._current_envelope()
        current_hash = _envelope_hash(current_envelope)
        current = self._verify_envelope(current_envelope, secret=secret)
        request = EnrollmentRevocationRequest(
            enrollment_id=current.enrollment_id,
            installation_binding_sha256=sha256(secret).hexdigest(),
            current_envelope_sha256=current_hash,
            reason=reason,
            client_nonce=self._nonce(),
        )
        receipt = self._issuer_call(lambda: self._issuer.revoke(request), "revocation")
        if not isinstance(receipt, EnrollmentRevocationReceipt):
            raise DesktopEnrollmentLifecycleBlocked("remote revocation receipt is invalid")
        receipt.validate(expected_enrollment_id=current.enrollment_id, now=self._now())
        self._vault_call(
            lambda: self._vault.delete_enrollment(expected_sha256=current_hash),
            "revocation",
        )
        return _result("REVOKED", current, remote_revocation_confirmed=True, expires_at=None)

    def disable_local(self) -> EnrollmentOperationResult:
        """Remove local use without pretending the firm has revoked the identity."""

        current_envelope = self._current_envelope()
        current_hash = _envelope_hash(current_envelope)
        self._vault_call(
            lambda: self._vault.delete_enrollment(expected_sha256=current_hash),
            "local disable",
        )
        return EnrollmentOperationResult(
            status="LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED",
            enrollment_id=None,
            actor_id=None,
            firm_id=None,
            expires_at=None,
            remote_revocation_confirmed=False,
        )

    def resolve_remote_operation(
        self,
        *,
        operation_id: str,
        operation_kind: str,
    ) -> EnrollmentOperationResolution:
        _require_nonce(operation_id)
        _require_operation_kind(operation_kind)
        secret = self._installation_secret()
        current_envelope = self._current_envelope_optional()
        if operation_kind == "ACTIVATE" and current_envelope is not None:
            raise DesktopEnrollmentLifecycleBlocked(
                "activation status cannot overwrite an existing enrollment"
            )
        if operation_kind != "ACTIVATE" and current_envelope is None:
            raise DesktopEnrollmentLifecycleBlocked("current desktop enrollment is unavailable")
        current_hash = (
            _envelope_hash(current_envelope) if current_envelope is not None else None
        )
        request = EnrollmentOperationStatusRequest(
            operation_id=operation_id,
            operation_kind=operation_kind,
            installation_binding_sha256=sha256(secret).hexdigest(),
            current_envelope_sha256=current_hash,
        )
        remote = self._issuer_call(
            lambda: self._issuer.query_operation(request),
            "operation status",
        )
        if not isinstance(remote, EnrollmentOperationRemoteStatus):
            raise DesktopEnrollmentLifecycleBlocked("remote operation status is invalid")
        remote.validate(request=request)
        if remote.state != "SUCCEEDED":
            return EnrollmentOperationResolution(
                operation_id=operation_id,
                operation_kind=operation_kind,
                state=remote.state,
                result=None,
            )

        if operation_kind == "ACTIVATE":
            envelope = remote.enrollment_envelope
            enrollment = self._verify_envelope(envelope, secret=secret)
            self._vault_call(
                lambda: self._vault.replace_enrollment(
                    expected_sha256=None,
                    envelope_text=envelope,
                ),
                "registration recovery",
            )
            result = _result(
                "REGISTERED",
                enrollment,
                remote_revocation_confirmed=False,
            )
        else:
            if current_envelope is None or current_hash is None:
                raise DesktopEnrollmentLifecycleBlocked(
                    "current desktop enrollment is unavailable"
                )
            current = self._verify_envelope(current_envelope, secret=secret)
            if operation_kind == "RENEW":
                renewed_envelope = remote.enrollment_envelope
                renewed = self._verify_envelope(renewed_envelope, secret=secret)
                if (
                    renewed.enrollment_id != current.enrollment_id
                    or renewed.actor.actor_id != current.actor.actor_id
                    or renewed.actor.firm_id != current.actor.firm_id
                    or renewed.expires_at <= current.expires_at
                ):
                    raise DesktopEnrollmentLifecycleBlocked(
                        "recovered renewal does not match current enrollment"
                    )
                self._vault_call(
                    lambda: self._vault.replace_enrollment(
                        expected_sha256=current_hash,
                        envelope_text=renewed_envelope,
                    ),
                    "renewal recovery",
                )
                result = _result(
                    "RENEWED",
                    renewed,
                    remote_revocation_confirmed=False,
                )
            else:
                receipt = remote.revocation_receipt
                if not isinstance(receipt, EnrollmentRevocationReceipt):
                    raise DesktopEnrollmentLifecycleBlocked(
                        "recovered revocation receipt is invalid"
                    )
                receipt.validate(
                    expected_enrollment_id=current.enrollment_id,
                    now=self._now(),
                )
                self._vault_call(
                    lambda: self._vault.delete_enrollment(
                        expected_sha256=current_hash,
                    ),
                    "revocation recovery",
                )
                result = _result(
                    "REVOKED",
                    current,
                    remote_revocation_confirmed=True,
                    expires_at=None,
                )
        return EnrollmentOperationResolution(
            operation_id=operation_id,
            operation_kind=operation_kind,
            state="SUCCEEDED",
            result=result,
        )

    def _installation_secret(self) -> bytes:
        try:
            secret = self._vault.installation_secret()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked("desktop installation binding is unavailable") from error
        if not isinstance(secret, bytes) or len(secret) != INSTALLATION_SECRET_BYTES:
            raise DesktopEnrollmentLifecycleBlocked("desktop installation binding is unavailable")
        return secret

    def _current_envelope(self) -> str:
        try:
            envelope = self._vault.current_envelope()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked("current desktop enrollment is unavailable") from error
        if not isinstance(envelope, str) or not envelope:
            raise DesktopEnrollmentLifecycleBlocked("current desktop enrollment is unavailable")
        return envelope

    def _current_envelope_optional(self) -> str | None:
        try:
            envelope = self._vault.current_envelope_optional()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked(
                "current desktop enrollment state is unavailable"
            ) from error
        if envelope is not None and (not isinstance(envelope, str) or not envelope):
            raise DesktopEnrollmentLifecycleBlocked(
                "current desktop enrollment state is unavailable"
            )
        return envelope

    def _verify_envelope(self, envelope: object, *, secret: bytes) -> DesktopEnrollment:
        if not isinstance(envelope, str):
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment response is invalid")
        try:
            return self._verifier.verify(envelope_text=envelope, installation_secret=secret)
        except DesktopEnrollmentBlocked as error:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment response failed signature verification") from error

    def _nonce(self) -> str:
        try:
            nonce = self._nonce_factory()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked("desktop enrollment nonce generation failed") from error
        _require_nonce(nonce)
        return nonce

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise DesktopEnrollmentLifecycleBlocked("desktop enrollment lifecycle clock must use UTC")
        return now

    @staticmethod
    def _issuer_call(operation, label: str):
        try:
            return operation()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked(f"firm enrollment {label} service is unavailable") from error

    @staticmethod
    def _vault_call(operation, label: str) -> None:
        try:
            operation()
        except Exception as error:
            raise DesktopEnrollmentLifecycleBlocked(f"desktop enrollment {label} was not committed") from error


_USE_ENROLLMENT_EXPIRY = object()


def _result(
    status: str,
    enrollment: DesktopEnrollment,
    *,
    remote_revocation_confirmed: bool,
    expires_at: datetime | None | object = _USE_ENROLLMENT_EXPIRY,
) -> EnrollmentOperationResult:
    resolved_expiry = enrollment.expires_at if expires_at is _USE_ENROLLMENT_EXPIRY else expires_at
    if resolved_expiry is not None and not isinstance(resolved_expiry, datetime):
        raise DesktopEnrollmentLifecycleBlocked("desktop enrollment result expiry is invalid")
    return EnrollmentOperationResult(
        status=status,
        enrollment_id=enrollment.enrollment_id,
        actor_id=enrollment.actor.actor_id,
        firm_id=enrollment.actor.firm_id,
        expires_at=resolved_expiry,
        remote_revocation_confirmed=remote_revocation_confirmed,
    )


def _require_opaque_secret(value: object) -> None:
    if not isinstance(value, str) or not _OPAQUE_SECRET.fullmatch(value) or any(character.isspace() for character in value):
        raise DesktopEnrollmentLifecycleBlocked("desktop activation secret is invalid")


def _require_nonce(value: object) -> None:
    if not isinstance(value, str) or not _NONCE.fullmatch(value):
        raise DesktopEnrollmentLifecycleBlocked("desktop enrollment nonce is invalid")


def _require_operation_kind(value: object) -> None:
    if not isinstance(value, str) or value not in _OPERATION_KINDS:
        raise DesktopEnrollmentLifecycleBlocked("desktop enrollment operation kind is invalid")


def _require_sha256(value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise DesktopEnrollmentLifecycleBlocked("desktop enrollment digest is invalid")


def _require_uuid(value: object, field_name: str) -> None:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError as error:
        raise DesktopEnrollmentLifecycleBlocked(f"desktop enrollment {field_name} is invalid") from error
    if parsed is None or str(parsed) != value:
        raise DesktopEnrollmentLifecycleBlocked(f"desktop enrollment {field_name} is invalid")


def _envelope_hash(envelope: str) -> str:
    try:
        encoded = envelope.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DesktopEnrollmentLifecycleBlocked("desktop enrollment envelope encoding is invalid") from error
    return sha256(encoded).hexdigest()
