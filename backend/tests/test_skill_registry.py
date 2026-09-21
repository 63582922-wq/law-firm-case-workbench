from __future__ import annotations

import unittest

from case_kernel.skill_registry import (
    CapabilityScope,
    SkillRegistryBlocked,
    default_case_skill_registry,
)


class CaseSkillRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_case_skill_registry()

    def test_only_implemented_skills_can_be_authorized(self) -> None:
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="pdf_reading",
                tool_id="extract_pdf_text",
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        enabled_readers = default_case_skill_registry(
            pdf_reading_adapter_enabled=True,
            common_document_adapter_enabled=True,
        )
        tool = enabled_readers.authorize_tool(
            skill_id="pdf_reading",
            tool_id="extract_pdf_text",
            granted_scopes=frozenset({CapabilityScope.CASE_READ}),
            lawyer_approved=False,
            release_locked=False,
        )
        self.assertFalse(tool.mutates_originals)
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="evidence_pdf_normalization",
                tool_id="normalize_image_or_text_pdf",
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=False,
                release_locked=False,
            )
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="office_pdf_rendering",
                tool_id="render_office_to_pdf",
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=False,
                release_locked=False,
            )

    def test_non_network_research_planning_is_autonomous_and_scope_limited(self) -> None:
        registry = default_case_skill_registry(
            legal_research_planner_enabled=True
        )
        tool = registry.authorize_tool(
            skill_id="legal_rule_research_planning",
            tool_id="plan_authoritative_rule_research",
            granted_scopes=frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
            lawyer_approved=False,
            release_locked=False,
        )
        self.assertFalse(tool.allows_external_network)
        self.assertFalse(tool.mutates_originals)

    def test_unwired_read_and_review_skills_are_fail_closed(self) -> None:
        for skill_id, tool_id, scopes in (
            ("office_reading", "parse_office_document", frozenset({CapabilityScope.CASE_READ})),
            ("document_consistency_review", "review_document_consistency", frozenset({CapabilityScope.CASE_READ})),
            ("legal_rule_research_planning", "plan_authoritative_rule_research", frozenset({CapabilityScope.PUBLIC_RESEARCH_READ})),
        ):
            with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
                self.registry.authorize_tool(
                    skill_id=skill_id,
                    tool_id=tool_id,
                    granted_scopes=scopes,
                    lawyer_approved=True,
                    release_locked=False,
                )

    def test_unlisted_tool_cannot_be_smuggled_through_a_skill(self) -> None:
        registry = default_case_skill_registry(pdf_reading_adapter_enabled=True)
        with self.assertRaisesRegex(SkillRegistryBlocked, "not allowed"):
            registry.authorize_tool(
                skill_id="pdf_reading",
                tool_id="plan_authoritative_rule_research",
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.PUBLIC_RESEARCH_READ}),
                lawyer_approved=True,
                release_locked=False,
            )

    def test_reviewable_office_skills_require_trusted_runtime_enablement(self) -> None:
        enabled = default_case_skill_registry(reviewable_office_drafts_enabled=True)
        tool = enabled.authorize_tool(
            skill_id="document_drafting",
            tool_id="create_reviewable_docx_draft",
            granted_scopes=frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertTrue(tool.writes_only_managed_derivatives)

    def test_dynamic_document_delivery_is_a_separate_local_capability(self) -> None:
        scopes = frozenset(
            {
                CapabilityScope.CASE_READ,
                CapabilityScope.MANAGED_DERIVATIVE_WRITE,
            }
        )
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="dynamic_document_delivery",
                tool_id="draft_reviewable_docx_package",
                granted_scopes=scopes,
                lawyer_approved=True,
                release_locked=False,
            )
        enabled = default_case_skill_registry(dynamic_document_delivery_enabled=True)
        tool = enabled.authorize_tool(
            skill_id="dynamic_document_delivery",
            tool_id="draft_reviewable_docx_package",
            granted_scopes=scopes,
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertFalse(tool.allows_external_network)
        self.assertTrue(tool.writes_only_managed_derivatives)
        self.assertFalse(tool.mutates_originals)
        self.assertEqual(
            enabled.get_skill("dynamic_document_delivery").approval_gate.value,
            "NONE",
        )

    def test_visual_understanding_requires_a_real_server_adapter(self) -> None:
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="image_visual_ocr",
                tool_id="understand_visual_page",
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        enabled = default_case_skill_registry(visual_page_adapter_enabled=True)
        with self.assertRaisesRegex(SkillRegistryBlocked, "lawyer approval"):
            enabled.authorize_tool(
                skill_id="image_visual_ocr",
                tool_id="understand_visual_page",
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        tool = enabled.authorize_tool(
            skill_id="image_visual_ocr",
            tool_id="understand_visual_page",
            granted_scopes=frozenset({CapabilityScope.CASE_READ}),
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertTrue(tool.allows_external_network)
        self.assertFalse(tool.mutates_originals)

    def test_search_can_enable_without_falsely_enabling_official_capture(self) -> None:
        scopes = frozenset({CapabilityScope.PUBLIC_RESEARCH_READ})
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="controlled_web_search",
                tool_id="search_public_web",
                granted_scopes=scopes,
                lawyer_approved=True,
                release_locked=False,
            )
        enabled = default_case_skill_registry(controlled_web_research_enabled=True)
        tool = enabled.authorize_tool(
            skill_id="controlled_web_search",
            tool_id="search_public_web",
            granted_scopes=scopes,
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertTrue(tool.allows_external_network)
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            enabled.authorize_tool(
                skill_id="official_source_capture",
                tool_id="capture_official_source",
                granted_scopes=frozenset(
                    {
                        CapabilityScope.PUBLIC_RESEARCH_READ,
                        CapabilityScope.MANAGED_DERIVATIVE_WRITE,
                    }
                ),
                lawyer_approved=True,
                release_locked=False,
            )


if __name__ == "__main__":
    unittest.main()
