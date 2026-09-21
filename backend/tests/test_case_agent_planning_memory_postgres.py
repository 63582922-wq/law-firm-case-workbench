from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_memory import (
    AgentMemoryBlocked,
    MemoryAuthority,
    MemoryLayer,
    MemoryRecord,
    MemorySourceRef,
    MemoryStatus,
    SourceExposure,
    SourceLocationKind,
    VerifiedRetrievalPrincipal,
)
from case_kernel.case_agent_planner import (
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
)
from case_kernel.case_agent_planning_memory import (
    PlanningMemoryBlocked,
    PlanningMemoryItem,
    PlanningMemoryPurpose,
    PlanningMemorySearchRequest,
    PlanningMemorySourceRef,
    build_receipt,
)
from case_kernel.case_agent_planning_memory_postgres import (
    PostgresPlanningMemoryEnrichmentStore,
    _audit_matches_receipt,
    _items_match_current_records,
    _resolve_run_owner_in_connection,
)
from case_kernel.case_agent_supervisor import (
    AgentGoal,
    AgentRunState,
    AgentRunStatus,
    BudgetUsage,
    CaseSnapshotRef,
    RunResourceBudget,
)
from case_kernel.models import Actor, Role


def uid() -> str:
    return str(uuid4())


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Result:
    def __init__(self, *, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.executed.append((normalized, params))
        return _Result(row=self.row)


class _NoMemoryStore(PostgresPlanningMemoryEnrichmentStore):
    def __init__(self, owner: Actor, *, error: AgentMemoryBlocked) -> None:
        super().__init__("postgresql://not-used.invalid/planning-memory")
        self.owner = owner
        self.error = error

    def _resolve_run_owner(self, **_):
        return self.owner, datetime(2026, 8, 13, 8, tzinfo=timezone.utc)

    def resolve_retrieval_principal(self, *, actor, matter_id):
        return VerifiedRetrievalPrincipal(
            actor=actor,
            matter_id=matter_id,
            matter_access_grant_hash=digest("grant"),
            matter_version=7,
            permission_group_ids=(),
        )

    def _load_current_receipt(self, **_):
        return None

    def retrieve_full_text(self, **_):
        raise self.error


class PlanningMemoryPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 13, 8, tzinfo=timezone.utc)
        self.firm_id = uid()
        self.matter_id = uid()
        self.owner = Actor(
            uid(), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.worker = Actor(
            uid(), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.snapshot_ref = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=7,
            snapshot_hash=digest("snapshot"),
            schema_version="case-ledger-snapshot-v1",
        )
        self.goal = AgentGoal.build(
            goal_id=uid(),
            objective="读取当前案件并规划后续工作",
            success_criteria=("计划有权威来源",),
            constraints=("不得越权",),
            requested_by=self.owner.actor_id,
        )
        self.state = AgentRunState(
            run_id=uid(),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            event_version=1,
            goal=self.goal,
            snapshot=self.snapshot_ref,
            budget=RunResourceBudget(20, 40, 10, 10_000, 3_600, 10_000_000),
            status=AgentRunStatus.CREATED,
            graph=None,
            tasks=(),
            approvals=(),
            artifacts=(),
            budget_usage=BudgetUsage(),
        )
        self.input_ref = PlanningInputRef(
            ref_id=f"material-object:{uid()}",
            kind=PlanningInputKind.MATERIAL,
            object_version="v1",
            content_hash=digest("material"),
            status=PlanningInputStatus.AVAILABLE,
            allowed_skill_ids=("material_inventory",),
        )
        self.base = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot_ref,
            authorized_inputs=(self.input_ref,),
            signals=(),
        )
        self.request = PlanningMemorySearchRequest(
            purpose=PlanningMemoryPurpose.DYNAMIC_CASE_PLANNING,
            query_text="借款利息和还款记录",
            layers=(MemoryLayer.CASE_LONG_TERM, MemoryLayer.PUBLIC_LEGAL),
            allowed_skill_ids=("legal_rule_research_planning",),
            legal_period_start=date(2019, 1, 1),
            legal_period_end=date(2026, 8, 13),
            legal_period_source_ref_ids=(self.input_ref.ref_id,),
            legal_period_binding_hash=digest("legal-period-binding"),
        )

    def owner_row(self, *, roles=None, matter_version=7):
        return {
            "run_id": self.state.run_id,
            "goal_id": self.goal.goal_id,
            "created_by": self.owner.actor_id,
            "snapshot_hash": self.snapshot_ref.snapshot_hash,
            "snapshot_matter_version": self.snapshot_ref.matter_version,
            "snapshot_schema_version": self.snapshot_ref.schema_version,
            "goal_hash": self.goal.goal_hash,
            "requested_by": self.owner.actor_id,
            "matter_version": matter_version,
            "checked_at": self.now,
            "owner_roles": (
                [Role.LEAD_LAWYER.value] if roles is None else roles
            ),
        }

    def record_and_item(self):
        group_id = uid()
        source = MemorySourceRef(
            source_type="CASE_FACT",
            source_id=uid(),
            source_version="v1",
            content_hash=digest("source"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.CASE_PRIVATE,
        )
        record = MemoryRecord(
            record_id=uid(),
            record_version=2,
            layer=MemoryLayer.CASE_LONG_TERM,
            status=MemoryStatus.CONFIRMED,
            authority=MemoryAuthority.CONFIRMED_CASE_LEDGER,
            content_hash=digest("memory-content"),
            source_refs=(source,),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=None,
            run_id=None,
            task_id=None,
            permission_group_ids=(group_id,),
            case_type_codes=(),
            procedure_stages=(),
            issue_tags=(),
            effective_from=None,
            effective_to=None,
            known_from=self.now,
            known_to=None,
            publication_approval_hash=None,
            provenance_hash=digest("provenance"),
            updated_at=self.now,
        )
        item = PlanningMemoryItem.build(
            record_id=record.record_id,
            record_version=record.record_version,
            layer=record.layer,
            authority=record.authority,
            content_hash=record.content_hash,
            provenance_hash=record.provenance_hash,
            summary="已确认的同案事实摘要。",
            source_refs=(
                PlanningMemorySourceRef(
                    source_type=source.source_type,
                    source_id=source.source_id,
                    source_version=source.source_version,
                    content_hash=source.content_hash,
                    exposure=source.exposure,
                    page_number=None,
                ),
            ),
            externally_disclosable=False,
            external_authorization_hash=None,
        )
        return record, item, group_id

    def receipt(self):
        _record, item, group_id = self.record_and_item()
        return build_receipt(
            enrichment_id=uid(),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            run_id=self.state.run_id,
            goal_id=self.goal.goal_id,
            goal_hash=self.goal.goal_hash,
            owner_actor_id=self.owner.actor_id,
            purpose=self.request.purpose,
            case_snapshot_hash=self.snapshot_ref.snapshot_hash,
            case_snapshot_version=self.snapshot_ref.matter_version,
            case_snapshot_schema_version=self.snapshot_ref.schema_version,
            base_planning_hash=self.base.planning_hash,
            query_hash=self.request.query_hash,
            query_contract_hash=self.request.query_contract_hash,
            query_fingerprint=digest("query-fingerprint"),
            retrieval_id=uid(),
            retrieval_scope_hash=digest("scope"),
            owner_grant_hash=digest("grant"),
            owner_roles=(Role.LEAD_LAWYER,),
            permission_group_ids=(group_id,),
            items=(item,),
            retrieved_at=self.now,
            verified_at=self.now,
            final_verified_at=self.now,
        )

    def test_run_owner_is_resolved_from_database_not_worker_claims(self) -> None:
        connection = _Connection(self.owner_row())
        owner, checked_at = _resolve_run_owner_in_connection(
            connection, state=self.state, worker=self.worker
        )
        self.assertEqual(owner, self.owner)
        self.assertEqual(checked_at, self.now)
        sql, params = connection.executed[0]
        self.assertIn("run.created_by", sql)
        self.assertIn("worker_role.role = 'SYSTEM_WORKER'", sql)
        self.assertIn("owner.status = 'ACTIVE'", sql)
        self.assertEqual(params[0], self.worker.actor_id)

    def test_effective_material_goal_requires_exact_append_only_review(self):
        from types import SimpleNamespace
        from dataclasses import asdict
        from case_kernel.case_agent_supervisor import PlanningMaterialScopeReviewPayload
        refs = ("evidence-page:" + uid(),)
        goal = AgentGoal.build(goal_id=self.goal.goal_id, objective=self.goal.objective,
            success_criteria=self.goal.success_criteria, constraints=self.goal.constraints,
            requested_by=self.goal.requested_by, material_read_refs=refs)
        state = replace(self.state, goal=goal)
        review = PlanningMaterialScopeReviewPayload(state.snapshot, self.goal.goal_hash, digest("original"),
            digest("request"), digest("planning"), refs, state.budget.max_output_bytes,
            state.budget.max_output_bytes, goal.goal_hash, digest("derived"), digest("graph"), self.owner.actor_id)
        for records, allowed in (([asdict(review)], True), ([], False),
            ([asdict(replace(review, original_goal_hash=digest("other")))], False),
            ([asdict(replace(review, material_read_refs=("evidence-page:" + uid(),)))], False)):
            class Connection:
                def execute(inner, statement, params):
                    if "event.payload" in statement:
                        return SimpleNamespace(fetchall=lambda: [{"payload": p} for p in records])
                    return SimpleNamespace(fetchone=lambda: self.owner_row())
            if allowed:
                owner, _ = _resolve_run_owner_in_connection(Connection(), state=state, worker=self.worker)
                self.assertEqual(owner, self.owner)
            else:
                with self.assertRaises(PlanningMemoryBlocked):
                    _resolve_run_owner_in_connection(Connection(), state=state, worker=self.worker)

    def test_revoked_owner_or_case_version_drift_blocks_retrieval(self) -> None:
        with self.assertRaisesRegex(PermissionError, "human matter role"):
            _resolve_run_owner_in_connection(
                _Connection(self.owner_row(roles=[])),
                state=self.state,
                worker=self.worker,
            )
        with self.assertRaisesRegex(PlanningMemoryBlocked, "case version"):
            _resolve_run_owner_in_connection(
                _Connection(self.owner_row(matter_version=8)),
                state=self.state,
                worker=self.worker,
            )
        with self.assertRaisesRegex(PermissionError, "resolve"):
            _resolve_run_owner_in_connection(
                _Connection(None), state=self.state, worker=self.worker
            )

    def test_record_version_or_source_drift_discards_old_receipt(self) -> None:
        record, item, _group_id = self.record_and_item()
        self.assertTrue(_items_match_current_records((item,), (record,)))
        self.assertFalse(
            _items_match_current_records(
                (item,), (replace(record, record_version=3),)
            )
        )
        changed_source = replace(
            record,
            source_refs=(replace(record.source_refs[0], content_hash=digest("new")),),
        )
        self.assertFalse(_items_match_current_records((item,), (changed_source,)))

    def test_retrieval_audit_must_match_owner_run_query_scope_and_records(self) -> None:
        receipt = self.receipt()
        item = receipt.items[0]
        audit = {
            "actor_id": receipt.owner_actor_id,
            "run_id": receipt.run_id,
            "task_id": None,
            "query_hash": receipt.query_hash,
            "query_fingerprint": receipt.query_fingerprint,
            "grant_hash": receipt.owner_grant_hash,
            "matter_version": receipt.case_snapshot_version,
            "scope_hash": receipt.retrieval_scope_hash,
            "verified_at": receipt.verified_at,
            "returned_record_refs": [
                {"record_id": item.record_id, "content_hash": item.content_hash}
            ],
        }
        self.assertTrue(_audit_matches_receipt(audit, receipt))
        self.assertFalse(
            _audit_matches_receipt(
                {**audit, "actor_id": self.worker.actor_id}, receipt
            )
        )
        self.assertFalse(
            _audit_matches_receipt(
                {**audit, "scope_hash": digest("other-scope")}, receipt
            )
        )

    def test_no_authorized_memory_is_optional_but_other_acl_failures_block(self) -> None:
        no_records = _NoMemoryStore(
            self.owner,
            error=AgentMemoryBlocked(
                "no memory records are authorized for this query"
            ),
        )
        self.assertIsNone(
            no_records.current_enrichment(
                state=self.state,
                worker=self.worker,
                base_snapshot=self.base,
                request=self.request,
            )
        )
        blocked = _NoMemoryStore(
            self.owner,
            error=AgentMemoryBlocked("authority changed after search"),
        )
        with self.assertRaisesRegex(PlanningMemoryBlocked, "ACL-first"):
            blocked.current_enrichment(
                state=self.state,
                worker=self.worker,
                base_snapshot=self.base,
                request=self.request,
            )

    def test_stale_run_can_retrieve_current_memory_for_replanning(self) -> None:
        no_records = _NoMemoryStore(
            self.owner,
            error=AgentMemoryBlocked(
                "no memory records are authorized for this query"
            ),
        )
        stale_state = replace(
            self.state,
            status=AgentRunStatus.STALE,
            stale=True,
        )

        self.assertIsNone(
            no_records.current_enrichment(
                state=stale_state,
                worker=self.worker,
                base_snapshot=self.base,
                request=self.request,
            )
        )

    def test_cancelled_run_cannot_retrieve_planning_memory(self) -> None:
        no_records = _NoMemoryStore(
            self.owner,
            error=AgentMemoryBlocked(
                "no memory records are authorized for this query"
            ),
        )
        cancelled_state = replace(
            self.state,
            status=AgentRunStatus.CANCELLED,
            cancelled=True,
        )

        with self.assertRaisesRegex(PlanningMemoryBlocked, "cancelled"):
            no_records.current_enrichment(
                state=cancelled_state,
                worker=self.worker,
                base_snapshot=self.base,
                request=self.request,
            )


if __name__ == "__main__":
    unittest.main()
