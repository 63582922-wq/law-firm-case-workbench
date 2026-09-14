"""PostgreSQL adapters for the browser-only opaque Web session boundary.

The session gateway and actor directory deliberately use separate, narrowly
privileged database roles:

* ``lawcase_web_session_gateway`` can read one row by an exact SHA-256 cookie
  digest, create a server-derived row after a firm context is known, and write
  a single revocation timestamp.  It has no case-data grants.
* ``lawcase_identity_directory`` checks that the server-stored actor still has
  an active OIDC directory mapping and an active firm membership.  It has no
  access to ``web_sessions`` or case contents.

Neither adapter accepts a raw OIDC JWT or a browser-provided firm/role.  Raw
opaque session and CSRF values never enter this module: only fixed-length
SHA-256 digests are passed to or returned from PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
import re
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from case_api.web_session import StoredWebSession, WebSessionBlocked
from case_kernel.models import Actor, Role


__all__ = (
    "PostgresWebSessionActorDirectory",
    "PostgresWebSessionStore",
    "WebSessionPersistenceBlocked",
)


_MAX_DSN_LENGTH = 4_096
_MAX_ISSUER_LENGTH = 1_024
_DATABASE_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SHA256_BYTES = 32
_HUMAN_ROLE_BY_VALUE = {
    role.value: role
    for role in Role
    if role is not Role.SYSTEM_WORKER
}


class WebSessionPersistenceBlocked(WebSessionBlocked):
    """A Web session persistence dependency is absent or unsafe."""


class PostgresWebSessionStore:
    """Persist and resolve only hash-based browser session records.

    The database role is checked on every operation.  It must match the role
    to which migration ``0024_web_sessions.sql`` grants access; a normal
    tenant application connection is intentionally rejected.
    """

    def __init__(
        self,
        dsn: str,
        *,
        database_role: str = "lawcase_web_session_gateway",
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._dsn = _validated_dsn(dsn)
        self._database_role = _validated_database_role(database_role)
        if connection_factory is not None and not callable(connection_factory):
            raise ValueError("Web session connection factory is invalid")
        self._connection_factory = connection_factory or self._open_psycopg_connection

    def create_session(self, *, session: StoredWebSession) -> None:
        """Insert a fully server-derived session record after identity proof."""

        _validate_stored_session_for_persistence(session)
        try:
            with self._cursor(read_only=False) as cursor:
                self._require_role(cursor)
                cursor.execute("SELECT set_config('app.firm_id', %s, true)", (session.firm_id,))
                cursor.execute(
                    """
                    INSERT INTO web_sessions (
                        session_id,
                        firm_id,
                        user_id,
                        issuer,
                        session_token_sha256,
                        csrf_token_sha256,
                        authenticated_at,
                        created_at,
                        expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session.session_id,
                        session.firm_id,
                        session.actor_id,
                        session.issuer,
                        session.session_token_sha256,
                        session.csrf_token_sha256,
                        session.authenticated_at,
                        session.created_at,
                        session.expires_at,
                    ),
                )
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session store is unavailable") from None

    def find_session_by_digest(self, *, session_token_sha256: bytes) -> StoredWebSession | None:
        """Read at most one session through the exact-digest RLS selector."""

        digest = _validated_digest(session_token_sha256)
        try:
            with self._cursor(read_only=True) as cursor:
                self._require_role(cursor)
                cursor.execute(
                    "SELECT set_config('app.web_session_token_sha256', %s, true)",
                    (digest.hex(),),
                )
                rows = _rows_from_cursor(
                    cursor,
                    sql="""
                        SELECT
                            session_id,
                            firm_id,
                            user_id,
                            issuer,
                            session_token_sha256,
                            csrf_token_sha256,
                            authenticated_at,
                            created_at,
                            expires_at,
                            revoked_at
                        FROM web_sessions
                        WHERE session_token_sha256 = %s
                        LIMIT 2
                    """,
                    params=(digest,),
                    label="Web session lookup",
                )
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session store is unavailable") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise WebSessionPersistenceBlocked("Web session lookup is ambiguous")
        return _stored_session_from_row(rows[0])

    def revoke_session(self, *, session_id: str, revoked_at: datetime) -> bool:
        """Perform the one allowed record mutation: server-side revocation."""

        normalized_session_id = _validated_uuid(session_id, label="Web session identifier")
        _validated_datetime(revoked_at, label="Web session revocation time")
        try:
            with self._cursor(read_only=False) as cursor:
                self._require_role(cursor)
                cursor.execute(
                    "SELECT set_config('app.web_session_id', %s, true)",
                    (normalized_session_id,),
                )
                cursor.execute(
                    """
                    UPDATE web_sessions
                    SET revoked_at = %s
                    WHERE session_id = %s
                      AND revoked_at IS NULL
                    RETURNING session_id
                    """,
                    (revoked_at, normalized_session_id),
                )
                rows = cursor.fetchall()
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session store is unavailable") from None
        if not isinstance(rows, (list, tuple)) or not all(isinstance(row, Mapping) for row in rows):
            raise WebSessionPersistenceBlocked("Web session revocation result is malformed")
        if len(rows) > 1:
            raise WebSessionPersistenceBlocked("Web session revocation is ambiguous")
        return len(rows) == 1

    def _open_psycopg_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _cursor(self, *, read_only: bool) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                with connection.cursor() as cursor:
                    if read_only:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    yield cursor
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session store is unavailable") from None

    def _require_role(self, cursor: Any) -> None:
        _require_database_role(cursor, expected=self._database_role, label="Web session gateway")


