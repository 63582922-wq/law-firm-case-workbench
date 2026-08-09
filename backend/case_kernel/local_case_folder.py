"""Read-only local case-folder manifesting.

This first implementation intentionally returns metadata and content hashes only.
It never copies, modifies, uploads, renames, opens with a third-party service, or
follows a symbolic link out of the lawyer-selected root directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator
from uuid import uuid4


class FolderScanBlocked(ValueError):
    """The requested read-only scan exceeds a local safety guardrail."""


@dataclass(frozen=True)
class FolderScanLimits:
    max_files: int = 10_000
    max_total_bytes: int = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class OriginalFileRecord:
    relative_path: str
    byte_size: int
    sha256: str
    detected_kind: str


@dataclass(frozen=True)
class FolderManifest:
    scan_id: str
    root_fingerprint: str
    scanned_at: datetime
    total_files: int
    total_bytes: int
    skipped_symlinks: int
    originals: tuple[OriginalFileRecord, ...]


KNOWN_FILE_KINDS = {
    ".pdf": "PDF",
    ".png": "IMAGE",
    ".jpg": "IMAGE",
    ".jpeg": "IMAGE",
    ".heic": "IMAGE",
    ".docx": "WORD_DOCUMENT",
    ".xlsx": "SPREADSHEET",
    ".xls": "SPREADSHEET",
    ".txt": "TEXT",
    ".eml": "EMAIL",
    ".zip": "ARCHIVE",
}


def root_fingerprint(selected_root: str | Path) -> str:
    """Create a local confirmation token without putting an absolute path in audit data."""
    resolved = Path(selected_root).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise FolderScanBlocked("the selected path must be an existing directory")
    return sha256(str(resolved).encode("utf-8")).hexdigest()


def scan_case_folder(
    selected_root: str | Path,
    *,
    confirmed_root_fingerprint: str,
    limits: FolderScanLimits = FolderScanLimits(),
) -> FolderManifest:
    """Build an immutable-looking inventory after the UI has shown scope and obtained confirmation."""
    root = Path(selected_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise FolderScanBlocked("the selected path must be an existing directory")
    actual_fingerprint = root_fingerprint(root)
    if confirmed_root_fingerprint != actual_fingerprint:
        raise FolderScanBlocked("folder confirmation no longer matches the selected root")
    if limits.max_files < 1 or limits.max_total_bytes < 1:
        raise ValueError("scan limits must be positive")

    originals: list[OriginalFileRecord] = []
    total_bytes = 0
    skipped_symlinks = 0
    for candidate in _iter_regular_files_without_symlinks(root):
        if candidate.is_symlink():
            skipped_symlinks += 1
            continue
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            skipped_symlinks += 1
            continue
        if len(originals) >= limits.max_files:
            raise FolderScanBlocked(f"file-count limit reached ({limits.max_files})")
        size = resolved.stat().st_size
        if total_bytes + size > limits.max_total_bytes:
            raise FolderScanBlocked(f"total-byte limit reached ({limits.max_total_bytes})")
        originals.append(
            OriginalFileRecord(
                relative_path=resolved.relative_to(root).as_posix(),
                byte_size=size,
                sha256=_hash_file(resolved),
                detected_kind=KNOWN_FILE_KINDS.get(resolved.suffix.lower(), "OTHER"),
            )
        )
        total_bytes += size

    return FolderManifest(
        scan_id=f"scan_{uuid4().hex}",
        root_fingerprint=actual_fingerprint,
        scanned_at=datetime.now(timezone.utc),
        total_files=len(originals),
        total_bytes=total_bytes,
        skipped_symlinks=skipped_symlinks,
        originals=tuple(sorted(originals, key=lambda item: item.relative_path)),
    )


def _iter_regular_files_without_symlinks(root: Path) -> Iterator[Path]:
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            yield candidate
            continue
        if candidate.is_file():
            yield candidate


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
