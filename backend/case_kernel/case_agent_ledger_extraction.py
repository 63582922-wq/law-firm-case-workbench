"""Source-bound, review-only fact and transaction extraction artifacts.

This is deliberately *not* a shortcut into ``case_facts`` or
``case_transactions``.  A model/reader may propose bounded, structured
records only after the Worker has supplied the exact evidence-page projection.
The returned artifact is canonical JSON for independent verification and
private staging.  A later server command may promote a staged record, but it
must re-read this artifact, its passed verifier receipt and its source pages;
neither a browser nor a model can submit the record body, a digest or a page
identifier to that command.

The existing CASE_CONTEXT artifact remains a whole-case summary.  It does not
contain enough typed information to create a new ledger object, so extraction
uses a separate schema rather than parsing display prose back into facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from typing import Iterable, Mapping
from uuid import UUID


CASE_LEDGER_EXTRACTION_ARTIFACT_KIND = "CASE_LEDGER_EXTRACTION_CANDIDATE"
CASE_LEDGER_EXTRACTION_SCHEMA = "agent-case-ledger-extraction-candidate-v1"
CASE_LEDGER_EXTRACTION_REVIEW_STATUS = "NEEDS_LAWYER_REVIEW"
AUTO_STAGE_CONFIDENCE_MINIMUM = 0.98

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_MONEY_RE = re.compile(r"^[0-9]{1,15}(?:\.[0-9]{1,6})?$")


class CaseLedgerExtractionBlocked(ValueError):
    """A proposed extraction is not fit for a review-only artifact."""


class ExtractionCandidateKind(StrEnum):
    FACT = "FACT"
    TRANSACTION = "TRANSACTION"


class ExtractionSourceMode(StrEnum):
    NATIVE_TEXT = "NATIVE_TEXT"
    OCR = "OCR"
    VISUAL = "VISUAL"


class ExtractionConflictCode(StrEnum):
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    PARTY_AMBIGUOUS = "PARTY_AMBIGUOUS"
    DATE_AMBIGUOUS = "DATE_AMBIGUOUS"
    AMOUNT_AMBIGUOUS = "AMOUNT_AMBIGUOUS"
    CROSS_PAGE_CONFLICT = "CROSS_PAGE_CONFLICT"
    CONTRADICTS_CASE_LEDGER = "CONTRADICTS_CASE_LEDGER"


class ExtractionRiskCode(StrEnum):
    OCR_DERIVED = "OCR_DERIVED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    LEGAL_CONCLUSION_RISK = "LEGAL_CONCLUSION_RISK"
    INCOMPLETE_TRANSACTION = "INCOMPLETE_TRANSACTION"
    UNTRUSTED_TEXT = "UNTRUSTED_TEXT"


class ExtractionDatePrecision(StrEnum):
    EXACT_DATE = "EXACT_DATE"
    MONTH_ONLY = "MONTH_ONLY"
    YEAR_ONLY = "YEAR_ONLY"
    UNKNOWN = "UNKNOWN"


class ExtractionTransactionDirection(StrEnum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"
    UNKNOWN = "UNKNOWN"


class ExtractionTransactionChannel(StrEnum):
    WECHAT = "WECHAT"
    BANK = "BANK"
    CASH = "CASH"
    CHAT_RECORD = "CHAT_RECORD"
    LOAN_INSTRUMENT = "LOAN_INSTRUMENT"
    OTHER = "OTHER"


@dataclass(frozen=True)
class ExtractionSupportingExcerpt:
    """Literal bounded excerpt which must be found in its source page text."""

    evidence_page_id: str
    text: str

    def validate(self, *, source_page_ids: frozenset[str]) -> None:
        _uuid(self.evidence_page_id, "extraction excerpt evidence_page_id")
        if self.evidence_page_id not in source_page_ids:
            raise CaseLedgerExtractionBlocked("extraction excerpt cites an unknown source page")
        _bounded_text(self.text, "extraction supporting_excerpt", 2_000)


@dataclass(frozen=True)
class CaseLedgerExtractionSourcePage:
    """One Worker-authorized evidence page available to the extractor."""

    input_ref: str
    evidence_page_id: str
    source_file_sha256: str
    page_number: int
    source_text_sha256: str
    source_mode: ExtractionSourceMode

    def validate(self) -> None:
        _code(self.input_ref, "extraction input_ref")
        if self.input_ref != f"evidence-page:{self.evidence_page_id}":
            raise CaseLedgerExtractionBlocked(
                "extraction input_ref must be the exact evidence page reference"
            )
        _uuid(self.evidence_page_id, "extraction evidence_page_id")
        _sha256(self.source_file_sha256, "extraction source_file_sha256")
        _sha256(self.source_text_sha256, "extraction source_text_sha256")
        if type(self.page_number) is not int or self.page_number < 1:
            raise CaseLedgerExtractionBlocked("extraction page_number is invalid")
        if not isinstance(self.source_mode, ExtractionSourceMode):
            raise CaseLedgerExtractionBlocked("extraction source_mode is invalid")


@dataclass(frozen=True)
class CaseLedgerExtractionCandidate:
    """Typed candidate content.  It is still only a review proposal."""

    kind: ExtractionCandidateKind
    source_refs: tuple[str, ...]
    evidence_page_ids: tuple[str, ...]
    confidence: float
    conflict_codes: tuple[ExtractionConflictCode, ...]
    risk_codes: tuple[ExtractionRiskCode, ...]
    supporting_excerpts: tuple[ExtractionSupportingExcerpt, ...]
    fact_text: str | None = None
    local_date: str | None = None
    date_precision: ExtractionDatePrecision | None = None
    amount: str | None = None
    currency: str | None = None
    direction: ExtractionTransactionDirection | None = None
    payer_label: str | None = None
    payee_label: str | None = None
    channel: ExtractionTransactionChannel | None = None
    transaction_reference: str | None = None

    def validate(self, *, source_page_ids: frozenset[str]) -> None:
        if not isinstance(self.kind, ExtractionCandidateKind):
            raise CaseLedgerExtractionBlocked("extraction candidate kind is invalid")
        if not self.source_refs or self.source_refs != tuple(sorted(set(self.source_refs))):
            raise CaseLedgerExtractionBlocked("extraction candidate source refs are invalid")
        if not self.evidence_page_ids or self.evidence_page_ids != tuple(sorted(set(self.evidence_page_ids))):
            raise CaseLedgerExtractionBlocked("extraction candidate evidence pages are invalid")
        for ref in self.source_refs:
            _code(ref, "extraction candidate source_ref")
        for page_id in self.evidence_page_ids:
            _uuid(page_id, "extraction candidate evidence_page_id")
            if page_id not in source_page_ids:
                raise CaseLedgerExtractionBlocked(
                    "extraction candidate cites a page outside the server projection"
                )
        if tuple(f"evidence-page:{value}" for value in self.evidence_page_ids) != self.source_refs:
            raise CaseLedgerExtractionBlocked(
                "extraction candidate source refs differ from evidence pages"
            )
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise CaseLedgerExtractionBlocked("extraction confidence is invalid")
        _enum_tuple(self.conflict_codes, ExtractionConflictCode, "extraction conflict")
        _enum_tuple(self.risk_codes, ExtractionRiskCode, "extraction risk")
        if (
            not self.supporting_excerpts
            or len(self.supporting_excerpts) > 20
            or tuple(item.evidence_page_id for item in self.supporting_excerpts)
            != tuple(sorted(item.evidence_page_id for item in self.supporting_excerpts))
            or len({item.evidence_page_id for item in self.supporting_excerpts})
            != len(self.supporting_excerpts)
        ):
            raise CaseLedgerExtractionBlocked("extraction supporting excerpts are invalid")
        for item in self.supporting_excerpts:
            item.validate(source_page_ids=source_page_ids)
        if not set(self.evidence_page_ids).issubset(
            {item.evidence_page_id for item in self.supporting_excerpts}
        ):
            raise CaseLedgerExtractionBlocked(
                "every extraction evidence page requires a supporting excerpt"
            )
        if self.kind is ExtractionCandidateKind.FACT:
            _bounded_text(self.fact_text, "extraction fact_text", 2_000)
            if any(
                value is not None
                for value in (
                    self.local_date, self.date_precision, self.amount,
                    self.currency, self.direction, self.payer_label,
                    self.payee_label, self.channel, self.transaction_reference,
                )
            ):
                raise CaseLedgerExtractionBlocked(
                    "fact extraction candidate cannot contain transaction fields"
                )
            return
        if self.fact_text is not None:
            raise CaseLedgerExtractionBlocked(
                "transaction extraction candidate cannot contain fact_text"
            )
        _validate_transaction_fields(self)

    @property
    def candidate_hash(self) -> str:
        return _canonical_hash(_candidate_payload(self))

    def eligible_for_automatic_staging(
        self, *, source_modes: Mapping[str, ExtractionSourceMode]
    ) -> bool:
        # "Automatic" here means entry into the private extraction staging
        # queue, never confirmation or a legal conclusion.
        return (
            self.confidence >= AUTO_STAGE_CONFIDENCE_MINIMUM
            and not self.conflict_codes
            and not self.risk_codes
            and all(
                source_modes[page_id] is ExtractionSourceMode.NATIVE_TEXT
                for page_id in self.evidence_page_ids
            )
        )


def build_case_ledger_extraction_candidate(
    *,
    task_input_hash: str,
    source_pages: Iterable[CaseLedgerExtractionSourcePage],
    candidates: Iterable[CaseLedgerExtractionCandidate],
) -> tuple[bytes, str]:
    """Create canonical, source-bound review-only bytes for a Worker.

    ``source_pages`` must originate from an authorized Worker projection.  The
    function intentionally has no browser identity, object-store key or page
    coordinates argument.
    """

    _sha256(task_input_hash, "extraction task_input_hash")
    pages = tuple(source_pages)
    if not 1 <= len(pages) <= 200:
        raise CaseLedgerExtractionBlocked("extraction requires 1 to 200 source pages")
    if tuple(item.input_ref for item in pages) != tuple(sorted(item.input_ref for item in pages)):
        raise CaseLedgerExtractionBlocked("extraction source pages must use sorted input refs")
    if len({item.evidence_page_id for item in pages}) != len(pages):
        raise CaseLedgerExtractionBlocked("extraction source pages are duplicated")
    for page in pages:
        page.validate()
    source_ids = frozenset(item.evidence_page_id for item in pages)
    records = tuple(candidates)
    if len(records) > 500:
        raise CaseLedgerExtractionBlocked("extraction candidate count exceeds limit")
    for item in records:
        item.validate(source_page_ids=source_ids)
    if len({item.candidate_hash for item in records}) != len(records):
        raise CaseLedgerExtractionBlocked("extraction candidates are duplicated")
    source_hash = _source_hash(pages)
    payload = {
        "schema_version": CASE_LEDGER_EXTRACTION_SCHEMA,
        "task_input_hash": task_input_hash,
        "source_hash": source_hash,
        "review_status": CASE_LEDGER_EXTRACTION_REVIEW_STATUS,
        "formal_fact": False,
        "formal_transaction": False,
        "legal_conclusion": False,
        "evidence_decision": False,
        "source_pages": [_source_page_payload(item) for item in pages],
        "candidates": [
            {"candidate_hash": item.candidate_hash, **_candidate_payload(item)}
            for item in records
        ],
    }
    encoded = _json_bytes(payload)
    if len(encoded) > 4 * 1024 * 1024:
        raise CaseLedgerExtractionBlocked("extraction candidate exceeds hard limit")
    return encoded, source_hash


def parse_case_ledger_extraction_candidate(raw: bytes | str) -> dict[str, object]:
    """Strictly parse untrusted stored bytes before server-side staging."""

    try:
        content = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(content, bytes):
            raise TypeError
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise CaseLedgerExtractionBlocked("extraction candidate is not valid JSON") from None
    if not isinstance(value, dict) or _json_bytes(value) != content:
        raise CaseLedgerExtractionBlocked("extraction candidate is not canonical JSON")
    _validate_artifact(value)
    return value


def build_fact_correction_proposal(
    raw: bytes, *, expected_artifact_hash: str, candidate_hash: str,
    revised_text: str, reason: str,
) -> bytes:
    """Build a non-authoritative lawyer proposal from exact stored bytes.

    The caller must independently authorize/re-read the artifact and bind
    actor, matter, version and idempotency in its persistence transaction.
    This pure function is not permission to save or approve a fact.
    """
    _sha256(expected_artifact_hash, "correction artifact hash")
    _sha256(candidate_hash, "correction candidate hash")
    if not isinstance(raw, bytes) or sha256(raw).hexdigest() != expected_artifact_hash:
        raise CaseLedgerExtractionBlocked("correction original artifact differs")
    artifact = parse_case_ledger_extraction_candidate(raw)
    matches = [item for item in artifact["candidates"] if item["candidate_hash"] == candidate_hash]
    if len(matches) != 1 or matches[0]["kind"] != ExtractionCandidateKind.FACT.value:
        raise CaseLedgerExtractionBlocked("correction requires an exact FACT candidate")
    original = matches[0]
    _bounded_text(revised_text, "correction revised text", 4_000)
    _bounded_text(reason, "correction reason", 2_000)
    if revised_text.strip() == original["fact_text"].strip():
        raise CaseLedgerExtractionBlocked("correction must change the proposed text")
    # Do not copy confidence into the revised record or remove original flags.
    # The immutable original candidate is kept for an honest change preview.
    return _json_bytes({
        "schema_version": "lawyer-fact-correction-proposal-v1",
        "review_status": "NEEDS_LAWYER_REVIEW",
        "original_artifact_hash": expected_artifact_hash,
        "source_hash": artifact["source_hash"],
        "original_candidate": original,
        "revised_text": revised_text.strip(),
        "reason": reason.strip(),
        "court_ready": False,
    })


def extraction_source_refs(value: Mapping[str, object]) -> frozenset[str]:
    """Return exact compiled refs after :func:`parse_case_ledger_extraction_candidate`."""

    pages = value.get("_source_pages")
    if not isinstance(pages, tuple):
        raise CaseLedgerExtractionBlocked("extraction artifact has not been server parsed")
    return frozenset(str(page["input_ref"]) for page in pages)


def extraction_candidate_is_eligible(
    artifact: Mapping[str, object], candidate_value: Mapping[str, object]
) -> bool:
    """Return whether one parsed candidate may enter private staging."""

    if not isinstance(candidate_value, dict):
        raise CaseLedgerExtractionBlocked("extraction candidate item is invalid")
    candidate = _candidate_from_payload(
        {key: item for key, item in candidate_value.items() if key != "candidate_hash"}
    )
    source_modes = {
        str(page["evidence_page_id"]): ExtractionSourceMode(page["source_mode"])
        for page in artifact["_source_pages"]  # internal parsed projection only
    }
    return candidate.eligible_for_automatic_staging(source_modes=source_modes)


def _validate_artifact(value: Mapping[str, object]) -> None:
    fields = {
        "schema_version", "task_input_hash", "source_hash", "review_status",
        "formal_fact", "formal_transaction", "legal_conclusion",
        "evidence_decision", "source_pages", "candidates",
    }
    if (
        set(value) != fields
        or value.get("schema_version") != CASE_LEDGER_EXTRACTION_SCHEMA
        or value.get("review_status") != CASE_LEDGER_EXTRACTION_REVIEW_STATUS
        or any(value.get(key) is not False for key in (
            "formal_fact", "formal_transaction", "legal_conclusion", "evidence_decision"
        ))
    ):
        raise CaseLedgerExtractionBlocked("extraction candidate review contract is invalid")
    _sha256(value.get("task_input_hash"), "extraction task_input_hash")
    _sha256(value.get("source_hash"), "extraction source_hash")
    raw_pages = value.get("source_pages")
    if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= 200:
        raise CaseLedgerExtractionBlocked("extraction source_pages are invalid")
    pages = tuple(_source_page_from_payload(item) for item in raw_pages)
    if tuple(item.input_ref for item in pages) != tuple(sorted(item.input_ref for item in pages)):
        raise CaseLedgerExtractionBlocked("extraction source pages must use sorted input refs")
    if len({item.evidence_page_id for item in pages}) != len(pages):
        raise CaseLedgerExtractionBlocked("extraction source pages are duplicated")
    if value["source_hash"] != _source_hash(pages):
        raise CaseLedgerExtractionBlocked("extraction source hash differs")
    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 500:
        raise CaseLedgerExtractionBlocked("extraction candidates are invalid")
    source_ids = frozenset(item.evidence_page_id for item in pages)
    candidate_hashes: set[str] = set()
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise CaseLedgerExtractionBlocked("extraction candidate item is invalid")
        supplied_hash = raw_candidate.get("candidate_hash")
        _sha256(supplied_hash, "extraction candidate_hash")
        candidate_payload = {key: item for key, item in raw_candidate.items() if key != "candidate_hash"}
        candidate = _candidate_from_payload(candidate_payload)
        candidate.validate(source_page_ids=source_ids)
        if candidate.candidate_hash != supplied_hash or supplied_hash in candidate_hashes:
            raise CaseLedgerExtractionBlocked("extraction candidate hash differs")
        candidate_hashes.add(supplied_hash)
    # The parser returns a fresh mapping with an internal projection.  This
    # avoids every later store re-parsing source pages from caller input.
    value["_source_pages"] = tuple(_source_page_payload(item) for item in pages)  # type: ignore[index]


def _source_hash(pages: tuple[CaseLedgerExtractionSourcePage, ...]) -> str:
    return _canonical_hash({
        "schema_version": "agent-case-ledger-extraction-source-set-v1",
        "pages": [_source_page_payload(item) for item in pages],
    })


def _source_page_payload(value: CaseLedgerExtractionSourcePage) -> dict[str, object]:
    return {
        "input_ref": value.input_ref,
        "evidence_page_id": value.evidence_page_id,
        "source_file_sha256": value.source_file_sha256,
        "page_number": value.page_number,
        "source_text_sha256": value.source_text_sha256,
        "source_mode": value.source_mode.value,
    }


def _source_page_from_payload(value: object) -> CaseLedgerExtractionSourcePage:
    if not isinstance(value, dict) or set(value) != {
        "input_ref", "evidence_page_id", "source_file_sha256", "page_number",
        "source_text_sha256", "source_mode",
    }:
        raise CaseLedgerExtractionBlocked("extraction source page schema is invalid")
    try:
        result = CaseLedgerExtractionSourcePage(
            input_ref=value["input_ref"], evidence_page_id=value["evidence_page_id"],
            source_file_sha256=value["source_file_sha256"], page_number=value["page_number"],
            source_text_sha256=value["source_text_sha256"],
            source_mode=ExtractionSourceMode(value["source_mode"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CaseLedgerExtractionBlocked("extraction source page is invalid") from error
    result.validate()
    return result


def _candidate_payload(value: CaseLedgerExtractionCandidate) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": value.kind.value,
        "source_refs": list(value.source_refs),
        "evidence_page_ids": list(value.evidence_page_ids),
        "confidence": round(float(value.confidence), 5),
        "conflict_codes": [item.value for item in value.conflict_codes],
        "risk_codes": [item.value for item in value.risk_codes],
        "supporting_excerpts": [
            {"evidence_page_id": item.evidence_page_id, "text": item.text}
            for item in value.supporting_excerpts
        ],
    }
    if value.kind is ExtractionCandidateKind.FACT:
        payload["fact_text"] = value.fact_text
        return payload
    payload.update({
        "local_date": value.local_date,
        "date_precision": value.date_precision.value if value.date_precision else None,
        "amount": value.amount,
        "currency": value.currency,
        "direction": value.direction.value if value.direction else None,
        "payer_label": value.payer_label,
        "payee_label": value.payee_label,
        "channel": value.channel.value if value.channel else None,
        "transaction_reference": value.transaction_reference,
    })
    return payload


def _candidate_from_payload(value: Mapping[str, object]) -> CaseLedgerExtractionCandidate:
    common = {
        "kind", "source_refs", "evidence_page_ids", "confidence", "conflict_codes", "risk_codes",
        "supporting_excerpts",
    }
    kind = value.get("kind")
    if kind == ExtractionCandidateKind.FACT.value:
        if set(value) != common | {"fact_text"}:
            raise CaseLedgerExtractionBlocked("fact extraction candidate schema is invalid")
    elif kind == ExtractionCandidateKind.TRANSACTION.value:
        if set(value) != common | {
            "local_date", "date_precision", "amount", "currency", "direction",
            "payer_label", "payee_label", "channel", "transaction_reference",
        }:
            raise CaseLedgerExtractionBlocked("transaction extraction candidate schema is invalid")
    else:
        raise CaseLedgerExtractionBlocked("extraction candidate kind is invalid")
    try:
        return CaseLedgerExtractionCandidate(
            kind=ExtractionCandidateKind(kind),
            source_refs=_enum_strings(value["source_refs"], "extraction source_refs"),
            evidence_page_ids=_enum_strings(value["evidence_page_ids"], "extraction evidence_page_ids"),
            confidence=value["confidence"],
            conflict_codes=_enum_values(value["conflict_codes"], ExtractionConflictCode, "extraction conflict"),
            risk_codes=_enum_values(value["risk_codes"], ExtractionRiskCode, "extraction risk"),
            supporting_excerpts=_supporting_excerpts(value["supporting_excerpts"]),
            fact_text=value.get("fact_text"), local_date=value.get("local_date"),
            date_precision=(ExtractionDatePrecision(value["date_precision"])
                            if value.get("date_precision") is not None else None),
            amount=value.get("amount"), currency=value.get("currency"),
            direction=(ExtractionTransactionDirection(value["direction"])
                       if value.get("direction") is not None else None),
            payer_label=value.get("payer_label"), payee_label=value.get("payee_label"),
            channel=(ExtractionTransactionChannel(value["channel"])
                     if value.get("channel") is not None else None),
            transaction_reference=value.get("transaction_reference"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CaseLedgerExtractionBlocked("extraction candidate fields are invalid") from error


def _validate_transaction_fields(value: CaseLedgerExtractionCandidate) -> None:
    if not isinstance(value.date_precision, ExtractionDatePrecision):
        raise CaseLedgerExtractionBlocked("transaction date_precision is invalid")
    if value.date_precision is ExtractionDatePrecision.EXACT_DATE:
        if not isinstance(value.local_date, str):
            raise CaseLedgerExtractionBlocked("exact transaction date is required")
        try:
            if date.fromisoformat(value.local_date).isoformat() != value.local_date:
                raise ValueError
        except ValueError as error:
            raise CaseLedgerExtractionBlocked("transaction local_date is invalid") from error
    elif value.local_date is not None:
        raise CaseLedgerExtractionBlocked("non-exact transaction cannot include local_date")
    if not isinstance(value.amount, str) or _MONEY_RE.fullmatch(value.amount) is None:
        raise CaseLedgerExtractionBlocked("transaction amount is invalid")
    try:
        amount = Decimal(value.amount)
    except InvalidOperation as error:
        raise CaseLedgerExtractionBlocked("transaction amount is invalid") from error
    if not amount.is_finite() or amount <= 0:
        raise CaseLedgerExtractionBlocked("transaction amount is invalid")
    if not isinstance(value.currency, str) or re.fullmatch(r"^[A-Z]{3}$", value.currency) is None:
        raise CaseLedgerExtractionBlocked("transaction currency is invalid")
    if not isinstance(value.direction, ExtractionTransactionDirection):
        raise CaseLedgerExtractionBlocked("transaction direction is invalid")
    if not isinstance(value.channel, ExtractionTransactionChannel):
        raise CaseLedgerExtractionBlocked("transaction channel is invalid")
    for item, label in (
        (value.payer_label, "transaction payer_label"),
        (value.payee_label, "transaction payee_label"),
        (value.transaction_reference, "transaction reference"),
    ):
        if item is not None:
            _bounded_text(item, label, 500)


def _enum_tuple(value: tuple[object, ...], enum_type: type[StrEnum], label: str) -> None:
    if value != tuple(sorted(set(value), key=lambda item: item.value)):
        raise CaseLedgerExtractionBlocked(f"{label} codes must be sorted and unique")
    if len(value) > 20 or any(not isinstance(item, enum_type) for item in value):
        raise CaseLedgerExtractionBlocked(f"{label} codes are invalid")


def _enum_values(value: object, enum_type: type[StrEnum], label: str) -> tuple[StrEnum, ...]:
    if not isinstance(value, list):
        raise CaseLedgerExtractionBlocked(f"{label} codes are invalid")
    try:
        result = tuple(enum_type(item) for item in value)
    except (TypeError, ValueError) as error:
        raise CaseLedgerExtractionBlocked(f"{label} codes are invalid") from error
    _enum_tuple(result, enum_type, label)
    return result


def _enum_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CaseLedgerExtractionBlocked(f"{label} are invalid")
    return tuple(value)


def _supporting_excerpts(value: object) -> tuple[ExtractionSupportingExcerpt, ...]:
    if not isinstance(value, list):
        raise CaseLedgerExtractionBlocked("extraction supporting excerpts are invalid")
    result: list[ExtractionSupportingExcerpt] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"evidence_page_id", "text"}:
            raise CaseLedgerExtractionBlocked("extraction supporting excerpt schema is invalid")
        try:
            result.append(ExtractionSupportingExcerpt(
                evidence_page_id=item["evidence_page_id"], text=item["text"]
            ))
        except (KeyError, TypeError) as error:
            raise CaseLedgerExtractionBlocked("extraction supporting excerpt is invalid") from error
    return tuple(result)


def _bounded_text(value: object, label: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(item) < 32 and item not in "\n\r\t" for item in value)
    ):
        raise CaseLedgerExtractionBlocked(f"{label} is invalid")


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CaseLedgerExtractionBlocked(f"{label} must be a SHA-256 digest")


def _code(value: object, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise CaseLedgerExtractionBlocked(f"{label} is invalid")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseLedgerExtractionBlocked(f"{label} must be a UUID") from error


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _reject_duplicate_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


__all__ = (
    "AUTO_STAGE_CONFIDENCE_MINIMUM",
    "CASE_LEDGER_EXTRACTION_ARTIFACT_KIND",
    "CASE_LEDGER_EXTRACTION_REVIEW_STATUS",
    "CASE_LEDGER_EXTRACTION_SCHEMA",
    "CaseLedgerExtractionBlocked",
    "CaseLedgerExtractionCandidate",
    "CaseLedgerExtractionSourcePage",
    "ExtractionCandidateKind",
    "ExtractionConflictCode",
    "ExtractionDatePrecision",
    "ExtractionRiskCode",
    "ExtractionSourceMode",
    "ExtractionSupportingExcerpt",
    "ExtractionTransactionChannel",
    "ExtractionTransactionDirection",
    "build_case_ledger_extraction_candidate",
    "extraction_candidate_is_eligible",
    "extraction_source_refs",
    "parse_case_ledger_extraction_candidate",
)
