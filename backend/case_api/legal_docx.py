"""诉讼文书排版：把结构化内容排成符合公文/诉讼文书习惯的 .docx。

为什么单独一层：交付给法院的文书与"系统界面上的说明"是两种东西。
本模块只负责**正式文书的排版与字体统一**，不掺任何系统提示语、警示语、颜色或图标。

排版基准（可复核，不随页面样式漂移）：

- 纸张 A4，页边距：上 3.7cm、下 3.5cm、左 2.8cm、右 2.6cm；
- 文书标题：宋体 22pt（二号）加粗、居中、段后 18pt；
- 一级小标题：黑体 16pt（三号），段前 12pt 段后 6pt；
- 正文：仿宋_GB2312 16pt（三号），行距固定值 28pt，首行缩进 2 字符；
- 表格：仿宋 14pt（四号），单线边框，表头加粗居中；
- 署名与日期：右对齐或按文书体例，无颜色、无下划线以外的修饰；
- 页脚页码：居中 "- 1 -"。

字体名按中文办公环境常见名称写入（仿宋_GB2312 / 宋体 / 黑体），
同时设置 w:eastAsia，确保在 Word 与 WPS 中都按中文字体渲染。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Iterable, Sequence

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

import os

# 字体默认用「宋体 / 黑体」：这两款在 Windows（SimSun/SimHei）与 macOS
# （Songti SC / Heiti SC）都有对应，Word/WPS 会自动映射，跨平台不会变成方框。
# 律所若统一要求公文的仿宋_GB2312，可用环境变量覆盖（见 docs/DELIVERABLES.md）。
BODY_FONT = os.environ.get("CASE_WORKBENCH_DOC_FONT_BODY", "宋体")
TITLE_FONT = os.environ.get("CASE_WORKBENCH_DOC_FONT_TITLE", "宋体")
HEADING_FONT = os.environ.get("CASE_WORKBENCH_DOC_FONT_HEADING", "黑体")
BODY_SIZE = Pt(16)        # 三号
TITLE_SIZE = Pt(22)       # 二号
HEADING_SIZE = Pt(16)
TABLE_SIZE = Pt(14)       # 四号
LINE_SPACING = Pt(28)     # 固定值 28 磅


@dataclass
class Paragraph:
    """一段正文。indent=False 用于"此致/法院名/署名"等不缩进的段落。"""

    text: str
    indent: bool = True
    bold: bool = False
    align: str = "left"          # left / center / right
    size: Pt | None = None


@dataclass
class Table:
    header: Sequence[str]
    rows: Sequence[Sequence[str]]
    widths: Sequence[float] | None = None


@dataclass
class Signature:
    """署名块：姓名行 + 日期行，右对齐，不缩进。"""

    lines: Sequence[str]


@dataclass
class LegalDocument:
    title: str
    blocks: list[object] = field(default_factory=list)
    footer_page_number: bool = True


def _set_font(run, font: str, size: Pt, *, bold: bool = False) -> None:
    run.font.name = font
    run.font.size = size
    run.font.bold = bold
    run.font.color.rgb = None            # 明确不设颜色：正文纯黑
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:ascii"), font)
    fonts.set(qn("w:hAnsi"), font)
    fonts.set(qn("w:eastAsia"), font)


def _style_paragraph(paragraph, *, font: str, size: Pt, indent_chars: int = 0,
                     align=WD_ALIGN_PARAGRAPH.LEFT, space_before: Pt | None = None,
                     space_after: Pt | None = None, line_spacing: Pt | None = LINE_SPACING,
                     bold: bool = False) -> None:
    fmt = paragraph.paragraph_format
    fmt.alignment = align
    if indent_chars:
        fmt.first_line_indent = Pt(size.pt * indent_chars)
    if line_spacing is not None:
        fmt.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        fmt.line_spacing = line_spacing
    if space_before is not None:
        fmt.space_before = space_before
    if space_after is not None:
        fmt.space_after = space_after
    for run in paragraph.runs:
        _set_font(run, font, size, bold=bold)


def _add_page_number_footer(section) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    _set_font(run, BODY_FONT, Pt(12))
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._element.append(fld_begin)
    run._element.append(instr)
    run._element.append(fld_end)


def _apply_page_setup(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(3.7)
    section.bottom_margin = Cm(3.5)
    section.left_margin = Cm(2.8)
    section.right_margin = Cm(2.6)


def _add_paragraph(document: Document, block: Paragraph) -> None:
    paragraph = document.add_paragraph()
    paragraph.add_run(block.text)
    align = {
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }.get(block.align, WD_ALIGN_PARAGRAPH.LEFT)
    _style_paragraph(
        paragraph, font=BODY_FONT, size=block.size or BODY_SIZE,
        indent_chars=2 if block.indent else 0, align=align, bold=block.bold,
    )
    paragraph.paragraph_format.space_after = Pt(0)


def _add_title(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.add_run(text)
    _style_paragraph(paragraph, font=TITLE_FONT, size=TITLE_SIZE,
                     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=Pt(18),
                     line_spacing=Pt(34), bold=True)


def _add_heading(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.add_run(text)
    _style_paragraph(paragraph, font=HEADING_FONT, size=HEADING_SIZE,
                     align=WD_ALIGN_PARAGRAPH.LEFT, space_before=Pt(12),
                     space_after=Pt(6))


def _set_table_borders(table) -> None:
    """显式画单线边框：不依赖模板样式，Word/WPS 都不会丢框线。"""
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "6")
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), "000000")
        borders.append(element)
    tbl_pr.append(borders)


def _apply_table_widths(table, block: Table) -> None:
    """固定表格布局：逐单元格宽度 + tblGrid + tblLayout=fixed。

    python-docx 只设置 cell.width 经常不生效（Word/WPS 会重新自动布局），
    因此这里把列宽写进 tblGrid，并把布局方式固定为 fixed。
    """
    from docx.shared import Cm as _Cm

    widths = list(block.widths or [])
    if not widths:
        return
    total = sum(widths)
    table.autofit = False

    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    table_width = OxmlElement("w:tblW")
    table_width.set(qn("w:w"), str(int(total * 567)))   # 厘米 → twips
    table_width.set(qn("w:type"), "dxa")
    tbl_pr.append(table_width)

    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        table._tbl.remove(grid)
    grid = OxmlElement("w:tblGrid")
    for width in widths:
        column = OxmlElement("w:gridCol")
        column.set(qn("w:w"), str(int(width * 567)))
        grid.append(column)
    table._tbl.insert(list(table._tbl).index(tbl_pr) + 1, grid)

    for column, width in zip(table.columns, widths):
        column.width = _Cm(width)
        for cell in column.cells:
            cell.width = _Cm(width)


def _add_table(document: Document, block: Table) -> None:
    table = document.add_table(rows=1, cols=len(block.header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_borders(table)
    header_cells = table.rows[0].cells
    for index, text in enumerate(block.header):
        header_cells[index].text = ""
        paragraph = header_cells[index].paragraphs[0]
        paragraph.add_run(str(text))
        _style_paragraph(paragraph, font=HEADING_FONT, size=TABLE_SIZE,
                         align=WD_ALIGN_PARAGRAPH.CENTER, line_spacing=Pt(20), bold=True)
    for row in block.rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = ""
            paragraph = cells[index].paragraphs[0]
            paragraph.add_run("" if value is None else str(value))
            _style_paragraph(paragraph, font=BODY_FONT, size=TABLE_SIZE,
                             line_spacing=Pt(20))
    _apply_table_widths(table, block)
    document.add_paragraph()


def _add_signature(document: Document, block: Signature) -> None:
    for line in block.lines:
        paragraph = document.add_paragraph()
        paragraph.add_run(line)
        _style_paragraph(paragraph, font=BODY_FONT, size=BODY_SIZE,
                         align=WD_ALIGN_PARAGRAPH.RIGHT, line_spacing=Pt(28))


def render_legal_document(document_spec) -> bytes:
    """把一份文书渲染成 .docx 字节。

    接受任何带 ``title`` 与 ``blocks`` 的对象（例如 matter_documents.LegalDocSpec），
    因此内容层不必依赖本层的 dataclass。
    """
    document = Document()
    _apply_page_setup(document)
    if getattr(document_spec, "footer_page_number", True):
        _add_page_number_footer(document.sections[0])
    if document_spec.title:
        _add_title(document, document_spec.title)
    for block in document_spec.blocks:
        if isinstance(block, Paragraph):
            _add_paragraph(document, block)
        elif isinstance(block, Table):
            _add_table(document, block)
        elif isinstance(block, Signature):
            _add_signature(document, block)
        else:  # 纯字符串按正文处理
            _add_paragraph(document, Paragraph(str(block)))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_documents(documents: Iterable[LegalDocument]) -> list[tuple[str, bytes]]:
    return [(spec.title, render_legal_document(spec)) for spec in documents]
