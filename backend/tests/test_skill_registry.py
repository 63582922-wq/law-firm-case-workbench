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
        tool = self.registry.authorize_tool(
            skill_id="evidence_pdf_normalization",
            tool_id="normalize_image_or_text_pdf",
            granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            lawyer_approved=False,
            release_locked=False,
        )
        self.assertTrue(tool.writes_only_managed_derivatives)
        with self.assertRaisesRegex(SkillRegistryBlocked, "not enabled"):
            self.registry.authorize_tool(
                skill_id="office_pdf_rendering",
                tool_id="render_office_to_pdf",
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=False,
                release_locked=False,
            )

    def test_research_requires_lawyer_approval_and_only_research_scope(self) -> None:
        with self.assertRaisesRegex(SkillRegistryBlocked, "lawyer approval"):
            self.registry.authorize_tool(
                skill_id="legal_rule_research",
                tool_id="search_authoritative_rules",
                granted_scopes=frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        tool = self.registry.authorize_tool(
            skill_id="legal_rule_research",
            tool_id="search_authoritative_rules",
            granted_scopes=frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertFalse(tool.allows_external_network)
        self.assertFalse(tool.mutates_originals)

    def test_unlisted_tool_cannot_be_smuggled_through_a_skill(self) -> None:
        with self.assertRaisesRegex(SkillRegistryBlocked, "not allowed"):
            self.registry.authorize_tool(
                skill_id="material_inventory",
                tool_id="search_authoritative_rules",
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.PUBLIC_RESEARCH_READ}),
                lawyer_approved=True,
                release_locked=False,
            )


if __name__ == "__main__":
    unittest.main()
