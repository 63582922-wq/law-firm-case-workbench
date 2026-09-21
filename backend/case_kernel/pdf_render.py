"""PDF 页面栅格化（供扫描版 PDF 的视觉 OCR 使用）。

法律实务中法院送达材料、银行流水常为扫描 PDF（无文本层）。要让 Agent 读到
这些材料，必须先把页面渲染为图片再走视觉 OCR。

后端探测顺序：
1. PyMuPDF（`fitz`，纯 Python 轮子，无系统依赖）；
2. `pdftoppm`（Poppler 命令行，服务器部署常见）；
3. 都没有 → 明确返回不可用原因，调用方降级并在报告中说明（不得静默假装已读）。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

RENDER_DPI = 110  # 约 1000×1400 像素/页，视觉 token 可控
MAX_RENDERED_PAGES_DEFAULT = 24

_PDFTOPPM_CANDIDATES = (
    "pdftoppm",
    "/opt/homebrew/bin/pdftoppm",
    "/usr/local/bin/pdftoppm",
)


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    path: Path


def _find_pdftoppm() -> str | None:
    for candidate in _PDFTOPPM_CANDIDATES:
        if os.sep in candidate:
            if Path(candidate).is_file():
                return candidate
        else:
            located = shutil.which(candidate)
            if located:
                return located
    return None


def renderer_backend() -> str:
    """返回可用栅格化后端名称：'pymupdf' / 'pdftoppm' / ''（不可用）。"""
    try:
        import fitz  # noqa: F401

        return "pymupdf"
    except ImportError:
        pass
    if _find_pdftoppm():
        return "pdftoppm"
    return ""


def renderer_unavailable_reason() -> str:
    return (
        "本机未安装 PDF 页面栅格化组件（PyMuPDF 或 Poppler 的 pdftoppm），"
        "扫描版 PDF 无法进入视觉 OCR；其金额与事实未被读取。"
    )


def render_pdf_pages(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    pages: list[int] | None = None,
    max_pages: int = MAX_RENDERED_PAGES_DEFAULT,
    dpi: int = RENDER_DPI,
) -> tuple[list[RenderedPage], str]:
    """把指定页（默认前 max_pages 页）渲染为 PNG。

    返回 (已渲染页面, 说明)。无可用后端时返回 ([], 原因)。
    """
    source = Path(pdf_path)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        return [], f"PDF 不存在：{source.name}"

    wanted = pages if pages is not None else None
    selected = sorted(set(wanted))[:max_pages] if wanted else None
    backend = renderer_backend()

    if backend == "pymupdf":
        import fitz

        rendered: list[RenderedPage] = []
        with fitz.open(source) as document:
            total = len(document)
            indices = selected or list(range(1, min(total, max_pages) + 1))
            for page_number in indices:
                if page_number < 1 or page_number > total:
                    continue
                page = document.load_page(page_number - 1)
                pixmap = page.get_pixmap(dpi=dpi)
                path = target_dir / f"{source.stem}_p{page_number:04d}.png"
                pixmap.save(str(path))
                rendered.append(RenderedPage(page_number, path))
        note = f"已用 PyMuPDF 渲染 {len(rendered)} 页（dpi={dpi}）"
        if selected is None and total > max_pages:
            note += f"；因上限仅渲染前 {max_pages}/{total} 页"
        return rendered, note

    if backend == "pdftoppm":
        binary = _find_pdftoppm()
        assert binary is not None
        rendered = []
        if selected is None:
            command = [binary, "-r", str(dpi), "-png", "-f", "1",
                       "-l", str(max_pages), str(source), str(target_dir / f"{source.stem}_p")]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if completed.returncode != 0:
                return [], f"pdftoppm 渲染失败：{completed.stderr.strip()[:200]}"
            for path in sorted(target_dir.glob(f"{source.stem}_p-*.png")):
                suffix = path.stem.rsplit("-", 1)[-1]
                if suffix.isdigit():
                    rendered.append(RenderedPage(int(suffix), path))
        else:
            for page_number in selected:
                command = [binary, "-r", str(dpi), "-png", "-f", str(page_number),
                           "-l", str(page_number), str(source),
                           str(target_dir / f"{source.stem}_p{page_number:04d}")]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
                if completed.returncode != 0:
                    continue
                matches = sorted(target_dir.glob(f"{source.stem}_p{page_number:04d}*.png"))
                if matches:
                    rendered.append(RenderedPage(page_number, matches[0]))
        return rendered, f"已用 pdftoppm 渲染 {len(rendered)} 页（dpi={dpi}）"

    return [], renderer_unavailable_reason()
