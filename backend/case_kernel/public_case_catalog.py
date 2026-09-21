"""Strict metadata catalog for real, official public-case research candidates.

The catalog deliberately contains no judgment body.  A public URL is not a
license for bulk capture, redistribution, model training, or formal reliance.
Candidates can guide discovery and synthetic regression design only until an
authorized lawyer reviews the exact archived bytes and the source licence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class PublicCaseCatalogBlocked(ValueError):
    """The public-case catalog violates a provenance or licence invariant."""


class LicenseStatus(StrEnum):
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class CandidateStatus(StrEnum):
    DISCOVERED_OFFICIAL = "DISCOVERED_OFFICIAL"
    LICENSE_REVIEW_REQUIRED = "LICENSE_REVIEW_REQUIRED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    FORMAL_USE_BLOCKED = "FORMAL_USE_BLOCKED"


class AcquisitionMode(StrEnum):
    METADATA_ONLY = "METADATA_ONLY"
    LAWYER_INITIATED_DOWNLOAD = "LAWYER_INITIATED_DOWNLOAD"
    INDIVIDUAL_AUTHORIZED_CAPTURE = "INDIVIDUAL_AUTHORIZED_CAPTURE"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


@dataclass(frozen=True)
class PublicCaseSourcePolicy:
    source_id: str
    publisher: str
    authority_level: str
    official_url: str
    allowed_domains: frozenset[str]
    approved_acquisition_modes: tuple[AcquisitionMode, ...]
    license_status: LicenseStatus
    bulk_capture_allowed: bool
    full_text_repository_allowed: bool
    commercial_training_allowed: bool


@dataclass(frozen=True)
class PublicCaseCandidate:
    candidate_id: str
    source_id: str
    title: str
    official_url: str
    discovered_on: date
    issue_tags: tuple[str, ...]
    evaluation_uses: tuple[str, ...]
    acquisition_mode: AcquisitionMode
    status: CandidateStatus
    formal_use_allowed: bool


@dataclass(frozen=True)
class PublicCaseCatalog:
    schema_version: str
    verified_on: date
    sources: tuple[PublicCaseSourcePolicy, ...]
    candidates: tuple[PublicCaseCandidate, ...]

    @classmethod
    def load(cls, path: Path) -> "PublicCaseCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PublicCaseCatalogBlocked("public-case catalog is unreadable") from error
        if not isinstance(payload, dict):
            raise PublicCaseCatalogBlocked("public-case catalog must be an object")
        _require_exact_fields(payload, {"schema_version", "verified_on", "sources", "candidates"}, "catalog")
        if payload["schema_version"] != "official-public-case-catalog-v1":
            raise PublicCaseCatalogBlocked("public-case catalog schema version is unsupported")
        verified_on = _date(payload["verified_on"], "verified_on")
        sources = tuple(_source(item) for item in _list(payload["sources"], "sources"))
        if not sources:
            raise PublicCaseCatalogBlocked("at least one public-case source is required")
        source_by_id = _unique_by_id(sources, "source")
        candidates = tuple(
            _candidate(item, source_by_id=source_by_id) for item in _list(payload["candidates"], "candidates")
        )
        if not candidates:
            raise PublicCaseCatalogBlocked("at least one public-case candidate is required")
        _unique_by_id(candidates, "candidate")
        return cls(
            schema_version=payload["schema_version"],
            verified_on=verified_on,
            sources=sources,
            candidates=candidates,
        )


def _source(value: Any) -> PublicCaseSourcePolicy:
    if not isinstance(value, dict):
        raise PublicCaseCatalogBlocked("public-case source must be an object")
    _require_exact_fields(
        value,
        {
            "source_id",
            "publisher",
            "authority_level",
            "official_url",
            "allowed_domains",
            "approved_acquisition_modes",
            "license_status",
            "bulk_capture_allowed",
            "full_text_repository_allowed",
            "commercial_training_allowed",
        },
        "source",
    )
    source_id = _text(value["source_id"], "source_id")
    domains = frozenset(_text(item, "allowed_domain").lower() for item in _list(value["allowed_domains"], "allowed_domains"))
    if not domains:
        raise PublicCaseCatalogBlocked("public-case source needs an allowed domain")
    official_url = _official_url(value["official_url"], domains=domains)
    try:
        modes = tuple(
            AcquisitionMode(_text(item, "acquisition_mode"))
            for item in _list(value["approved_acquisition_modes"], "approved_acquisition_modes")
        )
        license_status = LicenseStatus(_text(value["license_status"], "license_status"))
    except ValueError as error:
        raise PublicCaseCatalogBlocked("public-case source policy value is unsupported") from error
    if not modes or len(modes) != len(set(modes)):
        raise PublicCaseCatalogBlocked("public-case source acquisition modes are invalid")
    controls = {
        name: _bool(value[name], name)
        for name in ("bulk_capture_allowed", "full_text_repository_allowed", "commercial_training_allowed")
    }
    if license_status is not LicenseStatus.NOT_ESTABLISHED and license_status is not LicenseStatus.REVIEW_REQUIRED:
        raise PublicCaseCatalogBlocked("public-case source licence status is invalid")
    if any(controls.values()):
        raise PublicCaseCatalogBlocked("unlicensed public-case content capability must remain disabled")
    return PublicCaseSourcePolicy(
        source_id=source_id,
        publisher=_text(value["publisher"], "publisher"),
        authority_level=_text(value["authority_level"], "authority_level"),
        official_url=official_url,
        allowed_domains=domains,
        approved_acquisition_modes=modes,
        license_status=license_status,
        **controls,
    )


def _candidate(value: Any, *, source_by_id: dict[str, PublicCaseSourcePolicy]) -> PublicCaseCandidate:
    if not isinstance(value, dict):
        raise PublicCaseCatalogBlocked("public-case candidate must be an object")
    _require_exact_fields(
        value,
        {
            "candidate_id",
            "source_id",
            "title",
            "official_url",
            "discovered_on",
            "issue_tags",
            "evaluation_uses",
            "acquisition_mode",
            "status",
            "formal_use_allowed",
        },
        "candidate",
    )
    source_id = _text(value["source_id"], "source_id")
    try:
        source = source_by_id[source_id]
    except KeyError as error:
        raise PublicCaseCatalogBlocked("public-case candidate references an unknown source") from error
    try:
        mode = AcquisitionMode(_text(value["acquisition_mode"], "acquisition_mode"))
        status = CandidateStatus(_text(value["status"], "status"))
    except ValueError as error:
        raise PublicCaseCatalogBlocked("public-case candidate policy value is unsupported") from error
    if mode not in source.approved_acquisition_modes:
        raise PublicCaseCatalogBlocked("public-case candidate uses an unapproved acquisition mode")
    formal_use_allowed = _bool(value["formal_use_allowed"], "formal_use_allowed")
    if formal_use_allowed:
        raise PublicCaseCatalogBlocked("unreviewed public-case candidate cannot enter formal use")
    return PublicCaseCandidate(
        candidate_id=_text(value["candidate_id"], "candidate_id"),
        source_id=source_id,
        title=_text(value["title"], "title"),
        official_url=_official_url(value["official_url"], domains=source.allowed_domains),
        discovered_on=_date(value["discovered_on"], "discovered_on"),
        issue_tags=_text_tuple(value["issue_tags"], "issue_tags"),
        evaluation_uses=_text_tuple(value["evaluation_uses"], "evaluation_uses"),
        acquisition_mode=mode,
        status=status,
        formal_use_allowed=formal_use_allowed,
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicCaseCatalogBlocked("public-case catalog contains duplicate fields")
        result[key] = value
    return result


def _require_exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PublicCaseCatalogBlocked(f"public-case {label} fields are invalid")


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise PublicCaseCatalogBlocked(f"public-case {field_name} must be a list")
    return value


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PublicCaseCatalogBlocked(f"public-case {field_name} is invalid")
    return value


def _text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    items = tuple(_text(item, field_name) for item in _list(value, field_name))
    if not items or len(items) != len(set(items)):
        raise PublicCaseCatalogBlocked(f"public-case {field_name} is invalid")
    return items


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise PublicCaseCatalogBlocked(f"public-case {field_name} must be boolean")
    return value


def _date(value: Any, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(_text(value, field_name))
    except ValueError as error:
        raise PublicCaseCatalogBlocked(f"public-case {field_name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise PublicCaseCatalogBlocked(f"public-case {field_name} must be canonical")
    return parsed


def _official_url(value: Any, *, domains: frozenset[str]) -> str:
    url = _text(value, "official_url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in domains or parsed.username or parsed.password:
        raise PublicCaseCatalogBlocked("public-case official URL is outside the source allowlist")
    if parsed.fragment:
        raise PublicCaseCatalogBlocked("public-case official URL cannot contain a fragment")
    return url


def _unique_by_id(items: tuple[Any, ...], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    field_name = f"{label}_id"
    for item in items:
        key = getattr(item, field_name)
        if key in result:
            raise PublicCaseCatalogBlocked(f"duplicate public-case {label} identifier")
        result[key] = item
    return result
