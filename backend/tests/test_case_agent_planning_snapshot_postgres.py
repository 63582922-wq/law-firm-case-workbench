from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import replace
from decimal import Decimal
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_planning_snapshot import (
    CasePlanningProjectionBlocked,
    PlanningProjectionObjectType,
    ProjectionSectionState,
)
from case_kernel.case_agent_lawyer_decisions import (
    GovernedLawyerPlanningDecision,
    LawyerPlanningDecisionCode,
)
from case_kernel.case_agent_planning_snapshot_postgres import (
    PostgresCasePlanningProjectionRepository,
    _read_active_reextraction_planning_obligations,
    _read_capabilities,
    _read_case_ledger,
    _read_ledger_exception_decision_signals,
)
from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Result:
    def __init__(self, *, row=None, rows=()) -> None:
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Context:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class _PlanningConnection:
    def __init__(
        self,
        *,
        firm_id: str,
        matter_id: str,
        actor_id: str,
        authorized: bool = True,
        opening_version: int = 9,
        closing_version: int | None = None,
        has_0035: bool = True,
        has_0047: bool = False,
        has_0049: bool = False,
        partial_0049: bool = False,
        graph_hash_matches: bool = True,
        agent_signal_hash: str = "9" * 64,
        plan_activated_version: int | None = None,
    ) -> None:
        self.firm_id = firm_id
        self.matter_id = matter_id
        self.actor_id = actor_id
        self.authorized = authorized
        self.opening_version = opening_version
        self.closing_version = opening_version if closing_version is None else closing_version
        self.has_0035 = has_0035
        self.has_0047 = has_0047
        self.has_0049 = has_0049
        self.partial_0049 = partial_0049
        self.graph_hash_matches = graph_hash_matches
        self.agent_signal_hash = agent_signal_hash
        self.plan_activated_version = (
            opening_version
            if plan_activated_version is None
            else plan_activated_version
        )
        self.executed: list[tuple[str, tuple | None]] = []
        self._matter_reads = 0

        stable = lambda label: str(uuid5(UUID(matter_id), label))
        self.fact_id = stable("fact")
        self.claim_id = stable("claim")
        self.response_id = stable("response")
        self.issue_id = stable("issue")
        self.transaction_id = stable("transaction")
        self.file_id = stable("file")
        self.page_id = stable("page")
        self.decision_id = stable("page-decision")
        self.material_id = stable("material")
        self.profile_id = stable("profile")
        self.plan_id = stable("plan")
        self.plan_item_id = stable("plan-item")
        self.bundle_id = stable("bundle")
        self.source_id = stable("source")
        self.rule_version_id = stable("legal-rule-version")
        self.procedure_event_id = stable("procedure-event")
        self.run_id = stable("run")
        self.graph_id = stable("graph")
        self.task_id = stable("task")
        self.signal_id = stable("signal")
        self.lead_id = stable("lead")
        self.agent_decision = GovernedLawyerPlanningDecision.build(
            signal_id=self.signal_id,
            run_id=self.run_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            graph_id=self.graph_id,
            task_id=self.task_id,
            signal_version=1,
            decision_code=LawyerPlanningDecisionCode.WRONG_FACT_ASSUMPTION,
            note=f"[合成] 纠正版本 {self.agent_signal_hash[:1]}",
            source_ref_ids=(f"fact:{self.fact_id}",),
            task_input_hash="0" * 64,
            graph_hash="f" * 64,
            recorded_event_sequence=7,
            recorded_by=self.lead_id,
            decided_at=datetime(2026, 8, 13, 8, tzinfo=timezone.utc),
        )

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SET TRANSACTION") or normalized.startswith(
            "SELECT set_config"
        ):
            return _Result()
        if normalized.startswith("SELECT 1 FROM matters matter JOIN matter_actor_roles"):
            return _Result(row={"ok": 1} if self.authorized else None)
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return _Result(row={"ok": 1} if self.authorized else None)
        if "to_regclass('public.matters')" in normalized:
            return _Result(
                row={
                    "has_matters": True,
                    "has_case_facts": True,
                    "has_case_claims": True,
                    "has_case_transactions": True,
                    "has_evidence_files": True,
                    "has_evidence_pages": True,
                    "has_materials": True,
                    "has_posture": True,
                    "has_work_plan": True,
                    "has_legal": True,
                    "has_procedure": True,
                    "has_lawyer_signals": self.has_0035,
                    "has_any_ledger_exception_review": self.has_0047,
                    "has_ledger_exception_review": self.has_0047,
                    "has_any_ledger_exception_followups": (
                        self.has_0049 or self.partial_0049
                    ),
                    "has_ledger_exception_followups": self.has_0049,
                }
            )
        if normalized.startswith("SELECT matter_id, title, stage, version FROM matters"):
            self._matter_reads += 1
            version = self.opening_version if self._matter_reads == 1 else self.closing_version
            return _Result(
                row={
                    "matter_id": self.matter_id,
                    "title": "[合成] 原子规划投影案件",
                    "stage": "LEGAL_REVIEW",
                    "version": version,
                }
            )
        if normalized.startswith(
            "SELECT fact_id, original_text, origin, status, jsonb_array_length(evidence_links)"
        ):
            return _Result(
                rows=(
                    {
                        "fact_id": self.fact_id,
                        "original_text": "[合成] 一项律师标记争议的事实。",
                        "origin": "DEFENDANT_STATEMENT",
                        "status": "DISPUTED",
                        "evidence_count": 1,
                        "decision_hash": "1" * 64,
                        "decided_by": self.lead_id,
                    },
                )
            )
        if normalized.startswith(
            "SELECT claim_id, original_claim_text, claimed_amount, currency, status"
        ):
            return _Result(
                rows=(
                    {
                        "claim_id": self.claim_id,
                        "original_claim_text": "[合成] 一项经确认范围的诉请。",
                        "claimed_amount": Decimal("1000.00"),
                        "currency": "CNY",
                        "status": "CONFIRMED_SCOPE",
                        "evidence_count": 1,
                        "confirmation_hash": "2" * 64,
                        "confirmed_by": self.lead_id,
                    },
                )
            )
        if normalized.startswith(
            "SELECT claim_response_id, claim_id, position, partial_amount, currency"
        ):
            return _Result(
                rows=(
                    {
                        "claim_response_id": self.response_id,
                        "claim_id": self.claim_id,
                        "position": "DISPUTE",
                        "partial_amount": None,
                        "currency": None,
                        "approval_hash": "3" * 64,
                        "approved_by": self.lead_id,
                    },
                )
            )
        if normalized.startswith("SELECT claim_response_id, fact_id FROM case_claim_response_facts"):
            return _Result(rows=({"claim_response_id": self.response_id, "fact_id": self.fact_id},))
        if normalized.startswith("SELECT issue_id, question, status, approval_hash, approved_by"):
            return _Result(
                rows=(
                    {
                        "issue_id": self.issue_id,
                        "question": "[合成] 本案争点。",
                        "status": "CONFIRMED",
                        "approval_hash": "4" * 64,
                        "approved_by": self.lead_id,
                    },
                )
            )
        if normalized.startswith("SELECT issue_id, claim_id FROM case_dispute_issue_claims"):
            return _Result(rows=({"issue_id": self.issue_id, "claim_id": self.claim_id},))
        if normalized.startswith("SELECT issue_id, fact_id FROM case_dispute_issue_facts"):
            return _Result(rows=({"issue_id": self.issue_id, "fact_id": self.fact_id},))
        if normalized.startswith(
            "SELECT transaction_id, local_date, date_precision, amount, currency"
        ):
            return _Result(
                rows=(
                    {
                        "transaction_id": self.transaction_id,
                        "local_date": date(2020, 8, 20),
                        "date_precision": "EXACT_DATE",
                        "amount": Decimal("500.00"),
                        "currency": "CNY",
                        "direction": "OUTGOING",
                        "payer_label": "[合成] 付款方",
                        "payee_label": "[合成] 收款方",
                        "channel": "WECHAT",
                        "transaction_reference": "synthetic-reference",
                        "status": "CONFIRMED",
                        "evidence_count": 1,
                        "confirmation_hash": "5" * 64,
                        "confirmed_by": self.lead_id,
                    },
                )
            )
        if normalized.startswith(
            "SELECT classification_id, transaction_id, origin, nature, same_day_sequence"
        ):
            return _Result()
        if normalized.startswith("SELECT classification_id, obligation_id, amount, currency"):
            return _Result()
        if normalized.startswith(
            "SELECT duplicate_group_id, status, canonical_transaction_id"
        ):
            return _Result()
        if normalized.startswith("SELECT duplicate_group_id, transaction_id"):
            return _Result()
        if normalized.startswith(
            "SELECT material.material_object_id, material.content_sha256"
        ):
            return _Result(
                rows=(
                    {
                        "material_object_id": self.material_id,
                        "content_sha256": "6" * 64,
                    },
                )
            )
        if normalized.startswith(
            "SELECT page.evidence_page_id, page.evidence_file_id, page.page_number"
        ):
            return _Result(
                rows=(
                    {
                        "evidence_page_id": self.page_id,
                        "evidence_file_id": self.file_id,
                        "page_number": 1,
                        "rendered_page_sha256": "7" * 64,
                        "original_file_sha256": "8" * 64,
                        "media_type": "application/pdf",
                        "decision_id": self.decision_id,
                        "disposition": "INCLUDE",
                        "decision_status": "APPROVED",
                        "approval_hash": "a" * 64,
                    },
                )
            )
        if normalized.startswith(
            "SELECT profile.profile_id, profile.profile_version, profile.profile_hash"
        ):
            return _Result(
                row={
                    "profile_id": self.profile_id,
                    "profile_version": 2,
                    "profile_hash": "b" * 64,
                    "case_type_code": "CIVIL.PRIVATE_LENDING",
                    "procedure_stage": "FIRST_INSTANCE",
                    "represented_position": "DEFENDANT",
                    "authority_scope_code": "GENERAL_AUTHORITY",
                    "engagement_state": "ACTIVE",
                    "effective_status": "CURRENT",
                }
            )
        if normalized.startswith(
            "SELECT plan.plan_id, plan.plan_version, plan.status, plan.plan_hash"
        ):
            return _Result(
                row={
                    "plan_id": self.plan_id,
                    "plan_version": 3,
                    "status": "ACTIVE",
                    "plan_hash": "c" * 64,
                    "activated_matter_version": self.plan_activated_version,
                }
            )
        if normalized.startswith(
            "SELECT item_id, sequence, item_kind, readiness, title, purpose, rationale"
        ):
            return _Result(
                rows=(
                    {
                        "item_id": self.plan_item_id,
                        "sequence": 1,
                        "item_kind": "RESEARCH_TASK",
                        "readiness": "ACTIONABLE",
                        "title": "核对现行法源",
                        "purpose": "确认本案争点适用的现行规则及版本。",
                        "rationale": "当前工作计划要求核对法源。",
                        "risk_if_omitted": "可能引用不适用或失效的规则。",
                        "confidence": Decimal("0.90000"),
                        "review_gate": "LEGAL_AUTHORITY_REVIEW",
                        "delivery_target": "NOT_APPLICABLE",
                        "deliverable_kind": None,
                        "required_for_delivery": False,
                        "is_primary_document": False,
                    },
                )
            )
        if normalized.startswith("SELECT bundle_id FROM case_legal_bundles"):
            return _Result(row={"bundle_id": self.bundle_id})
        if normalized.startswith("SELECT source_snapshot_id AS snapshot_id"):
            return _Result(
                rows=({"snapshot_id": self.source_id, "expected_hash": "d" * 64},)
            )
        if normalized.startswith("SELECT snapshot_id, content_sha256"):
            return _Result(
                rows=({"snapshot_id": self.source_id, "content_sha256": "d" * 64},)
            )
        if normalized.startswith("SELECT DISTINCT segment.rule_version_id"):
            return _Result(
                rows=(
                    {
                        "rule_version_id": self.rule_version_id,
                        "segment_rule_version": "2026.01",
                        "approved_rule_version": "2026.01",
                        "approved_rule_status": "APPROVED",
                        "approval_hash": "f" * 64,
                    },
                )
            )
        if normalized.startswith("SELECT legal_event_id, event_kind, local_date, approval_hash"):
            return _Result(
                rows=(
                    {
                        "legal_event_id": self.procedure_event_id,
                        "event_kind": "CASE_ACCEPTED",
                        "local_date": date(2026, 8, 1),
                        "approval_hash": "e" * 64,
                    },
                )
            )
        if normalized.startswith(
            "SELECT signal.signal_id, signal.signal_version, signal.decision_code"
        ):
            decision = self.agent_decision
            return _Result(
                rows=(
                    {
                        "signal_id": decision.signal_id,
                        "signal_version": decision.signal_version,
                        "decision_code": decision.decision_code.value,
                        "category": decision.category.value,
                        "signal_status": decision.status.value,
                        "summary": decision.summary,
                        "source_ref_ids": list(decision.source_ref_ids),
                        "decision_hash": decision.decision_hash,
                        "task_input_hash": decision.task_input_hash,
                        "graph_hash": decision.graph_hash,
                        "subject_hash": decision.subject_hash,
                        "recorded_event_sequence": decision.recorded_event_sequence,
                        "recorded_by": decision.recorded_by,
                        "decided_at": decision.decided_at,
                        "supersedes_signal_id": decision.supersedes_signal_id,
                        "run_id": decision.run_id,
                        "graph_id": decision.graph_id,
                        "task_id": decision.task_id,
                        "run_matter_id": self.matter_id,
                        "graph_matter_id": self.matter_id,
                        "task_matter_id": self.matter_id,
                        "current_graph_hash": (
                            decision.graph_hash if self.graph_hash_matches else "1" * 64
                        ),
                        "current_task_input_hash": decision.task_input_hash,
                    },
                )
            )
        return _Result()


