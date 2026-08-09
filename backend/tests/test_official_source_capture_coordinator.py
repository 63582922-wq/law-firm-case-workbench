from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.official_source_capture import OfficialHttpResponse
from case_kernel.official_source_capture_coordinator import (
    OfficialSourceCaptureCoordinationBlocked,
    execute_claimed_official_source_capture,
)
from case_kernel.official_source_capture_postgres import OfficialSourceCaptureRunLease


class StaticTransport:
    def __init__(self, *, url: str, body: bytes, media_type: str = "application/json") -> None:
        self.url = url
        self.body = body
        self.media_type = media_type
        self.calls = 0

    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse:
        self.calls += 1
        return OfficialHttpResponse(
            status_code=200,
            final_url=self.url,
            media_type=self.media_type,
            headers={"content-type": self.media_type},
            body=self.body,
            peer_ip="8.8.8.8",
        )


class FakePersistence:
    def __init__(self, matter_id: str) -> None:
        self.matter_id = matter_id
        self.calls: list[tuple[str, dict]] = []

    def _receipt(self, name: str, kwargs: dict) -> CaseLedgerCommandReceipt:
        self.calls.append((name, kwargs))
        return CaseLedgerCommandReceipt(
            command_name=name.upper(),
            idempotency_key=kwargs["idempotency_key"],
            matter_id=self.matter_id,
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
            object_id=kwargs["run_id"],
        )

    def complete_capture(self, **kwargs):
        return self._receipt("complete_capture", kwargs)

    def fail_capture(self, **kwargs):
        return self._receipt("fail_capture", kwargs)


class OfficialSourceCaptureCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="official-source-coordinator-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case"
        self.case_root.mkdir()
        self.store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-official-source-key-v1",
            encryption_key=b"q" * 32,
        )
        self.firm_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.matter_id = str(uuid4())
        self.run_id = str(uuid4())
        self.url = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN"
        query = "一年期贷款市场报价利率 历史数据"
        self.lease = OfficialSourceCaptureRunLease(
            run_id=self.run_id,
            matter_id=self.matter_id,
            matter_version=3,
            lease_id=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            source_id="CFETS-LPR-HISTORY",
            publisher="全国银行间同业拆借中心",
            source_tier="OFFICIAL_RATE_DATA",
            target_url=self.url,
            query_sha256=sha256(query.encode("utf-8")).hexdigest(),
            authorization_hash="a" * 64,
            authorized_by=str(uuid4()),
            authorized_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            max_response_bytes=32 * 1024 * 1024,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def body() -> bytes:
        return json.dumps(
            {
                "head": {"rep_code": "200"},
                "records": [
                    {"showDateCN": "2026-06-20", "1Y": "3.00", "5Y": "3.50"},
                    {"showDateCN": "2026-07-20", "1Y": "3.00", "5Y": "3.50"},
                ],
            }
        ).encode("utf-8")

    def test_claimed_capture_encrypts_parses_and_only_enters_review_required(self) -> None:
        persistence = FakePersistence(self.matter_id)
        transport = StaticTransport(url=self.url, body=self.body())
        result = execute_claimed_official_source_capture(
            lease=self.lease,
            case_root=self.case_root,
            artifact_store=self.store,
            persistence=persistence,
            system_actor=self.actor,
            transport=transport,
        )
        self.assertEqual(result.status, "REVIEW_REQUIRED")
        self.assertEqual(result.parser_kind, "CFETS_LPR_JSON")
        self.assertEqual([name for name, _ in persistence.calls], ["complete_capture"])
        complete = persistence.calls[0][1]
        self.assertEqual(complete["peer_ip"], "8.8.8.8")
        self.assertEqual(complete["content_sha256"], sha256(self.body()).hexdigest())
        self.assertEqual(len(complete["parsed_summary"]["observations"]), 2)
        self.assertNotIn("raw_text", json.dumps(complete["parsed_summary"]))
        self.assertEqual(
            self.store.read_bytes(
                complete["storage_object_key"], expected_sha256=complete["content_sha256"]
            ),
            self.body(),
        )

    def test_parser_failure_records_bounded_code_without_error_or_source_text(self) -> None:
        persistence = FakePersistence(self.matter_id)
        invalid_body = json.dumps({"head": {"rep_code": "500"}, "records": []}).encode()
        result = execute_claimed_official_source_capture(
            lease=self.lease,
            case_root=self.case_root,
            artifact_store=self.store,
            persistence=persistence,
            system_actor=self.actor,
            transport=StaticTransport(url=self.url, body=invalid_body),
        )
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.failure_code, "LPR_PARSE_BLOCKED")
        self.assertEqual(persistence.calls[0][0], "fail_capture")
        self.assertEqual(set(persistence.calls[0][1]) - {
            "matter_id", "run_id", "lease_id", "actor", "expected_version", "idempotency_key", "failure_code"
        }, set())

    def test_query_hash_mismatch_fails_before_any_network_request(self) -> None:
        persistence = FakePersistence(self.matter_id)
        transport = StaticTransport(url=self.url, body=self.body())
        compromised = OfficialSourceCaptureRunLease(
            **{**self.lease.__dict__, "query_sha256": "f" * 64}
        )
        result = execute_claimed_official_source_capture(
            lease=compromised,
            case_root=self.case_root,
            artifact_store=self.store,
            persistence=persistence,
            system_actor=self.actor,
            transport=transport,
        )
        self.assertEqual(result.failure_code, "CAPTURE_INPUT_BLOCKED")
        self.assertEqual(transport.calls, 0)

    def test_non_worker_identity_is_rejected_without_mutating_run(self) -> None:
        persistence = FakePersistence(self.matter_id)
        lawyer = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        with self.assertRaisesRegex(OfficialSourceCaptureCoordinationBlocked, "SYSTEM_WORKER"):
            execute_claimed_official_source_capture(
                lease=self.lease,
                case_root=self.case_root,
                artifact_store=self.store,
                persistence=persistence,
                system_actor=lawyer,
                transport=StaticTransport(url=self.url, body=self.body()),
            )
        self.assertEqual(persistence.calls, [])


if __name__ == "__main__":
    unittest.main()
