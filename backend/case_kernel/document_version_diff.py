"""Deterministic, hash-only diff for approved structured document versions."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal

from .approved_draft_worker import (
    ApprovedDraft,
    ApprovedDraftBlocked,
    validate_approved_draft,
)


class DocumentVersionDiffBlocked(ValueError):
    """Version inputs are not bounded approved document snapshots."""


@dataclass(frozen=True)
class DocumentVersionChange:
    change_kind: Literal[
        "DOCUMENT_TITLE_CHANGED",
        "SECTION_ADDED",
        "SECTION_REMOVED",
        "SECTION_CHANGED",
    ]
    previous_section_index: int | None
    current_section_index: int | None
    previous_content_hash: str | None
    current_content_hash: str | None
    previous_source_refs_hash: str | None
    current_source_refs_hash: str | None


@dataclass(frozen=True)
class DocumentVersionDiff:
    previous_semantic_hash: str
    current_semantic_hash: str
    changed: bool
    requires_reapproval: bool
    changes: tuple[DocumentVersionChange, ...]
    diff_hash: str


def diff_approved_document_versions(
    *, previous: ApprovedDraft, current: ApprovedDraft
) -> DocumentVersionDiff:
    """Compare exact approved snapshots without retaining any paragraph body.

    Any text, heading, section ordering or source-reference change requires a
    new approval.  The returned projection contains only stable positions and
    SHA-256 values, so it is suitable for a future append-only audit record.
    """

    for draft in (previous, current):
        if not isinstance(draft, ApprovedDraft):
            raise DocumentVersionDiffBlocked("document version must be an approved draft")
        try:
            validate_approved_draft(draft)
        except ApprovedDraftBlocked as error:
            raise DocumentVersionDiffBlocked("document version is not a valid approved draft") from error
    previous_hash = approved_draft_semantic_hash(previous)
    current_hash = approved_draft_semantic_hash(current)
    changes: list[DocumentVersionChange] = []
    if previous.title != current.title:
        changes.append(DocumentVersionChange(
            "DOCUMENT_TITLE_CHANGED", None, None, _title_hash(previous.title), _title_hash(current.title),
            None, None,
        ))
    common = min(len(previous.sections), len(current.sections))
    for index in range(common):
        before, after = previous.sections[index], current.sections[index]
        before_content, after_content = _section_content_hash(before), _section_content_hash(after)
        before_refs, after_refs = _source_refs_hash(before.source_refs), _source_refs_hash(after.source_refs)
        if before_content != after_content or before_refs != after_refs:
            changes.append(DocumentVersionChange(
                "SECTION_CHANGED", index + 1, index + 1, before_content, after_content,
                before_refs, after_refs,
            ))
    for index in range(common, len(previous.sections)):
        section = previous.sections[index]
        changes.append(DocumentVersionChange(
            "SECTION_REMOVED", index + 1, None, _section_content_hash(section), None,
            _source_refs_hash(section.source_refs), None,
        ))
    for index in range(common, len(current.sections)):
        section = current.sections[index]
        changes.append(DocumentVersionChange(
            "SECTION_ADDED", None, index + 1, None, _section_content_hash(section),
            None, _source_refs_hash(section.source_refs),
        ))
    ordered = tuple(changes)
    changed = previous_hash != current_hash
    payload = {
        "previous_semantic_hash": previous_hash,
        "current_semantic_hash": current_hash,
        "changes": [item.__dict__ for item in ordered],
    }
    return DocumentVersionDiff(
        previous_semantic_hash=previous_hash,
        current_semantic_hash=current_hash,
        changed=changed,
        requires_reapproval=changed,
        changes=ordered,
        diff_hash=sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def approved_draft_semantic_hash(draft: ApprovedDraft) -> str:
    """Hash title, ordered content and cited sources, excluding approval metadata."""

    payload = {
        "title": draft.title,
        "sections": [
            {"heading": item.heading, "paragraphs": item.paragraphs, "source_refs": item.source_refs}
            for item in draft.sections
        ],
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _section_content_hash(section) -> str:
    return sha256(
        json.dumps(
            {"heading": section.heading, "paragraphs": section.paragraphs},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _title_hash(title: str) -> str:
    return sha256(title.encode("utf-8")).hexdigest()


def _source_refs_hash(source_refs: tuple[str, ...]) -> str:
    return sha256(
        json.dumps(source_refs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
