"""PostgreSQL source of truth and ACL-first FTS for Agent memory.

The database remains authoritative.  ``search_vector`` is an in-database,
disposable accelerator and never decides authorization.  This adapter resolves
fresh matter roles and groups, builds an authorized record set, constrains FTS
to that exact set, then re-reads authority and records before returning hits.

No embedding provider is configured here.  ``AuthorizedVectorSearchBackend``
is the deliberately narrow future extension point: it may receive only an
already-authorized scope and opaque hashes, never an unrestricted tenant index.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Iterator, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_agent_memory import (
    AgentMemoryBlocked,
    AuthorizedRetrievalScope,
    KnowledgePublicationCandidate,
    MemoryAuthority,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
    MemorySourceRef,
    MemoryStatus,
    PublicationTarget,
    RetrievalAuthoritySnapshot,
    RunMemoryCheckpoint,
    RetrievedMemoryHit,
    SimilaritySearchCandidate,
    SourceExposure,
    SourceLocationKind,
    VerifiedRetrievalPrincipal,
    authorize_retrieval_scope,
    checkpoint_chain_hash,
    rank_authorized_candidates,
)
from .case_ledger_postgres import (
    VersionConflict,
    _authorize_and_lock_matter,
    _validate_sha256,
    _validate_uuid,
)
from .models import Actor, Role


_HUMAN_CASE_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)
_GOVERNANCE_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
_TERMINAL_STATUSES = frozenset({MemoryStatus.REVOKED, MemoryStatus.DELETED})
_SUPPORTED_PUBLIC_AUTHORITIES = frozenset(
    {
        MemoryAuthority.PRIMARY_LAW,
        MemoryAuthority.JUDICIAL_INTERPRETATION,
        MemoryAuthority.OFFICIAL_CASE,
    }
)


@dataclass(frozen=True)
class MemoryIndexDocument:
    """Trusted, bounded FTS derivative; the encrypted source remains canonical."""

    content_object_key: str
    search_document: str
    search_document_hash: str
    extractor_id: str
    extractor_version: str
    indexing_receipt_hash: str

    def __post_init__(self) -> None:
        if not self.search_document.strip() or len(self.search_document.encode("utf-8")) > 1_048_576:
            raise AgentMemoryBlocked("memory FTS document must be non-empty and at most 1 MiB")
        for value, label in (
            (self.search_document_hash, "search_document_hash"),
            (self.indexing_receipt_hash, "indexing_receipt_hash"),
        ):
            _validate_sha256(label, value)
        if sha256(self.search_document.encode("utf-8")).hexdigest() != self.search_document_hash:
            raise AgentMemoryBlocked("memory FTS document hash does not match its bytes")
        for value, label in (
            (self.extractor_id, "extractor_id"),
            (self.extractor_version, "extractor_version"),
        ):
            if not value.strip() or len(value) > 200:
                raise AgentMemoryBlocked(f"{label} is missing or too long")


@dataclass(frozen=True)
class MemoryVersionWriteReceipt:
    record_id: str
    record_version: int
    content_hash: str
    provenance_hash: str
    status: MemoryStatus


@dataclass(frozen=True)
class AuthoritativeMemorySourceBinding:
    """Binds a domain citation to the current authoritative DB receipt hash."""

    source_ref: MemorySourceRef
    source_record_hash: str

    def __post_init__(self) -> None:
        _validate_sha256("source_record_hash", self.source_record_hash)


@dataclass(frozen=True)
class KnowledgePublicationWriteReceipt:
    publication_id: str
    published_object_id: str
    approval_hash: str
    target: PublicationTarget


@dataclass(frozen=True)
class PostgresFullTextRetrieval:
    retrieval_id: str
    search_mode: str
    scope: AuthorizedRetrievalScope
    hits: tuple[RetrievedMemoryHit, ...]
    verified_at: datetime


@dataclass(frozen=True)
class RunMemoryCheckpointWriteReceipt:
    checkpoint_id: str
    run_id: str
    sequence: int
    checkpoint_hash: str
    occurred_at: datetime


class AuthorizedVectorSearchBackend(Protocol):
    """Future vector accelerator, intentionally not implemented in this slice."""

    def search_authorized(
        self,
        *,
        scope: AuthorizedRetrievalScope,
        query_text_hash: str,
        limit: int,
    ) -> tuple[SimilaritySearchCandidate, ...]: ...


class PostgresCaseAgentMemoryStore:
    """Governed memory writes and in-database ACL-first full-text retrieval."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def resolve_retrieval_principal(
        self, *, actor: Actor, matter_id: str
    ) -> VerifiedRetrievalPrincipal:
        """Resolve a short-lived principal from current database grants."""

        _validate_actor(actor)
        _validate_uuid("matter_id", matter_id)
        with self._transaction(actor.firm_id) as connection:
            authority, roles = self._load_authority(
                connection, actor=actor, matter_id=matter_id
            )
            _require_claimed_human_roles(actor, roles)
            return VerifiedRetrievalPrincipal(
                actor=actor,
                matter_id=matter_id,
                matter_access_grant_hash=authority.matter_access_grant_hash,
                matter_version=authority.matter_version,
                permission_group_ids=authority.permission_group_ids,
            )

    def append_run_checkpoint(
        self, *, actor: Actor, checkpoint: RunMemoryCheckpoint
    ) -> RunMemoryCheckpointWriteReceipt:
        """Append one exact, recoverable working-memory checkpoint.

        A dedicated SYSTEM_WORKER may persist the checkpoint, but the memory
        remains owned by the human lawyer who created the run.  The database
        run projection, open approvals and retrieval audit rows are re-read in
        the same tenant transaction; callers cannot invent a plan hash,
        unresolved question or retrieval scope.
        """

        checkpoint.__post_init__()
        _require_pure_system_worker(actor)
        if actor.firm_id != checkpoint.firm_id:
            raise AgentMemoryBlocked("checkpoint crossed the worker tenant")
        if checkpoint.unresolved_question_ids != tuple(
            sorted(checkpoint.unresolved_question_ids)
        ):
            raise AgentMemoryBlocked("checkpoint unresolved questions must be sorted")
        if checkpoint.retrieval_scope_hashes != tuple(
            sorted(checkpoint.retrieval_scope_hashes)
        ):
            raise AgentMemoryBlocked("checkpoint retrieval scopes must be sorted")

        checkpoint_id = str(
            uuid5(
                NAMESPACE_URL,
                "lawcase-agent-memory-checkpoint:"
                f"{checkpoint.firm_id}:{checkpoint.matter_id}:{checkpoint.run_id}:"
                f"{checkpoint.sequence}:{checkpoint.checkpoint_hash}",
            )
        )
        with self._transaction(actor.firm_id) as connection:
            run = _load_checkpoint_run_authority(
                connection,
                actor=actor,
                matter_id=checkpoint.matter_id,
                run_id=checkpoint.run_id,
                lock=True,
            )
            _validate_checkpoint_against_run(
                connection, checkpoint=checkpoint, run=run
            )
            replay = connection.execute(
                """
                SELECT firm_id, matter_id, owner_actor_id, run_id, sequence,
                       previous_checkpoint_hash, case_snapshot_hash, plan_hash,
                       task_state_hash, unresolved_question_ids,
                       retrieval_scope_hashes, occurred_at, checkpoint_hash
                FROM case_agent_memory_checkpoints
                WHERE checkpoint_id = %s AND firm_id = %s
                """,
                (checkpoint_id, checkpoint.firm_id),
            ).fetchone()
            if replay is not None:
                if _checkpoint_from_row(replay) != checkpoint:
                    raise AgentMemoryBlocked("checkpoint replay differs from durable state")
                return RunMemoryCheckpointWriteReceipt(
                    checkpoint_id=checkpoint_id,
                    run_id=checkpoint.run_id,
                    sequence=checkpoint.sequence,
                    checkpoint_hash=checkpoint.checkpoint_hash,
                    occurred_at=checkpoint.occurred_at,
                )
            prior = connection.execute(
                """
                SELECT sequence, checkpoint_hash
                FROM case_agent_memory_checkpoints
                WHERE firm_id = %s AND matter_id = %s AND run_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                FOR UPDATE
                """,
                (checkpoint.firm_id, checkpoint.matter_id, checkpoint.run_id),
            ).fetchone()
            expected_sequence = 1 if prior is None else int(prior["sequence"]) + 1
            expected_previous = None if prior is None else prior["checkpoint_hash"]
            if (
                checkpoint.sequence != expected_sequence
                or checkpoint.previous_checkpoint_hash != expected_previous
            ):
                raise AgentMemoryBlocked("checkpoint does not extend the current run chain")

            inserted = connection.execute(
                """
                INSERT INTO case_agent_memory_checkpoints (
                    checkpoint_id, firm_id, matter_id, owner_actor_id, run_id,
                    sequence, previous_checkpoint_hash, case_snapshot_hash,
                    plan_hash, task_state_hash, unresolved_question_ids,
                    retrieval_scope_hashes, occurred_at, checkpoint_hash
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (checkpoint_id) DO NOTHING
                RETURNING checkpoint_id
                """,
                (
                    checkpoint_id,
                    checkpoint.firm_id,
                    checkpoint.matter_id,
                    checkpoint.owner_actor_id,
                    checkpoint.run_id,
                    checkpoint.sequence,
                    checkpoint.previous_checkpoint_hash,
                    checkpoint.case_snapshot_hash,
                    checkpoint.plan_hash,
                    checkpoint.task_state_hash,
                    list(checkpoint.unresolved_question_ids),
                    list(checkpoint.retrieval_scope_hashes),
                    checkpoint.occurred_at,
                    checkpoint.checkpoint_hash,
                ),
            ).fetchone()
            if inserted is None:
                concurrent_replay = connection.execute(
                    """
                    SELECT firm_id, matter_id, owner_actor_id, run_id, sequence,
                           previous_checkpoint_hash, case_snapshot_hash, plan_hash,
                           task_state_hash, unresolved_question_ids,
                           retrieval_scope_hashes, occurred_at, checkpoint_hash
                    FROM case_agent_memory_checkpoints
                    WHERE checkpoint_id = %s AND firm_id = %s
                    """,
                    (checkpoint_id, checkpoint.firm_id),
                ).fetchone()
                if concurrent_replay is None or _checkpoint_from_row(concurrent_replay) != checkpoint:
                    raise AgentMemoryBlocked("checkpoint replay differs from durable state")
            return RunMemoryCheckpointWriteReceipt(
                checkpoint_id=checkpoint_id,
                run_id=checkpoint.run_id,
                sequence=checkpoint.sequence,
                checkpoint_hash=checkpoint.checkpoint_hash,
                occurred_at=checkpoint.occurred_at,
            )

    def checkpoint_current_run(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        occurred_at: datetime,
    ) -> RunMemoryCheckpointWriteReceipt:
        """Build a checkpoint exclusively from current durable server state."""

        _require_pure_system_worker(actor)
        _validate_uuid("matter_id", matter_id)
        _validate_uuid("run_id", run_id)
        if occurred_at.tzinfo is None:
            raise AgentMemoryBlocked("checkpoint time must include a timezone")
        with self._transaction(actor.firm_id) as connection:
            run = _load_checkpoint_run_authority(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                lock=False,
            )
            if run["current_graph_hash"] is None:
                raise AgentMemoryBlocked("an unplanned run cannot create working memory")
            prior = connection.execute(
                """
                SELECT sequence, checkpoint_hash
                FROM case_agent_memory_checkpoints
                WHERE firm_id = %s AND matter_id = %s AND run_id = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (actor.firm_id, matter_id, run_id),
            ).fetchone()
            question_rows = connection.execute(
                """
                SELECT task_id
                FROM case_agent_task_heads
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND is_current = true AND status = 'WAITING_APPROVAL'
                ORDER BY task_id
                """,
                (run_id, actor.firm_id, matter_id),
            ).fetchall()
            retrieval_rows = connection.execute(
                """
                SELECT DISTINCT scope_hash
                FROM case_agent_memory_retrieval_audits
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND actor_id = %s
                ORDER BY scope_hash
                """,
                (run_id, actor.firm_id, matter_id, run["created_by"]),
            ).fetchall()
        checkpoint = RunMemoryCheckpoint.build(
            firm_id=actor.firm_id,
            matter_id=matter_id,
            owner_actor_id=str(run["created_by"]),
            run_id=run_id,
            sequence=1 if prior is None else int(prior["sequence"]) + 1,
            previous_checkpoint_hash=(
                None if prior is None else prior["checkpoint_hash"]
            ),
            case_snapshot_hash=run["snapshot_hash"],
            plan_hash=run["current_graph_hash"],
            task_state_hash=run["projection_hash"],
            unresolved_question_ids=tuple(
                str(row["task_id"]) for row in question_rows
            ),
            retrieval_scope_hashes=tuple(
                row["scope_hash"] for row in retrieval_rows
            ),
            occurred_at=occurred_at,
        )
        return self.append_run_checkpoint(actor=actor, checkpoint=checkpoint)

    def get_latest_run_checkpoint(
        self, *, actor: Actor, matter_id: str, run_id: str
    ) -> RunMemoryCheckpoint | None:
        """Return the latest checkpoint only after verifying its whole chain."""

        _require_pure_system_worker(actor)
        _validate_uuid("matter_id", matter_id)
        _validate_uuid("run_id", run_id)
        with self._transaction(actor.firm_id) as connection:
            run = _load_checkpoint_run_authority(
                connection,
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                lock=False,
            )
            rows = connection.execute(
                """
                SELECT firm_id, matter_id, owner_actor_id, run_id, sequence,
                       previous_checkpoint_hash, case_snapshot_hash, plan_hash,
                       task_state_hash, unresolved_question_ids,
                       retrieval_scope_hashes, occurred_at, checkpoint_hash
                FROM case_agent_memory_checkpoints
                WHERE firm_id = %s AND matter_id = %s AND run_id = %s
                ORDER BY sequence
                LIMIT 10001
                """,
                (actor.firm_id, matter_id, run_id),
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 10_000:
                raise AgentMemoryBlocked("checkpoint chain exceeds the bounded verifier")
            checkpoints = tuple(_checkpoint_from_row(row) for row in rows)
            checkpoint_chain_hash(checkpoints)
            _validate_checkpoint_against_run(
                connection, checkpoint=checkpoints[-1], run=run
            )
            return checkpoints[-1]

    def retrieve_full_text(
        self,
        *,
        actor: Actor,
        query: MemoryQuery,
        execution_run_id: str | None = None,
        execution_task_id: str | None = None,
    ) -> PostgresFullTextRetrieval:
        """Search only an ACL-authorized set and re-authorize before return."""

        _validate_actor(actor)
        _validate_query_actor(actor, query)
        if execution_run_id is not None:
            _validate_uuid("execution_run_id", execution_run_id)
        if execution_task_id is not None:
            _validate_uuid("execution_task_id", execution_task_id)
            if execution_run_id is None:
                raise AgentMemoryBlocked("an execution task requires its Agent run")
        if MemoryLayer.RUN_WORKING in query.access.layers and (
            execution_run_id != query.access.run_id
            or (
                execution_task_id is not None
                and query.access.task_id not in {None, execution_task_id}
            )
        ):
            raise AgentMemoryBlocked(
                "working-memory retrieval must bind the executing run and task"
            )
        with self._transaction(actor.firm_id) as connection:
            if execution_run_id is not None:
                _validate_execution_run_scope(
                    connection,
                    actor=actor,
                    matter_id=query.access.matter_id,
                    run_id=execution_run_id,
                    task_id=execution_task_id,
                )
            authority, roles = self._load_authority(
                connection, actor=actor, matter_id=query.access.matter_id
            )
            _require_claimed_human_roles(actor, roles)
            records = self._load_authoritative_records(
                connection, query=query, authority=authority
            )
            scope = authorize_retrieval_scope(
                query=query,
                authority=authority,
                authoritative_records=records,
                authorized_at=authority.checked_at,
            )
            candidates = self._search_fts(
                connection, query=query, scope=scope
            )

            # READ COMMITTED is intentional: the second statements can observe
            # a denial, tombstone, role revoke, group change or head change that
            # committed while FTS was running.
            fresh_authority, fresh_roles = self._load_authority(
                connection, actor=actor, matter_id=query.access.matter_id
            )
            _require_claimed_human_roles(actor, fresh_roles)
            if fresh_authority.matter_access_grant_hash != authority.matter_access_grant_hash:
                raise AgentMemoryBlocked("memory authority changed during retrieval")
            fresh_records = self._load_authoritative_records(
                connection, query=query, authority=fresh_authority
            )
            hits = rank_authorized_candidates(
                query,
                scope,
                candidates,
                authority=fresh_authority,
                authoritative_records={row.record_id: row for row in fresh_records},
                verified_at=fresh_authority.checked_at,
            )
            retrieval_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO case_agent_memory_retrieval_audits (
                    retrieval_id, firm_id, matter_id, actor_id, run_id, task_id,
                    query_hash,
                    query_fingerprint, grant_hash, matter_version, scope_hash,
                    authorized_record_refs, returned_record_refs, search_mode,
                    authorized_at, verified_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, 'POSTGRES_FTS_V1', %s, %s
                )
                """,
                (
                    retrieval_id,
                    actor.firm_id,
                    query.access.matter_id,
                    actor.actor_id,
                    execution_run_id,
                    execution_task_id,
                    sha256(query.query_text.encode("utf-8")).hexdigest(),
                    scope.query_fingerprint,
                    fresh_authority.matter_access_grant_hash,
                    fresh_authority.matter_version,
                    scope.scope_hash,
                    Jsonb(
                        [
                            {
                                "record_id": row.record_id,
                                "record_version": row.record_version,
                                "content_hash": row.content_hash,
                                "provenance_hash": row.provenance_hash,
                            }
                            for row in scope.authorized_records
                        ]
                    ),
                    Jsonb(
                        [
                            {"record_id": hit.record_id, "content_hash": hit.content_hash}
                            for hit in hits
                        ]
                    ),
                    scope.authorized_at,
                    fresh_authority.checked_at,
                ),
            )
            return PostgresFullTextRetrieval(
                retrieval_id=retrieval_id,
                search_mode="POSTGRES_FTS_V1",
                scope=scope,
                hits=hits,
                verified_at=fresh_authority.checked_at,
            )

    def register_publication(
        self,
        *,
        actor: Actor,
        candidate: KnowledgePublicationCandidate,
        expected_source_matter_version: int,
        storage_object_key: str,
        byte_size: int,
        media_type: str,
        sanitization_manifest_hash: str,
        first_review_hash: str,
        second_review_hash: str,
    ) -> KnowledgePublicationWriteReceipt:
        """Register a sanitized cross-case object after two human reviews."""

        _validate_actor(actor)
        _require_no_worker(actor)
        if actor.actor_id != candidate.approved_by or actor.firm_id != candidate.source_firm_id:
            raise AgentMemoryBlocked("publication command is not bound to its first approver")
        if candidate.second_approver_id is None:
            raise AgentMemoryBlocked("all cross-case publication requires two reviewers")
        if candidate.second_approver_id in {candidate.approved_by, candidate.owner_actor_id}:
            raise AgentMemoryBlocked("publication reviewers must be independent")
        _validate_sha256("sanitization_manifest_hash", sanitization_manifest_hash)
        _validate_sha256("first_review_hash", first_review_hash)
        _validate_sha256("second_review_hash", second_review_hash)
        if first_review_hash == second_review_hash:
            raise AgentMemoryBlocked("publication reviews must be two distinct receipts")
        _validate_object_key(storage_object_key, candidate.published_source_object_hash)
        if byte_size < 1:
            raise AgentMemoryBlocked("published object byte size must be positive")
        if not media_type.strip() or len(media_type) > 200:
            raise AgentMemoryBlocked("published object media type is invalid")

        with self._transaction(actor.firm_id) as connection:
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=candidate.source_matter_id,
                expected_version=expected_source_matter_version,
                allowed_roles=_GOVERNANCE_ROLES,
            )
            second = connection.execute(
                """
                SELECT 1
                FROM matter_actor_roles role
                JOIN users reviewer
                  ON reviewer.user_id = role.user_id AND reviewer.firm_id = role.firm_id
                WHERE role.matter_id = %s AND role.firm_id = %s
                  AND role.user_id = %s AND role.revoked_at IS NULL
                  AND role.role = ANY(%s) AND reviewer.status = 'ACTIVE'
                LIMIT 1
                """,
                (
                    candidate.source_matter_id,
                    actor.firm_id,
                    candidate.second_approver_id,
                    [role.value for role in _GOVERNANCE_ROLES],
                ),
            ).fetchone()
            if second is None:
                raise PermissionError("second publication reviewer lacks a current governance role")
            review_rows = connection.execute(
                """
                SELECT reviewer_id, review_order, review_hash
                FROM case_agent_knowledge_publication_reviews
                WHERE publication_id = %s AND firm_id = %s
                  AND source_matter_id = %s
                  AND candidate_approval_hash = %s
                  AND decision = 'APPROVED'
                ORDER BY review_order
                """,
                (
                    candidate.publication_id,
                    actor.firm_id,
                    candidate.source_matter_id,
                    candidate.approval_hash,
                ),
            ).fetchall()
            expected_reviews = {
                (candidate.approved_by, "FIRST", first_review_hash),
                (candidate.second_approver_id, "SECOND", second_review_hash),
            }
            actual_reviews = {
                (str(row["reviewer_id"]), row["review_order"], row["review_hash"])
                for row in review_rows
            }
            if actual_reviews != expected_reviews:
                raise AgentMemoryBlocked(
                    "publication requires two independent durable review receipts"
                )
            connection.execute(
                """
                INSERT INTO case_agent_published_knowledge_objects (
                    published_object_id, firm_id, content_sha256, object_sha256,
                    storage_object_key, sanitization_manifest_hash, byte_size, media_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate.published_source_object_id,
                    actor.firm_id,
                    candidate.published_content_hash,
                    candidate.published_source_object_hash,
                    storage_object_key,
                    sanitization_manifest_hash,
                    byte_size,
                    media_type,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_knowledge_publications (
                    publication_id, firm_id, target, source_matter_id,
                    published_object_id, published_content_hash,
                    published_source_object_hash, provenance_hash, owner_actor_id,
                    source_object_hashes, anonymization_review, conflict_review,
                    confidentiality_review, first_approved_by, second_approved_by,
                    first_review_hash, second_review_hash,
                    approved_permission_group_ids,
                    publication_policy_version, publication_policy_hash,
                    approved_at, approval_hash
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'PASSED', 'PASSED', 'PASSED', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    candidate.publication_id,
                    actor.firm_id,
                    candidate.target.value,
                    candidate.source_matter_id,
                    candidate.published_source_object_id,
                    candidate.published_content_hash,
                    candidate.published_source_object_hash,
                    candidate.provenance_hash,
                    candidate.owner_actor_id,
                    list(candidate.source_object_hashes),
                    candidate.approved_by,
                    candidate.second_approver_id,
                    first_review_hash,
                    second_review_hash,
                    list(candidate.permission_group_ids),
                    candidate.publication_policy_version,
                    candidate.publication_policy_hash,
                    candidate.approved_at,
                    candidate.approval_hash,
                ),
            )
            for group_id in candidate.permission_group_ids:
                result = connection.execute(
                    """
                    INSERT INTO case_agent_knowledge_publication_groups (
                        publication_id, group_id, firm_id
                    )
                    SELECT %s, permission_group.group_id, permission_group.firm_id
                    FROM case_agent_memory_permission_groups permission_group
                    WHERE permission_group.group_id = %s
                      AND permission_group.firm_id = %s
                      AND permission_group.scope_kind = 'FIRM'
                    """,
                    (candidate.publication_id, group_id, actor.firm_id),
                )
                if result.rowcount != 1:
                    raise AgentMemoryBlocked("publication group is not a current firm group")
            return KnowledgePublicationWriteReceipt(
                publication_id=candidate.publication_id,
                published_object_id=candidate.published_source_object_id,
                approval_hash=candidate.approval_hash,
                target=candidate.target,
            )

    def append_memory_version(
        self,
        *,
        actor: Actor,
        authority_matter_id: str,
        expected_matter_version: int,
        expected_record_version: int,
        record: MemoryRecord,
        source_bindings: tuple[AuthoritativeMemorySourceBinding, ...],
        index_document: MemoryIndexDocument,
        publication_id: str | None = None,
        terminal_reason_code: str | None = None,
    ) -> MemoryVersionWriteReceipt:
        """Append one immutable version and advance only its guarded head."""

        _validate_actor(actor)
        _validate_uuid("authority_matter_id", authority_matter_id)
        if record.record_version != expected_record_version + 1:
            raise VersionConflict("memory record version is not the expected next version")
        _validate_object_key(index_document.content_object_key, record.content_hash)
        if not source_bindings or len(source_bindings) != len(record.source_refs):
            raise AgentMemoryBlocked(
                "authoritative source bindings must exactly match the memory citations"
            )
        expected_sources = {
            (source.source_type, source.source_id, source.source_version, source.content_hash)
            for source in record.source_refs
        }
        bound_sources = {
            (
                binding.source_ref.source_type,
                binding.source_ref.source_id,
                binding.source_ref.source_version,
                binding.source_ref.content_hash,
            )
            for binding in source_bindings
        }
        if bound_sources != expected_sources or len(bound_sources) != len(source_bindings):
            raise AgentMemoryBlocked(
                "authoritative source bindings must exactly match the memory citations"
            )
        allowed_roles = self._allowed_write_roles(record.layer, actor)
        if record.layer is MemoryLayer.PUBLIC_LEGAL and record.authority not in _SUPPORTED_PUBLIC_AUTHORITIES:
            raise AgentMemoryBlocked("research leads cannot be promoted into public legal memory")
        if record.layer in {MemoryLayer.RUN_WORKING, MemoryLayer.CASE_LONG_TERM}:
            if record.matter_id != authority_matter_id:
                raise AgentMemoryBlocked("same-case memory crossed its authority matter")
        elif record.matter_id is not None:
            raise AgentMemoryBlocked("cross-case/public memory cannot retain a matter identifier")
        if record.firm_id is not None and record.firm_id != actor.firm_id:
            raise AgentMemoryBlocked("memory crossed the actor tenant")
        if record.layer in {MemoryLayer.LAWYER_PERSONAL, MemoryLayer.FIRM_KNOWLEDGE}:
            _validate_uuid("publication_id", publication_id)
        elif publication_id is not None:
            raise AgentMemoryBlocked("only published cross-case knowledge has a publication id")
        if record.status in _TERMINAL_STATUSES:
            if not terminal_reason_code or len(terminal_reason_code) > 100:
                raise AgentMemoryBlocked("terminal memory requires a bounded reason code")
        elif terminal_reason_code is not None:
            raise AgentMemoryBlocked("a live memory version cannot carry a terminal reason")
        if record.layer is MemoryLayer.RUN_WORKING:
            if record.owner_actor_id != actor.actor_id:
                raise AgentMemoryBlocked("working memory writer must be the owning actor")
            if Role.SYSTEM_WORKER in actor.roles:
                raise PermissionError(
                    "SYSTEM_WORKER cannot own lawyer working memory; it may only return task artifacts"
                )

        with self._transaction(actor.firm_id) as connection:
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=authority_matter_id,
                expected_version=expected_matter_version,
                allowed_roles=allowed_roles,
            )
            if record.layer is MemoryLayer.PUBLIC_LEGAL:
                registration = connection.execute(
                    """
                    SELECT registration_hash
                    FROM case_agent_public_legal_authority_registrations
                    WHERE registration_hash = %s AND firm_id = %s
                      AND authority = %s
                      AND effective_from = %s
                      AND effective_to IS NOT DISTINCT FROM %s
                      AND publication_approval_hash = %s
                    """,
                    (
                        record.source_authority_registry_hash,
                        actor.firm_id,
                        record.authority.value,
                        record.effective_from,
                        record.effective_to,
                        record.publication_approval_hash,
                    ),
                ).fetchone()
                if registration is None:
                    raise AgentMemoryBlocked(
                        "public legal memory is not bound to an approved authority registration"
                    )
                registered_source_rows = connection.execute(
                    """
                    SELECT authority_source.snapshot_id,
                           authority_source.snapshot_content_sha256,
                           authority_source.snapshot_verification_hash,
                           snapshot.official_url
                    FROM case_agent_public_legal_authority_registrations registration
                    JOIN case_agent_public_legal_authority_sources authority_source
                      ON authority_source.registration_id = registration.registration_id
                     AND authority_source.firm_id = registration.firm_id
                    JOIN official_legal_source_snapshots snapshot
                      ON snapshot.snapshot_id = authority_source.snapshot_id
                     AND snapshot.firm_id = authority_source.firm_id
                     AND snapshot.content_sha256 = authority_source.snapshot_content_sha256
                    WHERE registration.registration_hash = %s
                      AND registration.firm_id = %s
                      AND snapshot.verification_status = 'VERIFIED'
                      AND snapshot.license_status = 'ACTIVE'
                      AND snapshot.authority_level = registration.authority
                    """,
                    (record.source_authority_registry_hash, actor.firm_id),
                ).fetchall()
                registered_sources = {
                    (
                        str(row["snapshot_id"]),
                        row["snapshot_content_sha256"],
                        row["snapshot_verification_hash"],
                        row["official_url"],
                    )
                    for row in registered_source_rows
                }
                supplied_sources = {
                    (
                        binding.source_ref.source_id,
                        binding.source_ref.content_hash,
                        binding.source_record_hash,
                        binding.source_ref.source_url,
                    )
                    for binding in source_bindings
                }
                if supplied_sources != registered_sources:
                    raise AgentMemoryBlocked(
                        "public legal sources differ from the approved authority registration"
                    )
            publication_row = None
            publication_group_ids: tuple[str, ...] = ()
            if publication_id is not None:
                publication_row = connection.execute(
                    """
                    SELECT publication_id, target, source_matter_id, owner_actor_id,
                           published_content_hash, published_source_object_hash,
                           provenance_hash, approval_hash
                    FROM case_agent_knowledge_publications
                    WHERE publication_id = %s AND firm_id = %s
                    """,
                    (publication_id, actor.firm_id),
                ).fetchone()
                publication_group_rows = connection.execute(
                    """
                    SELECT publication_group.group_id
                    FROM case_agent_knowledge_publication_groups publication_group
                    WHERE publication_group.publication_id = %s
                      AND publication_group.firm_id = %s
                    ORDER BY publication_group.group_id
                    """,
                    (publication_id, actor.firm_id),
                ).fetchall()
                publication_group_ids = tuple(
                    str(row["group_id"]) for row in publication_group_rows
                )
                self._validate_publication_binding(
                    record=record,
                    authority_matter_id=authority_matter_id,
                    publication_id=publication_id,
                    publication_row=publication_row,
                    publication_group_ids=publication_group_ids,
                )

            head = connection.execute(
                """
                SELECT current_version, current_status
                FROM case_agent_memory_record_heads
                WHERE record_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (record.record_id, actor.firm_id),
            ).fetchone()
            actual_version = int(head["current_version"]) if head is not None else 0
            if actual_version != expected_record_version:
                raise VersionConflict(
                    f"expected memory version {expected_record_version}, current version is {actual_version}"
                )
            if head is not None and head["current_status"] in {
                MemoryStatus.REVOKED.value,
                MemoryStatus.DELETED.value,
            }:
                raise AgentMemoryBlocked("terminal memory cannot be resurrected")

            if record.status in _TERMINAL_STATUSES:
                connection.execute(
                    """
                    INSERT INTO case_agent_memory_tombstones (
                        tombstone_id, record_id, blocked_through_version, firm_id,
                        terminal_status, reason_code, denied_by, denied_at,
                        tombstone_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid4()),
                        record.record_id,
                        record.record_version,
                        actor.firm_id,
                        record.status.value,
                        terminal_reason_code,
                        actor.actor_id,
                        record.updated_at,
                        _terminal_hash(record, actor.actor_id, terminal_reason_code or ""),
                    ),
                )

            connection.execute(
                """
                INSERT INTO case_agent_memory_record_versions (
                    record_id, record_version, firm_id, semantic_firm_id,
                    layer, status, authority, content_sha256, content_object_key,
                    search_document, search_document_hash, extractor_id,
                    extractor_version, indexing_receipt_hash,
                    governance_matter_id, matter_id,
                    owner_actor_id, run_id, task_id, case_type_codes,
                    procedure_stages, issue_tags, effective_from, effective_to,
                    known_from, known_to, publication_id,
                    publication_approval_hash, source_authority_registry_hash,
                    provenance_hash, written_by, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    record.record_id,
                    record.record_version,
                    actor.firm_id,
                    record.firm_id,
                    record.layer.value,
                    record.status.value,
                    record.authority.value,
                    record.content_hash,
                    index_document.content_object_key,
                    index_document.search_document,
                    index_document.search_document_hash,
                    index_document.extractor_id,
                    index_document.extractor_version,
                    index_document.indexing_receipt_hash,
                    authority_matter_id,
                    record.matter_id,
                    record.owner_actor_id,
                    record.run_id,
                    record.task_id,
                    list(record.case_type_codes),
                    list(record.procedure_stages),
                    list(record.issue_tags),
                    record.effective_from,
                    record.effective_to,
                    record.known_from,
                    record.known_to,
                    publication_id,
                    record.publication_approval_hash,
                    record.source_authority_registry_hash,
                    record.provenance_hash,
                    actor.actor_id,
                    record.updated_at,
                ),
            )
            for group_id in record.permission_group_ids:
                connection.execute(
                    """
                    INSERT INTO case_agent_memory_record_groups (
                        record_id, record_version, group_id, firm_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (record.record_id, record.record_version, group_id, actor.firm_id),
                )
            for binding in source_bindings:
                self._insert_source_ref(
                    connection,
                    firm_id=actor.firm_id,
                    record=record,
                    binding=binding,
                )
            if head is None:
                if expected_record_version != 0:
                    raise VersionConflict("memory head is missing")
                connection.execute(
                    """
                    INSERT INTO case_agent_memory_record_heads (
                        record_id, firm_id, current_version, current_status,
                        current_content_sha256, current_provenance_hash, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.record_id,
                        actor.firm_id,
                        record.record_version,
                        record.status.value,
                        record.content_hash,
                        record.provenance_hash,
                        record.updated_at,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE case_agent_memory_record_heads
                    SET current_version = %s, current_status = %s,
                        current_content_sha256 = %s, current_provenance_hash = %s,
                        updated_at = %s
                    WHERE record_id = %s AND firm_id = %s AND current_version = %s
                    """,
                    (
                        record.record_version,
                        record.status.value,
                        record.content_hash,
                        record.provenance_hash,
                        record.updated_at,
                        record.record_id,
                        actor.firm_id,
                        expected_record_version,
                    ),
                )
            return MemoryVersionWriteReceipt(
                record_id=record.record_id,
                record_version=record.record_version,
                content_hash=record.content_hash,
                provenance_hash=record.provenance_hash,
                status=record.status,
            )

    def _load_authority(
        self, connection: Any, *, actor: Actor, matter_id: str
    ) -> tuple[RetrievalAuthoritySnapshot, frozenset[Role]]:
        row = connection.execute(
            """
            SELECT matter.version, clock_timestamp() AS checked_at,
                   COALESCE(
                       array_agg(DISTINCT role.role)
                           FILTER (WHERE role.role IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS active_roles
                   , COALESCE(
                       array_agg(DISTINCT role.role || ':' || role.granted_at::text)
                           FILTER (WHERE role.role IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS active_role_grants
            FROM matters matter
            JOIN users actor_row
              ON actor_row.user_id = %s AND actor_row.firm_id = matter.firm_id
             AND actor_row.status = 'ACTIVE'
            LEFT JOIN matter_actor_roles role
              ON role.matter_id = matter.matter_id AND role.firm_id = matter.firm_id
             AND role.user_id = actor_row.user_id AND role.revoked_at IS NULL
            WHERE matter.matter_id = %s AND matter.firm_id = %s
            GROUP BY matter.version
            """,
            (actor.actor_id, matter_id, actor.firm_id),
        ).fetchone()
        if row is None:
            raise PermissionError("actor has no active identity for this matter")
        roles = frozenset(Role(value) for value in row["active_roles"])
        if not roles.intersection(_HUMAN_CASE_ROLES):
            raise PermissionError("actor lacks an active human case role")
        if Role.SYSTEM_WORKER in roles:
            raise PermissionError("SYSTEM_WORKER identity cannot retrieve human case memory")
        group_rows = connection.execute(
            """
            SELECT DISTINCT permission_group.group_id,
                            membership.grant_hash,
                            permission_group.policy_hash
            FROM case_agent_memory_group_memberships membership
            JOIN case_agent_memory_permission_groups permission_group
              ON permission_group.group_id = membership.group_id
             AND permission_group.firm_id = membership.firm_id
            WHERE membership.firm_id = %s AND membership.member_actor_id = %s
              AND (permission_group.scope_kind = 'FIRM'
                   OR permission_group.matter_id = %s)
              AND NOT EXISTS (
                  SELECT 1 FROM case_agent_memory_group_member_denials denial
                  WHERE denial.membership_id = membership.membership_id
                    AND denial.firm_id = membership.firm_id
              )
            ORDER BY permission_group.group_id
            """,
            (actor.firm_id, actor.actor_id, matter_id),
        ).fetchall()
        groups = tuple(str(value["group_id"]) for value in group_rows)
        role_grants = tuple(sorted(str(value) for value in row["active_role_grants"]))
        group_grants = tuple(
            sorted(
                f"{value['group_id']}:{value['grant_hash']}:{value['policy_hash']}"
                for value in group_rows
            )
        )
        grant_hash = _matter_grant_hash(
            actor=actor,
            matter_id=matter_id,
            matter_version=int(row["version"]),
            roles=roles,
            group_ids=groups,
            role_grants=role_grants,
            group_grants=group_grants,
        )
        return (
            RetrievalAuthoritySnapshot(
                actor_id=actor.actor_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                matter_access_grant_hash=grant_hash,
                matter_version=int(row["version"]),
                permission_group_ids=groups,
                access_active=True,
                checked_at=row["checked_at"],
            ),
            roles,
        )

    def _load_authoritative_records(
        self,
        connection: Any,
        *,
        query: MemoryQuery,
        authority: RetrievalAuthoritySnapshot,
    ) -> tuple[MemoryRecord, ...]:
        access = query.access
        rows = connection.execute(
            """
            SELECT version.*
            FROM case_agent_memory_record_heads head
            JOIN case_agent_memory_record_versions version
              ON version.record_id = head.record_id
             AND version.record_version = head.current_version
             AND version.firm_id = head.firm_id
            WHERE head.firm_id = %s
              AND version.layer = ANY(%s)
              AND version.status NOT IN ('SUPERSEDED', 'REVOKED', 'DELETED')
              AND (version.status <> 'CANDIDATE' OR version.layer = 'RUN_WORKING')
              AND version.known_from <= %s
              AND (version.known_to IS NULL OR version.known_to > %s)
              AND (
                  version.layer <> 'PUBLIC_LEGAL'
                  OR (
                      EXISTS (
                          SELECT 1
                          FROM case_agent_memory_source_refs public_source
                          WHERE public_source.record_id = version.record_id
                            AND public_source.record_version = version.record_version
                            AND public_source.firm_id = version.firm_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM case_agent_memory_source_refs public_source
                          WHERE public_source.record_id = version.record_id
                            AND public_source.record_version = version.record_version
                            AND public_source.firm_id = version.firm_id
                            AND NOT EXISTS (
                                SELECT 1
                                FROM case_agent_public_legal_authority_registrations registration
                                JOIN case_agent_public_legal_authority_sources authority_source
                                  ON authority_source.registration_id = registration.registration_id
                                 AND authority_source.firm_id = registration.firm_id
                                JOIN official_legal_source_snapshots snapshot
                                  ON snapshot.snapshot_id = authority_source.snapshot_id
                                 AND snapshot.firm_id = authority_source.firm_id
                                 AND snapshot.content_sha256 = authority_source.snapshot_content_sha256
                                WHERE registration.registration_hash = version.source_authority_registry_hash
                                  AND registration.firm_id = version.firm_id
                                  AND registration.authority = version.authority
                                  AND registration.effective_from = version.effective_from
                                  AND registration.effective_to IS NOT DISTINCT FROM version.effective_to
                                  AND registration.publication_approval_hash = version.publication_approval_hash
                                  AND authority_source.snapshot_id = public_source.source_id
                                  AND authority_source.snapshot_content_sha256 = public_source.content_sha256
                                  AND authority_source.snapshot_verification_hash = public_source.source_record_hash
                                  AND snapshot.verification_status = 'VERIFIED'
                                  AND snapshot.license_status = 'ACTIVE'
                                  AND snapshot.authority_level = version.authority
                                  AND snapshot.official_url = public_source.source_url
                            )
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM case_agent_memory_tombstones tombstone
                  WHERE tombstone.record_id = version.record_id
                    AND tombstone.firm_id = version.firm_id
                    AND tombstone.blocked_through_version >= version.record_version
              )
              AND NOT EXISTS (
                  SELECT 1 FROM case_agent_memory_access_denials denial
                  WHERE denial.record_id = version.record_id
                    AND denial.firm_id = version.firm_id
                    AND (
                        (denial.subject_kind = 'ACTOR' AND denial.subject_id = %s)
                        OR (denial.subject_kind = 'GROUP'
                            AND denial.subject_id = ANY(%s::uuid[]))
                    )
              )
              AND (
                  (version.layer = 'RUN_WORKING'
                      AND version.matter_id = %s AND version.owner_actor_id = %s
                      AND version.run_id = %s
                      AND (%s::uuid IS NULL OR version.task_id IS NULL
                           OR version.task_id = %s))
                  OR (version.layer = 'CASE_LONG_TERM'
                      AND version.matter_id = %s
                      AND EXISTS (
                          SELECT 1 FROM case_agent_memory_record_groups record_group
                          WHERE record_group.record_id = version.record_id
                            AND record_group.record_version = version.record_version
                            AND record_group.group_id = ANY(%s::uuid[])
                      ))
                  OR (version.layer = 'LAWYER_PERSONAL'
                      AND version.owner_actor_id = %s)
                  OR (version.layer = 'FIRM_KNOWLEDGE'
                      AND EXISTS (
                          SELECT 1 FROM case_agent_memory_record_groups record_group
                          WHERE record_group.record_id = version.record_id
                            AND record_group.record_version = version.record_version
                            AND record_group.group_id = ANY(%s::uuid[])
                      ))
                  OR (version.layer = 'PUBLIC_LEGAL'
                      AND version.effective_from <= %s
                      AND (version.effective_to IS NULL OR version.effective_to >= %s))
              )
              AND (%s <> 'HARD' OR cardinality(%s::text[]) = 0
                   OR version.case_type_codes && %s::text[])
              AND (%s <> 'HARD' OR cardinality(%s::text[]) = 0
                   OR version.procedure_stages && %s::text[])
              AND (%s <> 'HARD' OR cardinality(%s::text[]) = 0
                   OR version.issue_tags && %s::text[])
            ORDER BY version.record_id
            """,
            (
                access.firm_id,
                [layer.value for layer in access.layers],
                access.knowledge_as_of,
                access.knowledge_as_of,
                access.actor_id,
                list(authority.permission_group_ids),
                access.matter_id,
                access.actor_id,
                access.run_id,
                access.task_id,
                access.task_id,
                access.matter_id,
                list(authority.permission_group_ids),
                access.actor_id,
                list(authority.permission_group_ids),
                access.legal_period_end,
                access.legal_period_start,
                access.case_type_mode.value,
                list(access.case_type_codes),
                list(access.case_type_codes),
                access.procedure_stage_mode.value,
                list(access.procedure_stages),
                list(access.procedure_stages),
                access.issue_tag_mode.value,
                list(access.issue_tags),
                list(access.issue_tags),
            ),
        ).fetchall()
        if not rows:
            return ()
        ids = [str(row["record_id"]) for row in rows]
        group_rows = connection.execute(
            """
            SELECT record_id, record_version, group_id
            FROM case_agent_memory_record_groups
            WHERE firm_id = %s AND record_id = ANY(%s::uuid[])
            ORDER BY record_id, group_id
            """,
            (access.firm_id, ids),
        ).fetchall()
        source_rows = connection.execute(
            """
            SELECT * FROM case_agent_memory_source_refs
            WHERE firm_id = %s AND record_id = ANY(%s::uuid[])
            ORDER BY record_id, record_version, source_ref_id
            """,
            (access.firm_id, ids),
        ).fetchall()
        groups: dict[tuple[str, int], list[str]] = {}
        for row in group_rows:
            groups.setdefault((str(row["record_id"]), int(row["record_version"])), []).append(
                str(row["group_id"])
            )
        sources: dict[tuple[str, int], list[MemorySourceRef]] = {}
        for row in source_rows:
            sources.setdefault((str(row["record_id"]), int(row["record_version"])), []).append(
                _source_from_row(row)
            )
        return tuple(
            _record_from_row(row, permission_group_ids=group_values, source_refs=source_values)
            for row in rows
            for group_values in (
                tuple(groups.get((str(row["record_id"]), int(row["record_version"])), ())),
            )
            for source_values in (
                tuple(sources.get((str(row["record_id"]), int(row["record_version"])), ())),
            )
            if source_values
        )

    def _search_fts(
        self,
        connection: Any,
        *,
        query: MemoryQuery,
        scope: AuthorizedRetrievalScope,
    ) -> tuple[SimilaritySearchCandidate, ...]:
        ids = [row.record_id for row in scope.authorized_records]
        versions = [row.record_version for row in scope.authorized_records]
        hashes = [row.content_hash for row in scope.authorized_records]
        rows = connection.execute(
            """
            WITH authorized AS (
                SELECT * FROM unnest(
                    %s::uuid[], %s::integer[], %s::text[]
                ) AS item(record_id, record_version, content_sha256)
            ), search_query AS (
                SELECT websearch_to_tsquery('simple', %s) AS value
            )
            SELECT version.record_id, version.record_version,
                   version.content_sha256,
                   LEAST(1.0, GREATEST(0.0,
                       ts_rank_cd(version.search_vector, search_query.value)
                   ))::float8 AS lexical_score
            FROM authorized
            JOIN case_agent_memory_record_versions version
              ON version.record_id = authorized.record_id
             AND version.record_version = authorized.record_version
             AND version.content_sha256 = authorized.content_sha256
            CROSS JOIN search_query
            WHERE version.firm_id = %s
              AND version.search_vector @@ search_query.value
            ORDER BY lexical_score DESC, version.record_id
            LIMIT %s
            """,
            (
                ids,
                versions,
                hashes,
                query.query_text,
                query.access.firm_id,
                min(100, max(query.max_results, query.max_results * 3)),
            ),
        ).fetchall()
        return tuple(
            SimilaritySearchCandidate(
                record_id=str(row["record_id"]),
                record_version=int(row["record_version"]),
                content_hash=row["content_sha256"],
                lexical_score=float(row["lexical_score"]),
                vector_score=0.0,
            )
            for row in rows
        )

    @staticmethod
    def _allowed_write_roles(layer: MemoryLayer, actor: Actor) -> frozenset[Role]:
        if layer is MemoryLayer.RUN_WORKING:
            if Role.SYSTEM_WORKER in actor.roles:
                raise PermissionError("SYSTEM_WORKER cannot write lawyer memory directly")
            return _HUMAN_CASE_ROLES
        _require_no_worker(actor)
        return _GOVERNANCE_ROLES

    @staticmethod
    def _validate_publication_binding(
        *,
        record: MemoryRecord,
        authority_matter_id: str,
        publication_id: str,
        publication_row: Mapping[str, Any] | None,
        publication_group_ids: tuple[str, ...],
    ) -> None:
        if publication_row is None:
            raise AgentMemoryBlocked("memory publication does not exist")
        expected_target = (
            PublicationTarget.LAWYER_PERSONAL.value
            if record.layer is MemoryLayer.LAWYER_PERSONAL
            else PublicationTarget.FIRM_KNOWLEDGE.value
        )
        if (
            str(publication_row["publication_id"]) != publication_id
            or publication_row["target"] != expected_target
            or str(publication_row["source_matter_id"]) != authority_matter_id
            or publication_row["published_content_hash"] != record.content_hash
            or publication_row["provenance_hash"] != record.provenance_hash
            or publication_row["approval_hash"] != record.publication_approval_hash
        ):
            raise AgentMemoryBlocked("memory record is not bound to the approved publication")
        if record.layer is MemoryLayer.LAWYER_PERSONAL and (
            str(publication_row["owner_actor_id"]) != record.owner_actor_id
        ):
            raise AgentMemoryBlocked("personal memory owner differs from its publication")
        expected_groups = tuple(sorted(record.permission_group_ids))
        if tuple(sorted(publication_group_ids)) != expected_groups:
            raise AgentMemoryBlocked(
                "memory groups differ from the approved publication scope"
            )

    @staticmethod
    def _insert_source_ref(
        connection: Any,
        *,
        firm_id: str,
        record: MemoryRecord,
        binding: AuthoritativeMemorySourceBinding,
    ) -> None:
        source = binding.source_ref
        _validate_uuid("source_id", source.source_id)
        connection.execute(
            """
            INSERT INTO case_agent_memory_source_refs (
                record_id, record_version, firm_id, source_type, source_id,
                source_version, source_record_hash, content_sha256,
                location_kind, exposure,
                page_number, normalized_box, paragraph_label, sheet_name,
                cell_range, start_millis, end_millis, source_url
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                record.record_id,
                record.record_version,
                firm_id,
                source.source_type,
                source.source_id,
                source.source_version,
                binding.source_record_hash,
                source.content_hash,
                source.location_kind.value,
                source.exposure.value,
                source.page_number,
                list(source.normalized_box) if source.normalized_box else None,
                source.paragraph_label,
                source.sheet_name,
                source.cell_range,
                source.start_millis,
                source.end_millis,
                source.source_url,
            ),
        )

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _record_from_row(
    row: Mapping[str, Any],
    *,
    permission_group_ids: tuple[str, ...],
    source_refs: tuple[MemorySourceRef, ...],
) -> MemoryRecord:
    return MemoryRecord(
        record_id=str(row["record_id"]),
        record_version=int(row["record_version"]),
        layer=MemoryLayer(row["layer"]),
        status=MemoryStatus(row["status"]),
        authority=MemoryAuthority(row["authority"]),
        content_hash=row["content_sha256"],
        source_refs=source_refs,
        firm_id=str(row["semantic_firm_id"]) if row["semantic_firm_id"] else None,
        matter_id=str(row["matter_id"]) if row["matter_id"] else None,
        owner_actor_id=str(row["owner_actor_id"]) if row["owner_actor_id"] else None,
        run_id=str(row["run_id"]) if row["run_id"] else None,
        task_id=str(row["task_id"]) if row["task_id"] else None,
        permission_group_ids=permission_group_ids,
        case_type_codes=tuple(row["case_type_codes"]),
        procedure_stages=tuple(row["procedure_stages"]),
        issue_tags=tuple(row["issue_tags"]),
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        known_from=row["known_from"],
        known_to=row["known_to"],
        publication_approval_hash=row["publication_approval_hash"],
        provenance_hash=row["provenance_hash"],
        updated_at=row["updated_at"],
        source_authority_registry_hash=row["source_authority_registry_hash"],
    )


def _source_from_row(row: Mapping[str, Any]) -> MemorySourceRef:
    box = row["normalized_box"]
    return MemorySourceRef(
        source_type=row["source_type"],
        source_id=str(row["source_id"]),
        source_version=row["source_version"],
        content_hash=row["content_sha256"],
        location_kind=SourceLocationKind(row["location_kind"]),
        exposure=SourceExposure(row["exposure"]),
        page_number=row["page_number"],
        normalized_box=tuple(float(value) for value in box) if box else None,
        paragraph_label=row["paragraph_label"],
        sheet_name=row["sheet_name"],
        cell_range=row["cell_range"],
        start_millis=row["start_millis"],
        end_millis=row["end_millis"],
        source_url=row["source_url"],
    )


def _checkpoint_from_row(row: Mapping[str, Any]) -> RunMemoryCheckpoint:
    return RunMemoryCheckpoint(
        firm_id=str(row["firm_id"]),
        matter_id=str(row["matter_id"]),
        owner_actor_id=str(row["owner_actor_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        previous_checkpoint_hash=row["previous_checkpoint_hash"],
        case_snapshot_hash=row["case_snapshot_hash"],
        plan_hash=row["plan_hash"],
        task_state_hash=row["task_state_hash"],
        unresolved_question_ids=tuple(
            sorted(str(value) for value in row["unresolved_question_ids"])
        ),
        retrieval_scope_hashes=tuple(
            sorted(str(value) for value in row["retrieval_scope_hashes"])
        ),
        occurred_at=row["occurred_at"],
        checkpoint_hash=row["checkpoint_hash"],
    )


def _load_checkpoint_run_authority(
    connection: Any,
    *,
    actor: Actor,
    matter_id: str,
    run_id: str,
    lock: bool,
) -> Mapping[str, Any]:
    # Lock only the authoritative run row.  A bare FOR UPDATE also attempts
    # to lock the joined users and matter_actor_roles rows, which incorrectly
    # requires the least-privileged Worker to hold UPDATE on those tables.
    suffix = "FOR UPDATE OF run" if lock else ""
    row = connection.execute(
        f"""
        SELECT run.run_id, run.firm_id, run.matter_id, run.created_by,
               run.snapshot_hash, run.current_graph_hash, run.projection_hash,
               run.current_event_version
        FROM case_agent_runs run
        JOIN users worker
          ON worker.user_id = %s AND worker.firm_id = run.firm_id
         AND worker.status = 'ACTIVE'
        JOIN matter_actor_roles worker_role
          ON worker_role.matter_id = run.matter_id
         AND worker_role.firm_id = run.firm_id
         AND worker_role.user_id = worker.user_id
         AND worker_role.role = 'SYSTEM_WORKER'
         AND worker_role.revoked_at IS NULL
        WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
        {suffix}
        """,
        (actor.actor_id, run_id, matter_id, actor.firm_id),
    ).fetchone()
    if row is None:
        raise PermissionError("worker cannot checkpoint this Agent run")
    return row


def _validate_checkpoint_against_run(
    connection: Any,
    *,
    checkpoint: RunMemoryCheckpoint,
    run: Mapping[str, Any],
) -> None:
    if (
        str(run["firm_id"]) != checkpoint.firm_id
        or str(run["matter_id"]) != checkpoint.matter_id
        or str(run["run_id"]) != checkpoint.run_id
        or str(run["created_by"]) != checkpoint.owner_actor_id
        or run["snapshot_hash"] != checkpoint.case_snapshot_hash
        or run["current_graph_hash"] is None
        or run["current_graph_hash"] != checkpoint.plan_hash
    ):
        raise AgentMemoryBlocked("checkpoint differs from the current Agent run")

    if checkpoint.task_state_hash != run["projection_hash"]:
        raise AgentMemoryBlocked("checkpoint task state is not current")

    if checkpoint.unresolved_question_ids:
        question_rows = connection.execute(
            """
            SELECT task_id
            FROM case_agent_task_heads
            WHERE run_id = %s AND firm_id = %s AND matter_id = %s
              AND is_current = true AND status = 'WAITING_APPROVAL'
              AND task_id = ANY(%s)
            """,
            (
                checkpoint.run_id,
                checkpoint.firm_id,
                checkpoint.matter_id,
                list(checkpoint.unresolved_question_ids),
            ),
        ).fetchall()
        if {str(row["task_id"]) for row in question_rows} != set(
            checkpoint.unresolved_question_ids
        ):
            raise AgentMemoryBlocked("checkpoint contains an unknown unresolved question")

    if checkpoint.retrieval_scope_hashes:
        retrieval_rows = connection.execute(
            """
            SELECT scope_hash
            FROM case_agent_memory_retrieval_audits
            WHERE run_id = %s AND firm_id = %s AND matter_id = %s
              AND actor_id = %s AND scope_hash = ANY(%s)
            """,
            (
                checkpoint.run_id,
                checkpoint.firm_id,
                checkpoint.matter_id,
                checkpoint.owner_actor_id,
                list(checkpoint.retrieval_scope_hashes),
            ),
        ).fetchall()
        if {row["scope_hash"] for row in retrieval_rows} != set(
            checkpoint.retrieval_scope_hashes
        ):
            raise AgentMemoryBlocked("checkpoint contains an unauthorized retrieval scope")


def _validate_execution_run_scope(
    connection: Any,
    *,
    actor: Actor,
    matter_id: str,
    run_id: str,
    task_id: str | None,
) -> None:
    run = connection.execute(
        """
        SELECT run.run_id
        FROM case_agent_runs run
        WHERE run.run_id = %s AND run.firm_id = %s AND run.matter_id = %s
          AND run.created_by = %s
        """,
        (run_id, actor.firm_id, matter_id, actor.actor_id),
    ).fetchone()
    if run is None:
        raise AgentMemoryBlocked("memory retrieval is not bound to the lawyer's Agent run")
    if task_id is not None:
        task = connection.execute(
            """
            SELECT task_id
            FROM case_agent_tasks
            WHERE run_id = %s AND task_id = %s AND firm_id = %s AND matter_id = %s
            """,
            (run_id, task_id, actor.firm_id, matter_id),
        ).fetchone()
        if task is None:
            raise AgentMemoryBlocked("memory retrieval task is outside the Agent run")


def _matter_grant_hash(
    *,
    actor: Actor,
    matter_id: str,
    matter_version: int,
    roles: Iterable[Role],
    group_ids: Iterable[str],
    role_grants: Iterable[str],
    group_grants: Iterable[str],
) -> str:
    payload = {
        "schema_version": "case-agent-memory-grant-v1",
        "actor_id": actor.actor_id,
        "firm_id": actor.firm_id,
        "matter_id": matter_id,
        "matter_version": matter_version,
        "roles": sorted(role.value for role in roles),
        "permission_group_ids": sorted(group_ids),
        "role_grants": sorted(role_grants),
        "group_grants": sorted(group_grants),
        "identity_status": "ACTIVE",
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _terminal_hash(record: MemoryRecord, actor_id: str, reason_code: str) -> str:
    payload = {
        "schema_version": "case-agent-memory-tombstone-v1",
        "record_id": record.record_id,
        "blocked_through_version": record.record_version,
        "terminal_status": record.status.value,
        "denied_by": actor_id,
        "reason_code": reason_code,
        "denied_at": record.updated_at.isoformat(),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_object_key(value: str, content_hash: str) -> None:
    _validate_sha256("content_hash", content_hash)
    expected = f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca"
    if value != expected:
        raise AgentMemoryBlocked("memory object key is not content-addressed")


def _validate_actor(actor: Actor) -> None:
    _validate_uuid("actor_id", actor.actor_id)
    _validate_uuid("firm_id", actor.firm_id)
    if not actor.roles:
        raise PermissionError("actor has no server-verified role")


def _require_pure_system_worker(actor: Actor) -> None:
    _validate_actor(actor)
    if actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("memory checkpoint requires a dedicated SYSTEM_WORKER")


def _require_no_worker(actor: Actor) -> None:
    if Role.SYSTEM_WORKER in actor.roles:
        raise PermissionError("SYSTEM_WORKER cannot publish or govern durable knowledge")
    if not actor.roles.intersection(_GOVERNANCE_ROLES):
        raise PermissionError("durable knowledge requires lead-lawyer or reviewer authority")


def _require_claimed_human_roles(actor: Actor, database_roles: frozenset[Role]) -> None:
    claimed = actor.roles.intersection(_HUMAN_CASE_ROLES)
    actual = database_roles.intersection(_HUMAN_CASE_ROLES)
    if not claimed or claimed != actual or Role.SYSTEM_WORKER in actor.roles:
        raise PermissionError("server identity roles differ from current matter roles")


def _validate_query_actor(actor: Actor, query: MemoryQuery) -> None:
    if (
        query.access.actor_id != actor.actor_id
        or query.access.firm_id != actor.firm_id
    ):
        raise AgentMemoryBlocked("memory query is not bound to the authenticated actor")


__all__ = [
    "AuthoritativeMemorySourceBinding",
    "AuthorizedVectorSearchBackend",
    "KnowledgePublicationWriteReceipt",
    "MemoryIndexDocument",
    "MemoryVersionWriteReceipt",
    "PostgresCaseAgentMemoryStore",
    "PostgresFullTextRetrieval",
    "RunMemoryCheckpointWriteReceipt",
]
