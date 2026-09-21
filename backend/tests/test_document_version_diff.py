import unittest

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.document_version_diff import diff_approved_document_versions


def draft(*, paragraph: str, sources: tuple[str, ...] = ("fact:1",), extra: bool = False) -> ApprovedDraft:
    sections = [ApprovedSection("答辩意见", (paragraph,), sources)]
    if extra:
        sections.append(ApprovedSection("证据说明", ("详见证据目录。",), ("evidence:1",)))
    return ApprovedDraft("民事答辩状", tuple(sections), "a" * 64)


class DocumentVersionDiffTests(unittest.TestCase):
    def test_identical_content_does_not_require_reapproval_even_if_input_approval_changes(self) -> None:
        before = draft(paragraph="本金为 10000 元。")
        after = ApprovedDraft(before.title, before.sections, "b" * 64)
        result = diff_approved_document_versions(previous=before, current=after)
        self.assertFalse(result.changed)
        self.assertFalse(result.requires_reapproval)
        self.assertEqual(result.changes, ())

    def test_changed_text_and_source_reference_are_hash_only_and_require_reapproval(self) -> None:
        before = draft(paragraph="本金为 10000 元。", sources=("fact:1",))
        after = draft(paragraph="本金为 9000 元。", sources=("fact:2",))
        result = diff_approved_document_versions(previous=before, current=after)
        self.assertTrue(result.changed)
        self.assertTrue(result.requires_reapproval)
        self.assertEqual(len(result.changes), 1)
        change = result.changes[0]
        self.assertEqual(change.change_kind, "SECTION_CHANGED")
        self.assertEqual(change.previous_section_index, 1)
        self.assertNotIn("10000", repr(result))
        self.assertNotIn("9000", repr(result))
        self.assertNotIn("fact:1", repr(result))

    def test_added_or_removed_sections_are_explicit_and_stable(self) -> None:
        before = draft(paragraph="本金为 10000 元。")
        after = draft(paragraph="本金为 10000 元。", extra=True)
        result = diff_approved_document_versions(previous=before, current=after)
        self.assertEqual(result.changes[0].change_kind, "SECTION_ADDED")
        self.assertEqual(result.changes[0].current_section_index, 2)
        self.assertEqual(len(result.diff_hash), 64)

    def test_title_change_is_explicit_and_requires_reapproval(self) -> None:
        before = draft(paragraph="本金为 10000 元。")
        after = ApprovedDraft("民事答辩状（修订）", before.sections, "c" * 64)
        result = diff_approved_document_versions(previous=before, current=after)
        self.assertTrue(result.requires_reapproval)
        self.assertEqual(result.changes[0].change_kind, "DOCUMENT_TITLE_CHANGED")
        self.assertNotIn("修订", repr(result))


if __name__ == "__main__":
    unittest.main()
