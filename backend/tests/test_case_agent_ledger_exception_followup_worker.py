from __future__ import annotations

from types import SimpleNamespace
import inspect
import re
import unittest
from uuid import uuid4

from case_kernel.case_agent_ledger_exception_followup_worker import (
    PostgresCaseLedgerExceptionFollowupAutomation,
)
from case_kernel.case_agent_worker import CaseAgentWorkerBlocked
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _CommandStore:
    def __init__(
        self, *, lose_bind=False, lose_bind_call=None, lose_satisfy=False
    ) -> None:
        self.bind_calls = []
        self.satisfy_calls = []
        self.lose_bind = lose_bind
        self.lose_bind_call = lose_bind_call
        self.lose_satisfy = lose_satisfy

    def bind_reextraction_task(self, **kwargs):
        self.bind_calls.append(kwargs)
        if (
            (self.lose_bind and len(self.bind_calls) == 1)
            or len(self.bind_calls) == self.lose_bind_call
        ):
            raise TimeoutError("binding commit result was lost")
        return SimpleNamespace(
            task_binding_id=_id(), matter_version=kwargs["expected_version"]
        )

    def satisfy_reextraction_graph(self, **kwargs):
        self.satisfy_calls.append(kwargs)
        if self.lose_satisfy and len(self.satisfy_calls) == 1:
            raise TimeoutError("set commit result was lost")
        return SimpleNamespace(
            followup_count=2,
            matter_version=kwargs["expected_version"] + 1,
        )


class _ReadAutomation(PostgresCaseLedgerExceptionFollowupAutomation):
    def __init__(self, *, rows, worker_actor, store) -> None:
        self.responses = list(rows)
        self.reads = []
        super().__init__(
            dsn="postgresql://not-used.invalid/lawcase",
            worker_actor=worker_actor,
            store=store,
        )

    def _read_rows(self, sql, params):
        self.reads.append((" ".join(sql.split()), params))
        if not self.responses:
            raise AssertionError("unexpected lifecycle read")
        rows = tuple(self.responses.pop(0))
        normalized = []
        for row in rows:
            if set(row) == {"followup_id"}:
                normalized.append({**row, "control_state": "HEALTHY"})
            elif "active_count" in row and "recovery_required_count" not in row:
                normalized.append({**row, "recovery_required_count": 0})
            else:
                normalized.append(row)
        return tuple(normalized)


class LedgerExceptionFollowupWorkerAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.run_id = _id()
        self.graph_id = _id()
        self.task_id = _id()
        self.followup_id = _id()
        self.worker = Actor(
            _id(), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )

    def _automation(self, rows, store=None):
        command_store = store or _CommandStore()
        return (
            _ReadAutomation(
                rows=rows, worker_actor=self.worker, store=command_store
            ),
            command_store,
        )

    def _bind(self, automation):
        return automation.bind_reextraction_task_for_claim(
            matter_id=self.matter_id,
            actor=self.worker,
            expected_version=9,
            run_id=self.run_id,
            graph_id=self.graph_id,
            task_id=self.task_id,
        )

    def _satisfy(self, automation):
        return automation.satisfy_reextraction_graph(
            matter_id=self.matter_id,
            actor=self.worker,
            run_id=self.run_id,
            graph_id=self.graph_id,
            expected_version=9,
            idempotency_key="ledger-reextract-set.00000000-0000-5000-8000-000000000001",
        )

    def test_unrelated_extraction_task_is_a_noop(self) -> None:
        automation, store = self._automation(((),))
        self.assertIsNone(self._bind(automation))
        self.assertEqual(store.bind_calls, [])

    def test_binding_commit_loss_replays_the_same_stable_intent(self) -> None:
        store = _CommandStore(lose_bind=True)
        match = ({"followup_id": self.followup_id},)
        automation, _ = self._automation((match, match), store)

        with self.assertRaises(TimeoutError):
            self._bind(automation)
        receipt = self._bind(automation)

        self.assertIsNotNone(receipt)
        self.assertEqual(len(store.bind_calls), 2)
        self.assertEqual(store.bind_calls[0], store.bind_calls[1])
        self.assertRegex(
            store.bind_calls[0]["idempotency_key"],
            r"^ledger-reextract-bind\.[0-9a-f-]{36}$",
        )

    def test_same_page_fact_and_transaction_followups_bind_as_one_cohort(self) -> None:
        followup_ids = tuple(sorted((self.followup_id, _id())))
        automation, store = self._automation(
            (tuple({"followup_id": value} for value in followup_ids),)
        )

        receipt = self._bind(automation)

        self.assertEqual(receipt.followup_count, 2)
        self.assertEqual(
            tuple(call["followup_id"] for call in store.bind_calls),
            followup_ids,
        )
        self.assertEqual(
            {call["task_id"] for call in store.bind_calls}, {self.task_id}
        )

    def test_partial_cohort_commit_replays_every_exact_binding_intent(self) -> None:
        followup_ids = tuple(sorted((self.followup_id, _id())))
        rows = tuple({"followup_id": value} for value in followup_ids)
        store = _CommandStore(lose_bind_call=2)
        automation, _ = self._automation((rows, rows), store)

        with self.assertRaises(TimeoutError):
            self._bind(automation)
        receipt = self._bind(automation)

        self.assertEqual(receipt.followup_count, 2)
        self.assertEqual(len(store.bind_calls), 4)
        self.assertEqual(store.bind_calls[0], store.bind_calls[2])
        self.assertEqual(store.bind_calls[1], store.bind_calls[3])

    def test_origin_run_b_followup_binds_in_adopted_control_run_a(self) -> None:
        automation, store = self._automation(
            (({"followup_id": self.followup_id},),)
        )

        self._bind(automation)

        sql, params = automation.reads[0]
        self.assertIn("control_assignment.control_run_id", sql)
        self.assertIn("control_head.current_state", sql)
        self.assertIn("task.run_id = followup.control_run_id", sql)
        self.assertNotIn("task.run_id = followup.origin_run_id", sql)
        self.assertEqual(params[2], self.run_id)
        self.assertEqual(store.bind_calls[0]["run_id"], self.run_id)

    def test_recovery_required_control_is_explicitly_blocked(self) -> None:
        automation, store = self._automation(
            (({
                "followup_id": self.followup_id,
                "control_state": "RECOVERY_REQUIRED",
            },),)
        )

        with self.assertRaisesRegex(
            CaseAgentWorkerBlocked, "REEXTRACTION_CONTROL_RECOVERY_REQUIRED"
        ):
            self._bind(automation)
        self.assertEqual(store.bind_calls, [])

    def test_missing_or_duplicate_graph_binding_is_explicitly_blocked(self) -> None:
        automation, store = self._automation(
            (({
                "active_count": 2,
                "obligation_violation_count": 1,
                "completed_count": 0,
            },),)
        )
        with self.assertRaisesRegex(
            CaseAgentWorkerBlocked, "REEXTRACTION_OBLIGATION_UNSATISFIED"
        ):
            self._satisfy(automation)
        self.assertEqual(store.satisfy_calls, [])

    def test_graph_without_active_or_completed_obligations_is_a_noop(self) -> None:
        automation, store = self._automation(
            (({
                "active_count": 0,
                "obligation_violation_count": 0,
                "completed_count": 0,
            },),)
        )
        self.assertIsNone(self._satisfy(automation))
        self.assertEqual(store.satisfy_calls, [])

    def test_lost_atomic_set_commit_reaches_exact_store_replay(self) -> None:
        store = _CommandStore(lose_satisfy=True)
        automation, _ = self._automation(
            (
                ({
                    "active_count": 2,
                    "obligation_violation_count": 0,
                    "completed_count": 0,
                },),
                ({
                    "active_count": 0,
                    "obligation_violation_count": 0,
                    "completed_count": 2,
                },),
            ),
            store,
        )

        with self.assertRaises(TimeoutError):
            self._satisfy(automation)
        receipt = self._satisfy(automation)

        self.assertEqual(receipt.followup_count, 2)
        self.assertEqual(len(store.satisfy_calls), 2)
        self.assertEqual(store.satisfy_calls[0], store.satisfy_calls[1])
        self.assertNotIn("followup_id", store.satisfy_calls[0])
        self.assertNotIn("reextraction_batch_id", store.satisfy_calls[0])

    def test_queries_use_exact_sources_and_current_binding_heads(self) -> None:
        source = inspect.getsource(
            PostgresCaseLedgerExceptionFollowupAutomation
        )
        self.assertIn("jsonb_array_elements_text", source)
        self.assertIn("task.source_refs = followup.source_refs", source)
        self.assertIn("control_run_id IS DISTINCT FROM %s", source)
        self.assertIn("case_agent_ledger_exception_control_heads", source)
        self.assertIn(
            "case_agent_ledger_exception_reextraction_task_binding_heads",
            source,
        )
        self.assertIsNone(re.search(r"except\s+Exception\s*:\s*(?:pass|return)", source))


if __name__ == "__main__":
    unittest.main()
