"""Deterministic PDF derivatives built only from a locked evidence Manifest.

The worker does no relevance classification. It receives already-approved page
selection and annotation coordinates, verifies every source PDF and source page,
then creates two minimal derivatives: selected source pages and the same pages
with approved red-box overlays. It never writes inside the lawyer-selected case
folder and never edits a source PDF.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from PIL import Image, ImageChops
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from .local_case_folder import root_fingerprint


class EvidenceDerivativeBlocked(ValueError):
    """A source, Manifest, coordinate, or output invariant is unsafe."""


@dataclass(frozen=True)
class SourcePdfBinding:
    evidence_file_id: str
    relative_path: str
    expected_sha256: str
    expected_page_count: int


@dataclass(frozen=True)
class ApprovedPageAnnotation:
    annotation_id: str
    x0: float
    y0: float
    x1: float
    y1: float
    label: str


@dataclass(frozen=True)
class IncludedManifestPage:
    evidence_page_id: str
    evidence_file_id: str
    source_page_number: int
    derivative_sequence: int
    annotations: tuple[ApprovedPageAnnotation, ...] = ()


@dataclass(frozen=True)
class LockedDerivativeManifest:
    manifest_id: str
    content_hash: str
    status: str
    pages: tuple[IncludedManifestPage, ...]


@dataclass(frozen=True)
class DerivativeArtifact:
    artifact_type: str
    path: Path
    sha256: str
    page_count: int


@dataclass(frozen=True)
class DerivativeBuildResult:
    manifest_id: str
    manifest_content_hash: str
    related_pages: DerivativeArtifact
    annotated_pages: DerivativeArtifact
    lineage_path: Path
    lineage_sha256: str


@dataclass(frozen=True)
class DerivativeVerification:
    verified: bool
    rendered_dpi: int
    page_count: int
    annotated_page_count: int
    changed_red_pixel_count: int
    related_pages_sha256: str
    annotated_pages_sha256: str


def build_evidence_derivatives(
    manifest: LockedDerivativeManifest,
    source_bindings: tuple[SourcePdfBinding, ...],
    *,
    case_root: str | Path,
    confirmed_case_root_fingerprint: str,
    output_directory: str | Path,
) -> DerivativeBuildResult:
    """Build minimal PDF derivatives without modifying or writing beside originals."""
    _validate_manifest(manifest)
    root = Path(case_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise EvidenceDerivativeBlocked("case root must be an existing directory")
    if root_fingerprint(root) != confirmed_case_root_fingerprint:
        raise EvidenceDerivativeBlocked("case-folder confirmation no longer matches the selected root")
    output = Path(output_directory).expanduser().resolve()
    if output == root or output.is_relative_to(root):
        raise EvidenceDerivativeBlocked("derivative output must not be written inside the original case folder")
    _prepare_empty_output(output)

    bindings = {binding.evidence_file_id: binding for binding in source_bindings}
    if len(bindings) != len(source_bindings):
        raise EvidenceDerivativeBlocked("each evidence file can have only one source binding")
    required_file_ids = {page.evidence_file_id for page in manifest.pages}
    if set(bindings) != required_file_ids:
        raise EvidenceDerivativeBlocked("source bindings must exactly match the files used by the locked Manifest")

    resolved_sources: dict[str, tuple[Path, PdfReader, str]] = {}
    source_hashes_before: dict[str, str] = {}
    for file_id, binding in bindings.items():
        _validate_uuid("evidence_file_id", file_id)
        _validate_sha256("source PDF", binding.expected_sha256)
        if binding.expected_page_count < 1:
            raise EvidenceDerivativeBlocked("source PDF page count must be positive")
        source_path = _resolve_source_pdf(root, binding.relative_path)
        source_hash = _file_sha256(source_path)
        if source_hash != binding.expected_sha256:
            raise EvidenceDerivativeBlocked("source PDF hash differs from the registered original")
        reader = PdfReader(str(source_path))
        if reader.is_encrypted:
            raise EvidenceDerivativeBlocked("encrypted source PDFs require a separate approved unlock workflow")
        if len(reader.pages) != binding.expected_page_count:
            raise EvidenceDerivativeBlocked("source PDF page count differs from the registered original")
        resolved_sources[file_id] = (source_path, reader, source_hash)
        source_hashes_before[file_id] = source_hash

    related_writer = PdfWriter()
    annotated_writer = PdfWriter()
    lineage_pages: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="evidence-derivative-overlay-", dir=output) as temporary:
        temporary_path = Path(temporary)
        for entry in manifest.pages:
            source_path, reader, source_hash = resolved_sources[entry.evidence_file_id]
            if entry.source_page_number > len(reader.pages):
                raise EvidenceDerivativeBlocked("Manifest page number exceeds the registered source PDF")
            source_page = reader.pages[entry.source_page_number - 1]
            _validate_page_geometry(source_page)
            related_writer.add_page(deepcopy(source_page))
            annotated_writer.add_page(deepcopy(source_page))
            target_page = annotated_writer.pages[-1]
            overlay_path = temporary_path / f"overlay-{entry.derivative_sequence}.pdf"
            _write_annotation_overlay(
                overlay_path,
                width=float(target_page.mediabox.width),
                height=float(target_page.mediabox.height),
                annotations=entry.annotations,
            )
            target_page.merge_page(PdfReader(str(overlay_path)).pages[0])
            lineage_pages.append(
                {
                    "derivative_sequence": entry.derivative_sequence,
                    "evidence_page_id": entry.evidence_page_id,
                    "evidence_file_id": entry.evidence_file_id,
                    "source_relative_path_sha256": sha256(
                        str(source_path.relative_to(root)).encode("utf-8")
                    ).hexdigest(),
                    "source_file_sha256": source_hash,
                    "source_page_number": entry.source_page_number,
                    "annotations": [asdict(annotation) for annotation in entry.annotations],
                }
            )

        related_path = output / "related-pages.pdf"
        annotated_path = output / "related-pages-red-box.pdf"
        _write_pdf_atomically(related_writer, related_path)
        _write_pdf_atomically(annotated_writer, annotated_path)

    for file_id, (source_path, _, _) in resolved_sources.items():
        if _file_sha256(source_path) != source_hashes_before[file_id]:
            raise EvidenceDerivativeBlocked("a source PDF changed while derivatives were being built")

    related_artifact = _inspect_artifact("RELATED_PAGES_PDF", related_path, len(manifest.pages))
    annotated_artifact = _inspect_artifact(
        "ANNOTATED_RELATED_PAGES_PDF", annotated_path, len(manifest.pages)
    )
    lineage = {
        "schema_version": "evidence-derivative-lineage-v1",
        "manifest_id": manifest.manifest_id,
        "manifest_content_hash": manifest.content_hash,
        "coordinate_space": "normalized top-left origin: 0 <= x,y <= 1",
        "originals_unchanged": True,
        "pages": lineage_pages,
        "artifacts": {
            "related_pages": {
                "file_name": related_artifact.path.name,
                "sha256": related_artifact.sha256,
                "page_count": related_artifact.page_count,
            },
            "annotated_pages": {
                "file_name": annotated_artifact.path.name,
                "sha256": annotated_artifact.sha256,
                "page_count": annotated_artifact.page_count,
            },
        },
    }
    lineage_path = output / "evidence-derivative-lineage.json"
    _write_json_atomically(lineage_path, lineage)
    return DerivativeBuildResult(
        manifest_id=manifest.manifest_id,
        manifest_content_hash=manifest.content_hash,
        related_pages=related_artifact,
        annotated_pages=annotated_artifact,
        lineage_path=lineage_path,
        lineage_sha256=_file_sha256(lineage_path),
    )


def verify_evidence_derivatives(
    result: DerivativeBuildResult,
    manifest: LockedDerivativeManifest,
    *,
    rendered_dpi: int = 144,
) -> DerivativeVerification:
    """Render both PDFs and verify page count, visual parity, and red-box deltas."""
    if result.manifest_id != manifest.manifest_id or result.manifest_content_hash != manifest.content_hash:
        raise EvidenceDerivativeBlocked("derivative result is not bound to the supplied locked Manifest")
    if rendered_dpi < 72 or rendered_dpi > 300:
        raise EvidenceDerivativeBlocked("verification DPI must be between 72 and 300")
    if shutil.which("pdftoppm") is None:
        raise EvidenceDerivativeBlocked("pdftoppm is required for derivative visual verification")
    _inspect_artifact("RELATED_PAGES_PDF", result.related_pages.path, len(manifest.pages))
    _inspect_artifact("ANNOTATED_RELATED_PAGES_PDF", result.annotated_pages.path, len(manifest.pages))
    if _file_sha256(result.related_pages.path) != result.related_pages.sha256:
        raise EvidenceDerivativeBlocked("related-pages PDF changed after build")
    if _file_sha256(result.annotated_pages.path) != result.annotated_pages.sha256:
        raise EvidenceDerivativeBlocked("annotated PDF changed after build")

    changed_red_pixels = 0
    annotated_page_count = 0
    with TemporaryDirectory(prefix="evidence-derivative-verify-") as temporary:
        root = Path(temporary)
        related_pages = _render_pdf(result.related_pages.path, root / "related", rendered_dpi)
        annotated_pages = _render_pdf(result.annotated_pages.path, root / "annotated", rendered_dpi)
        if len(related_pages) != len(manifest.pages) or len(annotated_pages) != len(manifest.pages):
            raise EvidenceDerivativeBlocked("rendered derivative page count does not match the locked Manifest")
        for index, entry in enumerate(manifest.pages):
            with Image.open(related_pages[index]).convert("RGB") as related_image:
                with Image.open(annotated_pages[index]).convert("RGB") as annotated_image:
                    if related_image.size != annotated_image.size:
                        raise EvidenceDerivativeBlocked("related and annotated page geometry differs")
                    difference = ImageChops.difference(related_image, annotated_image)
                    if entry.annotations:
                        annotated_page_count += 1
                        if difference.getbbox() is None:
                            raise EvidenceDerivativeBlocked("an approved annotation produced no visible PDF change")
                        changed_red_pixels += sum(
                            1
                            for red, green, blue in annotated_image.get_flattened_data()
                            if red > 150 and red > green * 1.5 and red > blue * 1.5
                        )
                    elif difference.getbbox() is not None:
                        raise EvidenceDerivativeBlocked("a page without annotations changed in the red-box derivative")
    if annotated_page_count and changed_red_pixels < annotated_page_count * 40:
        raise EvidenceDerivativeBlocked("approved red boxes are not visibly present after rendering")
    return DerivativeVerification(
        verified=True,
        rendered_dpi=rendered_dpi,
        page_count=len(manifest.pages),
        annotated_page_count=annotated_page_count,
        changed_red_pixel_count=changed_red_pixels,
        related_pages_sha256=result.related_pages.sha256,
        annotated_pages_sha256=result.annotated_pages.sha256,
    )


def _validate_manifest(manifest: LockedDerivativeManifest) -> None:
    _validate_uuid("manifest_id", manifest.manifest_id)
    _validate_sha256("manifest content_hash", manifest.content_hash)
    if manifest.status != "LOCKED":
        raise EvidenceDerivativeBlocked("only a current locked evidence Manifest can create derivatives")
    if not manifest.pages:
        raise EvidenceDerivativeBlocked("a PDF derivative requires at least one included page")
    sequences = [page.derivative_sequence for page in manifest.pages]
    if sequences != list(range(1, len(manifest.pages) + 1)):
        raise EvidenceDerivativeBlocked("Manifest derivative sequences must be unique, ordered, and contiguous")
    page_ids: set[str] = set()
    annotation_ids: set[str] = set()
    for page in manifest.pages:
        _validate_uuid("evidence_page_id", page.evidence_page_id)
        _validate_uuid("evidence_file_id", page.evidence_file_id)
        if page.evidence_page_id in page_ids:
            raise EvidenceDerivativeBlocked("a source page cannot enter one derivative twice")
        page_ids.add(page.evidence_page_id)
        if page.source_page_number < 1:
            raise EvidenceDerivativeBlocked("source page numbers must be positive")
        for annotation in page.annotations:
            _validate_uuid("annotation_id", annotation.annotation_id)
            if annotation.annotation_id in annotation_ids:
                raise EvidenceDerivativeBlocked("an approved annotation cannot be applied twice")
            annotation_ids.add(annotation.annotation_id)
            if not annotation.label.strip():
                raise EvidenceDerivativeBlocked("approved annotation label is required")
            if not (0 <= annotation.x0 < annotation.x1 <= 1 and 0 <= annotation.y0 < annotation.y1 <= 1):
                raise EvidenceDerivativeBlocked("annotation coordinates must be ordered and normalized")


def _resolve_source_pdf(root: Path, relative_path: str) -> Path:
    candidate_relative = Path(relative_path)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise EvidenceDerivativeBlocked("source binding must use a safe relative path")
    current = root
    for part in candidate_relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceDerivativeBlocked("source PDF path cannot traverse a symbolic link")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise EvidenceDerivativeBlocked("source PDF must be a regular file inside the selected case folder")
    if resolved.suffix.lower() != ".pdf":
        raise EvidenceDerivativeBlocked("this derivative worker accepts registered PDF originals only")
    return resolved


def _validate_page_geometry(page: Any) -> None:
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    if rotation != 0:
        raise EvidenceDerivativeBlocked("rotated PDF pages require an explicit coordinate transform before red boxes")
    media = tuple(float(value) for value in page.mediabox)
    crop = tuple(float(value) for value in page.cropbox)
    if media != crop:
        raise EvidenceDerivativeBlocked("cropped PDF pages require an explicit coordinate transform before red boxes")
    if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
        raise EvidenceDerivativeBlocked("source PDF page dimensions must be positive")


def _write_annotation_overlay(
    destination: Path,
    *,
    width: float,
    height: float,
    annotations: tuple[ApprovedPageAnnotation, ...],
) -> None:
    overlay = canvas.Canvas(
        str(destination),
        pagesize=(width, height),
        pageCompression=1,
        invariant=1,
    )
    overlay.setStrokeColorRGB(0.72, 0.04, 0.04)
    overlay.setLineWidth(2)
    for annotation in annotations:
        x = annotation.x0 * width
        y = (1 - annotation.y1) * height
        rectangle_width = (annotation.x1 - annotation.x0) * width
        rectangle_height = (annotation.y1 - annotation.y0) * height
        overlay.rect(x, y, rectangle_width, rectangle_height, stroke=1, fill=0)
    overlay.save()


def _write_pdf_atomically(writer: PdfWriter, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    with temporary.open("xb") as stream:
        writer.write(stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    destination.chmod(0o600)


def _write_json_atomically(destination: Path, payload: dict[str, Any]) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    destination.chmod(0o600)


def _inspect_artifact(artifact_type: str, path: Path, expected_pages: int) -> DerivativeArtifact:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise EvidenceDerivativeBlocked("generated derivative unexpectedly became encrypted")
    if len(reader.pages) != expected_pages:
        raise EvidenceDerivativeBlocked("generated derivative page count differs from the locked Manifest")
    return DerivativeArtifact(
        artifact_type=artifact_type,
        path=path,
        sha256=_file_sha256(path),
        page_count=len(reader.pages),
    )


def _render_pdf(path: Path, destination: Path, dpi: int) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=False)
    prefix = destination / "page"
    completed = subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(path), str(prefix)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise EvidenceDerivativeBlocked("PDF rendering failed during derivative verification")
    pages = sorted(destination.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1]))
    if not pages:
        raise EvidenceDerivativeBlocked("PDF rendering produced no pages")
    return pages


def _prepare_empty_output(output: Path) -> None:
    if output.exists():
        if not output.is_dir():
            raise EvidenceDerivativeBlocked("derivative output must be a directory")
        if any(output.iterdir()):
            raise EvidenceDerivativeBlocked("derivative output directory must be empty")
    else:
        output.mkdir(parents=True, mode=0o700)


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise EvidenceDerivativeBlocked(f"{label} must be a UUID") from error


def _validate_sha256(label: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise EvidenceDerivativeBlocked(f"{label} must be a lowercase SHA-256 value")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
