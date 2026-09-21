"""Private browser-upload staging before a source enters a Web case record.

The browser never supplies a trusted path, file hash, media type, page count,
firm or matter identity.  This module therefore does only the first, bounded
server-side step: stream one upload into a private staging directory, derive
its identity from bytes, and run the existing anti-malware/PDF inspection.

It intentionally has no HTTP route and no object-store implementation.  A Web
composition root must authorize the case first, then hand an admitted file to
a server-owned object store and evidence ledger in a recoverable workflow.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from unicodedata import category
from uuid import uuid4

from .evidence_intake_worker import (
    EvidenceIntakeBlocked,
    EvidenceIntakeInspection,
    FileSafetyScanner,
    inspect_authorized_original,
)
from .local_access_grants import AuthorizedOriginalFile


class WebUploadStagingBlocked(ValueError):
    """A browser upload cannot safely enter private server staging."""


class WebUploadRejected(WebUploadStagingBlocked):
    """A staged file did not pass the mandatory safety/format inspection."""


@dataclass(frozen=True)
class WebUploadLimits:
    """Byte-only boundary; client supplied ``Content-Length`` is never trusted."""

    max_file_bytes: int = 256 * 1024 * 1024
    read_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1 or self.max_file_bytes > 2 * 1024 * 1024 * 1024:
            raise ValueError("Web upload byte limit must be between 1 byte and 2 GiB")
        if self.read_chunk_bytes < 64 * 1024 or self.read_chunk_bytes > 4 * 1024 * 1024:
            raise ValueError("Web upload chunk size must be between 64 KiB and 4 MiB")


@dataclass(frozen=True)
class StagedWebUpload:
    """Private hand-off record.  Its path is never a browser response field."""

    upload_id: str
    display_name: str
    byte_size: int
    content_sha256: str
    path: Path = field(repr=False, compare=False)


@dataclass(frozen=True)
class AdmittedWebPdfUpload:
    """A static PDF that may be handed to private object storage/ledger code."""

    upload_id: str
    display_name: str
    byte_size: int
    content_sha256: str
    media_type: str
    page_count: int
    inspection_hash: str
    scanner_name: str
    scanner_definitions_version: str
    path: Path = field(repr=False, compare=False)


class WebUploadStagingArea:
    """A private, process-owned staging root for untrusted browser bytes.

    This is deliberately not a case folder and is not an object store.  Files
    are random-named, mode 0600, and must be explicitly discarded after an
    object-store handoff.  The class refuses relative roots and symlinks so a
    deployment cannot accidentally stage uploads in a repository or served
    directory.
    """

    def __init__(self, staging_root: str | Path, *, limits: WebUploadLimits = WebUploadLimits()) -> None:
        raw_root = Path(staging_root).expanduser()
        if not raw_root.is_absolute():
            raise WebUploadStagingBlocked("Web upload staging root must be an absolute path")
        if raw_root.exists() and raw_root.is_symlink():
            raise WebUploadStagingBlocked("Web upload staging root cannot be a symbolic link")
        try:
            raw_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            root = raw_root.resolve(strict=True)
            if not root.is_dir() or root.is_symlink():
                raise WebUploadStagingBlocked("Web upload staging root must be a directory")
            root.chmod(0o700)
        except OSError as error:
            raise WebUploadStagingBlocked("Web upload staging root is unavailable") from error
        self._root = root
        self._limits = limits

    @property
    def staging_root(self) -> Path:
        """Server-only root; callers must not serialize it to a browser."""

        return self._root

    def stage_stream(self, stream: BinaryIO, *, client_filename: str) -> StagedWebUpload:
        """Stream bytes to one private regular file and derive its hash/size."""

        if not hasattr(stream, "read"):
            raise WebUploadStagingBlocked("Web upload stream is invalid")
        display_name = _safe_display_name(client_filename)
        upload_id = str(uuid4())
        destination = self._root / f"upload-{upload_id}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        digest = sha256()
        byte_size = 0
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    block = stream.read(self._limits.read_chunk_bytes)
                    if not isinstance(block, (bytes, bytearray)):
                        raise WebUploadStagingBlocked("Web upload stream must produce bytes")
                    if not block:
                        break
                    byte_size += len(block)
                    if byte_size > self._limits.max_file_bytes:
                        raise WebUploadStagingBlocked("Web upload exceeds the configured file-size limit")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
            _assert_staged_file(destination, root=self._root, expected_bytes=byte_size, expected_sha256=digest.hexdigest())
        except (OSError, ValueError) as error:
            self._unlink_private(destination)
            if isinstance(error, WebUploadStagingBlocked):
                raise
            raise WebUploadStagingBlocked("Web upload could not be staged safely") from error
        return StagedWebUpload(
            upload_id=upload_id,
            display_name=display_name,
            byte_size=byte_size,
            content_sha256=digest.hexdigest(),
            path=destination,
        )

    async def stage_async_chunks(
        self,
        chunks: AsyncIterable[bytes],
        *,
        client_filename: str,
    ) -> StagedWebUpload:
        """Stream an ASGI request body without a multipart spool directory.

        A future FastAPI route should pass ``request.stream()`` here after it
        has authorized the current case.  Each browser file is one explicit
        request; the caller never needs to use a browser path or accept a
        temporary multipart upload file outside this private staging root.
        """

        if not hasattr(chunks, "__aiter__"):
            raise WebUploadStagingBlocked("Web upload async stream is invalid")
        display_name = _safe_display_name(client_filename)
        upload_id = str(uuid4())
        destination = self._root / f"upload-{upload_id}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        digest = sha256()
        byte_size = 0
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                async for block in chunks:
                    if not isinstance(block, (bytes, bytearray)):
                        raise WebUploadStagingBlocked("Web upload async stream must produce bytes")
                    if not block:
                        continue
                    byte_size += len(block)
                    if byte_size > self._limits.max_file_bytes:
                        raise WebUploadStagingBlocked("Web upload exceeds the configured file-size limit")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
            _assert_staged_file(destination, root=self._root, expected_bytes=byte_size, expected_sha256=digest.hexdigest())
        except (OSError, ValueError) as error:
            self._unlink_private(destination)
            if isinstance(error, WebUploadStagingBlocked):
                raise
            raise WebUploadStagingBlocked("Web upload could not be staged safely") from error
        return StagedWebUpload(
            upload_id=upload_id,
            display_name=display_name,
            byte_size=byte_size,
            content_sha256=digest.hexdigest(),
            path=destination,
        )

    def inspect_pdf(
        self,
        staged: StagedWebUpload,
        *,
        scanner: FileSafetyScanner,
        max_pdf_pages: int = 10_000,
    ) -> AdmittedWebPdfUpload:
        """Run mandatory scanner and structural PDF inspection without trust in metadata."""

        self._assert_current(staged)
        source = AuthorizedOriginalFile(
            relative_path=staged.display_name,
            path=staged.path,
            byte_size=staged.byte_size,
            sha256=staged.content_sha256,
        )
        try:
            inspection = inspect_authorized_original(
                source,
                detected_kind="PDF",
                scanner=scanner,
                max_pdf_pages=max_pdf_pages,
            )
        except EvidenceIntakeBlocked as error:
            raise WebUploadStagingBlocked("Web PDF inspection could not be completed safely") from error
        finally:
            # A scanner is an external process boundary.  Recheck even when it
            # reports a failure so an altered staging file is never retried.
            self._assert_current(staged)
        return _admitted_pdf(staged, inspection)

    def discard(self, staged: StagedWebUpload | AdmittedWebPdfUpload) -> None:
        """Erase a private staging file after a successful or failed hand-off."""

        self._unlink_private(staged.path)

    def _assert_current(self, staged: StagedWebUpload) -> None:
        if not isinstance(staged, StagedWebUpload):
            raise WebUploadStagingBlocked("Web upload staging handle is invalid")
        _assert_staged_file(
            staged.path,
            root=self._root,
            expected_bytes=staged.byte_size,
            expected_sha256=staged.content_sha256,
            expected_upload_id=staged.upload_id,
        )

    def _unlink_private(self, path: Path) -> None:
        try:
            if path.parent != self._root or not path.name.startswith("upload-") or not path.name.endswith(".part"):
                return
            path.unlink(missing_ok=True)
        except OSError as error:
            raise WebUploadStagingBlocked("Web upload staging file could not be removed") from error


def _admitted_pdf(staged: StagedWebUpload, inspection: EvidenceIntakeInspection) -> AdmittedWebPdfUpload:
    if inspection.outcome != "REGISTERABLE":
        reason = inspection.reason_code or "PDF_INSPECTION_INCOMPLETE"
        raise WebUploadRejected(f"Web PDF upload was rejected: {reason}")
    if inspection.media_type != "application/pdf" or inspection.page_count is None:
        raise WebUploadRejected("Web PDF upload inspection is incomplete")
    return AdmittedWebPdfUpload(
        upload_id=staged.upload_id,
        display_name=staged.display_name,
        byte_size=staged.byte_size,
        content_sha256=staged.content_sha256,
        media_type=inspection.media_type,
        page_count=inspection.page_count,
        inspection_hash=inspection.inspection_hash,
        scanner_name=inspection.scanner_name,
        scanner_definitions_version=inspection.scanner_definitions_version,
        path=staged.path,
    )


def _safe_display_name(value: str) -> str:
    if not isinstance(value, str):
        raise WebUploadStagingBlocked("Web upload file name is invalid")
    # Modern browsers do not send local paths, but this boundary makes such a
    # path non-observable even if a malformed client attempts to send one.
    candidate = PurePosixPath(value.replace("\\", "/")).name.strip()
    if candidate in {"", ".", ".."} or len(candidate.encode("utf-8")) > 255:
        raise WebUploadStagingBlocked("Web upload file name is invalid")
    if any(character == "\x00" or category(character).startswith("C") for character in candidate):
        raise WebUploadStagingBlocked("Web upload file name contains control characters")
    return candidate


def _assert_staged_file(
    path: Path,
    *,
    root: Path,
    expected_bytes: int,
    expected_sha256: str,
    expected_upload_id: str | None = None,
) -> None:
    if path.parent != root or path.is_symlink() or not path.is_file():
        raise WebUploadStagingBlocked("Web upload staging file is missing or unsafe")
    if expected_upload_id is not None and path.name != f"upload-{expected_upload_id}.part":
        raise WebUploadStagingBlocked("Web upload staging handle does not match its file")
    try:
        stat = path.stat()
    except OSError as error:
        raise WebUploadStagingBlocked("Web upload staging file is unavailable") from error
    if stat.st_size != expected_bytes:
        raise WebUploadStagingBlocked("Web upload staging file changed before inspection")
    digest = sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise WebUploadStagingBlocked("Web upload staging file is unavailable") from error
    if digest.hexdigest() != expected_sha256:
        raise WebUploadStagingBlocked("Web upload staging file changed before inspection")
