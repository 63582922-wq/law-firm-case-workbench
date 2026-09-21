import unittest

from case_kernel.evidence_manifest import (
    DuplicateResolution,
    EvidenceManifestBlocked,
    EvidenceManifestLedger,
    ManifestStatus,
    PageDisposition,
)
from case_kernel.models import Actor, Role


class EvidenceManifestLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("synthetic-assistant", "synthetic-firm", frozenset({Role.ASSISTANT}))
        self.lead = Actor("synthetic-lead", "synthetic-firm", frozenset({Role.LEAD_LAWYER}))
        self.ledger = EvidenceManifestLedger()
        source = self.ledger.add_original_file(
            self.assistant,
            original_label="[合成] 微信流水.pdf",
            original_file_sha256="a" * 64,
            byte_size=4096,
            media_type="application/pdf",
            page_count=3,
        )
        self.pages = self.ledger.pages_for_file(source.file_id)

    def _approve_page(self, page_index: int, disposition: PageDisposition):
        candidate = self.ledger.propose_page_decision(
            self.assistant,
            page_id=self.pages[page_index].page_id,
            disposition=disposition,
            reason="[合成] 律师核验后的页级处置理由。",
        )
        return self.ledger.approve_page_decision(
            self.lead,
            decision_id=candidate.decision_id,
            approval_hash=f"page-{page_index}-approved",
        )

    def test_lock_requires_a_decision_for_every_source_page(self) -> None:
        self._approve_page(0, PageDisposition.INCLUDE)
        with self.assertRaisesRegex(EvidenceManifestBlocked, "unresolved pages: 2"):
            self.ledger.lock_manifest(self.lead, approval_hash="manifest-approved")

    def test_duplicate_resolution_never_deletes_original_and_only_canonical_page_enters_derivative(self) -> None:
        group = self.ledger.add_duplicate_group_candidate(
            self.assistant,
            page_ids=(self.pages[0].page_id, self.pages[1].page_id),
        )
        resolved = self.ledger.resolve_duplicate_group(
            self.lead,
            group_id=group.group_id,
            same_source_page=True,
            canonical_page_id=self.pages[0].page_id,
            approval_hash="duplicate-approved",
        )
        self.assertEqual(resolved.status, DuplicateResolution.SAME_SOURCE_PAGE)
        self._approve_page(0, PageDisposition.INCLUDE)
        self._approve_page(1, PageDisposition.EXCLUDE)
        self._approve_page(2, PageDisposition.INCLUDE)
        manifest = self.ledger.lock_manifest(self.lead, approval_hash="manifest-approved")
        self.assertEqual(manifest.total_pages, 3)
        self.assertEqual(manifest.included_pages, 2)
        self.assertEqual(manifest.excluded_pages, 1)
        self.assertEqual(len(self.ledger.pages_for_file(self.pages[0].file_id)), 3)
        plan = self.ledger.build_derivative_plan(manifest.manifest_id)
        self.assertEqual([item.source_page_number for item in plan], [1, 3])

    def test_same_source_duplicate_blocks_two_included_pages(self) -> None:
        group = self.ledger.add_duplicate_group_candidate(
            self.assistant,
            page_ids=(self.pages[0].page_id, self.pages[1].page_id),
        )
        self.ledger.resolve_duplicate_group(
            self.lead,
            group_id=group.group_id,
            same_source_page=True,
            canonical_page_id=self.pages[0].page_id,
            approval_hash="duplicate-approved",
        )
        for index in range(3):
            self._approve_page(index, PageDisposition.INCLUDE)
        with self.assertRaisesRegex(EvidenceManifestBlocked, "include only"):
            self.ledger.lock_manifest(self.lead, approval_hash="manifest-approved")

    def test_red_box_is_an_approved_coordinate_annotation_not_an_original_edit(self) -> None:
        annotation = self.ledger.propose_annotation(
            self.assistant,
            page_id=self.pages[0].page_id,
            x0=0.1,
            y0=0.2,
            x1=0.8,
            y1=0.4,
            label="[合成] 与原告微信名相关的交易行",
        )
        annotation = self.ledger.approve_annotation(
            self.lead,
            annotation_id=annotation.annotation_id,
            approval_hash="annotation-approved",
        )
        self._approve_page(0, PageDisposition.INCLUDE)
        self._approve_page(1, PageDisposition.EXCLUDE)
        self._approve_page(2, PageDisposition.EXCLUDE)
        manifest = self.ledger.lock_manifest(self.lead, approval_hash="manifest-approved")
        plan = self.ledger.build_derivative_plan(manifest.manifest_id)
        self.assertEqual(plan[0].annotations, (annotation,))
        self.assertEqual(plan[0].source_file_sha256, "a" * 64)

    def test_upstream_page_decision_invalidates_locked_manifest(self) -> None:
        for index in range(3):
            self._approve_page(index, PageDisposition.INCLUDE)
        manifest = self.ledger.lock_manifest(self.lead, approval_hash="manifest-approved")
        replacement = self.ledger.propose_page_decision(
            self.assistant,
            page_id=self.pages[2].page_id,
            disposition=PageDisposition.EXCLUDE,
            reason="[合成] 复核后改为排除。",
        )
        self.ledger.approve_page_decision(
            self.lead,
            decision_id=replacement.decision_id,
            approval_hash="replacement-approved",
        )
        with self.assertRaisesRegex(EvidenceManifestBlocked, "current locked manifest"):
            self.ledger.build_derivative_plan(manifest.manifest_id)
        self.assertEqual(manifest.status, ManifestStatus.LOCKED)


if __name__ == "__main__":
    unittest.main()
