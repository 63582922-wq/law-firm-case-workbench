"""Persistent, approval-bound review pairs for generated Office documents."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from typing import Callable, Iterator
from uuid import uuid4
from io import BytesIO
from zipfile import BadZipFile, ZipFile

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_sha256,
    _validate_uuid,
)
from .models import Actor, Role


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_EDITABLE_BYTES = 64 * 1024 * 1024
_MAX_PDF_BYTES = 128 * 1024 * 1024


class PostgresReviewableDraftStore:
    """Registers an encrypted editable Office file and review PDF atomically."""

    _REGISTER_ROLES = frozenset({Role.SYSTEM_WORKER})
    _APPROVE_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})

    def __init__(self, dsn: str, *, artifact_reader: Callable[[str, str], bytes] | None = None) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._artifact_reader = artifact_reader

    def register_reviewable_office_draft_pair(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        document_kind: str,
        editable_media_type: str,
        editable_object_key: str,
        editable_sha256: str,
        editable_bytes: int,
        review_pdf_object_key: str,
        review_pdf_sha256: str,
        review_pdf_bytes: int,
        review_pdf_page_count: int,
        approval_input_hash: str,
        render_verification_hash: str,
        review_input_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._REGISTER_ROLES)
        _require_text(document_kind, "document_kind")
        normalized_kind = document_kind.strip()
        _validate_editable_media_type(editable_media_type)
        _validate_sha256("editable_sha256", editable_sha256)
        _validate_sha256("review_pdf_sha256", review_pdf_sha256)
        _validate_sha256("approval_input_hash", approval_input_hash)
        _validate_sha256("render_verification_hash", render_verification_hash)
        _validate_sha256("review_input_hash", review_input_hash)
        _validate_content_addressed_key(editable_object_key, editable_sha256)
        _validate_content_addressed_key(review_pdf_object_key, review_pdf_sha256)
        if not 0 < editable_bytes <= _MAX_EDITABLE_BYTES:
            raise CaseLedgerPersistenceBlocked("editable Office draft exceeds configured byte limit")
        if not 0 < review_pdf_bytes <= _MAX_PDF_BYTES or review_pdf_page_count < 1:
            raise CaseLedgerPersistenceBlocked("review PDF metadata is invalid")
        editable = self._read_authenticated_artifact(editable_object_key, editable_sha256)
        review_pdf = self._read_authenticated_artifact(review_pdf_object_key, review_pdf_sha256)
        if len(editable) != editable_bytes or len(review_pdf) != review_pdf_bytes:
            raise CaseLedgerPersistenceBlocked("reviewable Office pair byte size differs from encrypted object")
        _validate_office_container(editable, editable_media_type)
        _validate_pdf(review_pdf)
        derived_review_hash = _review_input_hash(
            editable_media_type=editable_media_type,
            editable_sha256=editable_sha256,
            editable_bytes=editable_bytes,
            review_pdf_sha256=review_pdf_sha256,
            review_pdf_bytes=review_pdf_bytes,
            review_pdf_page_count=review_pdf_page_count,
            approval_input_hash=approval_input_hash,
            render_verification_hash=render_verification_hash,
        )
        if derived_review_hash != review_input_hash:
            raise CaseLedgerPersistenceBlocked("reviewable Office pair hash differs from the exact stored outputs")

        command_name = "REGISTER_REVIEWABLE_OFFICE_DRAFT_PAIR"
        pair_id = str(uuid4())
        payload = {
            "matter_id": matter_id, "expected_version": expected_version,
            "document_kind": normalized_kind, "editable_media_type": editable_media_type,
            "editable_object_key": editable_object_key, "editable_sha256": editable_sha256,
            "editable_bytes": editable_bytes, "review_pdf_object_key": review_pdf_object_key,
            "review_pdf_sha256": review_pdf_sha256, "review_pdf_bytes": review_pdf_bytes,
            "review_pdf_page_count": review_pdf_page_count, "approval_input_hash": approval_input_hash,
            "render_verification_hash": render_verification_hash, "review_input_hash": review_input_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(connection, matter_id, actor, expected_version, idempotency_key, command_name, payload_hash, self._REGISTER_ROLES)
            if prior is not None:
                return prior
            connection.execute(
                """
                INSERT INTO reviewable_office_draft_pairs (
                    pair_id, firm_id, matter_id, document_kind, editable_media_type,
                    editable_object_key, editable_sha256, editable_bytes,
                    review_pdf_object_key, review_pdf_sha256, review_pdf_bytes,
                    review_pdf_page_count, approval_input_hash, render_verification_hash,
                    review_input_hash, status, registered_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'CANDIDATE', %s)
                """,
                (pair_id, actor.firm_id, matter_id, normalized_kind, editable_media_type,
                 editable_object_key, editable_sha256, editable_bytes, review_pdf_object_key,
                 review_pdf_sha256, review_pdf_bytes, review_pdf_page_count, approval_input_hash,
                 render_verification_hash, review_input_hash, actor.actor_id),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash,
                event_type="REVIEWABLE_OFFICE_DRAFT_PAIR_REGISTERED",
                object_type="REVIEWABLE_OFFICE_DRAFT_PAIR", object_id=pair_id,
                audit_payload={"pair_id": pair_id, "document_kind": normalized_kind,
                               "editable_sha256": editable_sha256, "review_pdf_sha256": review_pdf_sha256,
                               "review_input_hash": review_input_hash,
                               "render_verification_hash": render_verification_hash},
                stale_submission=False, stale_calculations=False,
            )

    def approve_reviewable_office_draft_pair(
        self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str,
        pair_id: str, approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(matter_id, actor, expected_version, idempotency_key, self._APPROVE_ROLES)
        _validate_uuid("pair_id", pair_id)
        _validate_sha256("approval_hash", approval_hash)
        command_name = "APPROVE_REVIEWABLE_OFFICE_DRAFT_PAIR"
        payload_hash = _payload_hash({"matter_id": matter_id, "expected_version": expected_version,
                                      "pair_id": pair_id, "approval_hash": approval_hash})
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(connection, matter_id, actor, expected_version, idempotency_key, command_name, payload_hash, self._APPROVE_ROLES)
            if prior is not None:
                return prior
            pair = connection.execute(
                """
                SELECT document_kind, editable_sha256, review_pdf_sha256, review_input_hash, status
                FROM reviewable_office_draft_pairs
                WHERE pair_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """, (pair_id, matter_id, actor.firm_id),
            ).fetchone()
            if pair is None:
                raise KeyError(pair_id)
            if pair["status"] != "CANDIDATE":
                raise CaseLedgerPersistenceBlocked("only a candidate Office draft pair can be approved")
            if pair["review_input_hash"] != approval_hash:
                raise CaseLedgerPersistenceBlocked("Office draft approval must bind to the exact review pair hash")
            connection.execute(
                """
                UPDATE reviewable_office_draft_pairs
                SET status = 'APPROVED', approved_by = %s, approval_hash = %s, approved_at = now()
                WHERE pair_id = %s AND matter_id = %s AND firm_id = %s
                """, (actor.actor_id, approval_hash, pair_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash,
                event_type="REVIEWABLE_OFFICE_DRAFT_PAIR_APPROVED",
                object_type="REVIEWABLE_OFFICE_DRAFT_PAIR", object_id=pair_id,
                audit_payload={"pair_id": pair_id, "document_kind": pair["document_kind"],
                               "editable_sha256": pair["editable_sha256"],
                               "review_pdf_sha256": pair["review_pdf_sha256"],
                               "approval_hash": approval_hash},
                # This pair is internal-only.  It cannot reach court export until a separately
                # generated PDF work product passes its own approval and QA chain.
                stale_submission=False, stale_calculations=False,
            )

    def _read_authenticated_artifact(self, object_key: str, expected_hash: str) -> bytes:
        if self._artifact_reader is None:
            raise CaseLedgerPersistenceBlocked("reviewable Office draft registration requires an encrypted-object verifier")
        try:
            content = self._artifact_reader(object_key, expected_hash)
        except Exception as error:
            raise CaseLedgerPersistenceBlocked("reviewable Office encrypted-object authentication failed") from error
        if not isinstance(content, bytes) or sha256(content).hexdigest() != expected_hash:
            raise CaseLedgerPersistenceBlocked("reviewable Office encrypted-object plaintext hash verification failed")
        return content

    @staticmethod
    def _validate_command(matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, roles: frozenset[Role]) -> None:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, roles)
        _require_positive_version(expected_version)

    @staticmethod
    def _begin(connection, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, command_name: str, payload_hash: str, allowed_roles: frozenset[Role]) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
        prior = _prior_receipt(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash)
        if prior is not None:
            return prior
        _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, allowed_roles=allowed_roles)
        return None

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _validate_editable_media_type(media_type: str) -> None:
    if media_type not in {_DOCX, _XLSX}:
        raise CaseLedgerPersistenceBlocked("reviewable draft must be a DOCX or XLSX file")


def _validate_content_addressed_key(object_key: str, content_hash: str) -> None:
    if object_key != f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca":
        raise CaseLedgerPersistenceBlocked("reviewable Office artifact object key is not content-addressed")


def _validate_office_container(content: bytes, media_type: str) -> None:
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
    except (BadZipFile, OSError) as error:
        raise CaseLedgerPersistenceBlocked("reviewable editable artifact is not a valid Office container") from error
    required = {"[Content_Types].xml", "word/document.xml"} if media_type == _DOCX else {"[Content_Types].xml", "xl/workbook.xml"}
    if not required.issubset(names):
        raise CaseLedgerPersistenceBlocked("reviewable editable artifact does not match its Office media type")


def _validate_pdf(content: bytes) -> None:
    if len(content) < 8 or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise CaseLedgerPersistenceBlocked("review PDF is not a complete PDF")


def _review_input_hash(*, editable_media_type: str, editable_sha256: str, editable_bytes: int, review_pdf_sha256: str, review_pdf_bytes: int, review_pdf_page_count: int, approval_input_hash: str, render_verification_hash: str) -> str:
    detected_kind = "WORD_DOCUMENT" if editable_media_type == _DOCX else "SPREADSHEET"
    payload = {
        "schema_version": "reviewable-office-draft-v1", "approval_hash": approval_input_hash,
        "detected_kind": detected_kind, "editable_media_type": editable_media_type,
        "editable_sha256": editable_sha256, "editable_bytes": editable_bytes,
        "review_pdf_sha256": review_pdf_sha256, "review_pdf_bytes": review_pdf_bytes,
        "review_pdf_page_count": review_pdf_page_count,
        "render_verification_hash": render_verification_hash,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
