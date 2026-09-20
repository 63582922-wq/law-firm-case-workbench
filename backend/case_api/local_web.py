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
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from threading import Lock, Thread
from typing import AsyncIterable, Mapping
from uuid import UUID, uuid4
import zipfile

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image
from pypdf import PdfReader, PdfWriter

from case_kernel.pdf_compat import (
    PdfUnreadable,
    pdf_page_count,
    pdf_page_texts,
    render_single_page_png,
)


MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
IMAGE_MEDIA_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_EXPANDED_BYTES = 1_073_741_824
SESSION_COOKIE = "lawcase_local_session"
CSRF_COOKIE = "lawcase_local_csrf"
SESSION_TTL = timedelta(hours=8)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")


class AnalysisRunRequest(BaseModel):
    """触发 Agent 分析的可选参数。"""

    model_config = ConfigDict(extra="forbid")

    case_number: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, pattern="^(被告|原告)$")
    stage: str | None = Field(default=None, max_length=60)
    budget_cny: float | None = Field(default=None, gt=0, le=50)
    case_config: dict[str, object] | None = None
    # 扫描件以图像发送时，图像内的身份证号/银行卡号无法在本机自动脱敏。
    # 默认 false（fail closed）；律师明确授权后才继续并把检出结果记为待核。
    allow_image_identifiers: bool = False


class DeliverableStateRequest(BaseModel):
    """交付清单：案件主体信息 + 各项交付物状态（律师维护）。"""

    model_config = ConfigDict(extra="forbid")

    parties: dict[str, object]
    states: dict[str, object]


class BriefSelectionRequest(BaseModel):
    """律师在答辩状页面做出的选择（唯一立场来源）。"""

    model_config = ConfigDict(extra="forbid")

    selections: dict[str, object]


class BriefGenerateRequest(BaseModel):
    """生成答辩状草稿的可选参数。"""

    model_config = ConfigDict(extra="forbid")

    case_number: str | None = Field(default=None, max_length=120)
    budget_cny: float | None = Field(default=None, gt=0, le=50)


class AnalysisConfigRequest(BaseModel):
    """律师确认的案件计算参数（正式数字的唯一来源，模型不得写入）。"""

    model_config = ConfigDict(extra="forbid")

    case_config: dict[str, object]



