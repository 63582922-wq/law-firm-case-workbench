"""HTTP boundary for the local-first standalone workspace.

Only a Tauri parent process holding the per-process parent token can submit an
absolute folder path or cause a file inventory.  The WebView receives a short
loopback session only after the native parent exchanges its one-use bootstrap;
that session can read local case metadata but cannot create a path capability,
scan a folder, call a model, or reach a firm-managed workflow.
"""

from __future__ import annotations

from datetime import datetime
from hmac import compare_digest
from ipaddress import ip_address
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from case_api.local_standalone_runtime import (
    LocalCaseSummary,
    LocalFolderInventoryItem,
    LocalFolderInventorySummary,
    LocalFolderSelection,
    LocalStandaloneAccessBlocked,
    LocalStandaloneConflict,
    LocalStandaloneNotFound,
    LocalStandaloneRuntime,
)
from case_api.persistent_identity import PersistentAuthenticationBlocked, ServerIdentityContext
from case_kernel.local_case_folder import FolderScanBlocked, scan_case_folder


_MAX_NATIVE_SELECTION_PATH = 4_096
_MAX_NATIVE_RESPONSE_BYTES = 65_536


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalFolderSelectionResponse(_StrictModel):
    selection_id: UUID
    display_name: str
    root_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_at: datetime


class LocalMaterialRootResponse(_StrictModel):
    display_name: str
    root_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    linked_at: datetime


class LocalFolderInventoryResponse(_StrictModel):
    scan_id: UUID
    root_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scanned_at: datetime
    total_files: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    skipped_symlinks: int = Field(ge=0)


class LocalCaseResponse(_StrictModel):
    case_id: UUID
    title: str
    stage: str
    matter_version: int = Field(ge=1)
    material_root: LocalMaterialRootResponse
    inventory: LocalFolderInventoryResponse | None
    created_at: datetime
    updated_at: datetime


class LocalCaseListResponse(_StrictModel):
    cases: tuple[LocalCaseResponse, ...]


class NativeFolderSelectionRequest(_StrictModel):
    selected_root: str = Field(min_length=1, max_length=_MAX_NATIVE_SELECTION_PATH)


class NativeCreateLocalCaseRequest(_StrictModel):
    title: str = Field(min_length=1, max_length=240)
    selection_id: UUID


class NativeReconnectMaterialRootRequest(_StrictModel):
    selection_id: UUID


class NativeInventoryRequest(_StrictModel):
    selection_id: UUID


class LocalInventoryItemResponse(_StrictModel):
    relative_path: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detected_kind: str


class LocalInventoryPageResponse(_StrictModel):
    scan_id: UUID
    items: tuple[LocalInventoryItemResponse, ...]
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)


class LocalCapabilitiesResponse(_StrictModel):
    workspace_mode: str
    case_creation: str
    material_root_reference: str
    read_only_inventory: str
    formal_facts_and_calculation: str
    court_submission: str
    external_model_execution: str
    external_network: str


