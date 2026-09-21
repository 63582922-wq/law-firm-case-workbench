from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg

from case_kernel.case_agent_ledger_exception_review import (
    LedgerExceptionDecision,
    LedgerExceptionReason,
    LedgerExceptionReextractionCohortCapacityExceeded,
    LedgerExceptionReextractionSourceWindowExceeded,
    LedgerExceptionReviewBlocked,
    LedgerExceptionRiskPolicy,
    LedgerExceptionSourcePolicy,
    exception_candidate_set_hash,
    exception_group_key_hash,
)
from case_kernel.case_agent_ledger_exception_review_postgres import (
    PostgresCaseLedgerExceptionReviewStore,
)
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Result:
    def __init__(self, *, row=None, rows=()) -> None:
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, *, receipt, session_id, actor, command_error=None) -> None:
        self.receipt = receipt
        self.session_id = session_id
        self.actor = actor
        self.command_error = command_error
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT set_config"):
            return _Result()
        if "decide_case_agent_ledger_exception_group_from_web_session" in normalized:
            if self.command_error is not None:
                raise self.command_error
            return _Result(row={"receipt": self.receipt})
        raise AssertionError(f"unexpected direct SQL outside 0048 boundary: {normalized}")


class _VersionConflictError(psycopg.DatabaseError):
    sqlstate = "P4091"


class _TerminalIntentConflictError(psycopg.DatabaseError):
    sqlstate = "P4092"


class _SourceWindowExceededError(psycopg.DatabaseError):
    sqlstate = "P6401"


class _CohortCapacityExceededError(psycopg.DatabaseError):
    sqlstate = "P9901"


class _UnknownDefinerError(psycopg.DatabaseError):
    sqlstate = "P0001"


