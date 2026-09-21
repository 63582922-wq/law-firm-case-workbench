"""Safe, source-bound reading of common lawyer case documents.

This worker accepts only a server-created materialization record.  It does not
open browser paths, launch Office/LibreOffice, evaluate formulas, execute RTF
fields, render HTML, load remote resources or recursively open attachments.
The supported formats are intentionally explicit: DOCX, XLSX, PPTX, RTF,
TXT, CSV, HTML and EML.

Every extracted item is a review candidate with an exact source locator.  It
is never a formal fact, transaction, legal conclusion or evidence decision.
Active content is blocked; non-executable but easily overlooked content such
as formulas, hidden rows, speaker notes and remote references is retained with
an explicit risk flag so that it cannot silently disappear from lawyer review.
"""

from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable
from uuid import UUID
import zipfile
from xml.etree import ElementTree


class CommonDocumentReadingBlocked(ValueError):
    """A common document cannot be read within the safe parser contract."""


class CommonDocumentFormat(StrEnum):
    DOCX = "DOCX"
    XLSX = "XLSX"
    PPTX = "PPTX"
    RTF = "RTF"
    TXT = "TXT"
    CSV = "CSV"
    HTML = "HTML"
    EML = "EML"


class DocumentCandidateKind(StrEnum):
    PARAGRAPH = "PARAGRAPH"
    TABLE_CELL = "TABLE_CELL"
    SPREADSHEET_CELL = "SPREADSHEET_CELL"
    COMMENT = "COMMENT"
    SLIDE_TEXT = "SLIDE_TEXT"
    SPEAKER_NOTE = "SPEAKER_NOTE"
    EMAIL_HEADER = "EMAIL_HEADER"
    EMAIL_BODY = "EMAIL_BODY"
    ATTACHMENT_METADATA = "ATTACHMENT_METADATA"


class DocumentReviewStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class DocumentReadBudget:
    max_source_bytes: int = 100 * 1024 * 1024
    max_archive_entries: int = 10_000
    max_archive_member_bytes: int = 25 * 1024 * 1024
    max_archive_total_bytes: int = 512 * 1024 * 1024
    max_compression_ratio: int = 200
    max_xml_depth: int = 128
    max_xml_elements: int = 1_000_000
    max_candidates: int = 250_000
    max_total_characters: int = 50_000_000
    max_candidate_characters: int = 100_000
    max_table_rows: int = 1_000_000
    max_table_columns: int = 16_384
    max_email_parts: int = 2_000
    max_email_decoded_bytes: int = 200 * 1024 * 1024

    def validate(self) -> None:
        limits = (
            ("max_source_bytes", self.max_source_bytes, 1, 1024**3),
            ("max_archive_entries", self.max_archive_entries, 1, 100_000),
            ("max_archive_member_bytes", self.max_archive_member_bytes, 1, 512 * 1024**2),
            ("max_archive_total_bytes", self.max_archive_total_bytes, 1, 2 * 1024**3),
            ("max_compression_ratio", self.max_compression_ratio, 1, 10_000),
            ("max_xml_depth", self.max_xml_depth, 1, 512),
            ("max_xml_elements", self.max_xml_elements, 1, 5_000_000),
            ("max_candidates", self.max_candidates, 1, 2_000_000),
            ("max_total_characters", self.max_total_characters, 1, 500_000_000),
            ("max_candidate_characters", self.max_candidate_characters, 1, 2_000_000),
            ("max_table_rows", self.max_table_rows, 1, 2_000_000),
            ("max_table_columns", self.max_table_columns, 1, 100_000),
            ("max_email_parts", self.max_email_parts, 1, 20_000),
            ("max_email_decoded_bytes", self.max_email_decoded_bytes, 1, 2 * 1024**3),
        )
        for label, value, minimum, maximum in limits:
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise CommonDocumentReadingBlocked(f"{label} is outside the document reader limit")
        if self.max_archive_member_bytes > self.max_archive_total_bytes:
            raise CommonDocumentReadingBlocked("archive member budget exceeds total archive budget")
        if self.max_candidate_characters > self.max_total_characters:
            raise CommonDocumentReadingBlocked("candidate character budget exceeds total character budget")


@dataclass(frozen=True)
class MaterializedDocumentSource:
    """Worker-only locator for one immutable, private object materialization."""

    source_object_id: str
    source_object_version: str = field(repr=False)
    materialization_root: Path = field(repr=False, compare=False)
    path: Path = field(repr=False, compare=False)
    byte_size: int
    content_sha256: str
    admitted_format: CommonDocumentFormat

    def validate(self, *, budget: DocumentReadBudget) -> None:
        try:
            UUID(self.source_object_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise CommonDocumentReadingBlocked("source_object_id must be a UUID") from error
        if (
            not isinstance(self.source_object_version, str)
            or self.source_object_version != self.source_object_version.strip()
            or not 1 <= len(self.source_object_version) <= 512
            or any(ord(character) < 32 for character in self.source_object_version)
        ):
            raise CommonDocumentReadingBlocked("source object version is invalid")
        if not isinstance(self.admitted_format, CommonDocumentFormat):
            raise CommonDocumentReadingBlocked("admitted document format is invalid")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 1 <= self.byte_size <= budget.max_source_bytes
        ):
            raise CommonDocumentReadingBlocked("materialized source exceeds the byte budget")
        _sha256_value(self.content_sha256, "source content_sha256")
        root = Path(self.materialization_root)
        path = Path(self.path)
        if not root.is_absolute() or not path.is_absolute():
            raise CommonDocumentReadingBlocked("materialized source paths must be absolute")
        if root.is_symlink() or not root.is_dir():
            raise CommonDocumentReadingBlocked("materialization root is missing or symbolic")
        if path.is_symlink() or not path.is_file():
            raise CommonDocumentReadingBlocked("materialized source is missing or symbolic")
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            raise CommonDocumentReadingBlocked("materialized source escaped its private worker root")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or path.stat().st_size != self.byte_size:
            raise CommonDocumentReadingBlocked("materialized source type or byte size changed")


@dataclass(frozen=True)
class DocumentSourceLocation:
    container_part: str
    section: str
    ordinal: int
    page_or_slide: int | None = None
    row: int | None = None
    column: int | None = None
    coordinate: str | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class DocumentReadCandidate:
    candidate_id: str
    kind: DocumentCandidateKind
    text: str = field(repr=False)
    content_hash: str
    location: DocumentSourceLocation
    risk_flags: tuple[str, ...]
    attributes: tuple[tuple[str, str], ...] = field(repr=False)
    literal_text_only: bool
    review_status: DocumentReviewStatus = DocumentReviewStatus.NEEDS_REVIEW


@dataclass(frozen=True)
class CommonDocumentReadResult:
    source_object_id: str
    source_sha256: str
    source_reference_hash: str
    detected_format: CommonDocumentFormat
    parser_version: str
    candidates: tuple[DocumentReadCandidate, ...] = field(repr=False)
    document_risk_flags: tuple[str, ...]
    review_status: DocumentReviewStatus
    result_hash: str


PARSER_VERSION = "1.0.0"

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PRESENTATION_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_RELATIONSHIP = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"

