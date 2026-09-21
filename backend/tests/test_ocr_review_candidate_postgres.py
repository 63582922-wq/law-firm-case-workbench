from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from case_kernel.models import Actor, Role
from case_kernel.ocr_review_candidate_postgres import (
    OcrReviewCandidateSpec,
    PostgresOcrReviewCandidateStore,
)


@dataclass
class _Result:
    row: dict | None = None
    def fetchone(self): return self.row


class _Connection:
    def __init__(self) -> None: self.executed: list[tuple[str, tuple | None]] = []
    def execute(self, sql: str, params: tuple | None = None):
        normalized = " ".join(sql.split()); self.executed.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"): return _Result({"authorized": 1})
        if "SELECT request_hash, response_json" in normalized: return _Result()
        if normalized.startswith("SELECT m.version,"): return _Result({"version": 3, "permitted": True})
        if normalized.startswith("UPDATE matters SET version = version + 1"): return _Result({"version": 4})
        return _Result()


class _Context:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self.connection
    def __exit__(self, *_): return False


class OcrReviewCandidatePostgresTests(TestCase):
    def test_worker_can_stage_only_authenticated_encrypted_utf8_candidate(self) -> None:
        content = "还款 100 元".encode("utf-8"); digest = sha256(content).hexdigest()
        matter_id, firm_id = str(uuid4()), str(uuid4())
        worker = Actor(str(uuid4()), firm_id, frozenset({Role.SYSTEM_WORKER}))
        store = PostgresOcrReviewCandidateStore("postgresql://not-used.invalid/test", artifact_reader=lambda key, expected: content if key.endswith(f"{digest}.lca") and expected == digest else b"")
        spec = OcrReviewCandidateSpec(str(uuid4()), str(uuid4()), "qwen", "a" * 64, f"{digest[:2]}/{digest[2:4]}/{digest}.lca", digest, len(content), "b" * 64)
        connection = _Connection()
        with patch("case_kernel.ocr_review_candidate_postgres.psycopg.connect", return_value=_Context(connection)):
            receipt = store.stage(matter_id=matter_id, actor=worker, expected_version=3, idempotency_key="ocr-stage-001", spec=spec)
        self.assertEqual(receipt.object_type, "OCR_REVIEW_CANDIDATE")
        self.assertIn("INSERT INTO ocr_review_candidates", "\n".join(sql for sql, _ in connection.executed))

