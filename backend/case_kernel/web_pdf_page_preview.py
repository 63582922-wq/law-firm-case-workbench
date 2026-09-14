"""Server-only, one-page PNG previews for Web-uploaded evidence PDFs.

The browser never receives a PDF, an object-store key, a storage version, or a
worker path.  A human actor first resolves an evidence-page locator through
the tenant evidence store; a separately supplied, server-owned system worker
then resolves the matching private source-object binding.  The service
materializes that object in a private worker directory, renders exactly the
registered page with a configured absolute ``pdftoppm`` executable, validates
the resulting PNG with Pillow, and removes all temporary material before
returning only browser-safe raster bytes.

This module deliberately has no HTTP, session, desktop-folder, local-grant,
or public-object-store concerns.  A Web route must authenticate its human
actor before calling :meth:`WebPdfPagePreviewService.render_page` and must
return the result with non-cacheable response headers.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
from tempfile import mkdtemp
from typing import Any, Iterator, Protocol
from uuid import UUID

from PIL import Image
from pypdf import PdfReader

from .evidence_manifest_postgres import WebEvidenceOriginalSourceLocator
from .models import Actor, Role
from .original_page_access import OriginalPageLocator
from .web_object_store import StoredWebEvidenceOriginal


__all__ = (
    "SystemWorkerActorProvider",
    "WebEvidencePagePreviewStore",
    "WebPdfPagePreview",
    "WebPdfPagePreviewBlocked",
    "WebPdfPagePreviewPolicy",
    "WebPdfPagePreviewService",
)


_HUMAN_READ_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY = re.compile(
    r"^originals/v1/"
    r"(?P<firm>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"(?P<matter>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"(?P<prefix>[0-9a-f]{2})/(?P<sha256>[0-9a-f]{64})/"
    r"(?P<object>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.pdf$"
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class WebPdfPagePreviewBlocked(PermissionError):
    """The service cannot safely expose the requested rendered evidence page."""


class WebEvidencePagePreviewStore(Protocol):
    """The two separately authorized evidence-store reads used by this service."""

    def get_original_page_locator(
        self,
        *,
        matter_id: str,
        evidence_page_id: str,
        actor: Actor,
    ) -> OriginalPageLocator: ...

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


class SystemWorkerActorProvider(Protocol):
    """Return the one server-owned worker actor for the requested firm."""

    def __call__(self, firm_id: str) -> Actor: ...


@dataclass(frozen=True)
class WebPdfPagePreviewPolicy:
    """Fixed server configuration for bounded private page rendering.

    ``worker_root`` and ``pdftoppm_executable`` must both be physical absolute
    paths.  The root is intentionally pre-created at mode ``0700`` rather than
    implicitly created from a browser request; this prevents a deployment from
    accidentally rendering into a served, shared, or user-selected folder.
    """

    worker_root: str | Path
    pdftoppm_executable: str | Path
    rendered_dpi: int = 144
    render_timeout_seconds: int = 30
    max_source_bytes: int = 256 * 1024 * 1024
    max_png_bytes: int = 32 * 1024 * 1024
    max_png_dimension: int = 10_000
    max_png_pixels: int = 40_000_000

    def __post_init__(self) -> None:
        root = _validated_private_worker_root(self.worker_root)
        renderer = _validated_renderer_executable(self.pdftoppm_executable)
        if type(self.rendered_dpi) is not int or not 72 <= self.rendered_dpi <= 200:
            raise ValueError("Web PDF preview DPI must be between 72 and 200")
        if type(self.render_timeout_seconds) is not int or not 1 <= self.render_timeout_seconds <= 60:
            raise ValueError("Web PDF preview render timeout must be between 1 and 60 seconds")
        if type(self.max_source_bytes) is not int or not 1 <= self.max_source_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("Web PDF preview source-byte limit is invalid")
        if type(self.max_png_bytes) is not int or not 24 <= self.max_png_bytes <= 128 * 1024 * 1024:
            raise ValueError("Web PDF preview PNG-byte limit is invalid")
        if type(self.max_png_dimension) is not int or not 64 <= self.max_png_dimension <= 20_000:
            raise ValueError("Web PDF preview PNG dimension limit is invalid")
        if type(self.max_png_pixels) is not int or not 4_096 <= self.max_png_pixels <= 100_000_000:
            raise ValueError("Web PDF preview PNG pixel limit is invalid")
        if self.max_png_pixels < self.max_png_dimension:
            raise ValueError("Web PDF preview PNG pixel limit is invalid")
        object.__setattr__(self, "worker_root", root)
        object.__setattr__(self, "pdftoppm_executable", renderer)


@dataclass(frozen=True)
class WebPdfPagePreview:
    """The only page-preview payload appropriate for a browser response.

    The PNG is intentionally omitted from ``repr`` and comparisons.  No
    original filename, PDF bytes, object key, storage version, or worker path
    is represented here.
    """

    evidence_page_id: str
    content_sha256: str
    width: int
    height: int
    png_content: bytes = field(repr=False, compare=False)
    media_type: str = "image/png"


class WebPdfPagePreviewService:
    """Materialize and rasterize one authorized Web evidence PDF page.

    The service assumes its caller has already established an authenticated
    opaque Web session.  It independently rejects a system worker supplied as
    the human actor and asks the store to re-authorize both the human page
    read and the server-owned worker object read.
    """

    def __init__(
        self,
        *,
        evidence_store: WebEvidencePagePreviewStore,
        object_store: _PrivateObjectStore,
        system_worker_for_firm: SystemWorkerActorProvider,
        policy: WebPdfPagePreviewPolicy,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        for method in ("get_original_page_locator", "get_web_uploaded_original_source_locator"):
            if not callable(getattr(evidence_store, method, None)):
                raise ValueError("Web PDF preview evidence store is invalid")
        if not callable(getattr(object_store, "materialize_verified_pdf", None)):
            raise ValueError("Web PDF preview object store is invalid")
        if not callable(system_worker_for_firm):
            raise ValueError("Web PDF preview system-worker resolver is invalid")
        if not isinstance(policy, WebPdfPagePreviewPolicy):
            raise ValueError("Web PDF preview policy is invalid")
        if runner is not None and not callable(runner):
            raise ValueError("Web PDF preview process runner is invalid")
        self._evidence_store = evidence_store
        self._object_store = object_store
        self._system_worker_for_firm = system_worker_for_firm
        self._policy = policy
        self._runner = runner or subprocess.run

    def render_page(
        self,
        *,
        actor: Actor,
        matter_id: str,
        evidence_page_id: str,
    ) -> WebPdfPagePreview:
        """Return one bounded PNG only after the complete Web provenance check.

        Any authorization, source-binding, materialization, PDF parsing,
        renderer, Pillow, or cleanup failure is intentionally collapsed into
        :class:`WebPdfPagePreviewBlocked`.  This prevents private object-store
        and worker details from becoming browser-visible error data.
        """

        _validate_human_request(actor=actor, matter_id=matter_id, evidence_page_id=evidence_page_id)
        try:
            page_locator = self._evidence_store.get_original_page_locator(
                matter_id=matter_id,
                evidence_page_id=evidence_page_id,
                actor=actor,
            )
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
        _validate_page_locator(
            page_locator,
            actor=actor,
            matter_id=matter_id,
            evidence_page_id=evidence_page_id,
            max_source_bytes=self._policy.max_source_bytes,
        )

        try:
            worker = self._system_worker_for_firm(actor.firm_id)
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
        _validate_system_worker(worker, firm_id=actor.firm_id)
        try:
            source_locator = self._evidence_store.get_web_uploaded_original_source_locator(
                matter_id=matter_id,
                evidence_file_id=page_locator.evidence_file_id,
                actor=worker,
            )
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
        stored = _validate_matching_web_source(
            source_locator,
            page_locator=page_locator,
            firm_id=actor.firm_id,
            matter_id=matter_id,
        )

        worker_directory = _create_private_preview_directory(self._policy.worker_root)
        try:
            return self._render_from_private_source(
                worker_directory=worker_directory,
                stored=stored,
                page_locator=page_locator,
            )
        except WebPdfPagePreviewBlocked:
            raise
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page could not be rendered") from None
        finally:
            _erase_private_preview_directory(worker_directory, root=self._policy.worker_root)

    def _render_from_private_source(
        self,
        *,
        worker_directory: Path,
        stored: StoredWebEvidenceOriginal,
        page_locator: OriginalPageLocator,
    ) -> WebPdfPagePreview:
        source_path = worker_directory / "source.pdf"
        try:
            materialized = self._object_store.materialize_verified_pdf(stored, destination=source_path)
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
        if not isinstance(materialized, Path) or materialized != source_path:
            raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")

        _verify_materialized_pdf(
            source_path,
            expected_sha256=page_locator.original_file_sha256,
            expected_byte_size=page_locator.byte_size,
            expected_page_count=page_locator.page_count,
            requested_page_number=page_locator.page_number,
            max_source_bytes=self._policy.max_source_bytes,
        )

        png_path = worker_directory / "page.png"
        command = [
            str(self._policy.pdftoppm_executable),
            "-f",
            str(page_locator.page_number),
            "-l",
            str(page_locator.page_number),
            "-singlefile",
            "-r",
            str(self._policy.rendered_dpi),
            "-png",
            str(source_path),
            str(png_path.with_suffix("")),
        ]
        try:
            completed = self._runner(
                command,
                check=False,
                capture_output=True,
                timeout=self._policy.render_timeout_seconds,
                cwd=str(worker_directory),
                stdin=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise WebPdfPagePreviewBlocked("the requested evidence page could not be rendered") from None
        except Exception:
            raise WebPdfPagePreviewBlocked("the requested evidence page could not be rendered") from None
        if type(getattr(completed, "returncode", None)) is not int or completed.returncode != 0:
            raise WebPdfPagePreviewBlocked("the requested evidence page could not be rendered")

        content, width, height = _read_validated_png(
            png_path,
            maximum_bytes=self._policy.max_png_bytes,
            maximum_dimension=self._policy.max_png_dimension,
            maximum_pixels=self._policy.max_png_pixels,
        )
        # Object-store materialization verifies before and after download, but
        # this second local hash closes the rendering-time race as well.
        _verify_materialized_pdf(
            source_path,
            expected_sha256=page_locator.original_file_sha256,
            expected_byte_size=page_locator.byte_size,
            expected_page_count=page_locator.page_count,
            requested_page_number=page_locator.page_number,
            max_source_bytes=self._policy.max_source_bytes,
        )
        return WebPdfPagePreview(
            evidence_page_id=page_locator.evidence_page_id,
            content_sha256=sha256(content).hexdigest(),
            width=width,
            height=height,
            png_content=content,
        )


def _validate_human_request(*, actor: Actor, matter_id: str, evidence_page_id: str) -> None:
    if not isinstance(actor, Actor):
        raise WebPdfPagePreviewBlocked("Web PDF preview requires an authenticated human actor")
    _validate_uuid(actor.actor_id, label="Web PDF preview actor")
    _validate_uuid(actor.firm_id, label="Web PDF preview firm")
    _validate_uuid(matter_id, label="Web PDF preview matter")
    _validate_uuid(evidence_page_id, label="Web PDF preview evidence page")
    if (
        not isinstance(actor.roles, frozenset)
        or not actor.roles
        or not all(isinstance(role, Role) for role in actor.roles)
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_HUMAN_READ_ROLES)
    ):
        raise WebPdfPagePreviewBlocked("Web PDF preview requires an authorized human actor")


def _validate_system_worker(actor: Actor, *, firm_id: str) -> None:
    if not isinstance(actor, Actor):
        raise WebPdfPagePreviewBlocked("Web PDF preview system worker is invalid")
    _validate_uuid(actor.actor_id, label="Web PDF preview system worker")
    _validate_uuid(actor.firm_id, label="Web PDF preview system worker firm")
    if actor.firm_id != firm_id or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise WebPdfPagePreviewBlocked("Web PDF preview system worker is invalid")


def _validate_page_locator(
    locator: object,
    *,
    actor: Actor,
    matter_id: str,
    evidence_page_id: str,
    max_source_bytes: int,
) -> None:
    if not isinstance(locator, OriginalPageLocator):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    for value, label in (
        (locator.firm_id, "Web PDF preview locator firm"),
        (locator.matter_id, "Web PDF preview locator matter"),
        (locator.evidence_page_id, "Web PDF preview locator page"),
        (locator.evidence_file_id, "Web PDF preview locator original"),
    ):
        _validate_uuid(value, label=label)
    if (
        locator.firm_id != actor.firm_id
        or locator.matter_id != matter_id
        or locator.evidence_page_id != evidence_page_id
        or locator.media_type != "application/pdf"
        or locator.normalized_pdf_object_key is not None
        or locator.normalized_pdf_sha256 is not None
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    _validate_sha256(locator.original_file_sha256, label="registered Web evidence PDF")
    if (
        type(locator.byte_size) is not int
        or not 1 <= locator.byte_size <= max_source_bytes
        or type(locator.page_count) is not int
        or not 1 <= locator.page_count <= 10_000
        or type(locator.page_number) is not int
        or not 1 <= locator.page_number <= locator.page_count
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")


def _validate_matching_web_source(
    source_locator: object,
    *,
    page_locator: OriginalPageLocator,
    firm_id: str,
    matter_id: str,
) -> StoredWebEvidenceOriginal:
    if not isinstance(source_locator, WebEvidenceOriginalSourceLocator):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    for value, label in (
        (source_locator.firm_id, "Web source locator firm"),
        (source_locator.matter_id, "Web source locator matter"),
        (source_locator.evidence_file_id, "Web source locator original"),
    ):
        _validate_uuid(value, label=label)
    if (
        source_locator.firm_id != firm_id
        or source_locator.matter_id != matter_id
        or source_locator.evidence_file_id != page_locator.evidence_file_id
        or source_locator.original_file_sha256 != page_locator.original_file_sha256
        or source_locator.byte_size != page_locator.byte_size
        or source_locator.page_count != page_locator.page_count
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    _validate_sha256(source_locator.source_reference_hash, label="Web source reference")
    try:
        stored = source_locator.stored_object()
    except Exception:
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
    if not isinstance(stored, StoredWebEvidenceOriginal):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    if (
        stored.content_sha256 != page_locator.original_file_sha256
        or stored.byte_size != page_locator.byte_size
        or stored.object_version_id != source_locator.object_version_id
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    _validate_web_object_key(
        stored.object_key,
        firm_id=firm_id,
        matter_id=matter_id,
        expected_sha256=page_locator.original_file_sha256,
    )
    try:
        reference_hash = sha256(stored.object_key.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable") from None
    if reference_hash != source_locator.source_reference_hash:
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    if stored.object_version_id is not None and (
        not isinstance(stored.object_version_id, str)
        or not stored.object_version_id
        or len(stored.object_version_id) > 512
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    return stored


def _validate_web_object_key(
    value: object,
    *,
    firm_id: str,
    matter_id: str,
    expected_sha256: str,
) -> None:
    if not isinstance(value, str) or len(value) > 512:
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    matched = _OBJECT_KEY.fullmatch(value)
    if matched is None:
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")
    if (
        str(UUID(matched.group("firm"))) != firm_id
        or str(UUID(matched.group("matter"))) != matter_id
        or matched.group("prefix") != expected_sha256[:2]
        or matched.group("sha256") != expected_sha256
    ):
        raise WebPdfPagePreviewBlocked("the requested evidence page is unavailable")


def _verify_materialized_pdf(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_size: int,
    expected_page_count: int,
    requested_page_number: int,
    max_source_bytes: int,
) -> None:
    _validate_sha256(expected_sha256, label="registered Web evidence PDF")
    if (
        type(expected_byte_size) is not int
        or not 1 <= expected_byte_size <= max_source_bytes
        or type(expected_page_count) is not int
        or not 1 <= expected_page_count <= 10_000
        or type(requested_page_number) is not int
        or not 1 <= requested_page_number <= expected_page_count
    ):
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
    try:
        with _open_private_regular_file(path, maximum_bytes=max_source_bytes) as source:
            metadata = os.fstat(source.fileno())
            if metadata.st_size != expected_byte_size:
                raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
            digest = sha256()
            while block := source.read(1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
            source.seek(0)
            if source.read(5) != b"%PDF-":
                raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
            source.seek(0)
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted or len(reader.pages) != expected_page_count:
                raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
            # Resolve only the requested page's object after checking the
            # bounded document structure.  pdftoppm receives the matching
            # first/last page arguments below, so no other page is emitted.
            page = reader.pages[requested_page_number - 1]
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
    except WebPdfPagePreviewBlocked:
        raise
    except Exception:
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid") from None


def _read_validated_png(
    path: Path,
    *,
    maximum_bytes: int,
    maximum_dimension: int,
    maximum_pixels: int,
) -> tuple[bytes, int, int]:
    try:
        with _open_private_regular_file(path, maximum_bytes=maximum_bytes) as source:
            metadata = os.fstat(source.fileno())
            if metadata.st_size < 24:
                raise WebPdfPagePreviewBlocked("rendered evidence page is invalid")
            content = source.read()
            if len(content) != metadata.st_size:
                raise WebPdfPagePreviewBlocked("rendered evidence page is invalid")
    except WebPdfPagePreviewBlocked:
        raise
    except Exception:
        raise WebPdfPagePreviewBlocked("rendered evidence page is invalid") from None
    width, height = _png_header_dimensions(content)
    if (
        width > maximum_dimension
        or height > maximum_dimension
        or width * height > maximum_pixels
    ):
        raise WebPdfPagePreviewBlocked("rendered evidence page dimensions exceed the preview limit")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or getattr(image, "is_animated", False):
                raise WebPdfPagePreviewBlocked("renderer returned an invalid evidence page")
            if image.size != (width, height):
                raise WebPdfPagePreviewBlocked("renderer returned an invalid evidence page")
            image.verify()
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise WebPdfPagePreviewBlocked("renderer returned an invalid evidence page")
            image.load()
    except WebPdfPagePreviewBlocked:
        raise
    except Exception:
        raise WebPdfPagePreviewBlocked("renderer returned an invalid evidence page") from None
    return content, width, height


def _png_header_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != _PNG_SIGNATURE or content[8:16] != b"\x00\x00\x00\rIHDR":
        raise WebPdfPagePreviewBlocked("renderer returned a non-PNG evidence page")
    width, height = struct.unpack(">II", content[16:24])
    if width < 1 or height < 1:
        raise WebPdfPagePreviewBlocked("renderer returned invalid evidence-page dimensions")
    return width, height


@contextmanager
def _open_private_regular_file(path: Path, *, maximum_bytes: int) -> Iterator[Any]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
    try:
        initial = os.lstat(path)
    except OSError:
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid") from None
    if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
    if initial.st_size < 1 or initial.st_size > maximum_bytes:
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        source = os.fdopen(descriptor, "rb")
    except OSError:
        raise WebPdfPagePreviewBlocked("private evidence materialization is invalid") from None
    try:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != initial.st_dev
            or opened.st_ino != initial.st_ino
            or opened.st_size != initial.st_size
        ):
            raise WebPdfPagePreviewBlocked("private evidence materialization is invalid")
        yield source
    finally:
        source.close()


def _validated_private_worker_root(value: str | Path) -> Path:
    try:
        root = Path(value)
    except TypeError as error:
        raise ValueError("Web PDF preview worker root is invalid") from error
    if not root.is_absolute():
        raise ValueError("Web PDF preview worker root must be an absolute path")
    _assert_no_symbolic_link_components(root, label="Web PDF preview worker root")
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise ValueError("Web PDF preview worker root is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("Web PDF preview worker root must be an existing 0700 directory")
    return root


def _validated_renderer_executable(value: str | Path) -> Path:
    try:
        executable = Path(value)
    except TypeError as error:
        raise ValueError("Web PDF preview renderer is invalid") from error
    if not executable.is_absolute():
        raise ValueError("Web PDF preview renderer must be an absolute path")
    _assert_no_symbolic_link_components(executable, label="Web PDF preview renderer")
    try:
        metadata = os.lstat(executable)
    except OSError as error:
        raise ValueError("Web PDF preview renderer is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not (metadata.st_mode & 0o111):
        raise ValueError("Web PDF preview renderer must be an executable regular file")
    return executable


def _create_private_preview_directory(root: Path) -> Path:
    _validated_private_worker_root(root)
    try:
        directory = Path(mkdtemp(prefix="web-pdf-preview-", dir=root))
        directory.chmod(0o700)
        metadata = os.lstat(directory)
    except OSError:
        raise WebPdfPagePreviewBlocked("private preview workspace is unavailable") from None
    if (
        directory.parent != root
        or not directory.name.startswith("web-pdf-preview-")
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or directory.is_symlink()
    ):
        try:
            shutil.rmtree(directory)
        except OSError:
            pass
        raise WebPdfPagePreviewBlocked("private preview workspace is unavailable")
    return directory


def _erase_private_preview_directory(directory: Path, *, root: Path) -> None:
    """Remove every temporary source and raster before any response is returned."""

    try:
        if (
            not isinstance(directory, Path)
            or directory.parent != root
            or not directory.name.startswith("web-pdf-preview-")
            or directory.is_symlink()
        ):
            raise OSError("unexpected preview workspace")
        metadata = os.lstat(directory)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OSError("unexpected preview workspace")
        shutil.rmtree(directory)
    except OSError:
        # Do not return a preview if the materialized original or raster could
        # remain in a supposedly temporary worker location.
        raise WebPdfPagePreviewBlocked("private preview workspace cleanup failed") from None


def _assert_no_symbolic_link_components(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    current = Path(path.anchor)
    for component in path.parts[len(current.parts) :]:
        if component in {"", ".", ".."}:
            raise ValueError(f"{label} is invalid")
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ValueError(f"{label} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} cannot use symbolic links")


def _validate_uuid(value: object, *, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebPdfPagePreviewBlocked(f"{label} is invalid") from error


def _validate_sha256(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WebPdfPagePreviewBlocked(f"{label} SHA-256 is invalid")
