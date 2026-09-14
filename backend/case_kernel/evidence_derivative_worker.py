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
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable
from uuid import UUID

from PIL import Image, ImageChops
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
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
class VerifiedMaterializedPdfSource:
    """A server-worker-only reference to one quarantined PDF materialization.

    ``source_path`` is deliberately an input-only worker concern.  It is never
    persisted into derivative lineage or returned by the materialized build
    result.  ``source_reference_hash`` is the caller-provided hash of the
    storage-layer reference that is safe to record as provenance instead.
    """

    evidence_file_id: str
    source_path: str | Path
    expected_sha256: str
    expected_page_count: int
    source_reference_hash: str


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
class MaterializedDerivativeArtifact:
    """Derivative metadata safe to return from a server-worker materialization."""

    artifact_type: str
    file_name: str
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
class MaterializedDerivativeBuildResult:
    """A path-free result for the Web worker's private output directory.

    The worker that supplied ``output_directory`` can resolve the fixed output
    names itself.  API and persistence layers receive only file names, hashes,
    and page counts, so neither a quarantined source path nor a worker staging
    path can cross the worker boundary.
    """

    manifest_id: str
    manifest_content_hash: str
    related_pages: MaterializedDerivativeArtifact
    annotated_pages: MaterializedDerivativeArtifact
    lineage_file_name: str
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


@dataclass(frozen=True)
class _ResolvedPdfSource:
    source_path: Path
    reader: PdfReader
    source_hash: str
    expected_page_count: int
    lineage_reference: dict[str, str]
    revalidate_path: Callable[[], Path]


@dataclass(frozen=True)
class _DerivativeBuildFiles:
    related_pages: DerivativeArtifact
    annotated_pages: DerivativeArtifact
    lineage_path: Path
    lineage_sha256: str


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

    bindings = _index_source_bindings(manifest, source_bindings)
    resolved_sources: dict[str, _ResolvedPdfSource] = {}
    for file_id, binding in bindings.items():
        _validate_uuid("evidence_file_id", file_id)
        _validate_source_registration(binding.expected_sha256, binding.expected_page_count)
        source_path = _resolve_source_pdf(root, binding.relative_path)
        source_hash, reader = _read_verified_source_pdf(
            source_path,
            expected_sha256=binding.expected_sha256,
            expected_page_count=binding.expected_page_count,
        )
        relative_path_hash = sha256(str(source_path.relative_to(root)).encode("utf-8")).hexdigest()
        resolved_sources[file_id] = _ResolvedPdfSource(
            source_path=source_path,
            reader=reader,
            source_hash=source_hash,
            expected_page_count=binding.expected_page_count,
            lineage_reference={"source_relative_path_sha256": relative_path_hash},
            revalidate_path=lambda binding=binding: _resolve_source_pdf(root, binding.relative_path),
        )

    build = _build_derivative_files(manifest, resolved_sources, output)
    return DerivativeBuildResult(
        manifest_id=manifest.manifest_id,
        manifest_content_hash=manifest.content_hash,
        related_pages=build.related_pages,
        annotated_pages=build.annotated_pages,
        lineage_path=build.lineage_path,
        lineage_sha256=build.lineage_sha256,
    )


