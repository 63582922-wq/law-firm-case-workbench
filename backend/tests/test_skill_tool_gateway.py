from __future__ import annotations

from hashlib import sha256
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from PIL import Image

from case_kernel.local_access_grants import AuthorizedOriginalFile
from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.document_consistency_reviewer import ApprovedDocumentSnapshot, CanonicalDocumentField
from case_kernel.skill_registry import CapabilityScope, default_case_skill_registry
from case_kernel.skill_tool_gateway import CaseSkillToolGateway, SkillToolGatewayBlocked


class CaseSkillToolGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = CaseSkillToolGateway(registry=default_case_skill_registry())

    def _source(self, path: Path) -> AuthorizedOriginalFile:
        return AuthorizedOriginalFile(path.name, path, path.stat().st_size, sha256(path.read_bytes()).hexdigest())

    def test_enabled_normalization_accepts_only_an_authorized_handle(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "付款截图.png"
            Image.new("RGB", (100, 60), color="white").save(path)
            result, output_hash = self.gateway.execute(
                skill_id="evidence_pdf_normalization",
                tool_id="normalize_image_or_text_pdf",
                payload={"source": self._source(path), "detected_kind": "IMAGE"},
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=False,
                release_locked=False,
            )
        self.assertEqual(output_hash, result.pdf_sha256)
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))
        with self.assertRaisesRegex(SkillToolGatewayBlocked, "authorized original"):
            self.gateway.execute(
                skill_id="evidence_pdf_normalization",
                tool_id="normalize_image_or_text_pdf",
                payload={"source": "/arbitrary/path.png", "detected_kind": "IMAGE"},
                granted_scopes=frozenset({CapabilityScope.CASE_READ, CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=False,
                release_locked=False,
            )

    def test_enabled_office_reading_returns_auditable_structured_result(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "材料.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>已付利息</w:t></w:r></w:p></w:body></w:document>")
            result, output_hash = self.gateway.execute(
                skill_id="office_reading",
                tool_id="parse_office_document",
                payload={"source": self._source(path), "detected_kind": "WORD_DOCUMENT"},
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )
        self.assertEqual(result.paragraphs[0].text, "已付利息")
        self.assertEqual(len(output_hash), 64)

    def test_gated_drafting_and_unregistered_adapter_are_blocked(self) -> None:
        with self.assertRaisesRegex(SkillToolGatewayBlocked, "not enabled"):
            self.gateway.execute(
                skill_id="document_drafting",
                tool_id="create_docx_draft",
                payload={},
                granted_scopes=frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                lawyer_approved=True,
                release_locked=False,
            )
        with self.assertRaisesRegex(SkillToolGatewayBlocked, "no local execution adapter"):
            self.gateway.execute(
                skill_id="material_inventory",
                tool_id="inspect_pdf_structure",
                payload={},
                granted_scopes=frozenset({CapabilityScope.CASE_READ}),
                lawyer_approved=False,
                release_locked=False,
            )

    def test_interest_transition_planner_is_a_review_gated_deterministic_skill(self) -> None:
        with self.assertRaisesRegex(SkillToolGatewayBlocked, "lawyer approval"):
            self.gateway.execute(
                skill_id="interest_calculation",
                tool_id="plan_private_lending_transition",
                payload={},
                granted_scopes=frozenset({CapabilityScope.FORMAL_CALCULATION}),
                lawyer_approved=False,
                release_locked=False,
            )
        result, output_hash = self.gateway.execute(
            skill_id="interest_calculation",
            tool_id="plan_private_lending_transition",
            payload={
                "contract_formed_on": date(2019, 6, 17),
                "claim_filed_on": date(2023, 4, 3),
                "first_instance_accepted_on": date(2023, 4, 6),
                "calculation_start": date(2019, 6, 17),
                "calculation_end": date(2023, 8, 1),
            },
            granted_scopes=frozenset({CapabilityScope.FORMAL_CALCULATION}),
            lawyer_approved=True,
            release_locked=False,
        )
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(len(output_hash), 64)

    def test_document_consistency_review_is_non_mutating_and_available_to_the_agent(self) -> None:
        draft = ApprovedDraft(
            title="民事答辩状",
            sections=(ApprovedSection("意见", ("案号为（2026）粤01民初100号。",), ("fact:1",)),),
            approval_hash="a" * 64,
        )
        result, output_hash = self.gateway.execute(
            skill_id="document_consistency_review",
            tool_id="review_document_consistency",
            payload={
                "documents": (ApprovedDocumentSnapshot("document-1", "DEFENCE_STATEMENT", draft),),
                "canonical_fields": (
                    CanonicalDocumentField("case_no", "案号", "（2026）粤01民初100号", ("DEFENCE_STATEMENT",)),
                ),
            },
            granted_scopes=frozenset({CapabilityScope.CASE_READ}),
            lawyer_approved=False,
            release_locked=False,
        )
        self.assertEqual(result.blocking_count, 0)
        self.assertEqual(len(output_hash), 64)
        self.assertEqual(draft.sections[0].paragraphs[0], "案号为（2026）粤01民初100号。")


if __name__ == "__main__":
    unittest.main()
