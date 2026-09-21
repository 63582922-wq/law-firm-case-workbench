"""PostgreSQL adapter for governed pre-planning memory enrichment.

The dedicated worker never becomes the retrieval principal.  It resolves the
human who created the exact Agent run from PostgreSQL, reconstructs that
lawyer's *current* matter roles and memory groups, and invokes the existing
ACL-first FTS adapter as that verified human principal.  Returned records are
re-authorized once more before a minimal receipt is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol
from uuid import uuid4

from psycopg.types.json import Jsonb

from .case_agent_memory import (
    AgentMemoryBlocked,
    MemoryAuthority,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
    RetrievedMemoryHit,
    SourceExposure,
    VerifiedRetrievalPrincipal,
    build_retrieval_query,
)
from .case_agent_memory_postgres import PostgresCaseAgentMemoryStore
from .case_agent_planner import CasePlanningSnapshot
from .case_agent_planning_memory import (
    PlanningMemoryBlocked,
    PlanningMemoryEnrichmentReceipt,
    PlanningMemoryItem,
    PlanningMemoryPurpose,
    PlanningMemorySearchRequest,
    PlanningMemorySourceRef,
    build_receipt,
    planning_memory_source_ref_hash,
)
from .case_agent_supervisor import AgentRunState
from .models import Actor, Role


_HUMAN_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)


@dataclass(frozen=True)
class CaseMemoryDisclosureRequest:
    """Exact input to the existing lawyer-authorized external-request boundary."""

    firm_id: str
    matter_id: str
    run_id: str
    goal_hash: str
    case_snapshot_hash: str
    base_planning_hash: str
    owner_actor_id: str
    purpose: PlanningMemoryPurpose
    query_hash: str
    query_contract_hash: str
    external_planner_contract_hash: str | None
    record_id: str
    record_version: int
    content_hash: str
    provenance_hash: str
    summary_hash: str
    source_ref_hash: str


class CaseMemoryDisclosureAuthorizationPort(Protocol):
    """Return the currently valid exact authorization hash, or ``None``.

    A production adapter must validate the existing external-request ledger's
    provider, service, processor region, retention/training policy, expiry,
    call/cost cap and selected-field set against this entire request.  It must
    not create or widen an authorization.
    """

    def current_authorization_hash(
        self, *, request: CaseMemoryDisclosureRequest
    ) -> str | None: ...


class PostgresPlanningMemoryEnrichmentStore(PostgresCaseAgentMemoryStore):
    """Real ACL-first FTS enrichment port for a planning snapshot provider."""

    def __init__(
        self,
        dsn: str,
        *,
        disclosure_authorization: CaseMemoryDisclosureAuthorizationPort | None = None,
    ) -> None:
        super().__init__(dsn)
        if disclosure_authorization is not None and not callable(
            getattr(disclosure_authorization, "current_authorization_hash", None)
        ):
            raise ValueError("case-memory disclosure authorization port is invalid")
        self._disclosure_authorization = disclosure_authorization

    def current_enrichment(
        self,
        *,
        state: AgentRunState,
        worker: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
    ) -> PlanningMemoryEnrichmentReceipt | None:
        _validate_enrichment_call(
            state=state,
            worker=worker,
            base_snapshot=base_snapshot,
            request=request,
        )

        # First resolve the actual human owner and current grants from the run;
        # a caller-supplied Actor can never nominate the retrieval principal.
        owner, current_time = self._resolve_run_owner(
            state=state, worker=worker
        )
        principal = self.resolve_retrieval_principal(
            actor=owner, matter_id=state.matter_id
        )
        query = _build_query(
            principal=principal,
            request=request,
            knowledge_as_of=current_time,
        )

        # Reuse is allowed only after live run/owner/group/record/source and
        # external-disclosure revalidation.  Any drift returns None and causes
        # a new ACL-first retrieval below; stale content is never used.
        existing = self._load_current_receipt(
            state=state,
            worker=worker,
            owner=owner,
            base_snapshot=base_snapshot,
            request=request,
            query=query,
        )
        if existing is not None:
            return existing

        try:
            retrieval = self.retrieve_full_text(
                actor=owner,
                query=query,
                execution_run_id=state.run_id,
            )
        except AgentMemoryBlocked as error:
            if str(error) == "no memory records are authorized for this query":
                return None
            raise PlanningMemoryBlocked("ACL-first memory retrieval was blocked") from error
        if not retrieval.hits:
            return None

        # A third, fresh authority read is intentional: only records still
        # authorized after FTS and after its audit commit can enter planning.
        with self._transaction(worker.firm_id) as connection:
            resolved_owner, resolved_time = _resolve_run_owner_in_connection(
                connection, state=state, worker=worker
            )
            if resolved_owner != owner:
                raise PlanningMemoryBlocked(
                    "run owner roles changed after memory retrieval"
                )
            authority, roles = self._load_authority(
                connection, actor=resolved_owner, matter_id=state.matter_id
            )
            if (
                authority.matter_access_grant_hash
                != query.access.matter_access_grant_hash
                or authority.matter_version != state.snapshot.matter_version
                or roles != owner.roles
            ):
                raise PlanningMemoryBlocked(
                    "run owner authority changed after memory retrieval"
                )
            if resolved_time > retrieval.scope.expires_at:
                raise PlanningMemoryBlocked(
                    "memory retrieval authorization expired before final verification"
                )
            current_query = _build_query(
                principal=VerifiedRetrievalPrincipal(
                    actor=resolved_owner,
                    matter_id=state.matter_id,
                    matter_access_grant_hash=authority.matter_access_grant_hash,
                    matter_version=authority.matter_version,
                    permission_group_ids=authority.permission_group_ids,
                ),
                request=request,
                knowledge_as_of=resolved_time,
            )
            records = self._load_authoritative_records(
                connection, query=current_query, authority=authority
            )
            items = self._build_items(
                connection,
                state=state,
                owner=resolved_owner,
                base_snapshot=base_snapshot,
                request=request,
                retrieval_hits=retrieval.hits,
                scope_records=retrieval.scope.authorized_records,
                current_records=records,
            )
            if not items:
                return None
            receipt = build_receipt(
                enrichment_id=str(uuid4()),
                firm_id=state.firm_id,
                matter_id=state.matter_id,
                run_id=state.run_id,
                goal_id=state.goal.goal_id,
                goal_hash=state.goal.goal_hash,
                owner_actor_id=owner.actor_id,
                purpose=request.purpose,
                case_snapshot_hash=state.snapshot.snapshot_hash,
                case_snapshot_version=state.snapshot.matter_version,
                case_snapshot_schema_version=state.snapshot.schema_version,
                base_planning_hash=base_snapshot.planning_hash,
                query_hash=request.query_hash,
                query_contract_hash=request.query_contract_hash,
                query_fingerprint=retrieval.scope.query_fingerprint,
                retrieval_id=retrieval.retrieval_id,
                retrieval_scope_hash=retrieval.scope.scope_hash,
                owner_grant_hash=authority.matter_access_grant_hash,
                owner_roles=tuple(sorted(roles, key=lambda item: item.value)),
                permission_group_ids=authority.permission_group_ids,
                items=items,
                retrieved_at=retrieval.scope.authorized_at,
                verified_at=retrieval.verified_at,
                final_verified_at=resolved_time,
            )
            self._insert_receipt(connection, worker=worker, receipt=receipt)
            return receipt

    def _resolve_run_owner(
        self, *, state: AgentRunState, worker: Actor
    ) -> tuple[Actor, datetime]:
        with self._transaction(worker.firm_id) as connection:
            return _resolve_run_owner_in_connection(
                connection, state=state, worker=worker
            )

    def _load_current_receipt(
        self,
        *,
        state: AgentRunState,
        worker: Actor,
        owner: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
        query: MemoryQuery,
    ) -> PlanningMemoryEnrichmentReceipt | None:
        with self._transaction(worker.firm_id) as connection:
            resolved_owner, current_time = _resolve_run_owner_in_connection(
                connection, state=state, worker=worker
            )
            if resolved_owner != owner:
                return None
            authority, roles = self._load_authority(
                connection, actor=owner, matter_id=state.matter_id
            )
            if (
                authority.matter_access_grant_hash
                != query.access.matter_access_grant_hash
                or authority.matter_version != state.snapshot.matter_version
                or roles != owner.roles
            ):
                return None
            row = connection.execute(
                """
                SELECT enrichment.*
                FROM case_agent_planning_memory_enrichments enrichment
                WHERE enrichment.firm_id = %s AND enrichment.matter_id = %s
                  AND enrichment.run_id = %s AND enrichment.goal_id = %s
                  AND enrichment.goal_hash = %s AND enrichment.owner_actor_id = %s
                  AND enrichment.purpose = %s
                  AND enrichment.case_snapshot_hash = %s
                  AND enrichment.case_snapshot_version = %s
                  AND enrichment.case_snapshot_schema_version = %s
                  AND enrichment.base_planning_hash = %s
                  AND enrichment.query_hash = %s
                  AND enrichment.query_contract_hash = %s
                  AND enrichment.owner_grant_hash = %s
                ORDER BY enrichment.created_at DESC, enrichment.enrichment_id DESC
                LIMIT 1
                """,
                (
                    state.firm_id,
                    state.matter_id,
                    state.run_id,
                    state.goal.goal_id,
                    state.goal.goal_hash,
                    owner.actor_id,
                    request.purpose.value,
                    state.snapshot.snapshot_hash,
                    state.snapshot.matter_version,
                    state.snapshot.schema_version,
                    base_snapshot.planning_hash,
                    request.query_hash,
                    request.query_contract_hash,
                    authority.matter_access_grant_hash,
                ),
            ).fetchone()
            if row is None:
                return None
            try:
                receipt = _receipt_from_row(row)
                receipt.validate()
                if (
                    receipt.query_contract_hash != request.query_contract_hash
                    or receipt.final_verified_at > current_time
                ):
                    return None
                records = self._load_authoritative_records(
                    connection,
                    query=_build_query(
                        principal=VerifiedRetrievalPrincipal(
                            actor=owner,
                            matter_id=state.matter_id,
                            matter_access_grant_hash=authority.matter_access_grant_hash,
                            matter_version=authority.matter_version,
                            permission_group_ids=authority.permission_group_ids,
                        ),
                        request=request,
                        knowledge_as_of=current_time,
                    ),
                    authority=authority,
                )
                if not _items_match_current_records(receipt.items, records):
                    return None
                if not self._disclosures_are_current(
                    state=state,
                    owner=owner,
                    base_snapshot=base_snapshot,
                    request=request,
                    items=receipt.items,
                ):
                    return None
                audit = connection.execute(
                    """
                    SELECT actor_id, run_id, task_id, query_hash,
                           query_fingerprint, grant_hash, matter_version,
                           scope_hash, returned_record_refs, verified_at
                    FROM case_agent_memory_retrieval_audits
                    WHERE retrieval_id = %s AND firm_id = %s AND matter_id = %s
                    """,
                    (receipt.retrieval_id, state.firm_id, state.matter_id),
                ).fetchone()
                if not _audit_matches_receipt(audit, receipt):
                    return None
                return receipt
            except (AgentMemoryBlocked, PlanningMemoryBlocked, ValueError, TypeError):
                return None

    def _build_items(
        self,
        connection: Any,
        *,
        state: AgentRunState,
        owner: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
        retrieval_hits: tuple[RetrievedMemoryHit, ...],
        scope_records: tuple[Any, ...],
        current_records: tuple[MemoryRecord, ...],
    ) -> tuple[PlanningMemoryItem, ...]:
        records = {item.record_id: item for item in current_records}
        scoped = {item.record_id: item for item in scope_records}
        hit_ids = tuple(item.record_id for item in retrieval_hits)
        if len(hit_ids) != len(set(hit_ids)):
            raise PlanningMemoryBlocked("memory retrieval returned duplicate records")
        for hit in retrieval_hits:
            record = records.get(hit.record_id)
            scope = scoped.get(hit.record_id)
            if (
                record is None
                or scope is None
                or record.record_version != scope.record_version
                or record.content_hash != hit.content_hash
                or record.content_hash != scope.content_hash
                or record.provenance_hash != scope.provenance_hash
            ):
                raise PlanningMemoryBlocked(
                    "memory record changed after retrieval; re-retrieval is required"
                )
        rows = connection.execute(
            """
            SELECT version.record_id, version.record_version,
                   version.content_sha256, version.provenance_hash,
                   version.search_document, version.search_document_hash
            FROM case_agent_memory_record_heads head
            JOIN case_agent_memory_record_versions version
              ON version.record_id = head.record_id
             AND version.record_version = head.current_version
             AND version.firm_id = head.firm_id
            WHERE version.firm_id = %s
              AND version.record_id = ANY(%s::uuid[])
            ORDER BY version.record_id
            """,
            (state.firm_id, list(hit_ids)),
        ).fetchall()
        row_by_id = {str(row["record_id"]): row for row in rows}
        result: list[PlanningMemoryItem] = []
        for hit in retrieval_hits:
            record = records[hit.record_id]
            row = row_by_id.get(hit.record_id)
            if (
                row is None
                or int(row["record_version"]) != record.record_version
                or row["content_sha256"] != record.content_hash
                or row["provenance_hash"] != record.provenance_hash
                or sha256(row["search_document"].encode("utf-8")).hexdigest()
                != row["search_document_hash"]
            ):
                raise PlanningMemoryBlocked(
                    "memory summary source changed after authorization"
                )
            summary = _minimal_summary(row["search_document"])
            sources = tuple(
                PlanningMemorySourceRef(
                    source_type=source.source_type,
                    source_id=source.source_id,
                    source_version=source.source_version,
                    content_hash=source.content_hash,
                    exposure=source.exposure,
                    page_number=source.page_number,
                )
                for source in record.source_refs
            )
            summary_hash = sha256(summary.encode("utf-8")).hexdigest()
            source_ref_hash = planning_memory_source_ref_hash(sources)
            disclosure_hash: str | None = None
            externally_disclosable = record.layer is not MemoryLayer.CASE_LONG_TERM
            if record.layer is MemoryLayer.CASE_LONG_TERM:
                disclosure_hash = self._current_disclosure_hash(
                    state=state,
                    owner=owner,
                    base_snapshot=base_snapshot,
                    request=request,
                    record=record,
                    summary_hash=summary_hash,
                    source_ref_hash=source_ref_hash,
                )
                externally_disclosable = disclosure_hash is not None
            result.append(
                PlanningMemoryItem.build(
                    record_id=record.record_id,
                    record_version=record.record_version,
                    layer=record.layer,
                    authority=record.authority,
                    content_hash=record.content_hash,
                    provenance_hash=record.provenance_hash,
                    summary=summary,
                    source_refs=sources,
                    externally_disclosable=externally_disclosable,
                    external_authorization_hash=disclosure_hash,
                )
            )
        return tuple(sorted(result, key=lambda item: item.ref_id))

    def _current_disclosure_hash(
        self,
        *,
        state: AgentRunState,
        owner: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
        record: MemoryRecord,
        summary_hash: str,
        source_ref_hash: str,
    ) -> str | None:
        if self._disclosure_authorization is None:
            return None
        if request.external_planner_contract_hash is None:
            return None
        value = self._disclosure_authorization.current_authorization_hash(
            request=CaseMemoryDisclosureRequest(
                firm_id=state.firm_id,
                matter_id=state.matter_id,
                run_id=state.run_id,
                goal_hash=state.goal.goal_hash,
                case_snapshot_hash=state.snapshot.snapshot_hash,
                base_planning_hash=base_snapshot.planning_hash,
                owner_actor_id=owner.actor_id,
                purpose=request.purpose,
                query_hash=request.query_hash,
                query_contract_hash=request.query_contract_hash,
                external_planner_contract_hash=request.external_planner_contract_hash,
                record_id=record.record_id,
                record_version=record.record_version,
                content_hash=record.content_hash,
                provenance_hash=record.provenance_hash,
                summary_hash=summary_hash,
                source_ref_hash=source_ref_hash,
            )
        )
        if value is not None:
            _hash(value, "case-memory disclosure authorization hash")
        return value

    def _disclosures_are_current(
        self,
        *,
        state: AgentRunState,
        owner: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
        items: tuple[PlanningMemoryItem, ...],
    ) -> bool:
        for item in items:
            if item.layer is not MemoryLayer.CASE_LONG_TERM:
                continue
            expected: str | None = None
            if (
                self._disclosure_authorization is not None
                and request.external_planner_contract_hash is not None
            ):
                expected = self._disclosure_authorization.current_authorization_hash(
                    request=CaseMemoryDisclosureRequest(
                        firm_id=state.firm_id,
                        matter_id=state.matter_id,
                        run_id=state.run_id,
                        goal_hash=state.goal.goal_hash,
                        case_snapshot_hash=state.snapshot.snapshot_hash,
                        base_planning_hash=base_snapshot.planning_hash,
                        owner_actor_id=owner.actor_id,
                        purpose=request.purpose,
                        query_hash=request.query_hash,
                        query_contract_hash=request.query_contract_hash,
                        external_planner_contract_hash=(
                            request.external_planner_contract_hash
                        ),
                        record_id=item.record_id,
                        record_version=item.record_version,
                        content_hash=item.content_hash,
                        provenance_hash=item.provenance_hash,
                        summary_hash=item.summary_hash,
                        source_ref_hash=planning_memory_source_ref_hash(item.source_refs),
                    )
                )
            if expected != item.external_authorization_hash:
                return False
        return True

    @staticmethod
    def _insert_receipt(
        connection: Any,
        *,
        worker: Actor,
        receipt: PlanningMemoryEnrichmentReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO case_agent_planning_memory_enrichments (
                enrichment_id, firm_id, matter_id, run_id, goal_id, goal_hash,
                owner_actor_id, purpose, case_snapshot_hash,
                case_snapshot_version, case_snapshot_schema_version,
                base_planning_hash, query_hash, query_contract_hash,
                query_fingerprint, retrieval_id,
                retrieval_scope_hash, owner_grant_hash, owner_roles,
                permission_group_ids, items, items_hash, retrieved_at,
                verified_at, final_verified_at, receipt_hash, created_by_worker
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            """,
            (
                receipt.enrichment_id,
                receipt.firm_id,
                receipt.matter_id,
                receipt.run_id,
                receipt.goal_id,
                receipt.goal_hash,
                receipt.owner_actor_id,
                receipt.purpose.value,
                receipt.case_snapshot_hash,
                receipt.case_snapshot_version,
                receipt.case_snapshot_schema_version,
                receipt.base_planning_hash,
                receipt.query_hash,
                receipt.query_contract_hash,
                receipt.query_fingerprint,
                receipt.retrieval_id,
                receipt.retrieval_scope_hash,
                receipt.owner_grant_hash,
                [item.value for item in receipt.owner_roles],
                list(receipt.permission_group_ids),
                Jsonb([_item_json(item) for item in receipt.items]),
                receipt.items_hash,
                receipt.retrieved_at,
                receipt.verified_at,
                receipt.final_verified_at,
                receipt.receipt_hash,
                worker.actor_id,
            ),
        )


