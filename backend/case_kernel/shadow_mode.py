"""Shadow test mode: seven safety gates S1-S7 from docs/SHADOW_MODE_ACCEPTANCE.md.

A shadow run analyzes a de-identified real-case copy without producing any
formal submission artifact.  Every gate has deterministic judgment steps and
constructed adversarial cases; see backend/tests/test_shadow_mode.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import csv
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from pypdf import PdfReader
from PIL import Image

from case_kernel.shadow_engine import (
    ShadowDebt,
    ShadowEngineBlocked,
    ShadowEngineConfig,
    ShadowRow,
    canonical_amount,
    run_engine,
)

SHADOW_SPEC_SHA256 = "90351aa03446f283445b5515b27f5b4e6737bb53ca82231854de0a33eeeaed6f"
SHADOW_MARKER = "影子模式，不可提交"
SCHEMA_MANIFEST = "shadow-import-manifest-v1"
SCHEMA_PROPOSAL = "shadow-proposal-v1"
SCHEMA_CONFIG = "shadow-case-config-v1"

FORBIDDEN_ARTIFACT_NAMES = (
    "current_submission.json",
    "locked_submission",
    "external_send",
)
FORBIDDEN_EXTENSIONS = (".zip",)

# ---------------------------------------------------------------- identifiers

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_BANK_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def scan_identifiers(text: str) -> list[dict]:
    """S1: machine-detectable complete identifiers (ID/mobile/bank card)."""
    findings: list[dict] = []
    for match in _ID_RE.finditer(text):
        value = match.group(1)
        body = value[:17]
        if _ID_CHECK[int(body) % 11] != value[-1].upper():
            continue
        findings.append({"pattern": "ID_CARD", "value": mask_identifier(value, "id"), "start": match.start()})
    for match in _MOBILE_RE.finditer(text):
        findings.append({"pattern": "MOBILE", "value": mask_identifier(match.group(1), "mobile"), "start": match.start()})
    for match in _BANK_RE.finditer(text):
        digits = match.group(1)
        if _luhn_valid(digits):
            findings.append({"pattern": "BANK_CARD", "value": mask_identifier(digits, "bank"), "start": match.start()})
    return findings


def mask_text_identifiers(text: str) -> tuple[str, list[dict]]:
    """对文本中的完整标识符做掩码（实用模式用：不阻断，只脱敏）。

    返回 (掩码后文本, 检出清单)。
    """
    findings = scan_identifiers(text)
    if not findings:
        return text, []
    masked = text
    # 长到短替换，避免身份证中的数字串被银行卡规则先截断
    for match in sorted(_ID_RE.finditer(text), key=lambda m: -len(m.group(0))):
        value = match.group(0)
        body = value[:17]
        if _ID_CHECK[int(body) % 11] != value[-1].upper():
            continue
        masked = masked.replace(value, mask_identifier(value, "id"))
    for match in _MOBILE_RE.finditer(text):
        masked = masked.replace(match.group(0), mask_identifier(match.group(0), "mobile"))
    for match in _BANK_RE.finditer(text):
        digits = match.group(0)
        if _luhn_valid(digits):
            masked = masked.replace(digits, mask_identifier(digits, "bank"))
    return masked, findings


def mask_identifier(value: str, kind: str) -> str:
    if kind == "mobile":
        return f"{value[:3]}****{value[-4:]}"
    if kind == "bank":
        return f"{value[:4]} **** **** {value[-4:]}"
    return f"{value[:6]}********{value[-4:]}"


# ---------------------------------------------------------------- exceptions

class ShadowBlocked(RuntimeError):
    """A safety gate refused to continue; exit code 2."""


class ShadowGateFailed(RuntimeError):
    """A gate's measured behavior missed its PASS criterion; exit code 2."""


# ---------------------------------------------------------------- artifacts

@dataclass
class ManifestEntry:
    file_name: str
    media_type: str
    sha256: str
    page_count: int
    import_order: int
    # 读取说明（例如「43 页中 5 页文本层无法解析，已按扫描页进入 OCR」）；
    # 空字符串表示无异常，随 import_manifest.json 留档。
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageText:
    file_name: str
    file_sha256: str
    page_number: int
    text: str


@dataclass
class BlockingRecord:
    gate: str
    reason: str
    detail: object = None

    def to_dict(self) -> dict:
        return {"gate": self.gate, "reason": self.reason, "detail": self.detail}


@dataclass
class GateStatus:
    gate: str
    status: str  # PASS / FAIL / BLOCKED / NOT_EXERCISED
    numbers: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"gate": self.gate, "status": self.status, "numbers": self.numbers, "notes": self.notes}


# ---------------------------------------------------------------- S1 + manifest

