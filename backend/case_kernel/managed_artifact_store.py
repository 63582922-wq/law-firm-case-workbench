"""Encrypted content-addressed storage for local managed work products.

The store is for derived artifacts and indexes, never original case files. The
encryption key is injected by the desktop key provider and is never persisted
with the object. Object keys reveal only the plaintext SHA-256 digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import struct
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .artifact_key_provider import ArtifactKeyProvider, MANAGED_ARTIFACT_KEY_PURPOSE


MAGIC = b"LCWART1\x00"
NONCE_BYTES = 12


class ManagedArtifactBlocked(ValueError):
    """The managed artifact operation violates encryption or path invariants."""


@dataclass(frozen=True)
class StoredArtifactObject:
    object_key: str
    plaintext_sha256: str
    plaintext_bytes: int
    key_id: str


class LocalEncryptedArtifactStore:
    """Small-scale local AES-256-GCM store for internal desktop preview."""

    def __init__(
        self,
        managed_root: str | Path,
        *,
        key_id: str,
        encryption_key: bytes,
        max_plaintext_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if len(encryption_key) != 32:
            raise ManagedArtifactBlocked("managed artifact encryption requires a 32-byte AES-256 key")
        if not key_id.strip() or len(key_id.encode("utf-8")) > 255:
            raise ManagedArtifactBlocked("managed artifact key_id must be 1 to 255 UTF-8 bytes")
        if max_plaintext_bytes < 1:
            raise ManagedArtifactBlocked("managed artifact byte limit must be positive")
        root = Path(managed_root).expanduser()
        if root.exists() and root.is_symlink():
            raise ManagedArtifactBlocked("managed artifact root cannot be a symbolic link")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._root = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ManagedArtifactBlocked("managed artifact root must be a directory")
        self._root.chmod(0o700)
        self._key_id = key_id.strip()
        self._key = bytes(encryption_key)
        self._max_plaintext_bytes = max_plaintext_bytes

    @classmethod
    def from_key_provider(
        cls,
        managed_root: str | Path,
        *,
        key_provider: ArtifactKeyProvider,
        max_plaintext_bytes: int = 256 * 1024 * 1024,
    ) -> "LocalEncryptedArtifactStore":
        material = key_provider.get_key(purpose=MANAGED_ARTIFACT_KEY_PURPOSE)
        return cls(
            managed_root,
            key_id=material.key_id,
            encryption_key=material.key_bytes,
            max_plaintext_bytes=max_plaintext_bytes,
        )

    @property
    def managed_root(self) -> Path:
        return self._root

    def assert_separate_from_case_root(self, case_root: str | Path) -> None:
        original_root = Path(case_root).expanduser().resolve(strict=True)
        if self._root == original_root or self._root.is_relative_to(original_root):
            raise ManagedArtifactBlocked("managed artifact storage must not be inside the original case folder")
        if original_root.is_relative_to(self._root):
            raise ManagedArtifactBlocked("the original case folder must not be inside managed artifact storage")

    def put_file(
        self,
        source: str | Path,
        *,
        expected_sha256: str,
        case_root: str | Path,
    ) -> StoredArtifactObject:
        self.assert_separate_from_case_root(case_root)
        _validate_sha256(expected_sha256)
        source_path = Path(source).expanduser()
        if source_path.is_symlink():
            raise ManagedArtifactBlocked("managed artifact source cannot be a symbolic link")
        source_path = source_path.resolve(strict=True)
        if not source_path.is_file():
            raise ManagedArtifactBlocked("managed artifact source must be a regular file")
        size = source_path.stat().st_size
        if size < 1 or size > self._max_plaintext_bytes:
            raise ManagedArtifactBlocked("managed artifact source exceeds the configured byte boundary")
        plaintext = source_path.read_bytes()
        actual_hash = sha256(plaintext).hexdigest()
        if actual_hash != expected_sha256:
            raise ManagedArtifactBlocked("managed artifact source hash differs from the verified worker output")
        object_key = _object_key(actual_hash)
        destination = self._safe_object_path(object_key)
        if destination.exists():
            existing = self._decrypt_object(destination, expected_hash=actual_hash)
            if existing != plaintext:
                raise ManagedArtifactBlocked("content-addressed artifact collision or corruption detected")
            return StoredArtifactObject(object_key, actual_hash, size, self._key_id)

        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.parent.chmod(0o700)
        nonce = os.urandom(NONCE_BYTES)
        key_id_bytes = self._key_id.encode("utf-8")
        aad = _aad(self._key_id, actual_hash)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, aad)
        payload = MAGIC + struct.pack("!H", len(key_id_bytes)) + key_id_bytes + nonce + ciphertext
        temporary = destination.parent / f".{destination.name}.{uuid4().hex}.part"
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, destination)
            destination.chmod(0o600)
        except FileExistsError:
            existing = self._decrypt_object(destination, expected_hash=actual_hash)
            if existing != plaintext:
                raise ManagedArtifactBlocked("content-addressed artifact collision or corruption detected")
        finally:
            temporary.unlink(missing_ok=True)
        return StoredArtifactObject(object_key, actual_hash, size, self._key_id)

    def read_bytes(self, object_key: str, *, expected_sha256: str) -> bytes:
        _validate_sha256(expected_sha256)
        if object_key != _object_key(expected_sha256):
            raise ManagedArtifactBlocked("artifact object key does not match the expected plaintext hash")
        return self._decrypt_object(self._safe_object_path(object_key), expected_hash=expected_sha256)

    def materialize(
        self,
        object_key: str,
        destination: str | Path,
        *,
        expected_sha256: str,
    ) -> Path:
        plaintext = self.read_bytes(object_key, expected_sha256=expected_sha256)
        target = Path(destination).expanduser()
        if target.exists() or target.is_symlink():
            raise ManagedArtifactBlocked("materialization destination must not already exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(plaintext)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)
        return target

    def _safe_object_path(self, object_key: str) -> Path:
        expected_parts = object_key.split("/")
        if len(expected_parts) != 3 or any(not part or part in {".", ".."} for part in expected_parts):
            raise ManagedArtifactBlocked("invalid managed artifact object key")
        candidate = (self._root / object_key).resolve()
        if not candidate.is_relative_to(self._root):
            raise ManagedArtifactBlocked("managed artifact object key escaped the configured root")
        return candidate

    def _decrypt_object(self, path: Path, *, expected_hash: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ManagedArtifactBlocked("managed artifact object is missing or unsafe")
        payload = path.read_bytes()
        minimum = len(MAGIC) + 2 + 1 + NONCE_BYTES + 16
        if len(payload) < minimum or payload[: len(MAGIC)] != MAGIC:
            raise ManagedArtifactBlocked("managed artifact header is invalid")
        key_id_length = struct.unpack("!H", payload[len(MAGIC) : len(MAGIC) + 2])[0]
        key_start = len(MAGIC) + 2
        key_end = key_start + key_id_length
        nonce_end = key_end + NONCE_BYTES
        if key_id_length < 1 or nonce_end + 16 > len(payload):
            raise ManagedArtifactBlocked("managed artifact header lengths are invalid")
        try:
            stored_key_id = payload[key_start:key_end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ManagedArtifactBlocked("managed artifact key identifier is invalid") from error
        if stored_key_id != self._key_id:
            raise ManagedArtifactBlocked("managed artifact requires a different encryption key version")
        nonce = payload[key_end:nonce_end]
        ciphertext = payload[nonce_end:]
        try:
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, _aad(stored_key_id, expected_hash))
        except InvalidTag as error:
            raise ManagedArtifactBlocked("managed artifact authentication failed") from error
        if sha256(plaintext).hexdigest() != expected_hash:
            raise ManagedArtifactBlocked("managed artifact plaintext hash verification failed")
        if len(plaintext) > self._max_plaintext_bytes:
            raise ManagedArtifactBlocked("managed artifact exceeds the configured byte boundary")
        return plaintext


def _object_key(plaintext_hash: str) -> str:
    return f"{plaintext_hash[:2]}/{plaintext_hash[2:4]}/{plaintext_hash}.lca"


def _aad(key_id: str, plaintext_hash: str) -> bytes:
    return f"lawcase-managed-artifact-v1|{key_id}|{plaintext_hash}".encode("utf-8")


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ManagedArtifactBlocked("expected_sha256 must be a lowercase SHA-256 value")
