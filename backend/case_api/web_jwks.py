"""Bounded HTTPS JWKS cache for the self-hosted OIDC login path.

The JWT verifier deliberately accepts an injected ``JwksProvider``.  This
module is the production-side adapter: it fetches only one configured HTTPS
endpoint, does not follow redirects, bounds and validates the JSON document,
and keeps a short server-memory cache.  It never sees a browser cookie,
authorization code, client secret, or case material.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
from threading import Lock
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

from .persistent_identity import PersistentAuthenticationBlocked


__all__ = (
    "CachedHttpsJwksProvider",
    "HttpsJwksFetcher",
    "JwksFetchPolicy",
    "UrlLibHttpsJwksFetcher",
    "WebJwksBlocked",
)


_MAX_JWKS_DOCUMENT_BYTES = 128 * 1024


class WebJwksBlocked(PersistentAuthenticationBlocked):
    """The configured OIDC signing-key document is unavailable or unsafe."""


@dataclass(frozen=True)
class JwksFetchPolicy:
    """Fixed OIDC issuer/JWKS network boundary for one deployment."""

    issuer: str
    jwks_url: str
    refresh_interval: timedelta = timedelta(minutes=5)
    timeout_seconds: float = 5.0
    max_document_bytes: int = _MAX_JWKS_DOCUMENT_BYTES

    def __post_init__(self) -> None:
        normalized_issuer = _normalize_https_url(self.issuer, label="OIDC issuer", allow_path=True)
        normalized_jwks = _normalize_https_url(self.jwks_url, label="OIDC JWKS endpoint", allow_path=True)
        if _origin_of(normalized_issuer) != _origin_of(normalized_jwks):
            raise ValueError("OIDC JWKS endpoint must share the configured issuer origin")
        if not isinstance(self.refresh_interval, timedelta) or not timedelta(seconds=15) <= self.refresh_interval <= timedelta(hours=1):
            raise ValueError("OIDC JWKS refresh interval must be between fifteen seconds and one hour")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not isfinite(float(self.timeout_seconds))
            or not 1.0 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("OIDC JWKS timeout must be between one and thirty seconds")
        if not isinstance(self.max_document_bytes, int) or not 1_024 <= self.max_document_bytes <= 1_024 * 1_024:
            raise ValueError("OIDC JWKS document limit must be between 1 KiB and 1 MiB")
        object.__setattr__(self, "issuer", normalized_issuer)
        object.__setattr__(self, "jwks_url", normalized_jwks)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class HttpsJwksFetcher(Protocol):
    """Tiny injectable HTTPS document fetcher for controlled tests/composition."""

    def fetch(self, *, url: str, timeout_seconds: float, max_bytes: int) -> bytes: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


class UrlLibHttpsJwksFetcher:
    """Standard-library HTTPS JSON fetcher that refuses all redirects."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirect())

    def fetch(self, *, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
        request = UrlRequest(
            url,
            headers={"Accept": "application/json", "User-Agent": "lawcase-workbench-jwks/1"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                if response.getcode() != 200:
                    raise WebJwksBlocked("OIDC JWKS endpoint did not return success")
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid content type")
                body = response.read(max_bytes + 1)
        except WebJwksBlocked:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise WebJwksBlocked("OIDC JWKS endpoint is unavailable") from None
        if not isinstance(body, bytes) or not body or len(body) > max_bytes:
            raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid document")
        return body


class CachedHttpsJwksProvider:
    """Thread-safe, fail-closed short-lived cache of one JWKS document.

    A network failure after expiry is an authentication failure, rather than a
    stale-key fallback.  That intentionally prioritizes safe identity
    verification over silently accepting a signer whose provider is no longer
    reachable.  ``refresh_jwks`` exists for an unknown ``kid`` rotation path.
    """

    def __init__(
        self,
        *,
        policy: JwksFetchPolicy,
        fetcher: HttpsJwksFetcher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, JwksFetchPolicy):
            raise ValueError("OIDC JWKS fetch policy is required")
        fetcher = fetcher or UrlLibHttpsJwksFetcher()
        if not callable(getattr(fetcher, "fetch", None)):
            raise ValueError("OIDC JWKS fetcher is invalid")
        self._policy = policy
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        _aware_datetime(self._clock())
        self._document: Mapping[str, Any] | None = None
        self._expires_at: datetime | None = None
        self._lock = Lock()

    def load_jwks(self) -> Mapping[str, Any]:
        """Return a defensive copy of the current validated signing-key set."""

        return self._load(force=False)

    def refresh_jwks(self) -> Mapping[str, Any]:
        """Force one bounded refresh after the verifier encounters an unknown key ID."""

        return self._load(force=True)

    def _load(self, *, force: bool) -> Mapping[str, Any]:
        now = _aware_datetime(self._clock())
        with self._lock:
            if not force and self._document is not None and self._expires_at is not None and now < self._expires_at:
                return deepcopy(self._document)
            try:
                raw = self._fetcher.fetch(
                    url=self._policy.jwks_url,
                    timeout_seconds=self._policy.timeout_seconds,
                    max_bytes=self._policy.max_document_bytes,
                )
                document = _parse_jwks(raw)
            except WebJwksBlocked:
                raise
            except Exception:
                raise WebJwksBlocked("OIDC JWKS endpoint is unavailable") from None
            self._document = document
            self._expires_at = now + self._policy.refresh_interval
            return deepcopy(document)


def _parse_jwks(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or not raw:
        raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid document")
    try:
        text = raw.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid document") from None
    if not isinstance(document, Mapping):
        raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid document")
    keys = document.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 32 or not all(isinstance(key, Mapping) for key in keys):
        raise WebJwksBlocked("OIDC JWKS endpoint returned an invalid document")
    return document


def _normalize_https_url(value: object, *, label: str, allow_path: bool) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError(f"{label} is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path)
    ):
        raise ValueError(f"{label} must be a canonical HTTPS URL")
    normalized = f"https://{hostname.lower()}"
    if port is not None and port != 443:
        normalized += f":{port}"
    normalized += parsed.path
    if value != normalized:
        raise ValueError(f"{label} must be canonical")
    return normalized


def _origin_of(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:  # pragma: no cover - validated before this call.
        raise ValueError("OIDC URL is invalid")
    return f"https://{hostname.lower()}" + (f":{parsed.port}" if parsed.port not in {None, 443} else "")


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("OIDC JWKS clock must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    try:
        timestamp = normalized.timestamp()
    except (OSError, OverflowError, ValueError):
        raise ValueError("OIDC JWKS clock is invalid") from None
    if not isfinite(timestamp):
        raise ValueError("OIDC JWKS clock is invalid")
    return normalized


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON value")
