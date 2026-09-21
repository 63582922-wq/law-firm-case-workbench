"""Execute one approved Agent Office-draft proposal inside the local worker.

This is the missing bridge between the append-only Agent plan and the existing
Office review-pair pipeline.  It accepts no HTTP request, browser path or model
prompt.  The caller supplies an already-approved structured snapshot held in
the worker process; the coordinator independently hashes it against the
immutable Agent proposal before allowing the registered Tool to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Protocol

from .agent_execution_postgres import ExecutableAgentToolProposal
from .approved_draft_worker import ApprovedDraft
from .case_ledger_postgres import CaseLedgerCommandReceipt
from .models import Actor, Role
from .reviewable_draft_coordinator import (
    CoordinatedReviewableOfficeDraft,
    ReviewableDraftPersistencePort,
    coordinate_reviewable_office_draft,
)
from .skill_registry import CapabilityScope
from .skill_tool_gateway import CaseSkillToolGateway, SkillToolGatewayBlocked


class AgentReviewableDraftExecutionBlocked(PermissionError):
    """A planned Agent action cannot become a reviewable Office draft."""


class AgentExecutionProposalReader(Protocol):
    def get_executable_proposal(self, **kwargs) -> ExecutableAgentToolProposal: ...


class AgentExecutionReceiptWriter(Protocol):
    def record_tool_execution_receipt(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class AgentReviewableDraftExecution:
    proposal_id: str
    coordinated_draft: CoordinatedReviewableOfficeDraft
    execution_receipt: CaseLedgerCommandReceipt


def approved_docx_draft_input_hash(draft: ApprovedDraft) -> str:
    """Hash the exact approved structured snapshot without retaining prose in SQL."""

    payload = {
        "schema_version": "agent-reviewable-docx-input-v1",
        "title": draft.title,
        "approval_hash": draft.approval_hash,
        "sections": [
            {
                "heading": section.heading,
                "paragraphs": list(section.paragraphs),
                "source_refs": list(section.source_refs),
            }
            for section in draft.sections
        ],
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def approved_xlsx_ledger_input_hash(
    *,
    approval_hash: str,
    sheet_name: str,
    columns: tuple[str, ...],
    rows: tuple[tuple[str | int | float | None, ...], ...],
) -> str:
    """Hash the exact approved ledger input before a Worker formats it as XLSX."""

    payload = {
        "schema_version": "agent-reviewable-xlsx-input-v1",
        "approval_hash": approval_hash,
        "sheet_name": sheet_name,
        "columns": list(columns),
        "rows": [list(row) for row in rows],
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def execute_agent_reviewable_docx_draft(
    *,
    matter_id: str,
    expected_version: int,
    proposal_id: str,
    worker: Actor,
    draft: ApprovedDraft,
    case_root: str | Path,
    gateway: CaseSkillToolGateway,
    proposal_reader: AgentExecutionProposalReader,
    draft_persistence: ReviewableDraftPersistencePort,
    artifact_store,
    receipt_writer: AgentExecutionReceiptWriter,
) -> AgentReviewableDraftExecution:
    """Generate, persist, and receipt one Word review pair from a planned action.

    The planned input hash must equal the complete approved snapshot.  The
    worker cannot substitute a different Skill, Tool, draft body, or lawyer
    approval after planning.  Both persistence operations are idempotent per
    proposal ID, while a completed proposal rejects another successful run.
    """

    _require_system_worker(worker)
    if expected_version < 1 or not matter_id.strip() or not proposal_id.strip():
        raise AgentReviewableDraftExecutionBlocked("Agent reviewable draft execution identity is invalid")
    proposal = proposal_reader.get_executable_proposal(
        matter_id=matter_id,
        actor=worker,
        proposal_id=proposal_id,
    )
    _require_docx_proposal(proposal, draft)
    try:
        generated, output_hash = gateway.execute(
            skill_id=proposal.skill_id,
            tool_id=proposal.tool_id,
            payload={"draft": draft},
            granted_scopes=frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            lawyer_approved=True,
            release_locked=False,
        )
    except SkillToolGatewayBlocked as error:
        raise AgentReviewableDraftExecutionBlocked("registered Word drafting Tool was unavailable") from error
    coordinated = coordinate_reviewable_office_draft(
        matter_id=matter_id,
        expected_version=expected_version,
        idempotency_key=_idempotency_key("agent-docx-pair", proposal.proposal_id),
        document_kind="AGENT_REVIEWABLE_DOCX",
        draft=generated,
        case_root=case_root,
        artifact_store=artifact_store,
        persistence=draft_persistence,
        system_actor=worker,
    )
    if generated.review_input_hash != coordinated.review_input_hash:
        raise AgentReviewableDraftExecutionBlocked("Agent Tool output does not match persisted review pair")
    receipt = receipt_writer.record_tool_execution_receipt(
        matter_id=matter_id,
        actor=worker,
        expected_version=coordinated.registration_receipt.matter_version,
        idempotency_key=_idempotency_key("agent-docx-receipt", proposal.proposal_id),
        proposal_id=proposal.proposal_id,
        status="SUCCEEDED",
        output_hash=output_hash,
    )
    return AgentReviewableDraftExecution(proposal.proposal_id, coordinated, receipt)


def execute_agent_reviewable_xlsx_ledger(
    *,
    matter_id: str,
    expected_version: int,
    proposal_id: str,
    worker: Actor,
    approval_hash: str,
    sheet_name: str,
    columns: tuple[str, ...],
    rows: tuple[tuple[str | int | float | None, ...], ...],
    case_root: str | Path,
    gateway: CaseSkillToolGateway,
    proposal_reader: AgentExecutionProposalReader,
    draft_persistence: ReviewableDraftPersistencePort,
    artifact_store,
    receipt_writer: AgentExecutionReceiptWriter,
) -> AgentReviewableDraftExecution:
    """Generate, persist, and receipt one planned Excel review pair."""

    _require_system_worker(worker)
    if expected_version < 1 or not matter_id.strip() or not proposal_id.strip():
        raise AgentReviewableDraftExecutionBlocked("Agent reviewable ledger execution identity is invalid")
    proposal = proposal_reader.get_executable_proposal(
        matter_id=matter_id,
        actor=worker,
        proposal_id=proposal_id,
    )
    _require_xlsx_proposal(
        proposal,
        approval_hash=approval_hash,
        sheet_name=sheet_name,
        columns=columns,
        rows=rows,
    )
    try:
        generated, output_hash = gateway.execute(
            skill_id=proposal.skill_id,
            tool_id=proposal.tool_id,
            payload={
                "approval_hash": approval_hash,
                "sheet_name": sheet_name,
                "columns": columns,
                "rows": rows,
            },
            granted_scopes=frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            lawyer_approved=True,
            release_locked=False,
        )
    except SkillToolGatewayBlocked as error:
        raise AgentReviewableDraftExecutionBlocked("registered Excel drafting Tool was unavailable") from error
    coordinated = coordinate_reviewable_office_draft(
        matter_id=matter_id,
        expected_version=expected_version,
        idempotency_key=_idempotency_key("agent-xlsx-pair", proposal.proposal_id),
        document_kind="AGENT_REVIEWABLE_XLSX",
        draft=generated,
        case_root=case_root,
        artifact_store=artifact_store,
        persistence=draft_persistence,
        system_actor=worker,
    )
    if generated.review_input_hash != coordinated.review_input_hash:
        raise AgentReviewableDraftExecutionBlocked("Agent Tool output does not match persisted ledger review pair")
    receipt = receipt_writer.record_tool_execution_receipt(
        matter_id=matter_id,
        actor=worker,
        expected_version=coordinated.registration_receipt.matter_version,
        idempotency_key=_idempotency_key("agent-xlsx-receipt", proposal.proposal_id),
        proposal_id=proposal.proposal_id,
        status="SUCCEEDED",
        output_hash=output_hash,
    )
    return AgentReviewableDraftExecution(proposal.proposal_id, coordinated, receipt)


def _require_system_worker(worker: Actor) -> None:
    if worker.roles != frozenset({Role.SYSTEM_WORKER}):
        raise AgentReviewableDraftExecutionBlocked("Agent Office draft execution requires a dedicated SYSTEM_WORKER")


def _require_docx_proposal(proposal: ExecutableAgentToolProposal, draft: ApprovedDraft) -> None:
    if proposal.skill_id != "document_drafting" or proposal.tool_id != "create_reviewable_docx_draft":
        raise AgentReviewableDraftExecutionBlocked("Agent proposal is not a reviewable Word drafting action")
    if proposal.approval_gate != "LAWYER_REVIEW":
        raise AgentReviewableDraftExecutionBlocked("Agent Word drafting proposal has an invalid approval gate")
    if proposal.input_hash != approved_docx_draft_input_hash(draft):
        raise AgentReviewableDraftExecutionBlocked("approved Word draft snapshot does not match the planned Agent input")


def _require_xlsx_proposal(
    proposal: ExecutableAgentToolProposal,
    *,
    approval_hash: str,
    sheet_name: str,
    columns: tuple[str, ...],
    rows: tuple[tuple[str | int | float | None, ...], ...],
) -> None:
    if proposal.skill_id != "spreadsheet_ledger" or proposal.tool_id != "create_reviewable_xlsx_ledger":
        raise AgentReviewableDraftExecutionBlocked("Agent proposal is not a reviewable Excel ledger action")
    if proposal.approval_gate != "LAWYER_REVIEW":
        raise AgentReviewableDraftExecutionBlocked("Agent Excel proposal has an invalid approval gate")
    if proposal.input_hash != approved_xlsx_ledger_input_hash(
        approval_hash=approval_hash,
        sheet_name=sheet_name,
        columns=columns,
        rows=rows,
    ):
        raise AgentReviewableDraftExecutionBlocked("approved Excel ledger snapshot does not match the planned Agent input")


def _idempotency_key(prefix: str, proposal_id: str) -> str:
    return f"{prefix}-{proposal_id}"
