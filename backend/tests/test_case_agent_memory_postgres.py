from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
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
    KnowledgePublicationCandidate,
    PublicationReview,
    PublicationTarget,
    SourceExposure,
    SourceLocationKind,
    RunMemoryCheckpoint,
    VerifiedRetrievalPrincipal,
    build_retrieval_query,
)
from case_kernel.case_agent_memory_postgres import (
    AuthoritativeMemorySourceBinding,
    MemoryIndexDocument,
    PostgresCaseAgentMemoryStore,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _Result:
    def __init__(self, *, row=None, rows=None, rowcount=1):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _ScriptedConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.executed.append((normalized, params))
        if not self.responses:
            raise AssertionError(f"unexpected SQL: {normalized}")
        response = self.responses.pop(0)
        if callable(response):
            return response(normalized, params)
        return response


class _TestStore(PostgresCaseAgentMemoryStore):
    def __init__(self, connection):
        super().__init__("postgresql://not-used.invalid/memory")
        self.connection = connection

    @contextmanager
    def _transaction(self, _firm_id):
        yield self.connection


class CaseAgentMemoryPostgresTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 13, 7, 0, tzinfo=timezone.utc)
        self.firm_id = _id()
        self.matter_id = _id()
        self.actor = Actor(
            _id(), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.group_id = _id()

    def _authority_responses(self, *, version=7, group_id=None, role="LEAD_LAWYER"):
        return [
            _Result(
                row={
                    "version": version,
                    "checked_at": self.now,
                    "active_roles": [role],
                    "active_role_grants": [f"{role}:{self.now.isoformat()}"],
                }
            ),
            _Result(
                rows=[]
                if group_id is None
                else [{
                    "group_id": group_id,
                    "grant_hash": _digest("group-grant"),
                    "policy_hash": _digest("group-policy"),
                }]
            ),
        ]

    def test_resolve_principal_uses_current_database_roles_and_groups(self):
        connection = _ScriptedConnection(
            self._authority_responses(group_id=self.group_id)
        )
        principal = _TestStore(connection).resolve_retrieval_principal(
            actor=self.actor, matter_id=self.matter_id
        )
        self.assertEqual(principal.permission_group_ids, (self.group_id,))
        self.assertEqual(principal.matter_version, 7)
        self.assertEqual(len(principal.matter_access_grant_hash), 64)
        role_sql = connection.executed[0][0]
        group_sql = connection.executed[1][0]
        self.assertIn("users actor_row", role_sql)
        self.assertIn("actor_row.status = 'ACTIVE'", role_sql)
        self.assertIn("group_member_denials", group_sql)

    def test_worker_appends_and_recovers_exact_run_checkpoint(self):
        worker = Actor(_id(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        run_id = _id()
        owner_id = _id()
        run_snapshot_hash = _digest("run-snapshot")
        graph_hash = _digest("graph")
        projection_hash = _digest("projection")
        checkpoint = RunMemoryCheckpoint.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=owner_id,
            run_id=run_id,
            sequence=1,
            previous_checkpoint_hash=None,
            case_snapshot_hash=run_snapshot_hash,
            plan_hash=graph_hash,
            task_state_hash=projection_hash,
            unresolved_question_ids=(),
            retrieval_scope_hashes=(),
            occurred_at=self.now,
        )
        run_row = {
            "run_id": run_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "created_by": owner_id,
            "snapshot_hash": run_snapshot_hash,
            "current_graph_hash": graph_hash,
            "projection_hash": projection_hash,
            "current_event_version": 4,
        }
        connection = _ScriptedConnection(
            [
                _Result(row=run_row),
                _Result(row=None),
                _Result(row=None),
                _Result(row={"checkpoint_id": _id()}),
            ]
        )
        receipt = _TestStore(connection).append_run_checkpoint(
            actor=worker, checkpoint=checkpoint
        )
        self.assertEqual(receipt.checkpoint_hash, checkpoint.checkpoint_hash)
        self.assertEqual(receipt.sequence, 1)
        sql = [statement for statement, _ in connection.executed]
        self.assertIn("worker_role.role = 'SYSTEM_WORKER'", sql[0])
        self.assertIn("FOR UPDATE OF run", sql[0])
        self.assertNotIn("FOR UPDATE JOIN", sql[0])
        self.assertIn("ON CONFLICT (checkpoint_id) DO NOTHING", sql[-1])

        checkpoint_row = {
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "owner_actor_id": owner_id,
            "run_id": run_id,
            "sequence": 1,
            "previous_checkpoint_hash": None,
            "case_snapshot_hash": run_snapshot_hash,
            "plan_hash": graph_hash,
            "task_state_hash": projection_hash,
            "unresolved_question_ids": [],
            "retrieval_scope_hashes": [],
            "occurred_at": self.now,
            "checkpoint_hash": checkpoint.checkpoint_hash,
        }
        connection = _ScriptedConnection(
            [
                _Result(row=run_row),
                _Result(rows=[checkpoint_row]),
                _Result(rows=[]),
            ]
        )
        restored = _TestStore(connection).get_latest_run_checkpoint(
            actor=worker, matter_id=self.matter_id, run_id=run_id
        )
        self.assertEqual(restored, checkpoint)

    def test_checkpoint_rejects_non_worker_and_projection_drift(self):
        run_id, owner_id = _id(), _id()
        checkpoint = RunMemoryCheckpoint.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=owner_id,
            run_id=run_id,
            sequence=1,
            previous_checkpoint_hash=None,
            case_snapshot_hash=_digest("snapshot"),
            plan_hash=_digest("plan"),
            task_state_hash=_digest("expected-projection"),
            unresolved_question_ids=(),
            retrieval_scope_hashes=(),
            occurred_at=self.now,
        )
        with self.assertRaisesRegex(PermissionError, "SYSTEM_WORKER"):
            _TestStore(_ScriptedConnection([])).append_run_checkpoint(
                actor=self.actor, checkpoint=checkpoint
            )
        worker = Actor(_id(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        run_row = {
            "run_id": run_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "created_by": owner_id,
            "snapshot_hash": checkpoint.case_snapshot_hash,
            "current_graph_hash": checkpoint.plan_hash,
            "projection_hash": _digest("new-projection"),
            "current_event_version": 5,
        }
        with self.assertRaisesRegex(AgentMemoryBlocked, "task state"):
            _TestStore(_ScriptedConnection([_Result(row=run_row)])).append_run_checkpoint(
                actor=worker, checkpoint=checkpoint
            )

    def test_claimed_roles_must_equal_current_database_roles(self):
        mixed_actor = Actor(
            self.actor.actor_id,
            self.firm_id,
            frozenset({Role.LEAD_LAWYER, Role.REVIEWER}),
        )
        connection = _ScriptedConnection(self._authority_responses())
        with self.assertRaisesRegex(PermissionError, "differ"):
            _TestStore(connection).resolve_retrieval_principal(
                actor=mixed_actor, matter_id=self.matter_id
            )

    def test_working_memory_retrieval_requires_exact_execution_run(self):
        run_id = _id()
        principal = VerifiedRetrievalPrincipal(
            actor=self.actor,
            matter_id=self.matter_id,
            matter_access_grant_hash=_digest("grant"),
            matter_version=7,
            permission_group_ids=(),
        )
        query = build_retrieval_query(
            principal=principal,
            query_text="继续核对本次任务的未决事项",
            layers=(MemoryLayer.RUN_WORKING,),
            knowledge_as_of=self.now,
            run_id=run_id,
        )
        with self.assertRaisesRegex(AgentMemoryBlocked, "executing run"):
            _TestStore(_ScriptedConnection([])).retrieve_full_text(
                actor=self.actor, query=query, execution_run_id=_id()
            )

    def test_execution_retrieval_rejects_run_owned_by_another_lawyer(self):
        run_id = _id()
        principal = VerifiedRetrievalPrincipal(
            actor=self.actor,
            matter_id=self.matter_id,
            matter_access_grant_hash=_digest("grant"),
            matter_version=7,
            permission_group_ids=(),
        )
        query = build_retrieval_query(
            principal=principal,
            query_text="检索本案已经确认的付款记录",
            layers=(MemoryLayer.CASE_LONG_TERM,),
            knowledge_as_of=self.now,
        )
        connection = _ScriptedConnection([_Result(row=None)])
        with self.assertRaisesRegex(AgentMemoryBlocked, "lawyer's Agent run"):
            _TestStore(connection).retrieve_full_text(
                actor=self.actor, query=query, execution_run_id=run_id
            )
        self.assertIn("run.created_by", connection.executed[0][0])

    def test_full_text_retrieval_is_acl_first_and_reauthorizes_after_search(self):
        content_hash = _digest("confirmed-ledger-memory")
        source_hash = _digest("confirmed-fact-decision")
        record_id, source_id = _id(), _id()
        version_row = {
            "record_id": record_id,
            "record_version": 1,
            "semantic_firm_id": self.firm_id,
            "layer": "CASE_LONG_TERM",
            "status": "CONFIRMED",
            "authority": "CONFIRMED_CASE_LEDGER",
            "content_sha256": content_hash,
            "matter_id": self.matter_id,
            "owner_actor_id": None,
            "run_id": None,
            "task_id": None,
            "case_type_codes": ["PRIVATE_LENDING"],
            "procedure_stages": ["FIRST_INSTANCE"],
            "issue_tags": ["INTEREST"],
            "effective_from": None,
            "effective_to": None,
            "known_from": self.now,
            "known_to": None,
            "publication_approval_hash": None,
            "source_authority_registry_hash": None,
            "provenance_hash": _digest("provenance"),
            "updated_at": self.now,
        }
        source_row = {
            "record_id": record_id,
            "record_version": 1,
            "source_type": "CASE_FACT",
            "source_id": source_id,
            "source_version": "1",
            "content_sha256": source_hash,
            "location_kind": "OBJECT",
            "exposure": "CASE_PRIVATE",
            "page_number": None,
            "normalized_box": None,
            "paragraph_label": None,
            "sheet_name": None,
            "cell_range": None,
            "start_millis": None,
            "end_millis": None,
            "source_url": None,
        }
        responses = []
        responses += self._authority_responses(group_id=self.group_id)
        responses += [
            _Result(rows=[version_row]),
            _Result(rows=[{"record_id": record_id, "record_version": 1, "group_id": self.group_id}]),
            _Result(rows=[source_row]),
            _Result(rows=[{
                "record_id": record_id,
                "record_version": 1,
                "content_sha256": content_hash,
                "lexical_score": 0.9,
            }]),
        ]
        responses += self._authority_responses(group_id=self.group_id)
        responses += [
            _Result(rows=[version_row]),
            _Result(rows=[{"record_id": record_id, "record_version": 1, "group_id": self.group_id}]),
            _Result(rows=[source_row]),
            _Result(rowcount=1),
        ]
        connection = _ScriptedConnection(responses)
        store = _TestStore(connection)
        principal = store.resolve_retrieval_principal(
            actor=self.actor, matter_id=self.matter_id
        )
        # Use a fresh script for retrieval because principal resolution itself
        # is a separate server request in the real Web runtime.
        connection.responses = responses = []
        responses += self._authority_responses(group_id=self.group_id)
        responses += [
            _Result(rows=[version_row]),
            _Result(rows=[{"record_id": record_id, "record_version": 1, "group_id": self.group_id}]),
            _Result(rows=[source_row]),
            _Result(rows=[{
                "record_id": record_id,
                "record_version": 1,
                "content_sha256": content_hash,
                "lexical_score": 0.9,
            }]),
        ]
        responses += self._authority_responses(group_id=self.group_id)
        responses += [
            _Result(rows=[version_row]),
            _Result(rows=[{"record_id": record_id, "record_version": 1, "group_id": self.group_id}]),
            _Result(rows=[source_row]),
            _Result(rowcount=1),
        ]
        query = build_retrieval_query(
            principal=principal,
            query_text="核对借款利息和已经确认的付款",
            layers=(MemoryLayer.CASE_LONG_TERM,),
            knowledge_as_of=self.now,
            case_type_codes=("PRIVATE_LENDING",),
            procedure_stages=("FIRST_INSTANCE",),
            issue_tags=("INTEREST",),
        )
        result = store.retrieve_full_text(actor=self.actor, query=query)
        self.assertEqual(result.search_mode, "POSTGRES_FTS_V1")
        self.assertEqual(tuple(hit.record_id for hit in result.hits), (record_id,))
        sql = [statement for statement, _ in connection.executed]
        fts_index = next(i for i, statement in enumerate(sql) if "websearch_to_tsquery" in statement)
        authoritative_indexes = [
            i for i, statement in enumerate(sql)
            if "FROM case_agent_memory_record_heads head" in statement
        ]
        self.assertEqual(len(authoritative_indexes), 2)
        self.assertLess(authoritative_indexes[0], fts_index)
        self.assertGreater(authoritative_indexes[1], fts_index)
        self.assertIn("FROM unnest", sql[fts_index])
        audit_sql, audit_params = connection.executed[-1]
        self.assertIn("memory_retrieval_audits", audit_sql)
        self.assertNotIn(query.query_text, audit_sql)
        self.assertNotIn(query.query_text, repr(audit_params))

    def test_authoritative_read_rechecks_current_public_source_status(self):
        connection = _ScriptedConnection([_Result(rows=[])])
        principal = VerifiedRetrievalPrincipal(
            actor=self.actor,
            matter_id=self.matter_id,
            matter_access_grant_hash=_digest("matter-grant"),
            matter_version=7,
            permission_group_ids=(self.group_id,),
        )
        query = build_retrieval_query(
            principal=principal,
            query_text="核对法律效力期间",
            layers=(MemoryLayer.PUBLIC_LEGAL,),
            knowledge_as_of=self.now,
            legal_period_start=self.now.date(),
            legal_period_end=self.now.date(),
        )
        authority = type("Authority", (), {
            "permission_group_ids": (self.group_id,),
        })()
        records = _TestStore(connection)._load_authoritative_records(
            connection, query=query, authority=authority
        )
        self.assertEqual(records, ())
        sql = connection.executed[0][0]
        self.assertIn("snapshot.verification_status = 'VERIFIED'", sql)
        self.assertIn("snapshot.license_status = 'ACTIVE'", sql)
        self.assertIn(
            "registration.registration_hash = version.source_authority_registry_hash",
            sql,
        )

    def test_authority_change_after_search_blocks_before_audit(self):
        principal_connection = _ScriptedConnection(
            self._authority_responses(group_id=self.group_id)
        )
        principal = _TestStore(principal_connection).resolve_retrieval_principal(
            actor=self.actor, matter_id=self.matter_id
        )
        # Empty authorized records stops before FTS, so directly test a group
        # change through the same database behavior used by retrieval.
        connection = _ScriptedConnection(
            self._authority_responses(group_id=_id())
        )
        new_principal = _TestStore(connection).resolve_retrieval_principal(
            actor=self.actor, matter_id=self.matter_id
        )
        self.assertNotEqual(
            principal.matter_access_grant_hash,
            new_principal.matter_access_grant_hash,
        )

    def test_worker_cannot_publish_or_govern_durable_memory(self):
        worker = Actor(_id(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        source = MemorySourceRef(
            source_type="CASE_FACT",
            source_id=_id(),
            source_version="1",
            content_hash=_digest("fact"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.CASE_PRIVATE,
        )
        record = MemoryRecord(
            record_id=_id(),
            record_version=1,
            layer=MemoryLayer.CASE_LONG_TERM,
            status=MemoryStatus.CONFIRMED,
            authority=MemoryAuthority.CONFIRMED_CASE_LEDGER,
            content_hash=_digest("memory"),
            source_refs=(source,),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=None,
            run_id=None,
            task_id=None,
            permission_group_ids=(self.group_id,),
            case_type_codes=(),
            procedure_stages=(),
            issue_tags=(),
            effective_from=None,
            effective_to=None,
            known_from=self.now,
            known_to=None,
            publication_approval_hash=None,
            provenance_hash=_digest("provenance"),
            updated_at=self.now,
        )
        document = MemoryIndexDocument(
            content_object_key=f"{record.content_hash[:2]}/{record.content_hash[2:4]}/{record.content_hash}.lca",
            search_document="借款 利息",
            search_document_hash=_digest("借款 利息"),
            extractor_id="trusted-text-extractor",
            extractor_version="1",
            indexing_receipt_hash=_digest("receipt"),
        )
        with self.assertRaisesRegex(PermissionError, "SYSTEM_WORKER"):
            _TestStore(_ScriptedConnection([])).append_memory_version(
                actor=worker,
                authority_matter_id=self.matter_id,
                expected_matter_version=1,
                expected_record_version=0,
                record=record,
                source_bindings=(
                    AuthoritativeMemorySourceBinding(
                        source_ref=source,
                        source_record_hash=_digest("confirmed-fact-receipt"),
                    ),
                ),
                index_document=document,
            )

    def test_fts_document_hash_is_verified_before_database_access(self):
        with self.assertRaisesRegex(AgentMemoryBlocked, "hash"):
            MemoryIndexDocument(
                content_object_key="aa/bb/" + "c" * 64 + ".lca",
                search_document="借款 利息",
                search_document_hash="a" * 64,
                extractor_id="trusted-extractor",
                extractor_version="1",
                indexing_receipt_hash="b" * 64,
            )

    def test_publication_without_two_durable_review_receipts_writes_nothing(self):
        reviewer_id = _id()
        candidate = KnowledgePublicationCandidate.build(
            publication_id=_id(),
            source_firm_id=self.firm_id,
            target=PublicationTarget.FIRM_KNOWLEDGE,
            source_matter_id=self.matter_id,
            source_object_hashes=(_digest("source-object"),),
            published_content_hash=_digest("sanitized-content"),
            published_source_object_id=_id(),
            published_source_object_hash=_digest("sanitized-object"),
            provenance_hash=_digest("publication-provenance"),
            owner_actor_id=None,
            permission_group_ids=(self.group_id,),
            anonymization_review=PublicationReview.PASSED,
            conflict_review=PublicationReview.PASSED,
            confidentiality_review=PublicationReview.PASSED,
            approved_by=self.actor.actor_id,
            second_approver_id=reviewer_id,
            publication_policy_version="1.0.0",
            publication_policy_hash=_digest("publication-policy"),
            approved_at=self.now,
        )
        connection = _ScriptedConnection(
            [
                _Result(row={"version": 7, "permitted": True}),
                _Result(row={"ok": 1}),
                _Result(rows=[]),
            ]
        )
        store = _TestStore(connection)
        with self.assertRaisesRegex(AgentMemoryBlocked, "two independent"):
            store.register_publication(
                actor=self.actor,
                candidate=candidate,
                expected_source_matter_version=7,
                storage_object_key=(
                    f"{candidate.published_source_object_hash[:2]}/"
                    f"{candidate.published_source_object_hash[2:4]}/"
                    f"{candidate.published_source_object_hash}.lca"
                ),
                byte_size=128,
                media_type="application/json",
                sanitization_manifest_hash=_digest("sanitization-manifest"),
                first_review_hash=_digest("first-review"),
                second_review_hash=_digest("second-review"),
            )
        sql = [statement for statement, _ in connection.executed]
        self.assertFalse(
            any("INSERT INTO case_agent_published_knowledge_objects" in value for value in sql)
        )
        self.assertFalse(
            any("INSERT INTO case_agent_knowledge_publications" in value for value in sql)
        )

    def test_source_binding_mismatch_fails_before_database_access(self):
        source = MemorySourceRef(
            source_type="CASE_FACT",
            source_id=_id(),
            source_version="1",
            content_hash=_digest("normalized-fact"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.CASE_PRIVATE,
        )
        record = MemoryRecord(
            record_id=_id(),
            record_version=1,
            layer=MemoryLayer.CASE_LONG_TERM,
            status=MemoryStatus.CONFIRMED,
            authority=MemoryAuthority.CONFIRMED_CASE_LEDGER,
            content_hash=_digest("memory-record"),
            source_refs=(source,),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=None,
            run_id=None,
            task_id=None,
            permission_group_ids=(self.group_id,),
            case_type_codes=(),
            procedure_stages=(),
            issue_tags=(),
            effective_from=None,
            effective_to=None,
            known_from=self.now,
            known_to=None,
            publication_approval_hash=None,
            provenance_hash=_digest("memory-provenance"),
            updated_at=self.now,
        )
        other_source = MemorySourceRef(
            source_type="CASE_FACT",
            source_id=_id(),
            source_version="1",
            content_hash=_digest("other-fact"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.CASE_PRIVATE,
        )
        document = MemoryIndexDocument(
            content_object_key=(
                f"{record.content_hash[:2]}/{record.content_hash[2:4]}/"
                f"{record.content_hash}.lca"
            ),
            search_document="借款 利息",
            search_document_hash=_digest("借款 利息"),
            extractor_id="trusted-extractor",
            extractor_version="1",
            indexing_receipt_hash=_digest("indexing-receipt"),
        )
        connection = _ScriptedConnection([])
        with self.assertRaisesRegex(AgentMemoryBlocked, "exactly match"):
            _TestStore(connection).append_memory_version(
                actor=self.actor,
                authority_matter_id=self.matter_id,
                expected_matter_version=7,
                expected_record_version=0,
                record=record,
                source_bindings=(
                    AuthoritativeMemorySourceBinding(
                        source_ref=other_source,
                        source_record_hash=_digest("other-receipt"),
                    ),
                ),
                index_document=document,
            )
        self.assertEqual(connection.executed, [])

    def test_firm_memory_groups_must_match_approved_publication_scope(self):
        published_object_id = _id()
        publication_id = _id()
        approved_group_id = _id()
        source = MemorySourceRef(
            source_type="PUBLISHED_KNOWLEDGE_OBJECT",
            source_id=published_object_id,
            source_version="1",
            content_hash=_digest("sanitized-object"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.PUBLISHED_SANITIZED,
        )
        record = MemoryRecord(
            record_id=_id(),
            record_version=1,
            layer=MemoryLayer.FIRM_KNOWLEDGE,
            status=MemoryStatus.PUBLISHED,
            authority=MemoryAuthority.FIRM_SOP,
            content_hash=_digest("sanitized-content"),
            source_refs=(source,),
            firm_id=self.firm_id,
            matter_id=None,
            owner_actor_id=None,
            run_id=None,
            task_id=None,
            permission_group_ids=(self.group_id,),
            case_type_codes=(),
            procedure_stages=(),
            issue_tags=(),
            effective_from=None,
            effective_to=None,
            known_from=self.now,
            known_to=None,
            publication_approval_hash=_digest("publication-approval"),
            provenance_hash=_digest("publication-provenance"),
            updated_at=self.now,
        )
        document = MemoryIndexDocument(
            content_object_key=(
                f"{record.content_hash[:2]}/{record.content_hash[2:4]}/"
                f"{record.content_hash}.lca"
            ),
            search_document="律所 办案 指引",
            search_document_hash=_digest("律所 办案 指引"),
            extractor_id="trusted-extractor",
            extractor_version="1",
            indexing_receipt_hash=_digest("indexing-receipt"),
        )
        connection = _ScriptedConnection(
            [
                _Result(row={"version": 7, "permitted": True}),
                _Result(
                    row={
                        "publication_id": publication_id,
                        "target": "FIRM_KNOWLEDGE",
                        "source_matter_id": self.matter_id,
                        "owner_actor_id": None,
                        "published_content_hash": record.content_hash,
                        "published_source_object_hash": source.content_hash,
                        "provenance_hash": record.provenance_hash,
                        "approval_hash": record.publication_approval_hash,
                    }
                ),
                _Result(rows=[{"group_id": approved_group_id}]),
            ]
        )
        with self.assertRaisesRegex(AgentMemoryBlocked, "approved publication scope"):
            _TestStore(connection).append_memory_version(
                actor=self.actor,
                authority_matter_id=self.matter_id,
                expected_matter_version=7,
                expected_record_version=0,
                record=record,
                source_bindings=(
                    AuthoritativeMemorySourceBinding(
                        source_ref=source,
                        source_record_hash=record.publication_approval_hash or "",
                    ),
                ),
                index_document=document,
                publication_id=publication_id,
            )
        self.assertFalse(
            any(
                "INSERT INTO case_agent_memory_record_versions" in statement
                for statement, _ in connection.executed
            )
        )

    def test_public_legal_memory_requires_exact_registered_snapshot_set(self):
        snapshot_id = _id()
        registry_hash = _digest("official-registry")
        approval_hash = _digest("official-approval")
        source = MemorySourceRef(
            source_type="OFFICIAL_LEGAL_SNAPSHOT",
            source_id=snapshot_id,
            source_version="1",
            content_hash=_digest("official-bytes"),
            location_kind=SourceLocationKind.WEB_FRAGMENT,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            source_url="https://flk.npc.gov.cn/detail.html",
        )
        record = MemoryRecord(
            record_id=_id(),
            record_version=1,
            layer=MemoryLayer.PUBLIC_LEGAL,
            status=MemoryStatus.PUBLISHED,
            authority=MemoryAuthority.PRIMARY_LAW,
            content_hash=_digest("official-memory"),
            source_refs=(source,),
            firm_id=None,
            matter_id=None,
            owner_actor_id=None,
            run_id=None,
            task_id=None,
            permission_group_ids=(),
            case_type_codes=(),
            procedure_stages=(),
            issue_tags=("INTEREST",),
            effective_from=self.now.date(),
            effective_to=None,
            known_from=self.now,
            known_to=None,
            publication_approval_hash=approval_hash,
            provenance_hash=_digest("official-provenance"),
            updated_at=self.now,
            source_authority_registry_hash=registry_hash,
        )
        document = MemoryIndexDocument(
            content_object_key=(
                f"{record.content_hash[:2]}/{record.content_hash[2:4]}/"
                f"{record.content_hash}.lca"
            ),
            search_document="法律 利息 规定",
            search_document_hash=_digest("法律 利息 规定"),
            extractor_id="trusted-extractor",
            extractor_version="1",
            indexing_receipt_hash=_digest("indexing-receipt"),
        )
        wrong_receipt = _digest("wrong-verification")
        connection = _ScriptedConnection(
            [
                _Result(row={"version": 7, "permitted": True}),
                _Result(row={"registration_hash": registry_hash}),
                _Result(
                    rows=[
                        {
                            "snapshot_id": snapshot_id,
                            "snapshot_content_sha256": source.content_hash,
                            "snapshot_verification_hash": wrong_receipt,
                            "official_url": source.source_url,
                        }
                    ]
                ),
            ]
        )
        with self.assertRaisesRegex(AgentMemoryBlocked, "approved authority registration"):
            _TestStore(connection).append_memory_version(
                actor=self.actor,
                authority_matter_id=self.matter_id,
                expected_matter_version=7,
                expected_record_version=0,
                record=record,
                source_bindings=(
                    AuthoritativeMemorySourceBinding(
                        source_ref=source,
                        source_record_hash=_digest("actual-verification"),
                    ),
                ),
                index_document=document,
            )
        self.assertFalse(
            any(
                "INSERT INTO case_agent_memory_record_versions" in statement
                for statement, _ in connection.executed
            )
        )


if __name__ == "__main__":
    unittest.main()