def create_local_standalone_app(
    runtime: LocalStandaloneRuntime,
    *,
    native_parent_api_token: str,
) -> FastAPI:
    """Build a no-docs, loopback-only local application boundary."""

    if len(native_parent_api_token) != 64 or any(
        character not in "0123456789abcdef" for character in native_parent_api_token
    ):
        raise ValueError("local standalone parent token is invalid")
    app = FastAPI(
        title="律师办案工作台 · 本机基础案卷",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["tauri://localhost"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Authorization"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def response_hardening(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(LocalStandaloneAccessBlocked)
    async def local_access_blocked(_: Request, exc: LocalStandaloneAccessBlocked):
        return _error(status.HTTP_403_FORBIDDEN, "LOCAL_WORKSPACE_ACCESS_BLOCKED", str(exc))

    @app.exception_handler(LocalStandaloneNotFound)
    async def local_not_found(_: Request, exc: LocalStandaloneNotFound):
        return _error(status.HTTP_404_NOT_FOUND, "LOCAL_CASE_NOT_FOUND", str(exc))

    @app.exception_handler(LocalStandaloneConflict)
    async def local_conflict(_: Request, exc: LocalStandaloneConflict):
        return _error(status.HTTP_409_CONFLICT, "LOCAL_WORKSPACE_CONFLICT", str(exc))

    @app.exception_handler(PersistentAuthenticationBlocked)
    async def local_session_blocked(_: Request, exc: PersistentAuthenticationBlocked):
        del exc
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "LOCAL_SESSION_UNAVAILABLE",
            "本机会话不可用或已到期；请重新启动工作台。",
        )

    @app.exception_handler(FolderScanBlocked)
    async def folder_scan_blocked(_: Request, exc: FolderScanBlocked):
        return _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "LOCAL_FOLDER_INVENTORY_BLOCKED",
            f"本机资料盘点未完成：{str(exc) or '范围不符合安全限制。'}",
        )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {
            "service": "lawcase-local-api",
            "mode": "local-standalone",
            "persistence": "local-configured",
            "external_network": "disabled",
            "external_model_execution": "disabled",
        }

    @app.post("/v1/desktop-sessions/exchange", tags=["desktop-session"])
    async def exchange_local_session(
        request: Request,
        response: Response,
        x_desktop_bootstrap: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        if x_desktop_bootstrap is None:
            raise PersistentAuthenticationBlocked("desktop bootstrap token is missing")
        grant = runtime.session_authority.exchange(
            request=request,
            bootstrap_token=x_desktop_bootstrap,
        )
        response.headers["Pragma"] = "no-cache"
        return {
            "status": "SESSION_READY",
            "access_token": grant.access_token,
            "session_id": grant.session_id,
            "expires_at": grant.expires_at,
        }

    async def get_identity(request: Request) -> ServerIdentityContext:
        identity = await runtime.session_authority.resolve(request)
        # The session is local-device access only, not a law-firm enrollment.
        if identity.actor.actor_id != runtime.actor.actor_id or identity.actor.firm_id != runtime.actor.firm_id:
            raise PersistentAuthenticationBlocked("local workspace session actor is invalid")
        return identity

    def require_native_parent(request: Request) -> None:
        client_host = request.client.host if request.client is not None else ""
        try:
            if not ip_address(client_host).is_loopback:
                raise LocalStandaloneAccessBlocked("本机资料操作只能由桌面主进程发起。")
        except ValueError as error:
            raise LocalStandaloneAccessBlocked("本机资料操作来源无效。") from error
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {native_parent_api_token}"
        if not compare_digest(authorization, expected):
            # Do not disclose that an untrusted local request reached a native route.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    @app.post(
        "/v1/native-local/folder-selections",
        response_model=LocalFolderSelectionResponse,
        tags=["native-local"],
    )
    async def register_native_folder_selection(
        request: Request,
        body: NativeFolderSelectionRequest,
    ) -> LocalFolderSelectionResponse:
        require_native_parent(request)
        selection = runtime.folder_selections.register(body.selected_root)
        return _selection_response(selection)

    @app.post(
        "/v1/native-local/cases",
        response_model=LocalCaseResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["native-local"],
    )
    async def create_native_local_case(
        request: Request,
        body: NativeCreateLocalCaseRequest,
    ) -> LocalCaseResponse:
        require_native_parent(request)
        selection = runtime.folder_selections.resolve(str(body.selection_id))
        summary = runtime.store.create_case(
            actor=runtime.actor,
            title=body.title,
            selection=selection.selection,
            idempotency_key=str(body.selection_id),
        )
        return _case_response(summary)

    @app.get(
        "/v1/native-local/cases",
        response_model=LocalCaseListResponse,
        tags=["native-local"],
    )
    async def list_native_local_cases(request: Request) -> LocalCaseListResponse:
        require_native_parent(request)
        return LocalCaseListResponse(cases=tuple(_case_response(item) for item in runtime.store.list_cases(actor=runtime.actor)))

    @app.get(
        "/v1/native-local/cases/{case_id}",
        response_model=LocalCaseResponse,
        tags=["native-local"],
    )
    async def open_native_local_case(request: Request, case_id: UUID) -> LocalCaseResponse:
        require_native_parent(request)
        return _case_response(runtime.store.get_case(actor=runtime.actor, case_id=str(case_id)))

    @app.post(
        "/v1/native-local/cases/{case_id}/material-root",
        response_model=LocalCaseResponse,
        tags=["native-local"],
    )
    async def reconnect_native_material_root(
        request: Request,
        case_id: UUID,
        body: NativeReconnectMaterialRootRequest,
    ) -> LocalCaseResponse:
        require_native_parent(request)
        selection = runtime.folder_selections.resolve(str(body.selection_id))
        return _case_response(
            runtime.store.reconnect_material_root(
                actor=runtime.actor,
                case_id=str(case_id),
                selection=selection.selection,
                idempotency_key=str(body.selection_id),
            )
        )

    @app.post(
        "/v1/native-local/cases/{case_id}/folder-inventory",
        response_model=LocalCaseResponse,
        tags=["native-local"],
    )
    async def inventory_native_material_root(
        request: Request,
        case_id: UUID,
        body: NativeInventoryRequest,
    ) -> LocalCaseResponse:
        require_native_parent(request)
        selection = runtime.folder_selections.resolve(str(body.selection_id))
        manifest = scan_case_folder(
            selection.root,
            confirmed_root_fingerprint=selection.selection.root_fingerprint,
        )
        return _case_response(
            runtime.store.record_inventory(
                actor=runtime.actor,
                case_id=str(case_id),
                selection=selection.selection,
                manifest=manifest,
                idempotency_key=str(body.selection_id),
            )
        )

    @app.get(
        "/v1/local-standalone/capabilities",
        response_model=LocalCapabilitiesResponse,
        tags=["local-standalone"],
    )
    async def get_local_capabilities(
        _: ServerIdentityContext = Depends(get_identity),
    ) -> LocalCapabilitiesResponse:
        return LocalCapabilitiesResponse(
            workspace_mode="LOCAL_STANDALONE",
            case_creation="AVAILABLE",
            material_root_reference="AVAILABLE",
            read_only_inventory="AVAILABLE_BY_NATIVE_SELECTION",
            formal_facts_and_calculation="FIRM_MANAGED_REQUIRED",
            court_submission="FIRM_MANAGED_REQUIRED",
            external_model_execution="DISABLED",
            external_network="DISABLED",
        )

    @app.get(
        "/v1/local-standalone/cases",
        response_model=LocalCaseListResponse,
        tags=["local-standalone"],
    )
    async def list_local_cases(
        identity: ServerIdentityContext = Depends(get_identity),
    ) -> LocalCaseListResponse:
        return LocalCaseListResponse(cases=tuple(_case_response(item) for item in runtime.store.list_cases(actor=identity.actor)))

    @app.get(
        "/v1/local-standalone/cases/{case_id}",
        response_model=LocalCaseResponse,
        tags=["local-standalone"],
    )
    async def get_local_case(
        case_id: UUID,
        identity: ServerIdentityContext = Depends(get_identity),
    ) -> LocalCaseResponse:
        return _case_response(runtime.store.get_case(actor=identity.actor, case_id=str(case_id)))

    @app.get(
        "/v1/local-standalone/cases/{case_id}/folder-inventories/{scan_id}/items",
        response_model=LocalInventoryPageResponse,
        tags=["local-standalone"],
    )
    async def get_local_inventory_items(
        case_id: UUID,
        scan_id: UUID,
        identity: ServerIdentityContext = Depends(get_identity),
        limit: int = 100,
        offset: int = 0,
    ) -> LocalInventoryPageResponse:
        items = runtime.store.list_inventory_items(
            actor=identity.actor,
            case_id=str(case_id),
            scan_id=str(scan_id),
            limit=limit,
            offset=offset,
        )
        # A bounded page has no unreliable optimistic "has more" flag.  One
        # extra record is intentionally not fetched, so callers only request
        # the next page after seeing a full prior page.
        next_offset = offset + len(items) if len(items) == limit else None
        return LocalInventoryPageResponse(
            scan_id=scan_id,
            items=tuple(_inventory_item_response(item) for item in items),
            offset=offset,
            next_offset=next_offset,
        )

    return app


