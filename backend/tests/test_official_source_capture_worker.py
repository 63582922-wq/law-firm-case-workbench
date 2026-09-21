from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import unittest

from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.official_source_capture_coordinator import OfficialSourceCaptureCoordinationResult
from case_kernel.official_source_capture_postgres import OfficialSourceCaptureRunLease
from case_kernel.official_source_capture_worker import (
    BoundedOfficialSourceCaptureWorker,
    OfficialSourceCaptureWorkerBlocked,
    run_authorized_official_source_capture,
    run_next_authorized_official_source_capture,
)


class CaptureStore:
    def __init__(self, lease: OfficialSourceCaptureRunLease) -> None:
        self.lease = lease
        self.claim_kwargs = None
        self.next_candidate = None

    def find_next_claimable_capture(self, *, actor):
        self.next_actor = actor
        return self.next_candidate

    def claim_capture(self, **kwargs):
        self.claim_kwargs = kwargs
        return self.lease

    def complete_capture(self, **kwargs):
        raise AssertionError(kwargs)

    def fail_capture(self, **kwargs):
        raise AssertionError(kwargs)


class OfficialSourceCaptureWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id, self.run_id, self.firm_id = str(uuid4()), str(uuid4()), str(uuid4())
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.lease = OfficialSourceCaptureRunLease(
            run_id=self.run_id,
            matter_id=self.matter_id,
            matter_version=4,
            lease_id=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            source_id="SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
            publisher="最高人民法院",
            source_tier="JUDICIAL_INTERPRETATION",
            target_url="https://www.court.gov.cn/",
            query_sha256="a" * 64,
            authorization_hash="b" * 64,
            authorized_by=str(uuid4()),
            authorized_at=datetime.now(timezone.utc),
            max_response_bytes=1024,
        )

    def test_claims_only_the_requested_run_before_invoking_coordinator(self) -> None:
        store = CaptureStore(self.lease)
        expected = object()
        with TemporaryDirectory(prefix="official-capture-worker-") as temporary:
            root = Path(temporary) / "case"
            root.mkdir()
            artifact_store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed", key_id="test", encryption_key=b"k" * 32
            )
            with patch(
                "case_kernel.official_source_capture_worker.execute_claimed_official_source_capture",
                return_value=expected,
            ) as execute:
                result = run_authorized_official_source_capture(
                    matter_id=self.matter_id,
                    run_id=self.run_id,
                    expected_version=3,
                    worker=self.worker,
                    claim_idempotency_key="official-worker-claim-0001",
                    case_root=root,
                    artifact_store=artifact_store,
                    store=store,
                )
        self.assertIs(result, expected)
        self.assertEqual(store.claim_kwargs["matter_id"], self.matter_id)
        self.assertEqual(store.claim_kwargs["run_id"], self.run_id)
        self.assertEqual(store.claim_kwargs["actor"], self.worker)
        execute.assert_called_once_with(
            lease=self.lease,
            case_root=root,
            artifact_store=artifact_store,
            persistence=store,
            system_actor=self.worker,
            transport=None,
        )

    def test_refuses_non_worker_identity_before_any_store_call(self) -> None:
        store = CaptureStore(self.lease)
        with TemporaryDirectory(prefix="official-capture-worker-") as temporary:
            root = Path(temporary)
            artifact_store = LocalEncryptedArtifactStore(
                root / "managed", key_id="test", encryption_key=b"k" * 32
            )
            with self.assertRaisesRegex(OfficialSourceCaptureWorkerBlocked, "SYSTEM_WORKER"):
                run_authorized_official_source_capture(
                    matter_id=self.matter_id,
                    run_id=self.run_id,
                    expected_version=3,
                    worker=Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})),
                    claim_idempotency_key="official-worker-claim-0001",
                    case_root=root,
                    artifact_store=artifact_store,
                    store=store,
                )
        self.assertIsNone(store.claim_kwargs)

    def test_next_run_executes_at_most_one_claimable_candidate(self) -> None:
        store = CaptureStore(self.lease)
        store.next_candidate = (self.matter_id, self.run_id, 3)
        with TemporaryDirectory(prefix="official-capture-worker-") as temporary:
            root = Path(temporary) / "case"; root.mkdir()
            artifact_store = LocalEncryptedArtifactStore(Path(temporary) / "managed", key_id="test", encryption_key=b"k" * 32)
            with patch("case_kernel.official_source_capture_worker.run_authorized_official_source_capture", return_value="done") as run:
                self.assertEqual(run_next_authorized_official_source_capture(worker=self.worker, case_root=root, artifact_store=artifact_store, store=store), "done")
                run.assert_called_once()
            store.next_candidate = None
            self.assertIsNone(run_next_authorized_official_source_capture(worker=self.worker, case_root=root, artifact_store=artifact_store, store=store))

    def test_next_run_builds_one_matter_bound_store_only_after_a_candidate_exists(self) -> None:
        store = CaptureStore(self.lease)
        store.next_candidate = (self.matter_id, self.run_id, 3)
        selected = object()
        factory_calls: list[str] = []
        with TemporaryDirectory(prefix="official-capture-worker-") as temporary:
            root = Path(temporary) / "case"; root.mkdir()
            with patch(
                "case_kernel.official_source_capture_worker.run_authorized_official_source_capture",
                return_value="done",
            ) as run:
                self.assertEqual(
                    run_next_authorized_official_source_capture(
                        worker=self.worker,
                        case_root=root,
                        artifact_store_factory=lambda matter_id: (
                            factory_calls.append(matter_id) or selected
                        ),
                        store=store,
                    ),
                    "done",
                )
        self.assertEqual(factory_calls, [self.matter_id])
        self.assertEqual(run.call_args.kwargs["artifact_store"], selected)

    def test_bounded_worker_is_idle_without_an_authorized_capture(self) -> None:
        store = CaptureStore(self.lease)
        with TemporaryDirectory(prefix="official-capture-worker-") as temporary:
            root = Path(temporary) / "case"; root.mkdir()
            worker = BoundedOfficialSourceCaptureWorker(
                worker=self.worker,
                case_root=root,
                store=store,
                artifact_store_factory=lambda _matter_id: object(),
            )
            self.assertFalse(worker.run_cycle())


if __name__ == "__main__":
    unittest.main()