class PostgresWebSessionActorDirectory:
    """Re-check an active human actor for every opaque session resolution.

    The session record supplies its server-derived issuer, firm and user IDs;
    this directory never receives them from a browser request.  It requires
    exactly one active OIDC mapping for that issuer/user/firm tuple and an
    active ``users`` row.  Any ambiguity or directory outage blocks the
    session instead of retaining stale roles.
    """

    def __init__(
        self,
        dsn: str,
        *,
        database_role: str = "lawcase_identity_directory",
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._dsn = _validated_dsn(dsn)
        self._database_role = _validated_database_role(database_role)
        if connection_factory is not None and not callable(connection_factory):
            raise ValueError("Web session actor directory connection factory is invalid")
        self._connection_factory = connection_factory or self._open_psycopg_connection

    def resolve_active_actor(self, *, actor_id: str, firm_id: str, issuer: str) -> Actor | None:
        """Return a current human actor or ``None`` when it is no longer active."""

        user_id = _validated_uuid(actor_id, label="Web session actor")
        tenant_id = _validated_uuid(firm_id, label="Web session firm")
        verified_issuer = _validated_issuer(issuer)
        try:
            with self._read_cursor() as cursor:
                self._require_role(cursor)
                # Existing users RLS is only entered after the server-derived
                # firm identifier is installed in this transaction.
                cursor.execute("SELECT set_config('app.firm_id', %s, true)", (tenant_id,))
                rows = _rows_from_cursor(
                    cursor,
                    sql="""
                        SELECT identity.active_human_roles
                        FROM web_oidc_identities AS identity
                        JOIN users AS app_user
                          ON app_user.user_id = identity.user_id
                         AND app_user.firm_id = identity.firm_id
                        WHERE identity.issuer = %s
                          AND identity.firm_id = %s
                          AND identity.user_id = %s
                          AND identity.is_active = TRUE
                          AND app_user.status = 'ACTIVE'
                        LIMIT 2
                    """,
                    params=(verified_issuer, tenant_id, user_id),
                    label="Web session actor directory",
                )
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session actor directory is unavailable") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise WebSessionPersistenceBlocked("Web session actor directory is ambiguous")
        return Actor(actor_id=user_id, firm_id=tenant_id, roles=_human_roles(rows[0]))

    def _open_psycopg_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _read_cursor(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    yield cursor
        except WebSessionPersistenceBlocked:
            raise
        except Exception:
            raise WebSessionPersistenceBlocked("Web session actor directory is unavailable") from None

    def _require_role(self, cursor: Any) -> None:
        _require_database_role(cursor, expected=self._database_role, label="Web session actor directory")


def _require_database_role(cursor: Any, *, expected: str, label: str) -> None:
    cursor.execute("SELECT current_user AS current_user")
    row = cursor.fetchone()
    if not isinstance(row, Mapping) or row.get("current_user") != expected:
        raise WebSessionPersistenceBlocked(f"{label} role check failed")


def _rows_from_cursor(
    cursor: Any,
    *,
    sql: str,
    params: tuple[object, ...],
    label: str,
) -> tuple[Mapping[str, Any], ...]:
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    if not isinstance(rows, (list, tuple)) or not all(isinstance(row, Mapping) for row in rows):
        raise WebSessionPersistenceBlocked(f"{label} result is malformed")
    return tuple(rows)


def _stored_session_from_row(row: Mapping[str, Any]) -> StoredWebSession:
    try:
        record = StoredWebSession(
            session_id=_validated_uuid(row["session_id"], label="Web session identifier"),
            actor_id=_validated_uuid(row["user_id"], label="Web session actor"),
            firm_id=_validated_uuid(row["firm_id"], label="Web session firm"),
            issuer=_validated_issuer(row["issuer"]),
            session_token_sha256=_validated_digest(row["session_token_sha256"]),
            csrf_token_sha256=_validated_digest(row["csrf_token_sha256"]),
            authenticated_at=_validated_datetime(row["authenticated_at"], label="Web session authentication time"),
            created_at=_validated_datetime(row["created_at"], label="Web session creation time"),
            expires_at=_validated_datetime(row["expires_at"], label="Web session expiry"),
            revoked_at=(
                _validated_datetime(row["revoked_at"], label="Web session revocation time")
                if row["revoked_at"] is not None
                else None
            ),
        )
        record.validate()
        return record
    except (KeyError, TypeError, ValueError, WebSessionBlocked):
        raise WebSessionPersistenceBlocked("Web session row is malformed") from None


def _validate_stored_session_for_persistence(session: StoredWebSession) -> None:
    if not isinstance(session, StoredWebSession):
        raise WebSessionPersistenceBlocked("Web session record is invalid")
    try:
        session.validate()
    except WebSessionBlocked:
        raise WebSessionPersistenceBlocked("Web session record is invalid") from None
    if session.revoked_at is not None:
        raise WebSessionPersistenceBlocked("Web session record is already revoked")


def _human_roles(row: Mapping[str, Any]) -> frozenset[Role]:
    try:
        values = row["active_human_roles"]
    except (KeyError, TypeError):
        raise WebSessionPersistenceBlocked("Web session actor roles are malformed") from None
    if not isinstance(values, (list, tuple)) or not values:
        raise WebSessionPersistenceBlocked("Web session actor roles are malformed")
    if not all(isinstance(value, str) and value in _HUMAN_ROLE_BY_VALUE for value in values):
        raise WebSessionPersistenceBlocked("Web session actor roles are malformed")
    roles = frozenset(_HUMAN_ROLE_BY_VALUE[value] for value in values)
    if len(roles) != len(values) or Role.SYSTEM_WORKER in roles:
        raise WebSessionPersistenceBlocked("Web session actor roles are malformed")
    return roles


def _validated_dsn(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_DSN_LENGTH
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError("Web session DSN is invalid")
    return value


def _validated_database_role(value: object) -> str:
    if not isinstance(value, str) or not _DATABASE_ROLE.fullmatch(value):
        raise ValueError("Web session database role is invalid")
    return value


def _validated_uuid(value: object, *, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebSessionPersistenceBlocked(f"{label} is invalid") from None


def _validated_digest(value: object) -> bytes:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes) or len(value) != _SHA256_BYTES:
        raise WebSessionPersistenceBlocked("Web session digest is invalid")
    return value


def _validated_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebSessionPersistenceBlocked(f"{label} is invalid")
    return value


def _validated_issuer(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ISSUER_LENGTH
        or value != value.strip()
        or not value.startswith("https://")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WebSessionPersistenceBlocked("Web session issuer is invalid")
    return value
