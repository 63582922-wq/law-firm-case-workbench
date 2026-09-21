from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from openpyxl import Workbook

from case_kernel.local_access_grants import AuthorizedOriginalFile
from case_kernel.office_reading_worker import OfficeReadingBlocked, read_authorized_office_document


class OfficeReadingWorkerTests(unittest.TestCase):
    def _source(self, path: Path) -> AuthorizedOriginalFile:
        return AuthorizedOriginalFile(path.name, path, path.stat().st_size, sha256(path.read_bytes()).hexdigest())

    def test_safe_docx_returns_paragraphs_and_table_cells_without_changing_source(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "答辩材料.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr(
                    "word/document.xml",
                    """<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body><w:p><w:r><w:t>借款事实</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>""",
                )
            before = path.read_bytes()
            result = read_authorized_office_document(self._source(path), detected_kind="WORD_DOCUMENT")
        self.assertEqual([item.text for item in result.paragraphs], ["借款事实", "金额"])
        self.assertEqual(result.table_cells[0].text, "金额")
        self.assertEqual(path.read_bytes() if path.exists() else before, before)

    def test_safe_xlsx_returns_shared_and_formula_cells_without_formula_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "付款台账.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr(
                    "xl/workbook.xml",
                    """<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets><sheet name=\"流水\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>""",
                )
                archive.writestr(
                    "xl/_rels/workbook.xml.rels",
                    """<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/></Relationships>""",
                )
                archive.writestr(
                    "xl/sharedStrings.xml",
                    """<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><si><t>已付利息</t></si></sst>""",
                )
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    """<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\"><f>SUM(B2:B3)</f><v>30</v></c></row></sheetData></worksheet>""",
                )
            result = read_authorized_office_document(self._source(path), detected_kind="SPREADSHEET")
        self.assertEqual([(cell.coordinate, cell.value, cell.formula) for cell in result.spreadsheet_cells], [("A1", "已付利息", None), ("B1", "30", "SUM(B2:B3)")])

    def test_macro_document_is_blocked_before_xml_reading(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "宏材料.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/vbaProject.bin", b"macro")
            with self.assertRaisesRegex(OfficeReadingBlocked, "structural inspection"):
                read_authorized_office_document(self._source(path), detected_kind="WORD_DOCUMENT")

    def test_standard_xlsx_with_package_root_relationship_is_readable(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "标准台账.xlsx"
            workbook = Workbook()
            workbook.active.title = "付款"
            workbook.active["A1"] = "已付利息"
            workbook.save(path)
            result = read_authorized_office_document(self._source(path), detected_kind="SPREADSHEET")
        self.assertEqual(result.spreadsheet_cells[0].sheet_name, "付款")
        self.assertEqual(result.spreadsheet_cells[0].value, "已付利息")


if __name__ == "__main__":
    unittest.main()
