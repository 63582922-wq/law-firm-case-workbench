from __future__ import annotations

import os
from pathlib import Path
import plistlib
import tempfile
import unittest

from scripts.finalize_macos_bundle import (
    BundleFinalizationBlocked,
    EXPECTED_BUNDLE_ID,
    EXPECTED_PRODUCT_NAME,
    finalize_macos_bundle,
)


class MacosBundleFinalizationTests(unittest.TestCase):
    def _bundle(self, root: Path, *, bundle_id: str = EXPECTED_BUNDLE_ID) -> Path:
        app = root / EXPECTED_PRODUCT_NAME
        executable_root = app / "Contents" / "MacOS"
        executable_root.mkdir(parents=True)
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleExecutable": "law-case-workbench",
                },
                handle,
            )
        for name in ("law-case-workbench", "lawcase-local-api"):
            target = executable_root / name
            target.write_bytes(b"synthetic executable")
            target.chmod(0o755)
        return app

    def test_signs_inside_out_then_strictly_verifies_fixed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._bundle(Path(directory))
            commands: list[list[str]] = []
            finalize_macos_bundle(app, runner=lambda command: commands.append(list(command)))
            self.assertEqual(len(commands), 4)
            self.assertEqual(commands[0][0:5], ["codesign", "--force", "--sign", "-", "--timestamp=none"])
            self.assertTrue(commands[0][-1].endswith("lawcase-local-api"))
            self.assertTrue(commands[1][-1].endswith("law-case-workbench"))
            self.assertIn(EXPECTED_BUNDLE_ID, commands[2])
            self.assertEqual(commands[3][0:4], ["codesign", "--verify", "--deep", "--strict"])

    def test_rejects_unexpected_bundle_identity_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._bundle(Path(directory), bundle_id="example.invalid")
            commands: list[list[str]] = []
            with self.assertRaises(BundleFinalizationBlocked):
                finalize_macos_bundle(app, runner=lambda command: commands.append(list(command)))
            self.assertEqual(commands, [])

    def test_rejects_sidecar_symlink_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self._bundle(root)
            sidecar = app / "Contents" / "MacOS" / "lawcase-local-api"
            sidecar.unlink()
            outside = root / "outside-sidecar"
            outside.write_bytes(b"synthetic executable")
            os.symlink(outside, sidecar)
            with self.assertRaises(BundleFinalizationBlocked):
                finalize_macos_bundle(app, runner=lambda command: None)


if __name__ == "__main__":
    unittest.main()