def _project_root() -> Path:
    """项目根目录（backend 的上一级），用于定位默认模型环境文件。"""
    return Path(__file__).resolve().parents[2]



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
                CREATE TABLE IF NOT EXISTS deliverable_states (
                    case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
                    parties_json TEXT NOT NULL DEFAULT '{}',
                    states_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS brief_runs (
                    case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
                    selections_json TEXT NOT NULL DEFAULT '{}',
                    selections_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'NOT_RUN',
                    progress INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT '',
                    gate_level TEXT NOT NULL DEFAULT '',
                    markdown_path TEXT NOT NULL DEFAULT '',
                    cost_cny TEXT NOT NULL DEFAULT '0.000000',
                    calls INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    engine_json TEXT NOT NULL DEFAULT '{}',
                    source_version INTEGER NOT NULL DEFAULT 0,
                    config_hash TEXT NOT NULL DEFAULT '',
                    analysis_run_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    generated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            if db.execute("SELECT 1 FROM identity WHERE singleton = 1").fetchone() is None:
                db.execute(
                    "INSERT INTO identity(singleton, actor_id, workspace_id, created_at) VALUES(1,?,?,?)",
                    (str(uuid4()), str(uuid4()), _iso(_now())),
                )
            self._migrate_agent_columns(db)
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _migrate_agent_columns(db: sqlite3.Connection) -> None:
        """为 analysis_runs 增补 Agent 分析列（幂等，兼容既有数据库）。"""
        existing = {row["name"] for row in db.execute("PRAGMA table_info(analysis_runs)")}
        additions = {
            "agent_status": "TEXT NOT NULL DEFAULT 'NOT_RUN'",
            "agent_progress": "INTEGER NOT NULL DEFAULT 0",
            "agent_stage": "TEXT NOT NULL DEFAULT ''",
            "agent_gate_level": "TEXT NOT NULL DEFAULT ''",
            "agent_report_path": "TEXT NOT NULL DEFAULT ''",
            "agent_cost_cny": "TEXT NOT NULL DEFAULT '0.000000'",
            "agent_calls": "INTEGER NOT NULL DEFAULT 0",
            "agent_error": "TEXT NOT NULL DEFAULT ''",
            "agent_engine_json": "TEXT NOT NULL DEFAULT '{}'",
            "agent_source_version": "INTEGER NOT NULL DEFAULT 0",
            "agent_run_id": "TEXT NOT NULL DEFAULT ''",
            "agent_started_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if column not in existing:
                db.execute(f"ALTER TABLE analysis_runs ADD COLUMN {column} {definition}")

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

    # ------------------------------------------------ 分片上传（大文件必需）

    def _staging_path(self, material_id: str) -> Path:
        return self.materials / f".{material_id}.upload"

    def staged_size(self, case_id: str, material_id: str) -> int:
        """已接收的分片字节数：客户端据此续传，服务端据此校验偏移。"""
        material = self._material(case_id, material_id)
        if str(material["state"]) == "COMPLETED":
            return int(material["byte_size"] or 0)
        staging = self._staging_path(material_id)
        return staging.stat().st_size if staging.is_file() else 0

    def append_chunk(self, case_id: str, material_id: str, offset: int, data: bytes) -> int:
        """把一片内容追加到暂存文件；偏移不符即拒绝，绝不拼接错位数据。"""
        material = self._material(case_id, material_id)
        if str(material["state"]) == "COMPLETED":
            raise LocalWebBlocked("该材料已接收完成，无需再传分片。")
        if not data:
            raise LocalWebBlocked("分片内容为空。")
        media_type = str(material["media_type"])
        limit = MAX_IMAGE_BYTES if media_type in IMAGE_MEDIA_TYPES else MAX_PDF_BYTES
        staging = self._staging_path(material_id)
        current = staging.stat().st_size if staging.is_file() else 0
        if offset != current:
            raise LocalWebConflict(
                f"分片偏移不符：服务端已有 {current} 字节，本次从 {offset} 开始。")
        if current + len(data) > limit:
            raise LocalWebBlocked("材料超过本机模式的单份大小限制。")
        with staging.open("ab") as output:
            output.write(data)
        os.chmod(staging, 0o600)
        return current + len(data)

    def finalize_upload(self, case_id: str, material_id: str) -> dict[str, object]:
        """收尾：校验整份文件、入库并出回执（与整份 PUT 同一套校验）。"""
        material = self._material(case_id, material_id)
        media_type = str(material["media_type"])
        staging = self._staging_path(material_id)
        if str(material["state"]) == "COMPLETED":
            with self._connect() as db:
                return self._receipt(db, case_id, material_id)
        if not staging.is_file():
            raise LocalWebBlocked("尚未收到任何内容，不能收尾。")
        payload = staging.read_bytes()
        if media_type == "application/pdf":
            return self._store_pdf(case_id, material_id, payload)
        if media_type in IMAGE_MEDIA_TYPES:
            return self._store_image(case_id, material_id, payload)
        raise LocalWebBlocked("该材料类型不支持分片上传。")

    def _store_pdf(self, case_id: str, material_id: str, payload: bytes) -> dict[str, object]:
        staging = self._staging_path(material_id)
        try:
            if len(payload) == 0:
                raise LocalWebBlocked("不能接收空文件。")
            if len(payload) > MAX_PDF_BYTES:
                raise LocalWebBlocked("PDF 超过本机模式的 256 MiB 限制。")
            staging.write_bytes(payload)
            return self._finish_pdf(case_id, material_id, staging)
        finally:
            staging.unlink(missing_ok=True)

    def _store_image(self, case_id: str, material_id: str, payload: bytes) -> dict[str, object]:
        suffix = IMAGE_MEDIA_TYPES[str(self._material(case_id, material_id)["media_type"])]
        staging = self._staging_path(material_id)
        try:
            if len(payload) == 0:
                raise LocalWebBlocked("不能接收空文件。")
            if len(payload) > MAX_IMAGE_BYTES:
                raise LocalWebBlocked("图片超过本机模式的 64 MiB 限制。")
            staging.write_bytes(payload)
            with Image.open(staging) as probe:
                probe.verify()
            with Image.open(staging) as probe:
                width, height = probe.size
            if width < 1 or height < 1 or width > 20_000 or height > 20_000:
                raise LocalWebBlocked("图片尺寸不符合本机模式限制。")
            content_hash = sha256(payload).hexdigest()
            storage_name = f"{content_hash}{suffix}"
            stored = self.materials / storage_name
            if stored.exists() and stored.is_symlink():
                raise LocalWebBlocked("本机材料对象不能是符号链接。")
            if not stored.exists():
                staging.replace(stored)
                os.chmod(stored, 0o600)
            with self._lock, self._connect() as db:
                row = db.execute("SELECT state FROM materials WHERE material_id=? AND case_id=?",
                                 (material_id, case_id)).fetchone()
                if row is None:
                    raise LocalWebNotFound("材料接收位不存在。")
                if str(row["state"]) == "COMPLETED":
                    return self._receipt(db, case_id, material_id)
                db.execute(
                    "UPDATE materials SET sha256=?, byte_size=?, page_count=1, state='COMPLETED', storage_name=?, completed_at=? WHERE material_id=?",
                    (content_hash, len(payload), storage_name, _iso(_now()), material_id))
                db.execute("INSERT INTO pages(page_id,material_id,page_number) VALUES(?,?,1)",
                           (str(uuid4()), material_id))
                db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?",
                           (_iso(_now()), case_id))
                return self._receipt(db, case_id, material_id)
        finally:
            staging.unlink(missing_ok=True)

    def _finish_pdf(self, case_id: str, material_id: str, staging: Path) -> dict[str, object]:
        size = staging.stat().st_size
        try:
            page_count, _backend = pdf_page_count(staging)
        except PdfUnreadable as error:
            raise LocalWebBlocked(f"PDF 无法解析：{error}") from None
        if page_count < 1 or page_count > 100_000:
            raise LocalWebBlocked("PDF 页数不符合本机模式限制。")
        content_hash = sha256(staging.read_bytes()).hexdigest()
        storage_name = f"{content_hash}.pdf"
        stored = self.materials / storage_name
        if stored.exists() and stored.is_symlink():
            raise LocalWebBlocked("本机材料对象不能是符号链接。")
        if not stored.exists():
            staging.replace(stored)
            os.chmod(stored, 0o600)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT state FROM materials WHERE material_id=? AND case_id=?",
                             (material_id, case_id)).fetchone()
            if row is None:
                raise LocalWebNotFound("材料接收位不存在。")
            if str(row["state"]) == "COMPLETED":
                return self._receipt(db, case_id, material_id)
            db.execute(
                "UPDATE materials SET sha256=?, byte_size=?, page_count=?, state='COMPLETED', storage_name=?, completed_at=? WHERE material_id=?",
                (content_hash, size, page_count, storage_name, _iso(_now()), material_id))
            for number in range(1, page_count + 1):
                db.execute("INSERT INTO pages(page_id,material_id,page_number) VALUES(?,?,?)",
                           (str(uuid4()), material_id, number))
            db.execute("UPDATE cases SET version=version+1, updated_at=? WHERE case_id=?",
                       (_iso(_now()), case_id))
            return self._receipt(db, case_id, material_id)

    def create_upload(self, case_id: str, name: str, expected_version: int, key: str, kind: str) -> dict[str, object]:
        case = self._case(case_id)
        if case["version"] != expected_version:
            raise LocalWebConflict("案件已变化，请刷新后再接收材料。")
        if kind == "PDF" and not name.lower().endswith(".pdf"):
            raise LocalWebBlocked("本地模式只接收 PDF 文件。")
        if kind == "ZIP" and not name.lower().endswith(".zip"):
            raise LocalWebBlocked("本地模式只接收 ZIP 材料包。")
        if kind == "IMAGE" and Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise LocalWebBlocked("图片材料须为 JPG 或 PNG。")
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
            media_type = (
                "application/pdf" if kind == "PDF"
                else "application/zip" if kind == "ZIP"
                else ("image/jpeg" if Path(name).suffix.lower() in {".jpg", ".jpeg"} else "image/png")
            )
            db.execute("INSERT INTO materials(material_id,case_id,display_name,byte_size,media_type,state,created_at) VALUES(?,?,?,?,?,?,?)", (material_id, case_id, name[:240], 0, media_type, "RECEIVING", now))
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
            try:
                # 法院/当事人导出的 PDF 常让 pypdf 直接抛错；这里用 Poppler 兜底读页数，
                # 否则整整一份证据材料会被挡在门外（内容仍按无文本层进入 OCR）。
                page_count, _backend = pdf_page_count(staging)
            except PdfUnreadable as error:
                raise LocalWebBlocked(f"PDF 无法解析：{error}") from None
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

    async def accept_image(self, case_id: str, material_id: str,
                           chunks: AsyncIterable[bytes]) -> dict[str, object]:
        """接收照片/截图材料（银行流水截图、借条照片等）。"""
        material = self._material(case_id, material_id)
        if str(material["media_type"]) not in IMAGE_MEDIA_TYPES:
            raise LocalWebBlocked("材料接收类型不匹配。")
        suffix = IMAGE_MEDIA_TYPES[str(material["media_type"])]
        staging = self.materials / f".{material_id}.upload"
        digest = sha256()
        size = 0
        try:
            with staging.open("wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise LocalWebBlocked("材料数据格式无效。")
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise LocalWebBlocked("图片超过本机模式的 64 MiB 限制。")
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise LocalWebBlocked("不能接收空文件。")
            try:
                with Image.open(staging) as probe:
                    probe.verify()
                with Image.open(staging) as probe:
                    width, height = probe.size
            except Exception:
                raise LocalWebBlocked("图片无法解析；请提供有效的 JPG 或 PNG。") from None
            if width < 1 or height < 1 or width > 20_000 or height > 20_000:
                raise LocalWebBlocked("图片尺寸不符合本机模式限制。")
            content_hash = digest.hexdigest()
            storage_name = f"{content_hash}{suffix}"
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
                db.execute("UPDATE materials SET sha256=?, byte_size=?, page_count=1, state='COMPLETED', storage_name=?, completed_at=? WHERE material_id=?", (content_hash, size, storage_name, _iso(_now()), material_id))
                db.execute("INSERT INTO pages(page_id,material_id,page_number) VALUES(?,?,1)", (str(uuid4()), material_id))
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
        from io import BytesIO

        if str(material["media_type"]) in IMAGE_MEDIA_TYPES:
            # 图片材料：渲染为单页 PDF 供预览（原件不被改写）。
            output = BytesIO()
            with Image.open(storage) as image:
                image.convert("RGB").save(output, format="PDF", resolution=150)
            return output.getvalue()
        try:
            reader = PdfReader(storage, strict=True)
            writer = PdfWriter()
            writer.add_page(reader.pages[int(row["page_number"]) - 1])
            output = BytesIO()
            writer.write(output)
            return output.getvalue()
        except Exception:  # noqa: BLE001 - 退化为渲染该页为 PNG
            png = render_single_page_png(storage, int(row["page_number"]))
            if png is None:
                raise LocalWebBlocked("该页无法预览：PDF 无法解析且缺少页面渲染组件。") from None
            return png

    def read_analysis(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            row = db.execute(
                """SELECT source_version, result_json, agent_status, agent_progress,
                          agent_stage, agent_gate_level, agent_report_path, agent_cost_cny,
                          agent_calls, agent_error, agent_engine_json, agent_source_version,
                          agent_run_id, agent_started_at
                   FROM analysis_runs WHERE case_id=?""",
                (case_id,),
            ).fetchone()
        if row is None:
            # 未运行时也返回与运行后同构的 agent 状态，避免客户端拿到残缺对象。
            return {
                "status": "NOT_RUN",
                "analysis": None,
                "agent": {
                    "status": "NOT_RUN", "progress": 0, "stage": "", "gate_level": "",
                    "cost_cny": "0.000000", "calls": 0, "error": "", "engine_numbers": {},
                    "report_available": False, "run_id": "", "started_at": "",
                },
            }
        stale = int(row["source_version"]) != int(case["version"])
        agent_status = str(row["agent_status"])
        agent_stale = bool(row["agent_source_version"]) and int(row["agent_source_version"]) != int(case["version"])
        if agent_stale:
            agent_status = "STALE"
        agent = {
            "status": agent_status,
            "progress": int(row["agent_progress"]),
            "stage": str(row["agent_stage"]),
            "gate_level": str(row["agent_gate_level"]),
            "cost_cny": str(row["agent_cost_cny"]),
            "calls": int(row["agent_calls"]),
            "error": str(row["agent_error"]),
            "engine_numbers": json.loads(str(row["agent_engine_json"]) or "{}"),
            "run_id": str(row["agent_run_id"] or ""),
            "started_at": str(row["agent_started_at"] or ""),
            "report_available": (
                bool(row["agent_report_path"])
                and Path(str(row["agent_report_path"])).is_file()
                and not agent_stale
            ),
        }
        if stale:
            return {"status": "STALE", "analysis": None, "agent": agent}
        return {
            "status": "COMPLETED",
            "analysis": json.loads(str(row["result_json"])),
            "agent": agent,
        }

    # ---------------------------------------------------------------- Agent 分析

    def _analysis_dir(self, case_id: str) -> Path:
        target = self.root / "analysis" / case_id
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        return target

    # ------------------------------------------------- 案件计算参数（律师确认）

    def read_case_config(self, case_id: str) -> dict[str, object] | None:
        """读取律师已确认的案件计算参数（正式数字的唯一来源）。"""
        path = self._analysis_dir(case_id) / "case_config.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def save_case_config(self, case_id: str, case_config: Mapping[str, object]) -> dict[str, object]:
        """校验后原子写入案件计算参数；不合法参数绝不落盘。

        引擎只接受 ``shadow-case-config-v1``：债务本金、放款/到期日、约定月利率，
        以及司法保护上限（LPR 四倍月利率）与利息暂计截止日。参数由律师填写，
        模型不得写入。

        参数变化会使既有决策包失效（正式数字随之改变），因此旧报告按 STALE 处理，
        必须重新运行分析后才能再次阅读或导出。
        """
        from case_kernel.case_payments import PaymentError, load_payments
        from case_kernel.case_sales_claim import SalesClaimError, load_sales_claim
        from case_kernel.shadow_mode import ShadowBlocked, load_case_config

        run_dir = self._analysis_dir(case_id)
        target = run_dir / "case_config.json"
        payload = json.dumps(case_config, ensure_ascii=False, indent=1) + "\n"
        previous = target.read_text(encoding="utf-8") if target.is_file() else ""
        probe = run_dir / "case_config.probe.json"
        probe.write_text(payload, encoding="utf-8")
        debts = case_config.get("debts") or []
        sales_claim = case_config.get("sales_claim")
        if debts:
            try:
                load_case_config(probe)
            except ShadowBlocked as error:
                probe.unlink(missing_ok=True)
                raise LocalWebBlocked(f"案件计算参数不合法：{error}") from None
            except (KeyError, ValueError, TypeError) as error:
                probe.unlink(missing_ok=True)
                raise LocalWebBlocked(f"案件计算参数字段缺失或格式错误：{error}") from None
        elif not isinstance(sales_claim, Mapping):
            # 买卖合同案由不需要借贷参数，但必须至少有一套口径，否则算不出正式数字
            probe.unlink(missing_ok=True)
            raise LocalWebBlocked(
                "案件计算参数没有内容：请填写借款参数，或勾选买卖合同货款口径并填写金额与日期。")
        try:  # 付款性质必须逐笔合法，绝不静默丢弃律师填写的付款
            load_payments(case_config)
        except PaymentError as error:
            probe.unlink(missing_ok=True)
            raise LocalWebBlocked(f"付款记录不合法：{error}") from None
        try:  # 货款口径（买卖合同）同样必须合法，否则不落盘
            load_sales_claim(case_config if isinstance(case_config, dict) else None)
        except SalesClaimError as error:
            probe.unlink(missing_ok=True)
            raise LocalWebBlocked(f"货款口径参数不合法：{error}") from None
        probe.replace(target)
        os.chmod(target, 0o600)
        if previous != payload:
            with self._lock, self._connect() as db:
                db.execute(
                    """UPDATE analysis_runs
                          SET agent_status='STALE', agent_report_path='', agent_source_version=-1
                        WHERE case_id=? AND agent_status IN ('COMPLETED','BLOCKED','FAILED')""",
                    (case_id,),
                )
        return case_config

    def _analysis_materials(self, case_id: str) -> Path:
        """把本案已接收材料汇集到分析目录（硬链接优先，避免重复占用空间）。"""
        target = self._analysis_dir(case_id) / "materials"
        target.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            rows = db.execute(
                """SELECT display_name, storage_name FROM materials
                   WHERE case_id=? AND state='COMPLETED' AND storage_name IS NOT NULL
                   ORDER BY created_at""",
                (case_id,),
            ).fetchall()
        for row in rows:
            source = self.materials / str(row["storage_name"])
            if not source.is_file():
                continue
            name = Path(str(row["display_name"])).name or f"material-{row['storage_name']}"
            destination = target / name
            if destination.exists():
                continue
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
        return target

    def _set_agent_state(self, case_id: str, **fields: object) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE analysis_runs SET {assignments} WHERE case_id=?",
                (*fields.values(), case_id),
            )

    def agent_report_path(self, case_id: str) -> Path | None:
        """返回可用报告路径；材料变化后结果失效，不得继续读取或导出。"""
        case = self._case(case_id)
        with self._connect() as db:
            row = db.execute(
                """SELECT agent_report_path, agent_status, agent_source_version
                   FROM analysis_runs WHERE case_id=?""",
                (case_id,),
            ).fetchone()
        if row is None or not str(row["agent_report_path"]):
            return None
        if str(row["agent_status"]) not in ("COMPLETED", "MODEL_NOT_CONFIGURED"):
            return None
        if int(row["agent_source_version"]) != int(case["version"]):
            return None  # 上游材料已变化 → 下游结果失效
        path = Path(str(row["agent_report_path"]))
        return path if path.is_file() else None

    def agent_report_stale(self, case_id: str) -> bool:
        """材料变化后旧报告是否已被判定失效（用于给出明确提示）。"""
        case = self._case(case_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT agent_source_version, agent_status FROM analysis_runs WHERE case_id=?",
                (case_id,),
            ).fetchone()
        if row is None:
            return False
        return (
            bool(int(row["agent_source_version"]))
            and int(row["agent_source_version"]) != int(case["version"])
            and str(row["agent_status"]) in ("COMPLETED", "STALE")
        )

    # ------------------------------------------------ 交付清单与应诉材料包

    def _deliverable_row(self, case_id: str):
        with self._connect() as db:
            return db.execute("SELECT * FROM deliverable_states WHERE case_id=?",
                              (case_id,)).fetchone()

    def read_deliverables(self, case_id: str) -> dict[str, object]:
        """交付清单状态 + 案件主体信息（律师维护；签字文件与答辩状共用）。"""
        from case_kernel.matter_deliverables import MatterParties, catalogue_payload

        self._case(case_id)
        row = self._deliverable_row(case_id)
        parties = json.loads(str(row["parties_json"]) or "{}") if row is not None else {}
        states = json.loads(str(row["states_json"]) or "{}") if row is not None else {}
        if not parties:
            # 首次进入时用答辩状页面已填的主体信息回填，避免重复录入。
            brief = self.read_brief(case_id)
            selections = brief.get("selections") or {}
            if isinstance(selections, dict):
                parties = {
                    key: selections.get(source) or ""
                    for key, source in (("respondent", "respondent"), ("claimant", "claimant"),
                                        ("court", "court"), ("case_number", "caseNumber"))
                }
        return {
            "catalogue": catalogue_payload(),
            "parties": MatterParties.from_dict(parties).to_dict(),
            "states": {str(key): str(value) for key, value in dict(states).items()},
            "updated_at": str(row["updated_at"]) if row is not None else "",
        }

    def save_deliverables(self, case_id: str, parties: Mapping[str, object],
                          states: Mapping[str, object]) -> dict[str, object]:
        from case_kernel.matter_deliverables import (
            CATALOGUE_BY_ID,
            DELIVERABLE_STATES,
            MatterParties,
        )

        self._case(case_id)
        normalized_parties = MatterParties.from_dict(parties).to_dict()
        normalized_states = {
            str(item_id): (str(status) if str(status) in DELIVERABLE_STATES else "未开始")
            for item_id, status in dict(states).items()
            if str(item_id) in CATALOGUE_BY_ID
        }
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO deliverable_states(case_id, parties_json, states_json, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(case_id) DO UPDATE SET
                     parties_json=excluded.parties_json,
                     states_json=excluded.states_json,
                     updated_at=excluded.updated_at""",
                (case_id, json.dumps(normalized_parties, ensure_ascii=False),
                 json.dumps(normalized_states, ensure_ascii=False), _iso(_now())),
            )
        return self.read_deliverables(case_id)

    def deliverable_materials(self, case_id: str) -> list[dict[str, object]]:
        return [
            {"display_name": str(item["display_name"]),
             "page_count": int(item["page_count"] or 0)}
            for item in self._case_materials(case_id)
        ]

    def deliverable_package_markdown(self, case_id: str) -> str:
        """整套应诉材料包：清单 + 签字文件 + 证据目录 + 已有答辩状草稿。"""
        from case_kernel.matter_deliverables import MatterParties, render_package

        state = self.read_deliverables(case_id)
        parties = MatterParties.from_dict(state.get("parties"))
        answer_path = self.brief_markdown_path(case_id)
        answer = answer_path.read_text(encoding="utf-8") if answer_path else ""
        return render_package(
            parties=parties,
            states=state.get("states") or {},
            materials=self.deliverable_materials(case_id),
            answer_draft=answer,
            generated_at=_iso(_now()),
        )

    # ------------------------------------------------ 答辩状草稿（律师工作稿）

    def _config_hash(self, case_id: str) -> str:
        path = self._analysis_dir(case_id) / "case_config.json"
        return _digest(path.read_text(encoding="utf-8")) if path.is_file() else ""

    def _analysis_run_id(self, case_id: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT agent_run_id FROM analysis_runs WHERE case_id=?",
                             (case_id,)).fetchone()
        return str(row["agent_run_id"]) if row is not None else ""

    def _brief_row(self, case_id: str):
        with self._connect() as db:
            return db.execute("SELECT * FROM brief_runs WHERE case_id=?", (case_id,)).fetchone()

    def read_brief(self, case_id: str) -> dict[str, object]:
        """答辩状状态 + 律师已保存的选择。上一轮上游变化后按 STALE 处理。"""
        from case_kernel.defence_brief import BriefSelections

        case = self._case(case_id)
        row = self._brief_row(case_id)
        if row is None:
            return {
                "selections": BriefSelections().to_dict(),
                "state": {
                    "status": "NOT_RUN", "progress": 0, "stage": "", "gate_level": "",
                    "cost_cny": "0.000000", "calls": 0, "error": "",
                    "engine_numbers": {}, "markdown_available": False, "stale": False,
                    "run_id": "", "started_at": "", "generated_at": "",
                },
            }
        stored_status = str(row["status"])
        upstream_changed = (
            int(row["source_version"] or 0) != int(case["version"])
            or str(row["config_hash"] or "") != self._config_hash(case_id)
            or str(row["analysis_run_id"] or "") != self._analysis_run_id(case_id)
        )
        # 草稿失效有两种来源：上游（材料/参数/分析）变化，或律师改了选择。
        stale = stored_status == "STALE" or upstream_changed
        status = "STALE" if (stale and stored_status != "RUNNING") else stored_status
        path = Path(str(row["markdown_path"])) if row["markdown_path"] else None
        return {
            "selections": json.loads(str(row["selections_json"]) or "{}"),
            "state": {
                "status": status,
                "progress": int(row["progress"]),
                "stage": str(row["stage"]),
                "gate_level": str(row["gate_level"]),
                "cost_cny": str(row["cost_cny"]),
                "calls": int(row["calls"]),
                "error": str(row["error"]),
                "engine_numbers": json.loads(str(row["engine_json"]) or "{}"),
                "markdown_available": bool(path and path.is_file() and not stale),
                "stale": stale,
                "run_id": str(row["run_id"] or ""),
                "started_at": str(row["started_at"] or ""),
                "generated_at": str(row["generated_at"] or ""),
            },
        }

    def save_brief_selections(self, case_id: str, selections: Mapping[str, object]) -> dict[str, object]:
        """保存律师选择；选择变化使既有草稿失效（立场变了，文书必须重做）。"""
        from case_kernel.defence_brief import BriefSelections

        self._case(case_id)
        normalized = BriefSelections.from_dict(selections).to_dict()
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        digest = _digest(payload)
        current = self._brief_row(case_id)
        if current is not None and str(current["selections_hash"]) == digest:
            return normalized
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO brief_runs(case_id, selections_json, selections_hash, status)
                   VALUES(?,?,?, 'NOT_RUN')
                   ON CONFLICT(case_id) DO UPDATE SET
                     selections_json=excluded.selections_json,
                     selections_hash=excluded.selections_hash,
                     status='STALE', markdown_path='', gate_level='', error=''""",
                (case_id, payload, digest),
            )
        return normalized

    def brief_markdown_path(self, case_id: str) -> Path | None:
        row = self._brief_row(case_id)
        if row is None or not str(row["markdown_path"]):
            return None
        state = self.read_brief(case_id)["state"]
        if state["stale"] or state["status"] not in ("COMPLETED", "MODEL_NOT_CONFIGURED"):
            return None
        path = Path(str(row["markdown_path"]))
        return path if path.is_file() else None

    def start_brief_generation(
        self,
        case_id: str,
        *,
        case_number: str,
        budget_cny: Decimal = Decimal("2"),
    ) -> dict[str, object]:
        """生成答辩状草稿（后台线程）；立即返回，不阻塞请求。"""
        from case_kernel.defence_brief import BriefSelections

        case = self._case(case_id)
        row = self._brief_row(case_id)
        if row is not None and str(row["status"]) == "RUNNING":
            return {"status": "RUNNING", "message": "答辩状正在生成中。"}
        selections = BriefSelections.from_dict(
            json.loads(str(row["selections_json"]) or "{}") if row is not None else None)
        run_dir = self._analysis_dir(case_id)
        config_path = run_dir / "case_config.json"
        report_path = self.agent_report_path(case_id)
        analysis_run_id = self._analysis_run_id(case_id)
        materials = [
            {"display_name": str(item["display_name"]), "page_count": int(item["page_count"] or 0)}
            for item in self._case_materials(case_id)
        ]
        env_file = self._resolve_model_env_file()
        preflight_path = self._write_brief_preflight(
            case_id, run_dir, case_number, selections, budget_cny, env_file)

        disabled = os.environ.get("CASE_WORKBENCH_DISABLE_AGENT", "").strip() == "1"
        run_id = str(uuid4())
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO brief_runs(case_id, selections_json, selections_hash, status)
                   VALUES(?,?,?, 'RUNNING')
                   ON CONFLICT(case_id) DO UPDATE SET
                     status='RUNNING', progress=0, stage='准备', gate_level='',
                     markdown_path='', cost_cny='0.000000', calls=0, error='',
                     engine_json='{}', source_version=?, config_hash=?, analysis_run_id=?,
                     run_id=?, started_at=?, generated_at=''""",
                (case_id, json.dumps(selections.to_dict(), ensure_ascii=False, sort_keys=True),
                 _digest(json.dumps(selections.to_dict(), ensure_ascii=False, sort_keys=True)),
                 int(case["version"]), self._config_hash(case_id), analysis_run_id,
                 run_id, _iso(_now())),
            )
            if disabled:
                db.execute("UPDATE brief_runs SET status='DISABLED', stage='' WHERE case_id=?",
                           (case_id,))

        def progress(stage: str, percent: int) -> None:
            self._set_brief_state(case_id, progress=max(0, min(100, percent)), stage=stage)

        def worker() -> None:
            from case_kernel.defence_brief_service import BriefRequest, run_brief

            try:
                result = run_brief(BriefRequest(
                    case_id=case_id,
                    output_root=run_dir,
                    selections=selections,
                    case_config_path=config_path if config_path.is_file() else None,
                    case_number=case_number,
                    analysis_report_path=report_path,
                    preflight_path=preflight_path,
                    env_file=env_file,
                    budget_cny=budget_cny,
                    materials=materials,
                    progress=progress,
                ))
                self._set_brief_state(
                    case_id,
                    status=result.status,
                    progress=100 if result.status in ("COMPLETED", "MODEL_NOT_CONFIGURED") else 0,
                    stage="完成" if result.status in ("COMPLETED", "MODEL_NOT_CONFIGURED") else "",
                    gate_level=result.gate_level,
                    markdown_path=str(run_dir / "答辩状草稿.md") if result.markdown else "",
                    cost_cny=result.cost_cny,
                    calls=result.calls,
                    error=result.error or "",
                    engine_json=json.dumps(result.engine_amounts, ensure_ascii=False),
                    generated_at=_iso(_now()) if result.markdown else "",
                )
            except Exception as error:  # noqa: BLE001 - 后台线程边界
                try:
                    self._set_brief_state(case_id, status="FAILED", progress=0,
                                          error=f"{type(error).__name__}: {error}")
                except Exception:  # noqa: BLE001 - 不得抛出未捕获异常
                    pass

        if disabled:
            return {"status": "DISABLED", "message": "后台生成已在当前环境禁用。"}
        Thread(target=worker, name=f"defence-brief-{case_id[:8]}", daemon=True).start()
        return {"status": "RUNNING", "message": "答辩状生成已开始。"}

    def _set_brief_state(self, case_id: str, **fields: object) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE brief_runs SET {assignments} WHERE case_id=?",
                       (*fields.values(), case_id))

    def _write_brief_preflight(
        self,
        case_id: str,
        run_dir: Path,
        case_number: str,
        selections,
        budget_cny: Decimal,
        env_file: Path | None,
    ) -> Path | None:
        """答辩状的数据路径记录：只发送文字，不发送材料像素；法源取律师登记。"""
        if env_file is None:
            return None
        preflight = {
            "schema": "shadow-preflight-v1",
            "purpose": "defence_brief_local_web",
            "case_id": case_id,
            "case_number": case_number,
            "role": "被告",
            "stage": "文书起草",
            "sent_fields": {"page_files": [], "pdf_text_layers_only": True,
                            "analysis_report": True},
            "provider": "aliyun-model-studio",
            "model": os.environ.get("CASE_WORKBENCH_MODEL_NAME", "qwen3-vl-plus"),
            "region": "cn-beijing",
            "retention": "不保存（调用即弃，不用于训练）",
            "budget_cap_cny": str(budget_cny),
            "trusted_authorities": list(selections.authorities),
            "approved_by": f"本地工作台律师点击确认（case={case_id}）",
            "confirmed": "true",
        }
        path = run_dir / "brief_preflight.json"
        path.write_text(json.dumps(preflight, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        return path

    def _case_materials(self, case_id: str):
        with self._connect() as db:
            return db.execute(
                """SELECT display_name, page_count FROM materials
                   WHERE case_id=? AND state='COMPLETED' AND media_type IN
                     ('application/pdf','image/jpeg','image/png')
                   ORDER BY created_at""",
                (case_id,),
            ).fetchall()

    def start_agent_analysis(
        self,
        case_id: str,
        *,
        case_number: str,
        role: str = "被告",
        stage_name: str = "一审应诉",
        budget_cny: Decimal = Decimal("2"),
        case_config: dict[str, object] | None = None,
        allow_image_identifiers: bool = False,
    ) -> dict[str, object]:
        """启动 Agent 深度分析（后台线程）；立即返回，不阻塞请求。"""
        case = self._case(case_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT agent_status FROM analysis_runs WHERE case_id=?", (case_id,)
            ).fetchone()
        if row is not None and str(row["agent_status"]) == "RUNNING":
            return {"status": "RUNNING", "message": "分析已在进行中。"}

        run_dir = self._analysis_dir(case_id)
        materials_dir = self._analysis_materials(case_id)
        config_path = run_dir / "case_config.json"
        if case_config:
            self.save_case_config(case_id, case_config)
        active_config = config_path if config_path.is_file() else None

        env_file = self._resolve_model_env_file()
        preflight_path = self._write_agent_preflight(
            case_id, run_dir, materials_dir, case_number, role, stage_name, budget_cny, env_file
        )

        disabled = os.environ.get("CASE_WORKBENCH_DISABLE_AGENT", "").strip() == "1"
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO analysis_runs(case_id,source_version,status,result_json,result_hash,generated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(case_id) DO UPDATE SET
                     agent_status='RUNNING', agent_progress=0, agent_stage='准备',
                     agent_gate_level='', agent_report_path='', agent_cost_cny='0.000000',
                     agent_calls=0, agent_error='', agent_engine_json='{}',
                     agent_source_version=?, agent_run_id=?, agent_started_at=?""",
                (case_id, int(case["version"]), "AGENT_RUNNING", "{}", _digest("agent:running"),
                 _iso(_now()), int(case["version"]), str(uuid4()), _iso(_now())),
            )
            if disabled:
                db.execute(
                    "UPDATE analysis_runs SET agent_status='DISABLED', agent_stage='' WHERE case_id=?",
                    (case_id,),
                )

        def progress(stage: str, percent: int) -> None:
            self._set_agent_state(case_id, agent_stage=stage, agent_progress=max(0, min(100, percent)))

        def worker() -> None:
            from case_kernel.case_analysis_service import AnalysisRequest, run_analysis

            try:
                result = run_analysis(AnalysisRequest(
                    case_id=case_id,
                    materials_dir=materials_dir,
                    output_root=run_dir,
                    case_number=case_number,
                    role=role,
                    stage=stage_name,
                    case_config_path=active_config,
                    preflight_path=preflight_path,
                    env_file=env_file,
                    budget_cny=budget_cny,
                    progress=progress,
                    allow_image_identifiers=allow_image_identifiers,
                ))
                self._set_agent_state(
                    case_id,
                    agent_status=result.status,
                    agent_progress=100 if result.status == "COMPLETED" else 0,
                    agent_stage="完成" if result.status == "COMPLETED" else "",
                    agent_gate_level=result.gate_level,
                    agent_report_path=str(run_dir / "决策包.md") if result.report_md else "",
                    agent_cost_cny=result.cost_cny,
                    agent_calls=result.calls,
                    agent_error=result.error or "",
                    agent_engine_json=json.dumps(result.engine_numbers, ensure_ascii=False),
                )
            except Exception as error:  # noqa: BLE001 - 后台线程边界
                # 记录失败状态本身也必须容错：工作区可能已被移除或数据库不可写。
                try:
                    self._set_agent_state(
                        case_id, agent_status="FAILED", agent_progress=0,
                        agent_error=f"{type(error).__name__}: {error}",
                    )
                except Exception:  # noqa: BLE001 - 后台线程不得抛出未捕获异常
                    pass

        if disabled:
            return {"status": "DISABLED", "message": "后台 Agent 已在当前环境禁用。"}
        Thread(target=worker, name=f"case-analysis-{case_id[:8]}", daemon=True).start()
        return {"status": "RUNNING", "message": "分析已开始，可在案件页查看进度。"}

    def _resolve_model_env_file(self) -> Path | None:
        """解析模型环境文件。

        安全默认：**不自动使用仓库内的真实密钥**，必须显式配置其一：
        - ``CASE_WORKBENCH_MODEL_ENV_FILE`` 指向具体 env 文件；或
        - ``CASE_WORKBENCH_ENABLE_MODEL=1`` 才回退到仓库默认 env 文件。
        未配置时分析走降级路径（确定性结果与正式数字仍可用），不会产生任何外部调用。
        """
        configured = os.environ.get("CASE_WORKBENCH_MODEL_ENV_FILE", "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_file() else None
        if os.environ.get("CASE_WORKBENCH_ENABLE_MODEL", "").strip() == "1":
            default = (_project_root() / "deployment" / "local-managed-test"
                       / "runtime" / "local-managed.env")
            return default if default.is_file() else None
        return None

    def _write_agent_preflight(
        self, case_id: str, run_dir: Path, materials_dir: Path, case_number: str,
        role: str, stage_name: str, budget_cny: Decimal, env_file: Path | None,
    ) -> Path | None:
        """写入本次运行的数据路径记录（律师点击即确认；内容不发送到本文件之外）。"""
        if env_file is None:
            return None
        page_files = sorted(
            str(path.relative_to(materials_dir))
            for path in materials_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
        preflight = {
            "schema": "shadow-preflight-v1",
            "purpose": "case_analysis_local_web",
            "case_id": case_id,
            "case_number": case_number,
            "role": role,
            "stage": stage_name,
            "sent_fields": {"page_files": page_files, "pdf_text_layers_only": True},
            "provider": "aliyun-model-studio",
            "model": os.environ.get("CASE_WORKBENCH_MODEL_NAME", "qwen3-vl-plus"),
            "region": "cn-beijing",
            "retention": "不保存（调用即弃，不用于训练）",
            "budget_cap_cny": str(budget_cny),
            "trusted_authorities": [],
            "approved_by": f"本地工作台律师点击确认（case={case_id}）",
            "confirmed": "true",
        }
        path = run_dir / "preflight.json"
        path.write_text(json.dumps(preflight, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return path

    def run_analysis(self, case_id: str) -> dict[str, object]:
        case = self._case(case_id)
        with self._connect() as db:
            materials = db.execute("SELECT material_id, display_name, storage_name, page_count, sha256, media_type FROM materials WHERE case_id=? AND state='COMPLETED' AND media_type IN ('application/pdf','image/jpeg','image/png') ORDER BY created_at", (case_id,)).fetchall()
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
            if str(material["media_type"]) in IMAGE_MEDIA_TYPES:
                # 图片材料：无文本层，进入需 OCR 队列（候选仍登记来源页）。
                scanned_pages += 1
                evidence_page_id = page_ids.get((str(material["material_id"]), 1))
                if evidence_page_id is None:
                    raise LocalWebBlocked("材料页面台账不完整，不能建立可追溯候选。")
                candidates.append({
                    "candidate_id": _digest(f"{material['sha256']}:image:1")[:24],
                    "evidence_page_id": evidence_page_id,
                    "kind": "OCR_REQUIRED",
                    "status": "CANDIDATE",
                    "source_file": str(material["display_name"]),
                    "source_sha256": str(material["sha256"]),
                    "page_number": 1,
                    "signals": [],
                    "dates": [],
                    "amounts": [],
                    "snippet": "本页为图片材料（截图/照片），需要视觉 OCR 复核。",
                    "human_action": "请律师在页面预览中核对；候选不会自动进入正式案件事实或交易台账。",
                })
                files.append({"material_id": str(material["material_id"]),
                              "display_name": str(material["display_name"]), "page_count": 1,
                              "text_layer_pages": 0, "candidate_page_count": 1})
                continue
            page_texts, _backend = pdf_page_texts(storage)
            if page_texts is None:
                # 无法提取文本层：按扫描件处理，逐页进入 OCR 候选，绝不当成"已读过"。
                count, _ = pdf_page_count(storage)
                for page_number in range(1, count + 1):
                    scanned_pages += 1
                    candidates.append({
                        "kind": "OCR_REQUIRED", "page_number": page_number,
                        "signals": [], "amounts": [], "dates": [],
                        "snippet": "本页没有可提取文字（PDF 文本层无法解析），需要视觉/OCR 服务复核。",
                        "source_file": str(material["display_name"]),
                        "source_sha256": str(material["sha256"] or ""),
                    })
                files.append({"material_id": str(material["material_id"]),
                              "display_name": str(material["display_name"]),
                              "page_count": count, "text_layer_pages": 0,
                              "candidate_page_count": count})
                continue
            try:
                for page_number, text in enumerate(page_texts, start=1):
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
                # 本机模式确实实现了 Agent 深度分析：确定性材料核对 + 正式数字 + 可选模型
                # 决策包（/analysis、/analysis/report、/analysis/export）。这里如实放行，
                # 否则律师在工作台里点不开「决策包」，已实现的能力等于不存在。
                # 事实确认、法律审阅、测算与受管成果文件仍在本机模式之外，保持 False。
                "can_run_agent": True,
                "can_confirm_fact": False,
                "can_review_facts": False,
                "can_review_legal": False,
                "can_run_calculation": False,
                "can_review_submission": False,
                "can_generate_documents": False,
                # 本机模式装配了答辩状草稿（确定性骨架 + 受门禁约束的模型文字）。
                "can_draft_defence_brief": True,
                # 本机模式装配了交付清单与应诉材料包（含当事人签字文件模板）。
                "can_manage_deliverables": True,
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
        normalized_type = body.content_type.split(";", 1)[0].strip().lower()
        suffix = Path(str(body.client_filename)).suffix.lower()
        if normalized_type == "application/pdf" and suffix == ".pdf":
            kind = "PDF"
        elif normalized_type in IMAGE_MEDIA_TYPES and suffix in {".jpg", ".jpeg", ".png"}:
            kind = "IMAGE"
        else:
            raise LocalWebBlocked("本机模式接收 PDF、JPG 或 PNG 材料。")
        upload = store.create_upload(str(case_id), body.client_filename,
                                     body.expected_version, idempotency_key or "", kind)
        return {"upload": upload}

    @app.put("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}/content")
    async def accept_upload(case_id: UUID, upload_id: UUID, request: Request, x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        material = store._material(str(case_id), str(upload_id))
        media_type = str(material["media_type"])
        request_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if media_type == "application/pdf":
            if request_type != "application/pdf":
                raise LocalWebBlocked("本机模式只接收 application/pdf。")
            receipt = await store.accept_pdf(str(case_id), str(upload_id), request.stream())
        elif media_type in IMAGE_MEDIA_TYPES:
            if request_type not in IMAGE_MEDIA_TYPES:
                raise LocalWebBlocked("本机模式只接收 image/jpeg 或 image/png。")
            receipt = await store.accept_image(str(case_id), str(upload_id), request.stream())
        else:
            raise LocalWebBlocked("材料接收类型不匹配。")
        return {"receipt": receipt}

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

    @app.get("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}/offset")
    async def upload_offset(case_id: UUID, upload_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return {"upload_id": str(upload_id), "offset": store.staged_size(str(case_id), str(upload_id))}

    @app.put("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}/chunks")
    async def accept_chunk(case_id: UUID, upload_id: UUID, request: Request,
                           x_lawcase_csrf: str | None = Header(default=None),
                           x_chunk_offset: str | None = Header(default=None)):
        """分片追加：大文件经反向代理也不会被 30 秒超时截断。"""
        ensure_write(request, x_lawcase_csrf)
        try:
            offset = int(x_chunk_offset or "")
        except ValueError:
            raise LocalWebBlocked("缺少有效的分片偏移（X-Chunk-Offset）。") from None
        if offset < 0:
            raise LocalWebBlocked("分片偏移不能为负。")
        data = await request.body()
        offset = store.append_chunk(str(case_id), str(upload_id), offset, data)
        return {"upload_id": str(upload_id), "offset": offset}

    @app.post("/api/local/v1/cases/{case_id}/material-uploads/{upload_id}/finalize")
    async def finalize_upload(case_id: UUID, upload_id: UUID, request: Request,
                              x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return {"receipt": store.finalize_upload(str(case_id), str(upload_id))}

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
    async def run_analysis(case_id: UUID, request: Request, response: Response,
                           body: AnalysisRunRequest | None = None,
                           x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        deterministic = store.run_analysis(str(case_id))
        payload = body or AnalysisRunRequest()
        case = store._case(str(case_id))
        configured_budget = os.environ.get("CASE_WORKBENCH_MODEL_BUDGET_CNY", "").strip()
        raw_budget = payload.budget_cny if payload.budget_cny is not None else (configured_budget or "2")
        try:
            budget = Decimal(str(raw_budget))
        except (InvalidOperation, TypeError):
            raise LocalWebBlocked("预算参数不是有效数字。") from None
        if budget <= 0 or budget > Decimal("50"):
            raise LocalWebBlocked("预算必须在 0 与 50 元之间。")
        started = store.start_agent_analysis(
            str(case_id),
            case_number=payload.case_number or str(case["title"]),
            role=payload.role or "被告",
            stage_name=payload.stage or "一审应诉",
            budget_cny=budget,
            case_config=payload.case_config,
            allow_image_identifiers=payload.allow_image_identifiers,
        )
        # 顶层保持既有契约（status/analysis）。agent 必须是与 GET 完全同构的**完整状态**：
        # 只回 {"status": "RUNNING"} 会让前端解析失败（服务端返回的文本字段格式不正确）。
        return {
            **deterministic,
            "agent": store.read_analysis(str(case_id))["agent"],
            "message": started.get("message", ""),
        }

    @app.get("/api/local/v1/cases/{case_id}/analysis/config")
    async def analysis_config(case_id: UUID, request: Request, response: Response):
        """读取律师已确认的案件计算参数，供界面回填。正式数字只来自这些参数。"""
        ensure_session(request, response)
        return {"case_config": store.read_case_config(str(case_id))}

    @app.put("/api/local/v1/cases/{case_id}/analysis/config")
    async def save_analysis_config(case_id: UUID, request: Request, response: Response,
                                   body: AnalysisConfigRequest,
                                   x_lawcase_csrf: str | None = Header(default=None)):
        """只保存参数、不运行分析；参数变化会使既有决策包失效。"""
        ensure_write(request, x_lawcase_csrf)
        store.save_case_config(str(case_id), body.case_config)
        return {"case_config": store.read_case_config(str(case_id))}

    @app.get("/api/local/v1/cases/{case_id}/analysis/report")
    async def analysis_report(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        path = store.agent_report_path(str(case_id))
        if path is None:
            if store.agent_report_stale(str(case_id)):
                raise LocalWebBlocked("案件材料或计算参数已变化，原分析结果已失效；请重新运行分析后再查看报告。")
            raise LocalWebNotFound("尚无分析报告，请先运行分析。")
        return Response(content=path.read_text(encoding="utf-8"),
                        media_type="text/markdown; charset=utf-8")

    @app.get("/api/local/v1/cases/{case_id}/analysis/export")
    async def analysis_export(case_id: UUID, request: Request, response: Response,
                              format: str = "md"):
        ensure_session(request, response)
        path = store.agent_report_path(str(case_id))
        if path is None:
            if store.agent_report_stale(str(case_id)):
                raise LocalWebBlocked("案件材料或计算参数已变化，原分析结果已失效；请重新运行分析后再导出。")
            raise LocalWebNotFound("尚无分析报告，无法导出。")
        text = path.read_text(encoding="utf-8")
        if format == "md":
            return Response(
                content=text,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="case-analysis.md"'},
            )
        if format == "docx":
            from case_api.analysis_export import render_decision_package_docx

            payload = render_decision_package_docx(text)
            return Response(
                content=payload,
                media_type=("application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"),
                headers={"Content-Disposition": 'attachment; filename="case-analysis.docx"'},
            )
        raise LocalWebBlocked("导出格式仅支持 md 或 docx。")

    # ------------------------------------------------ 交付清单与应诉材料包路由

    @app.get("/api/local/v1/cases/{case_id}/deliverables")
    async def deliverables(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        return store.read_deliverables(str(case_id))

    @app.put("/api/local/v1/cases/{case_id}/deliverables")
    async def save_deliverables(case_id: UUID, request: Request, response: Response,
                                body: DeliverableStateRequest,
                                x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        return store.save_deliverables(str(case_id), body.parties, body.states)

    @app.get("/api/local/v1/cases/{case_id}/deliverables/template/{item_id}")
    async def deliverable_template(case_id: UUID, item_id: str, request: Request,
                                   response: Response):
        ensure_session(request, response)
        from case_kernel.matter_deliverables import MatterParties, render_template

        state = store.read_deliverables(str(case_id))
        text = render_template(item_id, MatterParties.from_dict(state.get("parties")),
                               store.deliverable_materials(str(case_id)))
        if text is None:
            raise LocalWebNotFound("该交付物没有可用模板。")
        return {"item_id": item_id, "markdown": text}

    @app.get("/api/local/v1/cases/{case_id}/deliverables/export")
    async def deliverable_export(case_id: UUID, request: Request, response: Response,
                                 format: str = "md"):
        ensure_session(request, response)
        text = store.deliverable_package_markdown(str(case_id))
        if format == "md":
            return Response(content=text, media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="defence-package.md"'})
        if format == "docx":
            from case_api.analysis_export import render_decision_package_docx

            payload = render_decision_package_docx(text)
            return Response(
                content=payload,
                media_type=("application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"),
                headers={"Content-Disposition": 'attachment; filename="defence-package.docx"'},
            )
        raise LocalWebBlocked("导出格式仅支持 md 或 docx。")

    # ------------------------------------------------------ 答辩状草稿路由

    @app.get("/api/local/v1/cases/{case_id}/brief")
    async def brief(case_id: UUID, request: Request, response: Response):
        ensure_session(request, response)
        payload = store.read_brief(str(case_id))
        path = store.brief_markdown_path(str(case_id))
        return {**payload, "markdown": path.read_text(encoding="utf-8") if path else ""}

    @app.put("/api/local/v1/cases/{case_id}/brief")
    async def save_brief(case_id: UUID, request: Request, response: Response,
                         body: BriefSelectionRequest,
                         x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        store.save_brief_selections(str(case_id), body.selections)
        return store.read_brief(str(case_id))

    @app.post("/api/local/v1/cases/{case_id}/brief/generate")
    async def generate_brief(case_id: UUID, request: Request, response: Response,
                             body: BriefGenerateRequest | None = None,
                             x_lawcase_csrf: str | None = Header(default=None)):
        ensure_write(request, x_lawcase_csrf)
        payload = body or BriefGenerateRequest()
        case = store._case(str(case_id))
        configured_budget = os.environ.get("CASE_WORKBENCH_MODEL_BUDGET_CNY", "").strip()
        raw_budget = payload.budget_cny if payload.budget_cny is not None else (configured_budget or "2")
        try:
            budget = Decimal(str(raw_budget))
        except (InvalidOperation, TypeError):
            raise LocalWebBlocked("预算参数不是有效数字。") from None
        if budget <= 0 or budget > Decimal("50"):
            raise LocalWebBlocked("预算必须在 0 与 50 元之间。")
        store.start_brief_generation(
            str(case_id),
            case_number=payload.case_number or str(case["title"]),
            budget_cny=budget,
        )
        return store.read_brief(str(case_id))

    @app.get("/api/local/v1/cases/{case_id}/brief/export")
    async def export_brief(case_id: UUID, request: Request, response: Response,
                           format: str = "md"):
        ensure_session(request, response)
        path = store.brief_markdown_path(str(case_id))
        if path is None:
            state = store.read_brief(str(case_id))["state"]
            if state.get("stale"):
                raise LocalWebBlocked("案件材料、计算参数或分析结果已变化，原答辩状草稿已失效；请重新生成。")
            raise LocalWebNotFound("尚无答辩状草稿，请先生成。")
        text = path.read_text(encoding="utf-8")
        if format == "md":
            return Response(content=text, media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="defence-brief.md"'})
        if format == "docx":
            from case_api.analysis_export import render_decision_package_docx

            payload = render_decision_package_docx(text)
            return Response(
                content=payload,
                media_type=("application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"),
                headers={"Content-Disposition": 'attachment; filename="defence-brief.docx"'},
            )
        raise LocalWebBlocked("导出格式仅支持 md 或 docx。")

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
