from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4
import unittest

from case_kernel.case_agent_memory import (
    AgentMemoryBlocked,
    KnowledgePublicationCandidate,
    MemoryAuthority,
    MemoryLayer,
    MemoryRecord,
    MemorySourceRef,
    MemoryStatus,
    PublicationReview,
    PublicationTarget,
    RetrievalAuthoritySnapshot,
    RunMemoryCheckpoint,
    SimilaritySearchCandidate,
    SourceExposure,
    SourceLocationKind,
    VerifiedRetrievalPrincipal,
    authorize_retrieval_scope,
    build_retrieval_query,
    checkpoint_chain_hash,
    rank_authorized_candidates,
)
from case_kernel.models import Actor, Role


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class CaseAgentMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 13, 10, tzinfo=timezone.utc)
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.group_id = str(uuid4())
        self.actor = Actor(
            actor_id=self.actor_id,
            firm_id=self.firm_id,
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.principal = VerifiedRetrievalPrincipal(
            actor=self.actor,
            matter_id=self.matter_id,
            matter_access_grant_hash=digest("grant"),
            matter_version=8,
            permission_group_ids=(self.group_id,),
        )
        self.authority = RetrievalAuthoritySnapshot(
            actor_id=self.actor_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            matter_access_grant_hash=digest("grant"),
            matter_version=8,
            permission_group_ids=(self.group_id,),
            access_active=True,
            checked_at=self.now,
        )
        self.source = MemorySourceRef(
            source_type="EVIDENCE_PAGE",
            source_id=str(uuid4()),
            source_version="3",
            content_hash=digest("page"),
            location_kind=SourceLocationKind.IMAGE_REGION,
            exposure=SourceExposure.CASE_PRIVATE,
            page_number=2,
            normalized_box=(0.1, 0.2, 0.8, 0.6),
        )

    def case_record(
        self,
        *,
        matter_id: str | None = None,
        layer: MemoryLayer = MemoryLayer.CASE_LONG_TERM,
        run_id: str | None = None,
        task_id: str | None = None,
        owner_actor_id: str | None = None,
    ) -> MemoryRecord:
        return MemoryRecord(
            record_id=str(uuid4()),
            record_version=1,
            layer=layer,
            status=(
                MemoryStatus.CANDIDATE
                if layer is MemoryLayer.RUN_WORKING
                else MemoryStatus.CONFIRMED
            ),
            authority=MemoryAuthority.CONFIRMED_CASE_LEDGER,
            content_hash=digest(str(uuid4())),
            source_refs=(self.source,),
            firm_id=self.firm_id,
            matter_id=matter_id or self.matter_id,
            owner_actor_id=owner_actor_id,
            run_id=run_id,
            task_id=task_id,
            permission_group_ids=(
                ()
                if layer is MemoryLayer.RUN_WORKING
                else self.principal.permission_group_ids
            ),
            case_type_codes=("PRIVATE_LENDING",),
            procedure_stages=("FIRST_INSTANCE",),
            issue_tags=("INTEREST",),
            effective_from=None,
            effective_to=None,
            known_from=self.now - timedelta(days=1),
            known_to=None,
            publication_approval_hash=None,
            provenance_hash=digest("provenance"),
            updated_at=self.now,
            source_authority_registry_hash=None,
        )

    def query(
        self,
        *,
        layers: tuple[MemoryLayer, ...] = (MemoryLayer.CASE_LONG_TERM,),
        run_id: str | None = None,
        task_id: str | None = None,
        legal_period_start: date | None = None,
        legal_period_end: date | None = None,
    ):
        return build_retrieval_query(
            principal=self.principal,
            query_text="核对借款利息规则和已经确认的付款",
            layers=layers,
            knowledge_as_of=self.now,
            legal_period_start=legal_period_start,
            legal_period_end=legal_period_end,
            run_id=run_id,
            task_id=task_id,
            case_type_codes=("PRIVATE_LENDING",),
            procedure_stages=("FIRST_INSTANCE",),
            issue_tags=("INTEREST",),
        )

    def search(self, query, records: tuple[MemoryRecord, ...]):
        scope = authorize_retrieval_scope(
            query=query,
            authority=self.authority,
            authoritative_records=records,
            authorized_at=self.now,
        )
        candidates = tuple(
            SimilaritySearchCandidate(
                record_id=ref.record_id,
                record_version=ref.record_version,
                content_hash=ref.content_hash,
                lexical_score=0.8,
                vector_score=0.7,
            )
            for ref in scope.authorized_records
        )
        return rank_authorized_candidates(
            query,
            scope,
            candidates,
            authority=self.authority,
            authoritative_records={record.record_id: record for record in records},
            verified_at=self.now,
        )

    def test_acl_scope_is_built_before_similarity_search(self) -> None:
        own = self.case_record()
        other = self.case_record(matter_id=str(uuid4()))
        query = self.query()
        scope = authorize_retrieval_scope(
            query=query,
            authority=self.authority,
            authoritative_records=(other, own),
            authorized_at=self.now,
        )
        self.assertEqual(tuple(item.record_id for item in scope.authorized_records), (own.record_id,))
        with self.assertRaisesRegex(AgentMemoryBlocked, "outside the ACL scope"):
            rank_authorized_candidates(
                query,
                scope,
                (
                    SimilaritySearchCandidate(
                        other.record_id,
                        other.record_version,
                        other.content_hash,
                        1,
                        1,
                    ),
                ),
                authority=self.authority,
                authoritative_records={own.record_id: own, other.record_id: other},
                verified_at=self.now,
            )

    def test_same_case_long_term_memory_requires_explicit_access_group(self) -> None:
        with self.assertRaisesRegex(AgentMemoryBlocked, "explicit access group"):
            replace(self.case_record(), permission_group_ids=())

    def test_revoked_or_changed_grant_blocks_before_and_after_search(self) -> None:
        record = self.case_record()
        query = self.query()
        scope = authorize_retrieval_scope(
            query=query,
            authority=self.authority,
            authoritative_records=(record,),
            authorized_at=self.now,
        )
        revoked = replace(self.authority, access_active=False)
        with self.assertRaisesRegex(AgentMemoryBlocked, "revoked"):
            rank_authorized_candidates(
                query,
                scope,
                (),
                authority=revoked,
                authoritative_records={record.record_id: record},
                verified_at=self.now,
            )
        changed = replace(self.authority, matter_version=9)
        with self.assertRaisesRegex(AgentMemoryBlocked, "no longer matches"):
            rank_authorized_candidates(
                query,
                scope,
                (),
                authority=changed,
                authoritative_records={record.record_id: record},
                verified_at=self.now,
            )

    def test_working_memory_is_bound_to_owner_run_and_task(self) -> None:
        run_id = str(uuid4())
        task_id = str(uuid4())
        record = self.case_record(
            layer=MemoryLayer.RUN_WORKING,
            run_id=run_id,
            task_id=task_id,
            owner_actor_id=self.actor_id,
        )
        hits = self.search(
            self.query(
                layers=(MemoryLayer.RUN_WORKING,), run_id=run_id, task_id=task_id
            ),
            (record,),
        )
        self.assertEqual(tuple(item.record_id for item in hits), (record.record_id,))
        with self.assertRaisesRegex(AgentMemoryBlocked, "no memory records"):
            authorize_retrieval_scope(
                query=self.query(
                    layers=(MemoryLayer.RUN_WORKING,),
                    run_id=str(uuid4()),
                    task_id=task_id,
                ),
                authority=self.authority,
                authoritative_records=(record,),
                authorized_at=self.now,
            )

    def test_authoritative_tombstone_or_new_version_wins_over_old_index(self) -> None:
        record = self.case_record()
        query = self.query()
        scope = authorize_retrieval_scope(
            query=query,
            authority=self.authority,
            authoritative_records=(record,),
            authorized_at=self.now,
        )
        candidate = SimilaritySearchCandidate(
            record.record_id, record.record_version, record.content_hash, 1, 1
        )
        revoked = replace(record, record_version=2, status=MemoryStatus.REVOKED)
        hits = rank_authorized_candidates(
            query,
            scope,
            (candidate,),
            authority=self.authority,
            authoritative_records={record.record_id: revoked},
            verified_at=self.now,
        )
        self.assertEqual(hits, ())

    def test_cross_case_publication_cites_only_sanitized_derived_object(self) -> None:
        sanitized = MemorySourceRef(
            source_type="PUBLISHED_KNOWLEDGE_OBJECT",
            source_id=str(uuid4()),
            source_version="1",
            content_hash=digest("sanitized"),
            location_kind=SourceLocationKind.OBJECT,
            exposure=SourceExposure.PUBLISHED_SANITIZED,
        )
        personal = replace(
            self.case_record(),
            layer=MemoryLayer.LAWYER_PERSONAL,
            status=MemoryStatus.PUBLISHED,
            authority=MemoryAuthority.LAWYER_NOTE,
            source_refs=(sanitized,),
            matter_id=None,
            owner_actor_id=self.actor_id,
            permission_group_ids=(),
            publication_approval_hash=digest("approval"),
        )
        self.assertEqual(personal.source_refs, (sanitized,))
        with self.assertRaisesRegex(AgentMemoryBlocked, "sanitized published object"):
            replace(personal, source_refs=(self.source,))

    def test_historical_law_uses_legal_period_and_knowledge_time(self) -> None:
        official = MemorySourceRef(
            source_type="OFFICIAL_LEGAL_SNAPSHOT",
            source_id=str(uuid4()),
            source_version="1",
            content_hash=digest("law"),
            location_kind=SourceLocationKind.WEB_FRAGMENT,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            source_url="https://example.gov.cn/law/1",
        )
        law = replace(
            self.case_record(layer=MemoryLayer.RUN_WORKING, run_id=str(uuid4()), owner_actor_id=self.actor_id),
            layer=MemoryLayer.PUBLIC_LEGAL,
            status=MemoryStatus.PUBLISHED,
            authority=MemoryAuthority.JUDICIAL_INTERPRETATION,
            source_refs=(official,),
            firm_id=None,
            matter_id=None,
            owner_actor_id=None,
            case_type_codes=("PRIVATE_LENDING",),
            procedure_stages=("FIRST_INSTANCE",),
            effective_from=date(2015, 9, 1),
            effective_to=date(2020, 8, 19),
            publication_approval_hash=digest("official-review"),
            permission_group_ids=(),
            run_id=None,
            task_id=None,
            source_authority_registry_hash=digest("official-source-registry-v1"),
        )
        historical = self.search(
            self.query(
                layers=(MemoryLayer.PUBLIC_LEGAL,),
                legal_period_start=date(2019, 6, 17),
                legal_period_end=date(2019, 10, 19),
            ),
            (law,),
        )
        self.assertEqual(tuple(item.record_id for item in historical), (law.record_id,))
        with self.assertRaisesRegex(AgentMemoryBlocked, "no memory records"):
            authorize_retrieval_scope(
                query=self.query(
                    layers=(MemoryLayer.PUBLIC_LEGAL,),
                    legal_period_start=date(2021, 1, 1),
                    legal_period_end=date(2021, 12, 31),
                ),
                authority=self.authority,
                authoritative_records=(law,),
                authorized_at=self.now,
            )

    def test_publication_requires_reviews_and_explicit_firm_scope(self) -> None:
        arguments = dict(
            publication_id=str(uuid4()),
            source_firm_id=self.firm_id,
            target=PublicationTarget.LAWYER_PERSONAL,
            source_matter_id=self.matter_id,
            source_object_hashes=(digest("selected"),),
            published_content_hash=digest("redacted"),
            published_source_object_id=str(uuid4()),
            published_source_object_hash=digest("redacted-object"),
            provenance_hash=digest("publication-provenance"),
            owner_actor_id=self.actor_id,
            permission_group_ids=(),
            anonymization_review=PublicationReview.PASSED,
            conflict_review=PublicationReview.PASSED,
            confidentiality_review=PublicationReview.PASSED,
            approved_by=str(uuid4()),
            second_approver_id=None,
            publication_policy_version="1.0.0",
            publication_policy_hash=digest("publication-policy"),
            approved_at=self.now,
        )
        KnowledgePublicationCandidate.build(**arguments)
        with self.assertRaisesRegex(AgentMemoryBlocked, "all protected reviews"):
            KnowledgePublicationCandidate.build(
                **{**arguments, "confidentiality_review": PublicationReview.PENDING}
            )
        with self.assertRaisesRegex(AgentMemoryBlocked, "permission group"):
            KnowledgePublicationCandidate.build(
                **{
                    **arguments,
                    "target": PublicationTarget.FIRM_KNOWLEDGE,
                    "owner_actor_id": None,
                }
            )

    def test_image_memory_requires_source_coordinates(self) -> None:
        with self.assertRaisesRegex(AgentMemoryBlocked, "normalized box"):
            replace(self.source, normalized_box=None)

    def test_checkpoint_chain_is_deterministic_and_contiguous(self) -> None:
        run_id = str(uuid4())
        first = RunMemoryCheckpoint.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=self.actor_id,
            run_id=run_id,
            sequence=1,
            previous_checkpoint_hash=None,
            case_snapshot_hash=digest("case-1"),
            plan_hash=digest("plan-1"),
            task_state_hash=digest("tasks-1"),
            unresolved_question_ids=(),
            retrieval_scope_hashes=(),
            occurred_at=self.now,
        )
        second = RunMemoryCheckpoint.build(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            owner_actor_id=self.actor_id,
            run_id=run_id,
            sequence=2,
            previous_checkpoint_hash=first.checkpoint_hash,
            case_snapshot_hash=digest("case-2"),
            plan_hash=digest("plan-1"),
            task_state_hash=digest("tasks-2"),
            unresolved_question_ids=(),
            retrieval_scope_hashes=(),
            occurred_at=self.now,
        )
        self.assertEqual(checkpoint_chain_hash((first, second)), second.checkpoint_hash)
        self.assertEqual(checkpoint_chain_hash((second, first)), second.checkpoint_hash)
        with self.assertRaisesRegex(AgentMemoryBlocked, "contiguous"):
            checkpoint_chain_hash(
                (
                    first,
                    RunMemoryCheckpoint.build(
                        firm_id=self.firm_id,
                        matter_id=self.matter_id,
                        owner_actor_id=self.actor_id,
                        run_id=run_id,
                        sequence=3,
                        previous_checkpoint_hash=first.checkpoint_hash,
                        case_snapshot_hash=digest("case-3"),
                        plan_hash=digest("plan-1"),
                        task_state_hash=digest("tasks-3"),
                        unresolved_question_ids=(),
                        retrieval_scope_hashes=(),
                        occurred_at=self.now,
                    ),
                )
            )

    def test_public_legal_authority_requires_registered_source_type(self) -> None:
        official = MemorySourceRef(
            source_type="PUBLIC_RESEARCH_LEAD",
            source_id=str(uuid4()),
            source_version="1",
            content_hash=digest("lead"),
            location_kind=SourceLocationKind.WEB_FRAGMENT,
            exposure=SourceExposure.PUBLIC_OFFICIAL,
            source_url="https://example.gov.cn/lead/1",
        )
        with self.assertRaisesRegex(AgentMemoryBlocked, "registered source type"):
            replace(
                self.case_record(),
                layer=MemoryLayer.PUBLIC_LEGAL,
                status=MemoryStatus.PUBLISHED,
                authority=MemoryAuthority.PRIMARY_LAW,
                source_refs=(official,),
                firm_id=None,
                matter_id=None,
                owner_actor_id=None,
                permission_group_ids=(),
                effective_from=date(2026, 1, 1),
                publication_approval_hash=digest("official-review"),
                source_authority_registry_hash=digest("registry"),
            )


if __name__ == "__main__":
    unittest.main()
