"""Deterministic parser for encrypted official CFETS LPR snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
import json
import re

from .managed_artifact_store import LocalEncryptedArtifactStore
from .official_source_capture import CapturedOfficialSource


class LprSourceParseBlocked(ValueError):
    """The captured bytes cannot support a formal LPR observation candidate."""


@dataclass(frozen=True)
class LprObservation:
    publication_date: date
    effective_from: date
    effective_until: date | None
    one_year_rate: Decimal
    five_year_plus_rate: Decimal
    source_locator: str


@dataclass(frozen=True)
class ParsedLprSnapshot:
    source_id: str
    source_url: str
    content_sha256: str
    observations: tuple[LprObservation, ...]
    parsed_output_hash: str
    review_status: str = "HUMAN_REVIEW_REQUIRED"


_SOURCE_ID = "CFETS-LPR-HISTORY"
_EARLIEST_REFORMED_LPR = date(2019, 8, 20)
_ANNOUNCEMENT = re.compile(
    r"(?P<year>20\d{2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
    r".{0,500}?1\s*年期\s*LPR\s*为\s*(?P<one>\d+(?:\.\d+)?)\s*%"
    r".{0,300}?5\s*年期以上\s*LPR\s*为\s*(?P<five>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE | re.DOTALL,
)


def parse_captured_lpr_snapshot(
    *,
    capture: CapturedOfficialSource,
    artifact_store: LocalEncryptedArtifactStore,
) -> ParsedLprSnapshot:
    if capture.source_id != _SOURCE_ID:
        raise LprSourceParseBlocked("LPR parser requires the registered CFETS source")
    if capture.review_status != "HUMAN_REVIEW_REQUIRED":
        raise LprSourceParseBlocked("LPR capture has an unexpected review state")
    body = artifact_store.read_bytes(
        capture.encrypted_object.object_key,
        expected_sha256=capture.content_sha256,
    )
    if sha256(body).hexdigest() != capture.content_sha256 or len(body) != capture.content_bytes:
        raise LprSourceParseBlocked("LPR source bytes differ from the capture receipt")
    return parse_lpr_source_bytes(
        source_id=capture.source_id,
        source_url=capture.final_url,
        content_sha256=capture.content_sha256,
        media_type=capture.media_type,
        retrieved_on=capture.retrieved_at.date(),
        body=body,
    )


def parse_lpr_source_bytes(
    *,
    source_id: str,
    source_url: str,
    content_sha256: str,
    media_type: str,
    retrieved_on: date,
    body: bytes,
) -> ParsedLprSnapshot:
    """Parse authenticated official LPR bytes for capture registration.

    This is deliberately source-specific and receives the byte hash from the
    encrypted-object verifier.  It does not accept a rate value from a browser
    or from a previously serialized summary.
    """

    if source_id != _SOURCE_ID:
        raise LprSourceParseBlocked("LPR parser requires the registered CFETS source")
    if not body or sha256(body).hexdigest() != content_sha256:
        raise LprSourceParseBlocked("official LPR bytes do not match the authenticated content hash")
    if media_type == "application/json":
        raw = _parse_json_records(body, retrieved_on=retrieved_on)
    elif media_type in {"text/html", "application/xhtml+xml"}:
        raw = _parse_announcement_html(body, retrieved_on=retrieved_on)
    else:
        raise LprSourceParseBlocked("captured media type is not an LPR data format")
    observations = _build_intervals(raw)
    payload = {
        "schema_version": "parsed-official-lpr-v1",
        "source_id": source_id,
        "source_url": source_url,
        "content_sha256": content_sha256,
        "observations": [
            {
                "publication_date": item.publication_date.isoformat(),
                "effective_from": item.effective_from.isoformat(),
                "effective_until": item.effective_until.isoformat() if item.effective_until else None,
                "one_year_rate": format(item.one_year_rate, "f"),
                "five_year_plus_rate": format(item.five_year_plus_rate, "f"),
                "source_locator": item.source_locator,
            }
            for item in observations
        ],
        "review_status": "HUMAN_REVIEW_REQUIRED",
    }
    output_hash = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ParsedLprSnapshot(
        source_id=source_id,
        source_url=source_url,
        content_sha256=content_sha256,
        observations=observations,
        parsed_output_hash=output_hash,
    )


def _parse_json_records(body: bytes, *, retrieved_on: date) -> tuple[tuple[date, Decimal, Decimal, str], ...]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LprSourceParseBlocked("official LPR JSON is invalid") from error
    if not isinstance(payload, dict) or (payload.get("head") or {}).get("rep_code") != "200":
        raise LprSourceParseBlocked("official LPR JSON response code is not successful")
    records = payload.get("records")
    if not isinstance(records, list) or not records or len(records) > 24:
        raise LprSourceParseBlocked("official LPR JSON must contain 1 to 24 records")
    parsed: list[tuple[date, Decimal, Decimal, str]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise LprSourceParseBlocked("official LPR record is not an object")
        try:
            publication_date = date.fromisoformat(str(record["showDateCN"]))
            one_year_raw = record["1Y"]
            five_year_raw = record["5Y"]
        except (KeyError, TypeError, ValueError) as error:
            raise LprSourceParseBlocked("official LPR record fields are invalid") from error
        one_year = _percentage_to_rate(one_year_raw)
        five_year = _percentage_to_rate(five_year_raw)
        _validate_publication_date(publication_date, retrieved_on=retrieved_on)
        parsed.append((publication_date, one_year, five_year, f"records[{index}]"))
    return tuple(parsed)


def _parse_announcement_html(body: bytes, *, retrieved_on: date) -> tuple[tuple[date, Decimal, Decimal, str], ...]:
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LprSourceParseBlocked("official LPR announcement is not UTF-8") from error
    extractor = _VisibleTextExtractor()
    extractor.feed(decoded)
    visible_text = re.sub(r"\s+", " ", " ".join(extractor.parts))
    matches = tuple(_ANNOUNCEMENT.finditer(visible_text))
    if len(matches) != 1:
        raise LprSourceParseBlocked("official LPR announcement must contain exactly one rate statement")
    match = matches[0]
    publication_date = date(
        int(match.group("year")), int(match.group("month")), int(match.group("day"))
    )
    _validate_publication_date(publication_date, retrieved_on=retrieved_on)
    return (
        (
            publication_date,
            _percentage_to_rate(match.group("one")),
            _percentage_to_rate(match.group("five")),
            "official announcement rate statement",
        ),
    )


def _build_intervals(
    raw: tuple[tuple[date, Decimal, Decimal, str], ...],
) -> tuple[LprObservation, ...]:
    ordered = sorted(raw, key=lambda item: item[0])
    dates = [item[0] for item in ordered]
    if len(dates) != len(set(dates)):
        raise LprSourceParseBlocked("official LPR snapshot contains duplicate publication dates")
    return tuple(
        LprObservation(
            publication_date=item[0],
            effective_from=item[0],
            effective_until=ordered[index + 1][0] if index + 1 < len(ordered) else None,
            one_year_rate=item[1],
            five_year_plus_rate=item[2],
            source_locator=item[3],
        )
        for index, item in enumerate(ordered)
    )


def _percentage_to_rate(value) -> Decimal:
    try:
        percentage = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LprSourceParseBlocked("official LPR percentage is invalid") from error
    if percentage <= Decimal("0") or percentage >= Decimal("20"):
        raise LprSourceParseBlocked("official LPR percentage is outside the valid boundary")
    return (percentage / Decimal("100")).quantize(Decimal("0.000001"))


def _validate_publication_date(value: date, *, retrieved_on: date) -> None:
    if value < _EARLIEST_REFORMED_LPR or value > retrieved_on:
        raise LprSourceParseBlocked("official LPR publication date is outside the capture period")


class _VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data.strip():
            self.parts.append(data.strip())
