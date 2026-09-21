from __future__ import annotations

import inspect
from unittest import TestCase
from uuid import UUID, uuid4

from case_api.web_identity_directory import (
    PostgresOidcIdentityDirectory,
    WebIdentityDirectoryBlocked,
)
from case_kernel.models import Role


ISSUER = "https://login.example-law-firm.test/realms/lawcase"
SUBJECT = "verified-oidc-subject-1001"
DIRECTORY_ROLE = "lawcase_identity_directory"


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection
        self._rows: list[dict] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        normalized = " ".join(sql.split())
        self._connection.executed.append((normalized, params))
        if self._connection.error_marker and self._connection.error_marker in normalized:
            raise RuntimeError("private database failure detail")
        if normalized.startswith("SELECT current_user AS current_user"):
            self._rows = [{"current_user": self._connection.current_user}]
        elif normalized.startswith("SELECT firm_id, user_id FROM web_oidc_identities"):
            self._rows = list(self._connection.mapping_rows)
        elif "FROM web_oidc_identities AS identity JOIN users AS app_user" in normalized:
            self._rows = list(self._connection.membership_rows)
        else:
            self._rows = []

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _Connection:
    def __init__(
        self,
        *,
        current_user: str = DIRECTORY_ROLE,
        mapping_rows: tuple[dict, ...] = (),
        membership_rows: tuple[dict, ...] = (),
        error_marker: str | None = None,
    ) -> None:
        self.current_user = current_user
        self.mapping_rows = mapping_rows
        self.membership_rows = membership_rows
        self.error_marker = error_marker
        self.executed: list[tuple[str, tuple | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> _Connection:
        return self._connection

    def __exit__(self, *_: object) -> bool:
        return False


class PostgresOidcIdentityDirectoryTests(TestCase):
    def _directory(self, connection: _Connection) -> PostgresOidcIdentityDirectory:
        return PostgresOidcIdentityDirectory(
            "postgresql://not-used.invalid/lawcase_identity_directory_test",
            connection_factory=lambda: _ConnectionContext(connection),
        )

    def test_resolves_only_verified_issuer_subject_after_firm_context_is_set(self) -> None:
        firm_id, user_id = uuid4(), uuid4()
        connection = _Connection(
            mapping_rows=({"firm_id": firm_id, "user_id": user_id},),
            membership_rows=({"active_human_roles": ["LEAD_LAWYER", "REVIEWER"]},),
        )

        actor = self._directory(connection).resolve_actor(issuer=ISSUER, subject=SUBJECT)

        self.assertEqual(actor.actor_id, str(user_id))
        self.assertEqual(actor.firm_id, str(firm_id))
        self.assertEqual(actor.roles, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
        self.assertEqual(tuple(inspect.signature(self._directory(connection).resolve_actor).parameters), ("issuer", "subject"))

        statements = [statement for statement, _ in connection.executed]
        global_lookup = next(index for index, value in enumerate(statements) if value.startswith("SELECT firm_id, user_id"))
        firm_context = next(index for index, value in enumerate(statements) if value.startswith("SELECT set_config('app.firm_id'"))
        tenant_check = next(index for index, value in enumerate(statements) if "JOIN users AS app_user" in value)
        self.assertLess(global_lookup, firm_context)
        self.assertLess(firm_context, tenant_check)
        self.assertNotIn("JOIN users", statements[global_lookup])
        self.assertEqual(connection.executed[firm_context][1], (str(firm_id),))
        self.assertEqual(connection.executed[tenant_check][1], (ISSUER, SUBJECT, str(firm_id), str(user_id)))

    def test_unknown_or_inactive_mapping_returns_none_without_a_tenant_read(self) -> None:
        unknown_connection = _Connection()
        self.assertIsNone(self._directory(unknown_connection).resolve_actor(issuer=ISSUER, subject=SUBJECT))
        unknown_sql = "\n".join(statement for statement, _ in unknown_connection.executed)
        self.assertNotIn("set_config('app.firm_id'", unknown_sql)
        self.assertNotIn("JOIN users AS app_user", unknown_sql)

        firm_id, user_id = uuid4(), uuid4()
        inactive_connection = _Connection(mapping_rows=({"firm_id": firm_id, "user_id": user_id},))
        self.assertIsNone(self._directory(inactive_connection).resolve_actor(issuer=ISSUER, subject=SUBJECT))
        inactive_sql = "\n".join(statement for statement, _ in inactive_connection.executed)
        self.assertLess(inactive_sql.index("set_config('app.firm_id'"), inactive_sql.index("JOIN users AS app_user"))

    def test_invalid_human_roles_or_ambiguous_identity_fail_closed(self) -> None:
        firm_id, user_id = uuid4(), uuid4()
        system_role = _Connection(
            mapping_rows=({"firm_id": firm_id, "user_id": user_id},),
            membership_rows=({"active_human_roles": ["SYSTEM_WORKER"]},),
        )
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(system_role).resolve_actor(issuer=ISSUER, subject=SUBJECT)

        ambiguous = _Connection(
            mapping_rows=(
                {"firm_id": firm_id, "user_id": user_id},
                {"firm_id": uuid4(), "user_id": uuid4()},
            )
        )
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(ambiguous).resolve_actor(issuer=ISSUER, subject=SUBJECT)

    def test_invalid_lookup_is_rejected_before_opening_database_connection(self) -> None:
        connection = _Connection()
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(connection).resolve_actor(issuer="x" * 1_025, subject=SUBJECT)
        self.assertEqual(connection.executed, [])

        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(connection).resolve_actor(issuer=ISSUER, subject=" bad-subject")
        self.assertEqual(connection.executed, [])

    def test_directory_role_and_database_errors_fail_closed_without_detail_leakage(self) -> None:
        wrong_role = _Connection(current_user="lawcase_web_application")
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(wrong_role).resolve_actor(issuer=ISSUER, subject=SUBJECT)
        wrong_role_sql = "\n".join(statement for statement, _ in wrong_role.executed)
        self.assertNotIn("FROM web_oidc_identities", wrong_role_sql)

        failing = _Connection(error_marker="FROM web_oidc_identities")
        with self.assertRaises(WebIdentityDirectoryBlocked) as blocked:
            self._directory(failing).resolve_actor(issuer=ISSUER, subject=SUBJECT)
        self.assertNotIn("private database failure detail", str(blocked.exception))
        self.assertIsNone(blocked.exception.__cause__)

    def test_invalid_database_rows_are_not_coerced_to_an_actor(self) -> None:
        malformed = _Connection(
            mapping_rows=({"firm_id": "not-a-uuid", "user_id": str(uuid4())},),
        )
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(malformed).resolve_actor(issuer=ISSUER, subject=SUBJECT)

        firm_id, user_id = uuid4(), uuid4()
        duplicate_roles = _Connection(
            mapping_rows=({"firm_id": firm_id, "user_id": user_id},),
            membership_rows=({"active_human_roles": ["REVIEWER", "REVIEWER"]},),
        )
        with self.assertRaises(WebIdentityDirectoryBlocked):
            self._directory(duplicate_roles).resolve_actor(issuer=ISSUER, subject=SUBJECT)

        self.assertIsInstance(UUID(str(firm_id)), UUID)
