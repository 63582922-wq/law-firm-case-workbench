"""Stable references from structured case objects back to immutable source evidence."""

from __future__ import annotations

from dataclasses import dataclass


class EvidenceReferenceBlocked(ValueError):
    """A structured object lacks a usable pointer to its original evidence."""


@dataclass(frozen=True)
class EvidenceLink:
    evidence_id: str
    original_file_sha256: str
    page_number: int | None
    region_id: str | None
    original_label: str


def validate_evidence_links(links: tuple[EvidenceLink, ...]) -> None:
    """Require a page/file-level original reference; never accept a derivative as proof."""
    if not links:
        raise EvidenceReferenceBlocked("at least one original evidence link is required")
    for link in links:
        _require_text(link.evidence_id, "evidence id")
        _require_text(link.original_label, "evidence original label")
        normalized_hash = link.original_file_sha256.lower()
        if len(normalized_hash) != 64 or any(character not in "0123456789abcdef" for character in normalized_hash):
            raise EvidenceReferenceBlocked("evidence link requires the original file SHA-256")
        if link.page_number is not None and link.page_number < 1:
            raise EvidenceReferenceBlocked("evidence page numbers must be positive")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise EvidenceReferenceBlocked(f"{label} is required")
