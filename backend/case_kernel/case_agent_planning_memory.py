"""Governed pre-planning memory enrichment for the lawyer Agent.

Memory retrieval is deliberately performed after the base case projection has
closed its PostgreSQL ``REPEATABLE READ`` opening/closing fences, but before a
planner is called.  It therefore cannot weaken that atomic projection.  A
durable enrichment receipt binds the separate ACL-first FTS result to one run
owner, goal, case snapshot, query, base planning snapshot and purpose.  The
PostgreSQL adapter must revalidate the owner's current grants and every
returned record before returning a receipt.

The external planner sees only memory entries explicitly marked as externally
disclosable.  Same-case client memory is server-only unless an adapter backed
by the existing external-request authorization ledger supplies an exact grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Protocol
from uuid import UUID

from .case_agent_memory import (
    MemoryAuthority,
    MemoryLayer,
    MetadataFilterMode,
    SourceExposure,
)
from .case_agent_planner import (
    CasePlannerBlocked,
    CasePlanningSignal,
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlanningSignalCategory,
)
from .case_agent_supervisor import AgentRunState
from .models import Actor, Role


class PlanningMemoryBlocked(RuntimeError):
    """A pre-planning enrichment crossed an identity or version fence."""


class PlanningMemoryPurpose(StrEnum):
    DYNAMIC_CASE_PLANNING = "DYNAMIC_CASE_PLANNING"


_ALLOWED_LAYERS = frozenset(
    {
        MemoryLayer.CASE_LONG_TERM,
        MemoryLayer.LAWYER_PERSONAL,
        MemoryLayer.FIRM_KNOWLEDGE,
        MemoryLayer.PUBLIC_LEGAL,
    }
)
_OWNER_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_OWNER_AUXILIARY_ROLES = frozenset({Role.FIRM_ADMIN})
_MAX_MEMORY_SUMMARY_BYTES = 300


@dataclass(frozen=True)
class PlanningMemorySearchRequest:
    purpose: PlanningMemoryPurpose
    query_text: str
    layers: tuple[MemoryLayer, ...]
    allowed_skill_ids: tuple[str, ...]
    legal_period_start: date | None = None
    legal_period_end: date | None = None
    legal_period_source_ref_ids: tuple[str, ...] = ()
    legal_period_binding_hash: str | None = None
    case_type_codes: tuple[str, ...] = ()
    procedure_stages: tuple[str, ...] = ()
    issue_tags: tuple[str, ...] = ()
    case_type_mode: MetadataFilterMode = MetadataFilterMode.SOFT
    procedure_stage_mode: MetadataFilterMode = MetadataFilterMode.SOFT
    issue_tag_mode: MetadataFilterMode = MetadataFilterMode.SOFT
    max_results: int = 10
    external_planner_contract_hash: str | None = None

    def validate(self) -> None:
        if not isinstance(self.purpose, PlanningMemoryPurpose):
            raise PlanningMemoryBlocked("planning memory purpose is invalid")
        _text(self.query_text, "planning memory query", 4_000)
        if (
            not self.layers
            or len(self.layers) > 4
            or tuple(sorted(set(self.layers), key=lambda item: item.value)) != self.layers
            or not set(self.layers).issubset(_ALLOWED_LAYERS)
        ):
            raise PlanningMemoryBlocked("planning memory layers are invalid")
        if MemoryLayer.PUBLIC_LEGAL in self.layers and (
            self.legal_period_start is None
            or self.legal_period_end is None
            or not self.legal_period_source_ref_ids
            or self.legal_period_binding_hash is None
        ):
            raise PlanningMemoryBlocked(
                "planning legal memory requires an authoritative controversy period"
            )
        if MemoryLayer.PUBLIC_LEGAL not in self.layers and (
            self.legal_period_start is not None
            or self.legal_period_end is not None
            or self.legal_period_source_ref_ids
            or self.legal_period_binding_hash is not None
        ):
            raise PlanningMemoryBlocked(
                "non-legal planning memory cannot carry a legal period"
            )
        if (
            self.legal_period_start is not None
            and self.legal_period_end is not None
            and self.legal_period_end < self.legal_period_start
        ):
            raise PlanningMemoryBlocked("planning legal memory period is inverted")
        if (
            len(self.legal_period_source_ref_ids) > 100
            or tuple(sorted(set(self.legal_period_source_ref_ids)))
            != self.legal_period_source_ref_ids
        ):
            raise PlanningMemoryBlocked("planning legal period sources are invalid")
        for ref_id in self.legal_period_source_ref_ids:
            _code(ref_id, "planning legal period source")
        if self.legal_period_binding_hash is not None:
            _hash(self.legal_period_binding_hash, "planning legal period binding")
        if not self.allowed_skill_ids or len(self.allowed_skill_ids) > 100:
            raise PlanningMemoryBlocked("planning memory requires a Skill allowlist")
        if tuple(sorted(set(self.allowed_skill_ids))) != self.allowed_skill_ids:
            raise PlanningMemoryBlocked("planning memory Skill ids must be sorted and unique")
        for skill_id in self.allowed_skill_ids:
            _code(skill_id, "planning memory Skill id")
        for values, label in (
            (self.case_type_codes, "case type"),
            (self.procedure_stages, "procedure stage"),
            (self.issue_tags, "issue tag"),
        ):
            if len(values) > 100 or tuple(sorted(set(values))) != values:
                raise PlanningMemoryBlocked(f"planning memory {label} filters are invalid")
            for value in values:
                _code(value, f"planning memory {label}")
        if not 1 <= self.max_results <= 20:
            raise PlanningMemoryBlocked("planning memory result limit must be 1 to 20")
        if self.external_planner_contract_hash is not None:
            _hash(
                self.external_planner_contract_hash,
                "external planner contract hash",
            )

    @property
    def query_hash(self) -> str:
        self.validate()
        return sha256(self.query_text.encode("utf-8")).hexdigest()

    @property
    def query_contract_hash(self) -> str:
        """Bind every retrieval and planner-exposure input, not just query text."""

        self.validate()
        return _canonical_hash(
            {
                "schema_version": "planning-memory-search-request-v1",
                "purpose": self.purpose.value,
                "query_hash": self.query_hash,
                "layers": tuple(item.value for item in self.layers),
                "allowed_skill_ids": self.allowed_skill_ids,
                "legal_period_start": (
                    self.legal_period_start.isoformat()
                    if self.legal_period_start is not None
                    else None
                ),
                "legal_period_end": (
                    self.legal_period_end.isoformat()
                    if self.legal_period_end is not None
                    else None
                ),
                "legal_period_source_ref_ids": self.legal_period_source_ref_ids,
                "legal_period_binding_hash": self.legal_period_binding_hash,
                "case_type_codes": self.case_type_codes,
                "procedure_stages": self.procedure_stages,
                "issue_tags": self.issue_tags,
                "case_type_mode": self.case_type_mode.value,
                "procedure_stage_mode": self.procedure_stage_mode.value,
                "issue_tag_mode": self.issue_tag_mode.value,
                "max_results": self.max_results,
                "external_planner_contract_hash": self.external_planner_contract_hash,
            }
        )


@dataclass(frozen=True)
class AuthorizedPlanningLegalPeriod:
    """A server-derived controversy period bound to current planning inputs."""

    period_start: date
    period_end: date
    source_ref_ids: tuple[str, ...]
    binding_hash: str

    @classmethod
    def build(
        cls,
        *,
        period_start: date,
        period_end: date,
        source_ref_ids: Iterable[str],
        base_planning_hash: str,
    ) -> "AuthorizedPlanningLegalPeriod":
        refs = tuple(sorted(set(source_ref_ids)))
        values = {
            "schema_version": "planning-memory-legal-period-v1",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "source_ref_ids": refs,
            "base_planning_hash": base_planning_hash,
        }
        result = cls(
            period_start=period_start,
            period_end=period_end,
            source_ref_ids=refs,
            binding_hash=_canonical_hash(values),
        )
        result.validate(base_planning_hash=base_planning_hash)
        return result

    def validate(self, *, base_planning_hash: str) -> None:
        if self.period_end < self.period_start:
            raise PlanningMemoryBlocked("planning legal period is inverted")
        _hash(base_planning_hash, "base planning hash")
        if (
            not self.source_ref_ids
            or len(self.source_ref_ids) > 100
            or tuple(sorted(set(self.source_ref_ids))) != self.source_ref_ids
        ):
            raise PlanningMemoryBlocked("planning legal period sources are invalid")
        for ref_id in self.source_ref_ids:
            _code(ref_id, "planning legal period source")
        _hash(self.binding_hash, "planning legal period binding")
        expected = _canonical_hash(
            {
                "schema_version": "planning-memory-legal-period-v1",
                "period_start": self.period_start.isoformat(),
                "period_end": self.period_end.isoformat(),
                "source_ref_ids": self.source_ref_ids,
                "base_planning_hash": base_planning_hash,
            }
        )
        if expected != self.binding_hash:
            raise PlanningMemoryBlocked("planning legal period binding is stale")


class PlanningLegalPeriodResolver(Protocol):
    def current_period(
        self,
        *,
        state: AgentRunState,
        base_snapshot: CasePlanningSnapshot,
    ) -> AuthorizedPlanningLegalPeriod | None: ...


class PlanningMemorySearchRequestFactory(Protocol):
    def build_for_run(
        self,
        *,
        state: AgentRunState,
        base_snapshot: CasePlanningSnapshot,
    ) -> PlanningMemorySearchRequest: ...


class DynamicPlanningMemorySearchRequestFactory:
    """Build an FTS request from the current goal and authoritative signals.

    PUBLIC_LEGAL is deliberately omitted unless a trusted server resolver can
    return an exact controversy period bound to current planning references.
    No date is parsed from prose and no default period is guessed.
    """

    def __init__(
        self,
        *,
        allowed_skill_ids: tuple[str, ...],
        legal_period_resolver: PlanningLegalPeriodResolver | None = None,
        external_planner_contract_hash: str | None = None,
        max_results: int = 10,
    ) -> None:
        if (
            not allowed_skill_ids
            or len(allowed_skill_ids) > 100
            or tuple(sorted(set(allowed_skill_ids))) != allowed_skill_ids
        ):
            raise ValueError(
                "dynamic planning memory requires a sorted, unique Skill allowlist"
            )
        for skill_id in allowed_skill_ids:
            try:
                _code(skill_id, "planning memory Skill id")
            except PlanningMemoryBlocked as error:
                raise ValueError("dynamic planning memory Skill allowlist is invalid") from error
        if legal_period_resolver is not None and not callable(
            getattr(legal_period_resolver, "current_period", None)
        ):
            raise ValueError("planning legal period resolver is invalid")
        if external_planner_contract_hash is not None:
            try:
                _hash(
                    external_planner_contract_hash,
                    "external planner contract hash",
                )
            except PlanningMemoryBlocked as error:
                raise ValueError("external planner contract hash is invalid") from error
        if not 1 <= max_results <= 20:
            raise ValueError("dynamic planning memory result limit must be 1 to 20")
        self._allowed_skill_ids = tuple(allowed_skill_ids)
        self._legal_period_resolver = legal_period_resolver
        self._external_planner_contract_hash = external_planner_contract_hash
        self._max_results = max_results

    def build_for_run(
        self,
        *,
        state: AgentRunState,
        base_snapshot: CasePlanningSnapshot,
    ) -> PlanningMemorySearchRequest:
        base_snapshot.validate()
        if base_snapshot.case_snapshot != state.snapshot:
            raise PlanningMemoryBlocked(
                "dynamic memory query differs from the Agent run snapshot"
            )
        chunks = [
            state.goal.objective,
            *state.goal.success_criteria,
            *state.goal.constraints,
        ]
        chunks.extend(
            signal.summary
            for signal in base_snapshot.signals
            if signal.category
            in {
                PlanningSignalCategory.PROCEEDING,
                PlanningSignalCategory.PARTY_POSTURE,
                PlanningSignalCategory.WORK_PLAN,
                PlanningSignalCategory.CONFIRMED_FACT,
                PlanningSignalCategory.LEGAL_GAP,
            }
        )
        query_text = _bounded_query_text(chunks, maximum_bytes=4_000)
        layers = {
            MemoryLayer.CASE_LONG_TERM,
            MemoryLayer.LAWYER_PERSONAL,
            MemoryLayer.FIRM_KNOWLEDGE,
        }
        period: AuthorizedPlanningLegalPeriod | None = None
        if self._legal_period_resolver is not None:
            period = self._legal_period_resolver.current_period(
                state=state, base_snapshot=base_snapshot
            )
        if period is not None:
            period.validate(base_planning_hash=base_snapshot.planning_hash)
            authorized_refs = {item.ref_id for item in base_snapshot.authorized_inputs}
            if not set(period.source_ref_ids).issubset(authorized_refs):
                raise PlanningMemoryBlocked(
                    "planning legal period cites an unauthorized source"
                )
            layers.add(MemoryLayer.PUBLIC_LEGAL)
        request = PlanningMemorySearchRequest(
            purpose=PlanningMemoryPurpose.DYNAMIC_CASE_PLANNING,
            query_text=query_text,
            layers=tuple(sorted(layers, key=lambda item: item.value)),
            allowed_skill_ids=self._allowed_skill_ids,
            legal_period_start=None if period is None else period.period_start,
            legal_period_end=None if period is None else period.period_end,
            legal_period_source_ref_ids=(
                () if period is None else period.source_ref_ids
            ),
            legal_period_binding_hash=(None if period is None else period.binding_hash),
            # These are relevance hints, never ACL inputs.  Bound the complete
            # deterministic set to the request schema's maximum rather than
            # letting a large case projection turn into an unbounded FTS call.
            issue_tags=tuple(
                sorted(
                    {
                        signal.code
                        for signal in base_snapshot.signals
                        if signal.category
                        in {
                            PlanningSignalCategory.CONFIRMED_FACT,
                            PlanningSignalCategory.LEGAL_GAP,
                            PlanningSignalCategory.WORK_PLAN,
                        }
                    }
                )[:100]
            ),
            max_results=self._max_results,
            external_planner_contract_hash=self._external_planner_contract_hash,
        )
        request.validate()
        return request


@dataclass(frozen=True)
class PlanningMemorySourceRef:
    source_type: str
    source_id: str
    source_version: str
    content_hash: str
    exposure: SourceExposure
    page_number: int | None

    def validate(self) -> None:
        _code(self.source_type, "memory source type")
        _bounded(self.source_id, "memory source id", 300)
        _bounded(self.source_version, "memory source version", 300)
        _hash(self.content_hash, "memory source content hash")
        if not isinstance(self.exposure, SourceExposure):
            raise PlanningMemoryBlocked("memory source exposure is invalid")
        if self.page_number is not None and self.page_number < 1:
            raise PlanningMemoryBlocked("memory source page number is invalid")


@dataclass(frozen=True)
class PlanningMemoryItem:
    record_id: str
    record_version: int
    layer: MemoryLayer
    authority: MemoryAuthority
    content_hash: str
    provenance_hash: str
    summary: str
    summary_hash: str
    source_refs: tuple[PlanningMemorySourceRef, ...]
    externally_disclosable: bool
    external_authorization_hash: str | None
    item_hash: str

    @classmethod
    def build(
        cls,
        *,
        record_id: str,
        record_version: int,
        layer: MemoryLayer,
        authority: MemoryAuthority,
        content_hash: str,
        provenance_hash: str,
        summary: str,
        source_refs: tuple[PlanningMemorySourceRef, ...],
        externally_disclosable: bool,
        external_authorization_hash: str | None,
    ) -> "PlanningMemoryItem":
        normalized_summary = _text(
            summary, "planning memory summary", _MAX_MEMORY_SUMMARY_BYTES
        )
        summary_hash = sha256(normalized_summary.encode("utf-8")).hexdigest()
        provisional = {
            "record_id": record_id,
            "record_version": record_version,
            "layer": layer,
            "authority": authority,
            "content_hash": content_hash,
            "provenance_hash": provenance_hash,
            "summary": normalized_summary,
            "summary_hash": summary_hash,
            "source_refs": tuple(source_refs),
            "externally_disclosable": externally_disclosable,
            "external_authorization_hash": external_authorization_hash,
        }
        item = cls(
            **provisional,
            item_hash=_canonical_hash(_memory_item_payload(provisional)),
        )
        item.validate()
        return item

    def validate(self) -> None:
        _uuid(self.record_id, "memory record id")
        if self.record_version < 1:
            raise PlanningMemoryBlocked("memory record version is invalid")
        if self.layer not in _ALLOWED_LAYERS:
            raise PlanningMemoryBlocked("memory item layer is unavailable to planning")
        if not isinstance(self.authority, MemoryAuthority):
            raise PlanningMemoryBlocked("memory item authority is invalid")
        _hash(self.content_hash, "memory content hash")
        _hash(self.provenance_hash, "memory provenance hash")
        normalized = _text(
            self.summary, "planning memory summary", _MAX_MEMORY_SUMMARY_BYTES
        )
        _hash(self.summary_hash, "memory summary hash")
        if sha256(normalized.encode("utf-8")).hexdigest() != self.summary_hash:
            raise PlanningMemoryBlocked("memory summary hash differs from its text")
        if not self.source_refs or len(self.source_refs) > 50:
            raise PlanningMemoryBlocked("memory item requires bounded source references")
        for source in self.source_refs:
            source.validate()
        exposures = {source.exposure for source in self.source_refs}
        if self.layer is MemoryLayer.CASE_LONG_TERM:
            if exposures != {SourceExposure.CASE_PRIVATE}:
                raise PlanningMemoryBlocked("same-case memory source exposure is invalid")
            if self.externally_disclosable:
                _hash(
                    self.external_authorization_hash or "",
                    "case-memory external authorization hash",
                )
            elif self.external_authorization_hash is not None:
                raise PlanningMemoryBlocked(
                    "server-only case memory cannot carry an external authorization"
                )
        else:
            required = (
                SourceExposure.PUBLIC_OFFICIAL
                if self.layer is MemoryLayer.PUBLIC_LEGAL
                else SourceExposure.PUBLISHED_SANITIZED
            )
            if exposures != {required} or not self.externally_disclosable:
                raise PlanningMemoryBlocked(
                    "published or public memory must retain its safe exposure classification"
                )
            if self.external_authorization_hash is not None:
                raise PlanningMemoryBlocked(
                    "non-client memory does not use a case-content authorization"
                )
        _hash(self.item_hash, "memory item hash")
        expected = _canonical_hash(
            _memory_item_payload(
                {
                    "record_id": self.record_id,
                    "record_version": self.record_version,
                    "layer": self.layer,
                    "authority": self.authority,
                    "content_hash": self.content_hash,
                    "provenance_hash": self.provenance_hash,
                    "summary": normalized,
                    "summary_hash": self.summary_hash,
                    "source_refs": self.source_refs,
                    "externally_disclosable": self.externally_disclosable,
                    "external_authorization_hash": self.external_authorization_hash,
                }
            )
        )
        if self.item_hash != expected:
            raise PlanningMemoryBlocked("memory item hash differs from its content")

    @property
    def ref_id(self) -> str:
        return f"memory:{self.record_id}:v{self.record_version}"


@dataclass(frozen=True)
class PlanningMemoryEnrichmentReceipt:
    enrichment_id: str
    firm_id: str
    matter_id: str
    run_id: str
    goal_id: str
    goal_hash: str
    owner_actor_id: str
    purpose: PlanningMemoryPurpose
    case_snapshot_hash: str
    case_snapshot_version: int
    case_snapshot_schema_version: str
    base_planning_hash: str
    query_hash: str
    query_contract_hash: str
    query_fingerprint: str
    retrieval_id: str
    retrieval_scope_hash: str
    owner_grant_hash: str
    owner_roles: tuple[Role, ...]
    permission_group_ids: tuple[str, ...]
    items: tuple[PlanningMemoryItem, ...]
    items_hash: str
    retrieved_at: datetime
    verified_at: datetime
    final_verified_at: datetime
    receipt_hash: str

    def validate(self) -> None:
        for value, label in (
            (self.enrichment_id, "enrichment id"),
            (self.firm_id, "enrichment firm id"),
            (self.matter_id, "enrichment matter id"),
            (self.run_id, "enrichment run id"),
            (self.goal_id, "enrichment goal id"),
            (self.owner_actor_id, "enrichment owner id"),
            (self.retrieval_id, "enrichment retrieval id"),
        ):
            _uuid(value, label)
        for value, label in (
            (self.goal_hash, "enrichment goal hash"),
            (self.case_snapshot_hash, "enrichment case snapshot hash"),
            (self.base_planning_hash, "enrichment base planning hash"),
            (self.query_hash, "enrichment query hash"),
            (self.query_contract_hash, "enrichment query contract hash"),
            (self.query_fingerprint, "enrichment query fingerprint"),
            (self.retrieval_scope_hash, "enrichment retrieval scope hash"),
            (self.owner_grant_hash, "enrichment owner grant hash"),
            (self.items_hash, "enrichment items hash"),
            (self.receipt_hash, "enrichment receipt hash"),
        ):
            _hash(value, label)
        if not isinstance(self.purpose, PlanningMemoryPurpose):
            raise PlanningMemoryBlocked("enrichment purpose is invalid")
        if self.case_snapshot_version < 1:
            raise PlanningMemoryBlocked("enrichment case snapshot version is invalid")
        _code(self.case_snapshot_schema_version, "case snapshot schema version")
        if Role.SYSTEM_WORKER in self.owner_roles:
            raise PlanningMemoryBlocked("SYSTEM_WORKER cannot own lawyer memory retrieval")
        if (
            not self.owner_roles
            or any(not isinstance(item, Role) for item in self.owner_roles)
            or tuple(sorted(set(self.owner_roles), key=lambda item: item.value))
            != self.owner_roles
            or not set(self.owner_roles).issubset(
                _OWNER_ROLES | _OWNER_AUXILIARY_ROLES
            )
            or not set(self.owner_roles).intersection(_OWNER_ROLES)
        ):
            raise PlanningMemoryBlocked("enrichment owner roles are invalid")
        if tuple(sorted(set(self.permission_group_ids))) != self.permission_group_ids:
            raise PlanningMemoryBlocked("enrichment permission groups are invalid")
        for value in self.permission_group_ids:
            _uuid(value, "enrichment permission group")
        if not self.items or len(self.items) > 20:
            raise PlanningMemoryBlocked("enrichment requires 1 to 20 memory items")
        if tuple(sorted(self.items, key=lambda item: item.ref_id)) != self.items:
            raise PlanningMemoryBlocked("enrichment memory items must be sorted")
        for item in self.items:
            item.validate()
        if self.items_hash != _canonical_hash([item.item_hash for item in self.items]):
            raise PlanningMemoryBlocked("enrichment item-set hash is invalid")
        if (
            self.retrieved_at.tzinfo is None
            or self.verified_at.tzinfo is None
            or self.final_verified_at.tzinfo is None
        ):
            raise PlanningMemoryBlocked("enrichment times require a timezone")
        if self.verified_at < self.retrieved_at:
            raise PlanningMemoryBlocked("enrichment verification predates retrieval")
        if self.final_verified_at < self.verified_at:
            raise PlanningMemoryBlocked("final authorization predates FTS verification")
        if self.receipt_hash != _canonical_hash(_receipt_payload(self)):
            raise PlanningMemoryBlocked("enrichment receipt hash differs from its bindings")


class PlanningMemoryEnrichmentPort(Protocol):
    def current_enrichment(
        self,
        *,
        state: AgentRunState,
        worker: Actor,
        base_snapshot: CasePlanningSnapshot,
        request: PlanningMemorySearchRequest,
    ) -> PlanningMemoryEnrichmentReceipt | None: ...


class PlanningSnapshotProvider(Protocol):
    def build_for_run(
        self, *, state: AgentRunState, actor: Actor
    ) -> CasePlanningSnapshot: ...


class MemoryEnrichedPlanningSnapshotProvider:
    """Compose an atomic case snapshot with a separately revalidated receipt."""

    def __init__(
        self,
        *,
        base_provider: PlanningSnapshotProvider,
        enrichment_port: PlanningMemoryEnrichmentPort,
        request: PlanningMemorySearchRequest | None = None,
        request_factory: PlanningMemorySearchRequestFactory | None = None,
        max_inputs: int = 500,
    ) -> None:
        if not callable(getattr(base_provider, "build_for_run", None)):
            raise ValueError("base planning snapshot provider is invalid")
        if not callable(getattr(enrichment_port, "current_enrichment", None)):
            raise ValueError("planning memory enrichment port is invalid")
        if (request is None) == (request_factory is None):
            raise ValueError(
                "planning memory requires exactly one request or request factory"
            )
        if request is not None:
            request.validate()
        if request_factory is not None and not callable(
            getattr(request_factory, "build_for_run", None)
        ):
            raise ValueError("planning memory request factory is invalid")
        if not 1 <= max_inputs <= 500:
            raise ValueError("enriched planning input limit is invalid")
        self._base_provider = base_provider
        self._enrichment_port = enrichment_port
        self._request = request
        self._request_factory = request_factory
        self._max_inputs = max_inputs

    def build_for_run(
        self, *, state: AgentRunState, actor: Actor
    ) -> CasePlanningSnapshot:
        if actor.roles != frozenset({Role.SYSTEM_WORKER}) or actor.firm_id != state.firm_id:
            raise PermissionError("planning memory requires a dedicated matter worker")
        base = self._base_provider.build_for_run(state=state, actor=actor)
        base.validate()
        request = self._request
        if request is None:
            assert self._request_factory is not None
            request = self._request_factory.build_for_run(
                state=state,
                base_snapshot=base,
            )
            request.validate()
        receipt = self._enrichment_port.current_enrichment(
            state=state,
            worker=actor,
            base_snapshot=base,
            request=request,
        )
        if receipt is None:
            return base
        receipt.validate()
        _require_receipt_binding(
            receipt=receipt,
            state=state,
            base_snapshot=base,
            request=request,
        )
        memory_inputs = tuple(
            PlanningInputRef(
                ref_id=item.ref_id,
                kind=PlanningInputKind.AUTHORIZED_MEMORY,
                object_version=f"memory-v{item.record_version}",
                content_hash=item.content_hash,
                status=PlanningInputStatus.CONFIRMED,
                allowed_skill_ids=request.allowed_skill_ids,
                planner_visible=item.externally_disclosable,
            )
            for item in receipt.items
        )
        if len(base.authorized_inputs) + len(memory_inputs) > self._max_inputs:
            raise PlanningMemoryBlocked(
                "authorized memory would exceed the planning input boundary"
            )
        memory_signals = tuple(
            CasePlanningSignal(
                signal_id=f"signal:memory:{item.record_id}:v{item.record_version}",
                category=(
                    PlanningSignalCategory.LEGAL_GAP
                    if item.layer is MemoryLayer.PUBLIC_LEGAL
                    else PlanningSignalCategory.CONFIRMED_FACT
                ),
                code=f"AUTHORIZED_{item.layer.value}",
                status=PlanningInputStatus.CONFIRMED,
                summary=item.summary,
                source_ref_ids=(item.ref_id,),
            )
            for item in receipt.items
        )
        try:
            return CasePlanningSnapshot.build(
                case_snapshot=base.case_snapshot,
                authorized_inputs=(*base.authorized_inputs, *memory_inputs),
                signals=(*base.signals, *memory_signals),
                reextraction_obligations=base.reextraction_obligations,
            )
        except CasePlannerBlocked as error:
            raise PlanningMemoryBlocked(
                "authorized memory cannot form a valid planning snapshot"
            ) from error


def build_receipt(
    *,
    enrichment_id: str,
    firm_id: str,
    matter_id: str,
    run_id: str,
    goal_id: str,
    goal_hash: str,
    owner_actor_id: str,
    purpose: PlanningMemoryPurpose,
    case_snapshot_hash: str,
    case_snapshot_version: int,
    case_snapshot_schema_version: str,
    base_planning_hash: str,
    query_hash: str,
    query_contract_hash: str,
    query_fingerprint: str,
    retrieval_id: str,
    retrieval_scope_hash: str,
    owner_grant_hash: str,
    owner_roles: tuple[Role, ...],
    permission_group_ids: tuple[str, ...],
    items: tuple[PlanningMemoryItem, ...],
    retrieved_at: datetime,
    verified_at: datetime,
    final_verified_at: datetime,
) -> PlanningMemoryEnrichmentReceipt:
    sorted_items = tuple(sorted(items, key=lambda item: item.ref_id))
    values = {
        "enrichment_id": enrichment_id,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "run_id": run_id,
        "goal_id": goal_id,
        "goal_hash": goal_hash,
        "owner_actor_id": owner_actor_id,
        "purpose": purpose,
        "case_snapshot_hash": case_snapshot_hash,
        "case_snapshot_version": case_snapshot_version,
        "case_snapshot_schema_version": case_snapshot_schema_version,
        "base_planning_hash": base_planning_hash,
        "query_hash": query_hash,
        "query_contract_hash": query_contract_hash,
        "query_fingerprint": query_fingerprint,
        "retrieval_id": retrieval_id,
        "retrieval_scope_hash": retrieval_scope_hash,
        "owner_grant_hash": owner_grant_hash,
        "owner_roles": tuple(sorted(owner_roles, key=lambda item: item.value)),
        "permission_group_ids": tuple(sorted(permission_group_ids)),
        "items": sorted_items,
        "items_hash": _canonical_hash([item.item_hash for item in sorted_items]),
        "retrieved_at": retrieved_at,
        "verified_at": verified_at,
        "final_verified_at": final_verified_at,
    }
    provisional = PlanningMemoryEnrichmentReceipt(**values, receipt_hash="0" * 64)
    result = PlanningMemoryEnrichmentReceipt(
        **values,
        receipt_hash=_canonical_hash(_receipt_payload(provisional)),
    )
    result.validate()
    return result


def _require_receipt_binding(
    *,
    receipt: PlanningMemoryEnrichmentReceipt,
    state: AgentRunState,
    base_snapshot: CasePlanningSnapshot,
    request: PlanningMemorySearchRequest,
) -> None:
    if (
        receipt.firm_id != state.firm_id
        or receipt.matter_id != state.matter_id
        or receipt.run_id != state.run_id
        or receipt.goal_id != state.goal.goal_id
        or receipt.goal_hash != state.goal.goal_hash
        or receipt.owner_actor_id != state.goal.requested_by
        or receipt.purpose is not request.purpose
        or receipt.case_snapshot_hash != state.snapshot.snapshot_hash
        or receipt.case_snapshot_version != state.snapshot.matter_version
        or receipt.case_snapshot_schema_version != state.snapshot.schema_version
        or receipt.base_planning_hash != base_snapshot.planning_hash
        or receipt.query_hash != request.query_hash
        or receipt.query_contract_hash != request.query_contract_hash
    ):
        raise PlanningMemoryBlocked(
            "planning memory receipt belongs to another run, goal, snapshot or query"
        )


def _memory_item_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "planning-memory-item-v1",
        "record_id": values["record_id"],
        "record_version": values["record_version"],
        "layer": values["layer"].value,  # type: ignore[union-attr]
        "authority": values["authority"].value,  # type: ignore[union-attr]
        "content_hash": values["content_hash"],
        "provenance_hash": values["provenance_hash"],
        "summary": values["summary"],
        "summary_hash": values["summary_hash"],
        "source_refs": [
            {
                "source_type": item.source_type,
                "source_id": item.source_id,
                "source_version": item.source_version,
                "content_hash": item.content_hash,
                "exposure": item.exposure.value,
                "page_number": item.page_number,
            }
            for item in values["source_refs"]  # type: ignore[union-attr]
        ],
        "externally_disclosable": values["externally_disclosable"],
        "external_authorization_hash": values["external_authorization_hash"],
    }


def _receipt_payload(receipt: PlanningMemoryEnrichmentReceipt) -> dict[str, object]:
    return {
        "schema_version": "planning-memory-enrichment-receipt-v1",
        "enrichment_id": receipt.enrichment_id,
        "firm_id": receipt.firm_id,
        "matter_id": receipt.matter_id,
        "run_id": receipt.run_id,
        "goal_id": receipt.goal_id,
        "goal_hash": receipt.goal_hash,
        "owner_actor_id": receipt.owner_actor_id,
        "purpose": receipt.purpose.value,
        "case_snapshot_hash": receipt.case_snapshot_hash,
        "case_snapshot_version": receipt.case_snapshot_version,
        "case_snapshot_schema_version": receipt.case_snapshot_schema_version,
        "base_planning_hash": receipt.base_planning_hash,
        "query_hash": receipt.query_hash,
        "query_contract_hash": receipt.query_contract_hash,
        "query_fingerprint": receipt.query_fingerprint,
        "retrieval_id": receipt.retrieval_id,
        "retrieval_scope_hash": receipt.retrieval_scope_hash,
        "owner_grant_hash": receipt.owner_grant_hash,
        "owner_roles": tuple(item.value for item in receipt.owner_roles),
        "permission_group_ids": receipt.permission_group_ids,
        "item_hashes": tuple(item.item_hash for item in receipt.items),
        "items_hash": receipt.items_hash,
        "retrieved_at": receipt.retrieved_at.isoformat(),
        "verified_at": receipt.verified_at.isoformat(),
        "final_verified_at": receipt.final_verified_at.isoformat(),
    }


def planning_memory_source_ref_hash(
    sources: tuple[PlanningMemorySourceRef, ...],
) -> str:
    """Canonical hash used by the persistence and disclosure adapters."""

    for item in sources:
        item.validate()
    return _canonical_hash(
        [
            {
                "source_type": item.source_type,
                "source_id": item.source_id,
                "source_version": item.source_version,
                "content_hash": item.content_hash,
                "exposure": item.exposure.value,
                "page_number": item.page_number,
            }
            for item in sources
        ]
    )


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise PlanningMemoryBlocked(f"{label} must be a UUID") from error


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PlanningMemoryBlocked(f"{label} must be a SHA-256 hash")


def _code(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is None
    ):
        raise PlanningMemoryBlocked(f"{label} is invalid")


def _bounded(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlanningMemoryBlocked(f"{label} is missing or too long")
    return value.strip()


def _text(value: object, label: str, maximum_bytes: int) -> str:
    normalized = _bounded(value, label, maximum_bytes)
    if len(normalized.encode("utf-8")) > maximum_bytes:
        raise PlanningMemoryBlocked(f"{label} exceeds its byte boundary")
    return normalized


def _bounded_query_text(chunks: Iterable[str], *, maximum_bytes: int) -> str:
    """Build one deterministic local-FTS query without retaining its prose.

    The request factory receives server-projected case summaries, so it never
    parses a browser prompt.  Unicode clipping is performed on bytes to match
    the storage/request boundary while preserving valid UTF-8.
    """

    normalized: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, str):
            raise PlanningMemoryBlocked("planning memory query input is not text")
        value = " ".join(chunk.split())
        if value:
            normalized.append(value)
    if not normalized:
        raise PlanningMemoryBlocked("planning memory query has no authoritative terms")
    encoded = "\n".join(normalized).encode("utf-8")
    if len(encoded) > maximum_bytes:
        value = encoded[:maximum_bytes].decode("utf-8", errors="ignore").rstrip()
    else:
        value = encoded.decode("utf-8")
    return _text(value, "planning memory query", maximum_bytes)


__all__ = (
    "AuthorizedPlanningLegalPeriod",
    "DynamicPlanningMemorySearchRequestFactory",
    "MemoryEnrichedPlanningSnapshotProvider",
    "PlanningLegalPeriodResolver",
    "PlanningMemoryBlocked",
    "PlanningMemoryEnrichmentPort",
    "PlanningMemoryEnrichmentReceipt",
    "PlanningMemoryItem",
    "PlanningMemoryPurpose",
    "PlanningMemorySearchRequest",
    "PlanningMemorySearchRequestFactory",
    "PlanningMemorySourceRef",
    "build_receipt",
    "planning_memory_source_ref_hash",
)
