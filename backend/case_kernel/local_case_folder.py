"""Read-only local case-folder manifesting.

This first implementation intentionally returns metadata and content hashes only.
It never copies, modifies, uploads, renames, opens with a third-party service, or
follows a symbolic link out of the lawyer-selected root directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
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
    manifest_hash: str
    scanned_at: datetime
    total_files: int
    total_bytes: int
    skipped_symlinks: int
    originals: tuple[OriginalFileRecord, ...]


@dataclass(frozen=True)
class FolderFileChange:
    relative_path: str
    previous_relative_path: str | None
    byte_size: int
    sha256: str
    detected_kind: str
    change_kind: str
    present: bool


@dataclass(frozen=True)
class FolderManifestComparison:
    base_scan_id: str | None
    new_count: int
    modified_count: int
    moved_count: int
    missing_count: int
    unchanged_count: int
    duplicate_content_count: int
    files: tuple[FolderFileChange, ...]


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

    ordered_originals = tuple(sorted(originals, key=lambda item: item.relative_path))
    return FolderManifest(
        scan_id=str(uuid4()),
        root_fingerprint=actual_fingerprint,
        manifest_hash=folder_manifest_hash(actual_fingerprint, ordered_originals),
        scanned_at=datetime.now(timezone.utc),
        total_files=len(originals),
        total_bytes=total_bytes,
        skipped_symlinks=skipped_symlinks,
        originals=ordered_originals,
    )


def compare_folder_manifests(
    current: FolderManifest,
    previous: FolderManifest | None,
) -> FolderManifestComparison:
    """Classify a new immutable inventory without treating moved content as deletion."""

    previous_by_path = {item.relative_path: item for item in previous.originals} if previous else {}
    unused_previous_paths = set(previous_by_path)
    previous_by_content: dict[tuple[str, int], list[OriginalFileRecord]] = {}
    for item in previous.originals if previous else ():
        previous_by_content.setdefault((item.sha256, item.byte_size), []).append(item)

    changes: list[FolderFileChange] = []
    for item in current.originals:
        prior = previous_by_path.get(item.relative_path)
        previous_relative_path: str | None = None
        if prior is not None:
            unused_previous_paths.discard(prior.relative_path)
            change_kind = (
                "UNCHANGED"
                if (prior.sha256, prior.byte_size, prior.detected_kind)
                == (item.sha256, item.byte_size, item.detected_kind)
                else "MODIFIED"
            )
        else:
            content_matches = [
                candidate
                for candidate in previous_by_content.get((item.sha256, item.byte_size), ())
                if candidate.relative_path in unused_previous_paths
            ]
            if len(content_matches) == 1:
                moved = content_matches[0]
                unused_previous_paths.discard(moved.relative_path)
                previous_relative_path = moved.relative_path
                change_kind = "MOVED"
            else:
                change_kind = "NEW"
        changes.append(
            FolderFileChange(
                relative_path=item.relative_path,
                previous_relative_path=previous_relative_path,
                byte_size=item.byte_size,
                sha256=item.sha256,
                detected_kind=item.detected_kind,
                change_kind=change_kind,
                present=True,
            )
        )

    for relative_path in sorted(unused_previous_paths):
        item = previous_by_path[relative_path]
        changes.append(
            FolderFileChange(
                relative_path=item.relative_path,
                previous_relative_path=item.relative_path,
                byte_size=item.byte_size,
                sha256=item.sha256,
                detected_kind=item.detected_kind,
                change_kind="MISSING",
                present=False,
            )
        )

    change_order = {"NEW": 0, "MODIFIED": 1, "MOVED": 2, "MISSING": 3, "UNCHANGED": 4}
    ordered = tuple(sorted(changes, key=lambda item: (change_order[item.change_kind], item.relative_path)))
    content_counts: dict[tuple[str, int], int] = {}
    for item in current.originals:
        key = (item.sha256, item.byte_size)
        content_counts[key] = content_counts.get(key, 0) + 1
    return FolderManifestComparison(
        base_scan_id=previous.scan_id if previous else None,
        new_count=sum(item.change_kind == "NEW" for item in ordered),
        modified_count=sum(item.change_kind == "MODIFIED" for item in ordered),
        moved_count=sum(item.change_kind == "MOVED" for item in ordered),
        missing_count=sum(item.change_kind == "MISSING" for item in ordered),
        unchanged_count=sum(item.change_kind == "UNCHANGED" for item in ordered),
        duplicate_content_count=sum(count - 1 for count in content_counts.values() if count > 1),
        files=ordered,
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


def folder_manifest_hash(
    root_fingerprint_value: str,
    originals: tuple[OriginalFileRecord, ...],
) -> str:
    payload = {
        "root_fingerprint": root_fingerprint_value,
        "originals": [
            {
                "relative_path": item.relative_path,
                "byte_size": item.byte_size,
                "sha256": item.sha256,
                "detected_kind": item.detected_kind,
            }
            for item in originals
        ],
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
