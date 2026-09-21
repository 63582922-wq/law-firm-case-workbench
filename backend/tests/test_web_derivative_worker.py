from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import shutil
import unittest

from reportlab.pdfgen import canvas

from case_api.web_derivative_worker import WebDerivativeWorkerBlocked, WebEvidenceDerivativeWorker
from case_kernel.evidence_manifest_postgres import DerivativeRunLease, PersistentEvidenceSnapshot
from case_kernel.models import Actor, Role


class _Store:
    def __init__(self, *, firm_id: str, matter_id: str, file_id: str, source_hash: str, manifest_id: str, manifest_hash: str, source_locator: object) -> None:
        self.worker = Actor(actor_id=str(uuid4()), firm_id=firm_id, roles=frozenset({Role.SYSTEM_WORKER}))
        self.snapshot = PersistentEvidenceSnapshot(
            matter_id=matter_id,
            version=10,
            snapshot_hash="b" * 64,
            original_files=({
                "evidence_file_id": file_id,
                "original_file_sha256": source_hash,
                "page_count": 1,
            },),
            pages=({
                "evidence_page_id": str(uuid4()),
                "evidence_file_id": file_id,
                "page_number": 1,
                "decision": {"disposition": "INCLUDE"},
                "annotations": ({"annotation_id": str(uuid4()), "x0": 0.1, "y0": 0.1, "x1": 0.6, "y1": 0.4, "label": "还款位置", "status": "APPROVED"},),
            },),
            duplicate_groups=(),
            locked_manifest={"manifest_id": manifest_id, "content_hash": manifest_hash, "status": "LOCKED"},
            derivatives=(),
            derivative_runs=(),
        )
        self.lease = DerivativeRunLease(
            run_id=str(uuid4()), lease_id=str(uuid4()), matter_id=matter_id, manifest_id=manifest_id,
            manifest_content_hash=manifest_hash, attempt_count=1,
            lease_expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), matter_version=11,
        )
        self.source_locator = source_locator
        self.versions = 11
        self.completed = False
        self.failed = False

    def claim_derivative_run(self, **kwargs):
        self.claim_args = kwargs
        return replace(self.lease, run_id=kwargs["run_id"])

    def get_evidence_snapshot(self, **kwargs):
        return self.snapshot

    def get_web_uploaded_original_source_locator(self, **kwargs):
        return self.source_locator

    def register_derivative_candidate(self, **kwargs):
        self.versions += 1
        return SimpleNamespace(object_id=str(uuid4()), matter_version=self.versions)

    def verify_derivative(self, **kwargs):
        self.versions += 1
        return SimpleNamespace(matter_version=self.versions)

    def complete_derivative_run(self, **kwargs):
        self.completed = True
        return SimpleNamespace(matter_version=self.versions + 1)

    def fail_derivative_run(self, **kwargs):
        self.failed = True
        return SimpleNamespace(matter_version=self.versions + 1)


class _ObjectStore:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.stored: list[tuple[str, Path]] = []

    def materialize_verified_pdf(self, stored, *, destination):
        del stored
        target = Path(destination)
        shutil.copyfile(self.source, target)
        return target

    def put_verified_derivative(self, source_path, **kwargs):
        digest = kwargs["artifact_sha256"]
        self.stored.append((digest, Path(source_path)))
        return SimpleNamespace(object_key=f"{digest[:2]}/{digest[2:4]}/{digest}.lca")


class WebDerivativeWorkerTests(unittest.TestCase):
    def test_locked_manifest_is_built_and_completed_under_verified_firm_scope(self) -> None:
        firm_id, matter_id, file_id = str(uuid4()), str(uuid4()), str(uuid4())
        manifest_id, manifest_hash = str(uuid4()), "c" * 64
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            source = root / "source.pdf"
            pdf = canvas.Canvas(str(source))
            pdf.drawString(80, 700, "还款记录")
            pdf.save()
            source_hash = sha256(source.read_bytes()).hexdigest()
            source_locator = SimpleNamespace(stored_object=lambda: SimpleNamespace(
                object_key="originals/v1/%s/%s/%s/%s/%s.pdf" % (firm_id, matter_id, source_hash[:2], source_hash, str(uuid4())),
                content_sha256=source_hash,
                byte_size=source.stat().st_size,
                object_version_id=None,
            ), source_reference_hash="d" * 64)
            store = _Store(
                firm_id=firm_id, matter_id=matter_id, file_id=file_id, source_hash=source_hash,
                manifest_id=manifest_id, manifest_hash=manifest_hash, source_locator=source_locator,
            )
            object_store = _ObjectStore(source)
            run_id = str(uuid4())
            worker = WebEvidenceDerivativeWorker(
                evidence_store=store,
                object_store=object_store,
                worker_root=root,
                system_worker_for_firm=lambda value: store.worker if value == firm_id else Actor(actor_id=str(uuid4()), firm_id=value, roles=frozenset()),
            )
            worker.run(firm_id=firm_id, matter_id=matter_id, run_id=run_id, expected_version=10)
            self.assertTrue(store.completed)
            self.assertFalse(store.failed)
            self.assertEqual(len(object_store.stored), 2)
            self.assertEqual(store.claim_args["actor"].firm_id, firm_id)

    def test_worker_rejects_cross_firm_identity_before_claim(self) -> None:
        firm_id, matter_id = str(uuid4()), str(uuid4())
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            calls: list[object] = []
            class _StoreWithClaim:
                def claim_derivative_run(self, **kwargs):
                    calls.append(kwargs)
                    raise AssertionError("claim must not run")
                def fail_derivative_run(self, **kwargs):
                    calls.append(kwargs)
                def get_evidence_snapshot(self, **kwargs):
                    raise AssertionError("snapshot must not run")
                def get_web_uploaded_original_source_locator(self, **kwargs):
                    raise AssertionError("locator lookup must not run")
                def register_derivative_candidate(self, **kwargs):
                    raise AssertionError("register must not run")
                def verify_derivative(self, **kwargs):
                    raise AssertionError("verify must not run")
                def complete_derivative_run(self, **kwargs):
                    raise AssertionError("complete must not run")
            WebEvidenceDerivativeWorker(
                evidence_store=_StoreWithClaim(),
                object_store=SimpleNamespace(materialize_verified_pdf=lambda **kwargs: None, put_verified_derivative=lambda **kwargs: None),
                worker_root=root,
                system_worker_for_firm=lambda value: Actor(actor_id=str(uuid4()), firm_id=str(uuid4()), roles=frozenset({Role.SYSTEM_WORKER})),
            ).run(firm_id=firm_id, matter_id=matter_id, run_id=str(uuid4()), expected_version=1)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
