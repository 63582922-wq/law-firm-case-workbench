from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_document_adapters import DynamicDocumentTaskAdapter
from case_kernel.case_agent_document_binding_postgres import (
    PostgresDocumentBindingBlocked,
    PostgresDynamicDocumentBindingPort,
)
from case_kernel.case_agent_document_delivery import (
    AuthoritativeDocumentSource,
    DocumentSourceKind,
    ReviewableDocumentFormat,
    build_deterministic_case_review_memo_candidate,
    build_deterministic_defence_statement_candidate,
    build_deterministic_payment_ledger_candidate,
    canonical_document_candidate_bytes,
    first_release_reviewable_document_templates,
)
from case_kernel.case_agent_supervisor import AdapterExecutionMode, NetworkPolicy
from case_kernel.case_agent_planning_snapshot_postgres import (
    planning_work_plan_item_content_hash,
)
from case_kernel.case_ledger_postgres import _payload_hash
from case_kernel.case_work_plan import (
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
)
from case_kernel.deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
)
from case_kernel.deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftProvider,
)
from case_kernel.models import Actor, Role
from backend.tests.test_case_agent_document_delivery import (
    lawyer_decision_package_payload,
)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Cursor:
    def __init__(self, *, row=None, rows=()) -> None:
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args):
        return False


class _OfficialSourceText:
    def read_verified_source_text(self, **_kwargs):
        return "[合成] 已核验的官方法源摘录。"


class _VerifiedLawyerPackage:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def read_verified_lawyer_decision_package(self, **kwargs):
        self.calls.append(dict(kwargs))
        payload = lawyer_decision_package_payload()
        return AuthoritativeDocumentSource(
            input_ref=f"lawyer-decision-package:{uuid4()}",
            source_kind=DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE,
            source_version="verified-fixture",
            source_hash=sha256(payload).hexdigest(),
            label="已独立验证的律师决策包候选（仅供律师复核）",
            text=payload.decode("utf-8"),
        )


class _UnusedExchange:
    def send(self, **_kwargs):
        raise AssertionError("adapter preparation must not submit to a model")

    def recover(self, **_kwargs):
        raise AssertionError("adapter preparation must not recover a model call")


class _UnusedConverter:
    def convert_generated_document(self, *_args, **_kwargs):
        raise AssertionError("adapter preparation must not render a document")


class _UnusedStaging:
    def stage_document_package(self, _request):
        raise AssertionError("adapter preparation must not persist an artifact")


