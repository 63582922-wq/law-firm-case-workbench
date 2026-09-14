from __future__ import annotations

from dataclasses import dataclass
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from case_kernel.agent_execution_postgres import (
    AgentToolProposal,
    PostgresAgentExecutionStore,
    agent_execution_policy_hash,
)
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.models import Actor, Role
from case_kernel.skill_registry import default_case_skill_registry


@dataclass
class _Result:
    row: dict | None = None
    rows: tuple[dict, ...] = ()

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self) -> None:
        self.proposal_id = str(uuid4())
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return _Result({"authorized": 1})
        if "SELECT request_hash, response_json" in normalized:
            return _Result()
        if normalized.startswith("SELECT m.version,"):
            return _Result({"version": 4, "permitted": True})
        if "FROM agent_action_proposals" in normalized and "FOR KEY SHARE" in normalized:
            return _Result({"proposal_id": self.proposal_id, "skill_id": "office_reading", "tool_id": "parse_office_document", "input_hash": "c" * 64})
        if "FROM agent_action_proposals proposal" in normalized:
            return _Result({
                "proposal_id": self.proposal_id,
                "run_id": str(uuid4()),
                "skill_id": "document_drafting",
                "skill_version": "1.0.0",
                "tool_id": "create_reviewable_docx_draft",
                "approval_gate": "LAWYER_REVIEW",
                "input_hash": "c" * 64,
            })
        if "UPDATE matters SET version = version + 1" in normalized:
            return _Result({"version": 5})
        return _Result()


class _Context:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class AgentExecutionPostgresTests(TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.firm_id = str(uuid4())
        self.lawyer = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.assistant = Actor(str(uuid4()), self.firm_id, frozenset({Role.ASSISTANT}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.registry = default_case_skill_registry(
            pdf_reading_adapter_enabled=True,
            common_document_adapter_enabled=True,
        )
        self.store = PostgresAgentExecutionStore(
            "postgresql://not-used.invalid/lawcase_test", registry=self.registry
        )

    def _run(self, connection: _Connection, callback):
        with patch("case_kernel.agent_execution_postgres.psycopg.connect", return_value=_Context(connection)):
            return callback()

    def test_plan_only_records_implemented_registered_tools(self) -> None:
        connection = _Connection()
        receipt = self._run(connection, lambda: self.store.plan_agent_run(
            matter_id=self.matter_id, actor=self.lawyer, expected_version=4,
            idempotency_key="agent-plan-001", agent_id="case-manager", agent_version="1.0.0",
            policy_manifest_hash=agent_execution_policy_hash(self.registry), input_hash="b" * 64,
            proposals=(AgentToolProposal(1, "office_reading", "parse_office_document", "c" * 64, "d" * 64),),
        ))
        self.assertEqual(receipt.matter_version, 5)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO agent_runs", sql)
        self.assertIn("INSERT INTO agent_action_proposals", sql)
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "not an enabled registered"):
            self.store.plan_agent_run(
                matter_id=self.matter_id, actor=self.lawyer, expected_version=4,
                idempotency_key="agent-plan-002", agent_id="case-manager", agent_version="1.0.0",
                policy_manifest_hash=agent_execution_policy_hash(self.registry), input_hash="b" * 64,
                proposals=(AgentToolProposal(1, "document_drafting", "create_reviewable_docx_draft", "c" * 64, "d" * 64),),
            )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "policy manifest"):
            self.store.plan_agent_run(
                matter_id=self.matter_id, actor=self.lawyer, expected_version=4,
                idempotency_key="agent-plan-forged-policy", agent_id="case-manager", agent_version="1.0.0",
                policy_manifest_hash="a" * 64, input_hash="b" * 64,
                proposals=(AgentToolProposal(1, "office_reading", "parse_office_document", "c" * 64, "d" * 64),),
            )

    def test_planner_surface_excludes_registered_tools_without_execution_adapter(self) -> None:
        exposed = set(self.store.planning_skill_tools())
        self.assertIn(("pdf_reading", "extract_pdf_text"), exposed)
        self.assertNotIn(("material_inventory", "register_source_file"), exposed)
        self.assertNotIn(("evidence_pdf_normalization", "render_registered_page"), exposed)
        self.assertNotIn(("interest_calculation", "calculate_interest_schedule"), exposed)
    def test_only_system_worker_records_hashed_execution_outcome(self) -> None:
        connection = _Connection()
        receipt = self._run(connection, lambda: self.store.record_tool_execution_receipt(
            matter_id=self.matter_id, actor=self.worker, expected_version=4,
            idempotency_key="agent-receipt-001", proposal_id=connection.proposal_id,
            status="SUCCEEDED", output_hash="e" * 64,
        ))
        self.assertEqual(receipt.matter_version, 5)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO agent_tool_execution_receipts", sql)
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "requires only an error code"):
            self.store.record_tool_execution_receipt(
                matter_id=self.matter_id, actor=self.worker, expected_version=4,
                idempotency_key="agent-receipt-002", proposal_id=str(uuid4()),
                status="BLOCKED", output_hash="e" * 64,
            )

    def test_assistant_cannot_plan_office_drafting_even_when_the_runtime_enables_the_tool(self) -> None:
        enabled = PostgresAgentExecutionStore(
            "postgresql://not-used.invalid/lawcase_test",
            registry=default_case_skill_registry(reviewable_office_drafts_enabled=True),
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "lead lawyer or reviewer"):
            enabled.plan_agent_run(
                matter_id=self.matter_id, actor=self.assistant, expected_version=4,
                idempotency_key="agent-plan-draft-001", agent_id="case-manager", agent_version="1.0.0",
                policy_manifest_hash=agent_execution_policy_hash(default_case_skill_registry(reviewable_office_drafts_enabled=True)), input_hash="b" * 64,
                proposals=(AgentToolProposal(1, "document_drafting", "create_reviewable_docx_draft", "c" * 64, "d" * 64),),
            )

    def test_system_worker_reads_only_an_unfinished_minimal_execution_proposal(self) -> None:
        connection = _Connection()
        proposal = self._run(connection, lambda: self.store.get_executable_proposal(
            matter_id=self.matter_id, actor=self.worker, proposal_id=connection.proposal_id,
        ))
        self.assertEqual(proposal.proposal_id, connection.proposal_id)
        self.assertEqual(proposal.tool_id, "create_reviewable_docx_draft")
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("agent_tool_execution_receipts", sql)


if __name__ == "__main__":
    import unittest
    unittest.main()
