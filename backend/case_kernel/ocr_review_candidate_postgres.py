"""Append-only encrypted OCR candidates awaiting an explicit lawyer review."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt, CaseLedgerPersistenceBlocked, _advisory_lock,
    _authorize_and_lock_matter, _authorize_matter_read, _finish_command,
    _payload_hash, _prior_receipt, _require_positive_version, _require_roles,
    _validate_command_identity, _validate_read_identity, _validate_sha256,
)
from .models import Actor, Role


@dataclass(frozen=True)
class OcrReviewCandidateSpec:
    external_request_id: str
    evidence_page_id: str
    provider_id: str
    source_page_sha256: str
    content_object_key: str
    content_sha256: str
    content_bytes: int
    provider_request_ref_hash: str


@dataclass(frozen=True)
class PersistentOcrReviewCandidateSnapshot:
    matter_id: str
    matter_version: int
    candidates: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresOcrReviewCandidateStore:
    _STAGE = frozenset({Role.SYSTEM_WORKER})
    _REVIEW = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _READ = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER})

    def __init__(self, dsn: str, *, artifact_reader: Callable[[str, str], bytes] | None) -> None:
        self._dsn, self._reader = dsn, artifact_reader

    def stage(self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, spec: OcrReviewCandidateSpec) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key); _require_roles(actor, self._STAGE); _require_positive_version(expected_version)
        self._validate_spec(spec)
        review_hash = _payload_hash({"matter_id": matter_id, "spec": spec.__dict__})
        payload = {"matter_id": matter_id, "expected_version": expected_version, "spec": spec.__dict__, "review_hash": review_hash}
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name="STAGE_OCR_REVIEW_CANDIDATE", idempotency_key=idempotency_key)
            prior = _prior_receipt(connection, actor=actor, matter_id=matter_id, command_name="STAGE_OCR_REVIEW_CANDIDATE", idempotency_key=idempotency_key, payload_hash=_payload_hash(payload))
            if prior is not None: return prior
            _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, allowed_roles=self._STAGE)
            candidate_id = str(uuid4())
            connection.execute("INSERT INTO ocr_review_candidates (candidate_id, firm_id, matter_id, external_request_id, evidence_page_id, provider_id, source_page_sha256, content_object_key, content_sha256, content_bytes, provider_request_ref_hash, review_hash, status, staged_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CANDIDATE',%s)", (candidate_id, actor.firm_id, matter_id, spec.external_request_id, spec.evidence_page_id, spec.provider_id, spec.source_page_sha256, spec.content_object_key, spec.content_sha256, spec.content_bytes, spec.provider_request_ref_hash, review_hash, actor.actor_id))
            return _finish_command(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, command_name="STAGE_OCR_REVIEW_CANDIDATE", idempotency_key=idempotency_key, payload_hash=_payload_hash(payload), event_type="OCR_REVIEW_CANDIDATE_STAGED", object_type="OCR_REVIEW_CANDIDATE", object_id=candidate_id, audit_payload={"candidate_id": candidate_id, "external_request_id": spec.external_request_id, "evidence_page_id": spec.evidence_page_id, "content_sha256": spec.content_sha256, "review_hash": review_hash}, stale_submission=False, stale_calculations=False)

    def _validate_spec(self, spec: OcrReviewCandidateSpec) -> None:
        for label, value in (("external_request_id", spec.external_request_id), ("evidence_page_id", spec.evidence_page_id)):
            try: UUID(value)
            except (TypeError, ValueError) as error: raise CaseLedgerPersistenceBlocked(f"OCR candidate {label} must be UUID") from error
        if spec.provider_id != "qwen" or not 0 < spec.content_bytes <= 524288: raise CaseLedgerPersistenceBlocked("OCR candidate metadata is invalid")
        for label, value in (("source_page_sha256", spec.source_page_sha256), ("content_sha256", spec.content_sha256), ("provider_request_ref_hash", spec.provider_request_ref_hash)): _validate_sha256(label, value)
        expected_key = f"{spec.content_sha256[:2]}/{spec.content_sha256[2:4]}/{spec.content_sha256}.lca"
        if spec.content_object_key != expected_key or self._reader is None: raise CaseLedgerPersistenceBlocked("OCR candidate encrypted object is unavailable")
        raw = self._reader(spec.content_object_key, spec.content_sha256)
        if len(raw) != spec.content_bytes or sha256(raw).hexdigest() != spec.content_sha256:
            raise CaseLedgerPersistenceBlocked("OCR candidate encrypted object failed authentication")
        try: raw.decode("utf-8")
        except UnicodeDecodeError as error: raise CaseLedgerPersistenceBlocked("OCR candidate is not UTF-8 text") from error

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentOcrReviewCandidateSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor); _require_roles(actor, self._READ)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ)
            matter = connection.execute("SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s", (matter_id, actor.firm_id)).fetchone()
            if matter is None: raise KeyError(matter_id)
            rows = connection.execute("SELECT candidate_id, external_request_id, evidence_page_id, provider_id, source_page_sha256, content_sha256, content_bytes, provider_request_ref_hash, review_hash, status, review_reason, created_at, reviewed_at FROM ocr_review_candidates WHERE matter_id = %s AND firm_id = %s ORDER BY created_at DESC, candidate_id DESC", (matter_id, actor.firm_id)).fetchall()
        candidates = tuple({key: value.isoformat() if hasattr(value, "isoformat") else str(value) if key.endswith("_id") else value for key, value in row.items()} for row in rows)
        return PersistentOcrReviewCandidateSnapshot(matter_id, matter["version"], candidates, _payload_hash({"matter_id": matter_id, "matter_version": matter["version"], "candidates": candidates}))

    def read_text(self, *, matter_id: str, candidate_id: str, actor: Actor) -> str:
        _validate_read_identity(matter_id=matter_id, actor=actor); _require_roles(actor, self._READ)
        try: UUID(candidate_id)
        except (TypeError, ValueError) as error: raise KeyError(candidate_id) from error
        if self._reader is None: raise CaseLedgerPersistenceBlocked("OCR candidate encrypted object is unavailable")
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ)
            row = connection.execute("SELECT content_object_key, content_sha256, content_bytes FROM ocr_review_candidates WHERE candidate_id = %s AND matter_id = %s AND firm_id = %s", (candidate_id, matter_id, actor.firm_id)).fetchone()
        if row is None: raise KeyError(candidate_id)
        raw = self._reader(row["content_object_key"], row["content_sha256"])
        if len(raw) != row["content_bytes"] or sha256(raw).hexdigest() != row["content_sha256"]: raise CaseLedgerPersistenceBlocked("OCR candidate encrypted object failed authentication")
        try: return raw.decode("utf-8")
        except UnicodeDecodeError as error: raise CaseLedgerPersistenceBlocked("OCR candidate is not UTF-8 text") from error

    def review(self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, candidate_id: str, decision: str, review_hash: str, reason: str) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key); _require_roles(actor, self._REVIEW); _require_positive_version(expected_version); _validate_sha256("review_hash", review_hash)
        if decision not in {"ACCEPTED", "REJECTED"} or not reason.strip() or len(reason) > 480: raise CaseLedgerPersistenceBlocked("OCR candidate review decision is invalid")
        payload = {"matter_id": matter_id, "expected_version": expected_version, "candidate_id": candidate_id, "decision": decision, "review_hash": review_hash, "reason": reason.strip()}
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name="REVIEW_OCR_CANDIDATE", idempotency_key=idempotency_key)
            prior = _prior_receipt(connection, actor=actor, matter_id=matter_id, command_name="REVIEW_OCR_CANDIDATE", idempotency_key=idempotency_key, payload_hash=_payload_hash(payload))
            if prior is not None: return prior
            _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, allowed_roles=self._REVIEW)
            row = connection.execute("SELECT review_hash, status FROM ocr_review_candidates WHERE candidate_id = %s AND matter_id = %s AND firm_id = %s FOR UPDATE", (candidate_id, matter_id, actor.firm_id)).fetchone()
            if row is None: raise KeyError(candidate_id)
            if row["status"] != "CANDIDATE" or row["review_hash"] != review_hash: raise CaseLedgerPersistenceBlocked("OCR candidate review must bind to the current exact review hash")
            connection.execute("UPDATE ocr_review_candidates SET status = %s, reviewed_by = %s, reviewed_at = now(), review_reason = %s WHERE candidate_id = %s", (decision, actor.actor_id, reason.strip(), candidate_id))
            return _finish_command(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, command_name="REVIEW_OCR_CANDIDATE", idempotency_key=idempotency_key, payload_hash=_payload_hash(payload), event_type="OCR_REVIEW_CANDIDATE_REVIEWED", object_type="OCR_REVIEW_CANDIDATE", object_id=candidate_id, audit_payload={"candidate_id": candidate_id, "decision": decision, "review_hash": review_hash}, stale_submission=False, stale_calculations=False)

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,)); yield connection
