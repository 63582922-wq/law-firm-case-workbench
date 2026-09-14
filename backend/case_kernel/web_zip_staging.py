"""Bounded, server-side admission of ZIP material archives.

The browser may upload one ZIP as an opaque byte stream, but the archive is
never trusted merely because its central directory claims safe sizes.  This
module re-reads every entry, validates the decompressed byte count and CRC,
and returns only an immutable inventory suitable for a later server worker.
It deliberately does not extract files into the case directory.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
from unicodedata import category
from uuid import uuid4
from zipfile import BadZipFile, LargeZipFile, ZipFile, ZipInfo


class WebZipStagingBlocked(ValueError):
    """An archive cannot safely enter the server-owned material pipeline."""


@dataclass(frozen=True)
class WebZipLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_entries: int = 1_000
    max_entry_bytes: int = 256 * 1024 * 1024
    max_expanded_bytes: int = 1 * 1024 * 1024 * 1024
    max_compression_ratio: int = 1_000
    read_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not 1 <= self.max_archive_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("ZIP archive byte limit is invalid")
        if not 1 <= self.max_entries <= 100_000:
            raise ValueError("ZIP entry limit is invalid")
        if not 1 <= self.max_entry_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("ZIP entry byte limit is invalid")
        if not 1 <= self.max_expanded_bytes <= 8 * 1024 * 1024 * 1024:
            raise ValueError("ZIP expanded byte limit is invalid")
        if not 1 <= self.max_compression_ratio <= 100_000:
            raise ValueError("ZIP compression ratio limit is invalid")
        if not 64 * 1024 <= self.read_chunk_bytes <= 4 * 1024 * 1024:
            raise ValueError("ZIP read chunk size is invalid")


@dataclass(frozen=True)
class StagedWebZip:
    upload_id: str
    display_name: str
    byte_size: int
    content_sha256: str
    path: Path = field(repr=False, compare=False)


@dataclass(frozen=True)
class WebZipEntry:
    """Safe metadata for one PDF child; no private path is exposed."""

    name: str
    byte_size: int
    content_sha256: str
    compressed_byte_size: int


@dataclass(frozen=True)
class AdmittedWebZip:
    upload_id: str
    display_name: str
    byte_size: int
    content_sha256: str
    entries: tuple[WebZipEntry, ...]
    expanded_byte_size: int
    path: Path = field(repr=False, compare=False)


class WebZipStagingArea:
    """Private 0700 root for ZIP bytes before object-store handoff."""

    def __init__(self, root: str | Path, *, limits: WebZipLimits = WebZipLimits()) -> None:
        raw = Path(root).expanduser()
        if not raw.is_absolute() or raw.exists() and raw.is_symlink():
            raise WebZipStagingBlocked("ZIP staging root is invalid")
        try:
            raw.mkdir(parents=True, mode=0o700, exist_ok=True)
            resolved = raw.resolve(strict=True)
            if not resolved.is_dir() or resolved.is_symlink():
                raise WebZipStagingBlocked("ZIP staging root is invalid")
            resolved.chmod(0o700)
        except OSError as error:
            raise WebZipStagingBlocked("ZIP staging root is unavailable") from error
        self._root = resolved
        self._limits = limits

    @property
    def staging_root(self) -> Path:
        return self._root

    async def stage_async_chunks(self, chunks: AsyncIterable[bytes], *, client_filename: str) -> StagedWebZip:
        if not hasattr(chunks, "__aiter__"):
            raise WebZipStagingBlocked("ZIP upload stream is invalid")
        display_name = _safe_archive_name(client_filename)
        upload_id = str(uuid4())
        destination = self._root / f"archive-{upload_id}.part"
        digest = sha256()
        size = 0
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                async for block in chunks:
                    if not isinstance(block, (bytes, bytearray)):
                        raise WebZipStagingBlocked("ZIP upload stream must produce bytes")
                    if not block:
                        continue
                    size += len(block)
                    if size > self._limits.max_archive_bytes:
                        raise WebZipStagingBlocked("ZIP archive exceeds the configured size limit")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
            _assert_file(destination, root=self._root, expected_bytes=size, expected_sha256=digest.hexdigest())
        except (OSError, ValueError) as error:
            self._discard_path(destination)
            if isinstance(error, WebZipStagingBlocked):
                raise
            raise WebZipStagingBlocked("ZIP archive could not be staged safely") from error
        return StagedWebZip(upload_id, display_name, size, digest.hexdigest(), destination)

    def inspect_zip(self, staged: StagedWebZip) -> AdmittedWebZip:
        if not isinstance(staged, StagedWebZip):
            raise WebZipStagingBlocked("ZIP staging handle is invalid")
        _assert_file(staged.path, root=self._root, expected_bytes=staged.byte_size, expected_sha256=staged.content_sha256)
        entries: list[WebZipEntry] = []
        seen: set[str] = set()
        expanded = 0
        try:
            with ZipFile(staged.path, "r", allowZip64=True) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > self._limits.max_entries:
                    raise WebZipStagingBlocked("ZIP archive entry count is outside the allowed range")
                for info in infos:
                    name = _safe_pdf_entry_name(info)
                    normalized = name.casefold()
                    if normalized in seen:
                        raise WebZipStagingBlocked("ZIP archive contains duplicate material names")
                    seen.add(normalized)
                    if info.file_size < 1 or info.file_size > self._limits.max_entry_bytes:
                        raise WebZipStagingBlocked("ZIP archive contains an entry outside the allowed size")
                    if info.compress_size < 1:
                        raise WebZipStagingBlocked("ZIP archive contains an invalid compressed entry")
                    if info.file_size > info.compress_size * self._limits.max_compression_ratio:
                        raise WebZipStagingBlocked("ZIP archive compression ratio is unsafe")
                    expanded += info.file_size
                    if expanded > self._limits.max_expanded_bytes:
                        raise WebZipStagingBlocked("ZIP archive expands beyond the configured limit")
                    digest = sha256()
                    actual = 0
                    with archive.open(info, "r") as source:
                        while block := source.read(self._limits.read_chunk_bytes):
                            actual += len(block)
                            if actual > self._limits.max_entry_bytes or expanded - info.file_size + actual > self._limits.max_expanded_bytes:
                                raise WebZipStagingBlocked("ZIP archive expands beyond the configured limit")
                            digest.update(block)
                    if actual != info.file_size:
                        raise WebZipStagingBlocked("ZIP archive entry size changed while reading")
                    entries.append(WebZipEntry(name, actual, digest.hexdigest(), info.compress_size))
        except WebZipStagingBlocked:
            raise
        except (BadZipFile, LargeZipFile, OSError, RuntimeError, ValueError) as error:
            raise WebZipStagingBlocked("ZIP archive is invalid or could not be inspected") from error
        if not entries:
            raise WebZipStagingBlocked("ZIP archive contains no PDF materials")
        return AdmittedWebZip(
            upload_id=staged.upload_id,
            display_name=staged.display_name,
            byte_size=staged.byte_size,
            content_sha256=staged.content_sha256,
            entries=tuple(entries),
            expanded_byte_size=expanded,
            path=staged.path,
        )

    def discard(self, staged: StagedWebZip | AdmittedWebZip) -> None:
        if not isinstance(staged, (StagedWebZip, AdmittedWebZip)):
            return
        self._discard_path(staged.path)

    def _discard_path(self, path: Path) -> None:
        try:
            if path.parent == self._root and path.name.startswith("archive-") and path.name.endswith(".part"):
                path.unlink(missing_ok=True)
        except OSError as error:
            raise WebZipStagingBlocked("ZIP staging file could not be removed") from error


def _safe_archive_name(value: object) -> str:
    if not isinstance(value, str):
        raise WebZipStagingBlocked("ZIP file name is invalid")
    candidate = PurePosixPath(value.replace("\\", "/")).name.strip()
    if candidate in {"", ".", ".."} or len(candidate.encode("utf-8")) > 255 or not candidate.casefold().endswith(".zip"):
        raise WebZipStagingBlocked("ZIP file name is invalid")
    if any(category(char).startswith("C") for char in candidate):
        raise WebZipStagingBlocked("ZIP file name contains control characters")
    return candidate


def _safe_pdf_entry_name(info: ZipInfo) -> str:
    raw = info.filename
    if not isinstance(raw, str) or not raw or raw.endswith("/"):
        raise WebZipStagingBlocked("ZIP archive contains a directory entry")
    if info.flag_bits & 0x1:
        raise WebZipStagingBlocked("encrypted ZIP archives are not accepted")
    # Unix symlinks encode their type in the high mode bits.  DOS directory
    # flags are also rejected above, before any extraction is attempted.
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise WebZipStagingBlocked("ZIP archive contains a symbolic link")
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in path.parts):
        raise WebZipStagingBlocked("ZIP archive contains an unsafe path")
    if len(normalized.encode("utf-8")) > 255 or any(category(char).startswith("C") for char in normalized):
        raise WebZipStagingBlocked("ZIP archive entry name is invalid")
    if not normalized.casefold().endswith(".pdf"):
        raise WebZipStagingBlocked("ZIP archive may contain PDF materials only")
    return normalized


def _assert_file(path: Path, *, root: Path, expected_bytes: int, expected_sha256: str) -> None:
    if path.parent != root or path.is_symlink() or not path.is_file():
        raise WebZipStagingBlocked("ZIP staging file is missing or unsafe")
    try:
        if path.stat().st_size != expected_bytes:
            raise WebZipStagingBlocked("ZIP staging file changed before inspection")
        digest = sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise WebZipStagingBlocked("ZIP staging file is unavailable") from error
    if digest.hexdigest() != expected_sha256:
        raise WebZipStagingBlocked("ZIP staging file changed before inspection")
