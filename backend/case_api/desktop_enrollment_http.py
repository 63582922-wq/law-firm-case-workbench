"""Pinned HTTPS transport for the law-firm enrollment lifecycle service.

The browser never chooses the service origin or TLS pins. Both come from the
threshold-signed enrollment trust catalog. Requests are one-shot, bounded and
never automatically retried because registration and revocation are external
state changes.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
import http.client
import json
import re
import ssl
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .desktop_enrollment_lifecycle import (
    AuthenticatedFirmEnrollmentIssuer,
    DesktopEnrollmentLifecycleBlocked,
    EnrollmentOperationRemoteStatus,
    EnrollmentOperationStatusRequest,
    EnrollmentRegistrationRequest,
    EnrollmentRenewalRequest,
    EnrollmentRevocationReceipt,
    EnrollmentRevocationRequest,
)


MAX_REQUEST_BYTES = 20_000
MAX_RESPONSE_BYTES = 40_000
REQUEST_TIMEOUT_SECONDS = 10
_ALLOWED_PATHS = frozenset(
    {
        "/v1/desktop-enrollments/activate",
        "/v1/desktop-enrollments/renew",
        "/v1/desktop-enrollments/revoke",
        "/v1/desktop-enrollments/status",
    }
)
_DNS_NAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_ENVELOPE_RESPONSE_FIELDS = frozenset({"enrollment_envelope"})
_REVOCATION_RESPONSE_FIELDS = frozenset(
    {"revocation_id", "enrollment_id", "issuer", "effective_at", "accepted"}
)
_OPERATION_STATUS_RESPONSE_FIELDS = frozenset(
    {
        "operation_id",
        "operation_kind",
        "state",
        "enrollment_envelope",
        "revocation_receipt",
    }
)


class PinnedHttpsJsonTransport:
    def __init__(
        self,
        *,
        origin: str,
        tls_spki_sha256: tuple[str, ...],
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        parsed = urlsplit(origin)
        try:
            parsed_port = parsed.port
        except ValueError as error:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment service origin is invalid") from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not _DNS_NAME.fullmatch(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed_port is not None
            or origin != f"https://{parsed.hostname}"
        ):
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment service origin is invalid")
        if (
            not tls_spki_sha256
            or len(tls_spki_sha256) > 4
            or any(not _valid_spki_pin(pin) for pin in tls_spki_sha256)
            or tuple(sorted(set(tls_spki_sha256))) != tls_spki_sha256
        ):
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment TLS pins are invalid")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 15:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment timeout is invalid")
        self._host = parsed.hostname
        self._port = 443
        self._pins = tls_spki_sha256
        self._timeout = timeout_seconds
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def post_json(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        if path not in _ALLOWED_PATHS:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment service path is not allowed")
        try:
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment request is invalid") from error
        if not body or len(body) > MAX_REQUEST_BYTES:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment request size is invalid")

        connection = self._connection_factory(
            self._host,
            self._port,
            timeout=self._timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.connect()
            self._verify_peer_pin(connection)
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    "User-Agent": "lawcase-desktop-enrollment/1",
                },
            )
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if response.status != 200 or media_type != "application/json":
                raise DesktopEnrollmentLifecycleBlocked("firm enrollment service rejected the request")
            if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
                raise DesktopEnrollmentLifecycleBlocked("firm enrollment response size is invalid")
            return _strict_json_object(response_body)
        except DesktopEnrollmentLifecycleBlocked:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as error:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment service is unavailable") from error
        finally:
            connection.close()

    def _verify_peer_pin(self, connection: http.client.HTTPSConnection) -> None:
        socket = connection.sock
        if socket is None:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment TLS peer is unavailable")
        try:
            certificate_der = socket.getpeercert(binary_form=True)
            certificate = x509.load_der_x509_certificate(certificate_der)
            spki_der = certificate.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            actual = "sha256/" + b64encode(sha256(spki_der).digest()).decode("ascii")
        except (TypeError, ValueError) as error:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment TLS peer is invalid") from error
        if not any(compare_digest(actual, expected) for expected in self._pins):
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment TLS pin did not match")


class EnrollmentJsonTransport(Protocol):
    def post_json(self, path: str, payload: Mapping[str, object]) -> dict[str, object]: ...


class JsonFirmEnrollmentIssuer(AuthenticatedFirmEnrollmentIssuer):
    def __init__(self, transport: EnrollmentJsonTransport) -> None:
        self._transport = transport

    def register(self, request: EnrollmentRegistrationRequest) -> str:
        return self._envelope(
            self._transport.post_json(
                "/v1/desktop-enrollments/activate",
                asdict(request),
            )
        )

    def renew(self, request: EnrollmentRenewalRequest) -> str:
        return self._envelope(
            self._transport.post_json(
                "/v1/desktop-enrollments/renew",
                asdict(request),
            )
        )

    def revoke(self, request: EnrollmentRevocationRequest) -> EnrollmentRevocationReceipt:
        payload = self._transport.post_json(
            "/v1/desktop-enrollments/revoke",
            asdict(request),
        )
        if frozenset(payload) != _REVOCATION_RESPONSE_FIELDS:
            raise DesktopEnrollmentLifecycleBlocked("firm revocation response fields are invalid")
        return EnrollmentRevocationReceipt(
            revocation_id=_required_text(payload.get("revocation_id")),
            enrollment_id=_required_text(payload.get("enrollment_id")),
            issuer=_required_text(payload.get("issuer")),
            effective_at=_required_utc_timestamp(payload.get("effective_at")),
            accepted=payload.get("accepted") is True,
        )

    def query_operation(
        self,
        request: EnrollmentOperationStatusRequest,
    ) -> EnrollmentOperationRemoteStatus:
        payload = self._transport.post_json(
            "/v1/desktop-enrollments/status",
            asdict(request),
        )
        if frozenset(payload) != _OPERATION_STATUS_RESPONSE_FIELDS:
            raise DesktopEnrollmentLifecycleBlocked(
                "firm operation status response fields are invalid"
            )
        envelope = payload.get("enrollment_envelope")
        if envelope is not None and not isinstance(envelope, str):
            raise DesktopEnrollmentLifecycleBlocked(
                "firm operation status envelope is invalid"
            )
        receipt_payload = payload.get("revocation_receipt")
        receipt = None
        if receipt_payload is not None:
            if (
                not isinstance(receipt_payload, dict)
                or frozenset(receipt_payload) != _REVOCATION_RESPONSE_FIELDS
            ):
                raise DesktopEnrollmentLifecycleBlocked(
                    "firm operation status receipt is invalid"
                )
            receipt = EnrollmentRevocationReceipt(
                revocation_id=_required_text(receipt_payload.get("revocation_id")),
                enrollment_id=_required_text(receipt_payload.get("enrollment_id")),
                issuer=_required_text(receipt_payload.get("issuer")),
                effective_at=_required_utc_timestamp(receipt_payload.get("effective_at")),
                accepted=receipt_payload.get("accepted") is True,
            )
        status = EnrollmentOperationRemoteStatus(
            operation_id=_required_text(payload.get("operation_id")),
            operation_kind=_required_text(payload.get("operation_kind")),
            state=_required_text(payload.get("state")),
            enrollment_envelope=envelope,
            revocation_receipt=receipt,
        )
        status.validate(request=request)
        return status

    @staticmethod
    def _envelope(payload: dict[str, object]) -> str:
        if frozenset(payload) != _ENVELOPE_RESPONSE_FIELDS:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment response fields are invalid")
        envelope = payload.get("enrollment_envelope")
        if not isinstance(envelope, str) or not envelope or len(envelope.encode("utf-8")) > 16_384:
            raise DesktopEnrollmentLifecycleBlocked("firm enrollment response is invalid")
        return envelope


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DesktopEnrollmentLifecycleBlocked("firm enrollment response contains duplicate fields")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=reject_duplicates)
    except DesktopEnrollmentLifecycleBlocked:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response must be an object")
    return parsed


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response text is invalid")
    return value


def _required_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response time is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DesktopEnrollmentLifecycleBlocked("firm enrollment response time is invalid")
    return parsed


def _valid_spki_pin(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256/") or any(character.isspace() for character in value):
        return False
    try:
        return len(b64decode(value[7:], validate=True)) == 32
    except (ValueError, UnicodeEncodeError):
        return False
