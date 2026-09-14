from contextlib import contextmanager
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_kernel.case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _payload_hash,
)
from case_kernel.case_work_plan_postgres import PostgresCaseWorkPlanStore
from case_kernel.models import Actor, Role


class _Cursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = [] if rows is None else rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _ReviewConnection:
    def __init__(self, *, fixture: dict[str, object], replay=None):
        self.fixture = fixture
        self.replay = replay
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, tuple(params)))
        if "FROM matters m JOIN matter_actor_roles" in normalized:
            return _Cursor(row={"ok": 1})
        if "FROM case_work_plan_item_reviews" in normalized and "reviewed_by" in normalized:
            return _Cursor(row=self.replay)
        if normalized.startswith("SELECT m.version,"):
            return _Cursor(
                row={
                    "version": self.fixture["expected_version"],
                    "permitted": True,
                }
            )
        if normalized.startswith("SELECT plan.plan_id, plan.plan_hash"):
            return _Cursor(
                row={
                    "plan_id": self.fixture["plan_id"],
                    "plan_hash": self.fixture["plan_hash"],
                    "status": "CANDIDATE",
                    "planned_matter_version": self.fixture["expected_version"] - 1,
                    "plan_version": 2,
                    "latest_plan_version": 2,
                    "item_id": self.fixture["item_id"],
                }
            )
        if normalized.startswith("SELECT 1 FROM case_work_plan_item_reviews"):
            return _Cursor(row=None)
        return _Cursor()


class PostgresCaseWorkPlanWebReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.plan_id = str(uuid4())
        self.item_id = str(uuid4())
        self.actor = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.store = PostgresCaseWorkPlanStore("postgresql://unused")
        self.fixture = {
            "expected_version": 10,
            "plan_id": self.plan_id,
            "item_id": self.item_id,
            "plan_hash": "a" * 64,
        }

    def _transaction(self, connection):
        @contextmanager
        def transaction(_firm_id):
            yield connection

        return transaction

    def test_all_item_review_decisions_append_a_non_mutating_audit_event(self) -> None:
        connection = _ReviewConnection(fixture=self.fixture)
        with patch.object(
            self.store, "_transaction", side_effect=self._transaction(connection)
        ):
            receipt = self.store.review_item(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=10,
                idempotency_key="dynamic-plan-review-0001",
                plan_id=self.plan_id,
                item_id=self.item_id,
                decision="APPROVE",
                reason_code="VERIFIED_BY_COUNSEL",
                readiness_override=None,
                required_for_delivery_override=None,
            )
        self.assertEqual(receipt.matter_version, 10)
        audit = next(
            (entry for entry in connection.executed if "INSERT INTO audit_events" in entry[0]),
            None,
        )
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertIn("'CASE_WORK_PLAN_ITEM_REVIEWED'", audit[0])
        self.assertEqual(audit[1][4:6], (10, 10))
        self.assertFalse(
            any("CASE_WORK_PLAN_REPLANNING_REQUESTED" in sql for sql, _ in connection.executed)
        )

    def test_adverse_review_adds_replanning_outbox_without_advancing_matter(self) -> None:
        connection = _ReviewConnection(fixture=self.fixture)
        with patch.object(
            self.store, "_transaction", side_effect=self._transaction(connection)
        ):
            receipt = self.store.review_item(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=10,
                idempotency_key="dynamic-plan-review-0002",
                plan_id=self.plan_id,
                item_id=self.item_id,
                decision="REQUEST_CHANGE",
                reason_code="REQUIRES_FURTHER_RESEARCH",
                readiness_override="NEEDS_RESEARCH",
                required_for_delivery_override=None,
            )
        self.assertEqual(receipt.matter_version, 10)
        self.assertTrue(
            any("CASE_WORK_PLAN_REPLANNING_REQUESTED" in sql for sql, _ in connection.executed)
        )
        self.assertFalse(any(sql.startswith("UPDATE matters") for sql, _ in connection.executed))

    def test_commit_response_loss_replay_returns_original_review_before_version_lock(self) -> None:
        key = "dynamic-plan-review-0003"
        request_hash = _payload_hash(
            {
                "matter_id": self.matter_id,
                "expected_version": 10,
                "plan_id": self.plan_id,
                "item_id": self.item_id,
                "decision": "APPROVE",
                "reason_code": "VERIFIED_BY_COUNSEL",
                "readiness_override": None,
                "required_for_delivery_override": None,
            }
        )
        review_id = str(uuid4())
        connection = _ReviewConnection(
            fixture=self.fixture,
            replay={
                "review_id": review_id,
                "plan_id": self.plan_id,
                "item_id": self.item_id,
                "decision": "APPROVE",
                "request_hash": request_hash,
                "reviewed_matter_version": 10,
            },
        )
        with patch.object(
            self.store, "_transaction", side_effect=self._transaction(connection)
        ):
            receipt = self.store.review_item(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=10,
                idempotency_key=key,
                plan_id=self.plan_id,
                item_id=self.item_id,
                decision="APPROVE",
                reason_code="VERIFIED_BY_COUNSEL",
                readiness_override=None,
                required_for_delivery_override=None,
            )
        self.assertEqual(receipt.review_id, review_id)
        self.assertEqual(receipt.matter_version, 10)
        self.assertFalse(
            any(sql.startswith("SELECT m.version,") for sql, _ in connection.executed)
        )
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in connection.executed))

    def test_activation_replay_returns_receipt_without_reselecting_or_reconfirming(self) -> None:
        receipt = CaseLedgerCommandReceipt(
            command_name="ACTIVATE_CURRENT_CASE_WORK_PLAN",
            idempotency_key="dynamic-plan-activate-0001",
            matter_id=self.matter_id,
            matter_version=11,
            audit_event_id=str(uuid4()),
            object_type="CASE_WORK_PLAN",
            object_id=self.plan_id,
        )
        connection = _ReviewConnection(fixture=self.fixture)
        with (
            patch.object(
                self.store, "_transaction", side_effect=self._transaction(connection)
            ),
            patch.object(self.store, "_begin", return_value=receipt),
        ):
            replay = self.store.activate_current_plan(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=10,
                idempotency_key="dynamic-plan-activate-0001",
            )
        self.assertIs(replay, receipt)
        self.assertTrue(
            any(
                "FROM matters m JOIN matter_actor_roles" in sql
                for sql, _ in connection.executed
            )
        )
        self.assertFalse(
            any("FROM case_work_plans plan" in sql for sql, _ in connection.executed)
        )
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in connection.executed))


if __name__ == "__main__":
    unittest.main()