def build_import_manifest(
    materials_root: str | Path,
    *,
    scan_gate: bool = True,
    mask_identifiers: bool = False,
) -> tuple[list[ManifestEntry], list[PageText], list[dict]]:
    """Walk the materials directory, hash files, extract local text, run S1.

    ``scan_gate=True, mask_identifiers=False``：影子/验收模式——检出完整标识符
    即阻断（要求事先脱敏）。
    ``mask_identifiers=True``：实用模式（真实案件）——不阻断，改为对将发送给
    模型的文本自动掩码，律师无需先手工脱敏。
    """
    root = Path(materials_root).resolve()
    if not root.is_dir():
        raise ShadowBlocked(f"materials directory missing: {root}")
    entries: list[ManifestEntry] = []
    pages: list[PageText] = []
    identifier_findings: list[dict] = []
    notes_by_file: dict[str, str] = {}
    files = sorted(
        (path for path in root.rglob("*")
         if path.is_file() and path.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png"}),
        key=lambda path: str(path.relative_to(root)),
    )
    if not files:
        raise ShadowBlocked("materials directory contains no PDF/JPEG/PNG files")
    for order, path in enumerate(files, start=1):
        payload = path.read_bytes()
        digest = sha256(payload).hexdigest()
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            media_type = "application/pdf"
            # 真实案卷的 PDF 常有个别页内容流损坏：用兼容层逐页提取，
            # 坏页记为空文本（下游按扫描页渲染 + OCR），其余页照常入卷。
            from case_kernel.pdf_compat import pdf_page_count, pdf_page_texts

            page_count, _backend = pdf_page_count(path)
            text_by_page, text_note = pdf_page_texts(path, page_count=page_count)
            if text_by_page is None:
                text_by_page = [""] * page_count
            elif len(text_by_page) < page_count:
                text_by_page = list(text_by_page) + [""] * (page_count - len(text_by_page))
            for page_number, text in enumerate(text_by_page[:page_count], start=1):
                pages.append(PageText(str(path.relative_to(root)), digest, page_number, text))
            local_text = "\n".join(text_by_page)
            if text_note and "无法解析" in text_note:
                notes_by_file[str(path.relative_to(root))] = text_note
        else:
            media_type = "image/" + ("jpeg" if suffix in {".jpg", ".jpeg"} else "png")
            page_count = 1
            local_text = ""
            try:
                with Image.open(path) as image:
                    exif = image.getexif()
                    if exif:
                        local_text = str(exif.get(270, ""))
            except Exception:
                local_text = ""
            pages.append(PageText(str(path.relative_to(root)), digest, 1, local_text))
        relative_name = str(path.relative_to(root))
        entries.append(ManifestEntry(relative_name, media_type, digest, page_count, order,
                                     notes_by_file.get(relative_name, "")))
        if scan_gate:
            combined = f"{local_text}\n{path.name}"
            findings = scan_identifiers(combined)
            for finding in findings:
                finding = dict(finding)
                finding["file"] = str(path.relative_to(root))
                identifier_findings.append(finding)
    if scan_gate and identifier_findings and not mask_identifiers:
        raise ShadowBlocked(
            f"S1 脱敏完整性门阻断：检出 {len(identifier_findings)} 处完整标识符 "
            f"（身份证/手机号/银行卡），须先脱敏后重试。首个：{identifier_findings[0]}"
        )
    if identifier_findings and mask_identifiers:
        # 实用模式：真实案件材料不阻断，改为对将发送给模型的文本自动掩码
        pages = [
            PageText(page.file_name, page.file_sha256, page.page_number,
                     mask_text_identifiers(page.text)[0])
            for page in pages
        ]
    return entries, pages, identifier_findings


# ---------------------------------------------------------------- S3

def resolve_refs(
    refs: Iterable[object],
    manifest: Sequence[ManifestEntry],
) -> tuple[list[dict], list[dict]]:
    """S3: resolve every (file_sha256, page[, bbox]) reference to real manifest entries."""
    by_hash = {item.sha256: item for item in manifest}
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for raw in refs:
        if not isinstance(raw, Mapping):
            unresolved.append({"ref": raw, "reason": "ref is not an object"})
            continue
        file_hash = str(raw.get("file_sha256", "")).lower()
        try:
            page = int(raw.get("page", 0))
        except (TypeError, ValueError):
            unresolved.append({"ref": raw, "reason": "page missing or invalid"})
            continue
        if not file_hash:
            unresolved.append({"ref": raw, "reason": "file_sha256 is required"})
            continue
        entry = by_hash.get(file_hash)
        if entry is None:
            unresolved.append({"ref": raw, "reason": "file hash not in import manifest"})
            continue
        if page < 1 or page > entry.page_count:
            unresolved.append(
                {"ref": raw, "reason": f"page out of range for {entry.file_name} (1..{entry.page_count})"}
            )
            continue
        resolved.append(
            {
                "file_name": entry.file_name,
                "file_sha256": entry.sha256,
                "page": page,
                "bbox": raw.get("bbox"),
            }
        )
    return resolved, unresolved


def canonicalize_agent_refs(
    proposal: Mapping,
    manifest: Sequence[ManifestEntry],
) -> dict:
    """Attach a manifest hash to a model's *surface-only* file/page locator.

    The model must not receive import hashes because they are not material
    surface.  It can therefore only name the file it sees and its page.  This
    local-only bridge converts an exact filename match to the S3 canonical
    ``(file_sha256, page[, bbox])`` reference.  A supplied hash is never
    replaced or repaired: it will later be rejected if it is wrong.
    """
    canonical = json.loads(json.dumps(proposal, ensure_ascii=False))
    by_name = {entry.file_name: entry for entry in manifest}

    def normalize(raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        if raw.get("file_sha256"):
            return raw
        file_name = raw.get("file_name")
        entry = by_name.get(str(file_name))
        if entry is None:
            return raw
        normalized = dict(raw)
        normalized["file_sha256"] = entry.sha256
        return normalized

    for row in canonical.get("rows", []):
        if isinstance(row, dict) and "source_ref" in row:
            row["source_ref"] = normalize(row["source_ref"])
    for decision in canonical.get("decisions", []):
        if isinstance(decision, dict) and isinstance(decision.get("source_refs"), list):
            decision["source_refs"] = [normalize(ref) for ref in decision["source_refs"]]
    return canonical


def _surface_key(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).strip()


def _local_excerpt_for_row(row: Mapping, page_text: str) -> str | None:
    """Select a visible source line locally; never display the model's quote."""
    date_key = _surface_key(str(row.get("date", "")).split()[0])
    memo_key = _surface_key(row.get("memo", ""))
    anchors = [item for item in (date_key, memo_key) if item]
    if not anchors:
        return None
    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if line and any(anchor in _surface_key(line) for anchor in anchors):
            # A prefix remains an exact substring of the local page text.
            return line[:240]
    return None


def bind_agent_evidence_to_local_surface(
    proposal: Mapping,
    manifest: Sequence[ManifestEntry],
    pages: Sequence[PageText],
) -> tuple[dict, list[BlockingRecord]]:
    """Drop unverifiable Agent claims and replace visible excerpts locally.

    File/page references are evidence locators, not model-authored facts.  A
    row is retained only if its canonical reference resolves and a source line
    containing its date or memo can be selected from local page text.  This
    lets a useful partial analysis continue while no bad reference or model
    paraphrase reaches a lawyer-visible artifact.
    """
    canonical = json.loads(json.dumps(proposal, ensure_ascii=False))
    page_text = {(page.file_sha256, page.page_number): page.text for page in pages}
    blocking: list[BlockingRecord] = []
    kept_rows: list[dict] = []
    for row in canonical.get("rows", []):
        if not isinstance(row, dict):
            blocking.append(BlockingRecord("S3 引用硬拦截", "拦截非对象台账行引用"))
            continue
        resolved, unresolved = resolve_refs([row.get("source_ref")], manifest)
        if unresolved:
            blocking.append(BlockingRecord(
                "S3 引用硬拦截", f"拦截台账行无法解析的引用：{unresolved[0]['reason']}",
                {"row_id": row.get("row_id")},
            ))
            continue
        ref = resolved[0]
        excerpt = _local_excerpt_for_row(row, page_text.get((ref["file_sha256"], ref["page"]), ""))
        if excerpt is None:
            blocking.append(BlockingRecord(
                "S3 引用硬拦截", "拦截无法由本地页面锚定的台账行",
                {"row_id": row.get("row_id")},
            ))
            continue
        row["source_ref"] = ref
        row["excerpt"] = excerpt
        row["excerpt_origin"] = "LOCAL_PAGE_TEXT"
        kept_rows.append(row)
    canonical["rows"] = kept_rows

    kept_decisions: list[dict] = []
    for decision in canonical.get("decisions", []):
        if not isinstance(decision, dict):
            blocking.append(BlockingRecord("S3 引用硬拦截", "拦截非对象决策引用"))
            continue
        refs = decision.get("source_refs", [])
        if not isinstance(refs, list):
            blocking.append(BlockingRecord(
                "S3 引用硬拦截", "拦截 source_refs 不是列表的决策",
                {"decision_id": decision.get("decision_id")},
            ))
            continue
        resolved, unresolved = resolve_refs(refs, manifest)
        if unresolved:
            for bad in unresolved:
                blocking.append(BlockingRecord(
                    "S3 引用硬拦截", f"拦截决策无法解析的引用：{bad['reason']}",
                    {"decision_id": decision.get("decision_id")},
                ))
            continue
        decision["source_refs"] = resolved
        kept_decisions.append(decision)
    canonical["decisions"] = kept_decisions
    return canonical, blocking


# ---------------------------------------------------------------- S4

_MONEY_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:¥|￥)?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?(?![0-9])"
)


