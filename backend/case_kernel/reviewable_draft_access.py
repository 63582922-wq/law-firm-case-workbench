"""One-use local access grants for a reviewable Office draft pair.

The editable Office source and rendered PDF preview stay encrypted until a
lawyer/reviewer asks for exactly one local view or download.  This access
module deliberately does not share the evidence-derivative broker because a
draft candidate is neither evidence nor a court submission work product.
"""

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


class ReviewableDraftAccessBlocked(PermissionError):
    """A draft preview/download request is outside the local review scope."""


class ReviewableDraftAccessPurpose(str, Enum):
    REVIEW_PDF = "REVIEW_PDF"
    DOWNLOAD_EDITABLE = "DOWNLOAD_EDITABLE"


@dataclass(frozen=True)
class ReviewableOfficeDraftArtifactLocator:
    firm_id: str
    matter_id: str
    pair_id: str
    purpose: ReviewableDraftAccessPurpose
    media_type: str
    object_key: str = field(repr=False)
    artifact_sha256: str
    byte_size: int
    pair_status: str


@dataclass(frozen=True)
class IssuedReviewableDraftAccess:
    grant_id: str
    access_token: str = field(repr=False, compare=False)
    pair_id: str
    purpose: ReviewableDraftAccessPurpose
    expires_at: datetime


@dataclass(frozen=True)
class ReviewableDraftDelivery:
    pair_id: str
    purpose: ReviewableDraftAccessPurpose
    media_type: str
    file_name: str
    artifact_sha256: str
    content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AccessRecord:
    token_hash: str
    actor_id: str
    firm_id: str
    matter_id: str
    session_id: str
    locator: ReviewableOfficeDraftArtifactLocator
    expires_at: datetime


_READ_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReviewableOfficeDraftAccessBroker:
    def __init__(self, *, preview_ttl: timedelta = timedelta(seconds=90), download_ttl: timedelta = timedelta(seconds=45), max_outstanding: int = 500) -> None:
        for label, value in (("preview_ttl", preview_ttl), ("download_ttl", download_ttl)):
            if value <= timedelta(0) or value > timedelta(minutes=2):
                raise ValueError(f"{label} must be between 1 second and 2 minutes")
        if not 1 <= max_outstanding <= 10_000:
            raise ValueError("reviewable draft outstanding grant limit is invalid")
        self._preview_ttl = preview_ttl
        self._download_ttl = download_ttl
        self._max_outstanding = max_outstanding
        self._records: dict[str, _AccessRecord] = {}
        self._lock = Lock()

    def issue(self, *, locator: ReviewableOfficeDraftArtifactLocator, actor: Actor, session: LocalSessionProof, now: datetime | None = None) -> IssuedReviewableDraftAccess:
        current = _aware_now(now)
        _validate_identity(actor, session=session, matter_id=locator.matter_id, now=current)
        _validate_locator(locator, actor=actor)
        ttl = self._preview_ttl if locator.purpose is ReviewableDraftAccessPurpose.REVIEW_PDF else self._download_ttl
        expires_at = min(current + ttl, session.expires_at)
        if expires_at <= current:
            raise ReviewableDraftAccessBlocked("session expires before draft access can be issued")
        token = token_urlsafe(32)
        token_hash = sha256(token.encode("ascii")).hexdigest()
        record = _AccessRecord(token_hash, actor.actor_id, actor.firm_id, locator.matter_id, session.session_id, locator, expires_at)
        with self._lock:
            self._remove_expired(current)
            if len(self._records) >= self._max_outstanding:
                raise ReviewableDraftAccessBlocked("too many outstanding draft access grants")
            self._records[token_hash] = record
        return IssuedReviewableDraftAccess(str(uuid4()), token, locator.pair_id, locator.purpose, expires_at)

    def deliver(self, *, access_token: str, actor: Actor, matter_id: str, pair_id: str, session: LocalSessionProof, client_ip: str, artifact_store: LocalEncryptedArtifactStore, now: datetime | None = None) -> ReviewableDraftDelivery:
        current = _aware_now(now)
        _validate_loopback(client_ip)
        _validate_identity(actor, session=session, matter_id=matter_id, now=current)
        if not 20 <= len(access_token) <= 200 or not access_token.isascii():
            raise ReviewableDraftAccessBlocked("draft access token is invalid")
        token_hash = sha256(access_token.encode("ascii")).hexdigest()
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(token_hash)
            if record is None:
                raise ReviewableDraftAccessBlocked("draft access token is missing, expired, or already used")
            if (record.actor_id, record.firm_id, record.matter_id, record.session_id, record.locator.pair_id) != (actor.actor_id, actor.firm_id, matter_id, session.session_id, pair_id):
                raise ReviewableDraftAccessBlocked("draft access token is outside the authenticated scope")
            del self._records[token_hash]
        content = artifact_store.read_bytes(record.locator.object_key, expected_sha256=record.locator.artifact_sha256)
        if len(content) != record.locator.byte_size or sha256(content).hexdigest() != record.locator.artifact_sha256:
            raise ReviewableDraftAccessBlocked("reviewable draft encrypted content verification failed")
        if record.locator.purpose is ReviewableDraftAccessPurpose.REVIEW_PDF:
            if not content.startswith(b"%PDF-"):
                raise ReviewableDraftAccessBlocked("review PDF content is invalid")
            file_name = "文书审阅稿.pdf"
        elif record.locator.media_type.endswith("wordprocessingml.document"):
            if not content.startswith(b"PK"):
                raise ReviewableDraftAccessBlocked("editable Word content is invalid")
            file_name = "文书草稿.docx"
        else:
            if not content.startswith(b"PK"):
                raise ReviewableDraftAccessBlocked("editable Excel content is invalid")
            file_name = "核算草稿.xlsx"
        return ReviewableDraftDelivery(pair_id, record.locator.purpose, record.locator.media_type, file_name, record.locator.artifact_sha256, content)

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            matches = [key for key, record in self._records.items() if record.session_id == session_id]
            for key in matches:
                del self._records[key]
        return len(matches)

    def _remove_expired(self, current: datetime) -> None:
        for token_hash in [key for key, record in self._records.items() if record.expires_at <= current]:
            del self._records[token_hash]


