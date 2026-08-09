from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from case_kernel.evidence_derivative_worker import (
    ApprovedPageAnnotation,
    EvidenceDerivativeBlocked,
    IncludedManifestPage,
    LockedDerivativeManifest,
    SourcePdfBinding,
    build_evidence_derivatives,
    verify_evidence_derivatives,
)
from case_kernel.local_case_folder import root_fingerprint


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def create_source_pdf(path: Path, *, pages: int = 3) -> Path:
    document = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    for page_number in range(1, pages + 1):
        document.setFont("Helvetica-Bold", 18)
        document.drawString(72, 760, "SYNTHETIC EVIDENCE - NOT A CLIENT FILE")
        document.setFont("Helvetica", 12)
        document.drawString(72, 690, f"Synthetic source page {page_number}")
        document.drawString(72, 640, f"Synthetic transaction reference {page_number:04d}")
        document.showPage()
    document.save()
    return path


class EvidenceDerivativeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="evidence-derivative-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case-folder"
        self.case_root.mkdir()
        self.source = create_source_pdf(self.case_root / "synthetic-source.pdf")
        self.source_hash = file_hash(self.source)
        self.file_id = str(uuid4())
        self.first_page_id = str(uuid4())
        self.third_page_id = str(uuid4())
        self.annotation_id = str(uuid4())
        self.manifest = LockedDerivativeManifest(
            manifest_id=str(uuid4()),
            content_hash="a" * 64,
            status="LOCKED",
            pages=(
                IncludedManifestPage(
                    evidence_page_id=self.first_page_id,
                    evidence_file_id=self.file_id,
                    source_page_number=1,
                    derivative_sequence=1,
                    annotations=(
                        ApprovedPageAnnotation(
                            annotation_id=self.annotation_id,
                            x0=0.08,
                            y0=0.14,
                            x1=0.82,
                            y1=0.28,
                            label="[合成] 相关交易行",
                        ),
                    ),
                ),
                IncludedManifestPage(
                    evidence_page_id=self.third_page_id,
                    evidence_file_id=self.file_id,
                    source_page_number=3,
                    derivative_sequence=2,
                ),
            ),
        )
        self.binding = SourcePdfBinding(
            evidence_file_id=self.file_id,
            relative_path="synthetic-source.pdf",
            expected_sha256=self.source_hash,
            expected_page_count=3,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_locked_manifest_builds_minimal_related_and_red_box_pdfs_without_changing_original(self) -> None:
        output = self.root / "managed-output"
        result = build_evidence_derivatives(
            self.manifest,
            (self.binding,),
            case_root=self.case_root,
            confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
            output_directory=output,
        )
        verification = verify_evidence_derivatives(result, self.manifest)

        self.assertEqual(file_hash(self.source), self.source_hash)
        self.assertEqual(len(PdfReader(str(result.related_pages.path)).pages), 2)
        self.assertEqual(len(PdfReader(str(result.annotated_pages.path)).pages), 2)
        self.assertTrue(verification.verified)
        self.assertEqual(verification.annotated_page_count, 1)
        self.assertGreater(verification.changed_red_pixel_count, 40)
        lineage = json.loads(result.lineage_path.read_text(encoding="utf-8"))
        self.assertEqual([page["source_page_number"] for page in lineage["pages"]], [1, 3])
        self.assertEqual(lineage["pages"][0]["annotations"][0]["annotation_id"], self.annotation_id)
        self.assertNotIn(str(self.case_root), result.lineage_path.read_text(encoding="utf-8"))
        self.assertEqual(result.related_pages.path.name, "related-pages.pdf")
        self.assertEqual(result.annotated_pages.path.name, "related-pages-red-box.pdf")

        repeated = build_evidence_derivatives(
            self.manifest,
            (self.binding,),
            case_root=self.case_root,
            confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
            output_directory=self.root / "managed-output-repeat",
        )
        self.assertEqual(repeated.related_pages.sha256, result.related_pages.sha256)
        self.assertEqual(repeated.annotated_pages.sha256, result.annotated_pages.sha256)
        self.assertEqual(repeated.lineage_sha256, result.lineage_sha256)

    def test_source_page_actions_and_annotations_are_not_copied_to_derivatives(self) -> None:
        reader = PdfReader(str(self.source))
        writer = PdfWriter()
        for source_page in reader.pages:
            writer.add_page(source_page)
        javascript = DictionaryObject(
            {
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject("app.alert('synthetic')"),
            }
        )
        writer.pages[0][NameObject("/AA")] = DictionaryObject({NameObject("/O"): javascript})
        writer.pages[0][NameObject("/Annots")] = ArrayObject(
            [
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Link"),
                        NameObject("/A"): javascript,
                    }
                )
            ]
        )
        with self.source.open("wb") as stream:
            writer.write(stream)
        binding = SourcePdfBinding(
            evidence_file_id=self.file_id,
            relative_path="synthetic-source.pdf",
            expected_sha256=file_hash(self.source),
            expected_page_count=3,
        )
        result = build_evidence_derivatives(
            self.manifest,
            (binding,),
            case_root=self.case_root,
            confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
            output_directory=self.root / "sanitized-output",
        )
        for derivative_path in (result.related_pages.path, result.annotated_pages.path):
            derivative = PdfReader(str(derivative_path))
            self.assertNotIn("/Names", derivative.root_object)
            self.assertNotIn("/OpenAction", derivative.root_object)
            self.assertNotIn("/AA", derivative.pages[0])
            self.assertNotIn("/Annots", derivative.pages[0])

    def test_unlocked_or_noncontiguous_manifest_is_rejected_before_output(self) -> None:
        unlocked = LockedDerivativeManifest(
            manifest_id=self.manifest.manifest_id,
            content_hash=self.manifest.content_hash,
            status="INVALIDATED",
            pages=self.manifest.pages,
        )
        with self.assertRaisesRegex(EvidenceDerivativeBlocked, "only a current locked"):
            build_evidence_derivatives(
                unlocked,
                (self.binding,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                output_directory=self.root / "unlocked-output",
            )
        noncontiguous = LockedDerivativeManifest(
            manifest_id=self.manifest.manifest_id,
            content_hash=self.manifest.content_hash,
            status="LOCKED",
            pages=(
                IncludedManifestPage(
                    evidence_page_id=self.first_page_id,
                    evidence_file_id=self.file_id,
                    source_page_number=1,
                    derivative_sequence=2,
                ),
            ),
        )
        with self.assertRaisesRegex(EvidenceDerivativeBlocked, "contiguous"):
            build_evidence_derivatives(
                noncontiguous,
                (self.binding,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                output_directory=self.root / "sequence-output",
            )

    def test_hash_mismatch_and_writing_inside_original_folder_are_blocked(self) -> None:
        wrong_hash = SourcePdfBinding(
            evidence_file_id=self.file_id,
            relative_path="synthetic-source.pdf",
            expected_sha256="b" * 64,
            expected_page_count=3,
        )
        with self.assertRaisesRegex(EvidenceDerivativeBlocked, "hash differs"):
            build_evidence_derivatives(
                self.manifest,
                (wrong_hash,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                output_directory=self.root / "wrong-hash-output",
            )
        with self.assertRaisesRegex(EvidenceDerivativeBlocked, "must not be written inside"):
            build_evidence_derivatives(
                self.manifest,
                (self.binding,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                output_directory=self.case_root / "derived",
            )

    def test_rotated_page_is_blocked_until_coordinate_transform_is_explicit(self) -> None:
        rotated_path = self.case_root / "rotated.pdf"
        reader = PdfReader(str(self.source))
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        writer.pages[0].rotate(90)
        with rotated_path.open("wb") as stream:
            writer.write(stream)
        rotated_file_id = str(uuid4())
        rotated_manifest = LockedDerivativeManifest(
            manifest_id=str(uuid4()),
            content_hash="c" * 64,
            status="LOCKED",
            pages=(
                IncludedManifestPage(
                    evidence_page_id=str(uuid4()),
                    evidence_file_id=rotated_file_id,
                    source_page_number=1,
                    derivative_sequence=1,
                    annotations=(
                        ApprovedPageAnnotation(
                            annotation_id=str(uuid4()),
                            x0=0.1,
                            y0=0.1,
                            x1=0.5,
                            y1=0.3,
                            label="[合成] 坐标",
                        ),
                    ),
                ),
            ),
        )
        binding = SourcePdfBinding(
            evidence_file_id=rotated_file_id,
            relative_path="rotated.pdf",
            expected_sha256=file_hash(rotated_path),
            expected_page_count=1,
        )
        with self.assertRaisesRegex(EvidenceDerivativeBlocked, "rotated PDF pages"):
            build_evidence_derivatives(
                rotated_manifest,
                (binding,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                output_directory=self.root / "rotated-output",
            )


if __name__ == "__main__":
    unittest.main()
