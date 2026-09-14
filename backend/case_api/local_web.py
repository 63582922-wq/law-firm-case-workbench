"""Single-user, browser-local Web runtime.

This is intentionally separate from the firm-managed Web composition.  It is
for one lawyer on one computer: SQLite and an immutable private material
directory are enough to receive and inspect PDFs.  It does not silently
become a multi-user service and it does not invent legal facts, calculations,
or court-ready submission files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from typing import AsyncIterable, Mapping
from uuid import UUID, uuid4
import zipfile

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader, PdfWriter


MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_EXPANDED_BYTES = 1_073_741_824
SESSION_COOKIE = "lawcase_local_session"
CSRF_COOKIE = "lawcase_local_csrf"
SESSION_TTL = timedelta(hours=8)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")


class LocalWebBlocked(RuntimeError):
    """A local browser operation cannot be completed safely."""


class LocalWebNotFound(LocalWebBlocked):
    pass


class LocalWebConflict(LocalWebBlocked):
    pass


@dataclass(frozen=True)
class LocalWebIdentity:
    actor_id: str
    workspace_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _private_root(root: Path) -> Path:
    if not root.is_absolute():
        raise LocalWebBlocked("本机工作区必须使用绝对路径。")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise LocalWebBlocked("本机工作区不能是符号链接。")
    try:
        os.chmod(root, 0o700)
    except OSError as error:
        raise LocalWebBlocked("无法保护本机工作区。") from error
    materials = root / "materials"
    materials.mkdir(exist_ok=True)
    if materials.is_symlink():
        raise LocalWebBlocked("本机材料目录不能是符号链接。")
    os.chmod(materials, 0o700)
    return root


def default_local_root(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CASE_WORKBENCH_LOCAL_WEB_ROOT", "").strip()
    if configured:
        return Path(configured)
    if os.name == "nt":
        base = Path(environ.get("LOCALAPPDATA", str(Path.home())))
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "LawcaseWorkbench" / "local-web"


class LocalWebStore:
    """SQLite metadata plus immutable local material bytes."""

    def __init__(self, root: Path):
        self.root = _private_root(root)
        self.db_path = self.root / "local-web.sqlite3"
        self.materials = self.root / "materials"
        self._lock = Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    actor_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    command_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    PRIMARY KEY(command_name, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS materials (
                    material_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(case_id),
                    display_name TEXT NOT NULL,
                    sha256 TEXT,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
                    media_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    storage_name TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS pages (
                    page_id TEXT PRIMARY KEY,
                    material_id TEXT NOT NULL REFERENCES materials(material_id),
                    page_number INTEGER NOT NULL CHECK (page_number >= 1),
                    decision TEXT,
                    decision_reason TEXT,
                    annotation_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS locks (
                    case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
                    manifest_id TEXT NOT NULL,
                    locked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
                    source_version INTEGER NOT NULL CHECK (source_version >= 1),
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                );
                """
            )
            if db.execute("SELECT 1 FROM identity WHERE singleton = 1").fetchone() is None:
                db.execute(
                    "INSERT INTO identity(singleton, actor_id, workspace_id, created_at) VALUES(1,?,?,?)",
                    (str(uuid4()), str(uuid4()), _iso(_now())),
                )
        os.chmod(self.db_path, 0o600)

    @property
    def identity(self) -> LocalWebIdentity:
        with self._connect() as db:
            row = db.execute("SELECT actor_id, workspace_id FROM identity WHERE singleton = 1").fetchone()
        if row is None:
            raise LocalWebBlocked("本机会话身份未初始化。")
        return LocalWebIdentity(str(row["actor_id"]), str(row["workspace_id"]))

    def issue_session(self) -> tuple[str, str, datetime]:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        expires = _now() + SESSION_TTL
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO sessions(token_hash, csrf_token_hash, created_at, expires_at) VALUES(?,?,?,?)",
                (_digest(token), _digest(csrf), _iso(_now()), _iso(expires)),
            )
        return token, csrf, expires

    def validate_session(self, token: str | None, csrf: str | None, *, write: bool) -> None:
        if not token:
            raise LocalWebBlocked("本机会话不存在。")
        with self._connect() as db:
            row = db.execute("SELECT csrf_token_hash, expires_at FROM sessions WHERE token_hash = ?", (_digest(token),)).fetchone()
        if row is None or datetime.fromisoformat(str(row["expires_at"])) <= _now():
            raise LocalWebBlocked("本机会话已到期。")
        if write and (not csrf or not _same(_digest(csrf), str(row["csrf_token_hash"]))):
            raise LocalWebBlocked("本机写入请求缺少有效确认标记。")

    def list_cases(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.case_id, c.title, c.version, c.updated_at,
                   (SELECT COUNT(*) FROM materials m WHERE m.case_id=c.case_id AND m.state='COMPLETED') AS material_count
                   FROM cases c ORDER BY c.updated_at DESC, c.case_id DESC"""
            ).fetchall()
        return [self._case_projection(row) for row in rows]

    def create_case(self, title: str, key: str) -> dict[str, object]:
        title = " ".join(title.strip().split())
        if len(title) < 2 or len(title) > 160:
            raise LocalWebBlocked("案件名称须为 2 至 160 个可见字符。")
        key = _idempotency(key)
        payload = _digest(title)
        with self._lock, self._connect() as db:
            prior = db.execute("SELECT payload_hash, object_id FROM commands WHERE command_name='CREATE_CASE' AND idempotency_key=?", (key,)).fetchone()
            if prior:
                if not _same(str(prior["payload_hash"]), payload):
                    raise LocalWebConflict("相同请求编号不能提交不同案件名称。")
                row = db.execute("SELECT * FROM cases WHERE case_id=?", (str(prior["object_id"]),)).fetchone()
                if row is None:
                    raise LocalWebBlocked("建案回执已损坏，请检查本机工作区。")
                return self._case_projection(row)
            case_id = str(uuid4())
            now = _iso(_now())
            db.execute("INSERT INTO cases(case_id,title,version,created_at,updated_at) VALUES(?,?,1,?,?)", (case_id, title, now, now))
            db.execute("INSERT INTO commands(command_name,idempotency_key,payload_hash,object_id) VALUES('CREATE_CASE',?,?,?)", (key, payload, case_id))
            return self._case_projection(db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())

    def create_upload(self, case_id: str, name: str, expected_version: int, key: str, kind: str) -> dict[str, object]:
        case = self._case(case_id)
        if case["version"] != expected_version:
            raise LocalWebConflict("案件已变化，请刷新后再接收材料。")
        if kind == "PDF" and not name.lower().endswith(".pdf"):
            raise LocalWebBlocked("本地模式只接收 PDF 文件。")
        if kind == "ZIP" and not name.lower().endswith(".zip"):
            raise LocalWebBlocked("本地模式只接收 ZIP 材料包。")
        key = _idempotency(key)
        payload = _digest(json.dumps([case_id, name, expected_version, kind], ensure_ascii=False))
        with self._lock, self._connect() as db:
            prior = db.execute("SELECT payload_hash, object_id FROM commands WHERE command_name=? AND idempotency_key=?", (f"CREATE_UPLOAD_{kind}", key)).fetchone()
            if prior:
                if not _same(str(prior["payload_hash"]), payload):
                    raise LocalWebConflict("相同请求编号不能提交不同材料。")
                return {"upload_id": str(prior["object_id"]), "expires_at": _iso(_now() + timedelta(hours=1))}
            material_id = str(uuid4())
            now = _iso(_now())
            db.execute("INSERT INTO materials(material_id,case_id,display_name,byte_size,media_type,state,created_at) VALUES(?,?,?,?,?,?,?)", (material_id, case_id, name[:240], 0, "application/pdf" if kind == "PDF" else "application/zip", "RECEIVING", now))
            db.execute("INSERT INTO commands(command_name,idempotency_key,payload_hash,object_id) VALUES(?,?,?,?)", (f"CREATE_UPLOAD_{kind}", key, payload, material_id))
        return {"upload_id": material_id, "expires_at": _iso(_now() + timedelta(hours=1))}

    async def accept_pdf(self, case_id: str, material_id: str, chunks: AsyncIterable[bytes]) -> dict[str, object]:
        material = self._material(case_id, material_id)
        staging = self.materials / f".{material_id}.upload"
        digest = sha256()
        size = 0
        try:
            with staging.open("wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise LocalWebBlocked("材料数据格式无效。")
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise LocalWebBlocked("PDF 超过本机模式的 256 MiB 限制。")
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise LocalWebBlocked("不能接收空文件。")
            with staging.open("rb") as source:
                reader = PdfReader(source, strict=True)
                page_count = len(reader.pages)
                if page_count < 1 or page_count > 100_000:
                    raise LocalWebBlocked("PDF 页数不符合本机模式限制。")
            content_hash = digest.hexdigest()
            storage_name = f"{content_hash}.pdf"
            stored = self.materials / storage_name
            if stored.exists() and stored.is_symlink():
                raise LocalWebBlocked("本机材料对象不能是符号链接。")
            if not stored.exists():
                staging.replace(stored)
                os.chmod(stored, 0o600)
            else:
                staging.unlink(missing_ok=True)
            with self._lock, self._connect() as db:
                row = db.execute("SELECT state FROM materials WHERE material_id=? AND case_id=?", (material_id, case_id)).fetchone()
                if row is None:
                    raise LocalWebNotFound("材料接收位不存在。")
                if str(row["state"]) == "COMPLETED":
                    return self._receipt(db, case_id, material_id)
                db.execute("UPDATE materials SET sha256=?, byte_size=?, page_count=?, state='COMPLETED', storage_name=?, completed_at=? WHERE material_id=?", (content_hash, size, page_count, storage_name, _iso(_now()), material_id))
                for number in range(1, page_count + 1):
                    db.execute("INSERT INTO pages(page_id,material_id,page_number) VALUES(?,?,?)", (str(uuid4()), material_id, number))
                db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?", (_iso(_now()), case_id))
                return self._receipt(db, case_id, material_id)
        finally:
            staging.unlink(missing_ok=True)

    async def accept_archive(self, case_id: str, material_id: str, chunks: AsyncIterable[bytes]) -> dict[str, object]:
        material = self._material(case_id, material_id)
        if str(material["media_type"]) != "application/zip":
            raise LocalWebBlocked("材料接收类型不匹配。")
        staging = self.materials / f".{material_id}.upload"
        digest = sha256()
        size = 0
        try:
            with staging.open("wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise LocalWebBlocked("材料数据格式无效。")
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise LocalWebBlocked("ZIP 超过本机模式的 256 MiB 限制。")
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise LocalWebBlocked("不能接收空文件。")
            entries = 0
            expanded = 0
            with zipfile.ZipFile(staging) as archive:
                for info in archive.infolist():
                    entries += 1
                    if entries > MAX_ARCHIVE_ENTRIES:
                        raise LocalWebBlocked("ZIP 文件数量超过本机模式限制。")
                    expanded += int(info.file_size)
                    if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise LocalWebBlocked("ZIP 解压后大小超过本机模式限制。")
                    name = info.filename.replace("\\", "/")
                    parts = [part for part in name.split("/") if part]
                    if name.startswith("/") or any(part in {".", ".."} for part in parts):
                        raise LocalWebBlocked("ZIP 内含不安全路径。")
                    # ZIP symlinks are not extracted or followed in local mode.
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        raise LocalWebBlocked("ZIP 内含符号链接，未接收。")
            content_hash = digest.hexdigest()
            storage_name = f"{content_hash}.zip"
            stored = self.materials / storage_name
            if stored.exists() and stored.is_symlink():
                raise LocalWebBlocked("本机材料对象不能是符号链接。")
            if not stored.exists():
                staging.replace(stored)
                os.chmod(stored, 0o600)
            else:
                staging.unlink(missing_ok=True)
            with self._lock, self._connect() as db:
                row = self._material_row(db, case_id, material_id)
                if str(row["state"]) == "ARCHIVE_STORED":
                    return self._archive_receipt(db, case_id, material_id)
                db.execute("UPDATE materials SET sha256=?, byte_size=?, state='ARCHIVE_STORED', storage_name=?, completed_at=? WHERE material_id=?", (content_hash, size, storage_name, _iso(_now()), material_id))
                db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?", (_iso(_now()), case_id))
                return self._archive_receipt(db, case_id, material_id, entry_count=entries, expanded_byte_size=expanded)
        except zipfile.BadZipFile as error:
            raise LocalWebBlocked("ZIP 文件无法通过结构校验。") from error
        finally:
            staging.unlink(missing_ok=True)

    def status(self, case_id: str, material_id: str) -> dict[str, object]:
        with self._connect() as db:
            row = self._material_row(db, case_id, material_id)
            if str(row["media_type"]) == "application/zip":
                if str(row["state"]) == "ARCHIVE_STORED":
                    return {"operation_id": material_id, "kind": "ZIP", "state": "STORED_PENDING_PROCESSING", "receipt": self._archive_receipt(db, case_id, material_id)}
                return {"operation_id": material_id, "kind": "ZIP", "state": "PROCESSING", "receipt": None}
            if str(row["state"]) == "COMPLETED":
                return {"operation_id": material_id, "kind": "PDF", "state": "COMPLETED", "receipt": self._receipt(db, case_id, material_id)}
            return {"operation_id": material_id, "kind": "PDF", "state": "PROCESSING", "receipt": None}

    def summary(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            files = db.execute("SELECT * FROM materials WHERE case_id=? AND state='COMPLETED' ORDER BY created_at", (case_id,)).fetchall()
            total = db.execute("SELECT COUNT(*) AS n FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE m.case_id=? AND m.state='COMPLETED'", (case_id,)).fetchone()["n"]
            unresolved = db.execute("SELECT COUNT(*) AS n FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE m.case_id=? AND m.state='COMPLETED' AND p.decision IS NULL", (case_id,)).fetchone()["n"]
        readiness = _digest(json.dumps([case_id, case["version"], total, unresolved]))
        return {"matter_id": case_id, "matter_version": case["version"], "total_pages": int(total), "unresolved_page_count": int(unresolved), "pending_decision_count": 0, "unresolved_duplicate_count": 0, "manifest_readiness_hash": readiness, "original_files": [{"evidence_file_id": str(row["material_id"]), "original_label": str(row["display_name"]), "byte_size": int(row["byte_size"]), "media_type": str(row["media_type"]), "page_count": int(row["page_count"])} for row in files], "locked_manifest": self._lock_projection(case_id), "derivatives": [], "derivative_runs": []}

    def pages(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            rows = db.execute("SELECT p.*, m.display_name, m.material_id FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE m.case_id=? AND m.state='COMPLETED' ORDER BY m.created_at, p.page_number", (case_id,)).fetchall()
        return {"matter_id": case_id, "matter_version": case["version"], "total_count": len(rows), "items": [self._page_projection(row) for row in rows], "next_cursor": None, "has_more": False}

    def decide(self, case_id: str, page_id: str, disposition: str, reason: str) -> dict[str, object]:
        if disposition not in {"INCLUDE", "EXCLUDE"}:
            raise LocalWebBlocked("页面处理方式无效。")
        with self._lock, self._connect() as db:
            self._page_row(db, case_id, page_id)
            db.execute("UPDATE pages SET decision=?, decision_reason=? WHERE page_id=?", (disposition, reason[:500], page_id))
            db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?", (_iso(_now()), case_id))
            version = int(db.execute("SELECT version FROM cases WHERE case_id=?", (case_id,)).fetchone()["version"])
        return {"command_name": "LOCAL_PAGE_DECISION", "matter_id": case_id, "matter_version": version, "audit_event_id": str(uuid4()), "object_type": "PAGE", "object_id": page_id}

    def annotate(self, case_id: str, page_id: str, box: dict[str, object]) -> dict[str, object]:
        for key in ("x0", "y0", "x1", "y1"):
            if not isinstance(box.get(key), (int, float)) or not 0 <= float(box[key]) <= 1:
                raise LocalWebBlocked("红框坐标不在页面范围内。")
        with self._lock, self._connect() as db:
            row = self._page_row(db, case_id, page_id)
            annotations = json.loads(str(row["annotation_json"]))
            annotations.append({"annotation_id": str(uuid4()), "purpose": "EVIDENCE_SCOPE", **box, "label": str(box.get("label", "证据位置"))[:200], "status": "APPROVED"})
            db.execute("UPDATE pages SET annotation_json=? WHERE page_id=?", (json.dumps(annotations, ensure_ascii=False), page_id))
            db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?", (_iso(_now()), case_id))
            version = int(db.execute("SELECT version FROM cases WHERE case_id=?", (case_id,)).fetchone()["version"])
        return {"command_name": "LOCAL_PAGE_ANNOTATION", "matter_id": case_id, "matter_version": version, "audit_event_id": str(uuid4()), "object_type": "PAGE", "object_id": page_id}

    def lock_manifest(self, case_id: str) -> dict[str, object]:
        summary = self.summary(case_id)
        if summary["unresolved_page_count"]:
            raise LocalWebConflict("仍有页面未作纳入或排除决定。")
        manifest_id = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO locks(case_id,manifest_id,locked_at) VALUES(?,?,?)", (case_id, manifest_id, _iso(_now())))
            db.execute("UPDATE cases SET version=version+1,updated_at=? WHERE case_id=?", (_iso(_now()), case_id))
            version = int(db.execute("SELECT version FROM cases WHERE case_id=?", (case_id,)).fetchone()["version"])
        return {"command_name": "LOCAL_EVIDENCE_LOCK", "matter_id": case_id, "matter_version": version, "audit_event_id": str(uuid4()), "object_type": "MANIFEST", "object_id": manifest_id}

    def read_page_pdf(self, case_id: str, page_id: str) -> bytes:
        with self._connect() as db:
            row = self._page_row(db, case_id, page_id)
            material = self._material_row(db, case_id, str(row["material_id"]))
        storage = self.materials / str(material["storage_name"])
        if not storage.is_file() or storage.is_symlink():
            raise LocalWebBlocked("本机材料原件不可用。")
        reader = PdfReader(storage, strict=True)
        writer = PdfWriter()
        writer.add_page(reader.pages[int(row["page_number"]) - 1])
        from io import BytesIO
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    def read_analysis(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT source_version, result_json FROM analysis_runs WHERE case_id=?",
                (case_id,),
            ).fetchone()
        if row is None:
            return {"status": "NOT_RUN", "analysis": None}
        if int(row["source_version"]) != int(case["version"]):
            return {"status": "STALE", "analysis": None}
        return {"status": "COMPLETED", "analysis": json.loads(str(row["result_json"]))}

    def run_analysis(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            materials = db.execute("SELECT material_id, display_name, storage_name, page_count, sha256 FROM materials WHERE case_id=? AND state='COMPLETED' AND media_type='application/pdf' ORDER BY created_at", (case_id,)).fetchall()
            page_rows = db.execute(
                """SELECT p.page_id, p.material_id, p.page_number
                   FROM pages p
                   JOIN materials m ON m.material_id=p.material_id
                   WHERE m.case_id=? AND m.state='COMPLETED'
                   ORDER BY m.created_at, p.page_number""",
                (case_id,),
            ).fetchall()
        page_ids = {
            (str(row["material_id"]), int(row["page_number"])): str(row["page_id"])
            for row in page_rows
        }
        candidates: list[dict[str, object]] = []
        files: list[dict[str, object]] = []
        text_pages = 0
        scanned_pages = 0
        signal_counts: dict[str, int] = {}
        for material in materials:
            storage = self.materials / str(material["storage_name"])
            file_text_pages = 0
            file_candidates = 0
            try:
                reader = PdfReader(storage, strict=True)
                for page_number, page in enumerate(reader.pages, start=1):
                    text = " ".join((page.extract_text() or "").split())
                    if text:
                        file_text_pages += 1
                        text_pages += 1
                    else:
                        scanned_pages += 1
                    signals = _material_signals(text)
                    for signal in signals:
                        signal_counts[signal] = signal_counts.get(signal, 0) + 1
                    amounts = _find_amounts(text)
                    dates = _find_dates(text)
                    needs_ocr = not text
                    if signals or amounts or dates or needs_ocr:
                        file_candidates += 1
                        source_text = text[:280] if text else "本页没有可提取文字，可能是扫描件；需要视觉/OCR服务复核。"
                        candidate_seed = f"{material['sha256']}:{page_number}:{source_text}"
                        evidence_page_id = page_ids.get((str(material["material_id"]), page_number))
                        if evidence_page_id is None:
                            raise LocalWebBlocked("材料页面台账不完整，不能建立可追溯候选。")
                        candidates.append({
                            "candidate_id": _digest(candidate_seed)[:24],
                            "evidence_page_id": evidence_page_id,
                            "kind": "OCR_REQUIRED" if needs_ocr else ("PAYMENT_OR_CASE_SIGNAL" if any(item in {"还款", "转账", "支付", "收款"} for item in signals) else "TEXT_REVIEW"),
                            "status": "CANDIDATE",
                            "source_file": str(material["display_name"]),
                            "source_sha256": str(material["sha256"]),
                            "page_number": page_number,
                            "signals": signals,
                            "dates": dates[:8],
                            "amounts": amounts[:8],
                            "snippet": source_text,
                            "human_action": "请律师在页面预览中核对；候选不会自动进入正式案件事实或交易台账。",
                        })
            except Exception as error:
                raise LocalWebBlocked("本机材料分析未完成；原始 PDF 未被改写。") from error
            files.append({"material_id": str(material["material_id"]), "display_name": str(material["display_name"]), "page_count": int(material["page_count"]), "text_layer_pages": file_text_pages, "candidate_page_count": file_candidates})
        result = {
            "analysis_id": str(uuid4()),
            "mode": "LOCAL_DETERMINISTIC_TEXT_REVIEW",
            "status": "COMPLETED",
            "source_version": int(case["version"]),
            "generated_at": _iso(_now()),
            "summary": {"file_count": len(files), "page_count": sum(item["page_count"] for item in files), "text_layer_pages": text_pages, "scanned_pages": scanned_pages, "candidate_count": len(candidates), "signal_counts": signal_counts},
            "files": files,
            "candidates": candidates,
            "limitations": ["本机模式只分析 PDF 文本层，不调用外部视觉/OCR模型。", "候选日期、金额和关键词需要律师回到页面核对。", "分析结果不会自动写入正式事实、诉请、交易或利息台账。"],
        }
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO analysis_runs(case_id,source_version,status,result_json,result_hash,generated_at) VALUES(?,?,?,?,?,?)", (case_id, int(case["version"]), "COMPLETED", encoded, _digest(encoded), result["generated_at"]))
        return {"status": "COMPLETED", "analysis": result}

    def _case(self, case_id: str) -> sqlite3.Row:
        try:
            UUID(case_id)
        except (TypeError, ValueError):
            raise LocalWebNotFound("案件不存在。") from None
        with self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise LocalWebNotFound("案件不存在。")
        return row

    def _material(self, case_id: str, material_id: str) -> sqlite3.Row:
        with self._connect() as db:
            return self._material_row(db, case_id, material_id)

    @staticmethod
    def _material_row(db: sqlite3.Connection, case_id: str, material_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM materials WHERE material_id=? AND case_id=?", (material_id, case_id)).fetchone()
        if row is None:
            raise LocalWebNotFound("材料接收位不存在。")
        return row

    @staticmethod
    def _page_row(db: sqlite3.Connection, case_id: str, page_id: str) -> sqlite3.Row:
        row = db.execute("SELECT p.* FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE p.page_id=? AND m.case_id=?", (page_id, case_id)).fetchone()
        if row is None:
            raise LocalWebNotFound("证据页面不存在。")
        return row

    def _receipt(self, db: sqlite3.Connection, case_id: str, material_id: str) -> dict[str, object]:
        row = self._material_row(db, case_id, material_id)
        version = int(db.execute("SELECT version FROM cases WHERE case_id=?", (case_id,)).fetchone()["version"])
        return {"evidence_file_id": material_id, "display_name": str(row["display_name"]), "sha256": str(row["sha256"]), "page_count": int(row["page_count"]), "scan_status": "LOCAL_PDF_VALIDATED", "matter_version": version, "received_at": row["completed_at"]}

    def _archive_receipt(self, db: sqlite3.Connection, case_id: str, material_id: str, *, entry_count: int | None = None, expanded_byte_size: int | None = None) -> dict[str, object]:
        row = self._material_row(db, case_id, material_id)
        storage = self.materials / str(row["storage_name"])
        if entry_count is None or expanded_byte_size is None:
            entry_count = 0
            expanded_byte_size = 0
            if storage.is_file() and not storage.is_symlink():
                with zipfile.ZipFile(storage) as archive:
                    entry_count = len(archive.infolist())
                    expanded_byte_size = sum(int(item.file_size) for item in archive.infolist())
        return {"archive_id": material_id, "display_name": str(row["display_name"]), "sha256": str(row["sha256"]), "byte_size": int(row["byte_size"]), "entry_count": entry_count, "expanded_byte_size": expanded_byte_size, "processing_status": "STORED_PENDING_PROCESSING"}

    def _case_projection(self, row: sqlite3.Row) -> dict[str, object]:
        return {"case_id": str(row["case_id"]), "title": str(row["title"]), "version": int(row["version"]), "updated_at": str(row["updated_at"]), "material_count": int(row["material_count"]) if "material_count" in row.keys() else 0}

    def _page_projection(self, row: sqlite3.Row) -> dict[str, object]:
        decision = None if row["decision"] is None else {"decision_id": str(row["page_id"]), "disposition": str(row["decision"]), "reason": str(row["decision_reason"] or ""), "status": "APPROVED"}
        annotations = json.loads(str(row["annotation_json"]))
        return {"evidence_page_id": str(row["page_id"]), "evidence_file_id": str(row["material_id"]), "original_label": str(row["display_name"]), "page_number": int(row["page_number"]), "decision": decision, "pending_decision": None, "annotations": annotations}

    def _lock_projection(self, case_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT manifest_id FROM locks WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                return None
            total = db.execute("SELECT COUNT(*) AS n FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE m.case_id=? AND p.decision='INCLUDE'", (case_id,)).fetchone()["n"]
            excluded = db.execute("SELECT COUNT(*) AS n FROM pages p JOIN materials m ON m.material_id=p.material_id WHERE m.case_id=? AND p.decision='EXCLUDE'", (case_id,)).fetchone()["n"]
        return {"manifest_id": str(row["manifest_id"]), "status": "LOCKED_LOCAL", "total_pages": int(total) + int(excluded), "included_pages": int(total), "excluded_pages": int(excluded)}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseCreate(_StrictModel):
    title: str = Field(min_length=1, max_length=160)


class UploadCreate(_StrictModel):
    client_filename: str = Field(min_length=1, max_length=240)
    content_length: int | None = Field(default=None, ge=0, le=MAX_PDF_BYTES)
    content_type: str = "application/pdf"
    expected_version: int = Field(ge=1)


class EvidenceDecision(_StrictModel):
    expected_version: int = Field(ge=1)
    disposition: str
    reason: str = Field(min_length=1, max_length=500)


class EvidenceAnnotation(_StrictModel):
    expected_version: int = Field(ge=1)
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    label: str = Field(min_length=1, max_length=200)


def create_local_web_app(store: LocalWebStore | None = None) -> FastAPI:
    store = store or LocalWebStore(default_local_root(os.environ))
    app = FastAPI(title="律师办案工作台 · 本机 Web", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN"})
        return response

    @app.exception_handler(LocalWebBlocked)
    async def blocked(_: Request, exc: LocalWebBlocked):
        code = "LOCAL_WEB_CONFLICT" if isinstance(exc, LocalWebConflict) else "LOCAL_WEB_BLOCKED"
        return JSONResponse(status_code=409 if isinstance(exc, LocalWebConflict) else 422, content={"code": code, "message": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"service": "lawcase-local-web", "mode": "LOCAL_WEB", "persistence": "SQLITE_LOCAL", "external_network": "disabled", "models": "disabled"}

    def ensure_session(request: Request, response: Response, *, write: bool = False, create: bool = True) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        csrf = request.cookies.get(CSRF_COOKIE)
        if token is None:
            if not create:
                raise LocalWebBlocked("本机会话不存在。")
            token, csrf, expires = store.issue_session()
            response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False, path="/", max_age=int((expires - _now()).total_seconds()))
            response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=False, path="/", max_age=int((expires - _now()).total_seconds()))
            return
        store.validate_session(token, csrf, write=write)

    def ensure_write(request: Request, x_lawcase_csrf: str | None) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        csrf = request.cookies.get(CSRF_COOKIE)
        if not x_lawcase_csrf or csrf != x_lawcase_csrf:
            raise LocalWebBlocked("本机写入确认标记不匹配。")
        store.validate_session(token, csrf, write=True)

    @app.get("/api/local/v1/session")
    async def session(response: Response, request: Request):
        ensure_session(request, response)
        return {
            "authenticated": True,
            "actor": {"roles": ["LEAD_LAWYER"]},
            "expires_at": None,
            "capabilities": {
                "can_create_case": True,
                "can_upload_material": True,
                "can_upload_common_material": False,
                "can_review_case_posture": False,
                "can_confirm_case_posture": False,
                "can_review_evidence": True,
                "can_run_material_preprocessing": True,
                "can_run_agent": False,
                "can_confirm_fact": False,
                "can_review_facts": False,
                "can_review_legal": False,
                "can_run_calculation": False,
                "can_review_submission": False,
                "can_generate_documents": False,
            },
            # The browser renders the same product shell as the firm-managed
            # service.  This explicit marker prevents the offline SQLite
            # helper from being mistaken for the commercial runtime.
            "workspace_mode": "LOCAL_DEVELOPMENT",
        }

    @app.get("/api/local/v1/cases")
    async def cases(response: Response, request: Request):
        ensure_session(request, response)
        return {"cases": store.list_cases()}

    @app.post("/api/local/v1/cases", status_code=201)
    async def create_case(body: CaseCreate, request: Request, response: Response, x_lawcase_csrf: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        ensure_write(request, x_lawcase_csrf)
        return {"case": store.create_case(body.title, idempotency_key or "")}

    @app.post("/api/local/v1/cases/{case_id}/material-uploads", status_code=201)
    async def create_upload(case_id: UUID, body: UploadCreate, request: Request, x_lawcase_csrf: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        ensure_write(request, x_lawcase_csrf)
        if body.content_type != "application/pdf" or not str(body.client_filename).lower().endswith(".pdf"):
            raise LocalWebBlocked("本地 Web 模式当前只接收 PDF；ZIP 接收将在本地材料包版本中开放。")
        upload = store.create_upload(str(case_id), body.client_filename, body.expected_version, idempotency_key or "", "PDF")
        return {"upload": upload}

    @app.put("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}/content")
    async def accept_upload(case_id: UUID, upload_id: UUID, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/pdf":
            raise LocalWebBlocked("本地 Web 模式只接收 application/pdf。")
        return {"receipt": await store.accept_pdf(str(case_id), str(upload_id), request.stream())}

    @app.post("/api/local/v1/cases/{case_id}/material-archives", status_code=201)
    async def create_archive(case_id: UUID, body: UploadCreate, request: Request, x_lawcase_csrf: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        ensure_write(request, x_lawcase_csrf)
        if body.content_type != "application/zip" or not str(body.client_filename).lower().endswith(".zip"):
            raise LocalWebBlocked("本地 Web 模式只接收 ZIP 材料包。")
        upload = store.create_upload(str(case_id), body.client_filename, body.expected_version, idempotency_key or "", "ZIP")
        return {"upload": {"archive_id": upload["upload_id"], "expires_at": upload["expires_at"]}}

    @app.put("/api/local/v1/cases/{case_id}/material-archives/{archive_id}/content")
    async def accept_archive(case_id: UUID, archive_id: UUID, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
            raise LocalWebBlocked("本地 Web 模式只接收 application/zip。")
        return {"receipt": await store.accept_archive(str(case_id), str(archive_id), request.stream())}

    @app.get("/api/local/v1/cases/{case_id}/material-archives/{archive_id}")
    async def archive_status(case_id: UUID, archive_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return {"status": store.status(str(case_id), str(archive_id))}

    @app.get("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}")
    async def upload_status(case_id: UUID, upload_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return {"status": store.status(str(case_id), str(upload_id))}

    @app.get("/api/local/v1/cases/{case_id}/evidence-summary")
    async def evidence_summary(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        summary = store.summary(str(case_id))
        summary["can_batch_confirm_page_decisions"] = False
        return {"summary": summary}

    @app.get("/api/local/v1/cases/{case_id}/evidence-pages")
    async def evidence_pages(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return store.pages(str(case_id))

    @app.get("/api/local/v1/cases/{case_id}/evidence-pages/{page_id}/preview")
    async def page_preview(case_id: UUID, page_id: UUID, request: Request):
        ensure_session(request, Response(), create=False)
        return StreamingResponse(iter([store.read_page_pdf(str(case_id), str(page_id))]), media_type="application/pdf", headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})

    @app.post("/api/local/v1/cases/{case_id}/evidence-pages/{page_id}/decisions")
    async def decision(case_id: UUID, page_id: UUID, body: EvidenceDecision, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return {"receipt": store.decide(str(case_id), str(page_id), body.disposition, body.reason)}

    @app.post("/api/local/v1/cases/{case_id}/evidence-pages/{page_id}/annotations")
    async def annotation(case_id: UUID, page_id: UUID, body: EvidenceAnnotation, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return {"receipt": store.annotate(str(case_id), str(page_id), body.model_dump())}

    @app.post("/api/local/v1/cases/{case_id}/evidence-manifest/lock")
    async def lock(case_id: UUID, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return {"receipt": store.lock_manifest(str(case_id))}

    @app.get("/api/local/v1/cases/{case_id}/review")
    async def review(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"review": {"matter_id": str(case_id), "title": str(case["title"]), "stage": "LOCAL_MATERIALS_ONLY", "version": int(case["version"]), "snapshot_hash": _digest(json.dumps([str(case_id), int(case["version"])])), "facts": [], "claims": [], "issues": [], "transactions": []}}

    @app.get("/api/local/v1/cases/{case_id}/legal-review")
    async def legal_review(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"review": {"matter_id": str(case_id), "matter_version": int(case["version"]), "snapshot_hash": _digest(f"legal:{case_id}:{case['version']}"), "sources": [], "rule_versions": [], "legal_events": [], "fact_bindings": [], "current_bundle": None, "bundle_segments": []}}

    @app.get("/api/local/v1/cases/{case_id}/readiness")
    async def readiness(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"readiness": {"matter_id": str(case_id), "matter_version": int(case["version"]), "checks": [{"key": "local_mode", "label": "离线基础能力", "status": "READY", "detail": "可建案、接收 PDF、生成页级记录和记录人工审阅。"}, {"key": "managed_agent_runtime", "label": "完整办案 Agent 服务", "status": "BLOCKED", "detail": "当前离线开发运行时未装配受管身份、持久任务队列、视觉/OCR、联网检索、法律与文书服务。"}], "counts": {"facts": 0, "claims": 0, "transactions": 0, "candidate_items": 0, "verified_sources": 0, "approved_rules": 0}, "next_action": "当前只用于继续验证本机基础材料链；完整服务使用同一 Web 界面，但开发数据不会静默转入律所受管案卷。"}}

    @app.get("/api/local/v1/cases/{case_id}/calculations/{obligation_id}/current")
    async def calculation(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"calculation": {"matter_id": str(case_id), "matter_version": int(case["version"]), "snapshot_hash": _digest(f"calculation:{case_id}:{case['version']}"), "scenario": None, "run": None}}

    @app.get("/api/local/v1/cases/{case_id}/submission-review")
    async def submission(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"review": {"matter_id": str(case_id), "matter_version": int(case["version"]), "stage": "LOCAL_MATERIALS_ONLY", "snapshot_hash": _digest(f"submission:{case_id}:{case['version']}"), "work_products": [], "bundles": [], "current_bundle": None, "current_components": [], "current_export": None}}

    @app.get("/api/local/v1/cases/{case_id}/document-drafts")
    async def drafts(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        case = store._case(str(case_id))
        return {"matter_id": str(case_id), "matter_version": int(case["version"]), "snapshot_hash": _digest(f"drafts:{case_id}:{case['version']}"), "pairs": []}

    @app.get("/api/local/v1/cases/{case_id}/analysis")
    async def analysis(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return store.read_analysis(str(case_id))

    @app.post("/api/local/v1/cases/{case_id}/analysis")
    async def run_analysis(case_id: UUID, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return store.run_analysis(str(case_id))

    return app


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _same(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


def _idempotency(value: str) -> str:
    if not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise LocalWebBlocked("写入请求缺少有效的幂等编号。")
    return value


_SIGNAL_TERMS = ("借款", "还款", "偿还", "转账", "支付", "收款", "利息", "本金", "民间借贷", "微信", "支付宝")
_DATE_PATTERN = re.compile(r"20\d{2}[年/-]\d{1,2}(?:月|/|-)?\d{1,2}(?:日)?")
_AMOUNT_PATTERN = re.compile(r"(?:人民币|￥|¥)?\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:元|万元)")


def _material_signals(text: str) -> list[str]:
    return [term for term in _SIGNAL_TERMS if term in text]


def _find_dates(text: str) -> list[str]:
    return list(dict.fromkeys(_DATE_PATTERN.findall(text)))


def _find_amounts(text: str) -> list[str]:
    return list(dict.fromkeys(_AMOUNT_PATTERN.findall(text)))