def _validate_enrichment_call(
    *,
    state: AgentRunState,
    worker: Actor,
    base_snapshot: CasePlanningSnapshot,
    request: PlanningMemorySearchRequest,
) -> None:
    if worker.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("planning memory requires a dedicated SYSTEM_WORKER")
    if worker.firm_id != state.firm_id:
        raise PermissionError("planning memory worker belongs to another firm")
    request.validate()
    base_snapshot.validate()
    if base_snapshot.case_snapshot != state.snapshot:
        raise PlanningMemoryBlocked(
            "base planning snapshot differs from the Agent run snapshot"
        )
    if state.cancelled:
        raise PlanningMemoryBlocked("cancelled run cannot retrieve memory")


def _resolve_run_owner_in_connection(
    connection: Any, *, state: AgentRunState, worker: Actor
) -> tuple[Actor, datetime]:
    row = connection.execute(
        """
        SELECT run.run_id, run.goal_id, run.created_by, run.snapshot_hash,
               run.snapshot_matter_version, run.snapshot_schema_version,
               goal.goal_hash, goal.requested_by, matter.version AS matter_version,
               clock_timestamp() AS checked_at,
               COALESCE(
                   array_agg(DISTINCT owner_role.role)
                       FILTER (WHERE owner_role.role IS NOT NULL),
                   ARRAY[]::text[]
               ) AS owner_roles
        FROM case_agent_runs run
        JOIN case_agent_goals goal
          ON goal.goal_id = run.goal_id AND goal.firm_id = run.firm_id
         AND goal.matter_id = run.matter_id
        JOIN matters matter
          ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN users worker_user
          ON worker_user.user_id = %s AND worker_user.firm_id = run.firm_id
         AND worker_user.status = 'ACTIVE'
        JOIN matter_actor_roles worker_role
          ON worker_role.matter_id = run.matter_id
         AND worker_role.firm_id = run.firm_id
         AND worker_role.user_id = worker_user.user_id
         AND worker_role.role = 'SYSTEM_WORKER'
         AND worker_role.revoked_at IS NULL
        JOIN users owner
          ON owner.user_id = run.created_by AND owner.firm_id = run.firm_id
         AND owner.status = 'ACTIVE'
        LEFT JOIN matter_actor_roles owner_role
          ON owner_role.matter_id = run.matter_id
         AND owner_role.firm_id = run.firm_id
         AND owner_role.user_id = owner.user_id
         AND owner_role.revoked_at IS NULL
        WHERE run.run_id = %s AND run.firm_id = %s AND run.matter_id = %s
        GROUP BY run.run_id, run.goal_id, run.created_by, run.snapshot_hash,
                 run.snapshot_matter_version, run.snapshot_schema_version,
                 goal.goal_hash, goal.requested_by, matter.version
        """,
        (worker.actor_id, state.run_id, state.firm_id, state.matter_id),
    ).fetchone()
    if row is None:
        raise PermissionError("worker cannot resolve this Agent run owner")
    roles = frozenset(Role(value) for value in row["owner_roles"])
    if not roles.intersection(_HUMAN_ROLES) or Role.SYSTEM_WORKER in roles:
        raise PermissionError("Agent run owner lacks a current human matter role")
    goal_matches = row["goal_hash"] == state.goal.goal_hash
    if not goal_matches and state.goal.material_read_refs:
        # Original goal rows are immutable. Only an exact replayed scope
        # review can bridge their hash to the effective goal; text never can.
        review_row = connection.execute(
            """
            SELECT event.payload FROM case_agent_events event
            JOIN case_agent_runs run USING (run_id,firm_id,matter_id)
            WHERE event.run_id=%s AND event.firm_id=%s AND event.matter_id=%s
              AND event.event_type='PLANNING_MATERIAL_SCOPE_REVIEWED'
              AND run.current_event_version=%s
              AND event.event_sequence<=run.current_event_version
              AND NOT run.is_stale AND NOT run.is_cancelled
            """, (state.run_id, state.firm_id, state.matter_id, state.event_version),
        ).fetchall()
        if len(review_row) == 1:
            from .case_agent_postgres import _payload_from_json
            from .case_agent_supervisor import AgentEventType
            review = _payload_from_json(AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED, review_row[0]["payload"])
            goal_matches = (review.original_goal_hash == row["goal_hash"]
                and review.effective_goal_hash == state.goal.goal_hash
                and review.snapshot == state.snapshot
                and review.material_read_refs == state.goal.material_read_refs
                and review.approved_output_bytes == state.budget.max_output_bytes)
    if (
        str(row["goal_id"]) != state.goal.goal_id
        or not goal_matches
        or str(row["created_by"]) != state.goal.requested_by
        or str(row["requested_by"]) != state.goal.requested_by
        or row["snapshot_hash"] != state.snapshot.snapshot_hash
        or int(row["snapshot_matter_version"]) != state.snapshot.matter_version
        or row["snapshot_schema_version"] != state.snapshot.schema_version
        or int(row["matter_version"]) != state.snapshot.matter_version
    ):
        raise PlanningMemoryBlocked(
            "Agent run, goal or current case version changed before memory retrieval"
        )
    return (
        Actor(
            actor_id=str(row["created_by"]),
            firm_id=state.firm_id,
            roles=roles,
        ),
        row["checked_at"],
    )


