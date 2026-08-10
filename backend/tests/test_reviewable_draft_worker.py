from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import unittest

from reportlab.pdfgen import canvas

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf, SandboxedOfficePdfConverter
from case_kernel.reviewable_draft_worker import create_reviewable_docx_draft, create_reviewable_xlsx_ledger


class FakeConverter(SandboxedOfficePdfConverter):
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def convert_generated_document(self, content, *, content_sha256, source_name, detected_kind):
        self.calls.append((content, source_name, detected_kind))
        output = BytesIO()
        document = canvas.Canvas(output)
        document.drawString(30, 700, "synthetic rendered review PDF")
        document.save()
        pdf = output.getvalue()
        return ConvertedOfficePdf(
            source_sha256=content_sha256,
            detected_kind=detected_kind,
            converter_id="fake",
            converter_version="test",
            transform_hash="a" * 64,
            pdf_sha256=sha256(pdf).hexdigest(),
            pdf_bytes=len(pdf),
            page_count=1,
            render_verification_hash="b" * 64,
            pdf_content=pdf,
        )


class ReviewableDraftWorkerTests(unittest.TestCase):
    def test_docx_pair_binds_approved_input_editable_source_and_rendered_preview(self) -> None:
        converter = FakeConverter()
        result = create_reviewable_docx_draft(
            ApprovedDraft(
                title="答辩状",
                sections=(ApprovedSection("答辩意见", ("利息应依法核算。",), ("证据1",)),),
                approval_hash="c" * 64,
            ),
            converter=converter,
        )
        self.assertEqual(converter.calls[0][1:], ("approved-draft.docx", "WORD_DOCUMENT"))
        self.assertEqual(result.editable_artifact.content_sha256, result.review_pdf.source_sha256)
        self.assertEqual(len(result.review_input_hash), 64)

    def test_xlsx_pair_uses_safe_generated_source_and_review_binding(self) -> None:
        converter = FakeConverter()
        result = create_reviewable_xlsx_ledger(
            approval_hash="d" * 64,
            sheet_name="利息明细",
            columns=("日期", "金额"),
            rows=(("2020-08-20", 1000),),
            converter=converter,
        )
        self.assertEqual(converter.calls[0][1:], ("approved-ledger.xlsx", "SPREADSHEET"))
        self.assertEqual(result.editable_artifact.content_sha256, result.review_pdf.source_sha256)
        self.assertEqual(len(result.review_input_hash), 64)


if __name__ == "__main__":
    unittest.main()