def build_evidence_derivatives_from_materialized_sources(
    manifest: LockedDerivativeManifest,
    sources: tuple[VerifiedMaterializedPdfSource, ...],
    *,
    output_directory: str | Path,
) -> MaterializedDerivativeBuildResult:
    """Build derivatives from server-owned quarantined PDF materializations.

    This entry point is only for a trusted worker after upload quarantine and
    object-storage materialization.  It refuses relative paths, symlinks, and
    non-private output directories.  Neither source paths nor output paths are
    included in the returned result or in the evidence lineage JSON.
    """
    _validate_manifest(manifest)
    output = _prepare_private_output_directory(output_directory)
    indexed_sources = _index_materialized_sources(manifest, sources)
    resolved_sources: dict[str, _ResolvedPdfSource] = {}
    for file_id, source in indexed_sources.items():
        _validate_uuid("evidence_file_id", file_id)
        _validate_source_registration(source.expected_sha256, source.expected_page_count)
        _validate_sha256("source storage reference", source.source_reference_hash)
        source_path = _resolve_materialized_source_pdf(source.source_path)
        source_hash, reader = _read_verified_source_pdf(
            source_path,
            expected_sha256=source.expected_sha256,
            expected_page_count=source.expected_page_count,
        )
        resolved_sources[file_id] = _ResolvedPdfSource(
            source_path=source_path,
            reader=reader,
            source_hash=source_hash,
            expected_page_count=source.expected_page_count,
            lineage_reference={"source_storage_reference_sha256": source.source_reference_hash},
            revalidate_path=lambda source=source: _resolve_materialized_source_pdf(source.source_path),
        )

    build = _build_derivative_files(manifest, resolved_sources, output)
    return MaterializedDerivativeBuildResult(
        manifest_id=manifest.manifest_id,
        manifest_content_hash=manifest.content_hash,
        related_pages=MaterializedDerivativeArtifact(
            artifact_type=build.related_pages.artifact_type,
            file_name=build.related_pages.path.name,
            sha256=build.related_pages.sha256,
            page_count=build.related_pages.page_count,
        ),
        annotated_pages=MaterializedDerivativeArtifact(
            artifact_type=build.annotated_pages.artifact_type,
            file_name=build.annotated_pages.path.name,
            sha256=build.annotated_pages.sha256,
            page_count=build.annotated_pages.page_count,
        ),
        lineage_file_name=build.lineage_path.name,
        lineage_sha256=build.lineage_sha256,
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


def _index_source_bindings(
    manifest: LockedDerivativeManifest,
    source_bindings: tuple[SourcePdfBinding, ...],
) -> dict[str, SourcePdfBinding]:
    bindings: dict[str, SourcePdfBinding] = {}
    for binding in source_bindings:
        if not isinstance(binding, SourcePdfBinding):
            raise EvidenceDerivativeBlocked("source bindings must use registered PDF binding records")
        if binding.evidence_file_id in bindings:
            raise EvidenceDerivativeBlocked("each evidence file can have only one source binding")
        bindings[binding.evidence_file_id] = binding
    _validate_source_ids(manifest, bindings)
    return bindings


def _index_materialized_sources(
    manifest: LockedDerivativeManifest,
    sources: tuple[VerifiedMaterializedPdfSource, ...],
) -> dict[str, VerifiedMaterializedPdfSource]:
    indexed: dict[str, VerifiedMaterializedPdfSource] = {}
    for source in sources:
        if not isinstance(source, VerifiedMaterializedPdfSource):
            raise EvidenceDerivativeBlocked("materialized sources must use verified worker records")
        if source.evidence_file_id in indexed:
            raise EvidenceDerivativeBlocked("each evidence file can have only one materialized source")
        indexed[source.evidence_file_id] = source
    _validate_source_ids(manifest, indexed)
    return indexed


def _validate_source_ids(
    manifest: LockedDerivativeManifest,
    sources: dict[str, Any],
) -> None:
    required_file_ids = {page.evidence_file_id for page in manifest.pages}
    if set(sources) != required_file_ids:
        raise EvidenceDerivativeBlocked("source bindings must exactly match the files used by the locked Manifest")


def _validate_source_registration(expected_sha256: str, expected_page_count: int) -> None:
    _validate_sha256("source PDF", expected_sha256)
    if isinstance(expected_page_count, bool) or not isinstance(expected_page_count, int) or expected_page_count < 1:
        raise EvidenceDerivativeBlocked("source PDF page count must be positive")


def _read_verified_source_pdf(
    source_path: Path,
    *,
    expected_sha256: str,
    expected_page_count: int,
) -> tuple[str, PdfReader]:
    try:
        source_hash = _file_sha256(source_path)
    except OSError as error:
        raise EvidenceDerivativeBlocked("registered source PDF is no longer readable") from error
    if source_hash != expected_sha256:
        raise EvidenceDerivativeBlocked("source PDF hash differs from the registered original")
    try:
        reader = PdfReader(str(source_path))
        if reader.is_encrypted:
            raise EvidenceDerivativeBlocked("encrypted source PDFs require a separate approved unlock workflow")
        if len(reader.pages) != expected_page_count:
            raise EvidenceDerivativeBlocked("source PDF page count differs from the registered original")
    except EvidenceDerivativeBlocked:
        raise
    except Exception as error:
        raise EvidenceDerivativeBlocked("registered source PDF cannot be read safely") from error
    return source_hash, reader


def _build_derivative_files(
    manifest: LockedDerivativeManifest,
    resolved_sources: dict[str, _ResolvedPdfSource],
    output: Path,
) -> _DerivativeBuildFiles:
    related_writer = PdfWriter()
    annotated_writer = PdfWriter()
    lineage_pages: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="evidence-derivative-overlay-", dir=output) as temporary:
        temporary_path = Path(temporary)
        for entry in manifest.pages:
            source = resolved_sources[entry.evidence_file_id]
            if entry.source_page_number > len(source.reader.pages):
                raise EvidenceDerivativeBlocked("Manifest page number exceeds the registered source PDF")
            source_page = source.reader.pages[entry.source_page_number - 1]
            _validate_page_geometry(source_page)
            related_writer.add_page(_sanitized_page_copy(source_page))
            annotated_writer.add_page(_sanitized_page_copy(source_page))
            target_page = annotated_writer.pages[-1]
            overlay_path = temporary_path / f"overlay-{entry.derivative_sequence}.pdf"
            _write_annotation_overlay(
                overlay_path,
                width=float(target_page.mediabox.width),
                height=float(target_page.mediabox.height),
                annotations=entry.annotations,
            )
            target_page.merge_page(PdfReader(str(overlay_path)).pages[0])
            lineage_page = {
                "derivative_sequence": entry.derivative_sequence,
                "evidence_page_id": entry.evidence_page_id,
                "evidence_file_id": entry.evidence_file_id,
                "source_file_sha256": source.source_hash,
                "source_page_number": entry.source_page_number,
                "annotations": [asdict(annotation) for annotation in entry.annotations],
            }
            lineage_page.update(_safe_lineage_reference(source.lineage_reference))
            lineage_pages.append(lineage_page)

        related_path = output / "related-pages.pdf"
        annotated_path = output / "related-pages-red-box.pdf"
        _write_pdf_atomically(related_writer, related_path)
        _write_pdf_atomically(annotated_writer, annotated_path)

    _assert_sources_unchanged(resolved_sources)

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
    return _DerivativeBuildFiles(
        related_pages=related_artifact,
        annotated_pages=annotated_artifact,
        lineage_path=lineage_path,
        lineage_sha256=_file_sha256(lineage_path),
    )