class _ExceptionLifecycleConnection:
    def __init__(
        self, *, active_rows=(), duplicate_rows=(), obligation_rows=()
    ) -> None:
        self.active_rows = tuple(active_rows)
        self.duplicate_rows = tuple(duplicate_rows)
        self.obligation_rows = tuple(obligation_rows)
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.queries.append(normalized)
        if "FROM case_agent_ledger_exception_followup_heads head" in normalized:
            return _Result(rows=self.active_rows)
        if "FROM case_agent_ledger_exception_duplicate_heads head" in normalized:
            return _Result(rows=self.duplicate_rows)
        if "FROM case_agent_ledger_exception_followups followup" in normalized:
            return _Result(rows=self.obligation_rows)
        raise AssertionError(f"unexpected lifecycle projection query: {normalized}")


def _active_followup_row(
    *,
    evidence_page_ids: tuple[str, ...],
    followup_kind: str = "MORE_EVIDENCE",
    origin_decision: str = "REQUEST_MORE_EVIDENCE",
    reason_code: str = "EVIDENCE_GAP",
    reason_note: str | None = None,
    subject_hash: str = "b" * 64,
    followup_id: str | None = None,
) -> dict:
    return {
        "signal_scope": "ACTIVE_FOLLOWUP",
        "lifecycle_id": followup_id or _id(),
        "lifecycle_kind": followup_kind,
        "subject_hash": subject_hash,
        "head_event_id": _id(),
        "head_sequence": 1,
        "current_hash": "c" * 64,
        "signal_matter_version": 9,
        "head_integrity": True,
        "exception_decision_id": _id(),
        "origin_decision_hash": "a" * 64,
        "origin_decision": origin_decision,
        "reason_code": reason_code,
        "reason_note": reason_note,
        "origin_integrity": True,
        "candidate_count": len(evidence_page_ids),
        "group_integrity": True,
        "subject_integrity": True,
        "evidence_page_ids": list(sorted(evidence_page_ids)),
    }


