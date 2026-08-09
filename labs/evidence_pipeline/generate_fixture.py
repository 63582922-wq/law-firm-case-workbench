"""Create a privacy-safe PDF fixture for the evidence-page lab."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


PAGE_WIDTH, PAGE_HEIGHT = letter
CHINESE_FONT = "SyntheticChineseFixtureFont"
CHINESE_FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")


def _register_chinese_font() -> None:
    """Embed a local Unicode font so the PDF renderer can display Chinese reliably."""
    if CHINESE_FONT not in pdfmetrics.getRegisteredFontNames():
        if not CHINESE_FONT_PATH.is_file():
            raise RuntimeError(f"Chinese synthetic-fixture font not found: {CHINESE_FONT_PATH}")
        pdfmetrics.registerFont(TTFont(CHINESE_FONT, str(CHINESE_FONT_PATH)))


def _draw_page(document: canvas.Canvas, lines: list[str]) -> None:
    document.setFont(CHINESE_FONT, 16)
    document.drawString(72, 720, "合成测试材料——非客户证据")
    document.setFont(CHINESE_FONT, 12)
    y = 660
    for line in lines:
        document.drawString(72, y, line)
        y -= 36
    document.setFont(CHINESE_FONT, 10)
    document.drawString(72, 72, "本页仅用于非生产技术验证。")
    document.showPage()


def create_synthetic_fixture(destination: Path) -> Path:
    """Create five Chinese fixed pages, including one visual duplicate and one near match."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _register_chinese_font()
    document = canvas.Canvas(str(destination), pagesize=letter, pageCompression=0)
    document.setTitle("合成证据页处理实验夹具")
    document.setAuthor("律所案件 AI 工作台——合成测试夹具")

    _draw_page(
        document,
        [
            "法院送达材料",
            "本合成页面被故意设为非目标范围。",
            "编号：SYN-SERVICE-001",
        ],
    )

    target_transfer = [
        "微信转账记录",
        "交易对方昵称：合成对方甲 | 人民币 100.00 元",
        "日期：2020-08-20 | 交易单号：SYN-20200820-001",
        "用途：合成还款证据候选页",
    ]
    _draw_page(document, target_transfer)
    _draw_page(document, target_transfer)  # Exact visual duplicate of page 2.

    _draw_page(
        document,
        [
            "微信转账记录",
            "交易对方昵称：无关人员 | 人民币 88.00 元",
            "日期：2020-08-21 | 交易单号：SYN-20200821-002",
            "用途：合成无关转账",
        ],
    )

    _draw_page(
        document,
        [
            "微信转账记录",
            "交易对方昵称：合成对方申 | 人民币 120.00 元",
            "日期：2020-09-20 | 交易单号：SYN-20200920-003",
            "用途：需律师复核的合成近似名称记录",
        ],
    )
    document.save()
    return destination
