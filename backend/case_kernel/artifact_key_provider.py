"""Fail-closed OS key retrieval for encrypted local work products.

The project never accepts an encryption key from an API request or a checked-in
environment file.  The macOS adapter is deliberately read-only: provisioning,
rotation and recovery remain explicit desktop setup operations.
"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass, field
import re
import subprocess
import sys
from typing import Protocol


MANAGED_ARTIFACT_KEY_PURPOSE = "managed-artifact-aes256-gcm-v1"


class ArtifactKeyUnavailable(RuntimeError):
    """A required local encryption key cannot be obtained safely."""


@dataclass(frozen=True)
class ArtifactKeyMaterial:
    key_id: str
    purpose: str
    key_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.key_id):
            raise ArtifactKeyUnavailable("artifact key identifier is invalid")
        if self.purpose != MANAGED_ARTIFACT_KEY_PURPOSE:
            raise ArtifactKeyUnavailable("artifact key purpose is not supported")
        if len(self.key_bytes) != 32:
            raise ArtifactKeyUnavailable("artifact encryption requires exactly 32 key bytes")


class ArtifactKeyProvider(Protocol):
    def get_key(self, *, purpose: str) -> ArtifactKeyMaterial: ...


class MacOSKeychainArtifactKeyProvider:
    """Read one versioned AES key from the current macOS login Keychain."""

    def __init__(
        self,
        *,
        service: str,
        account: str,
        key_id: str,
        security_executable: str = "/usr/bin/security",
        platform_name: str | None = None,
        runner=None,
    ) -> None:
        if not service.strip() or len(service) > 200:
            raise ArtifactKeyUnavailable("Keychain service identifier is required")
        if not account.strip() or len(account) > 200:
            raise ArtifactKeyUnavailable("Keychain account identifier is required")
        if security_executable != "/usr/bin/security":
            raise ArtifactKeyUnavailable("only the fixed macOS security executable is allowed")
        self._service = service.strip()
        self._account = account.strip()
        self._key_id = key_id
        self._security_executable = security_executable
        self._platform_name = platform_name or sys.platform
        self._runner = runner or subprocess.run

    def get_key(self, *, purpose: str) -> ArtifactKeyMaterial:
        if purpose != MANAGED_ARTIFACT_KEY_PURPOSE:
            raise ArtifactKeyUnavailable("artifact key purpose is not supported")
        if self._platform_name != "darwin":
            raise ArtifactKeyUnavailable("macOS Keychain is unavailable on this platform")
        try:
            completed = self._runner(
                [
                    self._security_executable,
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    self._account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ArtifactKeyUnavailable("macOS Keychain lookup failed") from error
        if completed.returncode != 0:
            raise ArtifactKeyUnavailable("the configured artifact key is unavailable in macOS Keychain")
        encoded = completed.stdout.strip()
        if not encoded or len(encoded) > 128 or any(character.isspace() for character in encoded):
            raise ArtifactKeyUnavailable("the Keychain artifact key has an invalid encoding")
        try:
            key_bytes = b64decode(encoded, validate=True)
        except (ValueError, UnicodeEncodeError) as error:
            raise ArtifactKeyUnavailable("the Keychain artifact key has an invalid encoding") from error
        return ArtifactKeyMaterial(
            key_id=self._key_id,
            purpose=purpose,
            key_bytes=key_bytes,
        )


class RejectAllArtifactKeyProvider:
    def get_key(self, *, purpose: str) -> ArtifactKeyMaterial:
        del purpose
        raise ArtifactKeyUnavailable("artifact key provider is not configured")