def _safe_lineage_reference(reference: dict[str, str]) -> dict[str, str]:
    allowed_keys = {
        "source_relative_path_sha256",
        "source_storage_reference_sha256",
    }
    if len(reference) != 1 or not set(reference).issubset(allowed_keys):
        raise EvidenceDerivativeBlocked("source lineage reference is not safe for persistence")
    key, value = next(iter(reference.items()))
    _validate_sha256("source lineage reference", value)
    return {key: value}


def _assert_sources_unchanged(resolved_sources: dict[str, _ResolvedPdfSource]) -> None:
    for source in resolved_sources.values():
        try:
            revalidated_path = source.revalidate_path()
            if revalidated_path != source.source_path:
                raise EvidenceDerivativeBlocked("a source PDF changed while derivatives were being built")
            if _file_sha256(revalidated_path) != source.source_hash:
                raise EvidenceDerivativeBlocked("a source PDF changed while derivatives were being built")
            reader = PdfReader(str(revalidated_path))
            if reader.is_encrypted or len(reader.pages) != source.expected_page_count:
                raise EvidenceDerivativeBlocked("a source PDF changed while derivatives were being built")
        except EvidenceDerivativeBlocked:
            raise
        except Exception as error:
            raise EvidenceDerivativeBlocked("a source PDF changed while derivatives were being built") from error


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


