"""Worker orchestration for exact 0049 re-extraction obligations.

Python selects no completion subset.  It only binds a claimed exact-source
task to one active obligation and, after the whole verified graph is staged,
asks the database to atomically discover and satisfy the complete active set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_ledger_exception_followup_postgres import (
    PostgresCaseLedgerExceptionFollowupStore,
)
from .case_agent_worker import CaseAgentWorkerBlocked
from .models import Actor, Role


_BIND_KEY_NAMESPACE = UUID("5dc0b14d-c8be-5f60-8cf0-47b9dd6556f4")
_BIND_SET_NAMESPACE = UUID("296e08e3-3dad-5bbb-bbd2-8caf860f1899")


@dataclass(frozen=True)
class ReextractionTaskBindingSetReceipt:
    task_binding_id: str
    matter_version: int
    followup_count: int


class PostgresCaseLedgerExceptionFollowupAutomation:
    """Read-only intent matcher around the authoritative 0049 commands."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        store: PostgresCaseLedgerExceptionFollowupStore | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip() or dsn != dsn.strip():
            raise ValueError("exception follow-up Worker PostgreSQL DSN is required")
        _dedicated_worker(worker_actor)
        configured_store = store or PostgresCaseLedgerExceptionFollowupStore(dsn)
        if not all(
            callable(getattr(configured_store, method, None))
            for method in (
                "bind_reextraction_task",
                "satisfy_reextraction_graph",
            )
        ):
            raise ValueError("exception follow-up command store is invalid")
        self._dsn = dsn
        self._worker_actor = worker_actor
        self._store = configured_store

    def bind_reextraction_task_for_claim(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        run_id: str,
        graph_id: str,
        task_id: str,
    ) -> object | None:
        """Bind one exact active obligation before its task executes.

        Freshness is intentionally left to the definer command.  If a task
        has the exact obligation source set but an obsolete graph, it must be
        rejected by the authority rather than mistaken for an unrelated task.
        """

        self._validate_call(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            identifiers=(run_id, graph_id, task_id),
        )
        rows = self._read_rows(
            """
            WITH task_context AS (
                SELECT task.run_id, task.input_refs
                  FROM public.case_agent_tasks task
                 WHERE task.firm_id = %s
                   AND task.matter_id = %s
                   AND task.run_id = %s
                   AND task.graph_id = %s
                   AND task.task_id = %s
                   AND task.skill_id = 'case_ledger_extraction'
                   AND task.skill_version = '1.0.0'
                   AND task.tool_id = 'extract_case_ledger'
                   AND task.tool_version = '1.0.0'
            ),
            task_sources AS (
                SELECT task.run_id,
                       pg_catalog.count(ref.value)::integer AS source_count,
                       pg_catalog.count(DISTINCT ref.value)::integer
                           AS distinct_source_count,
                       pg_catalog.array_agg(
                           DISTINCT ref.value ORDER BY ref.value
                       ) AS source_refs
                  FROM task_context task
                  CROSS JOIN LATERAL pg_catalog.jsonb_array_elements_text(
                      task.input_refs
                  ) ref(value)
                 GROUP BY task.run_id
            ),
            followup_sources AS (
                SELECT followup.followup_id,
                       control_assignment.control_run_id,
                       control_head.current_state AS control_state,
                       pg_catalog.count(
                           DISTINCT page.evidence_page_id
                       )::integer AS source_count,
                       pg_catalog.array_agg(
                           DISTINCT 'evidence-page:' ||
                               page.evidence_page_id::text
                           ORDER BY 'evidence-page:' ||
                               page.evidence_page_id::text
                       ) AS source_refs
                  FROM public.case_agent_ledger_exception_followups followup
                  JOIN public.case_agent_ledger_exception_followup_heads head
                    ON head.followup_id = followup.followup_id
                   AND head.firm_id = followup.firm_id
                   AND head.matter_id = followup.matter_id
                   AND head.current_state = 'ACTIVE'
                  JOIN public.case_agent_ledger_exception_control_heads
                       control_head
                    ON control_head.firm_id = followup.firm_id
                   AND control_head.matter_id = followup.matter_id
                  JOIN public.case_agent_ledger_exception_control_assignments
                       control_assignment
                    ON control_assignment.control_assignment_id =
                           control_head.current_control_assignment_id
                   AND control_assignment.firm_id = control_head.firm_id
                   AND control_assignment.matter_id = control_head.matter_id
                   AND control_assignment.state_after =
                           control_head.current_state
                   AND control_assignment.assignment_sequence =
                           control_head.head_sequence
                  JOIN public.case_agent_ledger_exception_group_members member
                    ON member.exception_group_id =
                        followup.origin_exception_group_id
                   AND member.extraction_batch_id =
                        followup.origin_extraction_batch_id
                   AND member.firm_id = followup.firm_id
                   AND member.matter_id = followup.matter_id
                  JOIN public.case_agent_ledger_extraction_candidate_pages page
                    ON page.extraction_candidate_id =
                        member.extraction_candidate_id
                   AND page.firm_id = member.firm_id
                   AND page.matter_id = member.matter_id
                 WHERE followup.firm_id = %s
                   AND followup.matter_id = %s
                   AND followup.followup_kind = 'REEXTRACTION'
                 GROUP BY followup.followup_id,
                          control_assignment.control_run_id,
                          control_head.current_state
            )
            SELECT followup.followup_id, followup.control_state
              FROM followup_sources followup
              JOIN task_sources task
                ON task.run_id = followup.control_run_id
               AND task.source_count = task.distinct_source_count
               AND task.source_count = followup.source_count
               AND task.source_refs = followup.source_refs
             WHERE followup.source_count > 0
             ORDER BY followup.followup_id
            """,
            (
                actor.firm_id,
                matter_id,
                run_id,
                graph_id,
                task_id,
                actor.firm_id,
                matter_id,
            ),
        )
        if any(
            row.get("control_state") == "RECOVERY_REQUIRED" for row in rows
        ):
            raise CaseAgentWorkerBlocked(
                "REEXTRACTION_CONTROL_RECOVERY_REQUIRED"
            )
        followup_ids = _canonical_followup_ids(rows)
        if not followup_ids:
            return None
        underlying_receipts: list[object] = []
        for followup_id in followup_ids:
            receipt = self._store.bind_reextraction_task(
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=_stable_bind_key(
                    matter_id=matter_id,
                    expected_version=expected_version,
                    followup_id=followup_id,
                    run_id=run_id,
                    graph_id=graph_id,
                    task_id=task_id,
                ),
                followup_id=followup_id,
                run_id=run_id,
                graph_id=graph_id,
                task_id=task_id,
            )
            binding_id = getattr(receipt, "task_binding_id", None)
            _uuid(binding_id, "task_binding_id")
            if getattr(receipt, "matter_version", None) != expected_version:
                raise CaseAgentWorkerBlocked(
                    "re-extraction task binding receipt differs"
                )
            underlying_receipts.append(receipt)
        aggregate_intent = "\n".join(
            (matter_id, run_id, graph_id, task_id, *followup_ids)
        )
        return ReextractionTaskBindingSetReceipt(
            task_binding_id=str(
                uuid5(_BIND_SET_NAMESPACE, aggregate_intent)
            ),
            matter_version=expected_version,
            followup_count=len(underlying_receipts),
        )

    def satisfy_reextraction_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        graph_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> object | None:
        """Atomically satisfy the database-discovered complete active set.

        A completed exact graph is also recognized so a response lost after
        commit reaches the store's durable idempotency replay.  No active or
        completed set is a normal no-op for non-re-extraction graphs.
        """

        self._validate_call(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            identifiers=(run_id, graph_id),
        )
        if (
            not isinstance(idempotency_key, str)
            or not 16 <= len(idempotency_key) <= 128
        ):
            raise ValueError("re-extraction set idempotency key is invalid")
        row = self._read_obligation_state(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
            graph_id=graph_id,
        )
        active_count = _count(row, "active_count")
        violation_count = _count(row, "obligation_violation_count")
        recovery_required_count = _count(row, "recovery_required_count")
        completed_count = _count(row, "completed_count")
        if active_count == 0 and completed_count == 0:
            return None
        if recovery_required_count > 0:
            raise CaseAgentWorkerBlocked(
                "REEXTRACTION_CONTROL_RECOVERY_REQUIRED"
            )
        if violation_count != 0 or (active_count > 0 and completed_count > 0):
            raise CaseAgentWorkerBlocked(
                "REEXTRACTION_OBLIGATION_UNSATISFIED"
            )
        return self._store.satisfy_reextraction_graph(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
            graph_id=graph_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def _read_obligation_state(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        graph_id: str,
    ) -> Mapping[str, Any]:
        rows = self._read_rows(
            """
            WITH active AS (
                SELECT followup.followup_id,
                       control_assignment.control_run_id,
                       control_head.current_state AS control_state
                  FROM public.case_agent_ledger_exception_followups followup
                  JOIN public.case_agent_ledger_exception_followup_heads head
                    ON head.followup_id = followup.followup_id
                   AND head.firm_id = followup.firm_id
                   AND head.matter_id = followup.matter_id
                   AND head.current_state = 'ACTIVE'
                  LEFT JOIN public.case_agent_ledger_exception_control_heads
                       control_head
                    ON control_head.firm_id = followup.firm_id
                   AND control_head.matter_id = followup.matter_id
                  LEFT JOIN public.case_agent_ledger_exception_control_assignments
                       control_assignment
                    ON control_assignment.control_assignment_id =
                           control_head.current_control_assignment_id
                   AND control_assignment.firm_id = control_head.firm_id
                   AND control_assignment.matter_id = control_head.matter_id
                   AND control_assignment.state_after =
                           control_head.current_state
                   AND control_assignment.assignment_sequence =
                           control_head.head_sequence
                 WHERE followup.firm_id = %s
                   AND followup.matter_id = %s
                   AND followup.followup_kind = 'REEXTRACTION'
            ),
            obligation_state AS (
                SELECT active.followup_id, active.control_run_id,
                       active.control_state,
                       (
                           SELECT pg_catalog.count(*)
                             FROM public.case_agent_ledger_exception_reextraction_task_bindings
                                  binding
                            WHERE binding.followup_id = active.followup_id
                              AND binding.firm_id = %s
                              AND binding.matter_id = %s
                              AND binding.run_id = %s
                              AND binding.graph_id = %s
                       ) AS graph_binding_count,
                       EXISTS (
                           SELECT 1
                             FROM public.case_agent_ledger_exception_reextraction_task_binding_heads
                                  binding_head
                             JOIN public.case_agent_ledger_exception_reextraction_task_bindings
                                  current_binding
                               ON current_binding.task_binding_id =
                                    binding_head.current_task_binding_id
                              AND current_binding.followup_id =
                                    binding_head.followup_id
                              AND current_binding.firm_id = binding_head.firm_id
                              AND current_binding.matter_id = binding_head.matter_id
                            WHERE binding_head.followup_id = active.followup_id
                              AND binding_head.firm_id = %s
                              AND binding_head.matter_id = %s
                              AND current_binding.run_id = %s
                              AND current_binding.graph_id = %s
                       ) AS current_graph_bound
                  FROM active
            ),
            completed AS (
                SELECT pg_catalog.count(DISTINCT fulfillment.followup_id)::integer
                           AS completed_count
                  FROM public.case_agent_ledger_exception_reextraction_bindings
                       fulfillment
                  JOIN public.case_agent_ledger_exception_reextraction_task_bindings
                       task_binding
                    ON task_binding.task_binding_id = fulfillment.task_binding_id
                   AND task_binding.followup_id = fulfillment.followup_id
                   AND task_binding.firm_id = fulfillment.firm_id
                   AND task_binding.matter_id = fulfillment.matter_id
                 WHERE fulfillment.firm_id = %s
                   AND fulfillment.matter_id = %s
                   AND fulfillment.bound_by = %s
                   AND task_binding.run_id = %s
                   AND task_binding.graph_id = %s
            )
            SELECT (SELECT pg_catalog.count(*)::integer FROM active)
                       AS active_count,
                   (SELECT pg_catalog.count(*)::integer
                     FROM obligation_state
                     WHERE control_state IS DISTINCT FROM 'HEALTHY'
                        OR control_run_id IS DISTINCT FROM %s
                        OR graph_binding_count <> 1
                        OR current_graph_bound IS DISTINCT FROM true)
                       AS obligation_violation_count,
                   (SELECT pg_catalog.count(*)::integer
                      FROM obligation_state
                     WHERE control_state = 'RECOVERY_REQUIRED')
                       AS recovery_required_count,
                   completed.completed_count
              FROM completed
            """,
            (
                actor.firm_id,
                matter_id,
                actor.firm_id,
                matter_id,
                run_id,
                graph_id,
                actor.firm_id,
                matter_id,
                run_id,
                graph_id,
                actor.firm_id,
                matter_id,
                actor.actor_id,
                run_id,
                graph_id,
                run_id,
            ),
        )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise CaseAgentWorkerBlocked(
                "re-extraction obligation projection is invalid"
            )
        return rows[0]

    def _validate_call(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        identifiers: Sequence[str],
    ) -> None:
        _dedicated_worker(actor)
        if actor != self._worker_actor:
            raise PermissionError(
                "exception follow-up Worker identity differs from composition"
            )
        _uuid(matter_id, "matter_id")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be positive")
        for value in identifiers:
            _uuid(value, "lifecycle identifier")

    def _read_rows(
        self, sql: str, params: tuple[object, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        try:
            with _tenant_read_transaction(
                self._dsn, self._worker_actor.firm_id
            ) as connection:
                return tuple(connection.execute(sql, params).fetchall())
        except CaseAgentWorkerBlocked:
            raise
        except (psycopg.Error, OSError) as error:
            raise CaseAgentWorkerBlocked(
                "exception follow-up intent match failed closed"
            ) from error


@contextmanager
def _tenant_read_transaction(dsn: str, firm_id: str) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            yield connection


def _canonical_followup_ids(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    followup_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "followup_id",
            "control_state",
        }:
            raise CaseAgentWorkerBlocked(
                "re-extraction task obligation match is invalid"
            )
        if row["control_state"] != "HEALTHY":
            raise CaseAgentWorkerBlocked(
                "re-extraction task obligation control is not healthy"
            )
        followup_id = str(row["followup_id"])
        _uuid(followup_id, "followup_id")
        followup_ids.append(followup_id)
    if len(followup_ids) != len(set(followup_ids)):
        raise CaseAgentWorkerBlocked(
            "re-extraction task obligation match is duplicated"
        )
    canonical = tuple(sorted(followup_ids))
    if tuple(followup_ids) != canonical:
        raise CaseAgentWorkerBlocked(
            "re-extraction task obligation match is not canonical"
        )
    return canonical


def _stable_bind_key(
    *,
    matter_id: str,
    expected_version: int,
    followup_id: str,
    run_id: str,
    graph_id: str,
    task_id: str,
) -> str:
    intent = "\n".join(
        (
            matter_id,
            str(expected_version),
            followup_id,
            run_id,
            graph_id,
            task_id,
        )
    )
    return f"ledger-reextract-bind.{uuid5(_BIND_KEY_NAMESPACE, intent)}"


def _count(row: Mapping[str, Any], name: str) -> int:
    value = row.get(name)
    if type(value) is not int or value < 0:
        raise CaseAgentWorkerBlocked(
            "re-extraction obligation projection is invalid"
        )
    return value


def _dedicated_worker(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError(
            "exception follow-up automation requires a dedicated SYSTEM_WORKER"
        )
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "firm_id")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a UUID") from error


__all__ = (
    "PostgresCaseLedgerExceptionFollowupAutomation",
    "ReextractionTaskBindingSetReceipt",
)