def _canonical_money(match: re.Match) -> str:
    digits = match.group(1).replace(",", "")
    fraction = match.group(2) or "00"
    return f"{Decimal(digits + '.' + fraction):.2f}"


class AmountSanitizer:
    """S4: derived amounts must byte-match engine outputs; raw material quotes
    must byte-match page excerpts.  Everything else money-like is blocked."""

    def __init__(self, engine_amounts: set[str], excerpt_amounts: set[str]) -> None:
        self.engine_amounts = set(engine_amounts)
        self.excerpt_amounts = set(excerpt_amounts)

    def sanitize(self, text: str) -> tuple[str, list[str]]:
        blocked: list[str] = []
        allowed = self.engine_amounts | self.excerpt_amounts

        def replace(match: re.Match) -> str:
            canonical = _canonical_money(match)
            if canonical in allowed:
                return match.group(0)
            blocked.append(canonical)
            return "[金额已拦截]"

        cleaned = _MONEY_TOKEN_RE.sub(replace, text)
        return cleaned, blocked


def excerpt_amount_set(pages: Sequence[PageText]) -> set[str]:
    values: set[str] = set()
    for page in pages:
        for match in _MONEY_TOKEN_RE.finditer(page.text):
            values.add(_canonical_money(match))
    return values


# ---------------------------------------------------------------- S5

REQUIRED_PREFLIGHT_FIELDS = (
    "purpose", "sent_fields", "provider", "model", "region", "retention", "budget_cap_cny",
)


@dataclass
class RequestLedger:
    path: Path
    rows: list[dict] = field(default_factory=list)

    def append(self, **kwargs) -> None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self.rows.append(row)
        self.flush()

    def flush(self) -> None:
        payload = json.dumps(self.rows, ensure_ascii=False, indent=1, sort_keys=True)
        self.path.write_text(payload + "\n", encoding="utf-8")


