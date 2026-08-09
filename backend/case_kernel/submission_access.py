"""One-use, loopback-only download grants for a verified court ZIP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from ipaddress import ip_address
import re
from secrets import token_urlsafe
from threading import Lock
from uuid import UUID, uuid4

from .local_access_grants import LocalSessionProof
from .managed_artifact_store import LocalEncryptedArtifactStore
from .models import Actor, Role


class SubmissionAccessBlocked(PermissionError):
    """The verified court ZIP is not downloadable in this scope."""


@dataclass(frozen=True)
class VerifiedSubmissionExportLocator:
    firm_id: str
    matter_id: str
    export_id: str
    bundle_id: str
    object_key: str = field(repr=False)
    court_zip_sha256: str
    court_zip_bytes: int
    lifecycle: str
    validity: str


@dataclass(frozen=True)
class IssuedSubmissionAccess:
    grant_id: str
    access_token: str = field(repr=False, compare=False)
    export_id: str
    expires_at: datetime


@dataclass(frozen=True)
class SubmissionDelivery:
    export_id: str
    file_name: str
    media_type: str
    artifact_sha256: str
    content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AccessRecord:
    token_hash: str
    actor_id: str
    firm_id: str
    matter_id: str
    session_id: str
    locator: VerifiedSubmissionExportLocator
    expires_at: datetime


_EXPORT_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})


class SubmissionExportAccessBroker:
    def __init__(
        self,
        *,
        download_ttl: timedelta = timedelta(seconds=30),
        max_outstanding: int = 500,
    ) -> None:
        if download_ttl <= timedelta(0) or download_ttl > timedelta(minutes=1):
            raise ValueError("submission download TTL must be between 1 second and 1 minute")
        if max_outstanding < 1 or max_outstanding > 10_000:
            raise ValueError("submission outstanding grant limit is invalid")
        self._download_ttl = download_ttl
        self._max_outstanding = max_outstanding
        self._records: dict[str, _AccessRecord] = {}
        self._lock = Lock()

    def issue(
        self,
        *,
        locator: VerifiedSubmissionExportLocator,
        actor: Actor,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> IssuedSubmissionAccess:
        current = _aware_now(now)
        _validate_identity(actor, session=session, matter_id=locator.matter_id, now=current)
        _validate_locator(locator, actor=actor)
        expires_at = min(current + self._download_ttl, session.expires_at)
        if expires_at <= current:
            raise SubmissionAccessBlocked("session expires before court ZIP access can be issued")
        token = token_urlsafe(32)
        token_hash = sha256(token.encode("ascii")).hexdigest()
        record = _AccessRecord(
            token_hash=token_hash,
            actor_id=actor.actor_id,
            firm_id=actor.firm_id,
            matter_id=locator.matter_id,
            session_id=session.session_id,
            locator=locator,
            expires_at=expires_at,
        )
        with self._lock:
            self._remove_expired(current)
            if len(self._records) >= self._max_outstanding:
                raise SubmissionAccessBlocked("too many outstanding court ZIP access grants")
            self._records[token_hash] = record
        return IssuedSubmissionAccess(
            grant_id=str(uuid4()),
            access_token=token,
            export_id=locator.export_id,
            expires_at=expires_at,
        )

    def deliver(
        self,
        *,
        access_token: str,
        actor: Actor,
        matter_id: str,
        export_id: str,
        session: LocalSessionProof,
        client_ip: str,
        artifact_store: LocalEncryptedArtifactStore,
        now: datetime | None = None,
    ) -> SubmissionDelivery:
        current = _aware_now(now)
        _validate_loopback(client_ip)
        _validate_identity(actor, session=session, matter_id=matter_id, now=current)
        if not 20 <= len(access_token) <= 200 or not access_token.isascii():
            raise SubmissionAccessBlocked("court ZIP access token is invalid")
        token_hash = sha256(access_token.encode("ascii")).hexdigest()
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(token_hash)
            if record is None:
                raise SubmissionAccessBlocked(
                    "court ZIP access token is missing, expired, or already used"
                )
            if (
                record.actor_id != actor.actor_id
                or record.firm_id != actor.firm_id
                or record.matter_id != matter_id
                or record.session_id != session.session_id
                or record.locator.export_id != export_id
            ):
                raise SubmissionAccessBlocked("court ZIP token is outside the authenticated scope")
            del self._records[token_hash]
        content = artifact_store.read_bytes(
            record.locator.object_key,
            expected_sha256=record.locator.court_zip_sha256,
        )
        if len(content) != record.locator.court_zip_bytes or not content.startswith(b"PK"):
            raise SubmissionAccessBlocked("verified court ZIP content is invalid")
        return SubmissionDelivery(
            export_id=export_id,
            file_name="法院提交材料.zip",
            media_type="application/zip",
            artifact_sha256=record.locator.court_zip_sha256,
            content=content,
        )

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            matches = [key for key, record in self._records.items() if record.session_id == session_id]
            for key in matches:
                del self._records[key]
        return len(matches)

    def _remove_expired(self, current: datetime) -> None:
        for token_hash in [
            key for key, record in self._records.items() if record.expires_at <= current
        ]:
            del self._records[token_hash]


def _validate_identity(
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
            raise SubmissionAccessBlocked(f"court ZIP access requires UUID {label}") from error
    if not actor.roles.intersection(_EXPORT_ROLES) or actor.roles.intersection(
        {Role.FIRM_ADMIN, Role.SYSTEM_WORKER}
    ):
        raise SubmissionAccessBlocked("current role cannot download a court submission ZIP")
    if session.authentication_method != "OS_BOUND_LOCAL_SESSION":
        raise SubmissionAccessBlocked("court ZIP download requires an OS-bound local session")
    if session.authenticated_at.tzinfo is None or session.expires_at.tzinfo is None:
        raise SubmissionAccessBlocked("court ZIP session timestamps must be timezone-aware")
    if session.authenticated_at > now or session.expires_at <= now:
        raise SubmissionAccessBlocked("court ZIP session is not currently valid")


def _validate_locator(locator: VerifiedSubmissionExportLocator, *, actor: Actor) -> None:
    for label, value in (
        ("firm_id", locator.firm_id),
        ("matter_id", locator.matter_id),
        ("export_id", locator.export_id),
        ("bundle_id", locator.bundle_id),
    ):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise SubmissionAccessBlocked(f"verified court export requires UUID {label}") from error
    if locator.firm_id != actor.firm_id:
        raise SubmissionAccessBlocked("verified court export is outside the authenticated firm")
    if locator.lifecycle != "EXPORTED" or locator.validity != "VALID":
        raise SubmissionAccessBlocked("only the current valid exported submission is downloadable")
    if not re.fullmatch(r"[0-9a-f]{64}", locator.court_zip_sha256):
        raise SubmissionAccessBlocked("verified court ZIP hash is invalid")
    if locator.court_zip_bytes < 1 or locator.court_zip_bytes > 256 * 1024 * 1024:
        raise SubmissionAccessBlocked("verified court ZIP byte size is invalid")
    expected = (
        f"{locator.court_zip_sha256[:2]}/{locator.court_zip_sha256[2:4]}/"
        f"{locator.court_zip_sha256}.lca"
    )
    if locator.object_key != expected:
        raise SubmissionAccessBlocked("verified court ZIP object key is not hash-bound")


def _validate_loopback(client_ip: str) -> None:
    try:
        address = ip_address(client_ip)
    except ValueError as error:
        raise SubmissionAccessBlocked("court ZIP delivery requires a numeric loopback address") from error
    if not address.is_loopback:
        raise SubmissionAccessBlocked("court ZIP delivery is restricted to local loopback")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise SubmissionAccessBlocked("court ZIP access time must be timezone-aware")
    return current
