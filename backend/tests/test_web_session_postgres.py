from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
from typing import Any
from uuid import uuid4
from unittest import TestCase

from case_api.web_session import StoredWebSession
from case_api.web_session_postgres import (
    PostgresWebSessionActorDirectory,
    PostgresWebSessionStore,
    WebSessionPersistenceBlocked,
)
from case_kernel.models import Role


SESSION_GATEWAY_ROLE = "lawcase_web_session_gateway"
IDENTITY_DIRECTORY_ROLE = "lawcase_identity_directory"
ISSUER = "https://login.example-law-firm.test/realms/lawcase"
SESSION_TOKEN = "s" * 64
CSRF_TOKEN = "c" * 64


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self._connection.executed.append((normalized, params))
        if self._connection.error_marker and self._connection.error_marker in normalized:
            raise RuntimeError("private database error detail")
        if normalized == "SELECT current_user AS current_user":
            self._rows = [{"current_user": self._connection.current_user}]
        elif "FROM web_sessions" in normalized and normalized.startswith("SELECT"):
            self._rows = list(self._connection.session_rows)
        elif normalized.startswith("UPDATE web_sessions"):
            self._rows = list(self._connection.revoke_rows)
        elif "FROM web_oidc_identities AS identity" in normalized:
            self._rows = list(self._connection.actor_rows)
        else:
            self._rows = []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Connection:
    def __init__(
        self,
        *,
        current_user: str,
        session_rows: tuple[dict[str, Any], ...] = (),
        actor_rows: tuple[dict[str, Any], ...] = (),
        revoke_rows: tuple[dict[str, Any], ...] = (),
        error_marker: str | None = None,
    ) -> None:
        self.current_user = current_user
        self.session_rows = session_rows
        self.actor_rows = actor_rows
        self.revoke_rows = revoke_rows
        self.error_marker = error_marker
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> _Connection:
        return self._connection

    def __exit__(self, *_: object) -> bool:
        return False


class PostgresWebSessionStoreTests(TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        self.firm_id = str(uuid4())
        self.user_id = str(uuid4())
        self.session_id = str(uuid4())
        self.session = StoredWebSession(
            session_id=self.session_id,
            actor_id=self.user_id,
            firm_id=self.firm_id,
            issuer=ISSUER,
            session_token_sha256=sha256(SESSION_TOKEN.encode("ascii")).digest(),
            csrf_token_sha256=sha256(CSRF_TOKEN.encode("ascii")).digest(),
            authenticated_at=self.now - timedelta(minutes=5),
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=45),
        )

    @staticmethod
    def _store(connection: _Connection) -> PostgresWebSessionStore:
        return PostgresWebSessionStore(
            "postgresql://not-used.invalid/lawcase_session_test",
            connection_factory=lambda: _ConnectionContext(connection),
        )

    def test_create_uses_server_derived_firm_context_and_never_sends_raw_secrets(self) -> None:
        connection = _Connection(current_user=SESSION_GATEWAY_ROLE)

        self._store(connection).create_session(session=self.session)

        statements = [statement for statement, _ in connection.executed]
        self.assertEqual(statements[0], "SELECT current_user AS current_user")
        firm_context = next(index for index, value in enumerate(statements) if "app.firm_id" in value)
        insert = next(index for index, value in enumerate(statements) if value.startswith("INSERT INTO web_sessions"))
        self.assertLess(firm_context, insert)
        self.assertEqual(connection.executed[firm_context][1], (self.firm_id,))
        insert_params = connection.executed[insert][1]
        self.assertIsNotNone(insert_params)
        self.assertIn(self.session.session_token_sha256, insert_params)
        self.assertIn(self.session.csrf_token_sha256, insert_params)
        self.assertNotIn(SESSION_TOKEN, repr(connection.executed))
        self.assertNotIn(CSRF_TOKEN, repr(connection.executed))

    def test_exact_digest_lookup_sets_only_hash_selector_and_reconstructs_safe_record(self) -> None:
        connection = _Connection(
            current_user=SESSION_GATEWAY_ROLE,
            session_rows=(
                {
                    "session_id": self.session.session_id,
                    "firm_id": self.session.firm_id,
                    "user_id": self.session.actor_id,
                    "issuer": self.session.issuer,
                    "session_token_sha256": memoryview(self.session.session_token_sha256),
                    "csrf_token_sha256": self.session.csrf_token_sha256,
                    "authenticated_at": self.session.authenticated_at,
                    "created_at": self.session.created_at,
                    "expires_at": self.session.expires_at,
                    "revoked_at": None,
                },
            ),
        )

        resolved = self._store(connection).find_session_by_digest(
            session_token_sha256=self.session.session_token_sha256,
        )

        self.assertEqual(resolved, self.session)
        selector = next(
            params
            for statement, params in connection.executed
            if "app.web_session_token_sha256" in statement
        )
        self.assertEqual(selector, (self.session.session_token_sha256.hex(),))
        self.assertNotIn(SESSION_TOKEN, repr(connection.executed))
        self.assertNotIn(CSRF_TOKEN, repr(resolved))

    def test_revoke_is_the_only_write_path_and_role_or_database_errors_fail_closed(self) -> None:
        connection = _Connection(
            current_user=SESSION_GATEWAY_ROLE,
            revoke_rows=({"session_id": self.session_id},),
        )
        store = self._store(connection)
        self.assertTrue(store.revoke_session(session_id=self.session_id, revoked_at=self.now))
        statements = [statement for statement, _ in connection.executed]
        selector_index = next(index for index, value in enumerate(statements) if "app.web_session_id" in value)
        update_index = next(index for index, value in enumerate(statements) if value.startswith("UPDATE web_sessions"))
        self.assertLess(selector_index, update_index)
        self.assertEqual(connection.executed[selector_index][1], (self.session_id,))

        wrong_role = _Connection(current_user="lawcase_tenant_application")
        with self.assertRaises(WebSessionPersistenceBlocked):
            self._store(wrong_role).find_session_by_digest(
                session_token_sha256=self.session.session_token_sha256,
            )
        self.assertNotIn("FROM web_sessions", "\n".join(statement for statement, _ in wrong_role.executed))

        failing = _Connection(current_user=SESSION_GATEWAY_ROLE, error_marker="FROM web_sessions")
        with self.assertRaises(WebSessionPersistenceBlocked) as blocked:
            self._store(failing).find_session_by_digest(
                session_token_sha256=self.session.session_token_sha256,
            )
        self.assertNotIn("private database error detail", str(blocked.exception))
        self.assertIsNone(blocked.exception.__cause__)


