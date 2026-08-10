"""Versioned, least-privilege capability registry for the case Agent.

This module is deliberately not an LLM prompt catalogue.  It is the policy
layer between an Agent plan and deterministic tools: a model can request a
skill, but it receives only the explicitly declared tools, case scope and
approval gates.  No skill grants arbitrary filesystem, shell or network
access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class CapabilityScope(StrEnum):
    CASE_READ = "CASE_READ"
    MANAGED_DERIVATIVE_WRITE = "MANAGED_DERIVATIVE_WRITE"
    PUBLIC_RESEARCH_READ = "PUBLIC_RESEARCH_READ"
    FORMAL_CALCULATION = "FORMAL_CALCULATION"
    COURT_RELEASE = "COURT_RELEASE"


class SkillMaturity(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    GATED = "GATED"
    PLANNED = "PLANNED"


class ApprovalGate(StrEnum):
    NONE = "NONE"
    MATERIAL_SCOPE = "MATERIAL_SCOPE"
    LAWYER_REVIEW = "LAWYER_REVIEW"
    RELEASE_LOCK = "RELEASE_LOCK"


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    version: str
    scopes: frozenset[CapabilityScope]
    mutates_originals: bool
    allows_external_network: bool
    writes_only_managed_derivatives: bool


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    version: str
    title: str
    maturity: SkillMaturity
    required_scopes: frozenset[CapabilityScope]
    allowed_tools: tuple[str, ...]
    approval_gate: ApprovalGate
    output_kind: str
    prohibited_actions: tuple[str, ...]


class SkillRegistryBlocked(PermissionError):
    """A requested capability violates the registered least-privilege policy."""


class CaseSkillRegistry:
    def __init__(self, *, tools: Iterable[ToolDefinition], skills: Iterable[SkillDefinition]) -> None:
        tool_items = tuple(tools)
        skill_items = tuple(skills)
        self._tools = {tool.tool_id: tool for tool in tool_items}
        self._skills = {skill.skill_id: skill for skill in skill_items}
        if len(self._tools) == 0 or len(self._skills) == 0:
            raise ValueError("skill registry requires tools and skills")
        if len(self._tools) != len(tool_items) or len(self._skills) != len(skill_items):
            raise ValueError("skill registry identifiers must be unique")
        for skill in self._skills.values():
            if not skill.allowed_tools:
                raise ValueError(f"skill {skill.skill_id} must have at least one allowed tool")
            for tool_id in skill.allowed_tools:
                tool = self._tools.get(tool_id)
                if tool is None:
                    raise ValueError(f"skill {skill.skill_id} references unknown tool {tool_id}")
                if not skill.required_scopes.issuperset(tool.scopes):
                    raise ValueError(f"skill {skill.skill_id} omits a required scope for tool {tool_id}")
                if tool.mutates_originals:
                    raise ValueError(f"tool {tool_id} cannot mutate original case materials")

    def get_skill(self, skill_id: str) -> SkillDefinition:
        try:
            return self._skills[skill_id]
        except KeyError as error:
            raise SkillRegistryBlocked("requested skill is not registered") from error

    def authorize_tool(
        self,
        *,
        skill_id: str,
        tool_id: str,
        granted_scopes: frozenset[CapabilityScope],
        lawyer_approved: bool,
        release_locked: bool,
    ) -> ToolDefinition:
        skill = self.get_skill(skill_id)
        if skill.maturity is not SkillMaturity.IMPLEMENTED:
            raise SkillRegistryBlocked("requested skill is not enabled in this desktop release")
        if tool_id not in skill.allowed_tools:
            raise SkillRegistryBlocked("tool is not allowed by the requested skill")
        if not skill.required_scopes.issubset(granted_scopes):
            raise SkillRegistryBlocked("case grant does not cover the requested skill")
        if skill.approval_gate is ApprovalGate.LAWYER_REVIEW and not lawyer_approved:
            raise SkillRegistryBlocked("lawyer approval is required before this skill can run")
        if skill.approval_gate is ApprovalGate.RELEASE_LOCK and not release_locked:
            raise SkillRegistryBlocked("a locked release is required before this skill can run")
        return self._tools[tool_id]

    def list_skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(sorted(self._skills.values(), key=lambda skill: skill.skill_id))


def default_case_skill_registry(*, reviewable_office_drafts_enabled: bool = False) -> CaseSkillRegistry:
    """Return the product's explicit current capability surface.

    Word/Excel authoring and Office-to-PDF rendering remain GATED by default.
    A trusted desktop composition may enable the review-pair tools only after
    it has supplied the isolated converter, encrypted artifact store and
    review-pair persistence path.  This prevents a browser or generic Agent
    from merely toggling a capability flag.
    """
    tools = (
        ToolDefinition("register_source_file", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("inspect_pdf_structure", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("inspect_non_pdf_structure", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("normalize_image_or_text_pdf", "1.0.0", frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}), False, False, True),
        ToolDefinition("render_registered_page", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("parse_office_document", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("render_office_to_pdf", "1.0.0", frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}), False, False, True),
        ToolDefinition("create_reviewable_docx_draft", "1.0.0", frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}), False, False, True),
        ToolDefinition("create_reviewable_xlsx_ledger", "1.0.0", frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}), False, False, True),
        ToolDefinition("create_pdf_derivative", "1.0.0", frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}), False, False, True),
        ToolDefinition("review_document_consistency", "1.0.0", frozenset({CapabilityScope.CASE_READ}), False, False, False),
        ToolDefinition("search_authoritative_rules", "1.0.0", frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}), False, False, False),
        ToolDefinition("get_rule_snapshot", "1.0.0", frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}), False, False, False),
        ToolDefinition("plan_private_lending_transition", "1.0.0", frozenset({CapabilityScope.FORMAL_CALCULATION}), False, False, False),
        ToolDefinition("calculate_interest_schedule", "1.0.0", frozenset({CapabilityScope.FORMAL_CALCULATION}), False, False, False),
        ToolDefinition("validate_submission_bundle", "1.0.0", frozenset({CapabilityScope.COURT_RELEASE}), False, False, True),
    )
    no_original_mutation = (
        "不得改写、删除或移动律师选择的原始案卷文件",
        "不得把案卷内容发送给未获批准的外部服务",
        "不得以模型结论替代律师审批或确定性计算",
    )
    skills = (
        SkillDefinition("material_inventory", "1.0.0", "材料盘点与安全读取", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.CASE_READ}), ("register_source_file", "inspect_pdf_structure", "inspect_non_pdf_structure"), ApprovalGate.MATERIAL_SCOPE, "EvidenceInventory", no_original_mutation),
        SkillDefinition("evidence_pdf_normalization", "1.0.0", "图片与文本证据 PDF 规范化", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}), ("normalize_image_or_text_pdf", "render_registered_page"), ApprovalGate.MATERIAL_SCOPE, "NormalizedEvidencePdf", no_original_mutation),
        SkillDefinition("office_reading", "1.0.0", "Word 与 Excel 受控读取", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.CASE_READ}), ("parse_office_document",), ApprovalGate.MATERIAL_SCOPE, "OfficeExtraction", no_original_mutation),
        SkillDefinition("office_pdf_rendering", "1.0.0", "Word 与 Excel 隔离转 PDF", SkillMaturity.IMPLEMENTED if reviewable_office_drafts_enabled else SkillMaturity.GATED, frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}), ("render_office_to_pdf",), ApprovalGate.MATERIAL_SCOPE, "RenderedOfficePdf", no_original_mutation),
        SkillDefinition("document_drafting", "1.0.0", "答辩状和说明文书草拟", SkillMaturity.IMPLEMENTED if reviewable_office_drafts_enabled else SkillMaturity.GATED, frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}), ("create_reviewable_docx_draft", "create_pdf_derivative"), ApprovalGate.LAWYER_REVIEW, "DraftDocument", no_original_mutation),
        SkillDefinition("spreadsheet_ledger", "1.0.0", "交易台账与核算表生成", SkillMaturity.IMPLEMENTED if reviewable_office_drafts_enabled else SkillMaturity.GATED, frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}), ("create_reviewable_xlsx_ledger",), ApprovalGate.LAWYER_REVIEW, "LedgerWorkbook", no_original_mutation),
        SkillDefinition("document_consistency_review", "1.0.0", "文书一致性与来源缺口审查", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.CASE_READ}), ("review_document_consistency",), ApprovalGate.NONE, "DocumentConsistencyReport", no_original_mutation),
        SkillDefinition("legal_rule_research", "1.0.0", "官方法源研究候选规划", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}), ("search_authoritative_rules",), ApprovalGate.LAWYER_REVIEW, "LegalResearchCandidates", no_original_mutation + ("不得由候选规划直接访问网络；外部抓取必须另经律师授权的官方来源队列",)),
        SkillDefinition("interest_calculation", "1.0.0", "利息与本息冲抵计算", SkillMaturity.IMPLEMENTED, frozenset({CapabilityScope.FORMAL_CALCULATION}), ("plan_private_lending_transition", "calculate_interest_schedule"), ApprovalGate.LAWYER_REVIEW, "InterestCalculation", no_original_mutation),
        SkillDefinition("submission_bundle_validation", "1.0.0", "法院提交包校验", SkillMaturity.GATED, frozenset({CapabilityScope.COURT_RELEASE}), ("validate_submission_bundle",), ApprovalGate.RELEASE_LOCK, "SubmissionValidation", no_original_mutation),
    )
    return CaseSkillRegistry(tools=tools, skills=skills)