class _Context:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class PostgresLedgerExceptionDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.group_id = _id()
        self.run_id = _id()
        self.session_id = _id()
        self.actor = Actor(
            _id(), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.store = PostgresCaseLedgerExceptionReviewStore(
            "postgresql://not-used.invalid/lawcase"
        )

    def _receipt(self, *, final: bool):
        return {
            "command_name": "DECIDE_CASE_LEDGER_EXCEPTION_GROUP",
            "idempotency_key": "exception-route-0001",
            "matter_id": self.matter_id,
            "matter_version": 10 if final else 9,
            "audit_event_id": _id(),
            "object_type": (
                "CASE_LEDGER_EXTRACTION_RUN_REVIEW"
                if final
                else "CASE_LEDGER_EXCEPTION_GROUP"
            ),
            "object_id": self.run_id if final else self.group_id,
        }

    def _call(self, connection):
        with patch(
            "case_kernel.case_agent_ledger_exception_review_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            return self.store.decide_exception_group(
                matter_id=self.matter_id,
                actor=self.actor,
                server_session_id=self.session_id,
                expected_version=9,
                idempotency_key="exception-route-0001",
                exception_group_id=self.group_id,
                decision=LedgerExceptionDecision.DEFER_WITH_REASON,
                reason=LedgerExceptionReason.AWAITING_CLIENT_INPUT,
                reason_note=" 等待当事人补充流水 ",
            )

    def test_intermediate_or_mixed_terminal_route_does_not_advance_matter(self) -> None:
        connection = _Connection(
            receipt=self._receipt(final=False),
            session_id=self.session_id,
            actor=self.actor,
        )
        receipt = self._call(connection)
        self.assertEqual(receipt.matter_version, 9)
        call = next(
            item for item in connection.executed
            if "decide_case_agent_ledger_exception_group_from_web_session" in item[0]
        )
        self.assertEqual(call[1][0], self.session_id)
        self.assertEqual(call[1][2], self.group_id)
        self.assertEqual(call[1][7], "等待当事人补充流水")
        self.assertFalse(
            any("FROM web_sessions" in sql for sql, _ in connection.executed)
        )

    def test_exception_only_final_route_requires_exactly_one_version_advance(self) -> None:
        connection = _Connection(
            receipt=self._receipt(final=True),
            session_id=self.session_id,
            actor=self.actor,
        )
        receipt = self._call(connection)
        self.assertEqual(receipt.matter_version, 10)
        self.assertEqual(receipt.object_type, "CASE_LEDGER_EXTRACTION_RUN_REVIEW")

    def test_commit_lost_replay_uses_the_same_session_definer_command(self) -> None:
        for final in (False, True):
            with self.subTest(final=final):
                connection = _Connection(
                    receipt=self._receipt(final=final),
                    session_id=self.session_id,
                    actor=self.actor,
                )
                first = self._call(connection)
                second = self._call(connection)
                self.assertEqual(first, second)
                self.assertEqual(
                    sum(
                        "decide_case_agent_ledger_exception_group_from_web_session" in sql
                        for sql, _ in connection.executed
                    ),
                    2,
                )

    def test_only_explicit_definer_conflicts_map_to_http_conflict_domains(self) -> None:
        cases = (
            (_VersionConflictError("stale"), VersionConflict),
            (_TerminalIntentConflictError("other intent"), IdempotencyConflict),
        )
        for database_error, expected_error in cases:
            with self.subTest(sqlstate=database_error.sqlstate):
                connection = _Connection(
                    receipt=None,
                    session_id=self.session_id,
                    actor=self.actor,
                    command_error=database_error,
                )
                with self.assertRaises(expected_error):
                    self._call(connection)

    def test_generic_definer_or_database_failure_is_not_mapped_to_conflict(self) -> None:
        database_error = _UnknownDefinerError("unknown database failure")
        connection = _Connection(
            receipt=None,
            session_id=self.session_id,
            actor=self.actor,
            command_error=database_error,
        )
        with self.assertRaises(_UnknownDefinerError):
            self._call(connection)

    def test_only_explicit_0049_capacity_states_map_to_controlled_domains(self) -> None:
        cases = (
            (
                _SourceWindowExceededError("source window"),
                LedgerExceptionReextractionSourceWindowExceeded,
            ),
            (
                _CohortCapacityExceededError("cohort capacity"),
                LedgerExceptionReextractionCohortCapacityExceeded,
            ),
        )
        for database_error, expected_error in cases:
            with self.subTest(sqlstate=database_error.sqlstate):
                connection = _Connection(
                    receipt=None,
                    session_id=self.session_id,
                    actor=self.actor,
                    command_error=database_error,
                )
                with self.assertRaises(expected_error):
                    self._call(connection)

    def test_public_write_signature_cannot_accept_candidate_subset_or_hash(self) -> None:
        parameters = inspect.signature(
            PostgresCaseLedgerExceptionReviewStore.decide_exception_group
        ).parameters
        self.assertIn("server_session_id", parameters)
        for forbidden in (
            "candidate_id",
            "candidate_ids",
            "candidate_hash",
            "candidate_hashes",
            "evidence_page_ids",
            "subset",
        ):
            self.assertNotIn(forbidden, parameters)

    def test_member_reason_drift_is_rejected_before_a_group_is_projected(self) -> None:
        candidate_hash = "a" * 64
        reasons = ("OCR_DERIVED",)

        class MemberConnection:
            def execute(self, _sql, _params=None):
                return _Result(
                    rows=(
                        {
                            "candidate_hash": candidate_hash,
                            "candidate_kind": "FACT",
                            "review_reason_codes": ["PARTY_AMBIGUOUS"],
                            "review_lane": "EXCEPTION_REVIEW",
                            "eligible_for_bulk_promotion": False,
                        },
                    )
                )

        row = {
            "exception_group_id": self.group_id,
            "extraction_batch_id": _id(),
            "candidate_kind": "FACT",
            "canonical_reason_codes": list(reasons),
            "source_policy": "SOURCE_REVERIFICATION_REQUIRED",
            "risk_policy": "LOW_CONFIDENCE_REVIEW",
            "group_key_hash": exception_group_key_hash(
                candidate_kind="FACT",
                reason_codes=reasons,
                source_policy=(
                    LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
                ),
                risk_policy=LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW,
            ),
            "candidate_set_hash": exception_candidate_set_hash((candidate_hash,)),
            "candidate_count": 1,
            "decision": None,
            "reason_code": None,
        }
        with self.assertRaisesRegex(
            LedgerExceptionReviewBlocked, "member differs"
        ):
            self.store._validate_group(
                MemberConnection(),
                row=row,
                actor=self.actor,
                matter_id=self.matter_id,
            )


if __name__ == "__main__":
    unittest.main()
