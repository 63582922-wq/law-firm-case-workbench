from __future__ import annotations

import unittest

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.document_consistency_reviewer import (
    ApprovedDocumentSnapshot,
    CanonicalDocumentField,
    DocumentConsistencyBlocked,
    review_document_consistency,
)


class DocumentConsistencyReviewerTests(unittest.TestCase):
    def _draft(self, paragraphs: tuple[str, ...], *, refs: tuple[str, ...] = ("fact:1",)) -> ApprovedDraft:
        return ApprovedDraft(
            title="民事答辩状",
            sections=(ApprovedSection("答辩意见", paragraphs, refs),),
            approval_hash="a" * 64,
        )

    def test_reports_missing_and_conflicting_canonical_values_without_editing_input(self) -> None:
        draft = self._draft(("案号为（2026）粤01民初100号。货币为 USD。",))
        document = ApprovedDocumentSnapshot("doc-1", "DEFENCE_STATEMENT", draft)
        fields = (
            CanonicalDocumentField("case_no", "案号", "（2026）粤01民初100号", ("DEFENCE_STATEMENT",)),
            CanonicalDocumentField("currency", "币种", "CNY", ("DEFENCE_STATEMENT",), ("USD", "HKD")),
            CanonicalDocumentField("court", "受理法院", "广州市某区人民法院", ("DEFENCE_STATEMENT",)),
        )
        report = review_document_consistency(documents=(document,), canonical_fields=fields)
        self.assertEqual(report.blocking_count, 3)
        self.assertEqual({item.code for item in report.findings}, {"MISSING_CANONICAL_VALUE", "CONFLICTING_VALUE"})
        self.assertEqual(draft.sections[0].paragraphs[0], "案号为（2026）粤01民初100号。货币为 USD。")
        self.assertEqual(len(report.input_hash), 64)

    def test_rejects_unknown_document_kind_in_required_field(self) -> None:
        document = ApprovedDocumentSnapshot("doc-1", "DEFENCE_STATEMENT", self._draft(("内容",)))
        with self.assertRaisesRegex(DocumentConsistencyBlocked, "unknown required document kind"):
            review_document_consistency(
                documents=(document,),
                canonical_fields=(CanonicalDocumentField("court", "法院", "某法院", ("EVIDENCE_INDEX",)),),
            )


if __name__ == "__main__":
    unittest.main()
