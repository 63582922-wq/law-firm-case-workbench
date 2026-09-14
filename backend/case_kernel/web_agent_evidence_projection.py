"""Server-only evidence-page projections for the bounded Web material Agent.

The adapter accepts only an authenticated actor, a matter identifier and
registered evidence-page identifiers.  It never accepts browser-supplied PDF
bytes, text, hashes, file identifiers, object-store keys or filesystem paths.

For a human request the complete evidence snapshot is read first, so tenant
and matter membership are re-authorised by the evidence store before any
private object is resolved.  The matching per-firm ``SYSTEM_WORKER`` then
loads the immutable Web-uploaded original into a private ``0700`` workspace.
Every source/object/page binding is checked before and after bounded static
PDF text extraction.  Only page identifiers, source hashes, page numbers and
text projections leave this module; all materialized files are erased first.

Only an authenticated human case member may choose this projection.  The
separately injected worker is storage authority, never a substitute for that
human case-read authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
from tempfile import mkdtemp
from typing import Protocol
from uuid import UUID

from .evidence_manifest_postgres import (
    PersistentEvidenceSnapshot,
    WebEvidenceOriginalSourceLocator,
)
from .local_access_grants import AuthorizedOriginalFile
from .models import Actor, Role
from .original_page_access import OriginalPageLocator
from .pdf_reading_worker import read_authorized_pdf_document
from .web_agent_material_review import (
    MAX_AGENT_PAGES_PER_RUN,
    AgentEvidencePageProjection,
)
from .web_object_store import StoredWebEvidenceOriginal
from .web_pdf_page_preview import (
    WebPdfPagePreviewBlocked,
    _validate_matching_web_source,
    _verify_materialized_pdf,
)


__all__ = (
    "WebAgentEvidenceProjectionBlocked",
    "WebAgentEvidenceProjectionPolicy",
    "WebAgentEvidenceProjectionSource",
)


_HUMAN_READ_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)
_SHA256_CHARACTERS = frozenset("0123456789abcdef")


class WebAgentEvidenceProjectionBlocked(PermissionError):
    """The requested pages cannot safely become an Agent input projection."""


class _EvidenceProjectionStore(Protocol):
    def get_evidence_snapshot(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentEvidenceSnapshot: ...

    def get_web_uploaded_original_source_locator(
        self,
        *,
        matter_id: str,
        evidence_file_id: str,
        actor: Actor,
    ) -> WebEvidenceOriginalSourceLocator: ...


class _PrivateObjectStore(Protocol):
    def materialize_verified_pdf(
        self,
        stored: StoredWebEvidenceOriginal,
        *,
        destination: str | Path,
    ) -> Path: ...


@dataclass(frozen=True)
class WebAgentEvidenceProjectionPolicy:
    """Fixed limits for one server-side material-review projection."""

    worker_root: Path = field(repr=False)
    max_pages: int = MAX_AGENT_PAGES_PER_RUN
    max_source_bytes: int = 100 * 1024 * 1024
    max_total_source_bytes: int = 256 * 1024 * 1024
    max_page_text_bytes: int = 24 * 1024
    max_total_text_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        root = _validated_private_worker_root(self.worker_root)
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= MAX_AGENT_PAGES_PER_RUN:
            raise ValueError("Web Agent projection page limit is invalid")
        if type(self.max_source_bytes) is not int or not 1 <= self.max_source_bytes <= 100 * 1024 * 1024:
            raise ValueError("Web Agent projection source-byte limit is invalid")
        if (
            type(self.max_total_source_bytes) is not int
            or self.max_total_source_bytes < self.max_source_bytes
            or self.max_total_source_bytes > 2 * 1024 * 1024 * 1024
        ):
            raise ValueError("Web Agent projection total source-byte limit is invalid")
        if type(self.max_page_text_bytes) is not int or not 1 <= self.max_page_text_bytes <= 24 * 1024:
            raise ValueError("Web Agent projection page-text limit is invalid")
        if (
            type(self.max_total_text_bytes) is not int
            or self.max_total_text_bytes < self.max_page_text_bytes
            or self.max_total_text_bytes > 800 * 1024
        ):
            raise ValueError("Web Agent projection total text limit is invalid")
        object.__setattr__(self, "worker_root", root)


@dataclass(frozen=True)
class _RegisteredOriginal:
    evidence_file_id: str
    original_file_sha256: str
    byte_size: int
    media_type: str
    page_count: int


@dataclass(frozen=True)
class _RegisteredPage:
    evidence_page_id: str
    evidence_file_id: str
    page_number: int


class WebAgentEvidenceProjectionSource:
    """Build hash-bound, page-scoped Agent input from private Web evidence."""

    def __init__(
        self,
        *,
        evidence_store: _EvidenceProjectionStore,
        object_store: _PrivateObjectStore,
        system_worker_for_firm,
        policy: WebAgentEvidenceProjectionPolicy,
    ) -> None:
        for method in ("get_evidence_snapshot", "get_web_uploaded_original_source_locator"):
            if not callable(getattr(evidence_store, method, None)):
                raise ValueError("Web Agent evidence store is invalid")
        if not callable(getattr(object_store, "materialize_verified_pdf", None)):
            raise ValueError("Web Agent private object store is invalid")
        if not callable(system_worker_for_firm):
            raise ValueError("Web Agent system-worker resolver is invalid")
        if not isinstance(policy, WebAgentEvidenceProjectionPolicy):
            raise ValueError("Web Agent evidence projection policy is invalid")
        self._evidence_store = evidence_store
        self._object_store = object_store
        self._system_worker_for_firm = system_worker_for_firm
        self._policy = policy

    def __repr__(self) -> str:
        return "WebAgentEvidenceProjectionSource(<server-private>)"

    def load_pages(
        self,
        *,
        actor: Actor,
        matter_id: str,
        evidence_page_ids: tuple[str, ...],
    ) -> tuple[AgentEvidencePageProjection, ...]:
        _validate_request(
            actor=actor,
            matter_id=matter_id,
            evidence_page_ids=evidence_page_ids,
            maximum_pages=self._policy.max_pages,
        )
        try:
            snapshot = self._evidence_store.get_evidence_snapshot(
                matter_id=matter_id,
                actor=actor,
            )
        except Exception:
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable") from None
        originals, pages = _validated_snapshot(
            snapshot,
            actor=actor,
            matter_id=matter_id,
            requested_page_ids=evidence_page_ids,
            max_source_bytes=self._policy.max_source_bytes,
            max_total_source_bytes=self._policy.max_total_source_bytes,
        )
        # Resolve storage authority only after the human evidence snapshot has
        # proved the complete page selection belongs to this visible matter.
        worker = self._resolve_worker(actor)

        workspace = _create_private_workspace(self._policy.worker_root)
        try:
            page_text = self._materialize_and_read(
                workspace=workspace,
                actor=actor,
                worker=worker,
                matter_id=matter_id,
                originals=originals,
                pages=pages,
            )
            total_text_bytes = 0
            projections: list[AgentEvidencePageProjection] = []
            for page_id in evidence_page_ids:
                page = pages[page_id]
                original = originals[page.evidence_file_id]
                text = page_text[(page.evidence_file_id, page.page_number)]
                encoded = text.encode("utf-8")
                if len(encoded) > self._policy.max_page_text_bytes:
                    raise WebAgentEvidenceProjectionBlocked(
                        "one evidence page exceeds the Agent text boundary"
                    )
                total_text_bytes += len(encoded)
                if total_text_bytes > self._policy.max_total_text_bytes:
                    raise WebAgentEvidenceProjectionBlocked(
                        "the evidence selection exceeds the Agent text boundary"
                    )
                projections.append(
                    AgentEvidencePageProjection.build(
                        evidence_page_id=page.evidence_page_id,
                        source_file_sha256=original.original_file_sha256,
                        page_number=page.page_number,
                        extracted_text=text,
                    )
                )
            return tuple(projections)
        except WebAgentEvidenceProjectionBlocked:
            raise
        except Exception:
            # Never let an object key, local path, PDF parser detail or source
            # label become browser-visible through an exception message.
            raise WebAgentEvidenceProjectionBlocked(
                "the requested evidence pages could not be projected"
            ) from None
        finally:
            _erase_private_workspace(workspace, root=self._policy.worker_root)

    def _resolve_worker(self, actor: Actor) -> Actor:
        try:
            worker = self._system_worker_for_firm(actor.firm_id)
        except Exception:
            raise WebAgentEvidenceProjectionBlocked("the Web Agent worker is unavailable") from None
        _validate_system_worker(worker, firm_id=actor.firm_id)
        return worker

    def _materialize_and_read(
        self,
        *,
        workspace: Path,
        actor: Actor,
        worker: Actor,
        matter_id: str,
        originals: dict[str, _RegisteredOriginal],
        pages: dict[str, _RegisteredPage],
    ) -> dict[tuple[str, int], str]:
        selected_by_file: dict[str, list[_RegisteredPage]] = {}
        for page in pages.values():
            selected_by_file.setdefault(page.evidence_file_id, []).append(page)
        extracted: dict[tuple[str, int], str] = {}
        for sequence, evidence_file_id in enumerate(sorted(selected_by_file), start=1):
            original = originals[evidence_file_id]
            selected_pages = selected_by_file[evidence_file_id]
            try:
                source_locator = self._evidence_store.get_web_uploaded_original_source_locator(
                    matter_id=matter_id,
                    evidence_file_id=evidence_file_id,
                    actor=worker,
                )
            except Exception:
                raise WebAgentEvidenceProjectionBlocked(
                    "the requested evidence pages are unavailable"
                ) from None
            first_page = selected_pages[0]
            page_locator = OriginalPageLocator(
                firm_id=actor.firm_id,
                matter_id=matter_id,
                evidence_page_id=first_page.evidence_page_id,
                evidence_file_id=evidence_file_id,
                original_label="server-registered-evidence.pdf",
                original_file_sha256=original.original_file_sha256,
                byte_size=original.byte_size,
                media_type=original.media_type,
                page_count=original.page_count,
                page_number=first_page.page_number,
            )
            try:
                stored = _validate_matching_web_source(
                    source_locator,
                    page_locator=page_locator,
                    firm_id=actor.firm_id,
                    matter_id=matter_id,
                )
            except WebPdfPagePreviewBlocked:
                raise WebAgentEvidenceProjectionBlocked(
                    "the requested evidence pages are unavailable"
                ) from None
            source_path = workspace / f"source-{sequence:04d}.pdf"
            try:
                materialized = self._object_store.materialize_verified_pdf(
                    stored,
                    destination=source_path,
                )
            except Exception:
                raise WebAgentEvidenceProjectionBlocked(
                    "the requested evidence pages are unavailable"
                ) from None
            if not isinstance(materialized, Path) or materialized != source_path:
                raise WebAgentEvidenceProjectionBlocked("private evidence materialization is invalid")
            try:
                _verify_materialized_pdf(
                    source_path,
                    expected_sha256=original.original_file_sha256,
                    expected_byte_size=original.byte_size,
                    expected_page_count=original.page_count,
                    requested_page_number=first_page.page_number,
                    max_source_bytes=self._policy.max_source_bytes,
                )
                result = read_authorized_pdf_document(
                    AuthorizedOriginalFile(
                        relative_path="server-registered-evidence.pdf",
                        path=source_path,
                        byte_size=original.byte_size,
                        sha256=original.original_file_sha256,
                    )
                )
            except Exception:
                raise WebAgentEvidenceProjectionBlocked(
                    "private evidence extraction is invalid"
                ) from None
            if result.source_sha256 != original.original_file_sha256 or len(result.pages) != original.page_count:
                raise WebAgentEvidenceProjectionBlocked("private evidence extraction is invalid")
            for page in selected_pages:
                try:
                    value = result.pages[page.page_number - 1]
                except IndexError:
                    raise WebAgentEvidenceProjectionBlocked("private evidence extraction is invalid") from None
                if value.page_number != page.page_number or not isinstance(value.text, str):
                    raise WebAgentEvidenceProjectionBlocked("private evidence extraction is invalid")
                extracted[(evidence_file_id, page.page_number)] = value.text
            # Detect mutation while parsing before any projection leaves the
            # private workspace.
            try:
                _verify_materialized_pdf(
                    source_path,
                    expected_sha256=original.original_file_sha256,
                    expected_byte_size=original.byte_size,
                    expected_page_count=original.page_count,
                    requested_page_number=first_page.page_number,
                    max_source_bytes=self._policy.max_source_bytes,
                )
            except WebPdfPagePreviewBlocked:
                raise WebAgentEvidenceProjectionBlocked(
                    "private evidence extraction is invalid"
                ) from None
        return extracted


def _validate_request(
    *,
    actor: Actor,
    matter_id: str,
    evidence_page_ids: tuple[str, ...],
    maximum_pages: int,
) -> None:
    if not isinstance(actor, Actor):
        raise WebAgentEvidenceProjectionBlocked("Web Agent projection requires an authenticated actor")
    _require_uuid(actor.actor_id)
    _require_uuid(actor.firm_id)
    _require_uuid(matter_id)
    if not isinstance(actor.roles, frozenset) or not actor.roles:
        raise WebAgentEvidenceProjectionBlocked("Web Agent projection actor is invalid")
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_HUMAN_READ_ROLES):
        raise WebAgentEvidenceProjectionBlocked("Web Agent projection requires case read permission")
    if not isinstance(evidence_page_ids, tuple) or not 1 <= len(evidence_page_ids) <= maximum_pages:
        raise WebAgentEvidenceProjectionBlocked("Web Agent evidence-page selection is invalid")
    for page_id in evidence_page_ids:
        _require_uuid(page_id)
    if len(set(evidence_page_ids)) != len(evidence_page_ids):
        raise WebAgentEvidenceProjectionBlocked("Web Agent evidence-page selection is invalid")


def _validated_snapshot(
    snapshot: object,
    *,
    actor: Actor,
    matter_id: str,
    requested_page_ids: tuple[str, ...],
    max_source_bytes: int,
    max_total_source_bytes: int,
) -> tuple[dict[str, _RegisteredOriginal], dict[str, _RegisteredPage]]:
    if not isinstance(snapshot, PersistentEvidenceSnapshot) or snapshot.matter_id != matter_id:
        raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
    if type(snapshot.version) is not int or snapshot.version < 1:
        raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
    originals: dict[str, _RegisteredOriginal] = {}
    for item in snapshot.original_files:
        if not isinstance(item, dict):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
        try:
            original = _RegisteredOriginal(
                evidence_file_id=item["evidence_file_id"],
                original_file_sha256=item["original_file_sha256"],
                byte_size=item["byte_size"],
                media_type=item["media_type"],
                page_count=item["page_count"],
            )
        except (KeyError, TypeError):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable") from None
        _require_uuid(original.evidence_file_id)
        _require_sha256(original.original_file_sha256)
        if (
            not isinstance(original.media_type, str)
            or not original.media_type
            or type(original.byte_size) is not int
            or not 1 <= original.byte_size <= 2 * 1024 * 1024 * 1024
            or type(original.page_count) is not int
            or not 1 <= original.page_count <= 10_000
            or original.evidence_file_id in originals
        ):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
        originals[original.evidence_file_id] = original

    requested = set(requested_page_ids)
    pages: dict[str, _RegisteredPage] = {}
    source_locations: set[tuple[str, int]] = set()
    for item in snapshot.pages:
        if not isinstance(item, dict):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
        page_id = item.get("evidence_page_id")
        if page_id not in requested:
            continue
        try:
            page = _RegisteredPage(
                evidence_page_id=page_id,
                evidence_file_id=item["evidence_file_id"],
                page_number=item["page_number"],
            )
        except (KeyError, TypeError):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable") from None
        _require_uuid(page.evidence_page_id)
        _require_uuid(page.evidence_file_id)
        original = originals.get(page.evidence_file_id)
        location = (page.evidence_file_id, page.page_number)
        if (
            original is None
            or type(page.page_number) is not int
            or not 1 <= page.page_number <= original.page_count
            or page.evidence_page_id in pages
            or location in source_locations
        ):
            raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
        pages[page.evidence_page_id] = page
        source_locations.add(location)
    if set(pages) != requested:
        raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
    selected_file_ids = {page.evidence_file_id for page in pages.values()}
    if any(
        originals[file_id].media_type != "application/pdf"
        or originals[file_id].byte_size > max_source_bytes
        for file_id in selected_file_ids
    ):
        raise WebAgentEvidenceProjectionBlocked("the requested evidence pages are unavailable")
    total_source_bytes = sum(originals[file_id].byte_size for file_id in selected_file_ids)
    if total_source_bytes > max_total_source_bytes:
        raise WebAgentEvidenceProjectionBlocked("the evidence selection exceeds the source boundary")
    # ``actor`` is deliberately used only to bind this already-authorized
    # snapshot to its firm; no browser-supplied firm field exists here.
    _require_uuid(actor.firm_id)
    return originals, pages


def _validate_system_worker(actor: object, *, firm_id: str) -> None:
    if not isinstance(actor, Actor):
        raise WebAgentEvidenceProjectionBlocked("the Web Agent worker is unavailable")
    _require_uuid(actor.actor_id)
    _require_uuid(actor.firm_id)
    if actor.firm_id != firm_id or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise WebAgentEvidenceProjectionBlocked("the Web Agent worker is unavailable")


def _validated_private_worker_root(value: Path) -> Path:
    try:
        root = Path(value)
    except TypeError as error:
        raise ValueError("Web Agent worker root is invalid") from error
    if not root.is_absolute():
        raise ValueError("Web Agent worker root must be absolute")
    _assert_no_symlink_components(root)
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise ValueError("Web Agent worker root is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("Web Agent worker root must be an existing 0700 directory")
    return root


def _create_private_workspace(root: Path) -> Path:
    _validated_private_worker_root(root)
    try:
        workspace = Path(mkdtemp(prefix="web-agent-projection-", dir=root))
        workspace.chmod(0o700)
        metadata = os.lstat(workspace)
    except OSError:
        raise WebAgentEvidenceProjectionBlocked("the Web Agent workspace is unavailable") from None
    if (
        workspace.parent != root
        or not workspace.name.startswith("web-agent-projection-")
        or workspace.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        try:
            shutil.rmtree(workspace)
        except OSError:
            pass
        raise WebAgentEvidenceProjectionBlocked("the Web Agent workspace is unavailable")
    return workspace


def _erase_private_workspace(workspace: Path, *, root: Path) -> None:
    try:
        if (
            not isinstance(workspace, Path)
            or workspace.parent != root
            or not workspace.name.startswith("web-agent-projection-")
            or workspace.is_symlink()
        ):
            raise OSError("invalid workspace")
        metadata = os.lstat(workspace)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OSError("invalid workspace")
        shutil.rmtree(workspace)
    except OSError:
        raise WebAgentEvidenceProjectionBlocked("the Web Agent workspace cleanup failed") from None


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[len(current.parts) :]:
        if component in {"", ".", ".."}:
            raise ValueError("Web Agent worker root is invalid")
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ValueError("Web Agent worker root is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Web Agent worker root cannot use symbolic links")


def _require_uuid(value: object) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebAgentEvidenceProjectionBlocked("Web Agent identifier is invalid") from error


def _require_sha256(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        raise WebAgentEvidenceProjectionBlocked("Web Agent source binding is invalid")