def _current_duplicate_row(
    *,
    evidence_page_ids: tuple[str, ...],
    subject_hash: str = "d" * 64,
    disposition_id: str | None = None,
) -> dict:
    return {
        "signal_scope": "CURRENT_DUPLICATE",
        "lifecycle_id": disposition_id or _id(),
        "lifecycle_kind": "DUPLICATE",
        "subject_hash": subject_hash,
        "head_event_id": None,
        "head_sequence": None,
        "current_hash": "e" * 64,
        "signal_matter_version": 9,
        "head_integrity": True,
        "exception_decision_id": _id(),
        "origin_decision_hash": "e" * 64,
        "origin_decision": "REJECT_AS_DUPLICATE",
        "reason_code": "DUPLICATE_CONFIRMED",
        "reason_note": None,
        "origin_integrity": True,
        "candidate_count": len(evidence_page_ids),
        "group_integrity": True,
        "subject_integrity": True,
        "evidence_page_ids": list(sorted(evidence_page_ids)),
    }


def _active_reextraction_obligation_row(
    *,
    evidence_page_ids: tuple[str, ...],
    control_run_id: str,
    followup_id: str | None = None,
    subject_hash: str = "4" * 64,
) -> dict:
    return {
        "followup_id": followup_id or _id(),
        "control_run_id": control_run_id,
        "control_state": "HEALTHY",
        "subject_hash": subject_hash,
        "created_matter_version": 9,
        "head_event_id": _id(),
        "head_sequence": 1,
        "current_event_hash": "5" * 64,
        "head_integrity": True,
        "exception_decision_id": _id(),
        "origin_decision_hash": "6" * 64,
        "origin_integrity": True,
        "group_integrity": True,
        "subject_integrity": True,
        "evidence_page_ids": list(sorted(evidence_page_ids)),
    }


class PostgresCasePlanningProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.worker = Actor(_id(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.repository = PostgresCasePlanningProjectionRepository(
            "postgresql://not-used.invalid/lawcase_test"
        )

    def connection(self, **kwargs) -> _PlanningConnection:
        return _PlanningConnection(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            actor_id=self.worker.actor_id,
            **kwargs,
        )

    def expected_snapshot(self, connection: _PlanningConnection):
        return _read_case_ledger(
            connection, firm_id=self.firm_id, matter_id=self.matter_id
        ).snapshot

    def test_full_projection_is_one_repeatable_read_and_contains_current_ledgers(self) -> None:
        expected = self.expected_snapshot(self.connection())
        connection = self.connection()
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(connection),
        ) as connect:
            projection = self.repository.read_atomic_projection(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                actor=self.worker,
                expected_case_snapshot=expected,
            )
        connect.assert_called_once()
        self.assertEqual(projection.opening_case_snapshot, expected)
        self.assertEqual(projection.closing_case_snapshot, expected)
        self.assertEqual(projection.posture_state, ProjectionSectionState.AVAILABLE)
        self.assertEqual(projection.work_plan_state, ProjectionSectionState.AVAILABLE)
        self.assertEqual(projection.legal_state, ProjectionSectionState.AVAILABLE)
        self.assertEqual(projection.procedure_state, ProjectionSectionState.AVAILABLE)
        self.assertEqual(len(projection.lawyer_signals), 3)
        self.assertTrue(
            any(item.code == "LAWYER_REJECT_WRONG_FACT_ASSUMPTION" for item in projection.lawyer_signals)
        )
        object_types = {item.object_type for item in projection.objects}
        self.assertTrue(
            {
                PlanningProjectionObjectType.MATERIAL_OBJECT,
                PlanningProjectionObjectType.EVIDENCE_PAGE,
                PlanningProjectionObjectType.CASE_FACT,
                PlanningProjectionObjectType.CASE_CLAIM,
                PlanningProjectionObjectType.DISPUTE_ISSUE,
                PlanningProjectionObjectType.CASE_TRANSACTION,
                PlanningProjectionObjectType.POSTURE_PROFILE,
                PlanningProjectionObjectType.WORK_PLAN_ITEM,
                PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                PlanningProjectionObjectType.PROCEDURAL_EVENT,
            }.issubset(object_types)
        )
        evidence_page = next(
            item
            for item in projection.objects
            if item.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE
        )
        self.assertEqual(evidence_page.source_media_type, "application/pdf")
        self.assertTrue(connection.executed[0][0].startswith("SET TRANSACTION"))
        self.assertTrue(connection.executed[1][0].startswith("SELECT set_config"))
        self.assertEqual(
            sum(
                sql.startswith("SELECT matter_id, title, stage, version FROM matters")
                for sql, _ in connection.executed
            ),
            2,
        )

    def test_stale_active_work_plan_is_an_open_gap_not_a_planning_deadlock(self) -> None:
        expected = self.expected_snapshot(self.connection())
        connection = self.connection(plan_activated_version=8)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            projection = self.repository.read_atomic_projection(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                actor=self.worker,
                expected_case_snapshot=expected,
            )

        self.assertEqual(projection.work_plan_state, ProjectionSectionState.EMPTY)
        self.assertIsNone(projection.active_work_plan)
        self.assertFalse(
            any(
                item.object_type is PlanningProjectionObjectType.WORK_PLAN_ITEM
                for item in projection.objects
            )
        )

    def test_active_followup_sources_are_deterministically_sharded_not_truncated(self) -> None:
        pages = tuple(_id() for _ in range(101))
        connection = _ExceptionLifecycleConnection(
            active_rows=(_active_followup_row(evidence_page_ids=pages),)
        )
        signals = _read_ledger_exception_decision_signals(
            connection,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            authorized_refs=frozenset(
                f"evidence-page:{page_id}" for page_id in pages
            ),
        )
        self.assertEqual(len(signals), 2)
        self.assertEqual(tuple(len(item.source_ref_ids) for item in signals), (100, 1))
        self.assertEqual(
            set(ref for item in signals for ref in item.source_ref_ids),
            {f"evidence-page:{page_id}" for page_id in pages},
        )
        self.assertTrue(
            all(item.code == "LAWYER_REQUESTED_MORE_LEDGER_EVIDENCE" for item in signals)
        )
        self.assertEqual(len(connection.queries), 2)
        self.assertIn("head.current_state = 'ACTIVE'", connection.queries[0])
        self.assertIn(
            "FROM case_agent_ledger_exception_duplicate_heads head",
            connection.queries[1],
        )

    def test_active_reextraction_same_source_cohort_is_one_full_obligation(self) -> None:
        pages = tuple(_id() for _ in range(64))
        control_run_id = _id()
        followup_ids = (_id(), _id())
        connection = _ExceptionLifecycleConnection(
            obligation_rows=(
                _active_reextraction_obligation_row(
                    evidence_page_ids=pages,
                    control_run_id=control_run_id,
                    followup_id=followup_ids[0],
                    subject_hash="7" * 64,
                ),
                _active_reextraction_obligation_row(
                    evidence_page_ids=pages,
                    control_run_id=control_run_id,
                    followup_id=followup_ids[1],
                    subject_hash="8" * 64,
                ),
            )
        )

        obligations = _read_active_reextraction_planning_obligations(
            connection,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            authorized_refs=frozenset(
                f"evidence-page:{page_id}" for page_id in pages
            ),
        )

        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0].control_run_id, control_run_id)
        self.assertEqual(obligations[0].followup_ids, tuple(sorted(followup_ids)))
        self.assertEqual(len(obligations[0].source_ref_ids), 64)
        self.assertEqual(
            set(obligations[0].source_ref_ids),
            {f"evidence-page:{page_id}" for page_id in pages},
        )
        self.assertEqual(len(connection.queries), 1)
        self.assertIn("head.current_state = 'ACTIVE'", connection.queries[0])
        self.assertIn("followup.followup_kind = 'REEXTRACTION'", connection.queries[0])
        self.assertIn(
            "control_assignment.control_run_id", connection.queries[0]
        )
        self.assertIn(
            "control_head.current_state AS control_state", connection.queries[0]
        )

    def test_active_reextraction_multiple_control_runs_fail_closed(self) -> None:
        page_id = _id()
        rows = (
            _active_reextraction_obligation_row(
                evidence_page_ids=(page_id,),
                control_run_id=_id(),
                subject_hash="9" * 64,
            ),
            _active_reextraction_obligation_row(
                evidence_page_ids=(page_id,),
                control_run_id=_id(),
                subject_hash="a" * 64,
            ),
        )
        with self.assertRaisesRegex(
            CasePlanningProjectionBlocked, "multiple control runs"
        ):
            _read_active_reextraction_planning_obligations(
                _ExceptionLifecycleConnection(obligation_rows=rows),
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                authorized_refs=frozenset({f"evidence-page:{page_id}"}),
            )

    def test_active_reextraction_recovery_required_is_explicit(self) -> None:
        page_id = _id()
        row = _active_reextraction_obligation_row(
            evidence_page_ids=(page_id,),
            control_run_id=_id(),
        )
        row["control_state"] = "RECOVERY_REQUIRED"
        with self.assertRaisesRegex(
            CasePlanningProjectionBlocked,
            "REEXTRACTION_CONTROL_RECOVERY_REQUIRED",
        ):
            _read_active_reextraction_planning_obligations(
                _ExceptionLifecycleConnection(obligation_rows=(row,)),
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                authorized_refs=frozenset({f"evidence-page:{page_id}"}),
            )

    def test_current_duplicate_head_is_projected_without_historical_dispositions(self) -> None:
        page_id = _id()
        connection = _ExceptionLifecycleConnection(
            duplicate_rows=(
                _current_duplicate_row(evidence_page_ids=(page_id,)),
            )
        )
        signals = _read_ledger_exception_decision_signals(
            connection,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            authorized_refs=frozenset({f"evidence-page:{page_id}"}),
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].code, "LAWYER_REJECTED_EXTRACTION_DUPLICATE")
        self.assertIn(
            "disposition.duplicate_disposition_id = head.current_disposition_id",
            connection.queries[1],
        )
        self.assertNotIn(
            "FROM case_agent_ledger_exception_group_decisions decision",
            connection.queries[1],
        )

    def test_same_pages_with_different_subjects_remain_distinct_current_signals(self) -> None:
        page_id = _id()
        rows = (
            _active_followup_row(
                evidence_page_ids=(page_id,),
                reason_code="EVIDENCE_GAP",
                subject_hash="1" * 64,
            ),
            _active_followup_row(
                evidence_page_ids=(page_id,),
                reason_code="PARTY_DATE_AMOUNT_UNCLEAR",
                subject_hash="2" * 64,
            ),
        )
        signals = _read_ledger_exception_decision_signals(
            _ExceptionLifecycleConnection(active_rows=rows),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            authorized_refs=frozenset({f"evidence-page:{page_id}"}),
        )
        self.assertEqual(len(signals), 2)
        self.assertEqual(len({item.signal_id for item in signals}), 2)
        self.assertEqual(len({item.decision_hash for item in signals}), 2)

    def test_current_head_with_changed_exact_subject_binding_is_rejected(self) -> None:
        page_id = _id()
        row = _active_followup_row(evidence_page_ids=(page_id,))
        row["subject_integrity"] = False
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "exact group subject"):
            _read_ledger_exception_decision_signals(
                _ExceptionLifecycleConnection(active_rows=(row,)),
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                authorized_refs=frozenset({f"evidence-page:{page_id}"}),
            )

    def test_exact_subject_cannot_have_two_current_lifecycle_heads(self) -> None:
        page_id = _id()
        subject_hash = "3" * 64
        connection = _ExceptionLifecycleConnection(
            active_rows=(
                _active_followup_row(
                    evidence_page_ids=(page_id,), subject_hash=subject_hash
                ),
            ),
            duplicate_rows=(
                _current_duplicate_row(
                    evidence_page_ids=(page_id,), subject_hash=subject_hash
                ),
            ),
        )
        with self.assertRaisesRegex(
            CasePlanningProjectionBlocked, "more than one current lifecycle head"
        ):
            _read_ledger_exception_decision_signals(
                connection,
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                authorized_refs=frozenset({f"evidence-page:{page_id}"}),
            )

    def test_only_current_lifecycle_rows_consume_the_100_signal_limit(self) -> None:
        page_id = _id()
        authorized_refs = frozenset({f"evidence-page:{page_id}"})

        active_rows = tuple(
            _active_followup_row(
                evidence_page_ids=(page_id,),
                subject_hash=f"{index:064x}",
            )
            for index in range(1, 101)
        )
        signals = _read_ledger_exception_decision_signals(
            _ExceptionLifecycleConnection(active_rows=active_rows),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            authorized_refs=authorized_refs,
        )
        self.assertEqual(len(signals), 100)

        overflow = (
            *active_rows,
            _active_followup_row(
                evidence_page_ids=(page_id,), subject_hash=f"{101:064x}"
            ),
        )
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "more than 100"):
            _read_ledger_exception_decision_signals(
                _ExceptionLifecycleConnection(active_rows=overflow),
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                authorized_refs=authorized_refs,
            )

    def test_case_snapshot_hash_exactly_matches_existing_case_ledger_store(self) -> None:
        lead = Actor(_id(), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        existing_connection = self.connection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=_Context(existing_connection),
        ):
            existing = PostgresCaseLedgerStore("postgresql://not-used.invalid/test").get_case_snapshot(
                matter_id=self.matter_id, actor=lead
            )
        planning = self.expected_snapshot(self.connection())
        self.assertEqual(planning.matter_version, existing.version)
        self.assertEqual(planning.snapshot_hash, existing.snapshot_hash)

    def test_worker_scope_tenant_and_database_membership_fail_closed(self) -> None:
        expected = self.expected_snapshot(self.connection())
        mixed = Actor(
            self.worker.actor_id,
            self.firm_id,
            frozenset({Role.SYSTEM_WORKER, Role.LEAD_LAWYER}),
        )
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect"
        ) as connect:
            with self.assertRaisesRegex(PermissionError, "dedicated SYSTEM_WORKER"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=mixed,
                    expected_case_snapshot=expected,
                )
        connect.assert_not_called()

        foreign_worker = Actor(
            self.worker.actor_id,
            _id(),
            frozenset({Role.SYSTEM_WORKER}),
        )
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect"
        ) as connect:
            with self.assertRaisesRegex(PermissionError, "another firm"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=foreign_worker,
                    expected_case_snapshot=expected,
                )
        connect.assert_not_called()

        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect"
        ) as connect:
            with self.assertRaisesRegex(CasePlanningProjectionBlocked, "another matter"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=replace(expected, matter_id=_id()),
                )
        connect.assert_not_called()

        connection = self.connection(authorized=False)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(PermissionError, "active SYSTEM_WORKER"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )

    def test_opening_and_closing_snapshot_drift_are_rejected(self) -> None:
        expected = self.expected_snapshot(self.connection())
        opening_drift = self.connection(opening_version=10)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(opening_drift),
        ):
            with self.assertRaisesRegex(CasePlanningProjectionBlocked, "opening"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )

        closing_drift = self.connection(closing_version=10)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(closing_drift),
        ):
            with self.assertRaisesRegex(CasePlanningProjectionBlocked, "closing"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )

    def test_0035_and_current_graph_task_hashes_are_required(self) -> None:
        expected = self.expected_snapshot(self.connection())
        missing = self.connection(has_0035=False)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(missing),
        ):
            with self.assertRaisesRegex(CasePlanningProjectionBlocked, "0035"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )

        changed_graph = self.connection(graph_hash_matches=False)
        with patch(
            "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
            return_value=_Context(changed_graph),
        ):
            with self.assertRaisesRegex(CasePlanningProjectionBlocked, "run, graph or task"):
                self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )

    def test_0047_exception_review_requires_complete_0049_lifecycle_tables(self) -> None:
        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "complete 0049"):
            _read_capabilities(self.connection(has_0047=True))

        with self.assertRaisesRegex(CasePlanningProjectionBlocked, "partially installed"):
            _read_capabilities(
                self.connection(has_0047=True, partial_0049=True)
            )

        complete = self.connection(has_0047=True, has_0049=True)
        capabilities = _read_capabilities(complete)
        self.assertTrue(capabilities.ledger_exception_review)
        self.assertTrue(capabilities.ledger_exception_followups)
        capability_sql = complete.executed[0][0]
        self.assertIn(
            "case_agent_ledger_exception_evidence_source_bindings",
            capability_sql,
        )
        self.assertIn(
            "case_agent_ledger_exception_reextraction_task_bindings",
            capability_sql,
        )
        self.assertIn(
            "case_agent_ledger_exception_reextraction_task_binding_heads",
            capability_sql,
        )

    def test_current_lawyer_correction_changes_projection_hash(self) -> None:
        expected = self.expected_snapshot(self.connection())
        hashes = []
        for decision_hash in ("9" * 64, "8" * 64):
            connection = self.connection(agent_signal_hash=decision_hash)
            with patch(
                "case_kernel.case_agent_planning_snapshot_postgres.psycopg.connect",
                return_value=_Context(connection),
            ):
                projection = self.repository.read_atomic_projection(
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    actor=self.worker,
                    expected_case_snapshot=expected,
                )
            hashes.append(projection.projection_hash)
        self.assertNotEqual(hashes[0], hashes[1])

    def test_existing_migrations_are_the_repository_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        required = {
            "0001_core.sql": ("CREATE TABLE matters", "CREATE TABLE matter_actor_roles"),
            "0002_case_ledgers.sql": ("CREATE TABLE case_facts", "CREATE TABLE case_transactions"),
            "0003_evidence_manifest.sql": ("CREATE TABLE evidence_pages",),
            "0006_legal_source_rules.sql": ("CREATE TABLE official_legal_source_snapshots",),
            "0029_case_posture_profiles.sql": ("CREATE TABLE case_posture_profiles",),
            "0030_dynamic_case_work_plans.sql": ("CREATE TABLE case_work_plans",),
            "0035_case_agent_lawyer_decision_signals.sql": (
                "CREATE TABLE case_agent_lawyer_decision_signals",
                "FORCE ROW LEVEL SECURITY",
                "WHERE is_current",
            ),
            "0047_case_agent_ledger_exception_groups.sql": (
                "CREATE TABLE case_agent_ledger_exception_group_decisions",
                "validate_case_agent_ledger_exception_group_integrity",
            ),
            "0049_case_agent_ledger_exception_followups.sql": (
                "CREATE TABLE public.case_agent_ledger_exception_followups",
                "CREATE TABLE public.case_agent_ledger_exception_followup_heads",
                "CREATE TABLE public.case_agent_ledger_exception_evidence_source_bindings",
                "CREATE TABLE public.case_agent_ledger_exception_reextraction_task_bindings",
                "CREATE TABLE public.case_agent_ledger_exception_reextraction_task_binding_heads",
                "CREATE TABLE public.case_agent_ledger_exception_duplicate_heads",
                "WHERE current_state = 'ACTIVE'",
            ),
        }
        for filename, fragments in required.items():
            sql = (root / "migrations" / filename).read_text(encoding="utf-8")
            for fragment in fragments:
                self.assertIn(fragment, sql, filename)

        source = inspect.getsource(PostgresCasePlanningProjectionRepository)
        self.assertIn("_RepeatableReadPlanningTransaction", source)
        self.assertNotIn("PostgresCaseLedgerStore(", source)
        self.assertNotIn("PostgresCasePostureStore(", source)
        self.assertNotIn("PostgresCaseWorkPlanStore(", source)


if __name__ == "__main__":
    unittest.main()
