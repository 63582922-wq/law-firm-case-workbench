"""Process-local authorization for lawyer-selected case folders.

The desktop shell must establish an OS-bound session and show the selected
folder scope before this registry will issue a read grant.  The registry keeps
the absolute path only in memory; audit-safe handles expose a path fingerprint.
All grants disappear on process restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from .local_case_folder import (
    FolderManifest,
    FolderScanBlocked,
    FolderScanLimits,
    root_fingerprint,
    scan_case_folder,
)
from .models import Actor, Role


class LocalFolderAccessBlocked(PermissionError):
    """A local folder read is not covered by a current OS-bound grant."""


@dataclass(frozen=True)
class LocalSessionProof:
    session_id: str
    authentication_method: str
    authenticated_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class FolderGrantHandle:
    grant_id: str
    firm_id: str
    matter_id: str
    actor_id: str
    session_id: str
    root_fingerprint: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class FolderSelectionInspection:
    display_name: str
    root_fingerprint: str


@dataclass(frozen=True)
class AuthorizedOriginalFile:
    relative_path: str
    path: Path = field(repr=False, compare=False)
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class _FolderGrantRecord:
    handle: FolderGrantHandle
    resolved_root: Path
    device: int
    inode: int
    resolved_originals: dict[tuple[str, int, str], str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)


class LocalFolderGrantRegistry:
    def __init__(self, *, max_ttl: timedelta = timedelta(minutes=10)) -> None:
        if max_ttl <= timedelta(0) or max_ttl > timedelta(minutes=30):
            raise ValueError("local folder grant max_ttl must be between 1 second and 30 minutes")
        self._max_ttl = max_ttl
        self._records: dict[str, _FolderGrantRecord] = {}
        self._lock = Lock()

    def inspect_selection(
        self,
        *,
        selected_root: str | Path,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> FolderSelectionInspection:
        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        raw_root = Path(selected_root).expanduser()
        if raw_root.is_symlink():
            raise LocalFolderAccessBlocked("the selected case folder cannot be a symbolic link")
        resolved = raw_root.resolve(strict=True)
        if not resolved.is_dir():
            raise LocalFolderAccessBlocked("the selected case folder must be an existing directory")
        _reject_broad_root(resolved)
        return FolderSelectionInspection(
            display_name=resolved.name,
            root_fingerprint=root_fingerprint(resolved),
        )

    def issue_read_grant(
        self,
        *,
        selected_root: str | Path,
        confirmed_root_fingerprint: str,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> FolderGrantHandle:
        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        raw_root = Path(selected_root).expanduser()
        if raw_root.is_symlink():
            raise LocalFolderAccessBlocked("the selected case folder cannot be a symbolic link")
        resolved = raw_root.resolve(strict=True)
        if not resolved.is_dir():
            raise LocalFolderAccessBlocked("the selected case folder must be an existing directory")
        _reject_broad_root(resolved)
        actual_fingerprint = root_fingerprint(resolved)
        if actual_fingerprint != confirmed_root_fingerprint:
            raise LocalFolderAccessBlocked("folder confirmation does not match the selected root")
        stat = resolved.stat()
        expires_at = min(current + self._max_ttl, session.expires_at)
        if expires_at <= current:
            raise LocalFolderAccessBlocked("the local session expires before a folder grant can be issued")
        handle = FolderGrantHandle(
            grant_id=str(uuid4()),
            firm_id=actor.firm_id,
            matter_id=matter_id,
            actor_id=actor.actor_id,
            session_id=session.session_id,
            root_fingerprint=actual_fingerprint,
            issued_at=current,
            expires_at=expires_at,
        )
        with self._lock:
            self._remove_expired(current)
            self._records[handle.grant_id] = _FolderGrantRecord(
                handle=handle,
                resolved_root=resolved,
                device=stat.st_dev,
                inode=stat.st_ino,
            )
        return handle

    def validate_read(
        self,
        *,
        grant_id: str,
        selected_root: str | Path,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        now: datetime | None = None,
    ) -> FolderGrantHandle:
        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        try:
            UUID(grant_id)
        except (TypeError, ValueError) as error:
            raise LocalFolderAccessBlocked("local folder grant identifier is invalid") from error
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(grant_id)
        if record is None:
            raise LocalFolderAccessBlocked("local folder read grant is missing or expired")
        handle = record.handle
        if (
            handle.firm_id != actor.firm_id
            or handle.matter_id != matter_id
            or handle.actor_id != actor.actor_id
            or handle.session_id != session.session_id
        ):
            raise LocalFolderAccessBlocked("local folder read grant is outside the authenticated scope")
        raw_root = Path(selected_root).expanduser()
        if raw_root.is_symlink():
            raise LocalFolderAccessBlocked("the selected case folder cannot be a symbolic link")
        resolved = raw_root.resolve(strict=True)
        _reject_broad_root(resolved)
        stat = resolved.stat()
        if resolved != record.resolved_root or stat.st_dev != record.device or stat.st_ino != record.inode:
            raise LocalFolderAccessBlocked("the selected case folder changed after authorization")
        if root_fingerprint(resolved) != handle.root_fingerprint:
            raise LocalFolderAccessBlocked("the selected case folder fingerprint changed")
        return handle

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            grant_ids = [
                grant_id
                for grant_id, record in self._records.items()
                if record.handle.session_id == session_id
            ]
            for grant_id in grant_ids:
                del self._records[grant_id]
        return len(grant_ids)

    def resolve_registered_original(
        self,
        *,
        grant_id: str,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        expected_sha256: str,
        expected_byte_size: int,
        original_label: str,
        limits: FolderScanLimits = FolderScanLimits(),
        now: datetime | None = None,
    ) -> AuthorizedOriginalFile:
        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            raise LocalFolderAccessBlocked("registered original SHA-256 is invalid")
        if expected_byte_size < 1:
            raise LocalFolderAccessBlocked("registered original byte size is invalid")
        if not original_label.strip():
            raise LocalFolderAccessBlocked("registered original label is required")
        record = self._current_record(grant_id, current=current)
        handle = record.handle
        if (
            handle.firm_id != actor.firm_id
            or handle.matter_id != matter_id
            or handle.actor_id != actor.actor_id
            or handle.session_id != session.session_id
        ):
            raise LocalFolderAccessBlocked("local folder read grant is outside the authenticated scope")
        normalized_label = original_label.strip().replace("\\", "/")
        cache_key = (expected_sha256, expected_byte_size, normalized_label)
        try:
            stat = record.resolved_root.stat()
            if stat.st_dev != record.device or stat.st_ino != record.inode:
                raise LocalFolderAccessBlocked("the selected case folder changed after authorization")
            if root_fingerprint(record.resolved_root) != handle.root_fingerprint:
                raise LocalFolderAccessBlocked("the selected case folder fingerprint changed")
        except (OSError, FolderScanBlocked) as error:
            raise LocalFolderAccessBlocked("the selected case folder is unavailable or changed") from error
        with self._lock:
            selected_relative_path = record.resolved_originals.get(cache_key)
        if selected_relative_path is None:
            try:
                manifest = scan_case_folder(
                    record.resolved_root,
                    confirmed_root_fingerprint=handle.root_fingerprint,
                    limits=limits,
                )
            except (OSError, FolderScanBlocked) as error:
                raise LocalFolderAccessBlocked("the selected case folder is unavailable or changed") from error
            matches = [
                item
                for item in manifest.originals
                if item.sha256 == expected_sha256 and item.byte_size == expected_byte_size
            ]
            if not matches:
                raise LocalFolderAccessBlocked("the registered original is not present in the authorized case folder")
            if len(matches) > 1:
                label_matches = [
                    item
                    for item in matches
                    if item.relative_path == normalized_label
                    or Path(item.relative_path).name == Path(normalized_label).name
                ]
                if len(label_matches) != 1:
                    raise LocalFolderAccessBlocked(
                        "multiple files match the registered original; an explicit relative-path binding is required"
                    )
                matches = label_matches
            selected_relative_path = matches[0].relative_path
            with self._lock:
                if self._records.get(grant_id) is not record:
                    raise LocalFolderAccessBlocked("local folder read grant was revoked during resolution")
                record.resolved_originals[cache_key] = selected_relative_path
        raw_path = record.resolved_root / selected_relative_path
        try:
            if raw_path.is_symlink():
                raise LocalFolderAccessBlocked("the registered original cannot be a symbolic link")
            path = raw_path.resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(record.resolved_root):
                raise LocalFolderAccessBlocked("the registered original no longer resolves to a safe regular file")
            if path.stat().st_size != expected_byte_size or _hash_file(path) != expected_sha256:
                raise LocalFolderAccessBlocked("the registered original changed during authorization")
        except OSError as error:
            raise LocalFolderAccessBlocked("the registered original is unavailable or changed") from error
        return AuthorizedOriginalFile(
            relative_path=selected_relative_path,
            path=path,
            byte_size=expected_byte_size,
            sha256=expected_sha256,
        )

    def scan_granted_folder(
        self,
        *,
        grant_id: str,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        limits: FolderScanLimits = FolderScanLimits(),
        now: datetime | None = None,
    ) -> FolderManifest:
        """Scan the in-memory authorized root without returning its absolute path to the caller."""

        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        record = self._current_record(grant_id, current=current)
        handle = record.handle
        if (
            handle.firm_id != actor.firm_id
            or handle.matter_id != matter_id
            or handle.actor_id != actor.actor_id
            or handle.session_id != session.session_id
        ):
            raise LocalFolderAccessBlocked("local folder read grant is outside the authenticated scope")
        try:
            stat = record.resolved_root.stat()
            if stat.st_dev != record.device or stat.st_ino != record.inode:
                raise LocalFolderAccessBlocked("the selected case folder changed after authorization")
            if root_fingerprint(record.resolved_root) != handle.root_fingerprint:
                raise LocalFolderAccessBlocked("the selected case folder fingerprint changed")
            return scan_case_folder(
                record.resolved_root,
                confirmed_root_fingerprint=handle.root_fingerprint,
                limits=limits,
            )
        except (OSError, FolderScanBlocked) as error:
            raise LocalFolderAccessBlocked("the selected case folder is unavailable or changed") from error

    def resolve_scanned_original(
        self,
        *,
        grant_id: str,
        actor: Actor,
        matter_id: str,
        session: LocalSessionProof,
        relative_path: str,
        expected_sha256: str,
        expected_byte_size: int,
        now: datetime | None = None,
    ) -> AuthorizedOriginalFile:
        """Resolve one approved inventory row without rescanning or exposing the root."""

        current = _aware_now(now)
        _validate_actor_and_session(actor, matter_id=matter_id, session=session, now=current)
        record = self._current_record(grant_id, current=current)
        if (
            record.handle.firm_id != actor.firm_id
            or record.handle.matter_id != matter_id
            or record.handle.actor_id != actor.actor_id
            or record.handle.session_id != session.session_id
        ):
            raise LocalFolderAccessBlocked("local folder read grant is outside the authenticated scope")
        if (
            not relative_path
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise LocalFolderAccessBlocked("approved inventory relative path is invalid")
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            raise LocalFolderAccessBlocked("approved inventory SHA-256 is invalid")
        if expected_byte_size < 0:
            raise LocalFolderAccessBlocked("approved inventory byte size is invalid")
        raw_path = record.resolved_root.joinpath(*relative_path.split("/"))
        try:
            if raw_path.is_symlink():
                raise LocalFolderAccessBlocked("approved inventory file cannot be a symbolic link")
            path = raw_path.resolve(strict=True)
            root_stat = record.resolved_root.stat()
            if root_stat.st_dev != record.device or root_stat.st_ino != record.inode:
                raise LocalFolderAccessBlocked("the selected case folder changed after authorization")
            if not path.is_file() or not path.is_relative_to(record.resolved_root):
                raise LocalFolderAccessBlocked("approved inventory file escaped the authorized folder")
            if path.stat().st_size != expected_byte_size or _hash_file(path) != expected_sha256:
                raise LocalFolderAccessBlocked("approved inventory file changed after the lawyer-approved scan")
        except OSError as error:
            raise LocalFolderAccessBlocked("approved inventory file is unavailable or changed") from error
        return AuthorizedOriginalFile(
            relative_path=relative_path,
            path=path,
            byte_size=expected_byte_size,
            sha256=expected_sha256,
        )

    def _current_record(self, grant_id: str, *, current: datetime) -> _FolderGrantRecord:
        try:
            UUID(grant_id)
        except (TypeError, ValueError) as error:
            raise LocalFolderAccessBlocked("local folder grant identifier is invalid") from error
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(grant_id)
        if record is None:
            raise LocalFolderAccessBlocked("local folder read grant is missing or expired")
        return record

    def _remove_expired(self, current: datetime) -> None:
        for grant_id in [
            grant_id
            for grant_id, record in self._records.items()
            if record.handle.expires_at <= current
        ]:
            del self._records[grant_id]


def scan_authorized_case_folder(
    selected_root: str | Path,
    *,
    registry: LocalFolderGrantRegistry,
    grant_id: str,
    actor: Actor,
    matter_id: str,
    session: LocalSessionProof,
    limits: FolderScanLimits = FolderScanLimits(),
    now: datetime | None = None,
) -> FolderManifest:
    handle = registry.validate_read(
        grant_id=grant_id,
        selected_root=selected_root,
        actor=actor,
        matter_id=matter_id,
        session=session,
        now=now,
    )
    return scan_case_folder(
        selected_root,
        confirmed_root_fingerprint=handle.root_fingerprint,
        limits=limits,
    )


def _validate_actor_and_session(
    actor: Actor,
    *,
    matter_id: str,
    session: LocalSessionProof,
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
            raise LocalFolderAccessBlocked(f"local access requires UUID {label}") from error
    if not actor.roles.intersection(_READ_ROLES) or actor.roles.intersection(
        {Role.FIRM_ADMIN, Role.SYSTEM_WORKER}
    ):
        raise LocalFolderAccessBlocked("the current role cannot read original case folders")
    if session.authentication_method != "OS_BOUND_LOCAL_SESSION":
        raise LocalFolderAccessBlocked("local folder access requires an OS-bound local session")
    if session.authenticated_at.tzinfo is None or session.expires_at.tzinfo is None:
        raise LocalFolderAccessBlocked("local session timestamps must be timezone-aware")
    if session.authenticated_at > now or session.expires_at <= now:
        raise LocalFolderAccessBlocked("the local session is not currently valid")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise LocalFolderAccessBlocked("local access time must be timezone-aware")
    return current


def _reject_broad_root(resolved: Path) -> None:
    filesystem_root = Path(resolved.anchor).resolve()
    home_root = Path.home().resolve()
    if resolved in {filesystem_root, home_root}:
        raise LocalFolderAccessBlocked("select a dedicated case folder, not a filesystem or home root")


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