class _BindingConnection:
    """Server rows for one actual binder -> adapter preparation path."""

    def __init__(self, *, deliverable_kind: str) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.worker_id = str(uuid4())
        self.run_id = str(uuid4())
        self.source_run_id = str(uuid4())
        self.graph_id = str(uuid4())
        self.attempt_id = str(uuid4())
        self.plan_id = str(uuid4())
        self.item_id = str(uuid4())
        self.profile_id = str(uuid4())
        self.goal_id = str(uuid4())
        self.binding_id = str(uuid4())
        self.claim_binding_id = str(uuid4())
        self.legal_source_binding_id = str(uuid4())
        self.legal_rule_binding_id = str(uuid4())
        self.transaction_id = str(uuid4())
        self.fact_id = str(uuid4())
        self.claim_id = str(uuid4())
        self.claim_response_id = str(uuid4())
        self.legal_source_id = str(uuid4())
        self.legal_rule_id = str(uuid4())
        self.confirmed_by = str(uuid4())
        self.fact_decided_by = str(uuid4())
        self.response_approved_by = str(uuid4())
        self.matter_version = 19
        self.source_snapshot_version = 17
        self.task_input_hash = digest("task-input")
        self.snapshot_hash = digest("case-snapshot")
        self.plan_hash = digest("active-plan")
        self.profile_hash = digest("posture")
        self.goal_hash = digest("goal")
        self.binding_hash = digest("promotion-binding")
        self.claim_binding_hash = digest("promotion-claim-binding")
        self.legal_source_binding_hash = digest("promotion-legal-source-binding")
        self.legal_rule_binding_hash = digest("promotion-legal-rule-binding")
        self.confirmation_hash = digest("transaction-confirmation")
        self.fact_decision_hash = digest("fact-decision")
        self.claim_confirmation_hash = digest("claim-confirmation")
        self.claim_response_approval_hash = digest("claim-response-approval")
        self.legal_source_hash = digest("official-legal-source")
        self.legal_rule_approval_hash = digest("approved-legal-rule")
        self.deliverable_kind = deliverable_kind
        self.output_format = (
            ReviewableDocumentFormat.DOCX
            if deliverable_kind in {
                "CASE_REVIEW_MEMO",
                "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
                "DEFENCE_STATEMENT",
            }
            else ReviewableDocumentFormat.XLSX
        )
        self.tool_id = (
            "draft_reviewable_docx_package"
            if self.output_format is ReviewableDocumentFormat.DOCX
            else "draft_reviewable_xlsx_package"
        )
        self.transaction_projection = {
            "transaction_id": self.transaction_id,
            "local_date": date(2026, 7, 18),
            "date_precision": "EXACT_DATE",
            "amount": Decimal("125000.00"),
            "currency": "CNY",
            "direction": "INCOMING",
            "payer_label": "甲方",
            "payee_label": "乙方",
            "channel": "BANK",
            "transaction_reference": "BANK-20260718-001",
            "status": "CONFIRMED",
            "evidence_count": 1,
            "confirmation_hash": self.confirmation_hash,
            "confirmed_by": self.confirmed_by,
        }
        self.transaction_content_hash = _payload_hash(
            {
                "schema_version": "planning-case-transaction-v1",
                **self.transaction_projection,
            }
        )
        self.fact_projection = {
            "fact_id": self.fact_id,
            "original_text": "2026年7月18日，甲方向乙方支付货款125000元。",
            "origin": "ASSISTANT_ENTRY",
            "status": "CONFIRMED",
            "evidence_count": 1,
            "decision_hash": self.fact_decision_hash,
            "decided_by": self.fact_decided_by,
            "evidence_links": [{"evidence_page_id": "synthetic-page-reference"}],
            "correction_candidate_id": None,
        }
        self.fact_content_hash = _payload_hash(
            {
                "schema_version": "planning-case-fact-v1",
                **self.fact_projection,
            }
        )
        self.claim_projection = {
            "claim_id": self.claim_id,
            "original_claim_text": "请求被告返还借款本金人民币125,000.00元。",
            "claimed_amount": Decimal("125000.00"),
            "currency": "CNY",
            "status": "CONFIRMED_SCOPE",
            "evidence_count": 1,
            "confirmation_hash": self.claim_confirmation_hash,
            "confirmed_by": self.confirmed_by,
            "response": {
                "claim_response_id": self.claim_response_id,
                "position": "DISPUTE",
                "partial_amount": None,
                "currency": None,
                "confirmed_fact_ids": (self.fact_id,),
                "approval_hash": self.claim_response_approval_hash,
                "approved_by": self.response_approved_by,
            },
        }
        self.claim_content_hash = _payload_hash(
            {
                "schema_version": "planning-case-claim-v1",
                **self.claim_projection,
            }
        )
        self.item_hash = planning_work_plan_item_content_hash(
            plan_id=self.plan_id,
            plan_hash=self.plan_hash,
            row=self._plan_item_row(),
        )
        self.task_id = str(
            uuid5(
                UUID(self.graph_id),
                "active-work-plan-execution-task-v1:"
                f"{self.item_id}:{self.item_hash}:{self.deliverable_kind}",
            )
        )
        self.executed: list[str] = []

    def _plan_item_row(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "sequence": 3,
            "item_kind": "DOCUMENT_CANDIDATE",
            "readiness": "ACTIONABLE",
            "title": (
                "生成案件审阅意见候选"
                if self.deliverable_kind == "CASE_REVIEW_MEMO"
                else (
                    "生成民事答辩状候选"
                    if self.deliverable_kind == "DEFENCE_STATEMENT"
                    else "生成收付款核对表候选"
                )
            ),
            "purpose": "按已激活计划形成可复核内部成果。",
            "rationale": "精确模板和来源由服务器绑定。",
            "risk_if_omitted": "无法形成可下载工作产品。",
            "confidence": 0.5,
            "review_gate": "LEAD_LAWYER_CONFIRMATION",
            "delivery_target": "INTERNAL_WORK_PRODUCT",
            "deliverable_kind": self.deliverable_kind,
            "required_for_delivery": False,
            "is_primary_document": False,
            "prerequisites": [],
        }

    @property
    def input_refs(self) -> tuple[str, ...]:
        return (f"work-plan-item:{self.item_id}",)

    @property
    def worker(self) -> Actor:
        return Actor(
            self.worker_id,
            self.firm_id,
            frozenset({Role.SYSTEM_WORKER}),
        )

    def execute(self, sql: str, params=()) -> _Cursor:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if normalized.startswith("SELECT task.graph_id"):
            return _Cursor(
                row={
                    "graph_id": self.graph_id,
                    "matter_id": self.matter_id,
                    "input_refs": list(self.input_refs),
                    "input_hash": self.task_input_hash,
                    "tool_id": self.tool_id,
                    "snapshot_hash": self.snapshot_hash,
                    "snapshot_matter_version": self.matter_version,
                    "matter_version": self.matter_version,
                    "attempt_status": "RUNNING",
                    "execution_plan_id": self.plan_id,
                    "execution_plan_hash": self.plan_hash,
                    "execution_source_run_id": self.source_run_id,
                    "execution_item_id": self.item_id,
                    "execution_item_hash": self.item_hash,
                    "execution_deliverable_kind": self.deliverable_kind,
                    "execution_output_format": self.output_format.value,
                }
            )
        if normalized.startswith("SELECT plan.plan_id"):
            return _Cursor(
                row={
                    "plan_id": self.plan_id,
                    "plan_version": 4,
                    "status": "ACTIVE",
                    "plan_hash": self.plan_hash,
                    "profile_id": self.profile_id,
                    "profile_hash": self.profile_hash,
                    "activated_matter_version": self.matter_version,
                    **self._plan_item_row(),
                }
            )
        if "FROM case_work_plan_context_references" in normalized:
            rows = [
                {
                    "source_type": "POSTURE_PROFILE",
                    "source_id": self.profile_id,
                    "source_version": "2",
                    "source_hash": self.profile_hash,
                    "reference_use": "POSTURE",
                },
                {
                    "source_type": "AGENT_GOAL",
                    "source_id": self.goal_id,
                    "source_version": "v1",
                    "source_hash": self.goal_hash,
                    "reference_use": "OBJECTIVE",
                },
            ]
            rows.extend(self._agent_binding_references())
            return _Cursor(rows=rows)
        if "SELECT profile.profile_version AS version" in normalized:
            return _Cursor(
                row={
                    "version": 2,
                    "hash": self.profile_hash,
                    "current": True,
                    "conflict_key": None,
                }
            )
        if "FROM case_agent_goals" in normalized:
            return _Cursor(
                row={
                    "version": "v1",
                    "hash": self.goal_hash,
                    "current": True,
                    "conflict_key": None,
                }
            )
        if normalized.startswith("SELECT binding.object_version AS version"):
            binding = self._agent_binding_by_id(str(params[0]))
            return _Cursor(
                row={
                    "version": binding["source_version"],
                    "hash": binding["source_hash"],
                    "current": True,
                    "conflict_key": None,
                }
            )
        if normalized.startswith("SELECT profile.profile_id"):
            return _Cursor(
                row={
                    "profile_id": self.profile_id,
                    "profile_version": 2,
                    "profile_hash": self.profile_hash,
                    "case_type_code": (
                        "CIVIL.PRIVATE_LENDING"
                        if self.deliverable_kind == "DEFENCE_STATEMENT"
                        else "SALE_CONTRACT_DISPUTE"
                    ),
                    "procedure_stage": (
                        "FIRST_INSTANCE"
                        if self.deliverable_kind == "DEFENCE_STATEMENT"
                        else "PRE_LITIGATION"
                    ),
                    "represented_position": (
                        "DEFENDANT"
                        if self.deliverable_kind == "DEFENCE_STATEMENT"
                        else "CLAIMANT"
                    ),
                    "authority_scope_code": "GENERAL_AUTHORITY",
                    "engagement_state": "ACTIVE",
                    "display_label": "乙方",
                }
            )
        if normalized.startswith("SELECT reference_role"):
            rows = [
                {
                    "reference_role": "SOURCE",
                    "source_type": "POSTURE_PROFILE",
                    "source_id": self.profile_id,
                    "source_version": "2",
                    "source_hash": self.profile_hash,
                    "reference_use": "POSTURE",
                }
            ]
            rows.extend(
                {
                    "reference_role": "SOURCE",
                    **binding,
                }
                for binding in self._agent_binding_references()
            )
            return _Cursor(rows=rows)
        if normalized.startswith("SELECT binding.object_type"):
            binding = self._agent_binding_by_id(str(params[0]))
            return _Cursor(
                row={
                    **binding["projection"],
                    "binding_hash": binding["source_hash"],
                    "snapshot_matter_version": self.source_snapshot_version,
                }
            )
        if normalized.startswith("SELECT fact_id, original_text"):
            assert "decided_by, evidence_links" in normalized
            assert "correction_candidate_id" in normalized
            return _Cursor(row=dict(self.fact_projection))
        if normalized.startswith("SELECT original_text FROM case_facts"):
            return _Cursor(row={"original_text": self.fact_projection["original_text"]})
        if normalized.startswith("SELECT claim.claim_id, claim.original_claim_text"):
            response = self.claim_projection["response"]
            return _Cursor(
                row={
                    "claim_id": self.claim_id,
                    "original_claim_text": self.claim_projection["original_claim_text"],
                    "claimed_amount": self.claim_projection["claimed_amount"],
                    "currency": self.claim_projection["currency"],
                    "status": self.claim_projection["status"],
                    "evidence_count": self.claim_projection["evidence_count"],
                    "confirmation_hash": self.claim_confirmation_hash,
                    "confirmed_by": self.confirmed_by,
                    "claim_response_id": self.claim_response_id,
                    "position": response["position"],
                    "partial_amount": response["partial_amount"],
                    "response_currency": response["currency"],
                    "response_approval_hash": self.claim_response_approval_hash,
                    "response_approved_by": self.response_approved_by,
                    "confirmed_fact_ids": [self.fact_id],
                }
            )
        if normalized.startswith("SELECT claim.original_claim_text, claim.claimed_amount"):
            response = self.claim_projection["response"]
            return _Cursor(
                row={
                    "original_claim_text": self.claim_projection["original_claim_text"],
                    "claimed_amount": self.claim_projection["claimed_amount"],
                    "currency": self.claim_projection["currency"],
                    "position": response["position"],
                    "partial_amount": response["partial_amount"],
                    "partial_currency": response["currency"],
                }
            )
        if normalized.startswith("SELECT source.snapshot_id, source.content_sha256"):
            return _Cursor(
                row={
                    "snapshot_id": self.legal_source_id,
                    "content_sha256": self.legal_source_hash,
                }
            )
        if normalized.startswith("SELECT rule_version_id, rule_version, status, approval_hash"):
            return _Cursor(
                row={
                    "rule_version_id": self.legal_rule_id,
                    "rule_version": "2026.01",
                    "status": "APPROVED",
                    "approval_hash": self.legal_rule_approval_hash,
                }
            )
        if normalized.startswith("SELECT segment.segment_id"):
            return _Cursor(row={"segment_id": str(uuid4())})
        if normalized.startswith("SELECT snapshot_id, source_id, publisher, authority_level"):
            return _Cursor(
                row={
                    "snapshot_id": self.legal_source_id,
                    "source_id": "UNREGISTERED_TEST_SOURCE",
                    "publisher": "最高人民法院",
                    "authority_level": "司法解释",
                    "official_url": "https://www.court.gov.cn/example",
                    "provision_locator": "第六条",
                    "content_sha256": self.legal_source_hash,
                    "content_media_type": "text/plain",
                    "storage_object_key": "server-owned-fixture",
                }
            )
        if normalized.startswith("SELECT rule_id, rule_version, issue_key"):
            return _Cursor(
                row={
                    "rule_id": "PRIVATE_LENDING_INTEREST",
                    "rule_version": "2026.01",
                    "issue_key": "PRIVATE_LENDING_INTEREST",
                    "effective_from": date(2026, 1, 1),
                    "effective_to": None,
                    "trigger_event_kind": "DEFAULT",
                    "formula_kind": "FIXED_ANNUAL_RATE",
                    "base_annual_rate": Decimal("0.036500000000"),
                    "rate_multiplier": None,
                    "derived_annual_rate": Decimal("0.036500000000"),
                    "required_fact_keys": ["default_date"],
                    "transition_rule_versions": [],
                    "conflict_set": "PRIVATE_LENDING_RATE",
                    "priority": 1,
                }
            )
        if normalized.startswith("SELECT transaction_id, local_date"):
            return _Cursor(row=dict(self.transaction_projection))
        if normalized.startswith("SELECT transaction.local_date"):
            return _Cursor(
                row={
                    "local_date": self.transaction_projection["local_date"],
                    "date_precision": "EXACT_DATE",
                    "amount": self.transaction_projection["amount"],
                    "currency": "CNY",
                    "direction": "INCOMING",
                    "payer_label": "甲方",
                    "payee_label": "乙方",
                    "channel": "BANK",
                    "transaction_reference": "BANK-20260718-001",
                    "nature": "PRINCIPAL_REPAYMENT",
                    "same_day_sequence": 1,
                }
            )
        raise AssertionError(f"unexpected binding SQL: {normalized}")

    def _agent_binding_references(self) -> list[dict[str, object]]:
        if self.deliverable_kind == "CASE_REVIEW_MEMO":
            return [
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.binding_id,
                    "source_version": f"v{self.source_snapshot_version}",
                    "source_hash": self.binding_hash,
                    "reference_use": "FACT",
                    "projection": {
                        "object_type": "CASE_FACT",
                        "object_id": self.fact_id,
                        "object_version": f"v{self.source_snapshot_version}",
                        "content_hash": self.fact_content_hash,
                        "source_status": "CONFIRMED",
                        "reference_use": "FACT",
                    },
                }
            ]
        if self.deliverable_kind == "PAYMENT_LEDGER":
            return [
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.binding_id,
                    "source_version": f"v{self.source_snapshot_version}",
                    "source_hash": self.binding_hash,
                    "reference_use": "TRANSACTION",
                    "projection": {
                        "object_type": "CASE_TRANSACTION",
                        "object_id": self.transaction_id,
                        "object_version": f"v{self.source_snapshot_version}",
                        "content_hash": self.transaction_content_hash,
                        "source_status": "CONFIRMED",
                        "reference_use": "TRANSACTION",
                    },
                }
            ]
        if self.deliverable_kind == "DEFENCE_STATEMENT":
            return [
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.binding_id,
                    "source_version": f"v{self.source_snapshot_version}",
                    "source_hash": self.binding_hash,
                    "reference_use": "FACT",
                    "projection": {
                        "object_type": "CASE_FACT",
                        "object_id": self.fact_id,
                        "object_version": f"v{self.source_snapshot_version}",
                        "content_hash": self.fact_content_hash,
                        "source_status": "CONFIRMED",
                        "reference_use": "FACT",
                    },
                },
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.claim_binding_id,
                    "source_version": f"v{self.source_snapshot_version}",
                    "source_hash": self.claim_binding_hash,
                    "reference_use": "CLAIM_SCOPE",
                    "projection": {
                        "object_type": "CASE_CLAIM",
                        "object_id": self.claim_id,
                        "object_version": f"v{self.source_snapshot_version}",
                        "content_hash": self.claim_content_hash,
                        "source_status": "CONFIRMED",
                        "reference_use": "CLAIM_SCOPE",
                    },
                },
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.legal_source_binding_id,
                    "source_version": "v1",
                    "source_hash": self.legal_source_binding_hash,
                    "reference_use": "LEGAL_AUTHORITY",
                    "projection": {
                        "object_type": "VERIFIED_LEGAL_SOURCE",
                        "object_id": self.legal_source_id,
                        "object_version": "v1",
                        "content_hash": self.legal_source_hash,
                        "source_status": "LOCKED",
                        "reference_use": "LEGAL_AUTHORITY",
                    },
                },
                {
                    "source_type": "AGENT_TASK_INPUT",
                    "source_id": self.legal_rule_binding_id,
                    "source_version": "v1",
                    "source_hash": self.legal_rule_binding_hash,
                    "reference_use": "LEGAL_RULE",
                    "projection": {
                        "object_type": "APPROVED_LEGAL_RULE",
                        "object_id": self.legal_rule_id,
                        "object_version": "v1",
                        "content_hash": self.legal_rule_approval_hash,
                        "source_status": "LOCKED",
                        "reference_use": "LEGAL_RULE",
                    },
                },
            ]
        raise AssertionError("unexpected fixture deliverable kind")

    def _agent_binding_by_id(self, binding_id: str) -> dict[str, object]:
        for binding in self._agent_binding_references():
            if binding["source_id"] == binding_id:
                return binding
        raise AssertionError("unexpected fixture binding id")


