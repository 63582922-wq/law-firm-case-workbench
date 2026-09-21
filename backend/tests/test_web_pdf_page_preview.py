from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import subprocess
import unittest
from uuid import uuid4

from PIL import Image
from reportlab.pdfgen import canvas

from case_kernel.evidence_manifest_postgres import WebEvidenceOriginalSourceLocator
from case_kernel.models import Actor, Role
from case_kernel.original_page_access import OriginalPageLocator
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_pdf_page_preview import (
    WebPdfPagePreviewBlocked,
    WebPdfPagePreviewPolicy,
    WebPdfPagePreviewService,
)


def _two_page_pdf(path: Path) -> bytes:
    document = canvas.Canvas(str(path), pagesize=(300, 400))
    document.drawString(36, 360, "first registered page")
    document.showPage()
    document.drawString(36, 360, "second registered page")
    document.showPage()
    document.save()
    return path.read_bytes()


@dataclass
class FakeEvidenceStore:
    page_locator: OriginalPageLocator
    source_locator: WebEvidenceOriginalSourceLocator

    def __post_init__(self) -> None:
        self.page_calls: list[tuple[str, str, Actor]] = []
        self.source_calls: list[tuple[str, str, Actor]] = []

    def get_original_page_locator(self, *, matter_id: str, evidence_page_id: str, actor: Actor) -> OriginalPageLocator:
        self.page_calls.append((matter_id, evidence_page_id, actor))
        return self.page_locator

    def get_web_uploaded_original_source_locator(
        self, *, matter_id: str, evidence_file_id: str, actor: Actor
    ) -> WebEvidenceOriginalSourceLocator:
        self.source_calls.append((matter_id, evidence_file_id, actor))
        return self.source_locator


class FakeObjectStore:
    def __init__(self, content: bytes, *, return_other_path: bool = False) -> None:
        self.content = content
        self.return_other_path = return_other_path
        self.calls: list[tuple[StoredWebEvidenceOriginal, Path]] = []

    def materialize_verified_pdf(self, stored: StoredWebEvidenceOriginal, *, destination: str | Path) -> Path:
        path = Path(destination)
        self.calls.append((stored, path))
        path.write_bytes(self.content)
        path.chmod(0o600)
        if self.return_other_path:
            other = path.parent / "other.pdf"
            other.write_bytes(self.content)
            other.chmod(0o600)
            return other
        return path


class WebPdfPagePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="web-pdf-page-preview-test-")
        self.base = Path(self.temporary.name).resolve()
        self.worker_root = self.base / "worker"
        self.worker_root.mkdir(mode=0o700)
        self.worker_root.chmod(0o700)
        self.renderer = self.base / "pdftoppm"
        self.renderer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.renderer.chmod(0o700)
        self.source_pdf = self.base / "registered.pdf"
        self.source_bytes = _two_page_pdf(self.source_pdf)
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.evidence_file_id = str(uuid4())
        self.evidence_page_id = str(uuid4())
        self.human = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.source_hash = sha256(self.source_bytes).hexdigest()
        self.stored = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{self.firm_id}/{self.matter_id}/"
                f"{self.source_hash[:2]}/{self.source_hash}/{uuid4()}.pdf"
            ),
            content_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            object_version_id="opaque-version-1",
        )
        self.page_locator = OriginalPageLocator(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            evidence_page_id=self.evidence_page_id,
            evidence_file_id=self.evidence_file_id,
            original_label="微信记录.pdf",
            original_file_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            media_type="application/pdf",
            page_count=2,
            page_number=2,
        )
        self.source_locator = WebEvidenceOriginalSourceLocator(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            evidence_file_id=self.evidence_file_id,
            original_file_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            page_count=2,
            source_reference_hash=sha256(self.stored.object_key.encode("ascii")).hexdigest(),
            object_key=self.stored.object_key,
            object_version_id=self.stored.object_version_id,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(
        self,
        *,
        runner,
        page_locator: OriginalPageLocator | None = None,
        source_locator: WebEvidenceOriginalSourceLocator | None = None,
        object_store: FakeObjectStore | None = None,
        system_worker_for_firm=None,
        policy: WebPdfPagePreviewPolicy | None = None,
    ) -> tuple[WebPdfPagePreviewService, FakeEvidenceStore, FakeObjectStore]:
        evidence_store = FakeEvidenceStore(
            page_locator=page_locator or self.page_locator,
            source_locator=source_locator or self.source_locator,
        )
        store = object_store or FakeObjectStore(self.source_bytes)
        configured_policy = policy or WebPdfPagePreviewPolicy(
            worker_root=self.worker_root,
            pdftoppm_executable=self.renderer,
            max_png_dimension=2_000,
            max_png_pixels=2_000_000,
        )
        service = WebPdfPagePreviewService(
            evidence_store=evidence_store,
            object_store=store,
            system_worker_for_firm=system_worker_for_firm or (lambda firm_id: self.worker),
            policy=configured_policy,
            runner=runner,
        )
        return service, evidence_store, store

    @staticmethod
    def _valid_renderer(commands: list[tuple[list[str], dict]]):
        def runner(command, **kwargs):
            commands.append((command, kwargs))
            Image.new("RGB", (300, 400), color="white").save(Path(command[-1]).with_suffix(".png"))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        return runner

    def test_renders_exactly_one_authorized_page_to_browser_safe_png_and_erases_worker_files(self) -> None:
        commands: list[tuple[list[str], dict]] = []
        service, evidence_store, object_store = self._service(runner=self._valid_renderer(commands))

        preview = service.render_page(
            actor=self.human,
            matter_id=self.matter_id,
            evidence_page_id=self.evidence_page_id,
        )

        self.assertEqual(preview.evidence_page_id, self.evidence_page_id)
        self.assertEqual(preview.media_type, "image/png")
        self.assertEqual(preview.width, 300)
        self.assertEqual(preview.height, 400)
        self.assertEqual(preview.content_sha256, sha256(preview.png_content).hexdigest())
        self.assertTrue(preview.png_content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn(self.stored.object_key, repr(preview))
        self.assertNotIn(str(self.worker_root), repr(preview))
        self.assertEqual(evidence_store.page_calls, [(self.matter_id, self.evidence_page_id, self.human)])
        self.assertEqual(evidence_store.source_calls, [(self.matter_id, self.evidence_file_id, self.worker)])
        self.assertEqual(len(object_store.calls), 1)
        self.assertEqual(len(commands), 1)
        command, kwargs = commands[0]
        self.assertEqual(command[0], str(self.renderer))
        self.assertEqual(command[1:6], ["-f", "2", "-l", "2", "-singlefile"])
        self.assertIn("-png", command)
        self.assertNotIn(self.stored.object_key, command)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_a_source_locator_for_a_different_web_uploaded_original_before_materialization(self) -> None:
        other_file_id = str(uuid4())
        wrong_source = WebEvidenceOriginalSourceLocator(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            evidence_file_id=other_file_id,
            original_file_sha256=self.source_hash,
            byte_size=len(self.source_bytes),
            page_count=2,
            source_reference_hash=sha256(self.stored.object_key.encode("ascii")).hexdigest(),
            object_key=self.stored.object_key,
            object_version_id=self.stored.object_version_id,
        )
        service, _, object_store = self._service(
            runner=self._valid_renderer([]),
            source_locator=wrong_source,
        )

        with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "unavailable"):
            service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
        self.assertEqual(object_store.calls, [])
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_a_non_system_or_cross_firm_worker_before_private_object_lookup(self) -> None:
        invalid_workers = (
            Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER})),
            Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER})),
        )
        for bad_worker in invalid_workers:
            with self.subTest(worker=bad_worker):
                service, evidence_store, object_store = self._service(
                    runner=self._valid_renderer([]),
                    system_worker_for_firm=lambda firm_id, bad_worker=bad_worker: bad_worker,
                )

                with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "system worker"):
                    service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
                self.assertEqual(evidence_store.source_calls, [])
                self.assertEqual(object_store.calls, [])

    def test_rejects_invalid_or_oversized_renderer_output_and_removes_private_files(self) -> None:
        def oversized_renderer(command, **kwargs):
            # The pre-Pillow IHDR check catches this before a decoder can try
            # to allocate an excessive bitmap.
            content = (
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">II", 2_001, 400)
                + b"\x08\x02\x00\x00\x00"
            )
            Path(command[-1]).with_suffix(".png").write_bytes(content)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        service, _, _ = self._service(runner=oversized_renderer)
        with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "dimensions"):
            service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_uses_pillow_to_reject_a_corrupt_png_even_when_its_header_is_within_limits(self) -> None:
        def corrupt_renderer(command, **kwargs):
            content = (
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">II", 300, 400)
                + b"\x08\x02\x00\x00\x00"
            )
            Path(command[-1]).with_suffix(".png").write_bytes(content)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        service, _, _ = self._service(runner=corrupt_renderer)
        with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "invalid evidence page"):
            service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rehashes_the_materialized_original_after_rendering_and_fails_closed_on_mutation(self) -> None:
        def mutating_renderer(command, **kwargs):
            Path(command[-2]).write_bytes(b"changed while renderer was running")
            Image.new("RGB", (300, 400), color="white").save(Path(command[-1]).with_suffix(".png"))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        service, _, _ = self._service(runner=mutating_renderer)
        with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "materialization"):
            service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_rejects_an_object_store_that_returns_a_different_private_path(self) -> None:
        object_store = FakeObjectStore(self.source_bytes, return_other_path=True)
        service, _, _ = self._service(runner=self._valid_renderer([]), object_store=object_store)

        with self.assertRaisesRegex(WebPdfPagePreviewBlocked, "materialization"):
            service.render_page(actor=self.human, matter_id=self.matter_id, evidence_page_id=self.evidence_page_id)
        self.assertEqual(list(self.worker_root.iterdir()), [])

    def test_policy_requires_private_worker_root_and_absolute_nonsymlink_renderer(self) -> None:
        public_root = self.base / "public-worker"
        public_root.mkdir(mode=0o755)
        public_root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            WebPdfPagePreviewPolicy(worker_root=public_root, pdftoppm_executable=self.renderer)
        with self.assertRaisesRegex(ValueError, "absolute"):
            WebPdfPagePreviewPolicy(worker_root=self.worker_root, pdftoppm_executable="pdftoppm")


if __name__ == "__main__":
    unittest.main()
