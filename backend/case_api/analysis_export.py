"""决策包导出：Markdown → DOCX（律师内部工作稿，非法院提交件）。

用 python-docx 直接渲染，不依赖外部渲染服务：决策包是内部复核候选，
不是需要精确排版和签章的法院文书。
"""

from __future__ import annotations

from io import BytesIO
import re

SUBMISSION_WARNING = "本文件为律师复核候选，不是正式法律意见，不可直接提交法院。"


def _add_markdown(document, line: str) -> None:
    from docx.shared import Pt

    stripped = line.strip()
    if not stripped:
        document.add_paragraph()
        return
    if stripped.startswith("| "):  # 表格行：以等宽段落呈现，避免误排版
        paragraph = document.add_paragraph(stripped)
        for run in paragraph.runs:
            run.font.size = Pt(9)
        return
    if stripped.startswith("### "):
        document.add_heading(stripped[4:], level=3)
        return
    if stripped.startswith("## "):
        document.add_heading(stripped[3:], level=2)
        return
    if stripped.startswith("# "):
        document.add_heading(stripped[2:], level=1)
        return
    if stripped.startswith("> "):
        paragraph = document.add_paragraph(stripped[2:])
        paragraph.style = document.styles["Intense Quote"]
        return
    if stripped.startswith("- [ ] "):
        document.add_paragraph(stripped[6:], style="List Bullet")
        return
    if stripped.startswith(("- ", "* ")):
        document.add_paragraph(stripped[2:], style="List Bullet")
        return
    if re.match(r"^\d+\.\s", stripped):
        document.add_paragraph(re.sub(r"^\d+\.\s", "", stripped), style="List Number")
        return
    document.add_paragraph(re.sub(r"\*\*(.+?)\*\*", r"\1", stripped))


def render_decision_package_docx(report_markdown: str) -> bytes:
    """把决策包 Markdown 渲染为 DOCX 字节流。"""
    from docx import Document

    document = Document()
    warning = document.add_paragraph(SUBMISSION_WARNING)
    warning.style = document.styles["Intense Quote"]
    for line in report_markdown.splitlines():
        _add_markdown(document, line)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