def _case_response(summary: LocalCaseSummary) -> LocalCaseResponse:
    return LocalCaseResponse(
        case_id=UUID(summary.case_id),
        title=summary.title,
        stage=summary.stage,
        matter_version=summary.matter_version,
        material_root=LocalMaterialRootResponse(
            display_name=summary.material_root.display_name,
            root_fingerprint=summary.material_root.root_fingerprint,
            linked_at=summary.material_root.linked_at,
        ),
        inventory=(
            LocalFolderInventoryResponse(
                scan_id=UUID(summary.inventory.scan_id),
                root_fingerprint=summary.inventory.root_fingerprint,
                manifest_hash=summary.inventory.manifest_hash,
                scanned_at=summary.inventory.scanned_at,
                total_files=summary.inventory.total_files,
                total_bytes=summary.inventory.total_bytes,
                skipped_symlinks=summary.inventory.skipped_symlinks,
            )
            if summary.inventory is not None
            else None
        ),
        created_at=summary.created_at,
        updated_at=summary.updated_at,
    )


def _selection_response(selection: LocalFolderSelection) -> LocalFolderSelectionResponse:
    return LocalFolderSelectionResponse(
        selection_id=UUID(selection.selection_id),
        display_name=selection.display_name,
        root_fingerprint=selection.root_fingerprint,
        selected_at=selection.selected_at,
    )


def _inventory_item_response(item: LocalFolderInventoryItem) -> LocalInventoryItemResponse:
    return LocalInventoryItemResponse(
        relative_path=item.relative_path,
        byte_size=item.byte_size,
        sha256=item.sha256,
        detected_kind=item.detected_kind,
    )


def _error(status_code: int, code: str, message: str) -> Response:
    # Do not include filesystem locations, parent tokens, or traceback text.
    body = {"code": code, "message": message}
    encoded = json_dumps(body)
    if len(encoded) > _MAX_NATIVE_RESPONSE_BYTES:  # pragma: no cover - defensive cap.
        encoded = json_dumps({"code": "LOCAL_WORKSPACE_ERROR", "message": "本机基础案卷操作未完成。"})
    return Response(content=encoded, status_code=status_code, media_type="application/json")


def json_dumps(value: object) -> str:
    # Keep the failure envelope entirely deterministic and avoid serializing
    # unknown exception objects.
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