_ACTIVE_PACKAGE_PART_MARKERS = (
    "/vbaproject.bin",
    "/activex/",
    "/embeddings/",
    "customui/",
    "/externallinks/",
)
_EXTERNAL_SCHEMES = ("http:", "https:", "ftp:", "file:", "mailto:", "data:", "javascript:")
_FORMULA_ACTIVE_TOKENS = (
    "WEBSERVICE(",
    "HYPERLINK(",
    "DDE",
    "EXEC(",
    "CALL(",
    "REGISTER.ID(",
    "FILTERXML(",
)
_EXECUTABLE_ATTACHMENT_SUFFIXES = {
    ".bat", ".cmd", ".com", ".dll", ".exe", ".hta", ".jar", ".js", ".lnk",
    ".msi", ".ps1", ".py", ".scr", ".sh", ".vbs", ".wsf",
}


def read_materialized_common_document(
    source: MaterializedDocumentSource,
    *,
    budget: DocumentReadBudget | None = None,
) -> CommonDocumentReadResult:
    """Read one verified materialization without mutating or executing it."""

    limits = budget or DocumentReadBudget()
    limits.validate()
    source.validate(budget=limits)
    _verify_source(source)
    collector = _CandidateCollector(source=source, budget=limits)
    document_flags: set[str] = set()
    try:
        if source.admitted_format in {
            CommonDocumentFormat.DOCX,
            CommonDocumentFormat.XLSX,
            CommonDocumentFormat.PPTX,
        }:
            with zipfile.ZipFile(source.path) as archive:
                package = _inspect_ooxml_package(
                    archive, expected=source.admitted_format, budget=limits
                )
                document_flags.update(_package_review_risks(package))
                if source.admitted_format is CommonDocumentFormat.DOCX:
                    document_flags.update(_read_docx(package, collector))
                elif source.admitted_format is CommonDocumentFormat.XLSX:
                    document_flags.update(_read_xlsx(package, collector))
                else:
                    document_flags.update(_read_pptx(package, collector))
        else:
            raw = _read_bounded_source(source, limits.max_source_bytes)
            if source.admitted_format is CommonDocumentFormat.RTF:
                document_flags.update(_read_rtf(raw, collector, limits))
            elif source.admitted_format is CommonDocumentFormat.TXT:
                document_flags.update(_read_plain_text(raw, collector, limits))
            elif source.admitted_format is CommonDocumentFormat.CSV:
                document_flags.update(_read_csv(raw, collector, limits))
            elif source.admitted_format is CommonDocumentFormat.HTML:
                document_flags.update(_read_html(raw, collector, limits, section="document"))
            elif source.admitted_format is CommonDocumentFormat.EML:
                document_flags.update(_read_eml(raw, collector, limits))
            else:  # pragma: no cover - enum exhaustiveness
                raise CommonDocumentReadingBlocked("document format has no safe reader")
    except CommonDocumentReadingBlocked:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError, csv.Error) as error:
        raise CommonDocumentReadingBlocked("document structure cannot be parsed safely") from error
    _verify_source(source)
    candidates = collector.finish()
    source_reference_hash = _canonical_hash(
        {
            "schema_version": "common-document-source-ref-v1",
            "source_object_id": source.source_object_id,
            "source_object_version_hash": sha256(
                source.source_object_version.encode("utf-8")
            ).hexdigest(),
            "source_sha256": source.content_sha256,
            "byte_size": source.byte_size,
            "format": source.admitted_format.value,
        }
    )
    flags = tuple(sorted(document_flags))
    result_hash = _canonical_hash(
        {
            "schema_version": "common-document-read-result-v1",
            "source_reference_hash": source_reference_hash,
            "parser_version": PARSER_VERSION,
            "candidates": [_candidate_payload(item) for item in candidates],
            "document_risk_flags": flags,
            "review_status": DocumentReviewStatus.NEEDS_REVIEW.value,
        }
    )
    return CommonDocumentReadResult(
        source_object_id=source.source_object_id,
        source_sha256=source.content_sha256,
        source_reference_hash=source_reference_hash,
        detected_format=source.admitted_format,
        parser_version=PARSER_VERSION,
        candidates=candidates,
        document_risk_flags=flags,
        review_status=DocumentReviewStatus.NEEDS_REVIEW,
        result_hash=result_hash,
    )


@dataclass
class _CandidateCollector:
    source: MaterializedDocumentSource
    budget: DocumentReadBudget
    items: list[DocumentReadCandidate] = field(default_factory=list)
    total_characters: int = 0

    def add(
        self,
        *,
        kind: DocumentCandidateKind,
        text: str,
        location: DocumentSourceLocation,
        risk_flags: Iterable[str] = (),
        attributes: Iterable[tuple[str, str]] = (),
        literal_text_only: bool = True,
    ) -> None:
        normalized = _normalize_candidate_text(text)
        if not normalized:
            return
        if len(normalized) > self.budget.max_candidate_characters:
            raise CommonDocumentReadingBlocked("one extracted candidate exceeds the character budget")
        self.total_characters += len(normalized)
        if self.total_characters > self.budget.max_total_characters:
            raise CommonDocumentReadingBlocked("document extraction exceeds the character budget")
        if len(self.items) >= self.budget.max_candidates:
            raise CommonDocumentReadingBlocked("document extraction exceeds the candidate budget")
        flag_items = tuple(sorted(set(risk_flags)))
        for value in flag_items:
            _stable_code(value, "candidate risk flag")
        attribute_items = tuple(sorted(tuple(attributes)))
        if len(attribute_items) > 50 or len({item[0] for item in attribute_items}) != len(attribute_items):
            raise CommonDocumentReadingBlocked("candidate attributes are duplicated or unbounded")
        for key, value in attribute_items:
            _attribute_code(key, "candidate attribute key")
            if not isinstance(value, str) or len(value) > 10_000 or any(ord(char) < 32 for char in value):
                raise CommonDocumentReadingBlocked("candidate attribute value is invalid")
        content_hash = sha256(normalized.encode("utf-8")).hexdigest()
        candidate_id = _canonical_hash(
            {
                "schema_version": "common-document-candidate-v1",
                "source_sha256": self.source.content_sha256,
                "kind": kind.value,
                "text_hash": content_hash,
                "location": _location_payload(location),
                "risk_flags": flag_items,
                "attributes": attribute_items,
            }
        )
        self.items.append(
            DocumentReadCandidate(
                candidate_id=candidate_id,
                kind=kind,
                text=normalized,
                content_hash=content_hash,
                location=location,
                risk_flags=flag_items,
                attributes=attribute_items,
                literal_text_only=literal_text_only,
            )
        )

    def finish(self) -> tuple[DocumentReadCandidate, ...]:
        if not self.items:
            raise CommonDocumentReadingBlocked("document contains no reviewable text candidates")
        return tuple(self.items)


