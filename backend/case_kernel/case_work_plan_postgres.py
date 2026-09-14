"""PostgreSQL persistence for dynamic, lawyer-confirmed case work plans."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterator
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import IdempotencyConflict
from .request_context import current_request_id

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .case_work_plan import (
    AgentGoalRef,
    CaseWorkPlanCandidate,
    CaseWorkPlanContext,
    LawyerObjectiveRef,
    ResolvedWorkPlanReference,
    ValidatedCaseWorkPlan,
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
    validate_case_work_plan_candidate,
)
from .case_agent_planning_snapshot_postgres import (
    read_authoritative_projection_in_transaction,
)
from .case_agent_supervisor import (
    AgentDeliverableKind,
    AgentRiskLevel,
    CaseSnapshotRef,
)
from .case_agent_verifier import VerificationOutcome
from .case_agent_work_plan_promotion import (
    CompiledAgentWorkPlanPromotion,
    VerifiedGraphPromotionSource,
    VerifiedGraphTask,
    compile_verified_graph_work_plan_candidate,
    validate_verified_graph_promotion_source,
)
from .skill_registry import ApprovalGate
from .models import Actor, Role


@dataclass(frozen=True)
class PersistentCaseWorkPlanSnapshot:
    matter_id: str
    matter_version: int
    current_plan: dict[str, Any] | None
    plans: tuple[dict[str, Any], ...]
    items: tuple[dict[str, Any], ...]
    references: tuple[dict[str, Any], ...]
    prerequisites: tuple[dict[str, Any], ...]
    snapshot_hash: str
    item_references: tuple[dict[str, Any], ...] = ()
    item_reviews: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CaseWorkPlanItemReviewReceipt:
    review_id: str
    plan_id: str
    item_id: str
    decision: str
    matter_version: int


class PostgresCaseWorkPlanStore:
    """Register Agent candidates and activate only exact lead-lawyer decisions."""

    _REGISTER_ROLES = frozenset({Role.SYSTEM_WORKER})
    _ACTIVATE_ROLES = frozenset({Role.LEAD_LAWYER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def register_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        candidate: CaseWorkPlanCandidate,
        authoritative_context: CaseWorkPlanContext,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._REGISTER_ROLES
        )
        if authoritative_context.matter_id != matter_id or authoritative_context.matter_version != expected_version:
            raise CaseLedgerPersistenceBlocked(
                "server-owned work-plan context must bind the current matter and expected version"
            )
        request_payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "candidate": asdict(candidate),
            "authoritative_context": asdict(authoritative_context),
        }
        request_hash = _payload_hash(request_payload)
        command_name = "REGISTER_CASE_WORK_PLAN_CANDIDATE"
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=request_hash,
                allowed_roles=self._REGISTER_ROLES,
            )
            if prior is not None:
                return prior
            if isinstance(candidate.context.objective, AgentGoalRef):
                raise CaseLedgerPersistenceBlocked(
                    "Agent-goal candidates require the verified graph promotion command"
                )
            plan_id = str(uuid4())
            plan = validate_case_work_plan_candidate(
                candidate,
                plan_id=plan_id,
                authoritative_context=authoritative_context,
                resolve_reference=lambda reference: self._resolve_reference(
                    connection, actor=actor, matter_id=matter_id, reference=reference
                ),
            )
            plan_version = self._append_candidate(
                connection,
                actor=actor,
                plan=plan,
                expected_version=expected_version,
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                event_type="CASE_WORK_PLAN_CANDIDATE_REGISTERED",
                object_type="CASE_WORK_PLAN",
                object_id=plan.plan_id,
                audit_payload={
                    "plan_id": plan.plan_id,
                    "plan_version": plan_version,
                    "plan_hash": plan.plan_hash,
                    "context_hash": plan.context_hash,
                    "profile_id": plan.context.posture.profile_id,
                    "profile_hash": plan.context.posture.profile_hash,
                    "item_count": len(plan.items),
                    "required_court_document_count": len(plan.required_court_document_kinds),
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def promote_verified_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        run_id: str,
    ) -> CaseLedgerCommandReceipt:
        """Promote only a server-reloaded PASSED current graph to CANDIDATE.

        There is deliberately no candidate/hash/profile/firm argument.  This
        command is a Worker control-plane edge, not a browser or model API.
        """

        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._REGISTER_ROLES
        )
        _validate_uuid("run_id", run_id)
        command_name = "PROMOTE_VERIFIED_AGENT_GRAPH_TO_CASE_WORK_PLAN"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_version": expected_version,
                "run_id": run_id,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=request_hash,
                allowed_roles=self._REGISTER_ROLES,
            )
            if prior is not None:
                return prior
            source = self._read_verified_promotion_source(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                expected_version=expected_version,
            )
            self._assert_no_open_agent_ledger_review(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
            )
            existing = connection.execute(
                """
                SELECT plan_id
                FROM case_agent_work_plan_promotions
                WHERE verification_receipt_id = %s AND firm_id = %s AND matter_id = %s
                """,
                (
                    source.verification_receipt_id,
                    actor.firm_id,
                    matter_id,
                ),
            ).fetchone()
            if existing is not None:
                raise CaseLedgerPersistenceBlocked(
                    "this PASSED Agent graph already has a work-plan candidate"
                )
            projection = read_authoritative_projection_in_transaction(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                actor=actor,
                expected_case_snapshot=source.snapshot,
            )
            compiled = compile_verified_graph_work_plan_candidate(
                source=source,
                projection=projection,
            )
            binding_by_id = {
                item.binding_id: item for item in compiled.bindings
            }
            plan = validate_case_work_plan_candidate(
                compiled.candidate,
                plan_id=str(uuid4()),
                authoritative_context=compiled.candidate.context,
                resolve_reference=lambda reference: self._resolve_promotion_reference(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    reference=reference,
                    binding_by_id=binding_by_id,
                ),
            )
            plan_version = self._append_candidate(
                connection,
                actor=actor,
                plan=plan,
                expected_version=expected_version,
            )
            promotion_id = str(uuid4())
            self._insert_promotion(
                connection,
                actor=actor,
                plan=plan,
                promotion_id=promotion_id,
                compiled=compiled,
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                event_type="VERIFIED_AGENT_GRAPH_WORK_PLAN_CANDIDATE_REGISTERED",
                object_type="CASE_WORK_PLAN",
                object_id=plan.plan_id,
                audit_payload={
                    "plan_id": plan.plan_id,
                    "plan_version": plan_version,
                    "plan_hash": plan.plan_hash,
                    "run_id": source.run_id,
                    "graph_id": source.graph_id,
                    "graph_hash": source.graph_hash,
                    "verification_receipt_id": source.verification_receipt_id,
                    "verification_hash": source.verification_hash,
                    "snapshot_hash": source.snapshot.snapshot_hash,
                    "profile_id": plan.context.posture.profile_id,
                    "profile_hash": plan.context.posture.profile_hash,
                    "item_count": len(plan.items),
                    "requested_reviewable_document_count": len(
                        source.requested_deliverables
                    ),
                    "required_court_document_count": len(
                        plan.required_court_document_kinds
                    ),
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def review_item(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        plan_id: str,
        item_id: str,
        decision: str,
        reason_code: str,
        readiness_override: str | None,
        required_for_delivery_override: bool | None,
    ) -> CaseWorkPlanItemReviewReceipt:
        """Append one item review without changing the authoritative case version."""

        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._ACTIVATE_ROLES
        )
        _validate_uuid("plan_id", plan_id)
        _validate_uuid("item_id", item_id)
        if decision not in {"APPROVE", "REQUEST_CHANGE", "REJECT"}:
            raise CaseLedgerPersistenceBlocked("work-plan review decision is invalid")
        if reason_code not in {
            "VERIFIED_BY_COUNSEL",
            "NOT_APPLICABLE",
            "SUPERSEDED_BY_EVIDENCE",
            "REQUIRES_FURTHER_RESEARCH",
            "PROCEDURAL_POSTURE_CHANGED",
            "INCORRECT_SOURCE_BINDING",
        }:
            raise CaseLedgerPersistenceBlocked("work-plan review reason is invalid")
        if readiness_override not in {
            None,
            "ACTIONABLE",
            "NEEDS_RESEARCH",
            "NEEDS_INFORMATION",
        }:
            raise CaseLedgerPersistenceBlocked("work-plan review readiness is invalid")
        if required_for_delivery_override is not None and type(
            required_for_delivery_override
        ) is not bool:
            raise CaseLedgerPersistenceBlocked(
                "work-plan delivery review override is invalid"
            )
        has_override = (
            readiness_override is not None
            or required_for_delivery_override is not None
        )
        if (decision == "REQUEST_CHANGE") != has_override:
            raise CaseLedgerPersistenceBlocked(
                "only a change request may carry a structured override"
            )
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_version": expected_version,
                "plan_id": plan_id,
                "item_id": item_id,
                "decision": decision,
                "reason_code": reason_code,
                "readiness_override": readiness_override,
                "required_for_delivery_override": required_for_delivery_override,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="REVIEW_CASE_WORK_PLAN_ITEM",
                idempotency_key=idempotency_key,
            )
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._ACTIVATE_ROLES,
            )
            replay = connection.execute(
                """
                SELECT review_id, plan_id, item_id, decision, request_hash,
                       reviewed_matter_version
                FROM case_work_plan_item_reviews
                WHERE firm_id = %s AND matter_id = %s
                  AND reviewed_by = %s AND idempotency_key = %s
                """,
                (actor.firm_id, matter_id, actor.actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise IdempotencyConflict(
                        "idempotency key was reused with different input"
                    )
                return CaseWorkPlanItemReviewReceipt(
                    review_id=str(replay["review_id"]),
                    plan_id=str(replay["plan_id"]),
                    item_id=str(replay["item_id"]),
                    decision=str(replay["decision"]),
                    matter_version=int(replay["reviewed_matter_version"]),
                )
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                allowed_roles=self._ACTIVATE_ROLES,
            )
            plan_item = connection.execute(
                """
                SELECT plan.plan_id, plan.plan_hash, plan.status,
                       plan.planned_matter_version, plan.plan_version,
                       head.latest_plan_version, item.item_id
                FROM case_work_plans plan
                JOIN case_work_plan_heads head
                  ON head.matter_id = plan.matter_id AND head.firm_id = plan.firm_id
                JOIN case_work_plan_items item
                  ON item.plan_id = plan.plan_id
                 AND item.firm_id = plan.firm_id AND item.matter_id = plan.matter_id
                WHERE plan.plan_id = %s AND item.item_id = %s
                  AND plan.matter_id = %s AND plan.firm_id = %s
                FOR SHARE OF plan, item, head
                """,
                (plan_id, item_id, matter_id, actor.firm_id),
            ).fetchone()
            if (
                plan_item is None
                or str(plan_item["status"]) != "CANDIDATE"
                or int(plan_item["plan_version"])
                != int(plan_item["latest_plan_version"])
                or int(plan_item["planned_matter_version"]) + 1
                != expected_version
            ):
                raise CaseLedgerPersistenceBlocked(
                    "only an item in the exact current candidate may be reviewed"
                )
            prior_item_review = connection.execute(
                """
                SELECT 1 FROM case_work_plan_item_reviews
                WHERE plan_id = %s AND item_id = %s
                """,
                (plan_id, item_id),
            ).fetchone()
            if prior_item_review is not None:
                raise CaseLedgerPersistenceBlocked(
                    "this immutable plan item already has a lawyer review"
                )
            review_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_work_plan_item_reviews (
                    review_id, plan_id, item_id, firm_id, matter_id, plan_hash,
                    decision, reason_code, readiness_override,
                    required_for_delivery_override, reviewed_by,
                    idempotency_key, request_hash, reviewed_matter_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    review_id,
                    plan_id,
                    item_id,
                    actor.firm_id,
                    matter_id,
                    str(plan_item["plan_hash"]),
                    decision,
                    reason_code,
                    readiness_override,
                    required_for_delivery_override,
                    actor.actor_id,
                    idempotency_key,
                    request_hash,
                    expected_version,
                ),
            )
            audit_event_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, firm_id, matter_id, actor_id, event_type,
                    input_version, output_version, request_id, payload
                ) VALUES (
                    %s, %s, %s, %s, 'CASE_WORK_PLAN_ITEM_REVIEWED',
                    %s, %s, %s, %s
                )
                """,
                (
                    audit_event_id,
                    actor.firm_id,
                    matter_id,
                    actor.actor_id,
                    expected_version,
                    expected_version,
                    current_request_id() or str(uuid4()),
                    Jsonb(
                        {
                            "review_id": review_id,
                            "plan_id": plan_id,
                            "item_id": item_id,
                            "decision": decision,
                            "reason_code": reason_code,
                            "request_hash": request_hash,
                            "matter_version": expected_version,
                        }
                    ),
                ),
            )
            if decision in {"REQUEST_CHANGE", "REJECT"}:
                connection.execute(
                    """
                    INSERT INTO outbox_events (
                        firm_id, matter_id, aggregate_version, event_type, payload
                    ) VALUES (%s, %s, %s, 'CASE_WORK_PLAN_REPLANNING_REQUESTED', %s)
                    """,
                    (
                        actor.firm_id,
                        matter_id,
                        expected_version,
                        Jsonb(
                            {
                                "review_id": review_id,
                                "plan_id": plan_id,
                                "item_id": item_id,
                                "decision": decision,
                            }
                        ),
                    ),
                )
            return CaseWorkPlanItemReviewReceipt(
                review_id=review_id,
                plan_id=plan_id,
                item_id=item_id,
                decision=decision,
                matter_version=expected_version,
            )

    def activate_current_plan(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        """Activate the server-selected latest candidate in one transaction."""

        return self.activate_plan(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            plan_id=None,
            confirmation_hash=None,
        )

    def activate_plan(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        plan_id: str | None,
        confirmation_hash: str | None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._ACTIVATE_ROLES
        )
        server_select = plan_id is None and confirmation_hash is None
        if not server_select:
            if plan_id is None or confirmation_hash is None:
                raise CaseLedgerPersistenceBlocked(
                    "work-plan activation authority is incomplete"
                )
            _validate_uuid("plan_id", plan_id)
            _validate_sha256("confirmation_hash", confirmation_hash)
        command_name = (
            "ACTIVATE_CURRENT_CASE_WORK_PLAN"
            if server_select
            else "ACTIVATE_CASE_WORK_PLAN"
        )
        request_payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            **({} if server_select else {
                "plan_id": plan_id,
                "confirmation_hash": confirmation_hash,
            }),
        }
        request_hash = _payload_hash(request_payload)
        with self._transaction(actor.firm_id) as connection:
            # Exact replay is allowed after the command advanced the matter,
            # but it still requires the actor to retain an active database
            # role for this matter.
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._ACTIVATE_ROLES,
            )
            prior = self._begin(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=request_hash,
                allowed_roles=self._ACTIVATE_ROLES,
            )
            if prior is not None:
                return prior
            if server_select:
                plan = connection.execute(
                    """
                    SELECT plan.plan_id, plan.plan_version, plan.status,
                           plan.planned_matter_version, plan.profile_id,
                           plan.profile_version, plan.profile_hash,
                           plan.objective_approval_id, plan.agent_goal_id,
                           plan.objective_hash, plan.plan_hash
                    FROM case_work_plans plan
                    JOIN case_work_plan_heads head
                      ON head.matter_id = plan.matter_id
                     AND head.firm_id = plan.firm_id
                     AND head.latest_plan_version = plan.plan_version
                    WHERE plan.matter_id = %s AND plan.firm_id = %s
                    FOR UPDATE OF plan
                    """,
                    (matter_id, actor.firm_id),
                ).fetchone()
                if plan is None:
                    raise CaseLedgerPersistenceBlocked(
                        "there is no current work-plan candidate"
                    )
                plan_id = str(plan["plan_id"])
                confirmation_hash = str(plan["plan_hash"])
            else:
                plan = connection.execute(
                    """
                SELECT plan_id, plan_version, status, planned_matter_version,
                       profile_id, profile_version, profile_hash,
                       objective_approval_id, agent_goal_id, objective_hash, plan_hash
                FROM case_work_plans
                WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (plan_id, matter_id, actor.firm_id),
            ).fetchone()
            assert plan_id is not None and confirmation_hash is not None
            if plan is None:
                raise KeyError(plan_id)
            if plan["status"] != "CANDIDATE":
                raise CaseLedgerPersistenceBlocked("only a candidate work plan can be activated")
            if plan["plan_hash"] != confirmation_hash:
                raise CaseLedgerPersistenceBlocked(
                    "work-plan confirmation must bind the exact candidate hash"
                )
            if int(plan["planned_matter_version"]) + 1 != expected_version:
                raise CaseLedgerPersistenceBlocked(
                    "the matter changed after planning; regenerate the work plan"
                )
            adverse_review = connection.execute(
                """
                SELECT decision
                FROM case_work_plan_item_reviews
                WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
                  AND decision IN ('REQUEST_CHANGE', 'REJECT')
                LIMIT 1
                FOR SHARE
                """,
                (plan_id, matter_id, actor.firm_id),
            ).fetchone()
            if adverse_review is not None:
                raise CaseLedgerPersistenceBlocked(
                    "lawyer requested a change or rejection; promote a new verified plan"
                )
            self._assert_current_profile(
                connection,
                actor=actor,
                matter_id=matter_id,
                profile_id=str(plan["profile_id"]),
                profile_version=int(plan["profile_version"]),
                profile_hash=str(plan["profile_hash"]),
            )
            if plan["agent_goal_id"] is not None:
                if plan["objective_approval_id"] is not None:
                    raise CaseLedgerPersistenceBlocked(
                        "work plan has more than one objective authority"
                    )
                self._assert_current_agent_promotion(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    plan=plan,
                    expected_version=expected_version,
                )
            else:
                if plan["objective_approval_id"] is None:
                    raise CaseLedgerPersistenceBlocked(
                        "work plan has no objective authority"
                    )
                self._assert_current_objective(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    approval_id=str(plan["objective_approval_id"]),
                    objective_hash=str(plan["objective_hash"]),
                )
            self._assert_persisted_references_current(
                connection, actor=actor, matter_id=matter_id, plan_id=plan_id
            )
            head = connection.execute(
                """
                SELECT current_plan_id FROM case_work_plan_heads
                WHERE matter_id = %s AND firm_id = %s FOR UPDATE
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            prior_current = None if head is None else head["current_plan_id"]
            if prior_current is not None:
                connection.execute(
                    """
                    UPDATE case_work_plans
                    SET status = 'SUPERSEDED', stale_reason_code = 'NEW_PLAN_ACTIVATED',
                        updated_at = now()
                    WHERE plan_id = %s AND matter_id = %s AND firm_id = %s AND status = 'ACTIVE'
                    """,
                    (prior_current, matter_id, actor.firm_id),
                )
                connection.execute(
                    """
                    INSERT INTO case_work_plan_events (
                        plan_id, firm_id, matter_id, event_sequence, event_type,
                        effective_status, actor_id, cause_hash
                    ) SELECT %s, %s, %s, COALESCE(max(event_sequence), 0) + 1,
                             'PLAN_SUPERSEDED', 'SUPERSEDED', %s, %s
                      FROM case_work_plan_events WHERE plan_id = %s
                    """,
                    (
                        prior_current, actor.firm_id, matter_id, actor.actor_id,
                        confirmation_hash, prior_current,
                    ),
                )
            connection.execute(
                """
                UPDATE case_work_plans
                SET status = 'ACTIVE', confirmed_by = %s, confirmation_hash = %s,
                    confirmed_at = now(), activated_matter_version = %s, updated_at = now()
                WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (actor.actor_id, confirmation_hash, expected_version + 1, plan_id, matter_id, actor.firm_id),
            )
            connection.execute(
                """
                INSERT INTO case_work_plan_events (
                    plan_id, firm_id, matter_id, event_sequence, event_type,
                    effective_status, actor_id, cause_hash
                ) VALUES (%s, %s, %s, 2, 'PLAN_ACTIVATED', 'ACTIVE', %s, %s)
                """,
                (plan_id, actor.firm_id, matter_id, actor.actor_id, confirmation_hash),
            )
            connection.execute(
                """
                UPDATE case_work_plan_heads SET current_plan_id = %s, updated_at = now()
                WHERE matter_id = %s AND firm_id = %s
                """,
                (plan_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                event_type="CASE_WORK_PLAN_ACTIVATED",
                object_type="CASE_WORK_PLAN",
                object_id=plan_id,
                audit_payload={
                    "plan_id": plan_id,
                    "plan_hash": confirmation_hash,
                    "profile_id": str(plan["profile_id"]),
                    "profile_hash": str(plan["profile_hash"]),
                },
                stale_submission=True,
                stale_calculations=False,
            )

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentCaseWorkPlanSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES
            )
            matter = connection.execute(
                "SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            plans = _rows(
                connection.execute(
                    """
                    SELECT plan_id, plan_version, status, planned_matter_version,
                           profile_id, profile_version, profile_hash, objective_approval_id,
                           agent_goal_id,
                           objective_hash, claim_scope_hash, procedure_context_hash,
                           legal_context_hash, context_hash, plan_hash, agent_id, agent_version,
                           generated_at, required_court_document_kinds,
                           primary_court_document_kind, supersedes_plan_id,
                           confirmed_by, confirmed_at, activated_matter_version,
                           stale_reason_code, created_at, updated_at
                    FROM case_work_plans
                    WHERE matter_id = %s AND firm_id = %s
                    ORDER BY plan_version DESC
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            items = _rows(
                connection.execute(
                    """
                    SELECT item_id, plan_id, sequence, item_kind, readiness, title,
                           purpose, rationale, risk_if_omitted, confidence, review_gate,
                           delivery_target, deliverable_kind, required_for_delivery,
                           is_primary_document
                    FROM case_work_plan_items
                    WHERE matter_id = %s AND firm_id = %s
                    ORDER BY plan_id, sequence
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            references = _rows(
                connection.execute(
                    """
                    SELECT plan_id, source_type, source_id, source_version,
                           source_hash, reference_use
                    FROM case_work_plan_context_references
                    WHERE matter_id = %s AND firm_id = %s
                    ORDER BY plan_id, source_type, source_id
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            prerequisites = _rows(
                connection.execute(
                    """
                    SELECT plan_id, item_id, prerequisite_item_id
                    FROM case_work_plan_item_prerequisites
                    WHERE matter_id = %s AND firm_id = %s
                    ORDER BY plan_id, item_id, prerequisite_item_id
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            item_references = _rows(
                connection.execute(
                    """
                    SELECT reference.plan_id, reference.item_id,
                           reference.reference_role, reference.source_type,
                           reference.source_id, reference.source_version,
                           reference.reference_use,
                           COALESCE(binding.object_type, reference.source_type)
                               AS display_source_kind,
                           COALESCE(binding.object_id, reference.source_id)
                               AS display_source_id,
                           COALESCE(binding.object_version, reference.source_version)
                               AS display_locator
                    FROM case_work_plan_item_references reference
                    LEFT JOIN case_agent_work_plan_input_bindings binding
                      ON reference.source_type = 'AGENT_TASK_INPUT'
                     AND binding.plan_id = reference.plan_id
                     AND binding.binding_id = reference.source_id
                     AND binding.firm_id = reference.firm_id
                     AND binding.matter_id = reference.matter_id
                    WHERE reference.matter_id = %s AND reference.firm_id = %s
                    ORDER BY reference.plan_id, reference.item_id,
                             reference.reference_role, reference.source_id
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            item_reviews = _rows(
                connection.execute(
                    """
                    SELECT review_id, plan_id, item_id, decision, reason_code,
                           readiness_override, required_for_delivery_override,
                           reviewed_by, reviewed_at
                    FROM case_work_plan_item_reviews
                    WHERE matter_id = %s AND firm_id = %s
                    ORDER BY plan_id, item_id
                    """,
                    (matter_id, actor.firm_id),
                ).fetchall()
            )
            head = connection.execute(
                """
                SELECT current_plan_id FROM case_work_plan_heads
                WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
        current_id = None if head is None else str(head["current_plan_id"] or "") or None
        current = next((item for item in plans if str(item["plan_id"]) == current_id), None)
        if current is not None:
            current["is_stale_against_matter"] = (
                int(current["activated_matter_version"] or current["planned_matter_version"])
                != int(matter["version"])
            )
        payload = {
            "matter_id": matter_id,
            "matter_version": int(matter["version"]),
            "current_plan": current,
            "plans": plans,
            "items": items,
            "references": references,
            "prerequisites": prerequisites,
            "item_references": item_references,
            "item_reviews": item_reviews,
        }
        return PersistentCaseWorkPlanSnapshot(
            matter_id=matter_id,
            matter_version=int(matter["version"]),
            current_plan=current,
            plans=tuple(plans),
            items=tuple(items),
            references=tuple(references),
            prerequisites=tuple(prerequisites),
            snapshot_hash=_payload_hash(payload),
            item_references=tuple(item_references),
            item_reviews=tuple(item_reviews),
        )

    def _append_candidate(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        plan: ValidatedCaseWorkPlan,
        expected_version: int,
    ) -> int:
        """Append one validated candidate without making it the active plan."""

        if plan.status != "CANDIDATE" or plan.context.matter_version != expected_version:
            raise CaseLedgerPersistenceBlocked(
                "only a candidate bound to the locked matter version may be appended"
            )
        head = connection.execute(
            """
            SELECT latest_plan_version, current_plan_id
            FROM case_work_plan_heads
            WHERE matter_id = %s AND firm_id = %s
            FOR UPDATE
            """,
            (plan.context.matter_id, actor.firm_id),
        ).fetchone()
        plan_version = 1 if head is None else int(head["latest_plan_version"]) + 1
        supersedes_plan_id = None if head is None else head["current_plan_id"]

        objective_approval_id: str | None
        agent_goal_id: str | None
        objective_hash: str
        if isinstance(plan.context.objective, LawyerObjectiveRef):
            objective_approval_id = plan.context.objective.approval_id
            agent_goal_id = None
            objective_hash = plan.context.objective.objective_hash
        elif isinstance(plan.context.objective, AgentGoalRef):
            objective_approval_id = None
            agent_goal_id = plan.context.objective.goal_id
            objective_hash = plan.context.objective.goal_hash
        else:  # pragma: no cover - the closed domain union is validated earlier
            raise CaseLedgerPersistenceBlocked("work plan objective authority is unsupported")

        connection.execute(
            """
            INSERT INTO case_work_plans (
                plan_id, firm_id, matter_id, plan_version, status,
                planned_matter_version, profile_id, profile_version, profile_hash,
                objective_approval_id, agent_goal_id, objective_hash,
                claim_scope_hash, procedure_context_hash, legal_context_hash,
                context_hash, candidate_input_hash, plan_hash, agent_id,
                agent_version, generated_at, required_court_document_kinds,
                primary_court_document_kind, supersedes_plan_id, registered_by
            ) VALUES (
                %s, %s, %s, %s, 'CANDIDATE',
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                plan.plan_id,
                actor.firm_id,
                plan.context.matter_id,
                plan_version,
                expected_version,
                plan.context.posture.profile_id,
                plan.context.posture.profile_version,
                plan.context.posture.profile_hash,
                objective_approval_id,
                agent_goal_id,
                objective_hash,
                plan.context.claim_scope_hash,
                plan.context.procedure_context_hash,
                plan.context.legal_context_hash,
                plan.context_hash,
                plan.candidate_input_hash,
                plan.plan_hash,
                plan.agent_id,
                plan.agent_version,
                plan.generated_at,
                Jsonb(list(plan.required_court_document_kinds)),
                plan.primary_court_document_kind,
                supersedes_plan_id,
                actor.actor_id,
            ),
        )
        self._insert_plan_details(connection, actor=actor, plan=plan)
        connection.execute(
            """
            INSERT INTO case_work_plan_events (
                plan_id, firm_id, matter_id, event_sequence, event_type,
                effective_status, actor_id, cause_hash
            ) VALUES (%s, %s, %s, 1, 'CANDIDATE_REGISTERED', 'CANDIDATE', %s, %s)
            """,
            (
                plan.plan_id,
                actor.firm_id,
                plan.context.matter_id,
                actor.actor_id,
                plan.plan_hash,
            ),
        )
        if head is None:
            connection.execute(
                """
                INSERT INTO case_work_plan_heads (
                    matter_id, firm_id, latest_plan_version, current_plan_id
                ) VALUES (%s, %s, %s, NULL)
                """,
                (plan.context.matter_id, actor.firm_id, plan_version),
            )
        else:
            connection.execute(
                """
                UPDATE case_work_plan_heads
                SET latest_plan_version = %s, updated_at = now()
                WHERE matter_id = %s AND firm_id = %s
                """,
                (plan_version, plan.context.matter_id, actor.firm_id),
            )
        return plan_version

    def _read_verified_promotion_source(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        expected_version: int,
    ) -> VerifiedGraphPromotionSource:
        row = connection.execute(
            """
            SELECT agent_run.status AS run_status,
                   agent_run.is_stale AS run_is_stale,
                   agent_run.is_cancelled AS run_is_cancelled,
                   agent_run.snapshot_matter_version AS run_snapshot_matter_version,
                   agent_run.snapshot_schema_version AS run_snapshot_schema_version,
                   agent_run.snapshot_hash AS run_snapshot_hash,
                   agent_run.current_graph_id, agent_run.current_graph_version,
                   agent_run.current_graph_hash,
                   agent_run.verification_hash AS run_verification_hash,
                   graph.graph_id, graph.graph_version, graph.graph_hash,
                   graph.goal_hash AS graph_goal_hash,
                   graph.snapshot_matter_version AS graph_snapshot_matter_version,
                   graph.snapshot_schema_version AS graph_snapshot_schema_version,
                   graph.snapshot_hash AS graph_snapshot_hash,
                   goal.goal_id, goal.goal_hash, goal.requested_deliverables,
                   receipt.verification_receipt_id, receipt.outcome,
                   receipt.graph_hash AS verification_graph_hash,
                   receipt.snapshot_hash AS verification_snapshot_hash,
                   receipt.verification_hash, receipt.verifier_actor_id,
                   receipt.execution_actor_id, receipt.verified_at
            FROM case_agent_runs agent_run
            JOIN case_agent_task_graphs graph
              ON graph.graph_id = agent_run.current_graph_id
             AND graph.run_id = agent_run.run_id
             AND graph.firm_id = agent_run.firm_id
             AND graph.matter_id = agent_run.matter_id
            JOIN case_agent_goals goal
              ON goal.goal_id = agent_run.goal_id
             AND goal.firm_id = agent_run.firm_id
             AND goal.matter_id = agent_run.matter_id
            JOIN case_agent_verification_receipts receipt
              ON receipt.run_id = agent_run.run_id
             AND receipt.firm_id = agent_run.firm_id
             AND receipt.matter_id = agent_run.matter_id
             AND receipt.verification_hash = agent_run.verification_hash
            WHERE agent_run.run_id = %s
              AND agent_run.matter_id = %s AND agent_run.firm_id = %s
            -- Goals are append-only and the Worker intentionally has only
            -- SELECT after the active-plan boundary revokes goal mutation.
            -- PostgreSQL row locks require UPDATE privilege, so locking the
            -- immutable goal here would make every verified promotion fail.
            -- The mutable run head and exact graph/receipt remain locked;
            -- goal integrity is covered by immutable rows, hashes and the
            -- deferred promotion constraint.
            FOR SHARE OF agent_run, graph, receipt
            """,
            (run_id, matter_id, actor.firm_id),
        ).fetchone()
        if row is None:
            raise CaseLedgerPersistenceBlocked(
                "Agent run has no current independently verified graph"
            )
        if (
            int(row["run_snapshot_matter_version"]) != expected_version
            or int(row["graph_snapshot_matter_version"]) != expected_version
            or str(row["run_snapshot_schema_version"])
            != str(row["graph_snapshot_schema_version"])
            or str(row["run_snapshot_hash"]) != str(row["graph_snapshot_hash"])
            or str(row["current_graph_id"]) != str(row["graph_id"])
            or int(row["current_graph_version"]) != int(row["graph_version"])
            or str(row["current_graph_hash"]) != str(row["graph_hash"])
            or str(row["graph_goal_hash"]) != str(row["goal_hash"])
        ):
            raise CaseLedgerPersistenceBlocked(
                "Agent run, current graph, goal or case snapshot no longer agree"
            )

        task_rows = connection.execute(
            """
            SELECT task.task_id, task.sequence, task.title, task.purpose,
                   task.rationale, task.input_refs, task.skill_id,
                   task.risk_level, task.approval_gate
            FROM case_agent_tasks task
            WHERE task.graph_id = %s AND task.run_id = %s
              AND task.matter_id = %s AND task.firm_id = %s
            ORDER BY task.sequence ASC, task.task_id ASC
            FOR SHARE
            """,
            (row["graph_id"], run_id, matter_id, actor.firm_id),
        ).fetchall()
        dependency_rows = connection.execute(
            """
            SELECT task_id, dependency_task_id
            FROM case_agent_task_dependencies
            WHERE graph_id = %s AND run_id = %s
              AND matter_id = %s AND firm_id = %s
            ORDER BY task_id ASC, dependency_task_id ASC
            FOR SHARE
            """,
            (row["graph_id"], run_id, matter_id, actor.firm_id),
        ).fetchall()
        dependencies: dict[str, list[str]] = {}
        for dependency in dependency_rows:
            dependencies.setdefault(str(dependency["task_id"]), []).append(
                str(dependency["dependency_task_id"])
            )
        tasks: list[VerifiedGraphTask] = []
        for task in task_rows:
            input_refs = task["input_refs"]
            if not isinstance(input_refs, list) or any(
                not isinstance(item, str) for item in input_refs
            ):
                raise CaseLedgerPersistenceBlocked(
                    "persisted Agent task input references are invalid"
                )
            try:
                risk_level = AgentRiskLevel(str(task["risk_level"]))
                approval_gate = ApprovalGate(str(task["approval_gate"]))
            except ValueError as error:
                raise CaseLedgerPersistenceBlocked(
                    "persisted Agent task policy is unsupported"
                ) from error
            tasks.append(
                VerifiedGraphTask(
                    task_id=str(task["task_id"]),
                    sequence=int(task["sequence"]),
                    title=str(task["title"]),
                    purpose=str(task["purpose"]),
                    rationale=str(task["rationale"]),
                    dependency_ids=tuple(
                        dependencies.get(str(task["task_id"]), ())
                    ),
                    input_refs=tuple(input_refs),
                    skill_id=str(task["skill_id"]),
                    risk_level=risk_level,
                    approval_gate=approval_gate,
                )
            )
        try:
            outcome = VerificationOutcome(str(row["outcome"]))
        except ValueError as error:
            raise CaseLedgerPersistenceBlocked(
                "persisted Agent verification outcome is unsupported"
            ) from error
        raw_deliverables = row["requested_deliverables"]
        if not isinstance(raw_deliverables, list):
            raise CaseLedgerPersistenceBlocked(
                "persisted Agent requested deliverables are invalid"
            )
        try:
            requested_deliverables = tuple(
                AgentDeliverableKind(item) for item in raw_deliverables
            )
        except (TypeError, ValueError) as error:
            raise CaseLedgerPersistenceBlocked(
                "persisted Agent requested deliverables are unsupported"
            ) from error
        source = VerifiedGraphPromotionSource(
            run_id=run_id,
            run_status=str(row["run_status"]),
            run_is_stale=bool(row["run_is_stale"]),
            run_is_cancelled=bool(row["run_is_cancelled"]),
            graph_id=str(row["graph_id"]),
            graph_version=int(row["graph_version"]),
            graph_hash=str(row["graph_hash"]),
            snapshot=CaseSnapshotRef(
                matter_id=matter_id,
                matter_version=int(row["run_snapshot_matter_version"]),
                snapshot_hash=str(row["run_snapshot_hash"]),
                schema_version=str(row["run_snapshot_schema_version"]),
            ),
            goal_id=str(row["goal_id"]),
            goal_hash=str(row["goal_hash"]),
            requested_deliverables=requested_deliverables,
            verification_receipt_id=str(row["verification_receipt_id"]),
            verification_outcome=outcome,
            verification_graph_hash=str(row["verification_graph_hash"]),
            verification_snapshot_hash=str(row["verification_snapshot_hash"]),
            verification_hash=str(row["verification_hash"]),
            run_verification_hash=str(row["run_verification_hash"]),
            verifier_actor_id=str(row["verifier_actor_id"]),
            execution_actor_id=str(row["execution_actor_id"]),
            verified_at=row["verified_at"],
            tasks=tuple(tasks),
        )
        try:
            validate_verified_graph_promotion_source(source)
        except ValueError as error:
            raise CaseLedgerPersistenceBlocked(str(error)) from error
        return source

    def _resolve_promotion_reference(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        reference: WorkPlanReference,
        binding_by_id: dict[str, Any],
    ) -> ResolvedWorkPlanReference | None:
        if reference.source_type is not WorkPlanSourceType.AGENT_TASK_INPUT:
            return self._resolve_reference(
                connection, actor=actor, matter_id=matter_id, reference=reference
            )
        binding = binding_by_id.get(reference.source_id)
        if binding is None or binding.as_reference() != reference:
            return None
        return ResolvedWorkPlanReference(
            matter_id=matter_id,
            source_type=reference.source_type,
            source_id=reference.source_id,
            source_version=reference.source_version,
            source_hash=reference.source_hash,
            is_current=True,
            is_confirmed=True,
            is_effective=True,
        )

    def _insert_promotion(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        plan: ValidatedCaseWorkPlan,
        promotion_id: str,
        compiled: CompiledAgentWorkPlanPromotion,
    ) -> None:
        source = compiled.source
        connection.execute(
            """
            INSERT INTO case_agent_work_plan_promotions (
                promotion_id, plan_id, firm_id, matter_id, run_id, graph_id,
                graph_version, graph_hash, snapshot_matter_version,
                snapshot_schema_version, snapshot_hash, goal_id, goal_hash,
                posture_profile_id, posture_profile_version, posture_profile_hash,
                verification_receipt_id, verification_hash, verifier_actor_id,
                execution_actor_id, task_count, promoted_by
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                promotion_id,
                plan.plan_id,
                actor.firm_id,
                plan.context.matter_id,
                source.run_id,
                source.graph_id,
                source.graph_version,
                source.graph_hash,
                source.snapshot.matter_version,
                source.snapshot.schema_version,
                source.snapshot.snapshot_hash,
                source.goal_id,
                source.goal_hash,
                plan.context.posture.profile_id,
                plan.context.posture.profile_version,
                plan.context.posture.profile_hash,
                source.verification_receipt_id,
                source.verification_hash,
                source.verifier_actor_id,
                source.execution_actor_id,
                len(source.tasks),
                actor.actor_id,
            ),
        )
        for binding in compiled.bindings:
            connection.execute(
                """
                INSERT INTO case_agent_work_plan_input_bindings (
                    binding_id, plan_id, promotion_id, firm_id, matter_id,
                    input_ref, object_type, object_id, object_version,
                    content_hash, source_status, source_type, reference_use,
                    binding_hash
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, 'AGENT_TASK_INPUT', %s,
                    %s
                )
                """,
                (
                    binding.binding_id,
                    plan.plan_id,
                    promotion_id,
                    actor.firm_id,
                    plan.context.matter_id,
                    binding.input_ref,
                    binding.object_type.value,
                    binding.object_id,
                    binding.object_version,
                    binding.content_hash,
                    binding.source_status,
                    binding.reference_use.value,
                    binding.binding_hash,
                ),
            )

    @staticmethod
    def _assert_no_open_agent_ledger_review(
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT case_agent_ledger_extraction_run_review_resolved(
                %s, %s, %s
            ) AS review_resolved
            """,
            (run_id, actor.firm_id, matter_id),
        ).fetchone()
        if row is None or not bool(row["review_resolved"]):
            raise CaseLedgerPersistenceBlocked(
                "Agent work plan is blocked by open ledger review"
            )

    def _assert_current_agent_promotion(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        plan: dict[str, Any],
        expected_version: int,
    ) -> None:
        promotion = connection.execute(
            """
            SELECT promotion_id, run_id, graph_id, graph_version, graph_hash,
                   snapshot_matter_version, snapshot_schema_version, snapshot_hash,
                   goal_id, goal_hash, posture_profile_id, posture_profile_version,
                   posture_profile_hash, verification_receipt_id, verification_hash,
                   verifier_actor_id, execution_actor_id, task_count
            FROM case_agent_work_plan_promotions
            WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
            FOR SHARE
            """,
            (plan["plan_id"], matter_id, actor.firm_id),
        ).fetchone()
        if promotion is None:
            raise CaseLedgerPersistenceBlocked(
                "Agent-goal work plan has no verified graph promotion"
            )
        self._assert_no_open_agent_ledger_review(
            connection,
            actor=actor,
            matter_id=matter_id,
            run_id=str(promotion["run_id"]),
        )
        source = self._read_verified_promotion_source(
            connection,
            actor=actor,
            matter_id=matter_id,
            run_id=str(promotion["run_id"]),
            expected_version=int(plan["planned_matter_version"]),
        )
        expected_values = {
            "graph_id": source.graph_id,
            "graph_version": source.graph_version,
            "graph_hash": source.graph_hash,
            "snapshot_matter_version": source.snapshot.matter_version,
            "snapshot_schema_version": source.snapshot.schema_version,
            "snapshot_hash": source.snapshot.snapshot_hash,
            "goal_id": source.goal_id,
            "goal_hash": source.goal_hash,
            "verification_receipt_id": source.verification_receipt_id,
            "verification_hash": source.verification_hash,
            "verifier_actor_id": source.verifier_actor_id,
            "execution_actor_id": source.execution_actor_id,
            "task_count": len(source.tasks),
        }
        for key, expected in expected_values.items():
            actual = promotion[key]
            if isinstance(expected, int):
                matches = int(actual) == expected
            else:
                matches = str(actual) == expected
            if not matches:
                raise CaseLedgerPersistenceBlocked(
                    "Agent work-plan promotion no longer matches the current PASSED graph"
                )
        if (
            str(plan["agent_goal_id"]) != source.goal_id
            or str(plan["objective_hash"]) != source.goal_hash
            or str(promotion["posture_profile_id"]) != str(plan["profile_id"])
            or int(promotion["posture_profile_version"])
            != int(plan["profile_version"])
            or str(promotion["posture_profile_hash"]) != str(plan["profile_hash"])
            or source.snapshot.matter_version + 1 != expected_version
        ):
            raise CaseLedgerPersistenceBlocked(
                "Agent work-plan goal, posture or case version is no longer current"
            )

        binding_rows = connection.execute(
            """
            SELECT binding_id, input_ref, object_type, object_id, object_version,
                   content_hash, source_status, reference_use, binding_hash
            FROM case_agent_work_plan_input_bindings
            WHERE plan_id = %s AND promotion_id = %s
              AND matter_id = %s AND firm_id = %s
            ORDER BY input_ref ASC
            FOR SHARE
            """,
            (
                plan["plan_id"],
                promotion["promotion_id"],
                matter_id,
                actor.firm_id,
            ),
        ).fetchall()
        graph_input_refs = {
            input_ref for task in source.tasks for input_ref in task.input_refs
        }
        actual_input_refs = {str(row["input_ref"]) for row in binding_rows}
        if not graph_input_refs.issubset(actual_input_refs):
            raise CaseLedgerPersistenceBlocked(
                "Agent work-plan input bindings omit a verified graph source"
            )
        extension_rows = tuple(
            row
            for row in binding_rows
            if str(row["input_ref"]) not in graph_input_refs
        )
        def authorized_deliverable_extension(row: Any) -> bool:
            object_type = str(row["object_type"])
            source_status = str(row["source_status"])
            return (
                object_type == "CASE_FACT"
                and source_status == "CONFIRMED"
                and (
                    AgentDeliverableKind.CASE_REVIEW_MEMO
                    in source.requested_deliverables
                    or AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST
                    in source.requested_deliverables
                    or AgentDeliverableKind.DEFENCE_STATEMENT
                    in source.requested_deliverables
                )
            ) or (
                object_type == "CASE_TRANSACTION"
                and source_status == "CONFIRMED"
                and AgentDeliverableKind.PAYMENT_LEDGER
                in source.requested_deliverables
            ) or (
                object_type == "EVIDENCE_PAGE"
                and source_status == "CONFIRMED"
                and AgentDeliverableKind.EVIDENCE_CATALOGUE
                in source.requested_deliverables
            ) or (
                object_type == "CASE_CLAIM"
                and source_status == "CONFIRMED"
                and AgentDeliverableKind.DEFENCE_STATEMENT
                in source.requested_deliverables
            ) or (
                object_type in {"VERIFIED_LEGAL_SOURCE", "APPROVED_LEGAL_RULE"}
                and source_status == "LOCKED"
                and AgentDeliverableKind.DEFENCE_STATEMENT
                in source.requested_deliverables
            )

        if any(not authorized_deliverable_extension(row) for row in extension_rows):
            raise CaseLedgerPersistenceBlocked(
                "Agent work-plan contains a source not authorized by its structured deliverable intent"
            )
        binding_references: set[tuple[str, str, str, str]] = set()
        for row in binding_rows:
            input_ref = str(row["input_ref"])
            expected_binding_id = str(uuid5(UUID(source.graph_id), input_ref))
            payload = {
                "schema_version": "case-agent-work-plan-input-binding-v1",
                "run_id": source.run_id,
                "graph_id": source.graph_id,
                "graph_version": source.graph_version,
                "graph_hash": source.graph_hash,
                "snapshot_hash": source.snapshot.snapshot_hash,
                "verification_hash": source.verification_hash,
                "binding_id": expected_binding_id,
                "input_ref": input_ref,
                "object_type": str(row["object_type"]),
                "object_id": str(row["object_id"]),
                "object_version": str(row["object_version"]),
                "content_hash": str(row["content_hash"]),
                "source_status": str(row["source_status"]),
                "reference_use": str(row["reference_use"]),
            }
            binding_hash = _payload_hash(payload)
            if (
                str(row["binding_id"]) != expected_binding_id
                or str(row["binding_hash"]) != binding_hash
            ):
                raise CaseLedgerPersistenceBlocked(
                    "Agent work-plan input binding provenance is invalid"
                )
            binding_references.add(
                (
                    expected_binding_id,
                    str(row["object_version"]),
                    binding_hash,
                    str(row["reference_use"]),
                )
            )
        context_rows = connection.execute(
            """
            SELECT source_id, source_version, source_hash, reference_use
            FROM case_work_plan_context_references
            WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
              AND source_type = 'AGENT_TASK_INPUT'
            FOR SHARE
            """,
            (plan["plan_id"], matter_id, actor.firm_id),
        ).fetchall()
        context_references = {
            (
                str(row["source_id"]),
                str(row["source_version"]),
                str(row["source_hash"]),
                str(row["reference_use"]),
            )
            for row in context_rows
        }
        if context_references != binding_references:
            raise CaseLedgerPersistenceBlocked(
                "Agent work-plan context differs from its verified input bindings"
            )
        self._assert_requested_deliverable_items(
            connection,
            actor=actor,
            matter_id=matter_id,
            plan_id=str(plan["plan_id"]),
            source=source,
            binding_rows=binding_rows,
        )

    @staticmethod
    def _assert_requested_deliverable_items(
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        plan_id: str,
        source: VerifiedGraphPromotionSource,
        binding_rows: Any,
    ) -> None:
        rows = connection.execute(
            """
            SELECT item_id, item_kind, readiness, delivery_target, deliverable_kind,
                   required_for_delivery
            FROM case_work_plan_items
            WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
              AND deliverable_kind IS NOT NULL
            ORDER BY deliverable_kind ASC, item_id ASC
            FOR SHARE
            """,
            (plan_id, matter_id, actor.firm_id),
        ).fetchall()
        expected = tuple(item.value for item in source.requested_deliverables)
        actual = tuple(str(row["deliverable_kind"]) for row in rows)
        if actual != expected:
            raise CaseLedgerPersistenceBlocked(
                "Agent work-plan deliverables differ from the structured lawyer intent"
            )
        def binding_ids(object_type: str, source_status: str) -> set[str]:
            return {
                str(item["binding_id"])
                for item in binding_rows
                if str(item["object_type"]) == object_type
                and str(item["source_status"]) == source_status
            }

        requirements: dict[
            AgentDeliverableKind, tuple[tuple[str, str, str], ...]
        ] = {
            AgentDeliverableKind.CASE_REVIEW_MEMO: (
                ("CASE_FACT", "CONFIRMED", "FACT"),
            ),
            AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST: (
                ("CASE_FACT", "CONFIRMED", "FACT"),
            ),
            AgentDeliverableKind.PAYMENT_LEDGER: (
                ("CASE_TRANSACTION", "CONFIRMED", "TRANSACTION"),
            ),
            AgentDeliverableKind.EVIDENCE_CATALOGUE: (
                ("EVIDENCE_PAGE", "CONFIRMED", "EVIDENCE"),
            ),
            AgentDeliverableKind.DEFENCE_STATEMENT: (
                ("CASE_FACT", "CONFIRMED", "FACT"),
                ("CASE_CLAIM", "CONFIRMED", "CLAIM_SCOPE"),
                ("VERIFIED_LEGAL_SOURCE", "LOCKED", "LEGAL_AUTHORITY"),
                ("APPROVED_LEGAL_RULE", "LOCKED", "LEGAL_RULE"),
            ),
        }
        for row in rows:
            if (
                str(row["item_kind"]) != "DOCUMENT_CANDIDATE"
                or str(row["delivery_target"]) != "INTERNAL_WORK_PRODUCT"
            ):
                raise CaseLedgerPersistenceBlocked(
                    "Agent requested deliverable is not a reviewable document candidate"
                )
            kind = AgentDeliverableKind(str(row["deliverable_kind"]))
            required_sources = requirements.get(kind)
            if required_sources is None:  # pragma: no cover - enum catalogue guard
                raise CaseLedgerPersistenceBlocked(
                    "Agent requested deliverable is outside the governed source catalogue"
                )
            expected_readiness = (
                "ACTIONABLE"
                if all(
                    binding_ids(object_type, source_status)
                    for object_type, source_status, _ in required_sources
                )
                else "NEEDS_INFORMATION"
            )
            if str(row["readiness"]) != expected_readiness:
                raise CaseLedgerPersistenceBlocked(
                    "Agent requested deliverable readiness differs from its governed sources"
                )
            for object_type, source_status, reference_use in required_sources:
                governed_binding_ids = binding_ids(object_type, source_status)
                if not governed_binding_ids:
                    continue
                refs = connection.execute(
                    """
                    SELECT source_id
                    FROM case_work_plan_item_references
                    WHERE plan_id = %s AND item_id = %s
                      AND matter_id = %s AND firm_id = %s
                      AND reference_role = 'SOURCE'
                      AND source_type = 'AGENT_TASK_INPUT'
                      AND reference_use = %s
                    FOR SHARE
                    """,
                    (
                        plan_id,
                        row["item_id"],
                        matter_id,
                        actor.firm_id,
                        reference_use,
                    ),
                ).fetchall()
                if {str(item["source_id"]) for item in refs} != governed_binding_ids:
                    raise CaseLedgerPersistenceBlocked(
                        "Agent deliverable plan item does not bind its complete confirmed source set"
                    )
        actionable_rows = tuple(
            row for row in rows if str(row["readiness"]) == "ACTIONABLE"
        )
        if expected and not actionable_rows:
            raise CaseLedgerPersistenceBlocked(
                "Agent has no source-ready reviewable deliverable to activate"
            )
        if any(
            bool(row.get("required_for_delivery", False))
            and str(row["readiness"]) != "ACTIONABLE"
            for row in rows
        ):
            raise CaseLedgerPersistenceBlocked(
                "required Agent deliverables still need confirmed sources; refresh and promote a new plan"
            )

    def _insert_plan_details(
        self, connection: psycopg.Connection, *, actor: Actor, plan: ValidatedCaseWorkPlan
    ) -> None:
        for reference in plan.context.all_references:
            connection.execute(
                """
                INSERT INTO case_work_plan_context_references (
                    plan_id, firm_id, matter_id, source_type, source_id,
                    source_version, source_hash, reference_use
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    plan.plan_id, actor.firm_id, plan.context.matter_id,
                    reference.source_type.value, reference.source_id,
                    reference.source_version, reference.source_hash, reference.use.value,
                ),
            )
        for item in plan.items:
            connection.execute(
                """
                INSERT INTO case_work_plan_items (
                    item_id, plan_id, firm_id, matter_id, sequence, item_kind,
                    readiness, title, purpose, rationale, risk_if_omitted, confidence,
                    review_gate, delivery_target, deliverable_kind,
                    required_for_delivery, is_primary_document
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    item.item_id, plan.plan_id, actor.firm_id, plan.context.matter_id,
                    item.sequence, item.kind.value, item.readiness.value, item.title,
                    item.purpose, item.rationale, item.risk_if_omitted, item.confidence,
                    item.review_gate.value, item.delivery_target.value,
                    item.deliverable_kind, item.required_for_delivery, item.is_primary_document,
                ),
            )
            for role, references in (("TRIGGER", item.trigger_refs), ("SOURCE", item.source_refs)):
                for reference in references:
                    connection.execute(
                        """
                        INSERT INTO case_work_plan_item_references (
                            plan_id, item_id, firm_id, matter_id, reference_role,
                            source_type, source_id, source_version, source_hash, reference_use
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            plan.plan_id, item.item_id, actor.firm_id,
                            plan.context.matter_id, role, reference.source_type.value,
                            reference.source_id, reference.source_version,
                            reference.source_hash, reference.use.value,
                        ),
                    )
            for prerequisite in item.prerequisites:
                connection.execute(
                    """
                    INSERT INTO case_work_plan_item_prerequisites (
                        plan_id, item_id, prerequisite_item_id, firm_id, matter_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (plan.plan_id, item.item_id, prerequisite, actor.firm_id, plan.context.matter_id),
                )

    def _assert_current_profile(
        self, connection, *, actor: Actor, matter_id: str,
        profile_id: str, profile_version: int, profile_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT profile.profile_id, profile.profile_version, profile.profile_hash,
                   profile.status, head.current_profile_id
            FROM case_posture_profiles profile
            JOIN case_posture_profile_heads head
              ON head.matter_id = profile.matter_id AND head.firm_id = profile.firm_id
            WHERE profile.profile_id = %s AND profile.matter_id = %s AND profile.firm_id = %s
            FOR SHARE
            """,
            (profile_id, matter_id, actor.firm_id),
        ).fetchone()
        if (
            row is None or row["status"] != "CONFIRMED"
            or str(row["current_profile_id"]) != profile_id
            or int(row["profile_version"]) != profile_version
            or row["profile_hash"] != profile_hash
        ):
            raise CaseLedgerPersistenceBlocked(
                "work plan requires the exact current confirmed posture profile"
            )

    def _assert_current_objective(
        self, connection, *, actor: Actor, matter_id: str,
        approval_id: str, objective_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT approval_id, object_hash, approval_type, revoked_at
            FROM approvals objective
            WHERE objective.approval_id = %s AND objective.matter_id = %s AND objective.firm_id = %s
              AND objective.approval_type = 'CASE_WORK_PLAN_OBJECTIVE'
              AND NOT EXISTS (
                  SELECT 1 FROM approvals newer
                  WHERE newer.matter_id = objective.matter_id
                    AND newer.firm_id = objective.firm_id
                    AND newer.approval_type = objective.approval_type
                    AND newer.revoked_at IS NULL
                    AND (newer.approved_matter_version, newer.approved_at, newer.approval_id)
                      > (objective.approved_matter_version, objective.approved_at, objective.approval_id)
              )
            FOR SHARE
            """,
            (approval_id, matter_id, actor.firm_id),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or row["object_hash"] != objective_hash:
            raise CaseLedgerPersistenceBlocked(
                "work plan requires the current lawyer-approved objective"
            )

    def _assert_persisted_references_current(
        self, connection, *, actor: Actor, matter_id: str, plan_id: str
    ) -> None:
        assert_case_work_plan_references_current(
            connection, actor=actor, matter_id=matter_id, plan_id=plan_id
        )

    def _resolve_reference(
        self, connection, *, actor: Actor, matter_id: str, reference: WorkPlanReference
    ) -> ResolvedWorkPlanReference | None:
        return resolve_case_work_plan_reference(
            connection, actor=actor, matter_id=matter_id, reference=reference
        )


    def _begin(
        self, connection, *, actor: Actor, matter_id: str, expected_version: int,
        idempotency_key: str, command_name: str, payload_hash: str,
        allowed_roles: frozenset[Role],
    ) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(
            connection, actor=actor, matter_id=matter_id,
            command_name=command_name, idempotency_key=idempotency_key,
        )
        prior = _prior_receipt(
            connection, actor=actor, matter_id=matter_id, command_name=command_name,
            idempotency_key=idempotency_key, payload_hash=payload_hash,
        )
        if prior is not None:
            return prior
        _authorize_and_lock_matter(
            connection, actor=actor, matter_id=matter_id,
            expected_version=expected_version, allowed_roles=allowed_roles,
        )
        return None

    @staticmethod
    def _validate_command(
        matter_id: str, actor: Actor, expected_version: int,
        idempotency_key: str, roles: frozenset[Role],
    ) -> None:
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, roles)
        _require_positive_version(expected_version)

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _resolved(
    matter_id: str,
    reference: WorkPlanReference,
    row: dict[str, Any] | None,
    *,
    confirmed: bool,
) -> ResolvedWorkPlanReference | None:
    if row is None or row.get("hash") is None:
        return None
    version = row.get("version")
    if isinstance(version, datetime):
        version_text = version.isoformat()
    else:
        version_text = str(version)
    return ResolvedWorkPlanReference(
        matter_id=matter_id,
        source_type=reference.source_type,
        source_id=reference.source_id,
        source_version=version_text,
        source_hash=str(row["hash"]),
        is_current=bool(row.get("current")),
        is_confirmed=confirmed,
        is_effective=bool(row.get("current")),
        conflict_key=(str(row["conflict_key"]) if row.get("conflict_key") else None),
    )


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def assert_case_work_plan_references_current(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    plan_id: str,
) -> None:
    """Fail closed when any source bound to a plan changed after registration.

    This checker is shared by write commands and the document Worker's
    repeatable-read, read-only preflight.  It therefore reads the exact
    version/hash projection without acquiring row locks.  Write callers lock
    the matter/plan before entering this check; document package staging
    revalidates the same bindings in its own commit transaction.
    """

    rows = connection.execute(
        """
        SELECT source_type, source_id, source_version, source_hash, reference_use
        FROM case_work_plan_context_references
        WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
        """,
        (plan_id, matter_id, actor.firm_id),
    ).fetchall()
    if not rows:
        raise CaseLedgerPersistenceBlocked("work plan has no persisted source snapshot")
    for row in rows:
        try:
            reference = WorkPlanReference(
                source_type=WorkPlanSourceType(str(row["source_type"])),
                source_id=str(row["source_id"]),
                source_version=str(row["source_version"]),
                source_hash=str(row["source_hash"]),
                use=WorkPlanReferenceUse(str(row["reference_use"])),
            )
        except (KeyError, ValueError) as error:
            raise CaseLedgerPersistenceBlocked(
                "work plan contains an unsupported persisted source reference"
            ) from error
        resolved = resolve_case_work_plan_reference(
            connection, actor=actor, matter_id=matter_id, reference=reference
        )
        if (
            resolved is None
            or resolved.source_version != reference.source_version
            or resolved.source_hash != reference.source_hash
            or not resolved.is_current
            or not resolved.is_confirmed
            or not resolved.is_effective
        ):
            raise CaseLedgerPersistenceBlocked(
                "work plan source changed or became stale; regenerate and reconfirm the plan"
            )


def resolve_case_work_plan_reference(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    reference: WorkPlanReference,
) -> ResolvedWorkPlanReference | None:
    """Resolve one versioned source without mutating or row-locking it."""

    source_type = reference.source_type
    row: dict[str, Any] | None
    confirmed = True
    if source_type is WorkPlanSourceType.POSTURE_PROFILE:
        row = connection.execute(
            """
            SELECT profile.profile_version AS version, profile.profile_hash AS hash,
                   (profile.status = 'CONFIRMED'
                    AND head.current_profile_id = profile.profile_id) AS current,
                   NULL::text AS conflict_key
            FROM case_posture_profiles profile
            JOIN case_posture_profile_heads head
              ON head.matter_id = profile.matter_id AND head.firm_id = profile.firm_id
            WHERE profile.profile_id = %s AND profile.matter_id = %s AND profile.firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.LAWYER_OBJECTIVE:
        row = connection.execute(
            """
            SELECT objective.approved_matter_version AS version, objective.object_hash AS hash,
                   (objective.revoked_at IS NULL AND NOT EXISTS (
                       SELECT 1 FROM approvals newer
                       WHERE newer.matter_id = objective.matter_id
                         AND newer.firm_id = objective.firm_id
                         AND newer.approval_type = objective.approval_type
                         AND newer.revoked_at IS NULL
                         AND (newer.approved_matter_version, newer.approved_at, newer.approval_id)
                           > (objective.approved_matter_version, objective.approved_at, objective.approval_id)
                   )) AS current,
                   NULL::text AS conflict_key
            FROM approvals objective
            WHERE objective.approval_id = %s AND objective.matter_id = %s
              AND objective.firm_id = %s
              AND objective.approval_type = 'CASE_WORK_PLAN_OBJECTIVE'
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.AGENT_GOAL:
        row = connection.execute(
            """
            SELECT 'v1'::text AS version, goal_hash AS hash,
                   true AS current, NULL::text AS conflict_key
            FROM case_agent_goals
            WHERE goal_id = %s AND matter_id = %s AND firm_id = %s
            -- Agent goals are append-only.  A row lock adds no consistency
            -- guarantee here and would require UPDATE privilege that both
            -- the Worker and Web roles deliberately do not hold.
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.AGENT_TASK_INPUT:
        row = connection.execute(
            """
            SELECT binding.object_version AS version,
                   binding.binding_hash AS hash,
                   true AS current,
                   NULL::text AS conflict_key
            FROM case_agent_work_plan_input_bindings binding
            JOIN case_agent_work_plan_promotions promotion
              ON promotion.promotion_id = binding.promotion_id
             AND promotion.plan_id = binding.plan_id
             AND promotion.firm_id = binding.firm_id
             AND promotion.matter_id = binding.matter_id
            JOIN case_work_plans plan
              ON plan.plan_id = binding.plan_id
             AND plan.firm_id = binding.firm_id
             AND plan.matter_id = binding.matter_id
            JOIN matters matter
              ON matter.matter_id = binding.matter_id
             AND matter.firm_id = binding.firm_id
            JOIN case_agent_runs agent_run
              ON agent_run.run_id = promotion.run_id
             AND agent_run.firm_id = promotion.firm_id
             AND agent_run.matter_id = promotion.matter_id
            JOIN case_agent_task_graphs graph
              ON graph.graph_id = promotion.graph_id
             AND graph.run_id = promotion.run_id
             AND graph.firm_id = promotion.firm_id
             AND graph.matter_id = promotion.matter_id
            JOIN case_agent_verification_receipts receipt
              ON receipt.verification_receipt_id = promotion.verification_receipt_id
             AND receipt.run_id = promotion.run_id
             AND receipt.firm_id = promotion.firm_id
             AND receipt.matter_id = promotion.matter_id
            WHERE binding.binding_id = %s
              AND binding.matter_id = %s AND binding.firm_id = %s
              AND agent_run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
              AND NOT agent_run.is_stale AND NOT agent_run.is_cancelled
              AND agent_run.current_graph_id = graph.graph_id
              AND agent_run.current_graph_version = graph.graph_version
              AND agent_run.current_graph_hash = graph.graph_hash
              AND agent_run.snapshot_matter_version = graph.snapshot_matter_version
              AND agent_run.snapshot_schema_version = graph.snapshot_schema_version
              AND agent_run.snapshot_hash = graph.snapshot_hash
              AND agent_run.verification_hash = receipt.verification_hash
              AND receipt.outcome = 'PASSED'
              AND receipt.graph_hash = graph.graph_hash
              AND receipt.snapshot_hash = graph.snapshot_hash
              AND promotion.graph_version = graph.graph_version
              AND promotion.graph_hash = graph.graph_hash
              AND promotion.snapshot_matter_version = graph.snapshot_matter_version
              AND promotion.snapshot_schema_version = graph.snapshot_schema_version
              AND promotion.snapshot_hash = graph.snapshot_hash
              AND promotion.verification_hash = receipt.verification_hash
              AND (
                  (plan.status = 'CANDIDATE'
                   AND matter.version = plan.planned_matter_version + 1)
                  OR (plan.status = 'ACTIVE'
                      AND matter.version = plan.activated_matter_version)
              )
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.CLAIM:
        row = connection.execute(
            """
            SELECT updated_at AS version, confirmation_hash AS hash,
                   (status = 'CONFIRMED_SCOPE') AS current,
                   NULL::text AS conflict_key
            FROM case_claims
            WHERE claim_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.CASE_FACT:
        row = connection.execute(
            """
            SELECT updated_at AS version, decision_hash AS hash,
                   (status = 'CONFIRMED') AS current,
                   NULL::text AS conflict_key
            FROM case_facts
            WHERE fact_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.DISPUTE_ISSUE:
        row = connection.execute(
            """
            SELECT updated_at AS version, approval_hash AS hash,
                   (status = 'CONFIRMED') AS current,
                   NULL::text AS conflict_key
            FROM case_dispute_issues
            WHERE issue_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.TRANSACTION:
        row = connection.execute(
            """
            SELECT updated_at AS version, confirmation_hash AS hash,
                   (status = 'CONFIRMED') AS current,
                   NULL::text AS conflict_key
            FROM case_transactions
            WHERE transaction_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.EVIDENCE_PAGE:
        row = connection.execute(
            """
            SELECT page.page_number AS version, original.original_file_sha256 AS hash,
                   EXISTS (
                       SELECT 1 FROM evidence_page_decisions decision
                       WHERE decision.evidence_page_id = page.evidence_page_id
                         AND decision.matter_id = page.matter_id
                         AND decision.firm_id = page.firm_id
                         AND decision.status = 'APPROVED'
                         AND decision.disposition = 'INCLUDE'
                   ) AS current,
                   NULL::text AS conflict_key
            FROM evidence_pages page
            JOIN evidence_original_files original
              ON original.evidence_file_id = page.evidence_file_id
             AND original.firm_id = page.firm_id
             AND original.matter_id = page.matter_id
            WHERE page.evidence_page_id = %s AND page.matter_id = %s AND page.firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.EVIDENCE_MANIFEST:
        row = connection.execute(
            """
            SELECT ledger_version AS version, content_hash AS hash,
                   (status = 'LOCKED') AS current,
                   NULL::text AS conflict_key
            FROM evidence_manifests
            WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.LEGAL_EVENT:
        row = connection.execute(
            """
            SELECT approved_at AS version, approval_hash AS hash,
                   (status = 'APPROVED') AS current,
                   event_kind AS conflict_key
            FROM case_legal_events
            WHERE legal_event_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT:
        row = connection.execute(
            """
            SELECT retrieved_at AS version, content_sha256 AS hash,
                   (verification_status = 'VERIFIED' AND license_status = 'ACTIVE') AS current,
                   source_id AS conflict_key
            FROM official_legal_source_snapshots
            WHERE snapshot_id = %s AND firm_id = %s
            """,
            (reference.source_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.LEGAL_RULE_VERSION:
        row = connection.execute(
            """
            SELECT rule_version AS version, approval_hash AS hash,
                   (status = 'APPROVED') AS current,
                   conflict_set AS conflict_key
            FROM legal_rule_versions
            WHERE rule_version_id = %s AND firm_id = %s
            """,
            (reference.source_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.LEGAL_BUNDLE:
        row = connection.execute(
            """
            SELECT version, bundle_hash AS hash, (status = 'APPROVED') AS current,
                   NULL::text AS conflict_key
            FROM case_legal_bundles
            WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.CALCULATION_RUN:
        row = connection.execute(
            """
            SELECT scenario_version AS version, output_hash AS hash,
                   (status = 'VERIFIED') AS current,
                   NULL::text AS conflict_key
            FROM calculation_runs
            WHERE run_id = %s AND matter_id = %s AND firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    elif source_type is WorkPlanSourceType.COURT_PROCEEDING:
        row = connection.execute(
            """
            SELECT version.proceeding_version AS version, version.meaning_hash AS hash,
                   (head.current_version_id = version.proceeding_version_id) AS current,
                   NULL::text AS conflict_key
            FROM court_proceeding_versions version
            JOIN court_proceeding_heads head
              ON head.proceeding_id = version.proceeding_id
             AND head.matter_id = version.matter_id AND head.firm_id = version.firm_id
            WHERE version.proceeding_version_id = %s
              AND version.matter_id = %s AND version.firm_id = %s
            """,
            (reference.source_id, matter_id, actor.firm_id),
        ).fetchone()
    else:
        # Service/deadline ledgers and any future source types must be added as
        # explicit adapters.  Unknown sources never become current by default.
        return None
    return _resolved(matter_id, reference, row, confirmed=confirmed)