class PostgresWebSessionActorDirectoryTests(TestCase):
    @staticmethod
    def _directory(connection: _Connection) -> PostgresWebSessionActorDirectory:
        return PostgresWebSessionActorDirectory(
            "postgresql://not-used.invalid/lawcase_identity_test",
            connection_factory=lambda: _ConnectionContext(connection),
        )

    def test_directory_rechecks_active_mapping_after_server_firm_context(self) -> None:
        firm_id, user_id = str(uuid4()), str(uuid4())
        connection = _Connection(
            current_user=IDENTITY_DIRECTORY_ROLE,
            actor_rows=({"active_human_roles": ["LEAD_LAWYER", "REVIEWER"]},),
        )

        actor = self._directory(connection).resolve_active_actor(
            actor_id=user_id,
            firm_id=firm_id,
            issuer=ISSUER,
        )

        self.assertEqual(actor.actor_id, user_id)
        self.assertEqual(actor.firm_id, firm_id)
        self.assertEqual(actor.roles, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
        self.assertEqual(
            tuple(inspect.signature(self._directory(connection).resolve_active_actor).parameters),
            ("actor_id", "firm_id", "issuer"),
        )
        statements = [statement for statement, _ in connection.executed]
        firm_context = next(index for index, value in enumerate(statements) if "app.firm_id" in value)
        lookup = next(index for index, value in enumerate(statements) if "FROM web_oidc_identities AS identity" in value)
        self.assertLess(firm_context, lookup)
        self.assertEqual(connection.executed[firm_context][1], (firm_id,))
        self.assertEqual(connection.executed[lookup][1], (ISSUER, firm_id, user_id))

    def test_inactive_ambiguous_or_system_actor_directory_data_never_resolves(self) -> None:
        firm_id, user_id = str(uuid4()), str(uuid4())
        inactive = _Connection(current_user=IDENTITY_DIRECTORY_ROLE)
        self.assertIsNone(
            self._directory(inactive).resolve_active_actor(actor_id=user_id, firm_id=firm_id, issuer=ISSUER)
        )

        ambiguous = _Connection(
            current_user=IDENTITY_DIRECTORY_ROLE,
            actor_rows=(
                {"active_human_roles": ["LEAD_LAWYER"]},
                {"active_human_roles": ["LEAD_LAWYER"]},
            ),
        )
        with self.assertRaises(WebSessionPersistenceBlocked):
            self._directory(ambiguous).resolve_active_actor(actor_id=user_id, firm_id=firm_id, issuer=ISSUER)

        system = _Connection(
            current_user=IDENTITY_DIRECTORY_ROLE,
            actor_rows=({"active_human_roles": ["SYSTEM_WORKER"]},),
        )
        with self.assertRaises(WebSessionPersistenceBlocked):
            self._directory(system).resolve_active_actor(actor_id=user_id, firm_id=firm_id, issuer=ISSUER)


if __name__ == "__main__":
    import unittest

    unittest.main()
