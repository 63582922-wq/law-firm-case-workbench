"""Strictly decode an untrusted model response into a bounded draft candidate.

Model text is never treated as an instruction or a file path.  This module
accepts one exact JSON schema, rejects Markdown wrappers and unknown fields,
and creates a canonical content hash for a later lawyer-review record.  It
does not grant execution authority or create Office files.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any

from .agent_reviewable_draft_coordinator import (
    approved_docx_draft_input_hash,
    approved_xlsx_ledger_input_hash,
)
from .approved_draft_worker import ApprovedDraft, ApprovedSection


class AgentStructuredDraftBlocked(ValueError):
    """The model response is not a bounded, reviewable draft candidate."""


@dataclass(frozen=True)
class StructuredDocxDraftCandidate:
    draft: ApprovedDraft
    input_hash: str


@dataclass(frozen=True)
class StructuredXlsxDraftCandidate:
    approval_hash: str
    sheet_name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str | int | float | None, ...], ...]
    input_hash: str


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_SECTIONS = 200
_MAX_PARAGRAPHS = 20_000
_MAX_ROWS = 100_000
_MAX_COLUMNS = 200
_MAX_TEXT_CHARS = 1_000_000
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def parse_docx_candidate(raw: str | bytes) -> StructuredDocxDraftCandidate:
    value = _json_object(raw, {"schema_version", "title", "sections"}, "agent-docx-draft-v1")
    title = _text(value, "title", maximum=240)
    sections_value = value.get("sections")
    if not isinstance(sections_value, list) or not 1 <= len(sections_value) <= _MAX_SECTIONS:
        raise AgentStructuredDraftBlocked("draft sections are invalid")
    sections: list[ApprovedSection] = []
    paragraph_count = 0
    character_count = len(title)
    for section_value in sections_value:
        if not isinstance(section_value, dict) or set(section_value) != {"heading", "paragraphs", "source_refs"}:
            raise AgentStructuredDraftBlocked("draft section schema is invalid")
        heading = _text(section_value, "heading", maximum=240)
        paragraphs = _text_list(section_value.get("paragraphs"), maximum_item=20_000)
        source_refs = _references(section_value.get("source_refs"))
        paragraph_count += len(paragraphs)
        character_count += len(heading) + sum(len(item) for item in paragraphs) + sum(len(item) for item in source_refs)
        if paragraph_count > _MAX_PARAGRAPHS or character_count > _MAX_TEXT_CHARS:
            raise AgentStructuredDraftBlocked("draft text exceeds the review boundary")
        sections.append(ApprovedSection(heading, paragraphs, source_refs))
    approval_hash = _content_hash({"schema_version": "agent-docx-draft-content-v1", "title": title, "sections": _docx_sections_payload(sections)})
    draft = ApprovedDraft(title=title, sections=tuple(sections), approval_hash=approval_hash)
    return StructuredDocxDraftCandidate(draft=draft, input_hash=approved_docx_draft_input_hash(draft))


def parse_xlsx_candidate(raw: str | bytes) -> StructuredXlsxDraftCandidate:
    value = _json_object(raw, {"schema_version", "sheet_name", "columns", "rows"}, "agent-xlsx-ledger-v1")
    sheet_name = _text(value, "sheet_name", maximum=31)
    if any(character in sheet_name for character in "[]:*?/\\"):
        raise AgentStructuredDraftBlocked("ledger sheet name is invalid")
    columns = _text_list(value.get("columns"), maximum_item=160)
    if not 1 <= len(columns) <= _MAX_COLUMNS:
        raise AgentStructuredDraftBlocked("ledger columns are invalid")
    rows_value = value.get("rows")
    if not isinstance(rows_value, list) or not 1 <= len(rows_value) <= _MAX_ROWS:
        raise AgentStructuredDraftBlocked("ledger rows are invalid")
    rows: list[tuple[str | int | float | None, ...]] = []
    character_count = len(sheet_name) + sum(len(item) for item in columns)
    for row in rows_value:
        if not isinstance(row, list) or len(row) != len(columns):
            raise AgentStructuredDraftBlocked("ledger row width is invalid")
        values: list[str | int | float | None] = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (str, int, float, type(None))):
                raise AgentStructuredDraftBlocked("ledger value type is invalid")
            if isinstance(item, float) and (item != item or item in {float("inf"), float("-inf")}):
                raise AgentStructuredDraftBlocked("ledger numeric value is invalid")
            if isinstance(item, str):
                if len(item) > 20_000:
                    raise AgentStructuredDraftBlocked("ledger text value is invalid")
                character_count += len(item)
            values.append(item)
        if character_count > _MAX_TEXT_CHARS:
            raise AgentStructuredDraftBlocked("ledger text exceeds the review boundary")
        rows.append(tuple(values))
    approval_hash = _content_hash({"schema_version": "agent-xlsx-ledger-content-v1", "sheet_name": sheet_name, "columns": list(columns), "rows": [list(row) for row in rows]})
    input_hash = approved_xlsx_ledger_input_hash(approval_hash=approval_hash, sheet_name=sheet_name, columns=columns, rows=tuple(rows))
    return StructuredXlsxDraftCandidate(approval_hash, sheet_name, columns, tuple(rows), input_hash)


def _json_object(raw: str | bytes, keys: set[str], schema_version: str) -> dict[str, Any]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or not 2 <= len(encoded) <= _MAX_RESPONSE_BYTES:
        raise AgentStructuredDraftBlocked("model draft response size is invalid")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentStructuredDraftBlocked("model draft response must be one JSON object") from error
    if not isinstance(value, dict) or set(value) != keys or value.get("schema_version") != schema_version:
        raise AgentStructuredDraftBlocked("model draft response schema is invalid")
    return value


def _text(value: dict[str, Any], key: str, *, maximum: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip() or len(item) > maximum:
        raise AgentStructuredDraftBlocked(f"draft {key} is invalid")
    return item.strip()


def _text_list(value: object, *, maximum_item: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AgentStructuredDraftBlocked("draft text list is invalid")
    values = tuple(item.strip() for item in value if isinstance(item, str) and item.strip() and len(item) <= maximum_item)
    if len(values) != len(value):
        raise AgentStructuredDraftBlocked("draft text list contains an invalid item")
    return values


def _references(value: object) -> tuple[str, ...]:
    references = _text_list(value, maximum_item=160)
    if any(_REFERENCE_RE.fullmatch(item) is None for item in references):
        raise AgentStructuredDraftBlocked("draft source reference is invalid")
    return references


def _docx_sections_payload(sections: list[ApprovedSection]) -> list[dict[str, object]]:
    return [{"heading": item.heading, "paragraphs": list(item.paragraphs), "source_refs": list(item.source_refs)} for item in sections]


def _content_hash(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
