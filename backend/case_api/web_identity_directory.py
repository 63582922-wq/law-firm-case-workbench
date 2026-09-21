"""Least-privilege PostgreSQL directory for verified Web OIDC identities.

This adapter deliberately fulfils ``OidcActorMapping`` structurally without
importing the JWT verifier.  It receives only an already-verified OIDC
``(issuer, subject)`` pair, finds one server-provisioned mapping, and then
sets the mapped firm context before it reads the tenant-scoped ``users`` table.
It never accepts a browser-provided firm, user, role, or display attribute and
does not create mappings or users.

The connection must authenticate as the dedicated database role configured by
the deployment (``lawcase_identity_directory`` by default).  Database errors
and malformed rows fail closed without exposing connection details.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import re
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from case_kernel.models import Actor, Role


__all__ = (
    "PostgresOidcIdentityDirectory",
    "WebIdentityDirectoryBlocked",
)


_MAX_DSN_LENGTH = 4_096
_MAX_ISSUER_LENGTH = 1_024
_MAX_SUBJECT_LENGTH = 255
_DATABASE_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HUMAN_ROLES = frozenset(role for role in Role if role is not Role.SYSTEM_WORKER)
_HUMAN_ROLE_BY_VALUE = {role.value: role for role in _HUMAN_ROLES}


class WebIdentityDirectoryBlocked(PermissionError):
    """The identity directory cannot safely resolve a human actor."""


class PostgresOidcIdentityDirectory:
    """Resolve one verified OIDC issuer/subject through PostgreSQL.

    ``connection_factory`` exists for controlled composition and tests.  When
    omitted, the adapter opens a normal ``psycopg`` connection using ``dsn``.
    The factory must return a context-managed PostgreSQL connection whose
    cursors produce mapping rows (the default uses ``dict_row``).
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
            raise ValueError("Web OIDC identity directory connection factory is invalid")
        self._connection_factory = connection_factory or self._open_psycopg_connection

    def resolve_actor(self, *, issuer: str, subject: str) -> Actor | None:
        """Return the server-owned Actor for one verified identity, if active.

        No mapping, a disabled mapping, or an inactive firm membership returns
        ``None``.  Malformed data, a wrong database role, or any database error
        raises ``WebIdentityDirectoryBlocked`` so the OIDC resolver rejects the
        authentication rather than treating an uncertain identity as trusted.
        """

        verified_issuer = _validated_lookup_text(issuer, label="issuer", maximum=_MAX_ISSUER_LENGTH)
        verified_subject = _validated_lookup_text(subject, label="subject", maximum=_MAX_SUBJECT_LENGTH)
        try:
            with self._read_cursor() as cursor:
                self._require_directory_role(cursor)
                mapping_rows = _rows_from_cursor(
                    cursor,
                    sql="""
                        SELECT firm_id, user_id
                        FROM web_oidc_identities
                        WHERE issuer = %s
                          AND subject = %s
                          AND is_active = TRUE
                        LIMIT 2
                    """,
                    params=(verified_issuer, verified_subject),
                    label="global identity mapping",
                )
                if not mapping_rows:
                    return None
                if len(mapping_rows) != 1:
                    raise WebIdentityDirectoryBlocked("Web OIDC identity mapping is ambiguous")
                firm_id = _uuid_field(mapping_rows[0], field="firm_id")
                user_id = _uuid_field(mapping_rows[0], field="user_id")

                # ``users`` is tenant-scoped and protected by the existing
                # FORCE RLS policy.  This call must remain before every tenant
                # table read in this transaction.
                cursor.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))

                membership_rows = _rows_from_cursor(
                    cursor,
                    sql="""
                        SELECT identity.active_human_roles
                        FROM web_oidc_identities AS identity
                        JOIN users AS app_user
                          ON app_user.user_id = identity.user_id
                         AND app_user.firm_id = identity.firm_id
                        JOIN firms AS firm
                          ON firm.firm_id = identity.firm_id
                        WHERE identity.issuer = %s
                          AND identity.subject = %s
                          AND identity.firm_id = %s
                          AND identity.user_id = %s
                          AND identity.is_active = TRUE
                          AND app_user.status = 'ACTIVE'
                        LIMIT 2
                    """,
                    params=(verified_issuer, verified_subject, firm_id, user_id),
                    label="active firm membership",
                )
                if not membership_rows:
                    return None
                if len(membership_rows) != 1:
                    raise WebIdentityDirectoryBlocked("Web OIDC active membership is ambiguous")
                roles = _human_roles(membership_rows[0])
                return Actor(actor_id=user_id, firm_id=firm_id, roles=roles)
        except WebIdentityDirectoryBlocked:
            raise
        except Exception:
            raise WebIdentityDirectoryBlocked("Web OIDC identity directory is unavailable") from None

    def _open_psycopg_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _read_cursor(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                with connection.cursor() as cursor:
                    # Must be the first statement in the transaction.  The
                    # directory needs no write capability.
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    yield cursor
        except WebIdentityDirectoryBlocked:
            raise
        except Exception:
            raise WebIdentityDirectoryBlocked("Web OIDC identity directory is unavailable") from None

    def _require_directory_role(self, cursor: Any) -> None:
        cursor.execute("SELECT current_user AS current_user")
        row = cursor.fetchone()
        if not isinstance(row, Mapping):
            raise WebIdentityDirectoryBlocked("Web OIDC identity directory role check failed")
        current_user = row.get("current_user")
        if not isinstance(current_user, str) or current_user != self._database_role:
            raise WebIdentityDirectoryBlocked("Web OIDC identity directory role check failed")


def _rows_from_cursor(
    cursor: Any,
    *,
    sql: str,
    params: tuple[str, ...],
    label: str,
) -> tuple[Mapping[str, Any], ...]:
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    if not isinstance(rows, (list, tuple)) or not all(isinstance(row, Mapping) for row in rows):
        raise WebIdentityDirectoryBlocked(f"Web OIDC {label} result is malformed")
    return tuple(rows)


def _uuid_field(row: Mapping[str, Any], *, field: str) -> str:
    try:
        value = row[field]
    except (KeyError, TypeError) as error:
        raise WebIdentityDirectoryBlocked("Web OIDC identity mapping is malformed") from error
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise WebIdentityDirectoryBlocked("Web OIDC identity mapping is malformed")
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as error:
        raise WebIdentityDirectoryBlocked("Web OIDC identity mapping is malformed") from error


def _human_roles(row: Mapping[str, Any]) -> frozenset[Role]:
    try:
        values = row["active_human_roles"]
    except (KeyError, TypeError) as error:
        raise WebIdentityDirectoryBlocked("Web OIDC active roles are malformed") from error
    if not isinstance(values, (list, tuple)) or not values:
        raise WebIdentityDirectoryBlocked("Web OIDC active roles are malformed")
    if not all(isinstance(value, str) and value in _HUMAN_ROLE_BY_VALUE for value in values):
        raise WebIdentityDirectoryBlocked("Web OIDC active roles are malformed")
    roles = frozenset(_HUMAN_ROLE_BY_VALUE[value] for value in values)
    if len(roles) != len(values) or Role.SYSTEM_WORKER in roles:
        raise WebIdentityDirectoryBlocked("Web OIDC active roles are malformed")
    return roles


def _validated_dsn(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_DSN_LENGTH
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError("Web OIDC identity directory DSN is invalid")
    return value


def _validated_database_role(value: str) -> str:
    if not isinstance(value, str) or not _DATABASE_ROLE.fullmatch(value):
        raise ValueError("Web OIDC identity directory database role is invalid")
    return value


def _validated_lookup_text(value: str, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WebIdentityDirectoryBlocked(f"Web OIDC {label} is invalid")
    return value
