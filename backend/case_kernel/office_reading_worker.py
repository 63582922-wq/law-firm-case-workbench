"""Read safe DOCX/XLSX evidence without extracting or editing its container.

This worker only runs after the non-PDF structure inspector has rejected
macros, ActiveX and external relationships.  It reads a bounded XML subset
directly from the ZIP in memory and returns source-bound structured text; it
never evaluates formulas, opens Office, or writes alongside the original.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree
import zipfile

from .local_access_grants import AuthorizedOriginalFile
from .material_format_inspection import inspect_non_pdf_material


class OfficeReadingBlocked(ValueError):
    """The Office source cannot be safely read as structured evidence."""


@dataclass(frozen=True)
class OfficeParagraph:
    ordinal: int
    text: str


@dataclass(frozen=True)
class OfficeTableCell:
    table_ordinal: int
    row_ordinal: int
    column_ordinal: int
    text: str


@dataclass(frozen=True)
class SpreadsheetCell:
    sheet_name: str
    coordinate: str
    value: str
    formula: str | None


@dataclass(frozen=True)
class OfficeReadResult:
    source_sha256: str
    detected_kind: str
    paragraphs: tuple[OfficeParagraph, ...]
    table_cells: tuple[OfficeTableCell, ...]
    spreadsheet_cells: tuple[SpreadsheetCell, ...]


_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_XML_BYTES = 25 * 1024 * 1024
_MAX_PARAGRAPHS = 100_000
_MAX_TABLE_CELLS = 250_000
_MAX_SPREADSHEET_CELLS = 500_000
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_authorized_office_document(
    source: AuthorizedOriginalFile,
    *,
    detected_kind: str,
) -> OfficeReadResult:
    if detected_kind not in {"WORD_DOCUMENT", "SPREADSHEET"}:
        raise OfficeReadingBlocked("Office reader supports only DOCX and XLSX evidence")
    _verify_source(source)
    if source.byte_size > _MAX_SOURCE_BYTES:
        raise OfficeReadingBlocked("Office source exceeds the reader byte limit")
    checked = inspect_non_pdf_material(source.path, detected_kind=detected_kind)
    if checked.outcome != "REVIEW_REQUIRED":
        raise OfficeReadingBlocked(f"Office source was blocked by structural inspection: {checked.reason_code}")
    try:
        with zipfile.ZipFile(source.path) as archive:
            if detected_kind == "WORD_DOCUMENT":
                result = _read_docx(archive, source_sha256=source.sha256)
            else:
                result = _read_xlsx(archive, source_sha256=source.sha256)
    except OfficeReadingBlocked:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise OfficeReadingBlocked("Office XML cannot be safely parsed") from error
    _verify_source(source)
    return result


def _read_docx(archive: zipfile.ZipFile, *, source_sha256: str) -> OfficeReadResult:
    root = _parse_xml_member(archive, "word/document.xml")
    paragraphs: list[OfficeParagraph] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t"))
        if text:
            paragraphs.append(OfficeParagraph(len(paragraphs) + 1, text))
            if len(paragraphs) > _MAX_PARAGRAPHS:
                raise OfficeReadingBlocked("DOCX paragraph limit exceeded")
    table_cells: list[OfficeTableCell] = []
    for table_ordinal, table in enumerate(root.iter(f"{_WORD_NS}tbl"), start=1):
        for row_ordinal, row in enumerate(table.findall(f"{_WORD_NS}tr"), start=1):
            for column_ordinal, cell in enumerate(row.findall(f"{_WORD_NS}tc"), start=1):
                text = "".join(node.text or "" for node in cell.iter(f"{_WORD_NS}t"))
                table_cells.append(OfficeTableCell(table_ordinal, row_ordinal, column_ordinal, text))
                if len(table_cells) > _MAX_TABLE_CELLS:
                    raise OfficeReadingBlocked("DOCX table-cell limit exceeded")
    return OfficeReadResult(source_sha256, "WORD_DOCUMENT", tuple(paragraphs), tuple(table_cells), ())


def _read_xlsx(archive: zipfile.ZipFile, *, source_sha256: str) -> OfficeReadResult:
    shared_strings = _shared_strings(archive)
    workbook = _parse_xml_member(archive, "xl/workbook.xml")
    relations = _xlsx_relationships(archive)
    cells: list[SpreadsheetCell] = []
    for sheet in workbook.iter(f"{_SHEET_NS}sheet"):
        name = str(sheet.attrib.get("name", "")).strip()
        relation_id = sheet.attrib.get(f"{_DOC_REL_NS}id")
        member = relations.get(str(relation_id))
        if not name or member is None:
            raise OfficeReadingBlocked("XLSX worksheet relationship is malformed")
        sheet_root = _parse_xml_member(archive, member)
        for cell in sheet_root.iter(f"{_SHEET_NS}c"):
            coordinate = str(cell.attrib.get("r", "")).strip()
            if not coordinate:
                raise OfficeReadingBlocked("XLSX cell coordinate is missing")
            formula_node = cell.find(f"{_SHEET_NS}f")
            raw_value = cell.findtext(f"{_SHEET_NS}v", default="")
            if cell.attrib.get("t") == "s":
                try:
                    value = shared_strings[int(raw_value)]
                except (ValueError, IndexError) as error:
                    raise OfficeReadingBlocked("XLSX shared-string index is invalid") from error
            elif cell.attrib.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{_SHEET_NS}t"))
            else:
                value = raw_value
            formula = formula_node.text if formula_node is not None and formula_node.text else None
            cells.append(SpreadsheetCell(name, coordinate, value, formula))
            if len(cells) > _MAX_SPREADSHEET_CELLS:
                raise OfficeReadingBlocked("XLSX cell limit exceeded")
    return OfficeReadResult(source_sha256, "SPREADSHEET", (), (), tuple(cells))


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = _parse_xml_member(archive, "xl/sharedStrings.xml")
    values = ["".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t")) for item in root.iter(f"{_SHEET_NS}si")]
    if len(values) > _MAX_SPREADSHEET_CELLS:
        raise OfficeReadingBlocked("XLSX shared-string limit exceeded")
    return tuple(values)


def _xlsx_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    root = _parse_xml_member(archive, "xl/_rels/workbook.xml.rels")
    mappings: dict[str, str] = {}
    for relation in root:
        relation_id = str(relation.attrib.get("Id", ""))
        target = str(relation.attrib.get("Target", ""))
        relation_type = str(relation.attrib.get("Type", ""))
        if relation_type.endswith("/worksheet"):
            if not relation_id or not target or target.startswith("/") or ".." in target.split("/"):
                raise OfficeReadingBlocked("XLSX worksheet relationship is unsafe")
            mappings[relation_id] = f"xl/{target.lstrip('./')}"
    return mappings


def _parse_xml_member(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise OfficeReadingBlocked(f"Office package member is missing: {name}") from error
    if info.file_size < 1 or info.file_size > _MAX_XML_BYTES:
        raise OfficeReadingBlocked("Office XML member exceeds the reader limit")
    raw = archive.read(info)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise OfficeReadingBlocked("Office XML contains a prohibited declaration")
    return ElementTree.fromstring(raw)


def _verify_source(source: AuthorizedOriginalFile) -> None:
    if source.path.is_symlink() or not source.path.is_file() or source.path.stat().st_size != source.byte_size:
        raise OfficeReadingBlocked("authorized Office source is missing, symbolic, or changed")
    digest = sha256()
    with source.path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != source.sha256:
        raise OfficeReadingBlocked("authorized Office source hash changed during reading")
