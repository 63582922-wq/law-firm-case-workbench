"""Ephemeral hand-off from a lawyer's folder grant to the local intake worker.

The database queue deliberately never stores a local path, session token, or
folder grant.  This registry is kept only in the desktop sidecar process so a
queued run can read originals only while the lawyer's existing short-lived
folder grant and OS-bound session remain valid.  Restarting the desktop drops
every hand-off; the lawyer must explicitly re-authorize a new intake run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID

from .local_access_grants import LocalSessionProof
from .models import Actor, Role


class LocalEvidenceIntakeAuthorizationBlocked(PermissionError):
    """A local worker hand-off is outside the current human folder scope."""


@dataclass(frozen=True)
class EvidenceIntakeAuthorization:
    run_id: str
    matter_id: str
    folder_grant_id: str
    grant_actor: Actor
    grant_session: LocalSessionProof
    expected_version: int
    retry_not_before: datetime | None = None


class LocalEvidenceIntakeAuthorizationRegistry:
    """Process-local, one-run-at-a-time authorization state for intake work."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceIntakeAuthorization] = {}
        self._lock = Lock()

    def bind(
        self,
        *,
        run_id: str,
        matter_id: str,
        folder_grant_id: str,
        grant_actor: Actor,
        grant_session: LocalSessionProof,
        expected_version: int,
    ) -> EvidenceIntakeAuthorization:
        _validate_uuid("run_id", run_id)
        _validate_uuid("matter_id", matter_id)
        _validate_uuid("folder_grant_id", folder_grant_id)
        if expected_version < 1:
            raise LocalEvidenceIntakeAuthorizationBlocked("intake authorization needs a positive matter version")
        if grant_actor.roles.intersection({Role.SYSTEM_WORKER, Role.FIRM_ADMIN}):
            raise LocalEvidenceIntakeAuthorizationBlocked("only a human case member can authorize local intake")
        if not grant_actor.roles.intersection({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}):
            raise LocalEvidenceIntakeAuthorizationBlocked("folder grant actor has no permitted case role")
        if grant_session.expires_at.tzinfo is None or grant_session.authenticated_at.tzinfo is None:
            raise LocalEvidenceIntakeAuthorizationBlocked("local intake authorization needs timezone-aware session times")
        current = _now()
        if grant_session.expires_at <= current or grant_session.authenticated_at > current:
            raise LocalEvidenceIntakeAuthorizationBlocked("local intake authorization session is no longer valid")
        if grant_session.authentication_method != "OS_BOUND_LOCAL_SESSION":
            raise LocalEvidenceIntakeAuthorizationBlocked("local intake requires an OS-bound human session")
        authorization = EvidenceIntakeAuthorization(
            run_id=run_id,
            matter_id=matter_id,
            folder_grant_id=folder_grant_id,
            grant_actor=grant_actor,
            grant_session=grant_session,
            expected_version=expected_version,
        )
        with self._lock:
            self._remove_expired_locked()
            existing = self._records.get(run_id)
            if existing is not None and existing != authorization:
                raise LocalEvidenceIntakeAuthorizationBlocked("intake run is already bound to a different local authorization")
            self._records[run_id] = authorization
        return authorization

    def next_authorized_run(self, *, now: datetime | None = None) -> EvidenceIntakeAuthorization | None:
        current = _aware_now(now)
        with self._lock:
            self._remove_expired_locked(now=current)
            eligible = [
                authorization
                for authorization in self._records.values()
                if authorization.retry_not_before is None or authorization.retry_not_before <= current
            ]
            if not eligible:
                return None
            return min(eligible, key=lambda item: (item.grant_session.expires_at, item.run_id))

    def update_expected_version(self, *, run_id: str, expected_version: int) -> EvidenceIntakeAuthorization:
        if expected_version < 1:
            raise LocalEvidenceIntakeAuthorizationBlocked("intake authorization needs a positive matter version")
        with self._lock:
            self._remove_expired_locked()
            current = self._records.get(run_id)
            if current is None:
                raise LocalEvidenceIntakeAuthorizationBlocked("intake authorization is missing or expired")
            updated = EvidenceIntakeAuthorization(
                run_id=current.run_id,
                matter_id=current.matter_id,
                folder_grant_id=current.folder_grant_id,
                grant_actor=current.grant_actor,
                grant_session=current.grant_session,
                expected_version=expected_version,
                retry_not_before=current.retry_not_before,
            )
            self._records[run_id] = updated
            return updated

    def defer_until(self, *, run_id: str, retry_not_before: datetime) -> EvidenceIntakeAuthorization:
        """Hold a claimed item until its durable lease can safely be retried.

        The queue stores the lease; this process-local marker only prevents the
        same sidecar from mistaking a still-running lease for a completed run.
        It contains neither source paths nor credential material.
        """
        if retry_not_before.tzinfo is None:
            raise LocalEvidenceIntakeAuthorizationBlocked("intake retry time must be timezone-aware")
        with self._lock:
            self._remove_expired_locked()
            current = self._records.get(run_id)
            if current is None:
                raise LocalEvidenceIntakeAuthorizationBlocked("intake authorization is missing or expired")
            deferred = EvidenceIntakeAuthorization(
                run_id=current.run_id,
                matter_id=current.matter_id,
                folder_grant_id=current.folder_grant_id,
                grant_actor=current.grant_actor,
                grant_session=current.grant_session,
                expected_version=current.expected_version,
                retry_not_before=min(retry_not_before, current.grant_session.expires_at),
            )
            self._records[run_id] = deferred
            return deferred

    def remove(self, *, run_id: str) -> None:
        with self._lock:
            self._records.pop(run_id, None)

    def _remove_expired_locked(self, *, now: datetime | None = None) -> None:
        current = _aware_now(now)
        for run_id in [
            identifier
            for identifier, authorization in self._records.items()
            if authorization.grant_session.expires_at <= current
        ]:
            del self._records[run_id]


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise LocalEvidenceIntakeAuthorizationBlocked(f"local intake requires UUID {label}") from error


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_now(now: datetime | None) -> datetime:
    current = _now() if now is None else now
    if current.tzinfo is None:
        raise LocalEvidenceIntakeAuthorizationBlocked("local intake clock must be timezone-aware")
    return current
