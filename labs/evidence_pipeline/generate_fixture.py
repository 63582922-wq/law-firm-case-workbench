"""Create a privacy-safe PDF fixture for the evidence-page lab."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


PAGE_WIDTH, PAGE_HEIGHT = letter


def _draw_page(document: canvas.Canvas, lines: list[str]) -> None:
    document.setFont("Helvetica-Bold", 16)
    document.drawString(72, 720, "SYNTHETIC TEST FIXTURE — NOT CLIENT EVIDENCE")
    document.setFont("Helvetica", 12)
    y = 660
    for line in lines:
        document.drawString(72, y, line)
        y -= 36
    document.setFont("Helvetica-Oblique", 10)
    document.drawString(72, 72, "Generated solely for a non-production technical validation.")
    document.showPage()


def create_synthetic_fixture(destination: Path) -> Path:
    """Create five fixed pages, including one visual duplicate and one near match."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(destination), pagesize=letter, pageCompression=0)
    document.setTitle("Synthetic Evidence Page Lab Fixture")
    document.setAuthor("Law Firm Case Workbench — Test Fixture")

    _draw_page(
        document,
        [
            "COURT SERVICE NOTICE",
            "This synthetic page is deliberately outside the target scope.",
            "Reference: SYN-SERVICE-001",
        ],
    )

    target_transfer = [
        "WECHAT TRANSFER RECORD",
        "Counterparty Alias: Sample Claimant | CNY 100.00",
        "Date: 2020-08-20 | Transaction ID: SYN-20200820-001",
        "Purpose: synthetic repayment evidence candidate",
    ]
    _draw_page(document, target_transfer)
    _draw_page(document, target_transfer)  # Exact visual duplicate of page 2.

    _draw_page(
        document,
        [
            "WECHAT TRANSFER RECORD",
            "Counterparty Alias: Unrelated Party | CNY 88.00",
            "Date: 2020-08-21 | Transaction ID: SYN-20200821-002",
            "Purpose: synthetic unrelated transfer",
        ],
    )

    _draw_page(
        document,
        [
            "WECHAT TRANSFER RECORD",
            "Counterparty Alias: Sample Clamant | CNY 120.00",
            "Date: 2020-09-20 | Transaction ID: SYN-20200920-003",
            "Purpose: synthetic near-match requiring lawyer review",
        ],
    )
    document.save()
    return destination