def _validate_identity(actor: Actor, *, session: LocalSessionProof, matter_id: str, now: datetime) -> None:
    for label, value in (("actor_id", actor.actor_id), ("firm_id", actor.firm_id), ("matter_id", matter_id), ("session_id", session.session_id)):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise ReviewableDraftAccessBlocked(f"draft access requires UUID {label}") from error
    if not actor.roles.intersection(_READ_ROLES) or actor.roles.intersection({Role.FIRM_ADMIN, Role.SYSTEM_WORKER}):
        raise ReviewableDraftAccessBlocked("current role cannot review Office draft artifacts")
    if session.authentication_method != "OS_BOUND_LOCAL_SESSION":
        raise ReviewableDraftAccessBlocked("draft access requires an OS-bound local session")
    if session.authenticated_at.tzinfo is None or session.expires_at.tzinfo is None or session.authenticated_at > now or session.expires_at <= now:
        raise ReviewableDraftAccessBlocked("draft access session is not currently valid")


def _validate_locator(locator: ReviewableOfficeDraftArtifactLocator, *, actor: Actor) -> None:
    for label, value in (("firm_id", locator.firm_id), ("matter_id", locator.matter_id), ("pair_id", locator.pair_id)):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise ReviewableDraftAccessBlocked(f"draft locator requires UUID {label}") from error
    if locator.firm_id != actor.firm_id or locator.pair_status not in {"CANDIDATE", "APPROVED"}:
        raise ReviewableDraftAccessBlocked("reviewable draft is unavailable in this scope")
    if not _SHA256.fullmatch(locator.artifact_sha256) or not 0 < locator.byte_size <= 128 * 1024 * 1024:
        raise ReviewableDraftAccessBlocked("draft locator hash or byte size is invalid")
    if locator.object_key != f"{locator.artifact_sha256[:2]}/{locator.artifact_sha256[2:4]}/{locator.artifact_sha256}.lca":
        raise ReviewableDraftAccessBlocked("draft locator object key is not hash-bound")
    if locator.purpose is ReviewableDraftAccessPurpose.REVIEW_PDF:
        if locator.media_type != "application/pdf":
            raise ReviewableDraftAccessBlocked("review PDF locator media type is invalid")
    elif locator.media_type not in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        raise ReviewableDraftAccessBlocked("editable Office locator media type is invalid")


def _validate_loopback(value: str) -> None:
    try:
        allowed = ip_address(value).is_loopback
    except ValueError as error:
        raise ReviewableDraftAccessBlocked("draft delivery requires numeric loopback") from error
    if not allowed:
        raise ReviewableDraftAccessBlocked("draft delivery is restricted to local loopback")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ReviewableDraftAccessBlocked("draft access time must be timezone-aware")
    return current
