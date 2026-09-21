from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_api.desktop_evidence_intake_runtime import _run_next_authorized_item
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt, CaseLedgerPersistenceBlocked
from case_kernel.evidence_intake_postgres import EvidenceIntakeItemLease
from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.local_intake_authorizations import LocalEvidenceIntakeAuthorizationRegistry
from case_kernel.models import Actor, Role


class LocalWorkerStore:
    def __init__(self, lease: EvidenceIntakeItemLease) -> None:
        self.lease = lease
        self.claim_versions: list[int] = []
        self.reap_versions: list[int] = []
        self.claim_error: Exception | None = None

    def claim_evidence_intake_item(self, **kwargs) -> EvidenceIntakeItemLease:
        self.claim_versions.append(kwargs["expected_version"])
        if self.claim_error is not None:
            raise self.claim_error
        return self.lease

    def reap_exhausted_evidence_intake_items(self, **kwargs) -> CaseLedgerCommandReceipt:
        self.reap_versions.append(kwargs["expected_version"])
        return CaseLedgerCommandReceipt(
            command_name="REAP_EXHAUSTED_EVIDENCE_INTAKE_ITEMS",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=6,
            audit_event_id=str(uuid4()),
            object_type="EVIDENCE_INTAKE_RUN",
            object_id=kwargs["run_id"],
        )


class DesktopEvidenceIntakeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.run_id = str(uuid4())
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.actor.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.lease = EvidenceIntakeItemLease(
            run_id=self.run_id,
            item_id=str(uuid4()),
            lease_id=str(uuid4()),
            matter_id=self.matter_id,
            scan_id=str(uuid4()),
            scan_manifest_hash="a" * 64,
            relative_path="法院送达资料/起诉状.pdf",
            expected_byte_size=1024,
            expected_sha256="b" * 64,
            detected_kind="PDF",
            attempt_count=1,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            matter_version=4,
        )

    def _registry(self) -> LocalEvidenceIntakeAuthorizationRegistry:
        registry = LocalEvidenceIntakeAuthorizationRegistry()
        registry.bind(
            run_id=self.run_id,
            matter_id=self.matter_id,
            folder_grant_id=str(uuid4()),
            grant_actor=self.actor,
            grant_session=self.session,
            expected_version=3,
        )
        return registry

    def _runtime(self, store: LocalWorkerStore) -> object:
        return SimpleNamespace(
            services=SimpleNamespace(
                evidence_manifest_store=store,
                artifact_store=object(),
                office_pdf_converter=None,
            ),
            dependencies=SimpleNamespace(local_folder_grants=object()),
        )

    def test_failed_local_read_waits_for_lease_expiry_and_keeps_claim_version(self) -> None:
        registry = self._registry()
        store = LocalWorkerStore(self.lease)
        with patch(
            "case_api.desktop_evidence_intake_runtime.coordinate_claimed_evidence_intake_item",
            side_effect=RuntimeError("scanner temporarily unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "scanner"):
                _run_next_authorized_item(
                    registry=registry,
                    worker=self.worker,
                    persistent_runtime=self._runtime(store),  # type: ignore[arg-type]
                    scanner=object(),  # type: ignore[arg-type]
                )
        self.assertEqual([3], store.claim_versions)
        self.assertIsNone(registry.next_authorized_run(now=self.lease.lease_expires_at - timedelta(seconds=1)))
        retried = registry.next_authorized_run(now=self.lease.lease_expires_at)
        assert retried is not None
        self.assertEqual(4, retried.expected_version)

    def test_no_claimable_item_reaps_expired_attempts_using_latest_version(self) -> None:
        registry = self._registry()
        store = LocalWorkerStore(self.lease)
        store.claim_error = CaseLedgerPersistenceBlocked("evidence intake run has no claimable item")
        receipt = _run_next_authorized_item(
            registry=registry,
            worker=self.worker,
            persistent_runtime=self._runtime(store),  # type: ignore[arg-type]
            scanner=object(),  # type: ignore[arg-type]
        )
        self.assertIsInstance(receipt, CaseLedgerCommandReceipt)
        self.assertEqual([3], store.reap_versions)
        authorized = registry.next_authorized_run()
        assert authorized is not None
        self.assertEqual(6, authorized.expected_version)


if __name__ == "__main__":
    unittest.main()
