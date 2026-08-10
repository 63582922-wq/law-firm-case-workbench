from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from case_kernel.agent_execution_postgres import ExecutableAgentToolProposal
from case_kernel.agent_reviewable_draft_coordinator import (
    AgentReviewableDraftExecutionBlocked,
    approved_docx_draft_input_hash,
    approved_xlsx_ledger_input_hash,
    execute_agent_reviewable_docx_draft,
    execute_agent_reviewable_xlsx_ledger,
)
from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_coordinator import CoordinatedReviewableOfficeDraft


def receipt(*, version: int, object_type: str, object_id: str) -> CaseLedgerCommandReceipt:
    return CaseLedgerCommandReceipt("TEST", "agent-test-key-001", str(uuid4()), version, str(uuid4()), object_type, object_id)


@dataclass
class ProposalReader:
    proposal: ExecutableAgentToolProposal

    def get_executable_proposal(self, **_: object) -> ExecutableAgentToolProposal:
        return self.proposal


class Gateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(review_input_hash="d" * 64), "e" * 64


class ReceiptWriter:
    def __init__(self) -> None:
        self.payload: dict | None = None

    def record_tool_execution_receipt(self, **kwargs):
        self.payload = kwargs
        return receipt(version=12, object_type="AGENT_TOOL_EXECUTION_RECEIPT", object_id=str(uuid4()))


class AgentReviewableDraftCoordinatorTests(TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.worker = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        self.draft = ApprovedDraft(
            title="民事答辩状（审阅稿）",
            sections=(ApprovedSection("答辩意见", ("本金范围以已确认交易台账为准。",), ("fact:confirmed-1",)),),
            approval_hash="a" * 64,
        )
        self.proposal = ExecutableAgentToolProposal(
            proposal_id=str(uuid4()), run_id=str(uuid4()), skill_id="document_drafting",
            skill_version="1.0.0", tool_id="create_reviewable_docx_draft",
            approval_gate="LAWYER_REVIEW", input_hash=approved_docx_draft_input_hash(self.draft),
        )

    def test_executes_only_exact_planned_docx_and_records_result_after_pair_registration(self) -> None:
        gateway, writer = Gateway(), ReceiptWriter()
        coordinated = CoordinatedReviewableOfficeDraft(
            self.matter_id, str(uuid4()), None, None, "d" * 64,
            receipt(version=11, object_type="REVIEWABLE_OFFICE_DRAFT_PAIR", object_id=str(uuid4())),
        )
        with patch("case_kernel.agent_reviewable_draft_coordinator.coordinate_reviewable_office_draft", return_value=coordinated) as coordinate:
            result = execute_agent_reviewable_docx_draft(
                matter_id=self.matter_id, expected_version=10, proposal_id=self.proposal.proposal_id,
                worker=self.worker, draft=self.draft, case_root="/tmp", gateway=gateway,
                proposal_reader=ProposalReader(self.proposal), draft_persistence=object(), artifact_store=object(),
                receipt_writer=writer,
            )
        self.assertEqual(result.execution_receipt.matter_version, 12)
        self.assertEqual(gateway.calls[0]["tool_id"], "create_reviewable_docx_draft")
        self.assertEqual(coordinate.call_args.kwargs["expected_version"], 10)
        self.assertEqual(writer.payload["expected_version"], 11)
        self.assertEqual(writer.payload["output_hash"], "e" * 64)

    def test_rejects_a_draft_that_differs_from_the_planned_snapshot(self) -> None:
        changed = ApprovedDraft(
            title=self.draft.title,
            sections=(ApprovedSection("答辩意见", ("已经改变的正文。",), ("fact:confirmed-1",)),),
            approval_hash=self.draft.approval_hash,
        )
        with self.assertRaisesRegex(AgentReviewableDraftExecutionBlocked, "does not match"):
            execute_agent_reviewable_docx_draft(
                matter_id=self.matter_id, expected_version=10, proposal_id=self.proposal.proposal_id,
                worker=self.worker, draft=changed, case_root="/tmp", gateway=Gateway(),
                proposal_reader=ProposalReader(self.proposal), draft_persistence=object(), artifact_store=object(),
                receipt_writer=ReceiptWriter(),
            )

    def test_executes_only_exact_planned_xlsx_ledger(self) -> None:
        columns = ("日期", "金额", "币种")
        rows = (("2020-08-20", "1000.00", "CNY"),)
        proposal = ExecutableAgentToolProposal(
            proposal_id=str(uuid4()), run_id=str(uuid4()), skill_id="spreadsheet_ledger",
            skill_version="1.0.0", tool_id="create_reviewable_xlsx_ledger",
            approval_gate="LAWYER_REVIEW", input_hash=approved_xlsx_ledger_input_hash(
                approval_hash="b" * 64, sheet_name="核算表", columns=columns, rows=rows,
            ),
        )
        gateway, writer = Gateway(), ReceiptWriter()
        coordinated = CoordinatedReviewableOfficeDraft(
            self.matter_id, str(uuid4()), None, None, "d" * 64,
            receipt(version=11, object_type="REVIEWABLE_OFFICE_DRAFT_PAIR", object_id=str(uuid4())),
        )
        with patch("case_kernel.agent_reviewable_draft_coordinator.coordinate_reviewable_office_draft", return_value=coordinated):
            result = execute_agent_reviewable_xlsx_ledger(
                matter_id=self.matter_id, expected_version=10, proposal_id=proposal.proposal_id,
                worker=self.worker, approval_hash="b" * 64, sheet_name="核算表", columns=columns, rows=rows,
                case_root="/tmp", gateway=gateway, proposal_reader=ProposalReader(proposal),
                draft_persistence=object(), artifact_store=object(), receipt_writer=writer,
            )
        self.assertEqual(result.execution_receipt.matter_version, 12)
        self.assertEqual(gateway.calls[0]["tool_id"], "create_reviewable_xlsx_ledger")
