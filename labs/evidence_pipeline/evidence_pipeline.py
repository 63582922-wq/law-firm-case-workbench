"""Deterministic, non-production page processing for the synthetic PDF lab."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from copy import deepcopy

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import red
from reportlab.pdfgen import canvas


RENDER_DPI = 72
SIMILARITY_REVIEW_THRESHOLD = 0.80
ALIAS_PATTERN = re.compile(r"Counterparty Alias:\s*(.*?)\s*\|\s*CNY", re.IGNORECASE)


@dataclass(frozen=True)
class PipelineResult:
    manifest_path: Path
    related_pages_pdf: Path
    annotated_pages_pdf: Path


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise(value: str) -> str:
    return " ".join(value.split())


def _extract_alias(text: str) -> str | None:
    match = ALIAS_PATTERN.search(text)
    return _normalise(match.group(1)) if match else None


def _render_pages(source_pdf: Path, render_directory: Path) -> list[Path]:
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm is required to render the synthetic PDF fixture")
    render_directory.mkdir(parents=True, exist_ok=False)
    prefix = render_directory / "source-page"
    subprocess.run(
        ["pdftoppm", "-r", str(RENDER_DPI), "-png", str(source_pdf), str(prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = sorted(render_directory.glob("source-page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1]))
    if not pages:
        raise RuntimeError("PDF rendering produced no pages")
    return pages


def _draw_red_box_overlay(destination: Path, width: float, height: float) -> Path:
    """Draw a provenance-friendly red box around the alias line on the synthetic fixture."""
    overlay = canvas.Canvas(str(destination), pagesize=(width, height), pageCompression=0)
    overlay.setStrokeColor(red)
    overlay.setLineWidth(3)
    # PDF coordinates use a lower-left origin. The target alias is drawn at y=624.
    overlay.rect(66, 600, width - 132, 42, stroke=1, fill=0)
    overlay.save()
    return destination


def _write_selected_pages(
    reader: PdfReader,
    selected_page_numbers: list[int],
    related_destination: Path,
    annotated_destination: Path,
    output_directory: Path,
) -> dict[str, Any]:
    related_writer = PdfWriter()
    annotated_writer = PdfWriter()
    annotations: list[dict[str, Any]] = []

    for source_page_number in selected_page_numbers:
        source_page = reader.pages[source_page_number - 1]
        related_writer.add_page(deepcopy(source_page))

        overlay_path = output_directory / f"overlay-page-{source_page_number}.pdf"
        _draw_red_box_overlay(overlay_path, float(source_page.mediabox.width), float(source_page.mediabox.height))
        overlay_page = PdfReader(str(overlay_path)).pages[0]
        annotated_writer.add_page(deepcopy(source_page))
        annotated_page = annotated_writer.pages[-1]
        annotated_page.merge_page(overlay_page)
        annotations.append(
            {
                "source_page_number": source_page_number,
                "type": "RED_BOX",
                "coordinate_space": "PDF points, lower-left origin",
                "rectangle": {"x": 66, "y": 600, "width": float(annotated_page.mediabox.width) - 132, "height": 42},
                "reason": "Synthetic target alias line; technical visibility check only.",
            }
        )

    with related_destination.open("wb") as handle:
        related_writer.write(handle)
    with annotated_destination.open("wb") as handle:
        annotated_writer.write(handle)
    return {"selected_source_pages": selected_page_numbers, "annotations": annotations}


def _assert_empty_or_create(directory: Path) -> None:
    if directory.exists():
        if any(directory.iterdir()):
            raise ValueError(f"Output directory must be empty: {directory}")
    else:
        directory.mkdir(parents=True)


def run_pipeline(source_pdf: Path, output_directory: Path, target_alias: str) -> PipelineResult:
    """Create auditable derived files while leaving the source file byte-for-byte unchanged."""
    source_pdf = source_pdf.resolve()
    output_directory = output_directory.resolve()
    if not source_pdf.is_file():
        raise FileNotFoundError(source_pdf)
    if not target_alias.strip():
        raise ValueError("target_alias is required")
    _assert_empty_or_create(output_directory)

    source_hash_before = file_sha256(source_pdf)
    reader = PdfReader(str(source_pdf))
    render_paths = _render_pages(source_pdf, output_directory / "rendered-source-pages")
    if len(reader.pages) != len(render_paths):
        raise RuntimeError("Rendered page count does not match the PDF page count")

    target_normalised = _normalise(target_alias).casefold()
    first_seen_visual_hash: dict[str, int] = {}
    duplicate_members: dict[int, list[int]] = {}
    page_records: list[dict[str, Any]] = []

    for page_number, (page, render_path) in enumerate(zip(reader.pages, render_paths), start=1):
        text = _normalise(page.extract_text() or "")
        alias = _extract_alias(text)
        visual_hash = file_sha256(render_path)
        canonical_page = first_seen_visual_hash.setdefault(visual_hash, page_number)
        duplicate_members.setdefault(canonical_page, []).append(page_number)

        alias_normalised = alias.casefold() if alias else ""
        similarity = SequenceMatcher(None, target_normalised, alias_normalised).ratio() if alias else 0.0
        if alias_normalised == target_normalised:
            disposition = "IN_SCOPE_CANDIDATE"
            reason = "Exact alias match; lawyer must still confirm relevance and payment nature."
        elif similarity >= SIMILARITY_REVIEW_THRESHOLD:
            disposition = "SIMILAR_REVIEW_REQUIRED"
            reason = "Alias is similar but not identical; page is retained for lawyer review and not auto-excluded."
        else:
            disposition = "OUT_OF_SCOPE_CANDIDATE"
            reason = "No exact or near alias match in this synthetic test."

        page_records.append(
            {
                "source_page_number": page_number,
                "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "rendered_visual_sha256": visual_hash,
                "extracted_alias": alias,
                "alias_similarity_to_target": round(similarity, 4),
                "disposition": disposition,
                "reason": reason,
                "duplicate_canonical_source_page": canonical_page,
            }
        )

    duplicate_groups = [
        {"canonical_source_page": canonical, "source_pages": members}
        for canonical, members in duplicate_members.items()
        if len(members) > 1
    ]
    selected_page_numbers = [
        record["source_page_number"]
        for record in page_records
        if record["disposition"] == "IN_SCOPE_CANDIDATE"
        and record["duplicate_canonical_source_page"] == record["source_page_number"]
    ]
    related_pages_pdf = output_directory / "related-pages.pdf"
    annotated_pages_pdf = output_directory / "related-pages-red-box.pdf"
    derivative_data = _write_selected_pages(
        reader,
        selected_page_numbers,
        related_pages_pdf,
        annotated_pages_pdf,
        output_directory,
    )

    source_hash_after = file_sha256(source_pdf)
    manifest = {
        "schema_version": "0.1-synthetic-lab",
        "scope": "Non-production synthetic evidence-page validation only.",
        "source": {
            "file_name": source_pdf.name,
            "sha256_before": source_hash_before,
            "sha256_after": source_hash_after,
            "unchanged": source_hash_before == source_hash_after,
            "page_count": len(page_records),
        },
        "target_alias": target_alias,
        "exact_visual_duplicate_groups": duplicate_groups,
        "pages": page_records,
        "derivatives": {
            "related_pages_pdf": related_pages_pdf.name,
            "related_pages_pdf_sha256": file_sha256(related_pages_pdf),
            "annotated_pages_pdf": annotated_pages_pdf.name,
            "annotated_pages_pdf_sha256": file_sha256(annotated_pages_pdf),
            **derivative_data,
        },
        "summary": {
            "in_scope_candidates": sum(record["disposition"] == "IN_SCOPE_CANDIDATE" for record in page_records),
            "similar_review_required": sum(record["disposition"] == "SIMILAR_REVIEW_REQUIRED" for record in page_records),
            "out_of_scope_candidates": sum(record["disposition"] == "OUT_OF_SCOPE_CANDIDATE" for record in page_records),
            "selected_unique_source_pages": selected_page_numbers,
        },
    }
    manifest_path = output_directory / "pipeline-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return PipelineResult(manifest_path, related_pages_pdf, annotated_pages_pdf)


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
