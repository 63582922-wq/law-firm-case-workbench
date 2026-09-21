from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_api.web_agent_ledger_extraction_review_postgres import (
    PostgresAgentLedgerExtractionReviewStore,
    WebAgentLedgerExtractionReviewPersistenceBlocked,
    _BATCH_SQL,
    _CANDIDATE_SQL,
    preflight_web_ledger_confirmation_session_authority,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Result:
    def __init__(self, *, row=None, rows=None) -> None:
        self._row = row
        self._rows = [] if rows is None else rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, *, batch_rows, candidate_rows, permitted: bool = True) -> None:
        self.batch_rows = batch_rows
        self.candidate_rows = candidate_rows
        self.permitted = permitted
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return _Result(row={"authorized": 1} if self.permitted else None)
        if "FROM case_agent_ledger_extraction_batches batch" in normalized:
            return _Result(rows=self.batch_rows)
        if "FROM case_agent_ledger_extraction_candidates candidate" in normalized:
            return _Result(rows=self.candidate_rows)
        return _Result()


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class _ConfirmationStore:
    def __init__(self) -> None:
        self.calls = []

    def confirm_low_risk_batch(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(ok=True)


class _AuthorityPreflightConnection:
    function_names = (
        "authorize_case_agent_ledger_extraction_low_risk_confirmation",
        "finalize_case_agent_ledger_extraction_low_risk_confirmation",
        "decide_case_agent_ledger_exception_group_from_web_session",
    )
    function_argument_counts = (6, 2, 9)
    relation_names = (
        "web_sessions",
        "case_agent_ledger_extraction_session_approvals",
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_staging_events",
        "case_agent_ledger_extraction_candidates",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_extraction_promotions",
        "case_agent_ledger_extraction_batch_confirmations",
        "case_agent_ledger_exception_groups",
        "case_agent_ledger_exception_group_members",
        "case_agent_ledger_exception_group_decisions",
        "case_agent_ledger_exception_decision_events",
    )
    trigger_names = (
        "case_agent_ledger_extraction_session_approvals_append_only",
        "case_facts_session_bound_extraction_target_immutable",
        "case_transactions_session_bound_extraction_target_immutable",
    )

    def __init__(self, *, drift: str | None = None) -> None:
        self.drift = drift

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith("SET TRANSACTION"):
            return _Result()
        if "owner.rolcanlogin" in normalized:
            return _Result(
                row={
                    "current_role": (
                        "wrong_application" if self.drift == "role" else
                        "lawcase_web_application"
                    ),
                    "owner_can_login": False,
                    "owner_inherits": False,
                    "owner_is_super": False,
                    "owner_bypasses_rls": False,
                    "application_is_owner_member": False,
                }
            )
        if "pg_catalog.aclexplode" in normalized:
            return _Result(
                rows=[
                    {
                        "proname": name,
                        "pronargs": count,
                        "prosecdef": True,
                        "owner_name": "lawcase_ledger_confirmation_owner",
                        "proconfig": ["search_path=pg_catalog"],
                        "application_can_execute": not (
                            self.drift == "function_grant" and index == 0
                        ),
                        "public_can_execute": False,
                    }
                    for index, (name, count) in enumerate(
                        zip(
                            self.function_names,
                            self.function_argument_counts,
                            strict=True,
                        )
                    )
                ]
            )
        if "pg_catalog.has_table_privilege" in normalized:
            if normalized.startswith("WITH required_table"):
                return _Result(
                    row={
                        "owner_table_grants_complete": (
                            self.drift != "owner_grant"
                        ),
                        "owner_column_grants_complete": True,
                        "owner_function_grants_complete": True,
                    }
                )
            return _Result(
                rows=[
                    {
                        "relname": name,
                        "relrowsecurity": True,
                        "relforcerowsecurity": not (
                            self.drift == "force_rls" and name == "web_sessions"
                        ),
                        "owner_name": (
                            "lawcase_ledger_confirmation_owner"
                            if name == "case_agent_ledger_extraction_session_approvals"
                            else "lawcase_schema_owner"
                        ),
                        "application_can_insert": (
                            self.drift == "direct_dml"
                            and name == "case_agent_ledger_extraction_promotions"
                        ),
                        "application_can_update": False,
                        "application_can_delete": False,
                        "application_can_truncate": False,
                    }
                    for name in self.relation_names
                ]
            )
        if "FROM pg_catalog.pg_policy" in normalized:
            return _Result(
                rows=[
                    {
                        "polcmd": "r",
                        "policy_role": (
                            "lawcase_web_application"
                            if self.drift == "policy"
                            else "lawcase_ledger_confirmation_owner"
                        ),
                        "using_expression": (
                            "session_id::text = current_setting('app.web_session_id', true)"
                        ),
                    }
                ]
            )
        if "FROM pg_catalog.pg_trigger" in normalized:
            return _Result(
                rows=[
                    {"tgname": name, "tgenabled": "O"}
                    for name in self.trigger_names
                    if not (
                        self.drift == "trigger"
                        and name == self.trigger_names[0]
                    )
                ]
            )
        raise AssertionError(normalized)


class PostgresAgentLedgerExtractionReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.run_id = _id()
        self.graph_id = _id()
        self.batch_id = _id()
        self.candidate_id = _id()
        self.page_id = _id()
        self.actor = Actor(
            _id(), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.now = datetime.now(timezone.utc)

    def _batch_row(self, **overrides):
        row = {
            "extraction_batch_id": self.batch_id,
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "source_matter_version": 8,
            "staged_matter_version": 8,
            "candidate_count": 1,
            "eligible_candidate_count": 1,
            "created_at": self.now,
            "current_matter_version": 9,
            "current_graph_id": self.graph_id,
            "run_status": "READY_FOR_REVIEW",
            "is_stale": False,
            "is_cancelled": False,
            "confirmed_candidate_count": None,
            "confirmed_matter_version": None,
            "confirmed_at": None,
            "promotion_count": 0,
            "current_review_version": 9,
            "run_staging_complete": True,
        }
        row.update(overrides)
        return row

    def _candidate_row(self, **overrides):
        payload = {
            "candidate_hash": "a" * 64,
            "kind": "FACT",
            "source_refs": [f"evidence-page:{self.page_id}"],
            "evidence_page_ids": [self.page_id],
            "confidence": 0.995,
            "conflict_codes": [],
            "risk_codes": [],
            "supporting_excerpts": [
                {
                    "evidence_page_id": self.page_id,
                    "text": "转账人民币壹拾万元整",
                }
            ],
            "fact_text": "借款本金已通过银行转账交付。",
        }
        row = {
            "extraction_batch_id": self.batch_id,
            "extraction_candidate_id": self.candidate_id,
            "candidate_kind": "FACT",
            "confidence": 0.995,
            "review_lane": "BULK_PROMOTION_ELIGIBLE",
            "eligible_for_bulk_promotion": True,
            "review_reason_codes": [],
            "review_status": "NEEDS_LAWYER_REVIEW",
            "candidate_payload": payload,
            "source_pages": [
                {"evidence_page_id": self.page_id, "page_number": 3}
            ],
        }
        row.update(overrides)
        return row

    def _store(self, connection: _Connection, confirmation_store=None):
        return PostgresAgentLedgerExtractionReviewStore(
            "postgresql://lawcase_web_application:secret@db.internal/lawcase",
            confirmation_store=confirmation_store or _ConfirmationStore(),
            connection_factory=lambda: _ConnectionContext(connection),
        )

    def test_same_run_prior_ledger_confirmation_remains_review_ready(self) -> None:
        connection = _Connection(
            batch_rows=[self._batch_row()],
            candidate_rows=[self._candidate_row()],
        )
        batches = self._store(connection).list_review_batches(
            matter_id=self.matter_id, actor=self.actor
        )
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0].is_current)
        self.assertTrue(batches[0].exception_review_is_current)
        self.assertEqual(batches[0].current_matter_version, 9)
        self.assertEqual(batches[0].low_risk_count, 1)
        self.assertEqual(
            batches[0].candidates[0].summary,
            "借款本金已通过银行转账交付。",
        )
        self.assertEqual(batches[0].candidates[0].excerpts[0].page_number, 3)
        self.assertTrue(
            any(
                sql.startswith(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                for sql, _ in connection.executed
            )
        )

    def test_unrelated_version_advance_marks_batch_stale(self) -> None:
        connection = _Connection(
            batch_rows=[self._batch_row(current_review_version=None)],
            candidate_rows=[self._candidate_row()],
        )
        batch = self._store(connection).list_review_batches(
            matter_id=self.matter_id, actor=self.actor
        )[0]
        self.assertFalse(batch.is_current)
        self.assertFalse(batch.exception_review_is_current)

    def test_any_prior_promotion_marks_unconfirmed_batch_stale(self) -> None:
        connection = _Connection(
            batch_rows=[self._batch_row(promotion_count=1)],
            candidate_rows=[self._candidate_row()],
        )
        batch = self._store(connection).list_review_batches(
            matter_id=self.matter_id, actor=self.actor
        )[0]
        self.assertFalse(batch.is_current)

    def test_confirmed_batch_remains_visible_for_response_loss_reconciliation(self) -> None:
        connection = _Connection(
            batch_rows=[
                self._batch_row(
                    current_matter_version=10,
                    confirmed_candidate_count=1,
                    confirmed_matter_version=10,
                    confirmed_at=self.now,
                    promotion_count=1,
                )
            ],
            candidate_rows=[self._candidate_row()],
        )
        batch = self._store(connection).list_review_batches(
            matter_id=self.matter_id, actor=self.actor
        )[0]
        self.assertEqual(batch.confirmed_matter_version, 10)
        self.assertFalse(batch.is_current)
        self.assertFalse(batch.exception_review_is_current)

    def test_confirmed_low_risk_batch_keeps_current_exception_review_chain(self) -> None:
        connection = _Connection(
            batch_rows=[
                self._batch_row(
                    current_matter_version=10,
                    current_review_version=10,
                    confirmed_candidate_count=1,
                    confirmed_matter_version=10,
                    confirmed_at=self.now,
                    promotion_count=1,
                )
            ],
            candidate_rows=[self._candidate_row()],
        )
        batch = self._store(connection).list_review_batches(
            matter_id=self.matter_id, actor=self.actor
        )[0]
        self.assertFalse(batch.is_current)
        self.assertTrue(batch.exception_review_is_current)

    def test_firm_scoped_factory_is_fail_closed_and_preserves_human_actor(self) -> None:
        connection = _Connection(batch_rows=[], candidate_rows=[])
        confirmation = _ConfirmationStore()
        calls = []

        def factory(actor):
            calls.append(actor)
            return confirmation

        store = PostgresAgentLedgerExtractionReviewStore(
            "postgresql://lawcase_web_application:secret@db.internal/lawcase",
            confirmation_store_factory=factory,
            configured_firm_ids=frozenset({self.firm_id}),
            connection_factory=lambda: _ConnectionContext(connection),
        )
        self.assertTrue(store.is_available_for_actor(actor=self.actor))
        store.confirm_low_risk_batch(
            matter_id=self.matter_id,
            actor=self.actor,
            expected_version=9,
            idempotency_key="ledger-extraction-confirm-0001",
            extraction_batch_id=self.batch_id,
        )
        self.assertEqual(calls, [self.actor])
        self.assertIs(confirmation.calls[0]["actor"], self.actor)
        other = Actor(_id(), _id(), frozenset({Role.LEAD_LAWYER}))
        self.assertFalse(store.is_available_for_actor(actor=other))

    def test_projection_queries_do_not_select_browser_forbidden_secrets(self) -> None:
        sql = f"{_BATCH_SQL}\n{_CANDIDATE_SQL}".lower()
        for forbidden in (
            "candidate_hash",
            "object_key",
            "provider_id",
            "model_id",
            "prompt",
            "task_input_hash",
            "input_refs",
        ):
            self.assertNotIn(forbidden, sql)
        self.assertIn("current_review_version", _BATCH_SQL)
        self.assertIn("case_agent_ledger_extraction_run_staging_complete", _BATCH_SQL)
        self.assertNotIn("case_agent_work_plan_promotions", _BATCH_SQL)

    def test_0048_authority_preflight_accepts_complete_effective_boundary(self) -> None:
        connection = _AuthorityPreflightConnection()
        with patch(
            "case_api.web_agent_ledger_extraction_review_postgres.psycopg.connect",
            return_value=_ConnectionContext(connection),
        ):
            preflight_web_ledger_confirmation_session_authority(
                dsn="postgresql://lawcase_web_application:secret@db.internal/lawcase"
            )

    def test_0048_authority_preflight_rejects_every_privilege_boundary_drift(self) -> None:
        for drift in (
            "role",
            "function_grant",
            "force_rls",
            "direct_dml",
            "owner_grant",
            "policy",
            "trigger",
        ):
            with self.subTest(drift=drift):
                connection = _AuthorityPreflightConnection(drift=drift)
                with (
                    patch(
                        "case_api.web_agent_ledger_extraction_review_postgres.psycopg.connect",
                        return_value=_ConnectionContext(connection),
                    ),
                    self.assertRaises(
                        WebAgentLedgerExtractionReviewPersistenceBlocked
                    ),
                ):
                    preflight_web_ledger_confirmation_session_authority(
                        dsn=(
                            "postgresql://lawcase_web_application:secret@"
                            "db.internal/lawcase"
                        )
                    )


if __name__ == "__main__":
    unittest.main()
