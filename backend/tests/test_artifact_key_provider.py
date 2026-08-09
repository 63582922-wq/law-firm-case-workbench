from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from case_kernel.artifact_key_provider import (
    ArtifactKeyUnavailable,
    MANAGED_ARTIFACT_KEY_PURPOSE,
    MacOSKeychainArtifactKeyProvider,
    RejectAllArtifactKeyProvider,
)
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore


class RecordingRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kwargs) -> CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return CompletedProcess(args, self.returncode, self.stdout, self.stderr)


class ArtifactKeyProviderTests(unittest.TestCase):
    def provider(self, runner: RecordingRunner, *, platform_name: str = "darwin"):
        return MacOSKeychainArtifactKeyProvider(
            service="cn.lawcase.workbench.artifacts",
            account="firm-synthetic",
            key_id="artifact-key-v1",
            platform_name=platform_name,
            runner=runner,
        )

    def test_keychain_read_is_fixed_read_only_and_secret_is_not_represented(self) -> None:
        encoded = b64encode(b"k" * 32).decode("ascii")
        runner = RecordingRunner(stdout=f"{encoded}\n")
        material = self.provider(runner).get_key(purpose=MANAGED_ARTIFACT_KEY_PURPOSE)
        self.assertEqual(material.key_bytes, b"k" * 32)
        self.assertNotIn(encoded, repr(material))
        args, kwargs = runner.calls[0]
        self.assertEqual(args[0:2], ["/usr/bin/security", "find-generic-password"])
        self.assertNotIn(encoded, args)
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C"})
        self.assertEqual(kwargs["timeout"], 5)

    def test_store_can_be_built_without_an_api_or_environment_key(self) -> None:
        runner = RecordingRunner(stdout=b64encode(b"m" * 32).decode("ascii"))
        with TemporaryDirectory(prefix="artifact-key-store-test-") as temporary:
            store = LocalEncryptedArtifactStore.from_key_provider(
                Path(temporary) / "managed",
                key_provider=self.provider(runner),
            )
        self.assertEqual(store.managed_root.name, "managed")

    def test_missing_malformed_wrong_length_and_wrong_platform_fail_closed(self) -> None:
        for runner in (
            RecordingRunner(returncode=44, stderr="sensitive diagnostic"),
            RecordingRunner(stdout="not base64"),
            RecordingRunner(stdout=b64encode(b"short").decode("ascii")),
        ):
            with self.subTest(runner=runner):
                with self.assertRaises(ArtifactKeyUnavailable):
                    self.provider(runner).get_key(purpose=MANAGED_ARTIFACT_KEY_PURPOSE)
        with self.assertRaisesRegex(ArtifactKeyUnavailable, "unavailable on this platform"):
            self.provider(RecordingRunner(), platform_name="linux").get_key(
                purpose=MANAGED_ARTIFACT_KEY_PURPOSE
            )

    def test_unconfigured_provider_and_unknown_purpose_never_return_a_key(self) -> None:
        with self.assertRaisesRegex(ArtifactKeyUnavailable, "not configured"):
            RejectAllArtifactKeyProvider().get_key(purpose=MANAGED_ARTIFACT_KEY_PURPOSE)
        with self.assertRaisesRegex(ArtifactKeyUnavailable, "not supported"):
            self.provider(RecordingRunner()).get_key(purpose="unknown-purpose")


if __name__ == "__main__":
    unittest.main()