@dataclass(frozen=True)
class _OoxmlPackage:
    archive: zipfile.ZipFile = field(repr=False, compare=False)
    names: frozenset[str]
    infos: dict[str, zipfile.ZipInfo] = field(repr=False, compare=False)
    budget: DocumentReadBudget

    def xml(self, name: str) -> ElementTree.Element:
        normalized = _normalize_package_name(name)
        info = self.infos.get(normalized)
        if info is None:
            raise CommonDocumentReadingBlocked(f"required OOXML member is missing: {normalized}")
        if info.file_size < 1 or info.file_size > self.budget.max_archive_member_bytes:
            raise CommonDocumentReadingBlocked("OOXML XML member exceeds the byte budget")
        with self.archive.open(info, "r") as stream:
            raw = stream.read(self.budget.max_archive_member_bytes + 1)
        if len(raw) != info.file_size or len(raw) > self.budget.max_archive_member_bytes:
            raise CommonDocumentReadingBlocked("OOXML member size changed while reading")
        upper = raw.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise CommonDocumentReadingBlocked("OOXML contains a prohibited DTD or entity")
        root = ElementTree.fromstring(raw)
        depth, elements = _xml_metrics(root, maximum_elements=self.budget.max_xml_elements)
        if depth > self.budget.max_xml_depth:
            raise CommonDocumentReadingBlocked("OOXML XML depth exceeds the parser budget")
        if elements > self.budget.max_xml_elements:
            raise CommonDocumentReadingBlocked("OOXML element count exceeds the parser budget")
        return root


def _inspect_ooxml_package(
    archive: zipfile.ZipFile,
    *,
    expected: CommonDocumentFormat,
    budget: DocumentReadBudget,
) -> _OoxmlPackage:
    entries = archive.infolist()
    if not 1 <= len(entries) <= budget.max_archive_entries:
        raise CommonDocumentReadingBlocked("OOXML archive entry count exceeds the budget")
    infos: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in entries:
        name = _validated_package_name(info.filename)
        identity = name.casefold()
        if identity in infos:
            raise CommonDocumentReadingBlocked("OOXML contains duplicate normalized paths")
        if info.flag_bits & 0x1:
            raise CommonDocumentReadingBlocked("encrypted OOXML members are not supported")
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_IFMT(mode) == stat.S_IFLNK:
            raise CommonDocumentReadingBlocked("OOXML contains a symbolic link")
        if info.file_size < 0 or info.file_size > budget.max_archive_member_bytes:
            raise CommonDocumentReadingBlocked("OOXML member exceeds the byte budget")
        total += info.file_size
        if total > budget.max_archive_total_bytes:
            raise CommonDocumentReadingBlocked("OOXML expanded size exceeds the archive budget")
        if (
            info.file_size >= 1024 * 1024
            and info.file_size / max(info.compress_size, 1) > budget.max_compression_ratio
        ):
            raise CommonDocumentReadingBlocked("OOXML member exceeds the compression-ratio budget")
        infos[identity] = info
    names = frozenset(infos)
    expected_main = {
        CommonDocumentFormat.DOCX: "word/document.xml",
        CommonDocumentFormat.XLSX: "xl/workbook.xml",
        CommonDocumentFormat.PPTX: "ppt/presentation.xml",
    }[expected]
    family_mains = {
        "word/document.xml",
        "xl/workbook.xml",
        "ppt/presentation.xml",
    }.intersection(names)
    if "[content_types].xml" not in names or family_mains != {expected_main}:
        raise CommonDocumentReadingBlocked("OOXML family differs from the admitted format")
    for name in names:
        marker = f"/{name}/"
        lowered = f"/{name}"
        if any(token in lowered for token in _ACTIVE_PACKAGE_PART_MARKERS):
            raise CommonDocumentReadingBlocked("OOXML contains macros, embeds or active package parts")
        if name.endswith((".exe", ".dll", ".js", ".vbs", ".ps1", ".sh", ".bat")):
            raise CommonDocumentReadingBlocked("OOXML contains an executable package member")
        if marker.startswith("/customui/"):
            raise CommonDocumentReadingBlocked("OOXML contains an active custom UI")
    package = _OoxmlPackage(archive=archive, names=names, infos=infos, budget=budget)
    content_types = package.xml("[content_types].xml")
    content_type_text = ElementTree.tostring(content_types, encoding="unicode").casefold()
    if any(token in content_type_text for token in ("macroenabled", "activex", "vbaproject", "oleobject")):
        raise CommonDocumentReadingBlocked("OOXML content types declare active content")
    for name in sorted(value for value in names if value.endswith(".rels")):
        root = package.xml(name)
        for relation in root.iter(_PACKAGE_RELATIONSHIP):
            target = str(relation.attrib.get("Target", "")).strip()
            mode = str(relation.attrib.get("TargetMode", "")).casefold()
            rel_type = str(relation.attrib.get("Type", "")).casefold()
            if mode == "external" or _has_external_scheme(target):
                raise CommonDocumentReadingBlocked("OOXML contains an external relationship")
            relationship_kind = rel_type.rsplit("/", 1)[-1]
            if relationship_kind in {
                "oleobject",
                "package",
                "attachedtemplate",
                "control",
                "activexcontrol",
            }:
                raise CommonDocumentReadingBlocked("OOXML relationship declares embedded active content")
            if _relationship_escapes_package(name, target):
                raise CommonDocumentReadingBlocked("OOXML relationship escapes its package")
    return package


def _package_review_risks(package: _OoxmlPackage) -> set[str]:
    """Surface package content that the text parser intentionally does not interpret."""

    flags: set[str] = set()
    if any(
        name.startswith(("word/media/", "xl/media/", "ppt/media/"))
        for name in package.names
    ):
        flags.add("MEDIA_CONTENT_REQUIRES_VISUAL_REVIEW")
    if any(
        name.startswith(("word/charts/", "xl/charts/", "ppt/charts/"))
        for name in package.names
    ):
        flags.add("CHART_CONTENT_REQUIRES_VISUAL_REVIEW")
    if any(
        name.startswith(("word/diagrams/", "xl/diagrams/", "ppt/diagrams/"))
        for name in package.names
    ):
        flags.add("DIAGRAM_CONTENT_REQUIRES_VISUAL_REVIEW")
    return flags


