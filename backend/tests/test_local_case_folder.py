from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.local_case_folder import (
    FolderScanBlocked,
    FolderScanLimits,
    compare_folder_manifests,
    root_fingerprint,
    scan_case_folder,
)


class LocalCaseFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "synthetic-matter"
        self.root.mkdir()
        (self.root / "evidence").mkdir()
        self.pdf = self.root / "evidence" / "payment-proof.pdf"
        self.pdf.write_bytes(b"%PDF-synthetic-alpha")
        self.note = self.root / "timeline.txt"
        self.note.write_text("synthetic timeline", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manifest_is_read_only_hash_inventory(self) -> None:
        before = self.pdf.read_bytes()
        manifest = scan_case_folder(self.root, confirmed_root_fingerprint=root_fingerprint(self.root))

        self.assertEqual(manifest.total_files, 2)
        self.assertEqual(manifest.skipped_symlinks, 0)
        self.assertEqual(manifest.originals[0].relative_path, "evidence/payment-proof.pdf")
        self.assertEqual(manifest.originals[0].sha256, sha256(before).hexdigest())
        self.assertEqual(self.pdf.read_bytes(), before)

    def test_changed_folder_confirmation_or_limits_block_scan(self) -> None:
        with self.assertRaises(FolderScanBlocked):
            scan_case_folder(self.root, confirmed_root_fingerprint="wrong")
        with self.assertRaises(FolderScanBlocked):
            scan_case_folder(
                self.root,
                confirmed_root_fingerprint=root_fingerprint(self.root),
                limits=FolderScanLimits(max_files=1, max_total_bytes=1024),
            )

    def test_symlink_is_not_followed(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.pdf"
        outside.write_bytes(b"outside")
        (self.root / "outside-link.pdf").symlink_to(outside)

        manifest = scan_case_folder(self.root, confirmed_root_fingerprint=root_fingerprint(self.root))

        self.assertEqual(manifest.total_files, 2)
        self.assertEqual(manifest.skipped_symlinks, 1)
        self.assertNotIn("outside-link.pdf", {item.relative_path for item in manifest.originals})

    def test_incremental_comparison_distinguishes_move_modify_missing_and_duplicate(self) -> None:
        previous = scan_case_folder(self.root, confirmed_root_fingerprint=root_fingerprint(self.root))
        moved = self.root / "evidence" / "renamed-proof.pdf"
        self.pdf.rename(moved)
        self.note.write_text("changed timeline", encoding="utf-8")
        (self.root / "new.docx").write_bytes(b"synthetic word")

        current = scan_case_folder(self.root, confirmed_root_fingerprint=root_fingerprint(self.root))
        comparison = compare_folder_manifests(current, previous)
        by_path = {item.relative_path: item for item in comparison.files}

        self.assertEqual(by_path["evidence/renamed-proof.pdf"].change_kind, "MOVED")
        self.assertEqual(by_path["evidence/renamed-proof.pdf"].previous_relative_path, "evidence/payment-proof.pdf")
        self.assertEqual(by_path["timeline.txt"].change_kind, "MODIFIED")
        self.assertEqual(by_path["new.docx"].change_kind, "NEW")
        self.assertEqual(comparison.missing_count, 0)
        self.assertEqual(comparison.duplicate_content_count, 0)
        self.assertEqual(len(current.manifest_hash), 64)

        (self.root / "copy.pdf").write_bytes(moved.read_bytes())
        with_duplicate = scan_case_folder(self.root, confirmed_root_fingerprint=root_fingerprint(self.root))
        self.assertEqual(compare_folder_manifests(with_duplicate, current).duplicate_content_count, 1)


if __name__ == "__main__":
    unittest.main()
