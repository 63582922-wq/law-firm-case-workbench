"""ACL-first memory and retrieval contracts for the lawyer Agent OS.

This module deliberately contains no embedding provider and no model prompt.
PostgreSQL ledgers and managed objects remain the sources of truth; full-text
and vector indexes are disposable accelerators.  A retrieval backend must
apply :class:`RetrievalAccessFilter` before similarity search, and this module
revalidates every returned candidate before ranking it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from typing import Iterable, Mapping
from uuid import UUID

from .models import Actor, Role


class AgentMemoryBlocked(PermissionError):
    """Memory content or a retrieval result crossed a governed boundary."""


class MemoryLayer(StrEnum):
    RUN_WORKING = "RUN_WORKING"
    CASE_LONG_TERM = "CASE_LONG_TERM"
    LAWYER_PERSONAL = "LAWYER_PERSONAL"
    FIRM_KNOWLEDGE = "FIRM_KNOWLEDGE"
    PUBLIC_LEGAL = "PUBLIC_LEGAL"


class MemoryStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    DELETED = "DELETED"


class MemoryAuthority(StrEnum):
    CASE_EVIDENCE = "CASE_EVIDENCE"
    CONFIRMED_CASE_LEDGER = "CONFIRMED_CASE_LEDGER"
    LAWYER_NOTE = "LAWYER_NOTE"
    FIRM_SOP = "FIRM_SOP"
    PRIMARY_LAW = "PRIMARY_LAW"
    JUDICIAL_INTERPRETATION = "JUDICIAL_INTERPRETATION"
    OFFICIAL_CASE = "OFFICIAL_CASE"
    PUBLIC_RESEARCH_LEAD = "PUBLIC_RESEARCH_LEAD"


_CASE_AUTHORITIES = frozenset(
    {MemoryAuthority.CASE_EVIDENCE, MemoryAuthority.CONFIRMED_CASE_LEDGER}
)
_PERSONAL_AUTHORITIES = frozenset({MemoryAuthority.LAWYER_NOTE})
_FIRM_AUTHORITIES = frozenset({MemoryAuthority.FIRM_SOP})
_PUBLIC_AUTHORITIES = frozenset(
    {
        MemoryAuthority.PRIMARY_LAW,
        MemoryAuthority.JUDICIAL_INTERPRETATION,
        MemoryAuthority.OFFICIAL_CASE,
        MemoryAuthority.PUBLIC_RESEARCH_LEAD,
    }
)

_PUBLIC_SOURCE_TYPES: Mapping[MemoryAuthority, frozenset[str]] = {
    MemoryAuthority.PRIMARY_LAW: frozenset({"OFFICIAL_LEGAL_SNAPSHOT"}),
    MemoryAuthority.JUDICIAL_INTERPRETATION: frozenset(
        {"OFFICIAL_LEGAL_SNAPSHOT"}
    ),
    MemoryAuthority.OFFICIAL_CASE: frozenset({"OFFICIAL_CASE_SNAPSHOT"}),
    MemoryAuthority.PUBLIC_RESEARCH_LEAD: frozenset({"PUBLIC_RESEARCH_LEAD"}),
}


class SourceLocationKind(StrEnum):
    OBJECT = "OBJECT"
    DOCUMENT_PAGE = "DOCUMENT_PAGE"
    IMAGE_REGION = "IMAGE_REGION"
    PARAGRAPH = "PARAGRAPH"
    SHEET_RANGE = "SHEET_RANGE"
    MEDIA_TIME_RANGE = "MEDIA_TIME_RANGE"
    WEB_FRAGMENT = "WEB_FRAGMENT"


class SourceExposure(StrEnum):
    CASE_PRIVATE = "CASE_PRIVATE"
    PUBLISHED_SANITIZED = "PUBLISHED_SANITIZED"
    PUBLIC_OFFICIAL = "PUBLIC_OFFICIAL"


class PublicationTarget(StrEnum):
    LAWYER_PERSONAL = "LAWYER_PERSONAL"
    FIRM_KNOWLEDGE = "FIRM_KNOWLEDGE"


class PublicationReview(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PENDING = "PENDING"


class MetadataFilterMode(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"
    IGNORE = "IGNORE"


@dataclass(frozen=True)
class MemorySourceRef:
    source_type: str
    source_id: str
    source_version: str
    content_hash: str
    location_kind: SourceLocationKind
    exposure: SourceExposure
    page_number: int | None = None
    normalized_box: tuple[float, float, float, float] | None = None
    paragraph_label: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    start_millis: int | None = None
    end_millis: int | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        _required_code(self.source_type, "source_type")
        _required_text(self.source_id, "source_id", 300)
        _required_text(self.source_version, "source_version", 300)
        _required_sha256(self.content_hash, "content_hash")
        if self.location_kind in {SourceLocationKind.DOCUMENT_PAGE, SourceLocationKind.IMAGE_REGION}:
            if self.page_number is None or self.page_number < 1:
                raise AgentMemoryBlocked("page-bound memory requires a positive page number")
        elif self.page_number is not None:
            raise AgentMemoryBlocked("only page-bound memory may carry a page number")
        if self.location_kind is SourceLocationKind.IMAGE_REGION:
            _validate_box(self.normalized_box)
        elif self.normalized_box is not None:
            raise AgentMemoryBlocked("only image-region memory may carry a normalized box")
        if self.location_kind is SourceLocationKind.PARAGRAPH:
            _required_text(self.paragraph_label or "", "paragraph_label", 200)
        elif self.paragraph_label is not None:
            raise AgentMemoryBlocked("only paragraph memory may carry a paragraph label")
        if self.location_kind is SourceLocationKind.SHEET_RANGE:
            _required_text(self.sheet_name or "", "sheet_name", 200)
            _required_text(self.cell_range or "", "cell_range", 100)
        elif self.sheet_name is not None or self.cell_range is not None:
            raise AgentMemoryBlocked("only sheet-range memory may carry sheet coordinates")
        if self.location_kind is SourceLocationKind.MEDIA_TIME_RANGE:
            if (
                self.start_millis is None
                or self.end_millis is None
                or self.start_millis < 0
                or self.end_millis <= self.start_millis
            ):
                raise AgentMemoryBlocked("media memory requires a valid time range")
        elif self.start_millis is not None or self.end_millis is not None:
            raise AgentMemoryBlocked("only media memory may carry a time range")
        if self.location_kind is SourceLocationKind.WEB_FRAGMENT:
            if not isinstance(self.source_url, str) or not self.source_url.startswith("https://"):
                raise AgentMemoryBlocked("Web memory requires a captured HTTPS source URL")
        elif self.source_url is not None:
            raise AgentMemoryBlocked("only Web memory may carry a source URL")


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    record_version: int
    layer: MemoryLayer
    status: MemoryStatus
    authority: MemoryAuthority
    content_hash: str
    source_refs: tuple[MemorySourceRef, ...]
    firm_id: str | None
    matter_id: str | None
    owner_actor_id: str | None
    run_id: str | None
    task_id: str | None
    permission_group_ids: tuple[str, ...]
    case_type_codes: tuple[str, ...]
    procedure_stages: tuple[str, ...]
    issue_tags: tuple[str, ...]
    effective_from: date | None
    effective_to: date | None
    known_from: datetime
    known_to: datetime | None
    publication_approval_hash: str | None
    provenance_hash: str
    updated_at: datetime
    source_authority_registry_hash: str | None = None

    def __post_init__(self) -> None:
        _required_uuid(self.record_id, "record_id")
        if self.record_version < 1:
            raise AgentMemoryBlocked("memory record version must be positive")
        _required_sha256(self.content_hash, "content_hash")
        _required_sha256(self.provenance_hash, "provenance_hash")
        if not self.source_refs:
            raise AgentMemoryBlocked("memory must cite at least one source object")
        if self.updated_at.tzinfo is None:
            raise AgentMemoryBlocked("memory updated_at must include a timezone")
        if self.known_from.tzinfo is None or (
            self.known_to is not None and self.known_to.tzinfo is None
        ):
            raise AgentMemoryBlocked("memory knowledge times must include a timezone")
        if self.known_to is not None and self.known_to <= self.known_from:
            raise AgentMemoryBlocked("memory knowledge period is inverted")
        if self.updated_at < self.known_from:
            raise AgentMemoryBlocked("memory cannot update before it is known")
        _unique_codes(self.permission_group_ids, "permission_group_ids", uuid_values=True)
        _unique_codes(self.case_type_codes, "case_type_codes")
        _unique_codes(self.procedure_stages, "procedure_stages")
        _unique_codes(self.issue_tags, "issue_tags")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise AgentMemoryBlocked("memory effective period is inverted")
        self._validate_layer_scope()

    def _validate_layer_scope(self) -> None:
        if self.layer in {MemoryLayer.RUN_WORKING, MemoryLayer.CASE_LONG_TERM}:
            if self.source_authority_registry_hash is not None:
                raise AgentMemoryBlocked("case memory cannot claim a public-source registration")
            if self.authority not in _CASE_AUTHORITIES:
                raise AgentMemoryBlocked("case memory has an invalid authority tier")
            _required_uuid(self.firm_id, "firm_id")
            _required_uuid(self.matter_id, "matter_id")
            if self.layer is MemoryLayer.RUN_WORKING:
                _required_uuid(self.owner_actor_id, "owner_actor_id")
                _required_uuid(self.run_id, "run_id")
                if self.task_id is not None:
                    _required_uuid(self.task_id, "task_id")
                if self.status not in {
                    MemoryStatus.CANDIDATE,
                    MemoryStatus.CONFIRMED,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.REVOKED,
                    MemoryStatus.DELETED,
                }:
                    raise AgentMemoryBlocked("working memory cannot be published across tasks")
            else:
                if self.status not in {
                    MemoryStatus.CONFIRMED,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.REVOKED,
                    MemoryStatus.DELETED,
                }:
                    raise AgentMemoryBlocked(
                        "case long-term memory must come from a governed case object"
                    )
                if self.owner_actor_id is not None or self.run_id is not None or self.task_id is not None:
                    raise AgentMemoryBlocked("case long-term memory cannot retain a private run scope")
            if self.publication_approval_hash is not None:
                raise AgentMemoryBlocked("same-case memory does not use a cross-case publication approval")
            if any(source.exposure is not SourceExposure.CASE_PRIVATE for source in self.source_refs):
                raise AgentMemoryBlocked("same-case memory must cite private same-case sources")
            if self.layer is MemoryLayer.CASE_LONG_TERM and not self.permission_group_ids:
                raise AgentMemoryBlocked(
                    "same-case memory requires an explicit access group"
                )
            return
        if self.layer is MemoryLayer.LAWYER_PERSONAL:
            if self.source_authority_registry_hash is not None:
                raise AgentMemoryBlocked("personal knowledge cannot claim a public-source registration")
            if self.authority not in _PERSONAL_AUTHORITIES:
                raise AgentMemoryBlocked("personal knowledge has an invalid authority tier")
            _required_uuid(self.firm_id, "firm_id")
            _required_uuid(self.owner_actor_id, "owner_actor_id")
            if self.matter_id is not None or self.permission_group_ids:
                raise AgentMemoryBlocked("personal knowledge cannot expose a source matter or firm group")
            if self.run_id is not None or self.task_id is not None:
                raise AgentMemoryBlocked("personal knowledge cannot retain a source run")
            self._require_published()
            self._require_sanitized_publication_sources()
            return
        if self.layer is MemoryLayer.FIRM_KNOWLEDGE:
            if self.source_authority_registry_hash is not None:
                raise AgentMemoryBlocked("firm knowledge cannot claim a public-source registration")
            if self.authority not in _FIRM_AUTHORITIES:
                raise AgentMemoryBlocked("firm knowledge has an invalid authority tier")
            _required_uuid(self.firm_id, "firm_id")
            if self.matter_id is not None or self.owner_actor_id is not None:
                raise AgentMemoryBlocked("firm knowledge cannot expose a source matter or personal owner")
            if self.run_id is not None or self.task_id is not None:
                raise AgentMemoryBlocked("firm knowledge cannot retain a source run")
            self._require_published()
            self._require_sanitized_publication_sources()
            return
        if self.layer is MemoryLayer.PUBLIC_LEGAL:
            if any((self.firm_id, self.matter_id, self.owner_actor_id, self.permission_group_ids)):
                raise AgentMemoryBlocked("public legal memory cannot carry tenant identifiers")
            if self.run_id is not None or self.task_id is not None:
                raise AgentMemoryBlocked("public legal memory cannot carry a task scope")
            if self.authority not in _PUBLIC_AUTHORITIES:
                raise AgentMemoryBlocked("public legal memory has an invalid authority tier")
            self._require_published()
            if any(source.exposure is not SourceExposure.PUBLIC_OFFICIAL for source in self.source_refs):
                raise AgentMemoryBlocked("public legal memory must cite captured public sources")
            _required_sha256(
                self.source_authority_registry_hash or "",
                "source_authority_registry_hash",
            )
            allowed_source_types = _PUBLIC_SOURCE_TYPES[self.authority]
            if any(source.source_type not in allowed_source_types for source in self.source_refs):
                raise AgentMemoryBlocked(
                    "public legal authority is not bound to its registered source type"
                )
            if self.authority in {
                MemoryAuthority.PRIMARY_LAW,
                MemoryAuthority.JUDICIAL_INTERPRETATION,
            } and self.effective_from is None:
                raise AgentMemoryBlocked(
                    "primary law and judicial interpretations require an effective date"
                )

    def _require_published(self) -> None:
        if self.status not in {
            MemoryStatus.PUBLISHED,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.DELETED,
        }:
            raise AgentMemoryBlocked(
                "cross-task knowledge must originate from an explicit publication"
            )
        _required_sha256(self.publication_approval_hash or "", "publication_approval_hash")

    def _require_sanitized_publication_sources(self) -> None:
        if any(
            source.exposure is not SourceExposure.PUBLISHED_SANITIZED
            or source.source_type != "PUBLISHED_KNOWLEDGE_OBJECT"
            for source in self.source_refs
        ):
            raise AgentMemoryBlocked(
                "cross-case knowledge may cite only a sanitized published object"
            )


@dataclass(frozen=True)
class VerifiedRetrievalPrincipal:
    actor: Actor
    matter_id: str
    matter_access_grant_hash: str
    matter_version: int
    permission_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_uuid(self.actor.actor_id, "actor_id")
        _required_uuid(self.actor.firm_id, "firm_id")
        _required_uuid(self.matter_id, "matter_id")
        _required_sha256(self.matter_access_grant_hash, "matter_access_grant_hash")
        if self.matter_version < 1:
            raise AgentMemoryBlocked("retrieval principal requires a current matter version")
        if not self.actor.roles.intersection(
            {
                Role.ASSISTANT,
                Role.COLLABORATING_LAWYER,
                Role.LEAD_LAWYER,
                Role.REVIEWER,
            }
        ):
            raise AgentMemoryBlocked("retrieval principal requires a human case role")
        _unique_codes(self.permission_group_ids, "permission_group_ids", uuid_values=True)


@dataclass(frozen=True)
class RetrievalAuthoritySnapshot:
    """Fresh server-side authorization state used before and after search."""

    actor_id: str
    firm_id: str
    matter_id: str
    matter_access_grant_hash: str
    matter_version: int
    permission_group_ids: tuple[str, ...]
    access_active: bool
    checked_at: datetime

    def __post_init__(self) -> None:
        _required_uuid(self.actor_id, "actor_id")
        _required_uuid(self.firm_id, "firm_id")
        _required_uuid(self.matter_id, "matter_id")
        _required_sha256(self.matter_access_grant_hash, "matter_access_grant_hash")
        if self.matter_version < 1:
            raise AgentMemoryBlocked("authority snapshot requires a current matter version")
        _unique_codes(self.permission_group_ids, "permission_group_ids", uuid_values=True)
        if self.checked_at.tzinfo is None:
            raise AgentMemoryBlocked("authority checked_at must include a timezone")


@dataclass(frozen=True)
class RetrievalAccessFilter:
    firm_id: str
    matter_id: str
    actor_id: str
    matter_access_grant_hash: str
    matter_version: int
    permission_group_ids: tuple[str, ...]
    layers: tuple[MemoryLayer, ...]
    run_id: str | None
    task_id: str | None
    knowledge_as_of: datetime
    legal_period_start: date | None
    legal_period_end: date | None
    case_type_codes: tuple[str, ...]
    procedure_stages: tuple[str, ...]
    issue_tags: tuple[str, ...]
    case_type_mode: MetadataFilterMode
    procedure_stage_mode: MetadataFilterMode
    issue_tag_mode: MetadataFilterMode


@dataclass(frozen=True)
class MemoryQuery:
    query_text: str
    access: RetrievalAccessFilter
    max_results: int = 20

    def __post_init__(self) -> None:
        _required_text(self.query_text, "query_text", 4_000)
        if not 1 <= self.max_results <= 100:
            raise AgentMemoryBlocked("memory result limit must be between 1 and 100")


@dataclass(frozen=True)
class AuthorizedMemoryRecordRef:
    record_id: str
    record_version: int
    content_hash: str
    provenance_hash: str

    def __post_init__(self) -> None:
        _required_uuid(self.record_id, "record_id")
        if self.record_version < 1:
            raise AgentMemoryBlocked("authorized record version must be positive")
        _required_sha256(self.content_hash, "content_hash")
        _required_sha256(self.provenance_hash, "provenance_hash")


@dataclass(frozen=True)
class AuthorizedRetrievalScope:
    query_fingerprint: str
    authorized_records: tuple[AuthorizedMemoryRecordRef, ...]
    authorized_at: datetime
    expires_at: datetime
    scope_hash: str

    def __post_init__(self) -> None:
        _required_sha256(self.query_fingerprint, "query_fingerprint")
        _required_sha256(self.scope_hash, "scope_hash")
        if not self.authorized_records:
            raise AgentMemoryBlocked("authorized retrieval scope cannot be empty")
        if self.authorized_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise AgentMemoryBlocked("retrieval scope times must include a timezone")
        if self.expires_at <= self.authorized_at:
            raise AgentMemoryBlocked("retrieval scope expiry is invalid")


@dataclass(frozen=True)
class SimilaritySearchCandidate:
    """Scores only; a search index never supplies authoritative record state."""

    record_id: str
    record_version: int
    content_hash: str
    lexical_score: float
    vector_score: float

    def __post_init__(self) -> None:
        _required_uuid(self.record_id, "record_id")
        if self.record_version < 1:
            raise AgentMemoryBlocked("search candidate version must be positive")
        _required_sha256(self.content_hash, "content_hash")
        _unit_score(self.lexical_score, "lexical_score")
        _unit_score(self.vector_score, "vector_score")


@dataclass(frozen=True)
class RetrievedMemoryHit:
    record_id: str
    layer: MemoryLayer
    content_hash: str
    source_refs: tuple[MemorySourceRef, ...]
    authority: MemoryAuthority
    status: MemoryStatus
    combined_score: float
    retrieval_reason: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgePublicationCandidate:
    publication_id: str
    source_firm_id: str
    target: PublicationTarget
    source_matter_id: str
    source_object_hashes: tuple[str, ...]
    published_content_hash: str
    published_source_object_id: str
    published_source_object_hash: str
    provenance_hash: str
    owner_actor_id: str | None
    permission_group_ids: tuple[str, ...]
    anonymization_review: PublicationReview
    conflict_review: PublicationReview
    confidentiality_review: PublicationReview
    approved_by: str
    second_approver_id: str | None
    publication_policy_version: str
    publication_policy_hash: str
    approved_at: datetime
    approval_hash: str

    @classmethod
    def build(
        cls,
        *,
        publication_id: str,
        source_firm_id: str,
        target: PublicationTarget,
        source_matter_id: str,
        source_object_hashes: tuple[str, ...],
        published_content_hash: str,
        published_source_object_id: str,
        published_source_object_hash: str,
        provenance_hash: str,
        owner_actor_id: str | None,
        permission_group_ids: tuple[str, ...],
        anonymization_review: PublicationReview,
        conflict_review: PublicationReview,
        confidentiality_review: PublicationReview,
        approved_by: str,
        second_approver_id: str | None,
        publication_policy_version: str,
        publication_policy_hash: str,
        approved_at: datetime,
    ) -> "KnowledgePublicationCandidate":
        values = {
            "publication_id": publication_id,
            "source_firm_id": source_firm_id,
            "target": target,
            "source_matter_id": source_matter_id,
            "source_object_hashes": tuple(source_object_hashes),
            "published_content_hash": published_content_hash,
            "published_source_object_id": published_source_object_id,
            "published_source_object_hash": published_source_object_hash,
            "provenance_hash": provenance_hash,
            "owner_actor_id": owner_actor_id,
            "permission_group_ids": tuple(permission_group_ids),
            "anonymization_review": anonymization_review,
            "conflict_review": conflict_review,
            "confidentiality_review": confidentiality_review,
            "approved_by": approved_by,
            "second_approver_id": second_approver_id,
            "publication_policy_version": publication_policy_version,
            "publication_policy_hash": publication_policy_hash,
            "approved_at": approved_at,
        }
        provisional = cls.__new__(cls)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(
            provisional,
            "approval_hash",
            _knowledge_publication_approval_hash(provisional),
        )
        provisional.__post_init__()
        return provisional

    def __post_init__(self) -> None:
        _required_uuid(self.publication_id, "publication_id")
        _required_uuid(self.source_firm_id, "source_firm_id")
        _required_uuid(self.source_matter_id, "source_matter_id")
        _required_uuid(self.approved_by, "approved_by")
        _required_sha256(self.published_content_hash, "published_content_hash")
        _required_uuid(self.published_source_object_id, "published_source_object_id")
        _required_sha256(self.published_source_object_hash, "published_source_object_hash")
        _required_sha256(self.provenance_hash, "provenance_hash")
        _required_sha256(self.approval_hash, "approval_hash")
        _required_version(self.publication_policy_version, "publication_policy_version")
        _required_sha256(self.publication_policy_hash, "publication_policy_hash")
        if self.approved_at.tzinfo is None:
            raise AgentMemoryBlocked("knowledge publication approval time requires a timezone")
        if not self.source_object_hashes:
            raise AgentMemoryBlocked("knowledge publication requires selected source objects")
        for value in self.source_object_hashes:
            _required_sha256(value, "source_object_hash")
        if any(
            review is not PublicationReview.PASSED
            for review in (
                self.anonymization_review,
                self.conflict_review,
                self.confidentiality_review,
            )
        ):
            raise AgentMemoryBlocked("knowledge cannot publish before all protected reviews pass")
        if self.target is PublicationTarget.LAWYER_PERSONAL:
            _required_uuid(self.owner_actor_id, "owner_actor_id")
            if self.approved_by == self.owner_actor_id:
                raise AgentMemoryBlocked("personal knowledge publication requires independent review")
            if self.second_approver_id is not None:
                _required_uuid(self.second_approver_id, "second_approver_id")
            if self.permission_group_ids:
                raise AgentMemoryBlocked("personal publication cannot grant firm groups")
        else:
            if self.owner_actor_id is not None:
                raise AgentMemoryBlocked("firm publication cannot retain a personal owner")
            _unique_codes(self.permission_group_ids, "permission_group_ids", uuid_values=True)
            if not self.permission_group_ids:
                raise AgentMemoryBlocked(
                    "firm publication requires an explicit permission group"
                )
            _required_uuid(self.second_approver_id, "second_approver_id")
            if self.second_approver_id == self.approved_by:
                raise AgentMemoryBlocked("firm publication requires two distinct reviewers")
        expected_hash = _knowledge_publication_approval_hash(self)
        if self.approval_hash != expected_hash:
            raise AgentMemoryBlocked("knowledge publication approval receipt is not bound to its inputs")


@dataclass(frozen=True)
class RunMemoryCheckpoint:
    firm_id: str
    matter_id: str
    owner_actor_id: str
    run_id: str
    sequence: int
    previous_checkpoint_hash: str | None
    case_snapshot_hash: str
    plan_hash: str
    task_state_hash: str
    unresolved_question_ids: tuple[str, ...]
    retrieval_scope_hashes: tuple[str, ...]
    occurred_at: datetime
    checkpoint_hash: str

    def __post_init__(self) -> None:
        _required_uuid(self.firm_id, "checkpoint firm_id")
        _required_uuid(self.matter_id, "checkpoint matter_id")
        _required_uuid(self.owner_actor_id, "checkpoint owner_actor_id")
        _required_uuid(self.run_id, "run_id")
        if self.sequence < 1:
            raise AgentMemoryBlocked("checkpoint sequence must be positive")
        for label, value in (
            ("case_snapshot_hash", self.case_snapshot_hash),
            ("plan_hash", self.plan_hash),
            ("task_state_hash", self.task_state_hash),
        ):
            _required_sha256(value, label)
        if self.sequence == 1:
            if self.previous_checkpoint_hash is not None:
                raise AgentMemoryBlocked("the first checkpoint cannot have a previous hash")
        else:
            _required_sha256(
                self.previous_checkpoint_hash or "", "previous_checkpoint_hash"
            )
        _unique_codes(self.unresolved_question_ids, "unresolved_question_ids", uuid_values=True)
        if len(self.retrieval_scope_hashes) > 500 or len(set(self.retrieval_scope_hashes)) != len(
            self.retrieval_scope_hashes
        ):
            raise AgentMemoryBlocked("checkpoint retrieval scopes must be unique and bounded")
        for value in self.retrieval_scope_hashes:
            _required_sha256(value, "retrieval_scope_hash")
        if self.occurred_at.tzinfo is None:
            raise AgentMemoryBlocked("checkpoint occurred_at must include a timezone")
        _required_sha256(self.checkpoint_hash, "checkpoint_hash")
        if self.checkpoint_hash != _checkpoint_hash(self):
            raise AgentMemoryBlocked("checkpoint hash is not bound to its content")

    @classmethod
    def build(
        cls,
        *,
        firm_id: str,
        matter_id: str,
        owner_actor_id: str,
        run_id: str,
        sequence: int,
        previous_checkpoint_hash: str | None,
        case_snapshot_hash: str,
        plan_hash: str,
        task_state_hash: str,
        unresolved_question_ids: tuple[str, ...],
        retrieval_scope_hashes: tuple[str, ...],
        occurred_at: datetime,
    ) -> "RunMemoryCheckpoint":
        provisional = cls.__new__(cls)
        values = {
            "firm_id": firm_id,
            "matter_id": matter_id,
            "owner_actor_id": owner_actor_id,
            "run_id": run_id,
            "sequence": sequence,
            "previous_checkpoint_hash": previous_checkpoint_hash,
            "case_snapshot_hash": case_snapshot_hash,
            "plan_hash": plan_hash,
            "task_state_hash": task_state_hash,
            "unresolved_question_ids": tuple(unresolved_question_ids),
            "retrieval_scope_hashes": tuple(retrieval_scope_hashes),
            "occurred_at": occurred_at,
        }
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(provisional, "checkpoint_hash", _checkpoint_hash(provisional))
        provisional.__post_init__()
        return provisional


def build_retrieval_query(
    *,
    principal: VerifiedRetrievalPrincipal,
    query_text: str,
    layers: tuple[MemoryLayer, ...],
    knowledge_as_of: datetime,
    legal_period_start: date | None = None,
    legal_period_end: date | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    case_type_codes: tuple[str, ...] = (),
    procedure_stages: tuple[str, ...] = (),
    issue_tags: tuple[str, ...] = (),
    case_type_mode: MetadataFilterMode = MetadataFilterMode.HARD,
    procedure_stage_mode: MetadataFilterMode = MetadataFilterMode.HARD,
    issue_tag_mode: MetadataFilterMode = MetadataFilterMode.SOFT,
    max_results: int = 20,
) -> MemoryQuery:
    if not layers or len(set(layers)) != len(layers):
        raise AgentMemoryBlocked("retrieval layers must be non-empty and unique")
    if knowledge_as_of.tzinfo is None:
        raise AgentMemoryBlocked("knowledge_as_of must include a timezone")
    if legal_period_start and legal_period_end and legal_period_end < legal_period_start:
        raise AgentMemoryBlocked("legal retrieval period is inverted")
    if MemoryLayer.RUN_WORKING in layers:
        _required_uuid(run_id, "run_id")
        if task_id is not None:
            _required_uuid(task_id, "task_id")
    elif run_id is not None or task_id is not None:
        raise AgentMemoryBlocked("run/task filters require the working-memory layer")
    if MemoryLayer.PUBLIC_LEGAL in layers and (
        legal_period_start is None or legal_period_end is None
    ):
        raise AgentMemoryBlocked(
            "public legal retrieval requires an explicit legal-validity period"
        )
    return MemoryQuery(
        query_text=query_text,
        access=RetrievalAccessFilter(
            firm_id=principal.actor.firm_id,
            matter_id=principal.matter_id,
            actor_id=principal.actor.actor_id,
            matter_access_grant_hash=principal.matter_access_grant_hash,
            matter_version=principal.matter_version,
            permission_group_ids=tuple(sorted(principal.permission_group_ids)),
            layers=layers,
            run_id=run_id,
            task_id=task_id,
            knowledge_as_of=knowledge_as_of,
            legal_period_start=legal_period_start,
            legal_period_end=legal_period_end,
            case_type_codes=_unique_codes(case_type_codes, "case_type_codes"),
            procedure_stages=_unique_codes(procedure_stages, "procedure_stages"),
            issue_tags=_unique_codes(issue_tags, "issue_tags"),
            case_type_mode=case_type_mode,
            procedure_stage_mode=procedure_stage_mode,
            issue_tag_mode=issue_tag_mode,
        ),
        max_results=max_results,
    )


def authorize_retrieval_scope(
    *,
    query: MemoryQuery,
    authority: RetrievalAuthoritySnapshot,
    authoritative_records: Iterable[MemoryRecord],
    authorized_at: datetime,
    ttl_seconds: int = 30,
) -> AuthorizedRetrievalScope:
    """Build the only input a similarity backend may search.

    The caller must obtain ``authority`` and ``authoritative_records`` in one
    RLS-protected database transaction.  No similarity or embedding operation
    may occur before this scope is produced.
    """

    _validate_fresh_authority(query.access, authority, authorized_at)
    if not 1 <= ttl_seconds <= 60:
        raise AgentMemoryBlocked("retrieval scope TTL must be between 1 and 60 seconds")
    records = tuple(
        sorted(
            (
                AuthorizedMemoryRecordRef(
                    record_id=record.record_id,
                    record_version=record.record_version,
                    content_hash=record.content_hash,
                    provenance_hash=record.provenance_hash,
                )
                for record in authoritative_records
                if _is_accessible(query.access, record)
                and _passes_hard_metadata_filters(query.access, record)
            ),
            key=lambda value: value.record_id,
        )
    )
    if not records:
        raise AgentMemoryBlocked("no memory records are authorized for this query")
    query_fingerprint = _query_fingerprint(query)
    expires_at = authorized_at + timedelta(seconds=ttl_seconds)
    payload = {
        "query_fingerprint": query_fingerprint,
        "grant_hash": authority.matter_access_grant_hash,
        "matter_version": authority.matter_version,
        "permission_groups": authority.permission_group_ids,
        "records": [
            {
                "record_id": item.record_id,
                "record_version": item.record_version,
                "content_hash": item.content_hash,
                "provenance_hash": item.provenance_hash,
            }
            for item in records
        ],
        "authorized_at": authorized_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    scope_hash = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AuthorizedRetrievalScope(
        query_fingerprint=query_fingerprint,
        authorized_records=records,
        authorized_at=authorized_at,
        expires_at=expires_at,
        scope_hash=scope_hash,
    )


def rank_authorized_candidates(
    query: MemoryQuery,
    scope: AuthorizedRetrievalScope,
    candidates: Iterable[SimilaritySearchCandidate],
    *,
    authority: RetrievalAuthoritySnapshot,
    authoritative_records: Mapping[str, MemoryRecord],
    verified_at: datetime,
) -> tuple[RetrievedMemoryHit, ...]:
    """Revalidate current authority and source-of-truth rows after search."""

    _validate_fresh_authority(query.access, authority, verified_at)
    if verified_at < scope.authorized_at:
        raise AgentMemoryBlocked("retrieval verification precedes authorization")
    if verified_at > scope.expires_at:
        raise AgentMemoryBlocked("retrieval scope expired before ranking")
    if scope.query_fingerprint != _query_fingerprint(query):
        raise AgentMemoryBlocked("retrieval scope does not match the query")
    allowed = {
        (item.record_id, item.record_version, item.content_hash): item
        for item in scope.authorized_records
    }

    hits: list[RetrievedMemoryHit] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        candidate_key = (
            candidate.record_id,
            candidate.record_version,
            candidate.content_hash,
        )
        if candidate_key not in allowed:
            raise AgentMemoryBlocked("search backend returned a record outside the ACL scope")
        record = authoritative_records.get(candidate.record_id)
        if record is None:
            continue
        if record.record_id != candidate.record_id:
            raise AgentMemoryBlocked("authoritative memory mapping is inconsistent")
        if (
            record.record_version != candidate.record_version
            or record.content_hash != candidate.content_hash
            or record.provenance_hash != allowed[candidate_key].provenance_hash
        ):
            continue
        if not _is_accessible(query.access, record) or not _passes_hard_metadata_filters(
            query.access, record
        ):
            continue
        identity = (record.record_id, record.content_hash)
        if identity in seen:
            continue
        seen.add(identity)
        metadata_score, reasons = _metadata_match(query.access, record)
        authority_score = _AUTHORITY_SCORE[record.authority]
        combined = (
            candidate.lexical_score * 0.34
            + candidate.vector_score * 0.21
            + authority_score * 0.30
            + metadata_score * 0.15
        )
        hits.append(
            RetrievedMemoryHit(
                record_id=record.record_id,
                layer=record.layer,
                content_hash=record.content_hash,
                source_refs=record.source_refs,
                authority=record.authority,
                status=record.status,
                combined_score=round(combined, 8),
                retrieval_reason=tuple(reasons),
            )
        )
    hits.sort(key=lambda value: (-value.combined_score, value.record_id))
    return tuple(hits[: query.max_results])


def checkpoint_chain_hash(checkpoints: tuple[RunMemoryCheckpoint, ...]) -> str:
    if not checkpoints:
        raise AgentMemoryBlocked("checkpoint chain cannot be empty")
    ordered = tuple(sorted(checkpoints, key=lambda value: value.sequence))
    if tuple(item.sequence for item in ordered) != tuple(range(1, len(ordered) + 1)):
        raise AgentMemoryBlocked("checkpoint chain must be contiguous and start at one")
    if len(
        {
            (item.firm_id, item.matter_id, item.owner_actor_id, item.run_id)
            for item in ordered
        }
    ) != 1:
        raise AgentMemoryBlocked("checkpoint chain cannot cross Agent runs")
    previous: str | None = None
    for item in ordered:
        if item.previous_checkpoint_hash != previous:
            raise AgentMemoryBlocked("checkpoint chain previous hash is broken")
        if item.checkpoint_hash != _checkpoint_hash(item):
            raise AgentMemoryBlocked("checkpoint chain contains a modified record")
        previous = item.checkpoint_hash
    assert previous is not None
    return previous


_AUTHORITY_SCORE = {
    MemoryAuthority.PRIMARY_LAW: 1.0,
    MemoryAuthority.JUDICIAL_INTERPRETATION: 0.97,
    MemoryAuthority.CONFIRMED_CASE_LEDGER: 0.96,
    MemoryAuthority.CASE_EVIDENCE: 0.91,
    MemoryAuthority.OFFICIAL_CASE: 0.83,
    MemoryAuthority.FIRM_SOP: 0.70,
    MemoryAuthority.LAWYER_NOTE: 0.56,
    MemoryAuthority.PUBLIC_RESEARCH_LEAD: 0.30,
}


def _is_accessible(access: RetrievalAccessFilter, record: MemoryRecord) -> bool:
    if record.layer not in access.layers:
        return False
    if record.status in {
        MemoryStatus.SUPERSEDED,
        MemoryStatus.REVOKED,
        MemoryStatus.DELETED,
    }:
        return False
    if record.status is MemoryStatus.CANDIDATE and record.layer is not MemoryLayer.RUN_WORKING:
        return False
    if access.knowledge_as_of < record.known_from:
        return False
    if record.known_to and access.knowledge_as_of >= record.known_to:
        return False
    if record.layer is MemoryLayer.PUBLIC_LEGAL and not _legal_period_overlaps(
        access, record
    ):
        return False
    if record.layer is MemoryLayer.RUN_WORKING:
        if not (
            record.firm_id == access.firm_id
            and record.matter_id == access.matter_id
            and record.owner_actor_id == access.actor_id
            and record.run_id == access.run_id
        ):
            return False
        return access.task_id is None or record.task_id in {None, access.task_id}
    if record.layer is MemoryLayer.CASE_LONG_TERM:
        if record.firm_id != access.firm_id or record.matter_id != access.matter_id:
            return False
        if not record.permission_group_ids:
            return False
        return bool(
            set(record.permission_group_ids).intersection(access.permission_group_ids)
        )
    if record.layer is MemoryLayer.LAWYER_PERSONAL:
        return record.firm_id == access.firm_id and record.owner_actor_id == access.actor_id
    if record.layer is MemoryLayer.FIRM_KNOWLEDGE:
        if record.firm_id != access.firm_id:
            return False
        if not record.permission_group_ids:
            return False
        return bool(set(record.permission_group_ids).intersection(access.permission_group_ids))
    return record.layer is MemoryLayer.PUBLIC_LEGAL


def _metadata_match(
    access: RetrievalAccessFilter, record: MemoryRecord
) -> tuple[float, list[str]]:
    comparisons = (
        (
            access.case_type_codes,
            record.case_type_codes,
            access.case_type_mode,
            "案件类型匹配",
        ),
        (
            access.procedure_stages,
            record.procedure_stages,
            access.procedure_stage_mode,
            "程序阶段匹配",
        ),
        (access.issue_tags, record.issue_tags, access.issue_tag_mode, "争点标签匹配"),
    )
    scores: list[float] = []
    reasons: list[str] = []
    for requested, available, mode, reason in comparisons:
        if not requested or mode is not MetadataFilterMode.SOFT:
            continue
        matched = bool(set(requested).intersection(available))
        scores.append(1.0 if matched else 0.0)
        if matched:
            reasons.append(reason)
    if record.layer in {MemoryLayer.RUN_WORKING, MemoryLayer.CASE_LONG_TERM}:
        reasons.append("当前案件范围")
    elif record.layer is MemoryLayer.LAWYER_PERSONAL:
        reasons.append("律师本人已发布知识")
    elif record.layer is MemoryLayer.FIRM_KNOWLEDGE:
        reasons.append("律所已发布知识")
    else:
        reasons.append("当前有效公共法源")
    return (sum(scores) / len(scores) if scores else 0.5), reasons


def _passes_hard_metadata_filters(
    access: RetrievalAccessFilter, record: MemoryRecord
) -> bool:
    comparisons = (
        (access.case_type_codes, record.case_type_codes, access.case_type_mode),
        (
            access.procedure_stages,
            record.procedure_stages,
            access.procedure_stage_mode,
        ),
        (access.issue_tags, record.issue_tags, access.issue_tag_mode),
    )
    return all(
        mode is not MetadataFilterMode.HARD
        or not requested
        or bool(set(requested).intersection(available))
        for requested, available, mode in comparisons
    )


def _legal_period_overlaps(
    access: RetrievalAccessFilter, record: MemoryRecord
) -> bool:
    if access.legal_period_start is None and access.legal_period_end is None:
        return True
    query_start = access.legal_period_start or access.legal_period_end
    query_end = access.legal_period_end or access.legal_period_start
    if query_start is None or query_end is None:
        return False
    record_start = record.effective_from or date.min
    record_end = record.effective_to or date.max
    return record_start <= query_end and query_start <= record_end


def _validate_fresh_authority(
    access: RetrievalAccessFilter,
    authority: RetrievalAuthoritySnapshot,
    checked_at: datetime,
) -> None:
    if checked_at.tzinfo is None:
        raise AgentMemoryBlocked("retrieval verification time must include a timezone")
    if checked_at < authority.checked_at or checked_at - authority.checked_at > timedelta(seconds=30):
        raise AgentMemoryBlocked("retrieval authority snapshot is stale")
    if not authority.access_active:
        raise AgentMemoryBlocked("case access has been revoked")
    if (
        authority.actor_id != access.actor_id
        or authority.firm_id != access.firm_id
        or authority.matter_id != access.matter_id
        or authority.matter_access_grant_hash != access.matter_access_grant_hash
        or authority.matter_version != access.matter_version
        or tuple(sorted(authority.permission_group_ids)) != access.permission_group_ids
    ):
        raise AgentMemoryBlocked("retrieval authority no longer matches the query")


def _query_fingerprint(query: MemoryQuery) -> str:
    payload = {
        "query_text_hash": sha256(query.query_text.encode("utf-8")).hexdigest(),
        "access": {
            "firm_id": query.access.firm_id,
            "matter_id": query.access.matter_id,
            "actor_id": query.access.actor_id,
            "grant_hash": query.access.matter_access_grant_hash,
            "matter_version": query.access.matter_version,
            "permission_groups": query.access.permission_group_ids,
            "layers": tuple(item.value for item in query.access.layers),
            "run_id": query.access.run_id,
            "task_id": query.access.task_id,
            "knowledge_as_of": query.access.knowledge_as_of.isoformat(),
            "legal_period_start": (
                query.access.legal_period_start.isoformat()
                if query.access.legal_period_start
                else None
            ),
            "legal_period_end": (
                query.access.legal_period_end.isoformat()
                if query.access.legal_period_end
                else None
            ),
            "case_type_codes": query.access.case_type_codes,
            "procedure_stages": query.access.procedure_stages,
            "issue_tags": query.access.issue_tags,
            "filter_modes": (
                query.access.case_type_mode.value,
                query.access.procedure_stage_mode.value,
                query.access.issue_tag_mode.value,
            ),
        },
        "max_results": query.max_results,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _knowledge_publication_approval_hash(
    candidate: KnowledgePublicationCandidate,
) -> str:
    payload = {
        "schema_version": "knowledge-publication-approval-v1",
        "publication_id": candidate.publication_id,
        "source_firm_id": candidate.source_firm_id,
        "target": candidate.target.value,
        "source_matter_id": candidate.source_matter_id,
        "source_object_hashes": candidate.source_object_hashes,
        "published_content_hash": candidate.published_content_hash,
        "published_source_object_id": candidate.published_source_object_id,
        "published_source_object_hash": candidate.published_source_object_hash,
        "provenance_hash": candidate.provenance_hash,
        "owner_actor_id": candidate.owner_actor_id,
        "permission_group_ids": candidate.permission_group_ids,
        "reviews": (
            candidate.anonymization_review.value,
            candidate.conflict_review.value,
            candidate.confidentiality_review.value,
        ),
        "approved_by": candidate.approved_by,
        "second_approver_id": candidate.second_approver_id,
        "publication_policy_version": candidate.publication_policy_version,
        "publication_policy_hash": candidate.publication_policy_hash,
        "approved_at": candidate.approved_at.isoformat(),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _checkpoint_hash(checkpoint: RunMemoryCheckpoint) -> str:
    payload = {
        "schema_version": "agent-memory-checkpoint-v2",
        "firm_id": checkpoint.firm_id,
        "matter_id": checkpoint.matter_id,
        "owner_actor_id": checkpoint.owner_actor_id,
        "run_id": checkpoint.run_id,
        "sequence": checkpoint.sequence,
        "previous_checkpoint_hash": checkpoint.previous_checkpoint_hash,
        "case_snapshot_hash": checkpoint.case_snapshot_hash,
        "plan_hash": checkpoint.plan_hash,
        "task_state_hash": checkpoint.task_state_hash,
        "unresolved_question_ids": checkpoint.unresolved_question_ids,
        "retrieval_scope_hashes": checkpoint.retrieval_scope_hashes,
        "occurred_at": checkpoint.occurred_at.isoformat(),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _required_text(value: str, label: str, max_length: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > max_length:
        raise AgentMemoryBlocked(f"{label} is invalid")
    return value


def _required_code(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{1,119}", value) is None:
        raise AgentMemoryBlocked(f"{label} must be an uppercase code")
    return value


def _required_version(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None:
        raise AgentMemoryBlocked(f"{label} must be a semantic version")
    return value


def _required_uuid(value: str | None, label: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as error:
        raise AgentMemoryBlocked(f"{label} must be a UUID") from error
    return str(parsed)


def _required_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AgentMemoryBlocked(f"{label} must be a SHA-256 digest")
    return value


def _unique_codes(
    values: tuple[str, ...], label: str, *, uuid_values: bool = False
) -> tuple[str, ...]:
    if len(values) != len(set(values)) or len(values) > 100:
        raise AgentMemoryBlocked(f"{label} must be unique and bounded")
    for value in values:
        if uuid_values:
            _required_uuid(value, label)
        else:
            _required_code(value, label)
    return tuple(sorted(values))


def _validate_box(value: tuple[float, float, float, float] | None) -> None:
    if value is None or len(value) != 4:
        raise AgentMemoryBlocked("image-region memory requires a normalized box")
    x0, y0, x1, y1 = value
    if any(not math.isfinite(item) for item in value) or not (
        0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    ):
        raise AgentMemoryBlocked("normalized box is outside the page")


def _unit_score(value: float, label: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise AgentMemoryBlocked(f"{label} must be between zero and one")