def _read_docx(package: _OoxmlPackage, collector: _CandidateCollector) -> set[str]:
    flags: set[str] = set()
    parts = ["word/document.xml"]
    parts.extend(
        sorted(
            name
            for name in package.names
            if re.fullmatch(
                r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name
            )
        )
    )
    ordinal = 0
    table_ordinal = 0
    for part in parts:
        root = package.xml(part)
        if root.findall(f".//{_WORD_NS}altChunk"):
            raise CommonDocumentReadingBlocked("DOCX altChunk content is not safely supported")
        instructions = " ".join(
            node.text or "" for node in root.iter(f"{_WORD_NS}instrText")
        ).upper()
        if any(token in instructions for token in ("DDE", "INCLUDETEXT", "INCLUDEPICTURE")):
            raise CommonDocumentReadingBlocked("DOCX contains an active external field instruction")
        if instructions.strip():
            flags.add("FIELD_CODE_PRESENT")
        if root.findall(f".//{_WORD_NS}del") or root.findall(f".//{_WORD_NS}ins"):
            flags.add("TRACKED_CHANGES_PRESENT")
        table_paragraph_ids = {
            id(paragraph)
            for table in root.iter(f"{_WORD_NS}tbl")
            for paragraph in table.iter(f"{_WORD_NS}p")
        }
        for child in root.iter():
            if child.tag == f"{_WORD_NS}tbl":
                table_ordinal += 1
                for row_number, row in enumerate(child.findall(f"{_WORD_NS}tr"), start=1):
                    for column_number, cell in enumerate(
                        row.findall(f"{_WORD_NS}tc"), start=1
                    ):
                        text = _word_text(cell)
                        risks = _word_risks(cell)
                        flags.update(risks)
                        ordinal += 1
                        collector.add(
                            kind=DocumentCandidateKind.TABLE_CELL,
                            text=text,
                            location=DocumentSourceLocation(
                                part, f"table-{table_ordinal}", ordinal,
                                row=row_number, column=column_number,
                            ),
                            risk_flags=risks,
                        )
            elif child.tag == f"{_WORD_NS}p" and id(child) not in table_paragraph_ids:
                text = _word_text(child)
                risks = _word_risks(child)
                flags.update(risks)
                ordinal += 1
                kind = (
                    DocumentCandidateKind.COMMENT
                    if part == "word/comments.xml"
                    else DocumentCandidateKind.PARAGRAPH
                )
                collector.add(
                    kind=kind,
                    text=text,
                    location=DocumentSourceLocation(part, "paragraph", ordinal),
                    risk_flags=risks,
                )
    return flags


def _read_xlsx(package: _OoxmlPackage, collector: _CandidateCollector) -> set[str]:
    flags: set[str] = set()
    shared_strings = _xlsx_shared_strings(package)
    workbook = package.xml("xl/workbook.xml")
    relations = _relationship_map(package, "xl/_rels/workbook.xml.rels", "xl")
    hidden_sheets: set[str] = set()
    ordinal = 0
    for sheet in workbook.iter(f"{_SHEET_NS}sheet"):
        sheet_name = str(sheet.attrib.get("name", "")).strip()
        relation_id = str(sheet.attrib.get(f"{_OFFICE_REL_NS}id", ""))
        state = str(sheet.attrib.get("state", "visible")).casefold()
        if not sheet_name or relation_id not in relations:
            raise CommonDocumentReadingBlocked("XLSX sheet relationship is malformed")
        part = relations[relation_id]
        if state != "visible":
            hidden_sheets.add(sheet_name)
            flags.add("HIDDEN_SHEET_PRESENT")
        root = package.xml(part)
        hidden_columns = _xlsx_hidden_columns(root, package.budget)
        if hidden_columns:
            flags.add("HIDDEN_COLUMN_PRESENT")
        for row_number, row in enumerate(root.iter(f"{_SHEET_NS}row"), start=1):
            actual_row = _positive_int(row.attrib.get("r"), fallback=row_number)
            if actual_row > package.budget.max_table_rows:
                raise CommonDocumentReadingBlocked("XLSX row exceeds the table budget")
            hidden_row = str(row.attrib.get("hidden", "0")) in {"1", "true", "True"}
            if hidden_row:
                flags.add("HIDDEN_ROW_PRESENT")
            for cell in row.findall(f"{_SHEET_NS}c"):
                coordinate = str(cell.attrib.get("r", "")).upper()
                column_number = _xlsx_column_number(coordinate)
                if column_number > package.budget.max_table_columns:
                    raise CommonDocumentReadingBlocked("XLSX column exceeds the table budget")
                formula_node = cell.find(f"{_SHEET_NS}f")
                formula = formula_node.text.strip() if formula_node is not None and formula_node.text else None
                if formula and _active_formula(formula):
                    raise CommonDocumentReadingBlocked("XLSX formula requests an external or active operation")
                value = _xlsx_cell_text(cell, shared_strings)
                risks: set[str] = set()
                attributes: list[tuple[str, str]] = [("sheet_name", sheet_name)]
                if formula_node is not None:
                    risks.add("FORMULA_PRESENT_NOT_EVALUATED")
                    flags.add("FORMULA_PRESENT_NOT_EVALUATED")
                    attributes.append(("formula_literal", formula or "<SHARED_OR_EMPTY_FORMULA>"))
                if _formula_like_literal(value):
                    risks.add("FORMULA_LIKE_LITERAL")
                    flags.add("FORMULA_LIKE_LITERAL")
                if sheet_name in hidden_sheets or hidden_row or column_number in hidden_columns:
                    risks.add("HIDDEN_CONTENT")
                ordinal += 1
                collector.add(
                    kind=DocumentCandidateKind.SPREADSHEET_CELL,
                    text=value if value else (f"={formula}" if formula else ""),
                    location=DocumentSourceLocation(
                        part, sheet_name, ordinal, row=actual_row,
                        column=column_number, coordinate=coordinate,
                    ),
                    risk_flags=risks,
                    attributes=attributes,
                )
    for part in sorted(name for name in package.names if re.fullmatch(r"xl/comments\d*\.xml", name)):
        root = package.xml(part)
        for comment in root.iter(f"{_SHEET_NS}comment"):
            coordinate = str(comment.attrib.get("ref", "")).upper()
            ordinal += 1
            collector.add(
                kind=DocumentCandidateKind.COMMENT,
                text="".join(node.text or "" for node in comment.iter(f"{_SHEET_NS}t")),
                location=DocumentSourceLocation(
                    part, "comment", ordinal, coordinate=coordinate
                ),
                risk_flags=("COMMENT_CONTENT",),
            )
            flags.add("COMMENT_CONTENT")
    return flags


def _read_pptx(package: _OoxmlPackage, collector: _CandidateCollector) -> set[str]:
    flags: set[str] = set()
    presentation = package.xml("ppt/presentation.xml")
    relations = _relationship_map(package, "ppt/_rels/presentation.xml.rels", "ppt")
    slide_parts: list[tuple[str, bool]] = []
    for slide_id in presentation.iter(f"{_PRESENTATION_NS}sldId"):
        relation_id = str(slide_id.attrib.get(f"{_OFFICE_REL_NS}id", ""))
        part = relations.get(relation_id)
        if part is None:
            raise CommonDocumentReadingBlocked("PPTX slide relationship is malformed")
        hidden = str(slide_id.attrib.get("show", "1")).casefold() in {"0", "false"}
        slide_parts.append((part, hidden))
    if not slide_parts:
        raise CommonDocumentReadingBlocked("PPTX contains no slides")
    ordinal = 0
    for slide_number, (part, hidden) in enumerate(slide_parts, start=1):
        root = package.xml(part)
        if str(root.attrib.get("show", "1")).casefold() in {"0", "false"}:
            hidden = True
        if hidden:
            flags.add("HIDDEN_SLIDE_PRESENT")
        hidden_shape = any(
            str(value).casefold() in {"1", "true"}
            for element in root.iter()
            for key, value in element.attrib.items()
            if key.rsplit("}", 1)[-1].casefold() == "hidden"
        )
        if hidden_shape:
            flags.add("HIDDEN_SHAPE_PRESENT")
        for text_node in root.iter(f"{_DRAWING_NS}t"):
            ordinal += 1
            collector.add(
                kind=DocumentCandidateKind.SLIDE_TEXT,
                text=text_node.text or "",
                location=DocumentSourceLocation(
                    part, "slide-text", ordinal, page_or_slide=slide_number
                ),
                risk_flags=("HIDDEN_CONTENT",) if hidden or hidden_shape else (),
            )
    for part in sorted(
        name for name in package.names if re.fullmatch(r"ppt/notesslides/notesslide\d+\.xml", name)
    ):
        note_number = _trailing_number(part)
        root = package.xml(part)
        for text_node in root.iter(f"{_DRAWING_NS}t"):
            ordinal += 1
            collector.add(
                kind=DocumentCandidateKind.SPEAKER_NOTE,
                text=text_node.text or "",
                location=DocumentSourceLocation(
                    part, "speaker-note", ordinal, page_or_slide=note_number
                ),
                risk_flags=("SPEAKER_NOTES_CONTENT",),
            )
            flags.add("SPEAKER_NOTES_CONTENT")
    for part in sorted(name for name in package.names if name.startswith("ppt/comments/") and name.endswith(".xml")):
        root = package.xml(part)
        for text_node in root.iter():
            if text_node.tag.rsplit("}", 1)[-1] in {"text", "t"} and text_node.text:
                ordinal += 1
                collector.add(
                    kind=DocumentCandidateKind.COMMENT,
                    text=text_node.text,
                    location=DocumentSourceLocation(part, "comment", ordinal),
                    risk_flags=("COMMENT_CONTENT",),
                )
                flags.add("COMMENT_CONTENT")
    return flags


