"""Per-request correlation identity shared by API, audit, and outbox writes."""

from __future__ import annotations

from contextvars import ContextVar, Token


_request_id: ContextVar[str | None] = ContextVar("case_workbench_request_id", default=None)


def set_request_id(request_id: str) -> Token:
    return _request_id.set(request_id)


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()
