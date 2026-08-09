from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.local_case_folder import FolderScanBlocked, FolderScanLimits, root_fingerprint, scan_case_folder


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


if __name__ == "__main__":
    unittest.main()