def _build_query(
    *, principal: VerifiedRetrievalPrincipal, request: PlanningMemorySearchRequest,
    knowledge_as_of: datetime,
) -> MemoryQuery:
    return build_retrieval_query(
        principal=principal,
        query_text=request.query_text,
        layers=request.layers,
        knowledge_as_of=knowledge_as_of,
        legal_period_start=request.legal_period_start,
        legal_period_end=request.legal_period_end,
        case_type_codes=request.case_type_codes,
        procedure_stages=request.procedure_stages,
        issue_tags=request.issue_tags,
        case_type_mode=request.case_type_mode,
        procedure_stage_mode=request.procedure_stage_mode,
        issue_tag_mode=request.issue_tag_mode,
        max_results=request.max_results,
    )


def _minimal_summary(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise PlanningMemoryBlocked("memory summary source is empty")
    encoded = normalized.encode("utf-8")
    if len(encoded) <= 300:
        return normalized
    clipped = encoded[:300]
    while clipped:
        try:
            result = clipped.decode("utf-8").rstrip()
            if result:
                return result
        except UnicodeDecodeError:
            pass
        clipped = clipped[:-1]
    raise PlanningMemoryBlocked("memory summary could not be bounded")


def _item_json(item: PlanningMemoryItem) -> dict[str, object]:
    return {
        "record_id": item.record_id,
        "record_version": item.record_version,
        "layer": item.layer.value,
        "authority": item.authority.value,
        "content_hash": item.content_hash,
        "provenance_hash": item.provenance_hash,
        "summary": item.summary,
        "summary_hash": item.summary_hash,
        "source_refs": [
            {
                "source_type": source.source_type,
                "source_id": source.source_id,
                "source_version": source.source_version,
                "content_hash": source.content_hash,
                "exposure": source.exposure.value,
                "page_number": source.page_number,
            }
            for source in item.source_refs
        ],
        "externally_disclosable": item.externally_disclosable,
        "external_authorization_hash": item.external_authorization_hash,
        "item_hash": item.item_hash,
        "source_ref_hash": planning_memory_source_ref_hash(item.source_refs),
    }


def _receipt_from_row(row: Mapping[str, Any]) -> PlanningMemoryEnrichmentReceipt:
    items = tuple(
        PlanningMemoryItem(
            record_id=value["record_id"],
            record_version=int(value["record_version"]),
            layer=MemoryLayer(value["layer"]),
            authority=MemoryAuthority(value["authority"]),
            content_hash=value["content_hash"],
            provenance_hash=value["provenance_hash"],
            summary=value["summary"],
            summary_hash=value["summary_hash"],
            source_refs=tuple(
                PlanningMemorySourceRef(
                    source_type=source["source_type"],
                    source_id=source["source_id"],
                    source_version=source["source_version"],
                    content_hash=source["content_hash"],
                    exposure=SourceExposure(source["exposure"]),
                    page_number=source.get("page_number"),
                )
                for source in value["source_refs"]
            ),
            externally_disclosable=bool(value["externally_disclosable"]),
            external_authorization_hash=value.get("external_authorization_hash"),
            item_hash=value["item_hash"],
        )
        for value in row["items"]
    )
    return PlanningMemoryEnrichmentReceipt(
        enrichment_id=str(row["enrichment_id"]),
        firm_id=str(row["firm_id"]),
        matter_id=str(row["matter_id"]),
        run_id=str(row["run_id"]),
        goal_id=str(row["goal_id"]),
        goal_hash=row["goal_hash"],
        owner_actor_id=str(row["owner_actor_id"]),
        purpose=PlanningMemoryPurpose(row["purpose"]),
        case_snapshot_hash=row["case_snapshot_hash"],
        case_snapshot_version=int(row["case_snapshot_version"]),
        case_snapshot_schema_version=row["case_snapshot_schema_version"],
        base_planning_hash=row["base_planning_hash"],
        query_hash=row["query_hash"],
        query_contract_hash=row["query_contract_hash"],
        query_fingerprint=row["query_fingerprint"],
        retrieval_id=str(row["retrieval_id"]),
        retrieval_scope_hash=row["retrieval_scope_hash"],
        owner_grant_hash=row["owner_grant_hash"],
        owner_roles=tuple(sorted((Role(value) for value in row["owner_roles"]), key=lambda item: item.value)),
        permission_group_ids=tuple(sorted(str(value) for value in row["permission_group_ids"])),
        items=items,
        items_hash=row["items_hash"],
        retrieved_at=row["retrieved_at"],
        verified_at=row["verified_at"],
        final_verified_at=row["final_verified_at"],
        receipt_hash=row["receipt_hash"],
    )


def _items_match_current_records(
    items: tuple[PlanningMemoryItem, ...], records: tuple[MemoryRecord, ...]
) -> bool:
    current = {item.record_id: item for item in records}
    for item in items:
        record = current.get(item.record_id)
        if (
            record is None
            or record.record_version != item.record_version
            or record.content_hash != item.content_hash
            or record.provenance_hash != item.provenance_hash
            or planning_memory_source_ref_hash(
                tuple(
                    PlanningMemorySourceRef(
                        source_type=source.source_type,
                        source_id=source.source_id,
                        source_version=source.source_version,
                        content_hash=source.content_hash,
                        exposure=source.exposure,
                        page_number=source.page_number,
                    )
                    for source in record.source_refs
                )
            )
            != planning_memory_source_ref_hash(item.source_refs)
        ):
            return False
    return True


def _audit_matches_receipt(
    row: Mapping[str, Any] | None, receipt: PlanningMemoryEnrichmentReceipt
) -> bool:
    if row is None:
        return False
    returned = {
        (str(item["record_id"]), item["content_hash"])
        for item in row["returned_record_refs"]
    }
    expected = {(item.record_id, item.content_hash) for item in receipt.items}
    return (
        str(row["actor_id"]) == receipt.owner_actor_id
        and str(row["run_id"]) == receipt.run_id
        and row["task_id"] is None
        and row["query_hash"] == receipt.query_hash
        and row["query_fingerprint"] == receipt.query_fingerprint
        and row["grant_hash"] == receipt.owner_grant_hash
        and int(row["matter_version"]) == receipt.case_snapshot_version
        and row["scope_hash"] == receipt.retrieval_scope_hash
        and row["verified_at"] == receipt.verified_at
        and returned == expected
    )


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise PlanningMemoryBlocked(f"{label} must be a SHA-256 hash")


__all__ = (
    "CaseMemoryDisclosureAuthorizationPort",
    "CaseMemoryDisclosureRequest",
    "PostgresPlanningMemoryEnrichmentStore",
)
