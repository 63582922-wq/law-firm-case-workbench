"""Load release-pinned enrollment trust without inventing deployment keys.

The repository ships a deliberately disabled bootstrap. A production release
must replace it at packaging time with offline root public keys and a
threshold-signed catalog. No private key belongs in this file, the app bundle,
or the browser process.
"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Callable

from .trusted_enrollment_catalog import (
    EnrollmentTrustCatalogBlocked,
    TrustedCatalogRoot,
    TrustedEnrollmentCatalogVerifier,
    VerifiedEnrollmentTrustCatalog,
)


BOOTSTRAP_SCHEMA_VERSION = "lawcase-enrollment-trust-bootstrap-v1"
MAX_BOOTSTRAP_BYTES = 131_072
_BOOTSTRAP_FIELDS = frozenset(
    {"schema_version", "deployment_state", "threshold", "roots", "catalog_envelope"}
)
_ROOT_FIELDS = frozenset({"key_id", "public_key"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DesktopTrustBootstrapBlocked(PermissionError):
    """The bundled deployment trust bootstrap failed closed."""


@dataclass(frozen=True)
class DesktopEnrollmentTrustRuntime:
    phase: str
    message: str
    catalog_version: int | None = None
    catalog_sha256: str | None = None
    enrollment_api_origin: str | None = None
    catalog_expires_at: str | None = None
    active_issuer_key_count: int = 0
    catalog: VerifiedEnrollmentTrustCatalog | None = field(default=None, repr=False)


def default_bootstrap_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str) and bundle_root:
        return Path(bundle_root) / "case_api" / "deployment" / "enrollment_trust_bootstrap.json"
    return Path(__file__).resolve().parent / "deployment" / "enrollment_trust_bootstrap.json"


def load_desktop_enrollment_trust(
    path: Path | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> DesktopEnrollmentTrustRuntime:
    bootstrap_path = path or default_bootstrap_path()
    try:
        if bootstrap_path.is_symlink() or not bootstrap_path.is_file():
            raise DesktopTrustBootstrapBlocked("desktop enrollment trust bootstrap is unavailable")
        raw = bootstrap_path.read_bytes()
    except OSError as error:
        raise DesktopTrustBootstrapBlocked(
            "desktop enrollment trust bootstrap cannot be read"
        ) from error
    if not raw or len(raw) > MAX_BOOTSTRAP_BYTES or raw.startswith(b"\xef\xbb\xbf"):
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust bootstrap size is invalid")
    parsed = _strict_json(raw)
    if frozenset(parsed) != _BOOTSTRAP_FIELDS:
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust bootstrap fields are invalid")
    if parsed.get("schema_version") != BOOTSTRAP_SCHEMA_VERSION:
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust bootstrap schema is unsupported")

    deployment_state = parsed.get("deployment_state")
    if deployment_state == "NOT_CONFIGURED":
        if (
            type(parsed.get("threshold")) is not int
            or parsed.get("threshold") != 0
            or parsed.get("roots") != []
            or parsed.get("catalog_envelope") is not None
        ):
            raise DesktopTrustBootstrapBlocked(
                "disabled enrollment trust bootstrap cannot contain deployment trust"
            )
        return DesktopEnrollmentTrustRuntime(
            phase="NOT_CONFIGURED",
            message="生产信任目录尚未配置；律所登记保持禁用。",
        )
    if deployment_state != "PINNED":
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust deployment state is invalid")

    roots = _load_roots(parsed.get("roots"))
    threshold = parsed.get("threshold")
    if type(threshold) is not int or threshold < 1 or threshold > len(roots):
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust threshold is invalid")
    envelope = parsed.get("catalog_envelope")
    if not isinstance(envelope, dict):
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust catalog envelope is invalid")
    try:
        envelope_text = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        catalog = TrustedEnrollmentCatalogVerifier(
            roots=roots,
            threshold=threshold,
            clock=clock,
        ).verify(envelope_text)
    except (EnrollmentTrustCatalogBlocked, TypeError, ValueError) as error:
        raise DesktopTrustBootstrapBlocked(
            "desktop enrollment trust catalog verification failed"
        ) from error
    active_count = sum(1 for key in catalog.issuer_keys if key.status.value == "ACTIVE")
    return DesktopEnrollmentTrustRuntime(
        phase="READY",
        message="生产信任目录已通过门限签名、时效与回滚核验。",
        catalog_version=catalog.catalog_version,
        catalog_sha256=catalog.catalog_sha256,
        enrollment_api_origin=catalog.enrollment_api_origin,
        catalog_expires_at=catalog.expires_at.isoformat().replace("+00:00", "Z"),
        active_issuer_key_count=active_count,
        catalog=catalog,
    )


def blocked_desktop_enrollment_trust() -> DesktopEnrollmentTrustRuntime:
    return DesktopEnrollmentTrustRuntime(
        phase="BLOCKED",
        message="生产信任目录核验失败；律所登记保持禁用。",
    )


def _load_roots(value: object) -> dict[str, TrustedCatalogRoot]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust roots are invalid")
    roots: dict[str, TrustedCatalogRoot] = {}
    key_ids: list[str] = []
    for entry in value:
        if not isinstance(entry, dict) or frozenset(entry) != _ROOT_FIELDS:
            raise DesktopTrustBootstrapBlocked("desktop enrollment trust root fields are invalid")
        key_id = entry.get("key_id")
        public_key = entry.get("public_key")
        if not isinstance(key_id, str) or not _IDENTIFIER.fullmatch(key_id) or key_id in roots:
            raise DesktopTrustBootstrapBlocked("desktop enrollment trust root key is invalid")
        if not isinstance(public_key, str) or any(char.isspace() for char in public_key):
            raise DesktopTrustBootstrapBlocked("desktop enrollment trust root public key is invalid")
        try:
            decoded = b64decode(public_key, validate=True)
            root = TrustedCatalogRoot(key_id=key_id, public_key_bytes=decoded)
        except (ValueError, UnicodeEncodeError, EnrollmentTrustCatalogBlocked) as error:
            raise DesktopTrustBootstrapBlocked(
                "desktop enrollment trust root public key is invalid"
            ) from error
        roots[key_id] = root
        key_ids.append(key_id)
    if key_ids != sorted(key_ids):
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust roots are not canonical")
    return roots


def _strict_json(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DesktopTrustBootstrapBlocked(
                    "desktop enrollment trust bootstrap contains duplicate fields"
                )
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except DesktopTrustBootstrapBlocked:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopTrustBootstrapBlocked(
            "desktop enrollment trust bootstrap is invalid JSON"
        ) from error
    if not isinstance(parsed, dict):
        raise DesktopTrustBootstrapBlocked("desktop enrollment trust bootstrap must be an object")
    return parsed