class CaseAgentDocumentBindingPostgresTests(unittest.TestCase):
    def _promoted_source_port(self) -> PostgresDynamicDocumentBindingPort:
        port = PostgresDynamicDocumentBindingPort.__new__(
            PostgresDynamicDocumentBindingPort
        )
        port._worker = Actor(
            str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER})
        )
        return port

    def test_promoted_dispute_issue_is_rebound_to_its_confirmed_source(self):
        port = self._promoted_source_port()
        matter_id = str(uuid4())
        issue = {
            "issue_id": str(uuid4()),
            "question": "借款交付与还款事实是否已经由证据支持？",
            "status": "CONFIRMED",
            "approval_hash": digest("issue-approval"),
            "approved_by": str(uuid4()),
        }
        binding = {
            "object_type": "DISPUTE_ISSUE",
            "object_id": issue["issue_id"],
            "object_version": "v19",
            "content_hash": _payload_hash(
                {"schema_version": "planning-dispute-issue-v1", **issue}
            ),
            "source_status": "CONFIRMED",
            "reference_use": "FACT",
            "binding_hash": digest("issue-binding"),
            "snapshot_matter_version": 19,
        }

        class Connection:
            def execute(self, sql, _params):
                if "FROM case_agent_work_plan_input_bindings" in sql:
                    return _Cursor(row=binding)
                if "FROM case_dispute_issues" in sql:
                    return _Cursor(row=issue)
                raise AssertionError("unexpected promoted-source query")

        rebound: list[WorkPlanReference] = []
        expected = SimpleNamespace(source_kind="issue")

        def read_source(_connection, *, reference, **_kwargs):
            rebound.append(reference)
            return expected

        port._read_source = read_source
        result = port._read_promoted_input_source(
            Connection(),
            matter_id=matter_id,
            reference=WorkPlanReference(
                source_type=WorkPlanSourceType.AGENT_TASK_INPUT,
                source_id=str(uuid4()),
                source_version="v19",
                source_hash=digest("issue-binding"),
                use=WorkPlanReferenceUse.FACT,
            ),
        )
        self.assertIs(result, expected)
        self.assertEqual(
            rebound,
            [
                WorkPlanReference(
                    source_type=WorkPlanSourceType.DISPUTE_ISSUE,
                    source_id=issue["issue_id"],
                    source_version="v19",
                    source_hash=issue["approval_hash"],
                    use=WorkPlanReferenceUse.FACT,
                )
            ],
        )

    def test_promoted_evidence_page_is_rebound_to_its_approved_page(self):
        port = self._promoted_source_port()
        matter_id = str(uuid4())
        evidence = {
            "evidence_page_id": str(uuid4()),
            "evidence_file_id": str(uuid4()),
            "page_number": 3,
            "rendered_page_sha256": digest("rendered-page"),
            "original_file_sha256": digest("original-file"),
            "media_type": "application/pdf",
            "decision_id": str(uuid4()),
            "disposition": "INCLUDE",
            "decision_status": "APPROVED",
            "approval_hash": digest("evidence-approval"),
        }
        binding = {
            "object_type": "EVIDENCE_PAGE",
            "object_id": evidence["evidence_page_id"],
            "object_version": "v19",
            "content_hash": _payload_hash(
                {"schema_version": "planning-evidence-page-v1", **evidence}
            ),
            "source_status": "CONFIRMED",
            "reference_use": "EVIDENCE",
            "binding_hash": digest("evidence-binding"),
            "snapshot_matter_version": 19,
        }

        class Connection:
            def execute(self, sql, _params):
                if "FROM case_agent_work_plan_input_bindings" in sql:
                    return _Cursor(row=binding)
                if "FROM evidence_pages page" in sql:
                    return _Cursor(row=evidence)
                raise AssertionError("unexpected promoted-source query")

        rebound: list[WorkPlanReference] = []
        expected = SimpleNamespace(source_kind="evidence")

        def read_source(_connection, *, reference, **_kwargs):
            rebound.append(reference)
            return expected

        port._read_source = read_source
        result = port._read_promoted_input_source(
            Connection(),
            matter_id=matter_id,
            reference=WorkPlanReference(
                source_type=WorkPlanSourceType.AGENT_TASK_INPUT,
                source_id=str(uuid4()),
                source_version="v19",
                source_hash=digest("evidence-binding"),
                use=WorkPlanReferenceUse.EVIDENCE,
            ),
        )
        self.assertIs(result, expected)
        self.assertEqual(
            rebound,
            [
                WorkPlanReference(
                    source_type=WorkPlanSourceType.EVIDENCE_PAGE,
                    source_id=evidence["evidence_page_id"],
                    source_version="v19",
                    source_hash=evidence["original_file_sha256"],
                    use=WorkPlanReferenceUse.EVIDENCE,
                )
            ],
        )

    def test_binding_transaction_sets_worker_identity_for_content_job_rls(self):
        from unittest.mock import MagicMock, call
        port = PostgresDynamicDocumentBindingPort.__new__(PostgresDynamicDocumentBindingPort)
        port._dsn = "postgresql://test-only"
        port._worker = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("case_kernel.case_agent_document_binding_postgres.psycopg.connect", return_value=connection):
            with port._transaction() as actual:
                self.assertIs(actual, connection)
        self.assertEqual(connection.execute.call_args_list, [
            call("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"),
            call("SELECT set_config('app.firm_id', %s, true)", (port._worker.firm_id,)),
            call("SELECT set_config('app.actor_id', %s, true)", (port._worker.actor_id,)),
        ])

    def test_content_binding_rejects_missing_authority_and_changed_sources(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        from case_kernel.models import Actor, Role
        port = PostgresDynamicDocumentBindingPort.__new__(PostgresDynamicDocumentBindingPort)
        firm_id, matter_id, run_id, package_id, task_id, item_id, plan_id = [str(uuid4()) for _ in range(7)]
        port._worker = Actor(str(uuid4()), firm_id, frozenset({Role.SYSTEM_WORKER}))
        actor = Actor(str(uuid4()), firm_id, frozenset({Role.LEAD_LAWYER}))
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        port._transaction = lambda: nullcontext(connection)
        args = dict(actor=actor, package_id=package_id, matter_id=matter_id, run_id=run_id)
        with self.assertRaises(PostgresDocumentBindingBlocked):
            port.resolve_content_proposal_binding(**args)
        with self.assertRaises(PermissionError):
            port.resolve_content_proposal_binding(**{**args, "actor": Actor(actor.actor_id, str(uuid4()), actor.roles)})
        row = dict(matter_id=matter_id, matter_version=7, input_refs=[f"work-plan-item:{item_id}"], work_plan_id=plan_id, work_plan_hash="a" * 64, work_plan_item_id=item_id, task_id=task_id, task_input_hash="b" * 64, deliverable_kind="DEFENCE_STATEMENT", binding_hash="c" * 64)
        connection.execute.return_value.fetchone.return_value = row
        port._read_active_plan_item = Mock(return_value=({"plan_id": plan_id, "plan_hash": "a" * 64}, {}))
        port._templates = Mock()
        result = SimpleNamespace(binding_hash="c" * 64)
        port._assemble_binding = Mock(return_value=result)
        self.assertIs(port.resolve_content_proposal_binding(**args), result)
        result.binding_hash = "d" * 64
        with self.assertRaisesRegex(PostgresDocumentBindingBlocked, "sources or template"):
            port.resolve_content_proposal_binding(**args)

    def _prepare(self, deliverable_kind: str):
        connection = _BindingConnection(deliverable_kind=deliverable_kind)
        binding_port = PostgresDynamicDocumentBindingPort(
            dsn="postgresql://server-owned",
            worker_actor=connection.worker,
            templates=first_release_reviewable_document_templates(),
            official_source_text=_OfficialSourceText(),
            verified_lawyer_package=_VerifiedLawyerPackage(),
        )
        provider = DeepSeekDocumentDraftProvider(
            credentials=DeepSeekDocumentDraftCredentials("k" * 40),
            config=DeepSeekDocumentDraftConfig(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model="deepseek-chat",
                allowed_models=("deepseek-chat",),
            ),
            transport=lambda *_args: b"{}",
        )
        adapter = DynamicDocumentTaskAdapter(
            output_format=connection.output_format,
            binding_port=binding_port,
            provider=provider,
            exchange=_UnusedExchange(),
            converter=_UnusedConverter(),
            staging_port=_UnusedStaging(),
        )
        context = SimpleNamespace(
            claim=SimpleNamespace(
                run_id=connection.run_id,
                task_id=connection.task_id,
                attempt_id=connection.attempt_id,
            ),
            task=SimpleNamespace(
                skill=SimpleNamespace(tool_id=connection.tool_id),
                input_hash=connection.task_input_hash,
                input_refs=connection.input_refs,
                capability=SimpleNamespace(
                    execution_mode=AdapterExecutionMode.IN_PROCESS,
                    network_policy=NetworkPolicy.DENY,
                    allowed_domains=(),
                    writes_managed_derivatives=True,
                ),
                budget=SimpleNamespace(
                    max_external_calls=0
                ),
            ),
        )
        with patch.object(
            binding_port,
            "_transaction",
            return_value=_Transaction(connection),
        ):
            binding = adapter._resolve_binding(context)
            if connection.deliverable_kind == "CASE_REVIEW_MEMO":
                result = build_deterministic_case_review_memo_candidate(binding)
            elif connection.deliverable_kind == "DEFENCE_STATEMENT":
                result = build_deterministic_defence_statement_candidate(binding)
            else:
                result = build_deterministic_payment_ledger_candidate(binding)
        binding.validate()
        return connection, binding, result

    def test_case_review_memo_reaches_adapter_with_confirmed_fact_and_source_link(self):
        connection, binding, candidate = self._prepare("CASE_REVIEW_MEMO")
        self.assertEqual(binding.template.output_format, ReviewableDocumentFormat.DOCX)
        self.assertEqual(
            {source.source_kind for source in binding.sources},
            {
                DocumentSourceKind.POSTURE_PROFILE,
                DocumentSourceKind.WORK_PLAN_ITEM,
                DocumentSourceKind.CONFIRMED_FACT,
                DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE,
            },
        )
        candidate_bytes = canonical_document_candidate_bytes(candidate)
        self.assertIn(binding.binding_hash.encode("utf-8"), candidate_bytes)
        self.assertIn(
            connection.fact_projection["original_text"].encode("utf-8"),
            candidate_bytes,
        )
        self.assertIn(f"fact:{connection.fact_id}".encode("utf-8"), candidate_bytes)
        self.assertTrue(
            any("SELECT binding.object_type" in sql for sql in connection.executed)
        )

    def test_payment_ledger_safely_unwraps_confirmed_transaction_for_adapter(self):
        connection, binding, candidate = self._prepare("PAYMENT_LEDGER")
        self.assertEqual(binding.template.output_format, ReviewableDocumentFormat.XLSX)
        self.assertIn(
            DocumentSourceKind.CONFIRMED_TRANSACTION,
            {source.source_kind for source in binding.sources},
        )
        self.assertEqual(candidate.binding_hash, binding.binding_hash)
        self.assertEqual(len(candidate.rows), 1)
        self.assertEqual(
            candidate.rows[0].source_refs,
            (f"transaction:{connection.transaction_id}",),
        )
        self.assertIn("125000.00", candidate.rows[0].cells)
        self.assertTrue(
            any("SELECT binding.object_type" in sql for sql in connection.executed)
        )

    def test_defence_statement_rebinds_claim_law_and_decision_package(self):
        connection, binding, candidate = self._prepare("DEFENCE_STATEMENT")
        self.assertEqual(binding.template.output_format, ReviewableDocumentFormat.DOCX)
        self.assertEqual(
            {source.source_kind for source in binding.sources},
            {
                DocumentSourceKind.POSTURE_PROFILE,
                DocumentSourceKind.WORK_PLAN_ITEM,
                DocumentSourceKind.CONFIRMED_FACT,
                DocumentSourceKind.CONFIRMED_CLAIM,
                DocumentSourceKind.VERIFIED_LEGAL_SOURCE,
                DocumentSourceKind.APPROVED_LEGAL_RULE,
                DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE,
            },
        )
        self.assertEqual(candidate.deliverable_kind, "DEFENCE_STATEMENT")
        candidate_bytes = canonical_document_candidate_bytes(candidate)
        self.assertIn(
            connection.claim_projection["original_claim_text"].encode("utf-8"),
            candidate_bytes,
        )
        self.assertIn(
            f"claim:{connection.claim_id}".encode("utf-8"),
            candidate_bytes,
        )
        self.assertTrue(
            any("case_legal_bundles bundle" in sql for sql in connection.executed)
        )

    def test_ordinary_agent_run_cannot_bind_an_active_plan_document_item(self):
        connection = _BindingConnection(deliverable_kind="CASE_REVIEW_MEMO")
        binding_port = PostgresDynamicDocumentBindingPort(
            dsn="postgresql://server-owned",
            worker_actor=connection.worker,
            templates=first_release_reviewable_document_templates(),
            official_source_text=_OfficialSourceText(),
            verified_lawyer_package=_VerifiedLawyerPackage(),
        )
        original_execute = connection.execute

        def ordinary_run_execute(sql: str, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT task.graph_id"):
                connection.executed.append(normalized)
                return _Cursor(row=None)
            return original_execute(sql, params)

        connection.execute = ordinary_run_execute
        with patch.object(
            binding_port,
            "_transaction",
            return_value=_Transaction(connection),
        ):
            with self.assertRaisesRegex(
                PostgresDocumentBindingBlocked, "not current"
            ):
                binding_port.resolve_document_task(
                    run_id=connection.run_id,
                    task_id=connection.task_id,
                    attempt_id=connection.attempt_id,
                    task_input_hash=connection.task_input_hash,
                    input_refs=connection.input_refs,
                    expected_format=connection.output_format,
                )
        self.assertTrue(
            any(
                "JOIN case_agent_active_plan_execution_runs execution" in sql
                and "goal.active_plan_execution" in sql
                for sql in connection.executed
            )
        )

    def test_promoted_raw_material_is_never_disclosed_to_document_adapter(self):
        connection = _BindingConnection(deliverable_kind="PAYMENT_LEDGER")
        binding_port = PostgresDynamicDocumentBindingPort(
            dsn="postgresql://server-owned",
            worker_actor=connection.worker,
            templates=first_release_reviewable_document_templates(),
            official_source_text=_OfficialSourceText(),
            verified_lawyer_package=_VerifiedLawyerPackage(),
        )
        original_execute = connection.execute

        def raw_material_execute(sql: str, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT binding.object_type"):
                return _Cursor(
                    row={
                        "object_type": "MATERIAL_OBJECT",
                        "object_id": str(uuid4()),
                        "object_version": f"v{connection.source_snapshot_version}",
                        "content_hash": digest("raw-material"),
                        "source_status": "AVAILABLE",
                        "reference_use": "MATERIAL",
                        "binding_hash": connection.binding_hash,
                        "snapshot_matter_version": connection.source_snapshot_version,
                    }
                )
            return original_execute(sql, params)

        connection.execute = raw_material_execute
        with patch.object(
            binding_port,
            "_transaction",
            return_value=_Transaction(connection),
        ):
            with self.assertRaisesRegex(
                PostgresDocumentBindingBlocked,
                "not approved for document disclosure",
            ):
                binding_port.resolve_document_task(
                    run_id=connection.run_id,
                    task_id=connection.task_id,
                    attempt_id=connection.attempt_id,
                    task_input_hash=connection.task_input_hash,
                    input_refs=connection.input_refs,
                    expected_format=connection.output_format,
                )


if __name__ == "__main__":
    unittest.main()
