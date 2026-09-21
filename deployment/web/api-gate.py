"""Deliberate setup gate for the cross-platform Web workbench.

This process is used only when the deployment has not supplied the complete
production Web configuration. It must never look like a usable case service or
fall back to a demonstration matter.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


EXPECTED_MODE = "SETUP_GATED"
RUNTIME_MODE = os.environ.get("LAWCASE_WEB_RUNTIME_MODE", EXPECTED_MODE).strip()

app = FastAPI(
    title="律所案件 AI 工作台 · Web 部署前置检查",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def status_payload(*, code: str) -> dict[str, Any]:
    return {
        "service": "lawcase-web-api",
        "status": "SETUP_GATED" if RUNTIME_MODE == EXPECTED_MODE else "MISCONFIGURED",
        "code": code,
        "case_routes": "DISABLED",
        "uploads": "DISABLED",
        "object_access": "DISABLED",
        "reason": (
            "浏览器身份、上传隔离、对象存储访问和受控 Worker 尚未装配；"
            "本容器不会处理真实案件材料。"
        ),
    }


@app.middleware("http")
async def no_store(_: Request, call_next):
    response = await call_next(_)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/healthz")
async def healthz() -> JSONResponse:
    if RUNTIME_MODE != EXPECTED_MODE:
        return JSONResponse(status_code=503, content=status_payload(code="WEB_RUNTIME_MODE_INVALID"))
    return JSONResponse(status_code=200, content=status_payload(code="WEB_RUNTIME_SETUP_REQUIRED"))


@app.get("/setup-status")
@app.get("/readyz")
async def setup_status() -> JSONResponse:
    return JSONResponse(status_code=503, content=status_payload(code="WEB_RUNTIME_SETUP_REQUIRED"))


@app.api_route("/{requested_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def block_case_route(requested_path: str) -> JSONResponse:
    del requested_path
    return JSONResponse(status_code=503, content=status_payload(code="WEB_CASE_ROUTE_NOT_ENABLED"))