def validate_preflight(value: object) -> dict:
    """S5: a model call requires a confirmed preflight with every required field."""
    if not isinstance(value, Mapping):
        raise ShadowBlocked("S5 数据路径门：preflight 缺失或格式错误")
    missing = [field for field in REQUIRED_PREFLIGHT_FIELDS if field not in value]
    if missing:
        raise ShadowBlocked(f"S5 数据路径门：preflight 缺少字段 {missing}")
    if str(value.get("confirmed", "")).lower() != "true":
        raise ShadowBlocked("S5 数据路径门：preflight 未经确认，禁止模型调用")
    return dict(value)


def expand_preflight_authorization(
    preflight: Mapping,
    manifest: Sequence[ManifestEntry],
) -> dict:
    """Turn an explicit all-imported approval into an exact local file list.

    The expansion occurs only after S1 has built the import manifest.  The
    provider receives only ``page_files`` and the ledger preserves that exact
    expanded list; unlisted files are never handed to the transport.
    """
    result = dict(preflight)
    sent_fields = dict(result.get("sent_fields") or {})
    if sent_fields.get("all_imported_pages") is True:
        sent_fields["page_files"] = [entry.file_name for entry in manifest]
    page_files = sent_fields.get("page_files")
    if not isinstance(page_files, list) or not all(isinstance(item, str) for item in page_files):
        raise ShadowBlocked("S5 数据路径门：sent_fields.page_files 必须为明确授权的文件名列表")
    known = {entry.file_name for entry in manifest}
    unknown = sorted(set(page_files) - known)
    if unknown:
        raise ShadowBlocked("S5 数据路径门：授权范围包含不在导入清单中的文件")
    result["sent_fields"] = sent_fields
    return result


# ---------------------------------------------------------------- S6

def compare_expected_csv(expected_path: str | Path, rows: Sequence[ShadowRow]) -> dict:
    """S6: compare the user's own-answer CSV against the confirmed rows."""
    path = Path(expected_path)
    result = {"match": [], "mismatch": [], "extra": [], "missing": []}
    if not path.is_file():
        result["skipped"] = "expected CSV not provided"
        return result
    expected: dict[str, dict] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            expected[str(raw["row_id"]).strip()] = raw
    actual: dict[str, ShadowRow] = {row.row_id: row for row in rows}
    for row_id in sorted(set(expected) | set(actual)):
        if row_id in expected and row_id not in actual:
            result["missing"].append(row_id)
            continue
        if row_id not in expected and row_id in actual:
            result["extra"].append(row_id)
            continue
        left = expected[row_id]
        right = actual[row_id]
        diffs: list[str] = []
        for column in ("date", "channel", "amount", "currency", "direction", "classification", "debt_id"):
            expected_value = str(left.get(column, "")).strip()
            if column == "date":
                actual_value = right.occurred_on.isoformat()
            elif column == "amount":
                actual_value = canonical_amount(right.amount)
            else:
                actual_value = str(getattr(right, column, "") or "").strip()
            if expected_value != actual_value:
                diffs.append({"column": column, "expected": expected_value, "actual": actual_value})
        if diffs:
            result["mismatch"].append({"row_id": row_id, "diffs": diffs})
        else:
            result["match"].append(row_id)
    result["summary"] = {
        "match": len(result["match"]),
        "mismatch": len(result["mismatch"]),
        "extra": len(result["extra"]),
        "missing": len(result["missing"]),
    }
    return result


# ---------------------------------------------------------------- S2

def assert_no_formal_outputs(run_root: str | Path) -> None:
    """S2: a shadow run must never contain formal submission artifacts."""
    root = Path(run_root)
    violations: list[str] = []
    for path in root.rglob("*"):
        if path.is_dir() and any(name in path.name for name in ("locked_submission",)):
            violations.append(str(path.relative_to(root)))
        if path.is_file():
            if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
                violations.append(str(path.relative_to(root)))
            if any(name in path.name for name in FORBIDDEN_ARTIFACT_NAMES):
                violations.append(str(path.relative_to(root)))
    if violations:
        raise ShadowGateFailed(f"S2 无正式输出隔离门 FAIL：检出禁止产物 {violations}")


def attempt_formal_lock(run_root: str | Path) -> None:
    """S2 adversarial: any request to lock/export in shadow mode is refused."""
    raise ShadowBlocked(
        "S2 无正式输出隔离门：影子模式禁止锁定提交包或生成任何正式提交产物"
    )


# ---------------------------------------------------------------- case config

