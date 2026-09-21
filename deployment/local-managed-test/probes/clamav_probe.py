#!/usr/bin/env python3
"""Prove the exact Web scanner accepts clean PDF bytes and rejects EICAR."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

from case_kernel.evidence_intake_worker import ClamAvCommandScanner


def scan(scanner: ClamAvCommandScanner, path: Path) -> tuple[str, float]:
    digest = sha256(path.read_bytes()).hexdigest()
    started = monotonic()
    result = scanner.scan(path, expected_sha256=digest).result
    return result, monotonic() - started


def minimal_valid_pdf() -> bytes:
    """Return one blank page with a valid xref/trailer, not just PDF-like bytes."""

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objects = (
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n",
    )
    content = bytearray(header)
    offsets = [0]
    for item in objects:
        offsets.append(len(content))
        content.extend(item)
    xref_offset = len(content)
    content.extend(b"xref\n0 5\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(content)


def main() -> int:
    # Split the standard harmless antivirus test string so repository scanners
    # do not mistake this source file for a live malware sample.
    eicar = (
        b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
        b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )
    clean_pdf = minimal_valid_pdf()
    with tempfile.TemporaryDirectory(prefix="lawcase-clamav-") as directory:
        root = Path(directory)
        clean = root / "clean.pdf"
        infected = root / "eicar.txt"
        clean.write_bytes(clean_pdf)
        infected.write_bytes(eicar)
        pdfinfo = subprocess.run(
            ["/usr/bin/pdfinfo", str(clean)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if pdfinfo.returncode != 0:
            print("Clean scanner probe is not a structurally valid PDF", file=sys.stderr)
            return 2
        scanner = ClamAvCommandScanner(
            "/opt/lawcase-clamav/clamdscan",
            timeout_seconds=120,
        )
        clean_result, clean_seconds = scan(scanner, clean)
        infected_result, infected_seconds = scan(scanner, infected)
    if clean_result != "CLEAN":
        print("ClamAV did not accept the clean PDF probe", file=sys.stderr)
        return 2
    if infected_result != "INFECTED":
        print("ClamAV did not reject the EICAR probe with current definitions", file=sys.stderr)
        return 2
    print(
        "ClamAV definitions, valid clean PDF acceptance and EICAR rejection: PASS "
        f"(clean={clean_seconds:.3f}s eicar={infected_seconds:.3f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
