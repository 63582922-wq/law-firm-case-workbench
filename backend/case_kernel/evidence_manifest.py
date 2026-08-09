"""Immutable originals and lawyer-approved page disposition manifests.

The ledger never deletes a source page. "Remove an unrelated or duplicate
page" means excluding that page from a locked derivative plan while preserving
the original, the decision, the reason, and the audit identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .models import Actor, Role


class EvidenceManifestBlocked(ValueError):
    """The page ledger is incomplete, contradictory, or insufficiently approved."""


class PageDisposition(str, Enum):
    INCLUDE = "INCLUDE"
    EXCLUDE = "EXCLUDE"


class ReviewStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    INVALIDATED = "INVALIDATED"


class DuplicateResolution(str, Enum):
    CANDIDATE = "CANDIDATE"
    SAME_SOURCE_PAGE = "SAME_SOURCE_PAGE"
    DISTINCT_PAGES = "DISTINCT_PAGES"
    INVALIDATED = "INVALIDATED"


class ManifestStatus(str, Enum):
    LOCKED = "LOCKED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class OriginalEvidenceFile:
    file_id: str
    original_label: str
    original_file_sha256: str
    byte_size: int
    media_type: str
    page_count: int


@dataclass(frozen=True)
class EvidencePage:
    page_id: str
    file_id: str
    page_number: int


@dataclass(frozen=True)
class PageDecision:
    decision_id: str
    page_id: str
    disposition: PageDisposition
    reason: str
    status: ReviewStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class PageAnnotation:
    annotation_id: str
    page_id: str
    x0: float
    y0: float
    x1: float
    y1: float
    label: str
    status: ReviewStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class DuplicatePageGroup:
    group_id: str
    page_ids: tuple[str, ...]
    status: DuplicateResolution
    canonical_page_id: str | None
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class ManifestPageEntry:
    page_id: str
    file_id: str
    page_number: int
    disposition: PageDisposition
    decision_id: str
    derivative_sequence: int | None
    annotation_ids: tuple[str, ...]


@dataclass(frozen=True)
class LockedEvidenceManifest:
    manifest_id: str
    ledger_version: int
    status: ManifestStatus
    content_hash: str
    total_pages: int
    included_pages: int
    excluded_pages: int
    approved_by: str
    approval_hash: str
    entries: tuple[ManifestPageEntry, ...]


@dataclass(frozen=True)
class DerivativePagePlan:
    source_page_id: str
    source_file_sha256: str
    source_page_number: int
    derivative_sequence: int
    annotations: tuple[PageAnnotation, ...]


class EvidenceManifestLedger:
    def __init__(self) -> None:
        self._files: dict[str, OriginalEvidenceFile] = {}
        self._pages: dict[str, EvidencePage] = {}
        self._decisions: dict[str, PageDecision] = {}
        self._annotations: dict[str, PageAnnotation] = {}
        self._duplicates: dict[str, DuplicatePageGroup] = {}
        self._manifests: dict[str, LockedEvidenceManifest] = {}
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def add_original_file(
        self,
        actor: Actor,
        *,
        original_label: str,
        original_file_sha256: str,
        byte_size: int,
        media_type: str,
        page_count: int,
    ) -> OriginalEvidenceFile:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.SYSTEM_WORKER})
        _require_text(original_label, "original label")
        _validate_sha256(original_file_sha256, "original file")
        _require_text(media_type, "media type")
        if byte_size < 1 or page_count < 1:
            raise EvidenceManifestBlocked("original byte size and page count must be positive")
        source = OriginalEvidenceFile(
            file_id=f"evidence_file_{uuid4().hex}",
            original_label=original_label.strip(),
            original_file_sha256=original_file_sha256,
            byte_size=byte_size,
            media_type=media_type.strip(),
            page_count=page_count,
        )
        self._files[source.file_id] = source
        for page_number in range(1, page_count + 1):
            page = EvidencePage(
                page_id=f"evidence_page_{uuid4().hex}",
                file_id=source.file_id,
                page_number=page_number,
            )
            self._pages[page.page_id] = page
        self._version += 1
        self._invalidate_manifests()
        return source

    def pages_for_file(self, file_id: str) -> tuple[EvidencePage, ...]:
        if file_id not in self._files:
            raise EvidenceManifestBlocked("unknown original evidence file")
        return tuple(sorted((page for page in self._pages.values() if page.file_id == file_id), key=lambda page: page.page_number))

    def propose_page_decision(
        self,
        actor: Actor,
        *,
        page_id: str,
        disposition: PageDisposition,
        reason: str,
    ) -> PageDecision:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        self._require_page(page_id)
        _require_text(reason, "page decision reason")
        decision = PageDecision(
            decision_id=f"page_decision_{uuid4().hex}",
            page_id=page_id,
            disposition=disposition,
            reason=reason.strip(),
            status=ReviewStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._decisions[decision.decision_id] = decision
        self._version += 1
        return decision

    def approve_page_decision(
        self,
        actor: Actor,
        *,
        decision_id: str,
        approval_hash: str,
    ) -> PageDecision:
        _require_lead(actor)
        _require_text(approval_hash, "page decision approval hash")
        decision = self._decisions.get(decision_id)
        if decision is None or decision.status is not ReviewStatus.CANDIDATE:
            raise EvidenceManifestBlocked("only an active page decision candidate can be approved")
        for current_id, current in list(self._decisions.items()):
            if current.page_id == decision.page_id and current.status is ReviewStatus.APPROVED:
                self._decisions[current_id] = PageDecision(
                    decision_id=current.decision_id,
                    page_id=current.page_id,
                    disposition=current.disposition,
                    reason=current.reason,
                    status=ReviewStatus.INVALIDATED,
                    approved_by=None,
                    approval_hash=None,
                )
        approved = PageDecision(
            decision_id=decision.decision_id,
            page_id=decision.page_id,
            disposition=decision.disposition,
            reason=decision.reason,
            status=ReviewStatus.APPROVED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._decisions[decision_id] = approved
        self._version += 1
        self._invalidate_manifests()
        return approved

    def propose_annotation(
        self,
        actor: Actor,
        *,
        page_id: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        label: str,
    ) -> PageAnnotation:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        self._require_page(page_id)
        _require_text(label, "annotation label")
        _validate_rectangle(x0, y0, x1, y1)
        annotation = PageAnnotation(
            annotation_id=f"page_annotation_{uuid4().hex}",
            page_id=page_id,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            label=label.strip(),
            status=ReviewStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._annotations[annotation.annotation_id] = annotation
        self._version += 1
        return annotation

    def approve_annotation(self, actor: Actor, *, annotation_id: str, approval_hash: str) -> PageAnnotation:
        _require_lead(actor)
        _require_text(approval_hash, "annotation approval hash")
        annotation = self._annotations.get(annotation_id)
        if annotation is None or annotation.status is not ReviewStatus.CANDIDATE:
            raise EvidenceManifestBlocked("only an active annotation candidate can be approved")
        approved = PageAnnotation(
            annotation_id=annotation.annotation_id,
            page_id=annotation.page_id,
            x0=annotation.x0,
            y0=annotation.y0,
            x1=annotation.x1,
            y1=annotation.y1,
            label=annotation.label,
            status=ReviewStatus.APPROVED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._annotations[annotation_id] = approved
        self._version += 1
        self._invalidate_manifests()
        return approved

    def add_duplicate_group_candidate(self, actor: Actor, *, page_ids: tuple[str, ...]) -> DuplicatePageGroup:
        _require_role(actor, {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
        normalized = tuple(sorted(set(page_ids)))
        if len(normalized) < 2:
            raise EvidenceManifestBlocked("a duplicate group needs at least two distinct pages")
        for page_id in normalized:
            self._require_page(page_id)
        active_pages = {
            page_id
            for group in self._duplicates.values()
            if group.status is not DuplicateResolution.INVALIDATED
            for page_id in group.page_ids
        }
        if active_pages & set(normalized):
            raise EvidenceManifestBlocked("a page already belongs to an active duplicate group")
        group = DuplicatePageGroup(
            group_id=f"page_duplicate_group_{uuid4().hex}",
            page_ids=normalized,
            status=DuplicateResolution.CANDIDATE,
            canonical_page_id=None,
            approved_by=None,
            approval_hash=None,
        )
        self._duplicates[group.group_id] = group
        self._version += 1
        return group

    def resolve_duplicate_group(
        self,
        actor: Actor,
        *,
        group_id: str,
        same_source_page: bool,
        canonical_page_id: str | None,
        approval_hash: str,
    ) -> DuplicatePageGroup:
        _require_lead(actor)
        _require_text(approval_hash, "duplicate resolution approval hash")
        group = self._duplicates.get(group_id)
        if group is None or group.status is not DuplicateResolution.CANDIDATE:
            raise EvidenceManifestBlocked("only an active duplicate page group can be resolved")
        if same_source_page:
            if canonical_page_id not in group.page_ids:
                raise EvidenceManifestBlocked("same-source duplicate group requires a canonical member page")
            status = DuplicateResolution.SAME_SOURCE_PAGE
        else:
            if canonical_page_id is not None:
                raise EvidenceManifestBlocked("distinct pages cannot select a canonical page")
            status = DuplicateResolution.DISTINCT_PAGES
        resolved = DuplicatePageGroup(
            group_id=group.group_id,
            page_ids=group.page_ids,
            status=status,
            canonical_page_id=canonical_page_id,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._duplicates[group_id] = resolved
        self._version += 1
        self._invalidate_manifests()
        return resolved

    def lock_manifest(self, actor: Actor, *, approval_hash: str) -> LockedEvidenceManifest:
        _require_lead(actor)
        _require_text(approval_hash, "manifest approval hash")
        if not self._pages:
            raise EvidenceManifestBlocked("an evidence manifest requires at least one original page")
        approved_decisions = {
            decision.page_id: decision
            for decision in self._decisions.values()
            if decision.status is ReviewStatus.APPROVED
        }
        missing = set(self._pages) - set(approved_decisions)
        if missing:
            raise EvidenceManifestBlocked(f"all source pages require an approved disposition; unresolved pages: {len(missing)}")
        unresolved_groups = [group for group in self._duplicates.values() if group.status is DuplicateResolution.CANDIDATE]
        if unresolved_groups:
            raise EvidenceManifestBlocked("all duplicate page candidates must be resolved before manifest lock")
        for group in self._duplicates.values():
            if group.status is not DuplicateResolution.SAME_SOURCE_PAGE:
                continue
            included = [page_id for page_id in group.page_ids if approved_decisions[page_id].disposition is PageDisposition.INCLUDE]
            if included != [group.canonical_page_id]:
                raise EvidenceManifestBlocked("same-source duplicates must include only the approved canonical page")
        approved_annotations: dict[str, list[PageAnnotation]] = {}
        for annotation in self._annotations.values():
            if annotation.status is ReviewStatus.APPROVED:
                approved_annotations.setdefault(annotation.page_id, []).append(annotation)
        ordered_pages = sorted(
            self._pages.values(),
            key=lambda page: (self._files[page.file_id].original_label, page.file_id, page.page_number),
        )
        entries: list[ManifestPageEntry] = []
        derivative_sequence = 0
        for page in ordered_pages:
            decision = approved_decisions[page.page_id]
            sequence = None
            if decision.disposition is PageDisposition.INCLUDE:
                derivative_sequence += 1
                sequence = derivative_sequence
            entries.append(
                ManifestPageEntry(
                    page_id=page.page_id,
                    file_id=page.file_id,
                    page_number=page.page_number,
                    disposition=decision.disposition,
                    decision_id=decision.decision_id,
                    derivative_sequence=sequence,
                    annotation_ids=tuple(sorted(item.annotation_id for item in approved_annotations.get(page.page_id, ()))),
                )
            )
        payload = {
            "ledger_version": self._version,
            "entries": entries,
            "originals": tuple(sorted(self._files.values(), key=lambda item: item.file_id)),
            "duplicate_groups": tuple(sorted(self._duplicates.values(), key=lambda item: item.group_id)),
        }
        manifest = LockedEvidenceManifest(
            manifest_id=f"evidence_manifest_{uuid4().hex}",
            ledger_version=self._version,
            status=ManifestStatus.LOCKED,
            content_hash=_hash_payload(payload),
            total_pages=len(entries),
            included_pages=sum(entry.disposition is PageDisposition.INCLUDE for entry in entries),
            excluded_pages=sum(entry.disposition is PageDisposition.EXCLUDE for entry in entries),
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
            entries=tuple(entries),
        )
        self._manifests[manifest.manifest_id] = manifest
        self._version += 1
        return manifest

    def build_derivative_plan(self, manifest_id: str) -> tuple[DerivativePagePlan, ...]:
        manifest = self._manifests.get(manifest_id)
        if manifest is None or manifest.status is not ManifestStatus.LOCKED:
            raise EvidenceManifestBlocked("only a current locked manifest can build a derivative plan")
        plan: list[DerivativePagePlan] = []
        for entry in manifest.entries:
            if entry.disposition is PageDisposition.EXCLUDE:
                continue
            source = self._files[entry.file_id]
            annotations = tuple(self._annotations[annotation_id] for annotation_id in entry.annotation_ids)
            plan.append(
                DerivativePagePlan(
                    source_page_id=entry.page_id,
                    source_file_sha256=source.original_file_sha256,
                    source_page_number=entry.page_number,
                    derivative_sequence=entry.derivative_sequence or 0,
                    annotations=annotations,
                )
            )
        return tuple(plan)

    def _require_page(self, page_id: str) -> EvidencePage:
        page = self._pages.get(page_id)
        if page is None:
            raise EvidenceManifestBlocked("unknown source evidence page")
        return page

    def _invalidate_manifests(self) -> None:
        for manifest_id, manifest in list(self._manifests.items()):
            if manifest.status is ManifestStatus.LOCKED:
                self._manifests[manifest_id] = LockedEvidenceManifest(
                    manifest_id=manifest.manifest_id,
                    ledger_version=manifest.ledger_version,
                    status=ManifestStatus.INVALIDATED,
                    content_hash=manifest.content_hash,
                    total_pages=manifest.total_pages,
                    included_pages=manifest.included_pages,
                    excluded_pages=manifest.excluded_pages,
                    approved_by=manifest.approved_by,
                    approval_hash=manifest.approval_hash,
                    entries=manifest.entries,
                )


def _require_role(actor: Actor, allowed: set[Role]) -> None:
    if not actor.roles & allowed:
        raise EvidenceManifestBlocked("actor does not have a permitted evidence-manifest role")


def _require_lead(actor: Actor) -> None:
    _require_role(actor, {Role.LEAD_LAWYER})


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise EvidenceManifestBlocked(f"{label} is required")


def _validate_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise EvidenceManifestBlocked(f"{label} requires a lowercase SHA-256 value")


def _validate_rectangle(x0: float, y0: float, x1: float, y1: float) -> None:
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise EvidenceManifestBlocked("annotation rectangle must use ordered normalized coordinates within the page")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(value) for key, value in item.__dict__.items()}
        if isinstance(item, dict):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        return item

    encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
