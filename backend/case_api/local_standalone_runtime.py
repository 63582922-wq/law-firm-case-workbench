"""Local-first runtime for one lawyer's standalone desktop workspace.

This is deliberately *not* a small PostgreSQL replacement.  A standalone
installation may create and reopen its own case shell, keep a locally chosen
material-root reference, and make an explicit read-only inventory.  It does
not expose the firm-managed fact ledger, formal calculation, submission,
worker, or external-model paths.  Those paths keep their existing PostgreSQL
and enrolled-lawyer gates.

The SQLite ledger contains only local case metadata, audit/idempotency records,
material-root display metadata, and an explicitly requested file inventory.  A
selected absolute folder path never enters SQLite, an HTTP response, or an
audit payload; it lives only in the sidecar's short-lived native selection
registry while the desktop app is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Mapping
import unicodedata
from uuid import UUID, uuid4

from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.local_case_folder import FolderManifest, FolderScanBlocked, root_fingerprint
from case_kernel.models import Actor, Role
from case_kernel.runtime import RuntimeConfigurationBlocked, RuntimeMode, RuntimeSettings


_LOCAL_ROOT_ENV = "CASE_WORKBENCH_LOCAL_WORKSPACE_ROOT"
_DATABASE_NAME = "local-workspace.sqlite3"
_SELECTION_TTL = timedelta(minutes=15)
_MAX_SELECTIONS = 64
_SHA256_PATTERN = frozenset("0123456789abcdef")


class LocalStandaloneRuntimeBlocked(RuntimeError):
    """The standalone workspace cannot safely initialize."""


class LocalStandaloneAccessBlocked(PermissionError):
    """A native-only local operation is outside the current safe scope."""


class LocalStandaloneNotFound(LookupError):
    """The requested local case is absent or not owned by this installation."""


class LocalStandaloneConflict(ValueError):
    """A local optimistic-concurrency or idempotency condition failed."""


@dataclass(frozen=True)
class LocalMaterialRootReference:
    display_name: str
    root_fingerprint: str
    linked_at: datetime


@dataclass(frozen=True)
class LocalFolderInventorySummary:
    scan_id: str
    root_fingerprint: str
    manifest_hash: str
    scanned_at: datetime
    total_files: int
    total_bytes: int
    skipped_symlinks: int


@dataclass(frozen=True)
class LocalFolderInventoryItem:
    relative_path: str
    byte_size: int
    sha256: str
    detected_kind: str


@dataclass(frozen=True)
class LocalCaseSummary:
    case_id: str
    title: str
    stage: str
    matter_version: int
    material_root: LocalMaterialRootReference
    inventory: LocalFolderInventorySummary | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class LocalFolderSelection:
    selection_id: str
    display_name: str
    root_fingerprint: str
    selected_at: datetime


@dataclass(frozen=True)
class _NativeFolderSelectionRecord:
    selection: LocalFolderSelection
    root: Path
    device: int
    inode: int
    expires_at: datetime


class NativeFolderSelectionRegistry:
    """In-memory authority for one native OS-folder choice.

    The registry's records are intentionally not serializable: the absolute
    path is a sidecar-only capability, never a browser-facing local-case field.
    """

    def __init__(self, *, ttl: timedelta = _SELECTION_TTL) -> None:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=30):
            raise ValueError("local folder selection TTL must be between 1 second and 30 minutes")
        self._ttl = ttl
        self._records: dict[str, _NativeFolderSelectionRecord] = {}
        self._lock = Lock()

    def register(self, selected_root: str | Path, *, now: datetime | None = None) -> LocalFolderSelection:
        current = _aware_now(now)
        root = _resolve_safe_case_root(selected_root)
        metadata = root.stat()
        selection = LocalFolderSelection(
            selection_id=str(uuid4()),
            display_name=root.name,
            root_fingerprint=root_fingerprint(root),
            selected_at=current,
        )
        record = _NativeFolderSelectionRecord(
            selection=selection,
            root=root,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            expires_at=current + self._ttl,
        )
        with self._lock:
            self._remove_expired(current)
            if len(self._records) >= _MAX_SELECTIONS:
                oldest = min(self._records, key=lambda item: self._records[item].selection.selected_at)
                del self._records[oldest]
            self._records[selection.selection_id] = record
        return selection

    def resolve(self, selection_id: str, *, now: datetime | None = None) -> _NativeFolderSelectionRecord:
        _validate_uuid(selection_id, label="folder selection")
        current = _aware_now(now)
        with self._lock:
            self._remove_expired(current)
            record = self._records.get(selection_id)
        if record is None:
            raise LocalStandaloneAccessBlocked("本机文件夹选择已过期；请重新选择后再继续。")
        try:
            current_root = _resolve_safe_case_root(record.root)
            metadata = current_root.stat()
            if (
                current_root != record.root
                or metadata.st_dev != record.device
                or metadata.st_ino != record.inode
                or root_fingerprint(current_root) != record.selection.root_fingerprint
            ):
                raise LocalStandaloneAccessBlocked("所选资料文件夹在授权后已变化；请重新选择。")
        except (OSError, FolderScanBlocked) as error:
            raise LocalStandaloneAccessBlocked("所选资料文件夹已不可用；请重新选择。") from error
        return record

    def _remove_expired(self, now: datetime) -> None:
        for selection_id, record in tuple(self._records.items()):
            if record.expires_at <= now:
                del self._records[selection_id]


class LocalStandaloneStore:
    """Small append-only local ledger with no absolute material-root paths."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = Lock()
        self._initialize()

    @classmethod
    def open(cls, workspace_root: Path) -> "LocalStandaloneStore":
        root = _prepare_private_workspace_root(workspace_root)
        database_path = root / _DATABASE_NAME
        if database_path.exists() and database_path.is_symlink():
            raise LocalStandaloneRuntimeBlocked("本机基础案卷账本不能使用符号链接。")
        return cls(database_path)

    @property
    def database_path(self) -> Path:
        """Private local path for operational diagnostics only; never serialize it."""

        return self._database_path

    def identity(self) -> Actor:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT actor_id, workspace_id FROM local_workspace_identity WHERE singleton = 1"
            ).fetchone()
        if row is None:  # pragma: no cover - initialization creates this atomically.
            raise LocalStandaloneRuntimeBlocked("本机基础案卷身份未初始化。")
        return Actor(
            actor_id=str(row["actor_id"]),
            firm_id=str(row["workspace_id"]),
            roles=frozenset({Role.LEAD_LAWYER}),
        )

    def create_case(
        self,
        *,
        actor: Actor,
        title: str,
        selection: LocalFolderSelection,
        idempotency_key: str,
    ) -> LocalCaseSummary:
        normalized_title = _normalize_title(title)
        _validate_uuid(selection.selection_id, label="folder selection")
        _validate_uuid(idempotency_key, label="idempotency key")
        payload_hash = _hash_json(
            {
                "title": normalized_title,
                "display_name": selection.display_name,
                "root_fingerprint": selection.root_fingerprint,
            }
        )
        now = _aware_now(None)
        with self._lock, self._connect() as connection:
            prior = connection.execute(
                """
                SELECT payload_hash, case_id
                FROM local_commands
                WHERE actor_id = ? AND command_name = 'CREATE_LOCAL_CASE' AND idempotency_key = ?
                """,
                (actor.actor_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if not compare_digest(str(prior["payload_hash"]), payload_hash):
                    raise LocalStandaloneConflict("本机建案操作已使用相同确认标识提交了不同内容。")
                return self._load_case(connection, actor=actor, case_id=str(prior["case_id"]))

            case_id = str(uuid4())
            event_id = str(uuid4())
            root_id = str(uuid4())
            timestamp = _format_time(now)
            connection.execute(
                """
                INSERT INTO local_cases(
                    case_id, actor_id, title, stage, version, created_at, updated_at
                ) VALUES (?, ?, ?, 'MATERIALS_PENDING', 1, ?, ?)
                """,
                (case_id, actor.actor_id, normalized_title, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO local_material_roots(
                    root_id, case_id, display_name, root_fingerprint, linked_at, linked_by, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    root_id,
                    case_id,
                    selection.display_name,
                    selection.root_fingerprint,
                    timestamp,
                    actor.actor_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO local_audit_events(
                    event_id, case_id, actor_id, event_type, input_version, output_version, occurred_at, payload_hash
                ) VALUES (?, ?, ?, 'LOCAL_CASE_CREATED', 0, 1, ?, ?)
                """,
                (event_id, case_id, actor.actor_id, timestamp, payload_hash),
            )
            connection.execute(
                """
                INSERT INTO local_commands(
                    actor_id, command_name, idempotency_key, payload_hash, case_id, audit_event_id, created_at
                ) VALUES (?, 'CREATE_LOCAL_CASE', ?, ?, ?, ?, ?)
                """,
                (actor.actor_id, idempotency_key, payload_hash, case_id, event_id, timestamp),
            )
            return self._load_case(connection, actor=actor, case_id=case_id)

    def list_cases(self, *, actor: Actor) -> tuple[LocalCaseSummary, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT case_id FROM local_cases WHERE actor_id = ? ORDER BY updated_at DESC, case_id DESC",
                (actor.actor_id,),
            ).fetchall()
            return tuple(
                self._load_case(connection, actor=actor, case_id=str(row["case_id"]))
                for row in rows
            )

    def get_case(self, *, actor: Actor, case_id: str) -> LocalCaseSummary:
        _validate_uuid(case_id, label="local case")
        with self._connect() as connection:
            return self._load_case(connection, actor=actor, case_id=case_id)

    def reconnect_material_root(
        self,
        *,
        actor: Actor,
        case_id: str,
        selection: LocalFolderSelection,
        idempotency_key: str,
    ) -> LocalCaseSummary:
        _validate_uuid(case_id, label="local case")
        _validate_uuid(selection.selection_id, label="folder selection")
        _validate_uuid(idempotency_key, label="idempotency key")
        payload_hash = _hash_json(
            {
                "case_id": case_id,
                "display_name": selection.display_name,
                "root_fingerprint": selection.root_fingerprint,
            }
        )
        now = _aware_now(None)
        with self._lock, self._connect() as connection:
            prior = connection.execute(
                """
                SELECT payload_hash, case_id FROM local_commands
                WHERE actor_id = ? AND command_name = 'RECONNECT_LOCAL_MATERIAL_ROOT' AND idempotency_key = ?
                """,
                (actor.actor_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if not compare_digest(str(prior["payload_hash"]), payload_hash):
                    raise LocalStandaloneConflict("本机资料根关联操作已使用相同确认标识提交了不同内容。")
                return self._load_case(connection, actor=actor, case_id=str(prior["case_id"]))

            current = self._load_case_row(connection, actor=actor, case_id=case_id)
            next_version = int(current["version"]) + 1
            timestamp = _format_time(now)
            event_id = str(uuid4())
            connection.execute(
                "UPDATE local_material_roots SET active = 0 WHERE case_id = ? AND active = 1",
                (case_id,),
            )
            connection.execute(
                """
                INSERT INTO local_material_roots(
                    root_id, case_id, display_name, root_fingerprint, linked_at, linked_by, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    str(uuid4()),
                    case_id,
                    selection.display_name,
                    selection.root_fingerprint,
                    timestamp,
                    actor.actor_id,
                ),
            )
            connection.execute(
                """
                UPDATE local_cases
                SET stage = 'MATERIALS_PENDING', version = ?, updated_at = ?
                WHERE case_id = ? AND actor_id = ?
                """,
                (next_version, timestamp, case_id, actor.actor_id),
            )
            connection.execute(
                """
                INSERT INTO local_audit_events(
                    event_id, case_id, actor_id, event_type, input_version, output_version, occurred_at, payload_hash
                ) VALUES (?, ?, ?, 'LOCAL_MATERIAL_ROOT_RECONNECTED', ?, ?, ?, ?)
                """,
                (event_id, case_id, actor.actor_id, int(current["version"]), next_version, timestamp, payload_hash),
            )
            connection.execute(
                """
                INSERT INTO local_commands(
                    actor_id, command_name, idempotency_key, payload_hash, case_id, audit_event_id, created_at
                ) VALUES (?, 'RECONNECT_LOCAL_MATERIAL_ROOT', ?, ?, ?, ?, ?)
                """,
                (actor.actor_id, idempotency_key, payload_hash, case_id, event_id, timestamp),
            )
            return self._load_case(connection, actor=actor, case_id=case_id)

    def record_inventory(
        self,
        *,
        actor: Actor,
        case_id: str,
        selection: LocalFolderSelection,
        manifest: FolderManifest,
        idempotency_key: str,
    ) -> LocalCaseSummary:
        _validate_uuid(case_id, label="local case")
        _validate_uuid(selection.selection_id, label="folder selection")
        _validate_uuid(idempotency_key, label="idempotency key")
        if manifest.root_fingerprint != selection.root_fingerprint:
            raise LocalStandaloneAccessBlocked("本次盘点与已选择的资料文件夹不一致。")
        payload_hash = _hash_json(
            {
                "case_id": case_id,
                "root_fingerprint": manifest.root_fingerprint,
                "manifest_hash": manifest.manifest_hash,
                "total_files": manifest.total_files,
                "total_bytes": manifest.total_bytes,
            }
        )
        now = _aware_now(None)
        with self._lock, self._connect() as connection:
            prior = connection.execute(
                """
                SELECT payload_hash, case_id FROM local_commands
                WHERE actor_id = ? AND command_name = 'INVENTORY_LOCAL_MATERIAL_ROOT' AND idempotency_key = ?
                """,
                (actor.actor_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if not compare_digest(str(prior["payload_hash"]), payload_hash):
                    raise LocalStandaloneConflict("本机盘点操作已使用相同确认标识提交了不同内容。")
                return self._load_case(connection, actor=actor, case_id=str(prior["case_id"]))

            current = self._load_case_row(connection, actor=actor, case_id=case_id)
            active_root = connection.execute(
                """
                SELECT root_fingerprint FROM local_material_roots
                WHERE case_id = ? AND active = 1
                """,
                (case_id,),
            ).fetchone()
            if active_root is None or str(active_root["root_fingerprint"]) != manifest.root_fingerprint:
                raise LocalStandaloneConflict("请先将当前选择的文件夹关联为本案资料根，再进行盘点。")
            next_version = int(current["version"]) + 1
            timestamp = _format_time(now)
            event_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO local_folder_inventories(
                    scan_id, case_id, root_fingerprint, manifest_hash, scanned_at,
                    total_files, total_bytes, skipped_symlinks, recorded_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.scan_id,
                    case_id,
                    manifest.root_fingerprint,
                    manifest.manifest_hash,
                    _format_time(manifest.scanned_at),
                    manifest.total_files,
                    manifest.total_bytes,
                    manifest.skipped_symlinks,
                    next_version,
                ),
            )
            connection.executemany(
                """
                INSERT INTO local_folder_inventory_items(
                    scan_id, ordinal, relative_path, byte_size, sha256, detected_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    (
                        manifest.scan_id,
                        ordinal,
                        item.relative_path,
                        item.byte_size,
                        item.sha256,
                        item.detected_kind,
                    )
                    for ordinal, item in enumerate(manifest.originals, start=1)
                ),
            )
            connection.execute(
                """
                UPDATE local_cases
                SET stage = 'MATERIALS_INVENTORIED', version = ?, updated_at = ?
                WHERE case_id = ? AND actor_id = ?
                """,
                (next_version, timestamp, case_id, actor.actor_id),
            )
            connection.execute(
                """
                INSERT INTO local_audit_events(
                    event_id, case_id, actor_id, event_type, input_version, output_version, occurred_at, payload_hash
                ) VALUES (?, ?, ?, 'LOCAL_MATERIAL_ROOT_INVENTORIED', ?, ?, ?, ?)
                """,
                (event_id, case_id, actor.actor_id, int(current["version"]), next_version, timestamp, payload_hash),
            )
            connection.execute(
                """
                INSERT INTO local_commands(
                    actor_id, command_name, idempotency_key, payload_hash, case_id, audit_event_id, created_at
                ) VALUES (?, 'INVENTORY_LOCAL_MATERIAL_ROOT', ?, ?, ?, ?, ?)
                """,
                (actor.actor_id, idempotency_key, payload_hash, case_id, event_id, timestamp),
            )
            return self._load_case(connection, actor=actor, case_id=case_id)

    def list_inventory_items(
        self,
        *,
        actor: Actor,
        case_id: str,
        scan_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[LocalFolderInventoryItem, ...]:
        _validate_uuid(case_id, label="local case")
        _validate_uuid(scan_id, label="folder inventory")
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("local inventory pagination is invalid")
        with self._connect() as connection:
            self._load_case_row(connection, actor=actor, case_id=case_id)
            inventory = connection.execute(
                "SELECT scan_id FROM local_folder_inventories WHERE scan_id = ? AND case_id = ?",
                (scan_id, case_id),
            ).fetchone()
            if inventory is None:
                raise LocalStandaloneNotFound("本机资料盘点不存在。")
            rows = connection.execute(
                """
                SELECT relative_path, byte_size, sha256, detected_kind
                FROM local_folder_inventory_items
                WHERE scan_id = ?
                ORDER BY ordinal ASC
                LIMIT ? OFFSET ?
                """,
                (scan_id, limit, offset),
            ).fetchall()
        return tuple(
            LocalFolderInventoryItem(
                relative_path=str(row["relative_path"]),
                byte_size=int(row["byte_size"]),
                sha256=str(row["sha256"]),
                detected_kind=str(row["detected_kind"]),
            )
            for row in rows
        )

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_workspace_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    actor_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_cases (
                    case_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK (stage IN ('MATERIALS_PENDING', 'MATERIALS_INVENTORIED')),
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_material_roots (
                    root_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES local_cases(case_id),
                    display_name TEXT NOT NULL,
                    root_fingerprint TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    linked_by TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS local_material_roots_one_active
                    ON local_material_roots(case_id) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS local_folder_inventories (
                    scan_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES local_cases(case_id),
                    root_fingerprint TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    total_files INTEGER NOT NULL CHECK (total_files >= 0),
                    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
                    skipped_symlinks INTEGER NOT NULL CHECK (skipped_symlinks >= 0),
                    recorded_version INTEGER NOT NULL CHECK (recorded_version >= 1)
                );
                CREATE INDEX IF NOT EXISTS local_folder_inventories_case_time
                    ON local_folder_inventories(case_id, scanned_at DESC, scan_id DESC);
                CREATE TABLE IF NOT EXISTS local_folder_inventory_items (
                    scan_id TEXT NOT NULL REFERENCES local_folder_inventories(scan_id),
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                    relative_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    sha256 TEXT NOT NULL,
                    detected_kind TEXT NOT NULL,
                    PRIMARY KEY (scan_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS local_audit_events (
                    event_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES local_cases(case_id),
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    input_version INTEGER NOT NULL CHECK (input_version >= 0),
                    output_version INTEGER NOT NULL CHECK (output_version >= 1),
                    occurred_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_commands (
                    actor_id TEXT NOT NULL,
                    command_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    case_id TEXT NOT NULL REFERENCES local_cases(case_id),
                    audit_event_id TEXT NOT NULL REFERENCES local_audit_events(event_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (actor_id, command_name, idempotency_key)
                );
                """
            )
            row = connection.execute(
                "SELECT actor_id FROM local_workspace_identity WHERE singleton = 1"
            ).fetchone()
            if row is None:
                timestamp = _format_time(_aware_now(None))
                connection.execute(
                    """
                    INSERT INTO local_workspace_identity(singleton, actor_id, workspace_id, created_at)
                    VALUES (1, ?, ?, ?)
                    """,
                    (str(uuid4()), str(uuid4()), timestamp),
                )
        try:
            os.chmod(self._database_path, 0o600)
        except OSError as error:  # pragma: no cover - platform filesystem fault.
            raise LocalStandaloneRuntimeBlocked("无法保护本机基础案卷账本文件权限。") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=5,
            isolation_level="IMMEDIATE",
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _load_case(self, connection: sqlite3.Connection, *, actor: Actor, case_id: str) -> LocalCaseSummary:
        row = self._load_case_row(connection, actor=actor, case_id=case_id)
        root = connection.execute(
            """
            SELECT display_name, root_fingerprint, linked_at
            FROM local_material_roots
            WHERE case_id = ? AND active = 1
            """,
            (case_id,),
        ).fetchone()
        if root is None:  # pragma: no cover - schema and commands retain an active root.
            raise LocalStandaloneRuntimeBlocked("本机案件缺少资料根引用。")
        inventory_row = connection.execute(
            """
            SELECT scan_id, root_fingerprint, manifest_hash, scanned_at, total_files, total_bytes, skipped_symlinks
            FROM local_folder_inventories
            WHERE case_id = ? AND root_fingerprint = ?
            ORDER BY scanned_at DESC, scan_id DESC
            LIMIT 1
            """,
            (case_id, str(root["root_fingerprint"])),
        ).fetchone()
        inventory = _inventory_from_row(inventory_row) if inventory_row is not None else None
        return LocalCaseSummary(
            case_id=str(row["case_id"]),
            title=str(row["title"]),
            stage=str(row["stage"]),
            matter_version=int(row["version"]),
            material_root=LocalMaterialRootReference(
                display_name=str(root["display_name"]),
                root_fingerprint=str(root["root_fingerprint"]),
                linked_at=_parse_time(str(root["linked_at"])),
            ),
            inventory=inventory,
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    @staticmethod
    def _load_case_row(
        connection: sqlite3.Connection, *, actor: Actor, case_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT case_id, actor_id, title, stage, version, created_at, updated_at
            FROM local_cases WHERE case_id = ? AND actor_id = ?
            """,
            (case_id, actor.actor_id),
        ).fetchone()
        if row is None:
            raise LocalStandaloneNotFound("本机基础案卷不存在或不属于当前安装。")
        return row


@dataclass(frozen=True)
class LocalStandaloneRuntime:
    settings: RuntimeSettings
    store: LocalStandaloneStore
    actor: Actor
    session_authority: DesktopSessionAuthority
    folder_selections: NativeFolderSelectionRegistry


def build_local_standalone_runtime(
    *,
    environ: Mapping[str, str],
    parent_api_token: str,
) -> LocalStandaloneRuntime:
    """Create a local-only runtime without enrollment, PostgreSQL, or network."""

    try:
        settings = RuntimeSettings.from_environment(
            environ,
            default_mode=RuntimeMode.LOCAL_STANDALONE,
        )
    except RuntimeConfigurationBlocked as error:
        raise LocalStandaloneRuntimeBlocked("本机基础案卷运行配置无效。") from error
    if settings.mode is not RuntimeMode.LOCAL_STANDALONE:
        raise LocalStandaloneRuntimeBlocked("当前运行模式不是本机基础案卷。")
    if not _valid_parent_token(parent_api_token):
        raise LocalStandaloneRuntimeBlocked("本机基础案卷父进程通道无效。")
    store = LocalStandaloneStore.open(_local_workspace_root(environ))
    actor = store.identity()
    now = _aware_now(None)
    try:
        authority = DesktopSessionAuthority(
            actor=actor,
            bootstrap_token=parent_api_token,
            bootstrap_expires_at=now + timedelta(seconds=45),
            session_expires_at=now + timedelta(minutes=30),
            issuer="lawcase-local-standalone-session",
        )
    except Exception as error:  # pragma: no cover - covers clock/platform faults.
        raise LocalStandaloneRuntimeBlocked("本机基础案卷会话无法初始化。") from error
    return LocalStandaloneRuntime(
        settings=settings,
        store=store,
        actor=actor,
        session_authority=authority,
        folder_selections=NativeFolderSelectionRegistry(),
    )


def _local_workspace_root(environ: Mapping[str, str]) -> Path:
    raw = environ.get(_LOCAL_ROOT_ENV, "").strip()
    if raw:
        root = Path(raw)
        if not root.is_absolute():
            raise LocalStandaloneRuntimeBlocked("本机基础案卷目录必须是绝对路径。")
        return root
    return Path.home() / "Library" / "Application Support" / "cn.lawcase.workbench" / "local-workspace"


def _prepare_private_workspace_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_absolute() or root == Path("/"):
        raise LocalStandaloneRuntimeBlocked("本机基础案卷目录必须是私有非符号链接目录。")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = root.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise LocalStandaloneRuntimeBlocked("无法准备本机基础案卷目录。") from error
    if not resolved.is_dir() or resolved == Path("/"):
        raise LocalStandaloneRuntimeBlocked("本机基础案卷目录不可用。")
    if metadata.st_mode & 0o077:
        try:
            os.chmod(resolved, 0o700)
            metadata = resolved.stat()
        except OSError as error:
            raise LocalStandaloneRuntimeBlocked("无法保护本机基础案卷目录权限。") from error
        if metadata.st_mode & 0o077:
            raise LocalStandaloneRuntimeBlocked("本机基础案卷目录不能向其他用户开放。")
    return resolved


def _resolve_safe_case_root(selected_root: str | Path) -> Path:
    raw = Path(selected_root)
    if raw.is_symlink():
        raise LocalStandaloneAccessBlocked("所选资料文件夹不能是符号链接。")
    try:
        root = raw.resolve(strict=True)
    except OSError as error:
        raise LocalStandaloneAccessBlocked("所选资料文件夹无法核验。") from error
    if not root.is_dir():
        raise LocalStandaloneAccessBlocked("所选位置不是可读取的资料文件夹。")
    if root == Path("/") or root == Path.home().resolve():
        raise LocalStandaloneAccessBlocked("不能将文件系统根目录或整个用户目录作为案件资料范围。")
    return root


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not 2 <= len(normalized) <= 160:
        raise LocalStandaloneAccessBlocked("案件工作名称应为 2 至 160 个字符。")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise LocalStandaloneAccessBlocked("案件工作名称包含不可用字符。")
    return normalized


def _validate_uuid(value: str, *, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise LocalStandaloneAccessBlocked(f"{label} 标识无效。") from error


def _valid_parent_token(value: str) -> bool:
    return len(value) == 64 and all(character in _SHA256_PATTERN for character in value)


def _hash_json(value: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise LocalStandaloneRuntimeBlocked("本机基础案卷时钟必须携带时区。")
    return current.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _aware_now(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_now(parsed)


def _inventory_from_row(row: sqlite3.Row) -> LocalFolderInventorySummary:
    return LocalFolderInventorySummary(
        scan_id=str(row["scan_id"]),
        root_fingerprint=str(row["root_fingerprint"]),
        manifest_hash=str(row["manifest_hash"]),
        scanned_at=_parse_time(str(row["scanned_at"])),
        total_files=int(row["total_files"]),
        total_bytes=int(row["total_bytes"]),
        skipped_symlinks=int(row["skipped_symlinks"]),
    )