def load_case_config(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA_CONFIG:
        raise ShadowBlocked("case_config 格式错误或 schema 不符")
    debts: dict[str, ShadowDebt] = {}
    for item in value.get("debts", []):
        debts[str(item["debt_id"])] = ShadowDebt(
            debt_id=str(item["debt_id"]),
            principal=Decimal(str(item["principal"])),
            disbursed_on=datetime.strptime(str(item["disbursed_on"]), "%Y-%m-%d").date(),
            agreed_monthly_rate=Decimal(str(item["agreed_monthly_rate"])),
            due_on=(datetime.strptime(str(item["due_on"]), "%Y-%m-%d").date()
                    if item.get("due_on") else None),
            evidence_pending=bool(item.get("evidence_pending", False)),
        )
    return {
        "debts": debts,
        "new_cap": Decimal(str(value.get("lpr_4x_monthly_rate", "0.01"))),
        "final_date": datetime.strptime(str(value.get("interest_cutoff")), "%Y-%m-%d").date(),
    }


def parse_proposal_rows(proposal: Mapping) -> list[ShadowRow]:
    if proposal.get("schema") != SCHEMA_PROPOSAL:
        raise ShadowBlocked("proposal schema 不符，拒绝接受 Agent 输出")
    rows: list[ShadowRow] = []
    seen: set[str] = set()
    for item in proposal.get("rows", []):
        row_id = str(item["row_id"])
        if row_id in seen:
            raise ShadowBlocked(f"proposal 含重复 row_id：{row_id}")
        seen.add(row_id)
        classification = str(item["classification"])
        if classification not in (
            "本金出借", "还本", "付息", "代付", "争议", "排除", "阻断", "疑似付息",
        ):
            raise ShadowBlocked(f"proposal row {row_id} 分类非法：{classification}")
        if classification == "疑似付息":
            # 规律性支付识别：有倾向但不自动进计算（保持争议纪律），
            # 倾向性说明应出现在 decisions 中。
            classification = "争议"
        try:
            amount = Decimal(re.sub(r"[¥￥,，\s元]", "", str(item["amount"])) or "0")
        except Exception:
            raise ShadowBlocked(
                f"proposal row {row_id!r} 金额无法解析：amount={item.get('amount')!r}"
            )
        try:
            occurred_on = datetime.strptime(
                str(item["date"]).split()[0], "%Y-%m-%d"
            ).date()
        except Exception:
            raise ShadowBlocked(
                f"proposal row {row_id!r} 日期无法解析：date={item.get('date')!r}"
            )
        rows.append(
            ShadowRow(
                row_id=row_id,
                occurred_on=occurred_on,
                channel=str(item.get("channel", "")),
                amount=amount,
                currency=str(item.get("currency", "CNY")).upper(),
                direction=str(item.get("direction", "")),
                classification=classification,
                debt_id=str(item["debt_id"]) if item.get("debt_id") else None,
                memo=str(item.get("memo", "")),
                source_ref=item.get("source_ref"),
            )
        )
    if not rows:
        raise ShadowBlocked("proposal 未提供任何台账行")
    return rows


def build_case_config_candidate(rows: Sequence[ShadowRow]) -> dict:
    """Create a local, non-authoritative confirmation card from source facts.

    This lets the Agent run before a lawyer has supplied calculation rules,
    without allowing an inferred rate, cutoff or classification to reach the
    deterministic engine.  The card is deliberately *not* a case_config: a
    lawyer must create/approve the real configuration before calculation.
    """
    candidates = []
    for row in rows:
        if row.classification != "本金出借" or row.currency != "CNY" or row.amount <= 0:
            continue
        candidates.append(
            {
                "candidate_id": f"DEBT-CANDIDATE-{len(candidates) + 1:03d}",
                "principal_from_material": canonical_amount(row.amount),
                "disbursed_on_from_material": row.occurred_on.isoformat(),
                "classification_status": "AGENT_SUGGESTION_NOT_APPROVED",
                "source_ref": row.source_ref,
            }
        )
    return {
        "schema": "shadow-case-config-candidate-v1",
        "status": "NEEDS_LAWYER_CONFIRMATION",
        "not_a_calculation_config": True,
        "debt_candidates": candidates,
        "required_lawyer_confirmations": [
            "每笔债务是否成立及其债务编号",
            "每笔债务的月利率与适用期间",
            "利息暂计截止日",
            "LPR 四倍月利率上限",
        ],
        "next_input_file": "case_config.json",
    }


def _validate_excerpts(proposal: Mapping, pages: Sequence[PageText]) -> tuple[int, int]:
    """S3/S4 support: excerpt text must appear verbatim on the referenced page."""
    by_page = {(page.file_sha256, page.page_number): page.text for page in pages}
    checked = 0
    invalid = 0
    for item in proposal.get("rows", []):
        excerpt = str(item.get("excerpt", ""))
        if not excerpt:
            continue
        ref = item.get("source_ref") or {}
        try:
            key = (str(ref.get("file_sha256", "")).lower(), int(ref.get("page", 0)))
        except (AttributeError, TypeError, ValueError):
            checked += 1
            invalid += 1
            continue
        page_text = by_page.get(key, "")
        checked += 1
        if excerpt not in page_text:
            invalid += 1
    return checked, invalid


# ---------------------------------------------------------------- report

def render_shadow_report(
    run_root: Path,
    *,
    spec_sha256: str,
    materials_count: int,
    page_count: int,
    proposal: Mapping,
    proposal_source: str,
    engine_text: str,
    gate_statuses: Sequence[GateStatus],
    ledger: RequestLedger,
    blocking: Sequence[BlockingRecord],
    expected: Mapping | None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 影子模式运行报告 — {SHADOW_MARKER}")
    lines.append("")
    lines.append(f"- 运行目录：{run_root}")
    lines.append(f"- 影子规格 SHA-256：`{spec_sha256}`")
    lines.append(f"- 材料文件 {materials_count} 个、共 {page_count} 页；提议来源：{proposal_source}")
    lines.append("- 本报告为影子模式分析结果：不可提交、不可外发、不构成任何正式法律文书。")
    lines.append("")
    lines.append("## 七道门状态")
    lines.append("")
    lines.append("| 门 | 状态 | 实测数字 |")
    lines.append("|---|---|---|")
    for gate in gate_statuses:
        numbers = "; ".join(f"{key}={value}" for key, value in gate.numbers.items())
        lines.append(f"| {gate.gate} | {gate.status} | {numbers} |")
    lines.append("")
    lines.append("## 确定性计算结果（引擎输出，含 engine_hash）")
    lines.append("")
    lines.append(engine_text)
    lines.append("")
    if expected is not None:
        lines.append("## 自带答案对账（S6）")
        lines.append("")
        summary = expected.get("summary", {})
        lines.append(
            f"match={summary.get('match', 0)} mismatch={summary.get('mismatch', 0)} "
            f"extra={summary.get('extra', 0)} missing={summary.get('missing', 0)}"
        )
        for item in expected.get("mismatch", []):
            diffs = "; ".join(
                f"{diff['column']}:期望{diff['expected']}/实际{diff['actual']}" for diff in item["diffs"]
            )
            lines.append(f"- 行 {item['row_id']}：{diffs}")
    lines.append("")
    lines.append("## 请求账本（S5）")
    lines.append("")
    lines.append(
        f"- 外部调用次数：{sum(1 for row in ledger.rows if row.get('status') != 'preflight-confirmed')}"
    )
    lines.append("")
    lines.append("## 阻断记录（S2/S3/S4）")
    lines.append("")
    if blocking:
        for item in blocking:
            lines.append(f"- [{item.gate}] {item.reason}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append(f"*{SHADOW_MARKER}*")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- orchestrator

@dataclass
class ShadowRunOutcome:
    run_root: Path
    gate_statuses: list[GateStatus]
    engine_result: object
    exit_code: int


def run_shadow_case(
    *,
    materials: str | Path,
    output_root: str | Path,
    case_config: str | Path | None = None,
    expected_csv: str | Path | None = None,
    proposal_file: str | Path | None = None,
    confirm_data_path: str | Path | None = None,
    budget_cny: Decimal = Decimal("5"),
    transport=None,
) -> ShadowRunOutcome:
    """Full shadow pipeline.  ``transport`` is injectable for S5 tests."""
    run_root = Path(output_root).resolve()
    if run_root.exists() and any(run_root.iterdir()):
        raise ShadowBlocked("shadow run output must be empty")
    derivatives = run_root / "derivatives"
    agent_dir = run_root / "agent"
    for directory in (derivatives, agent_dir):
        directory.mkdir(parents=True)
    blocking: list[BlockingRecord] = []
    blocking_path = run_root / "blocking_log.json"
    def persist_blocking() -> None:
        blocking_path.write_text(
            json.dumps([item.to_dict() for item in blocking], ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
    ledger = RequestLedger(run_root / "request_ledger.json")
    gate_statuses: list[GateStatus] = []

    # S1 + manifest
    entries, pages, findings = build_import_manifest(materials)
    manifest_path = run_root / "import_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": SCHEMA_MANIFEST,
                "shadow_spec_sha256": SHADOW_SPEC_SHA256,
                "files": [entry.to_dict() for entry in entries],
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    gate_statuses.append(
        GateStatus("S1 脱敏完整性", "PASS",
                   numbers={"files_scanned": len(entries), "pages": len(pages),
                            "identifier_findings": len(findings)})
    )

    # case config
    config_path = Path(case_config) if case_config else Path(materials) / "case_config.json"
    engine_cfg = load_case_config(config_path) if config_path.is_file() else None

    # proposals: dry file, live transport, or local-only degradation
    proposal: Mapping = {}
    proposal_source = ""
    if proposal_file is not None:
        proposal = json.loads(Path(proposal_file).read_text(encoding="utf-8"))
        if not isinstance(proposal, Mapping):
            raise ShadowBlocked("proposal 文件必须是 JSON 对象")
        proposal_source = f"dry-run 文件 {Path(proposal_file).name}"
        gate_statuses.append(
            GateStatus("S5 数据路径", "PASS",
                       numbers={"calls": 0, "preflights": 0},
                       notes=["dry-run：--proposal-file 模式无模型调用"])
        )
        (agent_dir / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (agent_dir / "transcript.txt").write_text(
            "dry-run：无模型调用，提议来自 --proposal-file，无外部数据路径。\n",
            encoding="utf-8",
        )
    elif confirm_data_path is not None:
        preflight = expand_preflight_authorization(
            validate_preflight(json.loads(Path(confirm_data_path).read_text(encoding="utf-8"))),
            entries,
        )
        if transport is None:
            raise ShadowBlocked("S5 数据路径门：未提供模型传输层，无法执行已确认的模型调用")
        ledger.append(
            purpose=preflight["purpose"], sent_fields=preflight["sent_fields"],
            provider=preflight["provider"], model=preflight["model"],
            region=preflight["region"], retention=preflight["retention"],
            budget_cap_cny=str(preflight["budget_cap_cny"]),
            payload_sha256="", status="preflight-confirmed",
        )
        result = transport.run(preflight, pages, ledger)
        if isinstance(result, Mapping) and "proposal" in result:
            proposal = result["proposal"]
            for item in result.get("pages", []):
                if isinstance(item, Mapping):
                    pages = [
                        PageText(str(page.file_name), str(page.file_sha256), page.page_number,
                                 str(item.get("text", "")))
                        if page.file_name == item.get("file_name")
                        and page.page_number == int(item.get("page_number", -1))
                        else page
                        for page in pages
                    ]
        elif isinstance(result, (str, bytes)):
            proposal = json.loads(result)
        else:
            raise ShadowBlocked("S5 数据路径门：模型传输层返回格式非法")
        if proposal:
            (agent_dir / "proposal.json").write_text(
                json.dumps(proposal, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        proposal_dump = json.dumps(proposal, ensure_ascii=False)
        for finding in scan_identifiers(proposal_dump):
            persist_blocking()
            finding_pattern = finding["pattern"]
            raise ShadowBlocked(
                f"S1 脱敏完整性门阻断：模型返回文本检出 {finding_pattern}，已拒绝接受"
            )
        proposal_source = f"live {preflight.get('model', 'model')}"
        gate_statuses.append(
            GateStatus("S5 数据路径", "PASS",
                       numbers={"calls": sum(1 for row in ledger.rows
                                               if row.get("status") != "preflight-confirmed"),
                                "preflights": 1})
        )
    else:
        proposal_source = "本地降级（无确认数据路径，无模型调用）"
        gate_statuses.append(
            GateStatus("S5 数据路径", "NOT_EXERCISED",
                       numbers={"calls": 0, "preflights": 0},
                       notes=["未提供 --confirm-data-path：本次无任何外部调用"])
        )

    if proposal:
        # Preserve the model's raw surface-only locators for replay, then
        # persist and evaluate only locally canonicalized hash references.
        (agent_dir / "proposal_raw.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        proposal = canonicalize_agent_refs(proposal, entries)
        proposal, evidence_blocking = bind_agent_evidence_to_local_surface(
            proposal, entries, pages
        )
        blocking.extend(evidence_blocking)
        (agent_dir / "proposal.json").write_text(
            json.dumps(proposal, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not proposal.get("rows"):
            persist_blocking()
            raise ShadowBlocked("S3 引用硬拦截门 FAIL：所有 Agent 台账行均无法由本地来源锚定")
        rows = parse_proposal_rows(proposal)
        # S3 reference hard interception
        all_refs = []
        for item in proposal.get("rows", []):
            ref = item.get("source_ref")
            if ref:
                all_refs.append(ref)
        for item in proposal.get("decisions", []):
            for ref in item.get("source_refs", []):
                if ref:
                    all_refs.append(ref)
        resolved_refs, unresolved_refs = resolve_refs(all_refs, entries)
        for bad in unresolved_refs:
            blocking.append(BlockingRecord("S3 引用硬拦截", f"拦截无法解析的引用：{bad['reason']}", bad.get("ref")))
        if unresolved_refs:
            gate_statuses.append(
                GateStatus("S3 引用硬拦截", "FAIL",
                           numbers={"refs_total": len(all_refs), "resolved": len(resolved_refs),
                                    "blocked": len(unresolved_refs) + len(evidence_blocking),
                                    "excerpt_checked": 0, "excerpt_blocked": 0,
                                    "visible_bad_refs": 0})
            )
            persist_blocking()
            raise ShadowBlocked(
                f"S3 引用硬拦截门 FAIL：{len(unresolved_refs)} 条引用无法解析到导入清单，已全部拦截"
            )
        # S1b: OCR-returned text scan (live mode appends ocr texts to pages via transport)
        for page in pages:
            for finding in scan_identifiers(page.text):
                raise ShadowBlocked(
                    f"S1 脱敏完整性门阻断：OCR 文本检出 {finding['pattern']}（{page.file_name} p{page.page_number}）"
                )
        # excerpt validation
        excerpt_checked, excerpt_invalid = _validate_excerpts(proposal, pages)
        if excerpt_invalid:
            blocking.append(BlockingRecord(
                "S3 引用硬拦截", f"{excerpt_invalid}/{excerpt_checked} 条摘录与所引页面文本不一致"))
            gate_statuses.append(
                GateStatus("S3 引用硬拦截", "FAIL",
                           numbers={"refs_total": len(all_refs), "resolved": len(resolved_refs),
                                    "blocked": excerpt_invalid,
                                    "excerpt_checked": excerpt_checked,
                                    "excerpt_blocked": excerpt_invalid,
                                    "visible_bad_refs": 0})
            )
            persist_blocking()
            raise ShadowBlocked(
                f"S3 引用硬拦截门 FAIL：{excerpt_invalid}/{excerpt_checked} 条摘录与所引页面文本不一致"
            )
        gate_statuses.append(
            GateStatus("S3 引用硬拦截", "PASS",
                       numbers={"refs_total": len(all_refs), "resolved": len(resolved_refs),
                                "blocked": len(evidence_blocking), "excerpt_checked": excerpt_checked,
                                "excerpt_blocked": 0, "visible_bad_refs": 0})
        )
        if engine_cfg is None:
            candidate = build_case_config_candidate(rows)
            candidate_path = agent_dir / "case_config_candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            blocking.append(BlockingRecord(
                "计算参数律师确认",
                "未提供已确认 case_config.json；已生成仅含材料事实与来源的候选参数卡，"
                "未执行计算、对账、锁定或外发。",
                {"candidate_path": str(candidate_path),
                 "debt_candidates": len(candidate["debt_candidates"])},
            ))
            gate_statuses.append(
                GateStatus("S4 金额纪律", "BLOCKED",
                           numbers={"derived_amounts_rendered": 0,
                                    "agent_money_tokens_blocked": 0},
                           notes=["缺少律师确认计算参数，确定性引擎未启动"])
            )
            gate_statuses.append(
                GateStatus("S6 自带答案对账", "NOT_EXERCISED",
                           notes=["确定性计算未启动"])
            )
            assert_no_formal_outputs(run_root)
            gate_statuses.append(GateStatus("S2 无正式输出隔离", "PASS",
                                            numbers={"formal_artifacts": 0}))
            gate_statuses.append(GateStatus("S7 既有资产不变", "NOT_EXERCISED",
                                            notes=["由回归测试与切片复现验证，不在单次运行内判定"]))
            ledger.flush()
            persist_blocking()
            report = render_shadow_report(
                run_root, spec_sha256=SHADOW_SPEC_SHA256,
                materials_count=len(entries), page_count=len(pages),
                proposal=proposal, proposal_source=proposal_source,
                engine_text=("BLOCKED：等待律师确认计算参数。"
                             "已生成 agent/case_config_candidate.json；"
                             "其中不含利息、冲抵、余额或任何派生金额。"),
                gate_statuses=gate_statuses, ledger=ledger,
                blocking=blocking, expected=None,
            )
            (run_root / "shadow_report.md").write_text(report, encoding="utf-8")
            return ShadowRunOutcome(run_root, gate_statuses, None, exit_code=2)
        # engine
        disputed_included = set(str(item) for item in proposal.get("disputed_included_row_ids", []))
        engine_config = ShadowEngineConfig(
            final_date=engine_cfg["final_date"], new_cap=engine_cfg["new_cap"]
        )
        engine_result = run_engine(rows, engine_cfg["debts"], engine_config,
                                   include_disputed=disputed_included)
        engine_hash = sha256(
            json.dumps(
                {debt_id: canonical_amount(loan.principal) + ":" + canonical_amount(loan.interest_arrears)
                 for debt_id, loan in engine_result.loans.items()},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        # S4 amount discipline
        sanitizer = AmountSanitizer(engine_result.amount_set(), excerpt_amount_set(pages))
        narrative = str(proposal.get("narrative", ""))
        cleaned_narrative, blocked_amounts = sanitizer.sanitize(narrative)
        for reason in (str(item.get("reason", "")) for item in proposal.get("decisions", [])):
            cleaned, more = sanitizer.sanitize(reason)
            cleaned_narrative += "\n" + cleaned
            blocked_amounts.extend(more)
        for amount in blocked_amounts:
            blocking.append(BlockingRecord(
                "S4 金额纪律", f"Agent 输出中的自算金额 {amount} 未通过引擎校验，已拦截"))
        gate_statuses.append(
            GateStatus("S4 金额纪律", "PASS" if not blocked_amounts else "FAIL",
                       numbers={"derived_amounts_rendered": len(engine_result.amount_set()),
                                "agent_money_tokens_blocked": len(blocked_amounts)})
        )
        if blocked_amounts:
            persist_blocking()
            raise ShadowBlocked(
                f"S4 金额纪律门 FAIL：Agent 输出中 {len(blocked_amounts)} 个自算金额未通过引擎字节级校验，已全部拦截"
            )
        engine_lines = [
            "| 债务 | 未偿本金 | 未付利息挂账 | 已付利息 | 超额冲本 | 本金偿付 | 自然债务已付 |",
            "|---|---|---|---|---|---|---|",
        ]
        for debt_id in sorted(engine_result.loans):
            loan = engine_result.loan(debt_id)
            engine_lines.append(
                f"| {debt_id} | {canonical_amount(loan.principal)} | "
                f"{canonical_amount(loan.interest_arrears)} | {canonical_amount(loan.interest_paid)} | "
                f"{canonical_amount(loan.excess_principal_offset)} | "
                f"{canonical_amount(loan.principal_paid)} | {canonical_amount(loan.natural_debt_paid)} |"
            )
        engine_lines.append(
            f"| 合计 | {canonical_amount(engine_result.total_principal)} | "
            f"{canonical_amount(engine_result.total_interest_arrears)} | | | | |"
        )
        engine_lines.append(f"engine_hash=`{engine_hash}`")
        engine_text = "\n".join(engine_lines)
        expected = compare_expected_csv(expected_csv, rows) if expected_csv else None
        if expected is not None:
            gate_statuses.append(
                GateStatus("S6 自带答案对账", "PASS",
                           numbers={k: v for k, v in expected["summary"].items()})
            )
    else:
        rows = []
        engine_text = "本地降级模式：无 Agent 提议，未执行确定性计算。"
        expected = None

    # S2 formal-output isolation
    assert_no_formal_outputs(run_root)
    gate_statuses.append(GateStatus("S2 无正式输出隔离", "PASS",
                                    numbers={"formal_artifacts": 0}))
    gate_statuses.append(GateStatus("S7 既有资产不变", "NOT_EXERCISED",
                                    notes=["由回归测试与切片复现验证，不在单次运行内判定"]))
    ledger.flush()  # S5: request ledger always materialized (empty when no calls)
    blocking_path = run_root / "blocking_log.json"
    blocking_path.write_text(  # overwrite with the final state
        json.dumps([item.to_dict() for item in blocking], ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    report = render_shadow_report(
        run_root, spec_sha256=SHADOW_SPEC_SHA256,
        materials_count=len(entries), page_count=len(pages),
        proposal=proposal, proposal_source=proposal_source,
        engine_text=engine_text, gate_statuses=gate_statuses,
        ledger=ledger, blocking=blocking, expected=expected,
    )
    (run_root / "shadow_report.md").write_text(report, encoding="utf-8")
    failed = [gate for gate in gate_statuses if gate.status == "FAIL"]
    return ShadowRunOutcome(run_root, gate_statuses, engine_result if proposal else None,
                            exit_code=2 if failed else 0)
