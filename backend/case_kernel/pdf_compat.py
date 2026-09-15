"""PDF 兼容层：pypdf 打不开的 PDF（法院/当事人常见的导出件）也要能收进来。

真实案卷里常出现 pypdf 无法解析的 PDF（对象流损坏、非标准生成器导出）。
`pdfinfo`/`pdftoppm`（Poppler）通常仍能读页数并正常栅格化，因此：

- 页数：先 pypdf，失败再 `pdfinfo`，两者都失败才拒绝；
- 文本层：pypdf 失败时返回 ``None``，调用方按「无文本层」处理（进入 OCR 队列），
  绝不假装读过；
- 单页预览：pypdf 失败时用 `pdftoppm` 渲染该页为 PNG。

探测顺序与 `pdf_render` 一致：PATH → Homebrew → /usr/local → Codex 运行时缓存。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

_TOOL_DIRECTORIES = (
    "",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override"),
)

_MAX_PAGE_COUNT = 100_000


class PdfUnreadable(RuntimeError):
    """PDF 无法读取页数：调用方应明确拒绝，而不是当成 0 页材料。"""


def find_tool(name: str) -> str | None:
    """在已知目录里查找 Poppler 工具。"""
    for directory in _TOOL_DIRECTORIES:
        candidate = name if not directory else os.path.join(directory, name)
        if os.sep in candidate:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def pdf_page_count(path: str | Path) -> tuple[int, str]:
    """返回 (页数, 读取方式)。两者都失败时抛 PdfUnreadable。"""
    try:
        from pypdf import PdfReader

        with open(path, "rb") as handle:
            count = len(PdfReader(handle, strict=True).pages)
        if count < 1 or count > _MAX_PAGE_COUNT:
            raise PdfUnreadable(f"PDF 页数超出本机模式限制：{count}")
        return count, "pypdf"
    except PdfUnreadable:
        raise
    except Exception:  # noqa: BLE001 - 换 Poppler 再试
        pass

    binary = find_tool("pdfinfo")
    if binary is None:
        raise PdfUnreadable("PDF 无法解析，且本机没有 pdfinfo（Poppler）可用作后备。")
    try:
        completed = subprocess.run([binary, str(path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise PdfUnreadable(f"pdfinfo 无法读取该 PDF：{error}") from None
    match = re.search(r"^Pages:\s+(\d+)", completed.stdout, re.MULTILINE)
    if not match:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise PdfUnreadable("pdfinfo 未能给出页数：" + (detail[0] if detail else "无输出"))
    count = int(match.group(1))
    if count < 1 or count > _MAX_PAGE_COUNT:
        raise PdfUnreadable(f"PDF 页数超出本机模式限制：{count}")
    return count, "poppler"


def pdf_page_texts(path: str | Path, *, page_count: int | None = None) -> tuple[list[str] | None, str]:
    """按页返回文本层。

    真实案卷里常见「页树可读、个别页内容流损坏」的 PDF：整份丢弃会让律师
    白丢几十页证据。因此逐页提取——坏页记为空文本（下游按扫描页进入 OCR），
    其余页保留文本，并在说明里写明坏了几页。

    pypdf 连页树都读不动时返回 ``(None, 原因)``，由调用方按整份无文本层处理。
    """
    from pypdf import PdfReader

    texts: list[str] = []
    failed = 0
    try:
        with open(path, "rb") as handle:  # 句柄必须在提取期间保持打开：内容流是惰性读取的
            reader = PdfReader(handle, strict=True)
            total = len(reader.pages)
            for index in range(total):
                try:
                    text = reader.pages[index].extract_text() or ""
                except Exception:  # noqa: BLE001 - 单页内容流损坏不影响其余页
                    text = ""
                    failed += 1
                texts.append(" ".join(text.split()))
    except Exception as error:  # noqa: BLE001 - 页树都读不动：整份按无文本层
        count = page_count
        backend = "pypdf"
        if count is None:
            count, backend = pdf_page_count(path)
        return None, f"{type(error).__name__}（已用 {backend} 读出 {count} 页，按无文本层进入 OCR）"

    note = "pypdf"
    if failed:
        note = f"pypdf（{failed}/{total} 页文本层无法解析，已按扫描页进入 OCR）"
    return texts, note


def render_single_page_png(path: str | Path, page_number: int, *, dpi: int = 120) -> bytes | None:
    """用 pdftoppm 渲染单页为 PNG；不可用时返回 None（调用方给出明确错误）。"""
    binary = find_tool("pdftoppm")
    if binary is None:
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        try:
            completed = subprocess.run(
                [binary, "-f", str(page_number), "-l", str(page_number),
                 "-r", str(dpi), "-png", "-singlefile", str(path), str(prefix)],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        candidate = prefix.with_suffix(".png")
        if completed.returncode != 0 or not candidate.is_file():
            return None
        return candidate.read_bytes()