def _read_plain_text(
    raw: bytes, collector: _CandidateCollector, budget: DocumentReadBudget
) -> set[str]:
    text, encoding = _decode_text(raw)
    stripped = text.lstrip().casefold()
    if stripped.startswith(("{\\rtf", "<!doctype html", "<html")):
        raise CommonDocumentReadingBlocked("plain-text content differs from the admitted format")
    _reject_controls(text)
    _add_text_paragraphs(
        text, collector, part="text", section="body", kind=DocumentCandidateKind.PARAGRAPH,
        attributes=(("encoding", encoding),),
    )
    return set()


def _read_csv(
    raw: bytes, collector: _CandidateCollector, budget: DocumentReadBudget
) -> set[str]:
    text, encoding = _decode_text(raw)
    _reject_controls(text)
    flags: set[str] = set()
    stream = io.StringIO(text, newline="")
    try:
        reader = csv.reader(stream, delimiter=",", strict=True)
        ordinal = 0
        for row_number, row in enumerate(reader, start=1):
            if row_number > budget.max_table_rows or len(row) > budget.max_table_columns:
                raise CommonDocumentReadingBlocked("CSV dimensions exceed the table budget")
            for column_number, value in enumerate(row, start=1):
                risks: set[str] = set()
                if _formula_like_literal(value):
                    risks.add("FORMULA_LIKE_LITERAL")
                    flags.add("FORMULA_LIKE_LITERAL")
                ordinal += 1
                collector.add(
                    kind=DocumentCandidateKind.TABLE_CELL,
                    text=value,
                    location=DocumentSourceLocation(
                        "csv", "row", ordinal, row=row_number, column=column_number,
                        coordinate=f"R{row_number}C{column_number}",
                    ),
                    risk_flags=risks,
                    attributes=(("encoding", encoding),),
                )
    except csv.Error as error:
        raise CommonDocumentReadingBlocked("CSV quoting or row structure is malformed") from error
    return flags


def _read_html(
    raw: bytes,
    collector: _CandidateCollector,
    budget: DocumentReadBudget,
    *,
    section: str,
) -> set[str]:
    text, encoding = _decode_text(raw)
    stripped = text.lstrip().casefold()
    if not stripped.startswith(("<!doctype html", "<html")):
        raise CommonDocumentReadingBlocked("HTML signature differs from the admitted format")
    parser = _SafeVisibleHtmlParser(
        collector=collector,
        part="html",
        section=section,
        encoding=encoding,
    )
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise CommonDocumentReadingBlocked("HTML structure cannot be parsed safely") from error
    if parser.depth != 0:
        raise CommonDocumentReadingBlocked("HTML element nesting is unbalanced")
    return parser.flags


def _read_eml(
    raw: bytes, collector: _CandidateCollector, budget: DocumentReadBudget
) -> set[str]:
    if b"\x00" in raw:
        raise CommonDocumentReadingBlocked("EML contains binary control bytes")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as error:
        raise CommonDocumentReadingBlocked("EML structure is malformed") from error
    if message.defects:
        raise CommonDocumentReadingBlocked("EML parser reported structural defects")
    flags: set[str] = set()
    ordinal = 0
    for header in ("date", "from", "to", "cc", "bcc", "subject", "message-id"):
        values = message.get_all(header, failobj=[])
        for value in values:
            ordinal += 1
            collector.add(
                kind=DocumentCandidateKind.EMAIL_HEADER,
                text=str(value),
                location=DocumentSourceLocation("eml", f"header:{header}", ordinal),
                attributes=(("header_name", header),),
            )
    decoded_total = 0
    part_count = 0
    stack: list[Message] = [message]
    while stack:
        part = stack.pop()
        part_count += 1
        if part_count > budget.max_email_parts:
            raise CommonDocumentReadingBlocked("EML part count exceeds the budget")
        media_type = part.get_content_type().casefold()
        filename = part.get_filename()
        disposition = str(part.get_content_disposition() or "").casefold()
        if filename is not None or disposition == "attachment" or media_type == "message/rfc822":
            raw_attachment = part.as_bytes(policy=policy.default)
            decoded_total += len(raw_attachment)
            if decoded_total > budget.max_email_decoded_bytes:
                raise CommonDocumentReadingBlocked("EML decoded payload exceeds the budget")
            safe_name = _safe_attachment_name(filename or "attached-message.eml")
            suffix = Path(safe_name).suffix.casefold()
            risks = {"ATTACHMENT_NOT_RECURSIVELY_PARSED"}
            if suffix in _EXECUTABLE_ATTACHMENT_SUFFIXES:
                risks.add("EXECUTABLE_ATTACHMENT_QUARANTINED")
            flags.update(risks)
            ordinal += 1
            collector.add(
                kind=DocumentCandidateKind.ATTACHMENT_METADATA,
                text=safe_name,
                location=DocumentSourceLocation("eml", f"mime-part-{part_count}", ordinal),
                risk_flags=risks,
                attributes=(
                    ("attachment_media_type", media_type),
                    ("attachment_bytes", str(len(raw_attachment))),
                    ("attachment_sha256", sha256(raw_attachment).hexdigest()),
                ),
            )
            continue
        if part.is_multipart():
            children = part.get_payload()
            if not isinstance(children, list):
                raise CommonDocumentReadingBlocked("EML multipart payload is malformed")
            stack.extend(reversed(children))
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            plain = part.get_payload(decode=False)
            if isinstance(plain, str):
                payload = plain.encode(part.get_content_charset() or "utf-8", errors="strict")
            else:
                payload = b""
        decoded_total += len(payload)
        if decoded_total > budget.max_email_decoded_bytes:
            raise CommonDocumentReadingBlocked("EML decoded payload exceeds the budget")
        if media_type not in {"text/plain", "text/html"}:
            flags.add("UNSUPPORTED_INLINE_MIME_PART")
            continue
        charset = part.get_content_charset() or "utf-8"
        body_text, normalized_encoding = _decode_text(payload, declared_charset=charset)
        if media_type == "text/plain":
            _reject_controls(body_text)
            before = len(collector.items)
            _add_text_paragraphs(
                body_text,
                collector,
                part=f"eml-part-{part_count}",
                section="plain-body",
                kind=DocumentCandidateKind.EMAIL_BODY,
                attributes=(("encoding", normalized_encoding),),
            )
            ordinal += len(collector.items) - before
        else:
            parser = _SafeVisibleHtmlParser(
                collector=collector,
                part=f"eml-part-{part_count}",
                section="html-body",
                encoding=normalized_encoding,
                candidate_kind=DocumentCandidateKind.EMAIL_BODY,
            )
            parser.feed(body_text)
            parser.close()
            if parser.depth != 0:
                raise CommonDocumentReadingBlocked("EML HTML body nesting is unbalanced")
            flags.update(parser.flags)
    return flags


