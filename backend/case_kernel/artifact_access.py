"""Short-lived, one-use access to verified encrypted evidence derivatives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from ipaddress import ip_address
import re
from secrets import token_urlsafe
from threading import Lock
from uuid import UUID, uuid4

from .local_access_grants import LocalSessionProof
from .managed_artifact_store import LocalEncryptedArtifactStore
from .models import Actor, Role


class ArtifactAccessBlocked(PermissionError):
    """An artifact preview or download is not currently authorized."""


class ArtifactAccessPurpose(str, Enum):
    INLINE_PREVIEW = "INLINE_PREVIEW"
    DOWNLOAD = "DOWNLOAD"


@dataclass(frozen=True)
class VerifiedDerivativeLocator:
    firm_id: str
    matter_id: str
    derivative_id: str
    manifest_id: str
    artifact_type: str
    object_key: str = field(repr=False)
    artifact_sha256: str
    page_count: int
    status: str


@dataclass(frozen=True)
class IssuedArtifactAccess:
    grant_id: str
    access_token: str = field(repr=False, compare=False)
    derivative_id: str
    purpose: ArtifactAccessPurpose
    expires_at: datetime


@dataclass(frozen=True)
class ArtifactDelivery:
    derivative_id: str
    purpose: ArtifactAccessPurpose
    media_type: str
    file_name: str
    artifact_sha256: str
    content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AccessRecord:
    grant_id: str
    token_hash: str
    actor_id: str
    firm_id: str
    matter_id: str
    session_id: str
    purpose: ArtifactAccessPurpose
    locator: VerifiedDerivativeLocator
    expires_at: datetime


_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)


class EphemeralArtifactAccessBroker:
    def __init__(
        self,
        *,
        preview_ttl: timedelta = timedelta(seconds=90),
        download_ttl: timedelta = timedelta(seconds=45),
        max_outstanding: int = 1_000,
    ) -> None:
        for label, value in (("preview_ttl", preview_ttl), ("download_ttl", download_ttl)):
            if value <= timedelta(0) or value > timedelta(minutes=2):
                raise ValueError(f"{label} must be between 1 second and 2 minutes")
        if max_outstanding < 1 or max_outstanding > 100_000:
            raise ValueError("max_outstanding is outside the supported boundary")
        self._preview_ttl = preview_ttl
        self._download_ttl = download_ttl
        self._max_outstanding = max_outstanding
        self._records: dict[str, _AccessRecord] = {}
        self._lock = Lock()

    def issue(
        self,
        *,
        locator: VerifiedDerivativeLocator,
        purpose: ArtifactAccessPurpose,
        actor: Actor,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> IssuedArtifactAccess:
        current = _aware_now(now)
        _validate_access_identity(actor, session=session, matter_id=locator.matter_id, now=current)
        _validate_locator(locator, actor=actor)
        ttl = self._preview_ttl if purpose is ArtifactAccessPurpose.INLINE_PREVIEW else self._download_ttl
        expires_at = min(current + ttl, session.expires_at)
        if expires_at <= current:
            raise ArtifactAccessBlocked("the authenticated session expires before artifact access can be issued")
        token = token_urlsafe(32)
        token_hash = sha256(token.encode("ascii")).hexdigest()
        record = _AccessRecord(
            grant_id=str(uuid4()),
            token_hash=token_hash,
            actor_id=actor.actor_id,
            firm_id=actor.firm_id,
            matter_id=locator.matter_id,
            session_id=session.session_id,
            purpose=purpose,
            locator=locator,
            expires_at=expires_at,
        )
        with self._lock:
            self._remove_expired(current)
            if len(self._records) >= self._max_outstanding:
                raise ArtifactAccessBlocked("too many outstanding artifact access grants")
            self._records[token_hash] = record
        return IssuedArtifactAccess(
            grant_id=record.grant_id,
            access_token=token,
            derivative_id=locator.derivative_id,
            purpose=purpose,
            expires_at=expires_at,
        )

    def deliver(
        self,
        *,
        access_token: str,
        actor: Actor,
        matter_id: str,
        derivative_id: str,
        session: LocalSessionProof,
        client_ip: str,
        artifact_store: LocalEncryptedArtifactStore,
        now: datetime | None = None,
    ) -> ArtifactDelivery:
        current = _aware_now(now)
        _validate_loopback(client_ip)
        _validate_access_identity(actor, session=session, matter_id=matter_id, now=current)
        if not 20 <= len(access_token) <= 200 or not access_token.isascii():
            raise ArtifactAccessBlocked("artifact access token is invalid")
        token_hash = sha256(access_token.encode("ascii")).hexdigest()
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(token_hash)
            if record is None:
                raise ArtifactAccessBlocked("artifact access token is missing, expired, or already used")
            if (
                record.actor_id != actor.actor_id
                or record.firm_id != actor.firm_id
                or record.matter_id != matter_id
                or record.session_id != session.session_id
                or record.locator.derivative_id != derivative_id
            ):
                raise ArtifactAccessBlocked("artifact access token is outside the authenticated scope")
            del self._records[token_hash]
        content = artifact_store.read_bytes(
            record.locator.object_key,
            expected_sha256=record.locator.artifact_sha256,
        )
        file_name = (
            "related-pages-red-box.pdf"
            if record.locator.artifact_type == "ANNOTATED_RELATED_PAGES_PDF"
            else "related-pages.pdf"
        )
        return ArtifactDelivery(
            derivative_id=record.locator.derivative_id,
            purpose=record.purpose,
            media_type="application/pdf",
            file_name=file_name,
            artifact_sha256=record.locator.artifact_sha256,
            content=content,
        )

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            hashes = [
                token_hash
                for token_hash, record in self._records.items()
                if record.session_id == session_id
            ]
            for token_hash in hashes:
                del self._records[token_hash]
        return len(hashes)

    def _remove_expired(self, current: datetime) -> None:
        for token_hash in [
            token_hash
            for token_hash, record in self._records.items()
            if record.expires_at <= current
        ]:
            del self._records[token_hash]


def _validate_access_identity(
    actor: Actor,
    *,
    session: LocalSessionProof,
    matter_id: str,
    now: datetime,
) -> None:
    for label, value in (
        ("actor_id", actor.actor_id),
        ("firm_id", actor.firm_id),
        ("matter_id", matter_id),
        ("session_id", session.session_id),
    ):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise ArtifactAccessBlocked(f"artifact access requires UUID {label}") from error
    if not actor.roles.intersection(_READ_ROLES) or actor.roles.intersection(
        {Role.FIRM_ADMIN, Role.SYSTEM_WORKER}
    ):
        raise ArtifactAccessBlocked("the current role cannot preview or download evidence artifacts")
    if session.authentication_method != "OS_BOUND_LOCAL_SESSION":
        raise ArtifactAccessBlocked("artifact access requires an OS-bound local session")
    if session.authenticated_at.tzinfo is None or session.expires_at.tzinfo is None:
        raise ArtifactAccessBlocked("artifact access session timestamps must be timezone-aware")
    if session.authenticated_at > now or session.expires_at <= now:
        raise ArtifactAccessBlocked("the artifact access session is not currently valid")


def _validate_locator(locator: VerifiedDerivativeLocator, *, actor: Actor) -> None:
    for label, value in (
        ("firm_id", locator.firm_id),
        ("matter_id", locator.matter_id),
        ("derivative_id", locator.derivative_id),
        ("manifest_id", locator.manifest_id),
    ):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise ArtifactAccessBlocked(f"verified artifact requires UUID {label}") from error
    if locator.firm_id != actor.firm_id:
        raise ArtifactAccessBlocked("verified artifact is outside the authenticated firm")
    if locator.status != "VERIFIED":
        raise ArtifactAccessBlocked("only a currently verified evidence derivative can be accessed")
    if locator.artifact_type not in {"RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"}:
        raise ArtifactAccessBlocked("verified artifact type is not previewable")
    if locator.page_count < 1:
        raise ArtifactAccessBlocked("verified artifact page count is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", locator.artifact_sha256):
        raise ArtifactAccessBlocked("verified artifact hash is invalid")
    expected_key = (
        f"{locator.artifact_sha256[:2]}/{locator.artifact_sha256[2:4]}/"
        f"{locator.artifact_sha256}.lca"
    )
    if locator.object_key != expected_key:
        raise ArtifactAccessBlocked("verified artifact storage key is not hash-bound")


def _validate_loopback(client_ip: str) -> None:
    try:
        address = ip_address(client_ip)
    except ValueError as error:
        raise ArtifactAccessBlocked("artifact delivery requires a numeric loopback client address") from error
    if not address.is_loopback:
        raise ArtifactAccessBlocked("artifact delivery is restricted to the local loopback interface")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ArtifactAccessBlocked("artifact access time must be timezone-aware")
    return current
