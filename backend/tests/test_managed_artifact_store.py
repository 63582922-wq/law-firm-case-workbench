from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.managed_artifact_store import (
    LocalEncryptedArtifactStore,
    ManagedArtifactBlocked,
)


class LocalEncryptedArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="managed-artifact-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case-folder"
        self.case_root.mkdir()
        self.managed_root = self.root / "managed-data"
        self.source = self.root / "worker-output.pdf"
        self.plaintext = b"%PDF-1.4\nSYNTHETIC DERIVATIVE ONLY\n%%EOF\n"
        self.source.write_bytes(self.plaintext)
        self.plaintext_hash = sha256(self.plaintext).hexdigest()
        self.store = LocalEncryptedArtifactStore(
            self.managed_root,
            key_id="synthetic-test-key-v1",
            encryption_key=b"k" * 32,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_put_is_encrypted_content_addressed_and_round_trips_exact_bytes(self) -> None:
        stored = self.store.put_file(
            self.source,
            expected_sha256=self.plaintext_hash,
            case_root=self.case_root,
        )
        encrypted_path = self.managed_root / stored.object_key
        encrypted_bytes = encrypted_path.read_bytes()
        self.assertNotIn(b"SYNTHETIC DERIVATIVE ONLY", encrypted_bytes)
        self.assertNotEqual(encrypted_bytes, self.plaintext)
        self.assertEqual(self.store.read_bytes(stored.object_key, expected_sha256=self.plaintext_hash), self.plaintext)
        materialized = self.store.materialize(
            stored.object_key,
            self.root / "materialized" / "related-pages.pdf",
            expected_sha256=self.plaintext_hash,
        )
        self.assertEqual(materialized.read_bytes(), self.plaintext)
        self.assertEqual(stored.object_key, f"{self.plaintext_hash[:2]}/{self.plaintext_hash[2:4]}/{self.plaintext_hash}.lca")

    def test_same_plaintext_reuses_object_without_creating_a_second_version(self) -> None:
        first = self.store.put_file(self.source, expected_sha256=self.plaintext_hash, case_root=self.case_root)
        second = self.store.put_bytes(
            self.plaintext,
            expected_sha256=self.plaintext_hash,
            case_root=self.case_root,
        )
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.managed_root.rglob("*.lca"))), 1)

    def test_put_bytes_rejects_wrong_hash_without_writing_plaintext(self) -> None:
        with self.assertRaisesRegex(ManagedArtifactBlocked, "plaintext hash differs"):
            self.store.put_bytes(
                self.plaintext,
                expected_sha256="a" * 64,
                case_root=self.case_root,
            )
        self.assertEqual(list(self.managed_root.rglob("*.lca")), [])

    def test_tamper_wrong_key_and_wrong_hash_fail_closed(self) -> None:
        stored = self.store.put_file(self.source, expected_sha256=self.plaintext_hash, case_root=self.case_root)
        wrong_key_store = LocalEncryptedArtifactStore(
            self.managed_root,
            key_id="synthetic-test-key-v1",
            encryption_key=b"z" * 32,
        )
        with self.assertRaisesRegex(ManagedArtifactBlocked, "authentication failed"):
            wrong_key_store.read_bytes(stored.object_key, expected_sha256=self.plaintext_hash)
        with self.assertRaisesRegex(ManagedArtifactBlocked, "object key does not match"):
            self.store.read_bytes(stored.object_key, expected_sha256="a" * 64)
        encrypted_path = self.managed_root / stored.object_key
        payload = bytearray(encrypted_path.read_bytes())
        payload[-1] ^= 1
        encrypted_path.write_bytes(payload)
        with self.assertRaisesRegex(ManagedArtifactBlocked, "authentication failed"):
            self.store.read_bytes(stored.object_key, expected_sha256=self.plaintext_hash)

    def test_managed_root_and_case_root_must_be_separate(self) -> None:
        nested_store = LocalEncryptedArtifactStore(
            self.case_root / "managed",
            key_id="synthetic-test-key-v1",
            encryption_key=b"k" * 32,
        )
        with self.assertRaisesRegex(ManagedArtifactBlocked, "must not be inside"):
            nested_store.put_file(
                self.source,
                expected_sha256=self.plaintext_hash,
                case_root=self.case_root,
            )


if __name__ == "__main__":
    unittest.main()
