"""Read retained synthetic PDFs with the production reader, without intake or models.

This is a local byte-reader qualification only, not a Web authorization grant,
database registration, OCR result, or Agent success receipt.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from case_kernel.local_access_grants import AuthorizedOriginalFile
from case_kernel.pdf_reading_worker import read_authorized_pdf_document


def inspect_materials(root: Path) -> dict:
    project = Path(__file__).resolve().parents[3]
    root = root.resolve(strict=True)
    if not root.is_relative_to(project / "artifacts") or root.stat().st_mode & 0o077:
        raise ValueError("private project artifact directory required")
    manifest = json.loads((root / "generation_manifest.json").read_text())
    if manifest.get("schema_version") != "golden-case-unassisted-source-draft-v1" or manifest.get("synthetic_only") is not True:
        raise ValueError("only explicit unassisted synthetic drafts are allowed")
    results, pending = [], []
    for item in manifest["files"]:
        name = item["file_name"]
        if Path(name).name != name:
            raise ValueError("source name must be a basename")
        path = root / "sources" / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root / "sources"):
            raise ValueError("source must remain inside the retained source directory")
        if path.stat().st_size > 100 * 1024**2:
            raise ValueError("source exceeds native read budget")
        before = sha256(path.read_bytes()).hexdigest()
        if before != item["file_sha256"]:
            raise ValueError("source differs from retained manifest")
        if item["media_type"] != "application/pdf":
            pending.append(dict(file_name=name, status="NOT_READ_REQUIRES_VISUAL_ADAPTER"))
            continue
        read = read_authorized_pdf_document(AuthorizedOriginalFile(name, path, path.stat().st_size, before))
        if len(read.pages) != item["page_count"]:
            raise ValueError("reader page count differs from retained manifest")
        if any("@GC_TX" in page.text or "@GC_ID" in page.text for page in read.pages):
            raise ValueError("machine-assisted fixture must not qualify")
        results.append(dict(file_name=name, source_sha256=read.source_sha256,
            pages=[dict(page_number=page.page_number, text_sha256=page.text_sha256,
                        character_count=len(page.text)) for page in read.pages]))
    return dict(scope="LOCAL_PRODUCTION_PDF_READER_ONLY", files=results, pending=pending,
                web_intake_verified=False, agent_run_executed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    print(json.dumps(inspect_materials(parser.parse_args().root), ensure_ascii=False))
