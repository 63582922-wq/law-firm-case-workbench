from __future__ import annotations

from io import BytesIO
import unittest

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from case_kernel.approved_draft_worker import (
    ApprovedDraft,
    ApprovedDraftBlocked,
    ApprovedSection,
    create_docx_draft,
    create_pdf_draft,
    create_xlsx_ledger,
)


class ApprovedDraftWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.draft = ApprovedDraft(
            title="民事答辩状（草稿）",
            sections=(ApprovedSection("答辩意见", ("对本金金额无异议。",), ("事实-001", "规则-002")),),
            approval_hash="a" * 64,
        )

    def test_generates_readable_docx_and_pdf_from_approved_content(self) -> None:
        docx = create_docx_draft(self.draft)
        pdf = create_pdf_draft(self.draft)
        self.assertEqual(docx.media_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn("答辩意见", "\n".join(paragraph.text for paragraph in Document(BytesIO(docx.content)).paragraphs))
        self.assertEqual(pdf.media_type, "application/pdf")
        self.assertEqual(len(PdfReader(BytesIO(pdf.content)).pages), 1)

    def test_xlsx_ledger_escapes_formula_injection(self) -> None:
        artifact = create_xlsx_ledger(
            approval_hash="b" * 64,
            sheet_name="付款台账",
            columns=("付款说明", "金额"),
            rows=(("=HYPERLINK(\"https://example.invalid\")", 100),),
        )
        workbook = load_workbook(BytesIO(artifact.content), data_only=False)
        sheet = workbook["付款台账"]
        self.assertEqual(sheet["A2"].value, "'=HYPERLINK(\"https://example.invalid\")")
        self.assertEqual(sheet["B2"].value, 100)

    def test_unapproved_content_is_refused(self) -> None:
        with self.assertRaisesRegex(ApprovedDraftBlocked, "approval"):
            create_docx_draft(
                ApprovedDraft("草稿", self.draft.sections, "not-an-approval-hash")
            )
        with self.assertRaisesRegex(ApprovedDraftBlocked, "requires at least one"):
            create_xlsx_ledger(
                approval_hash="c" * 64,
                sheet_name="空台账",
                columns=("金额",),
                rows=(),
            )


if __name__ == "__main__":
    unittest.main()
