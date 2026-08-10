"""Bind a structured Agent draft candidate to an exact lawyer-review decision.

This domain boundary is deliberately model- and database-neutral.  It makes
the approval object that persistent storage and the desktop worker must agree
on; a model response alone cannot construct an executable proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .agent_structured_draft import StructuredDocxDraftCandidate, StructuredXlsxDraftCandidate
from .models import Actor, Role


class AgentDraftReviewBlocked(PermissionError):
    """A candidate or lawyer decision does not meet the execution boundary."""


@dataclass(frozen=True)
class AgentDraftReviewCandidate:
    document_kind: str
    input_hash: str
    content_hash: str
    review_hash: str
    skill_id: str
    tool_id: str


@dataclass(frozen=True)
class ApprovedAgentDraftCandidate:
    candidate: AgentDraftReviewCandidate
    approval_hash: str
    approved_by: str


def serialize_docx_candidate(candidate: StructuredDocxDraftCandidate) -> bytes:
    """Canonical encrypted-payload representation; it contains no approval decision."""

    payload = {
        "schema_version": "agent-docx-draft-v1",
        "title": candidate.draft.title,
        "sections": [
            {"heading": section.heading, "paragraphs": list(section.paragraphs), "source_refs": list(section.source_refs)}
            for section in candidate.draft.sections
        ],
    }
    return _canonical_bytes(payload)


def serialize_xlsx_candidate(candidate: StructuredXlsxDraftCandidate) -> bytes:
    """Canonical encrypted-payload representation; it contains no approval decision."""

    return _canonical_bytes({
        "schema_version": "agent-xlsx-ledger-v1",
        "sheet_name": candidate.sheet_name,
        "columns": list(candidate.columns),
        "rows": [list(row) for row in candidate.rows],
    })


def prepare_docx_review_candidate(candidate: StructuredDocxDraftCandidate) -> AgentDraftReviewCandidate:
    content_hash = _hash({"schema_version": "agent-docx-review-content-v1", "title": candidate.draft.title, "approval_hash": candidate.draft.approval_hash, "sections": [{"heading": section.heading, "paragraphs": list(section.paragraphs), "source_refs": list(section.source_refs)} for section in candidate.draft.sections]})
    return _prepare("DOCX", candidate.input_hash, content_hash, "document_drafting", "create_reviewable_docx_draft")


def prepare_xlsx_review_candidate(candidate: StructuredXlsxDraftCandidate) -> AgentDraftReviewCandidate:
    content_hash = _hash({"schema_version": "agent-xlsx-review-content-v1", "approval_hash": candidate.approval_hash, "sheet_name": candidate.sheet_name, "columns": list(candidate.columns), "rows": [list(row) for row in candidate.rows]})
    return _prepare("XLSX", candidate.input_hash, content_hash, "spreadsheet_ledger", "create_reviewable_xlsx_ledger")


def approve_agent_draft_candidate(
    *, candidate: AgentDraftReviewCandidate, lawyer: Actor, supplied_review_hash: str
) -> ApprovedAgentDraftCandidate:
    if not isinstance(candidate, AgentDraftReviewCandidate):
        raise AgentDraftReviewBlocked("Agent draft review candidate is invalid")
    if not lawyer.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
        raise AgentDraftReviewBlocked("only a lead lawyer or reviewer can approve an Agent draft candidate")
    if supplied_review_hash != candidate.review_hash:
        raise AgentDraftReviewBlocked("lawyer approval must bind to the exact Agent draft review hash")
    return ApprovedAgentDraftCandidate(candidate, supplied_review_hash, lawyer.actor_id)


def _prepare(document_kind: str, input_hash: str, content_hash: str, skill_id: str, tool_id: str) -> AgentDraftReviewCandidate:
    if not _sha256(input_hash) or not _sha256(content_hash):
        raise AgentDraftReviewBlocked("Agent candidate hash is invalid")
    review_hash = _hash({"schema_version": "agent-draft-review-v1", "document_kind": document_kind, "input_hash": input_hash, "content_hash": content_hash, "skill_id": skill_id, "tool_id": tool_id})
    return AgentDraftReviewCandidate(document_kind, input_hash, content_hash, review_hash, skill_id, tool_id)


def _hash(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