def _resolve_materialized_source_pdf(source_path: str | Path) -> Path:
    """Validate a worker-materialized source without normalizing away symlinks."""
    try:
        candidate = Path(source_path)
    except TypeError as error:
        raise EvidenceDerivativeBlocked("materialized source path is required") from error
    if not candidate.is_absolute():
        raise EvidenceDerivativeBlocked("materialized source path must be absolute")
    _assert_no_symbolic_link_components(
        candidate,
        label="materialized source path",
        allow_missing_tail=False,
    )
    try:
        metadata = os.lstat(candidate)
    except OSError as error:
        raise EvidenceDerivativeBlocked("materialized source PDF is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceDerivativeBlocked("materialized source PDF must be a regular file")
    if candidate.suffix.lower() != ".pdf":
        raise EvidenceDerivativeBlocked("this derivative worker accepts registered PDF originals only")
    return candidate


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


def _sanitized_page_copy(page: Any) -> Any:
    sanitized = deepcopy(page)
    for key in ("/AA", "/Annots", "/B", "/Dur", "/PresSteps", "/Trans"):
        sanitized.pop(NameObject(key), None)
    return sanitized


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
    forbidden_catalog_keys = {"/AA", "/AcroForm", "/Collection", "/Names", "/OpenAction"}
    if forbidden_catalog_keys.intersection(str(key) for key in reader.root_object.keys()):
        raise EvidenceDerivativeBlocked("generated derivative contains an active document catalog entry")
    forbidden_page_keys = {"/AA", "/Annots", "/B", "/Dur", "/PresSteps", "/Trans"}
    for page in reader.pages:
        if forbidden_page_keys.intersection(str(key) for key in page.keys()):
            raise EvidenceDerivativeBlocked("generated derivative contains an active page entry")
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


def _prepare_private_output_directory(output_directory: str | Path) -> Path:
    """Create or validate an empty 0700 server-worker output directory.

    The materialized-source entry point intentionally does not call
    ``Path.resolve()``: resolving first would hide a symlink in a worker path.
    The lexical absolute path is checked component by component before use.
    """
    try:
        output = Path(output_directory)
    except TypeError as error:
        raise EvidenceDerivativeBlocked("private derivative output directory is required") from error
    if not output.is_absolute():
        raise EvidenceDerivativeBlocked("private derivative output directory must be absolute")
    _assert_no_symbolic_link_components(
        output,
        label="private derivative output directory",
        allow_missing_tail=True,
    )
    try:
        metadata = os.lstat(output)
    except FileNotFoundError:
        try:
            output.mkdir(parents=True, mode=0o700)
            output.chmod(0o700)
        except OSError as error:
            raise EvidenceDerivativeBlocked("private derivative output directory cannot be created") from error
    except OSError as error:
        raise EvidenceDerivativeBlocked("private derivative output directory is unavailable") from error
    _assert_no_symbolic_link_components(
        output,
        label="private derivative output directory",
        allow_missing_tail=False,
    )
    try:
        metadata = os.lstat(output)
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvidenceDerivativeBlocked("private derivative output must be a directory")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise EvidenceDerivativeBlocked("private derivative output directory must not grant group or public access")
        if any(output.iterdir()):
            raise EvidenceDerivativeBlocked("private derivative output directory must be empty")
    except EvidenceDerivativeBlocked:
        raise
    except OSError as error:
        raise EvidenceDerivativeBlocked("private derivative output directory cannot be inspected") from error
    return output


def _assert_no_symbolic_link_components(
    path: Path,
    *,
    label: str,
    allow_missing_tail: bool,
) -> None:
    if not path.is_absolute():
        raise EvidenceDerivativeBlocked(f"{label} must be absolute")
    anchor_parts = Path(path.anchor).parts
    components = path.parts[len(anchor_parts) :]
    if any(component in {"", ".", ".."} for component in components):
        raise EvidenceDerivativeBlocked(f"{label} cannot use parent-directory traversal")
    current = Path(path.anchor)
    for component in components:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return
            raise EvidenceDerivativeBlocked(f"{label} is unavailable") from None
        except OSError as error:
            raise EvidenceDerivativeBlocked(f"{label} cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise EvidenceDerivativeBlocked(f"{label} cannot traverse a symbolic link")


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise EvidenceDerivativeBlocked(f"{label} must be a UUID") from error


def _validate_sha256(label: str, value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise EvidenceDerivativeBlocked(f"{label} must be a lowercase SHA-256 value")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
