"""Strict, version-bound keyset cursors for large case projections.

Cursors are positioning data, never authorization.  Every paged query must
still re-authorize firm, matter and role in the database transaction.  The
canonical token prevents ambiguous parsing and binds subsequent pages to the
same matter version without introducing a new server-side session store.
"""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass
import json
import re
from uuid import UUID


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_CURSOR_BYTES = 512
_CURSOR_VERSION = 1
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,512}$")
_KIND = re.compile(r"^[A-Z][A-Z0-9_]{2,39}$")
_SORT_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{1,96}$")
_FIELDS = frozenset({"v", "kind", "matter_id", "matter_version", "sort_values"})


class StablePaginationBlocked(ValueError):
    """A page size or cursor failed the strict projection boundary."""


@dataclass(frozen=True)
class StablePageCursor:
    kind: str
    matter_id: str
    matter_version: int
    sort_values: tuple[str, ...]


def validate_page_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise StablePaginationBlocked("page size must be between 1 and 100")
    return value


def encode_page_cursor(
    *,
    kind: str,
    matter_id: str,
    matter_version: int,
    sort_values: tuple[str, ...],
) -> str:
    cursor = _validated_cursor(
        kind=kind,
        matter_id=matter_id,
        matter_version=matter_version,
        sort_values=sort_values,
    )
    raw = _canonical_payload(cursor)
    token = urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(token.encode("ascii")) > MAX_CURSOR_BYTES:
        raise StablePaginationBlocked("page cursor is too long")
    return token


def decode_page_cursor(
    token: str,
    *,
    expected_kind: str,
    expected_matter_id: str,
) -> StablePageCursor:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise StablePaginationBlocked("page cursor is invalid")
    try:
        padding = "=" * ((4 - len(token) % 4) % 4)
        raw = b64decode((token + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise StablePaginationBlocked("page cursor is invalid") from error
    if not raw or len(raw) > MAX_CURSOR_BYTES:
        raise StablePaginationBlocked("page cursor is invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise StablePaginationBlocked("page cursor contains duplicate fields")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except StablePaginationBlocked:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StablePaginationBlocked("page cursor is invalid") from error
    if not isinstance(payload, dict) or frozenset(payload) != _FIELDS:
        raise StablePaginationBlocked("page cursor fields are invalid")
    if payload.get("v") != _CURSOR_VERSION or isinstance(payload.get("v"), bool):
        raise StablePaginationBlocked("page cursor version is invalid")
    sort_values = payload.get("sort_values")
    if not isinstance(sort_values, list) or any(not isinstance(item, str) for item in sort_values):
        raise StablePaginationBlocked("page cursor sort key is invalid")
    cursor = _validated_cursor(
        kind=payload.get("kind"),
        matter_id=payload.get("matter_id"),
        matter_version=payload.get("matter_version"),
        sort_values=tuple(sort_values),
    )
    if cursor.kind != expected_kind or cursor.matter_id != expected_matter_id:
        raise StablePaginationBlocked("page cursor scope does not match this projection")
    if _canonical_payload(cursor) != raw or encode_page_cursor(**cursor.__dict__) != token:
        raise StablePaginationBlocked("page cursor is not canonical")
    return cursor


def _validated_cursor(
    *,
    kind: object,
    matter_id: object,
    matter_version: object,
    sort_values: tuple[object, ...],
) -> StablePageCursor:
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise StablePaginationBlocked("page cursor kind is invalid")
    try:
        parsed_matter_id = str(UUID(matter_id)) if isinstance(matter_id, str) else ""
    except ValueError as error:
        raise StablePaginationBlocked("page cursor matter is invalid") from error
    if parsed_matter_id != matter_id:
        raise StablePaginationBlocked("page cursor matter is invalid")
    if type(matter_version) is not int or matter_version < 1:
        raise StablePaginationBlocked("page cursor version is invalid")
    if not 1 <= len(sort_values) <= 4 or any(
        not isinstance(value, str)
        or not _SORT_VALUE.fullmatch(value)
        or value != value.strip()
        for value in sort_values
    ):
        raise StablePaginationBlocked("page cursor sort key is invalid")
    return StablePageCursor(
        kind=kind,
        matter_id=matter_id,
        matter_version=matter_version,
        sort_values=tuple(sort_values),
    )


def _canonical_payload(cursor: StablePageCursor) -> bytes:
    return json.dumps(
        {
            "v": _CURSOR_VERSION,
            "kind": cursor.kind,
            "matter_id": cursor.matter_id,
            "matter_version": cursor.matter_version,
            "sort_values": list(cursor.sort_values),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