def _read_rtf(
    raw: bytes, collector: _CandidateCollector, budget: DocumentReadBudget
) -> set[str]:
    if not raw.lstrip().startswith(b"{\\rtf"):
        raise CommonDocumentReadingBlocked("RTF signature differs from the admitted format")
    if len(raw) > budget.max_source_bytes:
        raise CommonDocumentReadingBlocked("RTF source exceeds the byte budget")
    lowered = raw.lower()
    prohibited = (
        b"\\object", b"\\objdata", b"\\bin", b"\\field", b"\\filetbl",
        b"\\datastore", b"\\dde", b"\\includetext", b"\\includepicture",
    )
    if any(token in lowered for token in prohibited):
        raise CommonDocumentReadingBlocked("RTF contains embedded, binary or active field content")
    text = _rtf_to_bounded_text(raw, max_depth=budget.max_xml_depth)
    _reject_controls(text)
    _add_text_paragraphs(
        text, collector, part="rtf", section="paragraph",
        kind=DocumentCandidateKind.PARAGRAPH,
    )
    return set()


class _SafeVisibleHtmlParser(HTMLParser):
    _BLOCK_TAGS = {"address", "article", "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "td", "th"}
    _PROHIBITED_TAGS = {"applet", "base", "embed", "form", "iframe", "object", "script"}
    _SKIP_TAGS = {"style", "template", "noscript"}
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(
        self,
        *,
        collector: _CandidateCollector,
        part: str,
        section: str,
        encoding: str,
        candidate_kind: DocumentCandidateKind = DocumentCandidateKind.PARAGRAPH,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.collector = collector
        self.part = part
        self.section = section
        self.encoding = encoding
        self.candidate_kind = candidate_kind
        self.depth = 0
        self.skip_depth = 0
        self.stack: list[tuple[str, list[str], int]] = []
        self.flags: set[str] = set()
        self.ordinal = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if name in self._PROHIBITED_TAGS:
            raise CommonDocumentReadingBlocked("HTML contains an executable or active element")
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if name == "meta" and attributes.get("http-equiv", "").casefold() == "refresh":
            raise CommonDocumentReadingBlocked("HTML contains an active redirect")
        for key in ("href", "src", "action", "poster"):
            value = attributes.get(key, "").strip()
            if value and (_has_external_scheme(value) or value.startswith("//")):
                self.flags.add("EXTERNAL_REFERENCE_PRESENT_NOT_FETCHED")
        if any(key.startswith("on") for key in attributes):
            raise CommonDocumentReadingBlocked("HTML contains inline event-handler script")
        style = attributes.get("style", "").replace(" ", "").casefold()
        if any(token in style for token in ("display:none", "visibility:hidden", "opacity:0")):
            self.flags.add("HIDDEN_CONTENT")
        if name in self._VOID_TAGS:
            return
        self.depth += 1
        if self.depth > self.collector.budget.max_xml_depth:
            raise CommonDocumentReadingBlocked("HTML nesting exceeds the parser budget")
        if name in self._SKIP_TAGS:
            self.skip_depth += 1
        if name in self._BLOCK_TAGS:
            self.stack.append((name, [], self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        self.handle_starttag(tag, attrs)
        if name not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in self._VOID_TAGS:
            return
        if name in self._BLOCK_TAGS and self.stack:
            block_name, chunks, line_start = self.stack.pop()
            if block_name != name:
                raise CommonDocumentReadingBlocked("HTML block nesting is malformed")
            self.ordinal += 1
            kind = (
                DocumentCandidateKind.TABLE_CELL
                if name in {"td", "th"}
                else self.candidate_kind
            )
            self.collector.add(
                kind=kind,
                text=" ".join(chunks),
                location=DocumentSourceLocation(
                    self.part, f"{self.section}:{name}", self.ordinal,
                    line_start=line_start, line_end=self.getpos()[0],
                ),
                risk_flags=self.flags,
                attributes=(("encoding", self.encoding),),
            )
        if name in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        self.depth -= 1
        if self.depth < 0:
            raise CommonDocumentReadingBlocked("HTML closing tag is unbalanced")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        normalized = _normalize_candidate_text(data)
        if not normalized:
            return
        if self.stack:
            self.stack[-1][1].append(normalized)
        else:
            self.ordinal += 1
            self.collector.add(
                kind=self.candidate_kind,
                text=normalized,
                location=DocumentSourceLocation(
                    self.part, self.section, self.ordinal,
                    line_start=self.getpos()[0], line_end=self.getpos()[0],
                ),
                risk_flags=self.flags,
                attributes=(("encoding", self.encoding),),
            )

    def handle_pi(self, data: str) -> None:
        raise CommonDocumentReadingBlocked("HTML contains a processing instruction")

    def handle_entityref(self, name: str) -> None:
        raise CommonDocumentReadingBlocked("HTML contains an unresolved named entity")


def _xlsx_shared_strings(package: _OoxmlPackage) -> tuple[str, ...]:
    if "xl/sharedstrings.xml" not in package.names:
        return ()
    root = package.xml("xl/sharedstrings.xml")
    values = tuple(
        "".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t"))
        for item in root.iter(f"{_SHEET_NS}si")
    )
    if len(values) > package.budget.max_candidates:
        raise CommonDocumentReadingBlocked("XLSX shared strings exceed the candidate budget")
    return values


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> str:
    raw = cell.findtext(f"{_SHEET_NS}v", default="")
    cell_type = str(cell.attrib.get("t", ""))
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as error:
            raise CommonDocumentReadingBlocked("XLSX shared-string index is invalid") from error
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_SHEET_NS}t"))
    if cell_type == "b":
        return {"0": "FALSE", "1": "TRUE"}.get(raw, raw)
    return raw


def _xlsx_hidden_columns(root: ElementTree.Element, budget: DocumentReadBudget) -> set[int]:
    hidden: set[int] = set()
    for column in root.iter(f"{_SHEET_NS}col"):
        if str(column.attrib.get("hidden", "0")) not in {"1", "true", "True"}:
            continue
        start = _positive_int(column.attrib.get("min"), fallback=0)
        end = _positive_int(column.attrib.get("max"), fallback=0)
        if not start or end < start or end > budget.max_table_columns:
            raise CommonDocumentReadingBlocked("XLSX hidden-column range is malformed")
        if end - start + 1 > budget.max_table_columns:
            raise CommonDocumentReadingBlocked("XLSX hidden-column range exceeds the budget")
        hidden.update(range(start, end + 1))
    return hidden


def _relationship_map(package: _OoxmlPackage, rels_name: str, base: str) -> dict[str, str]:
    root = package.xml(rels_name)
    result: dict[str, str] = {}
    for relation in root.iter(_PACKAGE_RELATIONSHIP):
        relation_id = str(relation.attrib.get("Id", ""))
        target = str(relation.attrib.get("Target", ""))
        rel_type = str(relation.attrib.get("Type", "")).casefold()
        if not relation_id:
            raise CommonDocumentReadingBlocked("OOXML relationship id is missing")
        if not any(token in rel_type for token in ("worksheet", "slide")):
            continue
        part = _resolve_package_target(rels_name, target)
        if not part.startswith(f"{base}/") or part not in package.names:
            raise CommonDocumentReadingBlocked("OOXML document relationship target is missing")
        result[relation_id] = part
    return result


def _word_text(element: ElementTree.Element) -> str:
    return "".join(
        node.text or ""
        for node in element.iter()
        if node.tag in {f"{_WORD_NS}t", f"{_WORD_NS}delText", f"{_WORD_NS}tab", f"{_WORD_NS}br"}
    ).replace("\t", " ")


def _word_risks(element: ElementTree.Element) -> set[str]:
    risks: set[str] = set()
    if element.findall(f".//{_WORD_NS}vanish") or element.findall(f".//{_WORD_NS}webHidden"):
        risks.add("HIDDEN_CONTENT")
    if element.findall(f".//{_WORD_NS}del") or element.findall(f".//{_WORD_NS}ins"):
        risks.add("TRACKED_CHANGE_CONTENT")
    return risks


def _add_text_paragraphs(
    text: str,
    collector: _CandidateCollector,
    *,
    part: str,
    section: str,
    kind: DocumentCandidateKind,
    attributes: Iterable[tuple[str, str]] = (),
) -> None:
    lines = text.splitlines()
    buffer: list[str] = []
    start_line = 1
    ordinal = 0
    for line_number, line in enumerate((*lines, ""), start=1):
        if line.strip():
            if not buffer:
                start_line = line_number
            buffer.append(line)
            continue
        if not buffer:
            continue
        ordinal += 1
        collector.add(
            kind=kind,
            text="\n".join(buffer),
            location=DocumentSourceLocation(
                part, section, ordinal, line_start=start_line, line_end=line_number - 1
            ),
            attributes=attributes,
        )
        buffer = []


def _rtf_to_bounded_text(raw: bytes, *, max_depth: int) -> str:
    # RTF control syntax is ASCII.  Literal high bytes use the declared ANSI
    # code page; Unicode escapes are handled independently.
    match = re.search(br"\\ansicpg(\d+)", raw[:4096])
    codepage = int(match.group(1)) if match else 1252
    encoding = {65001: "utf-8", 936: "gb18030", 1252: "cp1252"}.get(codepage)
    if encoding is None:
        raise CommonDocumentReadingBlocked("RTF declares an unsupported ANSI code page")
    text = raw.decode("latin-1")
    destinations = {
        "fonttbl", "colortbl", "stylesheet", "info", "pict", "generator",
        "themedata", "colorschememapping", "latentstyles",
    }
    output: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip = False
    uc_skip = 1
    pending_skip = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            stack.append((skip, uc_skip))
            if len(stack) > max_depth:
                raise CommonDocumentReadingBlocked("RTF nesting exceeds the parser budget")
            index += 1
            continue
        if char == "}":
            if not stack:
                raise CommonDocumentReadingBlocked("RTF group closing brace is unbalanced")
            skip, uc_skip = stack.pop()
            index += 1
            continue
        if char != "\\":
            if not skip:
                if pending_skip:
                    pending_skip -= 1
                else:
                    output.append(char)
            index += 1
            continue
        index += 1
        if index >= len(text):
            raise CommonDocumentReadingBlocked("RTF ends in an incomplete escape")
        symbol = text[index]
        if symbol in "{}\\":
            if not skip and not pending_skip:
                output.append(symbol)
            elif pending_skip:
                pending_skip -= 1
            index += 1
            continue
        if symbol == "'":
            if index + 2 >= len(text) or not re.fullmatch(r"[0-9a-fA-F]{2}", text[index + 1:index + 3]):
                raise CommonDocumentReadingBlocked("RTF hexadecimal escape is malformed")
            escaped = bytearray()
            cursor = index
            while (
                cursor + 2 < len(text)
                and text[cursor] == "'"
                and re.fullmatch(r"[0-9a-fA-F]{2}", text[cursor + 1:cursor + 3])
            ):
                escaped.append(int(text[cursor + 1:cursor + 3], 16))
                cursor += 3
                if cursor + 1 < len(text) and text[cursor] == "\\" and text[cursor + 1] == "'":
                    cursor += 1
                else:
                    break
            if not skip and not pending_skip:
                try:
                    output.append(bytes(escaped).decode(encoding))
                except UnicodeDecodeError as error:
                    raise CommonDocumentReadingBlocked("RTF ANSI escape cannot be decoded safely") from error
            elif pending_skip:
                pending_skip = max(0, pending_skip - len(escaped))
            index = cursor
            continue
        if symbol == "*":
            skip = True
            index += 1
            continue
        word_match = re.match(r"([A-Za-z]+)(-?\d+)? ?", text[index:])
        if not word_match:
            # Formatting control symbol such as \~ or \_.
            if not skip and symbol in {"~", "_"}:
                output.append(" ")
            index += 1
            continue
        word = word_match.group(1).casefold()
        parameter = int(word_match.group(2)) if word_match.group(2) else None
        index += len(word_match.group(0))
        if word in destinations:
            skip = True
        elif word == "uc" and parameter is not None and 0 <= parameter <= 10:
            uc_skip = parameter
        elif word == "u" and parameter is not None and not skip:
            value = parameter if parameter >= 0 else parameter + 65536
            output.append(chr(value))
            pending_skip = uc_skip
        elif word in {"par", "line"} and not skip:
            output.append("\n")
        elif word == "tab" and not skip:
            output.append("\t")
    if stack:
        raise CommonDocumentReadingBlocked("RTF group opening brace is unbalanced")
    return "".join(output)


def _decode_text(raw: bytes, *, declared_charset: str | None = None) -> tuple[str, str]:
    if not isinstance(raw, bytes) or not raw:
        raise CommonDocumentReadingBlocked("text source is empty")
    if declared_charset:
        normalized = declared_charset.strip().casefold().replace("_", "-")
        aliases = {
            "utf8": "utf-8", "gbk": "gb18030", "gb2312": "gb18030",
            "windows-1252": "cp1252", "iso-8859-1": "latin-1", "us-ascii": "ascii",
        }
        encoding = aliases.get(normalized, normalized)
        if encoding not in {"utf-8", "utf-16", "utf-16-le", "utf-16-be", "gb18030", "cp1252", "latin-1", "ascii"}:
            raise CommonDocumentReadingBlocked("text declares an unsupported charset")
        try:
            return raw.decode(encoding, errors="strict"), encoding.upper()
        except (UnicodeDecodeError, LookupError) as error:
            raise CommonDocumentReadingBlocked("text bytes differ from the declared charset") from error
    encodings: tuple[tuple[str, str], ...]
    if raw.startswith(codecs.BOM_UTF8):
        encodings = (("utf-8-sig", "UTF-8"),)
    elif raw.startswith(codecs.BOM_UTF16_LE):
        encodings = (("utf-16", "UTF-16LE"),)
    elif raw.startswith(codecs.BOM_UTF16_BE):
        encodings = (("utf-16", "UTF-16BE"),)
    else:
        if b"\x00" in raw:
            raise CommonDocumentReadingBlocked("text contains binary NUL bytes without a Unicode BOM")
        encodings = (("utf-8", "UTF-8"), ("gb18030", "GB18030"))
    for encoding, label in encodings:
        try:
            decoded = raw.decode(encoding, errors="strict")
            if decoded.encode(encoding, errors="strict") == raw or encoding == "utf-8-sig":
                return decoded, label
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    raise CommonDocumentReadingBlocked("text encoding is unsupported or ambiguous")


def _reject_controls(text: str) -> None:
    if any(ord(character) < 32 and character not in "\n\r\t\f" for character in text):
        raise CommonDocumentReadingBlocked("text contains prohibited control characters")


def _verify_source(source: MaterializedDocumentSource) -> None:
    if source.path.is_symlink() or not source.path.is_file() or source.path.stat().st_size != source.byte_size:
        raise CommonDocumentReadingBlocked("materialized source changed during reading")
    digest = sha256()
    with source.path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != source.content_sha256:
        raise CommonDocumentReadingBlocked("materialized source hash differs from its object receipt")


def _read_bounded_source(source: MaterializedDocumentSource, maximum: int) -> bytes:
    with source.path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) != source.byte_size or len(raw) > maximum:
        raise CommonDocumentReadingBlocked("materialized source exceeds the read budget")
    return raw


def _validated_package_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise CommonDocumentReadingBlocked("OOXML package path is invalid")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or path.is_absolute() or ".." in path.parts or not path.parts:
        raise CommonDocumentReadingBlocked("OOXML package path escapes the archive")
    return "/".join(path.parts).casefold()


def _normalize_package_name(value: str) -> str:
    return _validated_package_name(value)


def _relationship_escapes_package(rels_name: str, target: str) -> bool:
    try:
        _resolve_package_target(rels_name, target)
    except CommonDocumentReadingBlocked:
        return True
    return False


def _resolve_package_target(rels_name: str, target: str) -> str:
    normalized = target.replace("\\", "/").strip()
    if not normalized or normalized.startswith("//") or _has_external_scheme(normalized):
        raise CommonDocumentReadingBlocked("OOXML relationship target is invalid")
    if normalized.startswith("/"):
        parts: list[str] = []
        target_parts = PurePosixPath(normalized.lstrip("/")).parts
    else:
        relation = PurePosixPath(rels_name)
        base_parent = relation.parent.parent
        parts = list(base_parent.parts)
        target_parts = PurePosixPath(normalized).parts
    for part in target_parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise CommonDocumentReadingBlocked("OOXML relationship escapes the package")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise CommonDocumentReadingBlocked("OOXML relationship target is empty")
    return "/".join(parts).casefold()


def _has_external_scheme(value: str) -> bool:
    lowered = value.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in _EXTERNAL_SCHEMES)


def _xml_metrics(root: ElementTree.Element, *, maximum_elements: int) -> tuple[int, int]:
    maximum = 1
    count = 0
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > maximum_elements:
            return maximum, count
        maximum = max(maximum, depth)
        stack.extend((child, depth + 1) for child in node)
    return maximum, count


def _xlsx_column_number(coordinate: str) -> int:
    match = re.fullmatch(r"([A-Z]{1,4})[1-9][0-9]*", coordinate)
    if match is None:
        raise CommonDocumentReadingBlocked("XLSX cell coordinate is malformed")
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - 64
    return result


def _positive_int(value: object, *, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        result = int(str(value))
    except ValueError as error:
        raise CommonDocumentReadingBlocked("document ordinal is malformed") from error
    if result < 1:
        raise CommonDocumentReadingBlocked("document ordinal must be positive")
    return result


def _active_formula(formula: str) -> bool:
    upper = formula.upper().replace(" ", "")
    return any(token in upper for token in _FORMULA_ACTIVE_TOKENS) or bool(
        re.search(r"\[[^\]]+\]", formula)
    )


def _formula_like_literal(value: str) -> bool:
    return bool(value) and value[0] in {"=", "+", "-", "@"}


def _safe_attachment_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or value.strip() in {".", ".."}
    ):
        raise CommonDocumentReadingBlocked("EML attachment filename is unsafe")
    return value.strip()


def _trailing_number(value: str) -> int | None:
    match = re.search(r"(\d+)\.xml$", value)
    return int(match.group(1)) if match else None


def _normalize_candidate_text(value: str) -> str:
    if not isinstance(value, str):
        raise CommonDocumentReadingBlocked("candidate text must be Unicode text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    _reject_controls(normalized)
    return normalized


def _location_payload(location: DocumentSourceLocation) -> dict[str, object]:
    return {
        "container_part": location.container_part,
        "section": location.section,
        "ordinal": location.ordinal,
        "page_or_slide": location.page_or_slide,
        "row": location.row,
        "column": location.column,
        "coordinate": location.coordinate,
        "line_start": location.line_start,
        "line_end": location.line_end,
    }


def _candidate_payload(candidate: DocumentReadCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind.value,
        "text_hash": candidate.content_hash,
        "location": _location_payload(candidate.location),
        "risk_flags": candidate.risk_flags,
        "attributes": candidate.attributes,
        "literal_text_only": candidate.literal_text_only,
        "review_status": candidate.review_status.value,
    }


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _sha256_value(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CommonDocumentReadingBlocked(f"{label} must be a lowercase SHA-256")


def _stable_code(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value) is None:
        raise CommonDocumentReadingBlocked(f"{label} must be a stable code")


def _attribute_code(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value) is None:
        raise CommonDocumentReadingBlocked(f"{label} must be a stable code")


__all__ = (
    "CommonDocumentFormat",
    "CommonDocumentReadResult",
    "CommonDocumentReadingBlocked",
    "DocumentCandidateKind",
    "DocumentReadBudget",
    "DocumentReadCandidate",
    "DocumentReviewStatus",
    "DocumentSourceLocation",
    "MaterializedDocumentSource",
    "PARSER_VERSION",
    "read_materialized_common_document",
)
