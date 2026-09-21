from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from reportlab.pdfgen import canvas

from case_kernel.evidence_manifest_postgres import (
    PersistentEvidenceSnapshot,
    WebEvidenceOriginalSourceLocator,
)
from case_kernel.models import Actor, Role
from case_kernel.web_agent_evidence_projection import (
    WebAgentEvidenceProjectionBlocked,
    WebAgentEvidenceProjectionPolicy,
    WebAgentEvidenceProjectionSource,
)
from case_kernel.web_object_store import StoredWebEvidenceOriginal


def _pdf(path: Path, texts: tuple[str | None, ...]) -> bytes:
    document = canvas.Canvas(str(path), pagesize=(300, 400), pageCompression=1)
    for text in texts:
        if text is not None:
            document.drawString(36, 360, text)
        document.showPage()
    document.save()
    return path.read_bytes()


class _EvidenceStore:
    def __init__(
        self,
        *,
        firm_id: str,
        matter_id: str,
        snapshot: PersistentEvidenceSnapshot,
        source_locator: WebEvidenceOriginalSourceLocator,
    ) -> None:
        self.firm_id = firm_id
        self.matter_id = matter_id
        self.snapshot = snapshot
        self.source_locator = source_locator
        self.snapshot_calls: list[tuple[str, Actor]] = []
        self.source_calls: list[tuple[str, str, Actor]] = []

    def get_evidence_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentEvidenceSnapshot:
        self.snapshot_calls.append((matter_id, actor))
        if matter_id != self.matter_id or actor.firm_id != self.firm_id:
            raise PermissionError("not visible")
        return self.snapshot

    def get_web_uploaded_original_source_locator(
        self,
        *,
        matter_id: str,
        evidence_file_id: str,
        actor: Actor,
    ) -> WebEvidenceOriginalSourceLocator:
        self.source_calls.append((matter_id, evidence_file_id, actor))
        if (
            matter_id != self.matter_id
            or evidence_file_id != self.source_locator.evidence_file_id
            or actor.firm_id != self.firm_id
            or actor.roles != frozenset({Role.SYSTEM_WORKER})
        ):
            raise PermissionError("not visible")
        return self.source_locator


class _ObjectStore:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[StoredWebEvidenceOriginal, Path]] = []

    def materialize_verified_pdf(
        self,
        stored: StoredWebEvidenceOriginal,
        *,
        destination: str | Path,
    ) -> Path:
        target = Path(destination)
        self.calls.append((stored, target))
        target.write_bytes(self.content)
        target.chmod(0o600)
        return target


class WebAgentEvidenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="web-agent-evidence-projection-test-")
        self.base = Path(self.temporary.name).resolve()
        self.worker_root = self.base / "worker"
        self.worker_root.mkdir(mode=0o700)
        self.worker_root.chmod(0o700)
        self.source_path = self.base / "source.pdf"
        self.source_bytes = _pdf(self.source_path, ("party payment 5000", None))
        self.source_hash = sha256(self.source_bytes).hexdigest()
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.file_id = str(uuid4())
        self.page_one = str(uuid4())
        self.page_two = str(uuid4())
        self.human = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.stored = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{self.firm_id}/{self.matter_id}/"
                f"{self.source_hash[:2]}/{self.source_hash}/{uuid4()}.pdf"
            ),
            content_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            object_version_id="opaque-version-1",
        )
        self.source_locator = WebEvidenceOriginalSourceLocator(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            evidence_file_id=self.file_id,
            original_file_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            page_count=2,
            source_reference_hash=sha256(self.stored.object_key.encode("ascii")).hexdigest(),
            object_key=self.stored.object_key,
            object_version_id=self.stored.object_version_id,
        )
        self.snapshot = PersistentEvidenceSnapshot(
            matter_id=self.matter_id,
            version=7,
            snapshot_hash="f" * 64,
            original_files=(
                {
                    "evidence_file_id": self.file_id,
                    "original_label": "private name.pdf",
                    "original_file_sha256": self.source_hash,
                    "byte_size": len(self.source_bytes),
                    "media_type": "application/pdf",
                    "page_count": 2,
                    "source_scan_fingerprint": "e" * 64,
                    "supersedes_file_id": None,
                    "created_at": "2026-08-13T00:00:00+00:00",
                },
            ),
            pages=(
                {
                    "evidence_page_id": self.page_one,
                    "evidence_file_id": self.file_id,
                    "page_number": 1,
                    "rendered_page_sha256": None,
                    "decision": None,
                    "pending_decision": None,
                    "annotations": (),
                },
                {
                    "evidence_page_id": self.page_two,
                    "evidence_file_id": self.file_id,
                    "page_number": 2,
                    "rendered_page_sha256": None,
                    "decision": None,
                    "pending_decision": None,
                    "annotations": (),
                },
            ),
            duplicate_groups=(),
            locked_manifest=None,
            derivatives=(),
            derivative_runs=(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source(
        self,
        *,
        snapshot: PersistentEvidenceSnapshot | None = None,
        source_locator: WebEvidenceOriginalSourceLocator | None = None,
        object_store: _ObjectStore | None = None,
        worker=None,
    ) -> tuple[WebAgentEvidenceProjectionSource, _EvidenceStore, _ObjectStore]:
        evidence_store = _EvidenceStore(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            snapshot=snapshot or self.snapshot,
            source_locator=source_locator or self.source_locator,
        )
        objects = object_store or _ObjectStore(self.source_bytes)
        source = WebAgentEvidenceProjectionSource(
            evidence_store=evidence_store,
            object_store=objects,
            system_worker_for_firm=(worker or (lambda firm_id: self.worker)),
            policy=WebAgentEvidenceProjectionPolicy(worker_root=self.worker_root),
        )
        return source, evidence_store, objects

    def test_projects_selected_registered_pages_and_erases_every_private_file(self) -> None:
        source, evidence_store, objects = self._source()

        projections = source.load_pages(
            actor=self.human,
            matter_id=self.matter_id,
            evidence_page_ids=(self.page_one, self.page_two),
        )

        self.assertEqual([item.evidence_page_id for item in projections], [self.page_one, self.page_two])
        self.assertEqual([item.page_number for item in projections], [1, 2])
        self.assertIn("5000", projections[0].extracted_text)
        self.assertEqual(projections[1].extracted_text, "")
        self.assertEqual({item.source_file_sha256 for item in projections}, {self.source_hash})
        self.assertEqual(evidence_store.snapshot_calls, [(self.matter_id, self.human)])
        self.assertEqual(evidence_store.source_calls, [(self.matter_id, self.file_id, self.worker)])
        self.assertEqual(len(objects.calls), 1)
        self.assertNotIn(self.stored.object_key, repr(source))
        self.assertNotIn(str(self.worker_root), repr(source))
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_cross_firm_and_cross_matter_before_private_object_lookup(self) -> None:
        for actor, matter_id in (
            (Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER})), self.matter_id),
            (self.human, str(uuid4())),
        ):
            with self.subTest(actor=actor, matter_id=matter_id):
                source, evidence_store, objects = self._source()
                with self.assertRaisesRegex(WebAgentEvidenceProjectionBlocked, "unavailable"):
                    source.load_pages(
                        actor=actor,
                        matter_id=matter_id,
                        evidence_page_ids=(self.page_one,),
                    )
                self.assertEqual(evidence_store.source_calls, [])
                self.assertEqual(objects.calls, [])
                self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_unknown_page_before_worker_or_object_store(self) -> None:
        source, evidence_store, objects = self._source()
        with self.assertRaisesRegex(WebAgentEvidenceProjectionBlocked, "unavailable"):
            source.load_pages(
                actor=self.human,
                matter_id=self.matter_id,
                evidence_page_ids=(str(uuid4()),),
            )
        self.assertEqual(evidence_store.source_calls, [])
        self.assertEqual(objects.calls, [])
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_wrong_materialized_hash_without_leaking_private_values_and_cleans_up(self) -> None:
        altered = self.source_bytes + b"changed"
        source, _, objects = self._source(object_store=_ObjectStore(altered))
        with self.assertRaises(WebAgentEvidenceProjectionBlocked) as raised:
            source.load_pages(
                actor=self.human,
                matter_id=self.matter_id,
                evidence_page_ids=(self.page_one,),
            )
        message = str(raised.exception)
        self.assertNotIn(self.stored.object_key, message)
        self.assertNotIn(str(self.worker_root), message)
        self.assertNotIn("private name", message)
        self.assertEqual(len(objects.calls), 1)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_source_binding_for_another_original_before_materialization(self) -> None:
        wrong = replace(self.source_locator, evidence_file_id=str(uuid4()))
        source, _, objects = self._source(source_locator=wrong)
        with self.assertRaises(WebAgentEvidenceProjectionBlocked):
            source.load_pages(
                actor=self.human,
                matter_id=self.matter_id,
                evidence_page_ids=(self.page_one,),
            )
        self.assertEqual(objects.calls, [])
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_cross_firm_source_binding_before_materialization(self) -> None:
        wrong_firm = str(uuid4())
        wrong = replace(self.source_locator, firm_id=wrong_firm)
        source, _, objects = self._source(source_locator=wrong)
        with self.assertRaises(WebAgentEvidenceProjectionBlocked):
            source.load_pages(
                actor=self.human,
                matter_id=self.matter_id,
                evidence_page_ids=(self.page_one,),
            )
        self.assertEqual(objects.calls, [])
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_cross_firm_worker_before_private_object_lookup(self) -> None:
        wrong_worker = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        source, evidence_store, objects = self._source(worker=lambda firm_id: wrong_worker)
        with self.assertRaisesRegex(WebAgentEvidenceProjectionBlocked, "worker"):
            source.load_pages(
                actor=self.human,
                matter_id=self.matter_id,
                evidence_page_ids=(self.page_one,),
            )
        self.assertEqual(evidence_store.source_calls, [])
        self.assertEqual(objects.calls, [])
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_system_worker_cannot_replace_the_human_case_read_authorization(self) -> None:
        source, evidence_store, objects = self._source()
        with self.assertRaisesRegex(WebAgentEvidenceProjectionBlocked, "case read"):
            source.load_pages(
                actor=self.worker,
                matter_id=self.matter_id,
                evidence_page_ids=(self.page_one,),
            )
        self.assertEqual(evidence_store.snapshot_calls, [])
        self.assertEqual(objects.calls, [])


if __name__ == "__main__":
    unittest.main()
