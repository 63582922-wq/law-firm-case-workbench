"""Approval- and dependency-bound PostgreSQL submission workflow."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .models import Actor, Role
from .submission_access import VerifiedSubmissionExportLocator
from .submission_bundle_compiler import (
    SubmissionBundleCompilationBlocked,
    verify_submission_export_bytes,
)


_PDF_MEDIA_TYPE = "application/pdf"
_MAX_WORK_PRODUCT_BYTES = 128 * 1024 * 1024
_MAX_EXPORT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class SubmissionComponentSelection:
    work_product_id: str
    sequence: int
    court_filename: str


@dataclass(frozen=True)
class PersistentSubmissionSnapshot:
    matter_id: str
    matter_version: int
    stage: str
    work_products: tuple[dict[str, Any], ...]
    bundles: tuple[dict[str, Any], ...]
    current_bundle: dict[str, Any] | None
    current_components: tuple[dict[str, Any], ...]
    current_export: dict[str, Any] | None
    snapshot_hash: str


@dataclass(frozen=True)
class PersistentLockedSubmissionCompilation:
    matter_id: str
    matter_version: int
    bundle: dict[str, Any]
    components: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresSubmissionStore:
    _REGISTER_ROLES = frozenset(
        {Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.SYSTEM_WORKER}
    )
    _REVIEW_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _LEAD_ROLES = frozenset({Role.LEAD_LAWYER})
    _SYSTEM_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )
    _EXPORT_READ_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})

    def __init__(
        self,
        dsn: str,
        *,
        artifact_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._artifact_reader = artifact_reader

    def register_work_product_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        document_kind: str,
        audience: str,
        media_type: str,
        storage_object_key: str,
        artifact_sha256: str,
        byte_size: int,
        page_count: int | None,
        semantic_text_sha256: str | None,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._REGISTER_ROLES
        )
        _require_text(document_kind, "document_kind")
        normalized_kind = document_kind.strip()
        if audience not in {"COURT_SUBMISSION", "INTERNAL_ONLY"}:
            raise CaseLedgerPersistenceBlocked("unsupported work-product audience")
        if media_type != _PDF_MEDIA_TYPE:
            raise CaseLedgerPersistenceBlocked("v1 court work products must be PDF")
        _validate_sha256("artifact_sha256", artifact_sha256)
        if semantic_text_sha256 is not None:
            _validate_sha256("semantic_text_sha256", semantic_text_sha256)
        if normalized_kind == "DEFENCE_STATEMENT" and semantic_text_sha256 is None:
            raise CaseLedgerPersistenceBlocked(
                "defence statement requires the extracted approved-text hash"
            )
        if byte_size < 1 or byte_size > _MAX_WORK_PRODUCT_BYTES:
            raise CaseLedgerPersistenceBlocked("work product exceeds configured byte limit")
        if page_count is None or page_count < 1:
            raise CaseLedgerPersistenceBlocked("PDF work product requires a positive page count")
        _validate_content_addressed_key(storage_object_key, artifact_sha256)
        plaintext = self._read_authenticated_artifact(storage_object_key, artifact_sha256)
        if len(plaintext) != byte_size:
            raise CaseLedgerPersistenceBlocked("work-product byte size differs from encrypted object")
        _validate_pdf(plaintext)

        command_name = "REGISTER_SUBMISSION_WORK_PRODUCT_CANDIDATE"
        work_product_id = str(uuid4())
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "document_kind": normalized_kind,
            "audience": audience,
            "media_type": media_type,
            "storage_object_key": storage_object_key,
            "artifact_sha256": artifact_sha256,
            "byte_size": byte_size,
            "page_count": page_count,
            "semantic_text_sha256": semantic_text_sha256,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._REGISTER_ROLES,
            )
            if prior is not None:
                return prior
            connection.execute(
                """
                INSERT INTO submission_work_products (
                    work_product_id, firm_id, matter_id, document_kind, audience,
                    media_type, storage_object_key, artifact_sha256, byte_size,
                    page_count, semantic_text_sha256, status, registered_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'CANDIDATE', %s)
                """,
                (
                    work_product_id,
                    actor.firm_id,
                    matter_id,
                    normalized_kind,
                    audience,
                    media_type,
                    storage_object_key,
                    artifact_sha256,
                    byte_size,
                    page_count,
                    semantic_text_sha256,
                    actor.actor_id,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="SUBMISSION_WORK_PRODUCT_CANDIDATE_REGISTERED",
                object_type="SUBMISSION_WORK_PRODUCT",
                object_id=work_product_id,
                audit_payload={
                    "work_product_id": work_product_id,
                    "document_kind": normalized_kind,
                    "audience": audience,
                    "artifact_sha256": artifact_sha256,
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def approve_work_product(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        work_product_id: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._REVIEW_ROLES
        )
        _validate_uuid("work_product_id", work_product_id)
        _validate_sha256("approval_hash", approval_hash)
        command_name = "APPROVE_SUBMISSION_WORK_PRODUCT"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "work_product_id": work_product_id,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._REVIEW_ROLES,
            )
            if prior is not None:
                return prior
            product = connection.execute(
                """
                SELECT document_kind, audience, artifact_sha256, status
                FROM submission_work_products
                WHERE work_product_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (work_product_id, matter_id, actor.firm_id),
            ).fetchone()
            if product is None:
                raise KeyError(work_product_id)
            if product["status"] != "CANDIDATE":
                raise CaseLedgerPersistenceBlocked("only a candidate work product can be approved")
            connection.execute(
                """
                UPDATE submission_work_products
                SET status = 'APPROVED', approved_by = %s,
                    approval_hash = %s, approved_at = now()
                WHERE work_product_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (actor.actor_id, approval_hash, work_product_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="SUBMISSION_WORK_PRODUCT_APPROVED",
                object_type="SUBMISSION_WORK_PRODUCT",
                object_id=work_product_id,
                audit_payload={
                    "work_product_id": work_product_id,
                    "document_kind": product["document_kind"],
                    "audience": product["audience"],
                    "artifact_sha256": product["artifact_sha256"],
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
                stale_calculations=False,
            )

    def create_qa_ready_bundle(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        selections: tuple[SubmissionComponentSelection, ...],
        required_document_kinds: tuple[str, ...],
        evidence_manifest_id: str,
        legal_bundle_id: str,
        calculation_run_id: str,
        final_text_approval_id: str,
        expected_qa_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES
        )
        normalized_selections = _validate_selections(selections)
        normalized_required = _unique_texts(required_document_kinds, "required_document_kinds")
        if not normalized_required:
            raise CaseLedgerPersistenceBlocked("submission profile requires document kinds")
        for label, value in (
            ("evidence_manifest_id", evidence_manifest_id),
            ("legal_bundle_id", legal_bundle_id),
            ("calculation_run_id", calculation_run_id),
            ("final_text_approval_id", final_text_approval_id),
        ):
            _validate_uuid(label, value)
        _validate_sha256("expected_qa_hash", expected_qa_hash)
        command_name = "CREATE_QA_READY_SUBMISSION_BUNDLE"
        request_payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "selections": tuple(
                {
                    "work_product_id": item.work_product_id,
                    "sequence": item.sequence,
                    "court_filename": item.court_filename,
                }
                for item in normalized_selections
            ),
            "required_document_kinds": normalized_required,
            "evidence_manifest_id": evidence_manifest_id,
            "legal_bundle_id": legal_bundle_id,
            "calculation_run_id": calculation_run_id,
            "final_text_approval_id": final_text_approval_id,
            "expected_qa_hash": expected_qa_hash,
        }
        request_hash = _payload_hash(request_payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=request_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            matter = connection.execute(
                """
                SELECT stage, current_submission_bundle_id
                FROM matters WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter["stage"] != "FINAL_QA":
                raise CaseLedgerPersistenceBlocked(
                    "submission QA bundle can only be created from FINAL_QA"
                )
            if matter["current_submission_bundle_id"] is not None:
                raise CaseLedgerPersistenceBlocked("a current submission bundle already exists")
            products = _load_selected_products(
                connection,
                actor=actor,
                matter_id=matter_id,
                selections=normalized_selections,
            )
            selected_kinds = {row["document_kind"] for row in products}
            missing_kinds = sorted(set(normalized_required) - selected_kinds)
            if missing_kinds:
                raise CaseLedgerPersistenceBlocked(
                    "submission is missing required documents: " + ", ".join(missing_kinds)
                )
            defence_products = [row for row in products if row["document_kind"] == "DEFENCE_STATEMENT"]
            if len(defence_products) != 1 or defence_products[0]["semantic_text_sha256"] is None:
                raise CaseLedgerPersistenceBlocked(
                    "submission requires exactly one text-hash-bound defence statement"
                )
            manifest = connection.execute(
                """
                SELECT manifest_id, content_hash, status
                FROM evidence_manifests
                WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (evidence_manifest_id, matter_id, actor.firm_id),
            ).fetchone()
            legal_bundle = connection.execute(
                """
                SELECT bundle_id, bundle_hash, status
                FROM case_legal_bundles
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (legal_bundle_id, matter_id, actor.firm_id),
            ).fetchone()
            calculation = connection.execute(
                """
                SELECT run_id, output_hash, status, legal_bundle_id, legal_bundle_hash
                FROM calculation_runs
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (calculation_run_id, matter_id, actor.firm_id),
            ).fetchone()
            approval = connection.execute(
                """
                SELECT approval_id, object_hash, approval_type,
                       approved_matter_version, revoked_at
                FROM approvals
                WHERE approval_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (final_text_approval_id, matter_id, actor.firm_id),
            ).fetchone()
            if manifest is None or manifest["status"] != "LOCKED":
                raise CaseLedgerPersistenceBlocked("submission requires the current locked evidence Manifest")
            if legal_bundle is None or legal_bundle["status"] != "APPROVED":
                raise CaseLedgerPersistenceBlocked("submission requires the current approved legal bundle")
            if (
                calculation is None
                or calculation["status"] != "VERIFIED"
                or str(calculation["legal_bundle_id"]) != legal_bundle_id
                or calculation["legal_bundle_hash"] != legal_bundle["bundle_hash"]
            ):
                raise CaseLedgerPersistenceBlocked(
                    "submission calculation must be verified against the selected legal bundle"
                )
            if (
                approval is None
                or approval["approval_type"] != "FINAL_TEXT"
                or approval["revoked_at"] is not None
                or approval["approved_matter_version"] != expected_version
                or approval["object_hash"] != defence_products[0]["semantic_text_sha256"]
            ):
                raise CaseLedgerPersistenceBlocked(
                    "submission defence statement differs from the current final-text approval"
                )

            input_payload = _compilation_input_payload(
                matter_id=matter_id,
                matter_version=expected_version,
                required_document_kinds=normalized_required,
                selections=normalized_selections,
                products=products,
                evidence_manifest_id=evidence_manifest_id,
                evidence_manifest_hash=manifest["content_hash"],
                legal_bundle_id=legal_bundle_id,
                legal_bundle_hash=legal_bundle["bundle_hash"],
                calculation_run_id=calculation_run_id,
                calculation_output_hash=calculation["output_hash"],
                final_text_approval_id=final_text_approval_id,
                final_text_hash=approval["object_hash"],
                approved_by=actor.actor_id,
            )
            input_hash = _payload_hash(input_payload)
            if input_hash != expected_qa_hash:
                raise CaseLedgerPersistenceBlocked(
                    "submission QA hash differs from the current server-derived inputs"
                )
            bundle_id = str(uuid4())
            connection.execute(
                """
                UPDATE submission_bundles
                SET validity = 'STALE'
                WHERE matter_id = %s AND firm_id = %s AND validity = 'VALID'
                  AND lifecycle IN ('DRAFT', 'QA_READY')
                """,
                (matter_id, actor.firm_id),
            )
            connection.execute(
                """
                INSERT INTO submission_bundles (
                    bundle_id, matter_id, firm_id, lifecycle, validity,
                    final_text_hash, approved_by, approved_matter_version
                ) VALUES (%s, %s, %s, 'QA_READY', 'VALID', %s, %s, %s)
                """,
                (
                    bundle_id,
                    matter_id,
                    actor.firm_id,
                    approval["object_hash"],
                    actor.actor_id,
                    expected_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO submission_compilation_specs (
                    bundle_id, firm_id, matter_id, export_profile, currency,
                    input_hash, required_document_kinds,
                    evidence_manifest_id, evidence_manifest_hash,
                    legal_bundle_id, legal_bundle_hash,
                    calculation_run_id, calculation_output_hash,
                    final_text_approval_id, final_text_hash,
                    qa_hash, qa_approved_by, qa_approved_at
                ) VALUES (%s, %s, %s, 'COURT_PDF_ONLY_V1', 'CNY', %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    bundle_id,
                    actor.firm_id,
                    matter_id,
                    input_hash,
                    Jsonb(list(normalized_required)),
                    evidence_manifest_id,
                    manifest["content_hash"],
                    legal_bundle_id,
                    legal_bundle["bundle_hash"],
                    calculation_run_id,
                    calculation["output_hash"],
                    final_text_approval_id,
                    approval["object_hash"],
                    input_hash,
                    actor.actor_id,
                ),
            )
            for selection, product in zip(normalized_selections, products, strict=True):
                connection.execute(
                    """
                    INSERT INTO submission_bundle_components (
                        bundle_id, work_product_id, firm_id, matter_id, sequence,
                        document_kind, court_filename, media_type, storage_object_key,
                        artifact_sha256, byte_size, approval_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bundle_id,
                        selection.work_product_id,
                        actor.firm_id,
                        matter_id,
                        selection.sequence,
                        product["document_kind"],
                        selection.court_filename,
                        product["media_type"],
                        product["storage_object_key"],
                        product["artifact_sha256"],
                        product["byte_size"],
                        product["approval_hash"],
                    ),
                )
            connection.execute(
                "UPDATE matters SET stage = 'READY_TO_EXPORT' WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                event_type="SUBMISSION_BUNDLE_QA_READY",
                object_type="SUBMISSION_BUNDLE",
                object_id=bundle_id,
                audit_payload={
                    "bundle_id": bundle_id,
                    "input_hash": input_hash,
                    "qa_hash": input_hash,
                    "currency": "CNY",
                    "component_count": len(products),
                    "evidence_manifest_hash": manifest["content_hash"],
                    "legal_bundle_hash": legal_bundle["bundle_hash"],
                    "calculation_output_hash": calculation["output_hash"],
                    "final_text_hash": approval["object_hash"],
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def lock_submission_bundle(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        bundle_id: str,
        expected_input_hash: str,
        lock_approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._LEAD_ROLES
        )
        _validate_uuid("bundle_id", bundle_id)
        _validate_sha256("expected_input_hash", expected_input_hash)
        _validate_sha256("lock_approval_hash", lock_approval_hash)
        command_name = "LOCK_SUBMISSION_BUNDLE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "bundle_id": bundle_id,
            "expected_input_hash": expected_input_hash,
            "lock_approval_hash": lock_approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._LEAD_ROLES,
            )
            if prior is not None:
                return prior
            row = connection.execute(
                """
                SELECT bundle.lifecycle, bundle.validity, spec.input_hash,
                       manifest.status AS manifest_status,
                       legal.status AS legal_status,
                       calculation.status AS calculation_status,
                       approval.revoked_at,
                       matter.stage, matter.current_submission_bundle_id
                FROM submission_bundles bundle
                JOIN submission_compilation_specs spec
                  ON spec.bundle_id = bundle.bundle_id AND spec.firm_id = bundle.firm_id
                 AND spec.matter_id = bundle.matter_id
                JOIN evidence_manifests manifest
                  ON manifest.manifest_id = spec.evidence_manifest_id
                 AND manifest.firm_id = spec.firm_id AND manifest.matter_id = spec.matter_id
                 AND manifest.content_hash = spec.evidence_manifest_hash
                JOIN case_legal_bundles legal
                  ON legal.bundle_id = spec.legal_bundle_id
                 AND legal.firm_id = spec.firm_id AND legal.matter_id = spec.matter_id
                 AND legal.bundle_hash = spec.legal_bundle_hash
                JOIN calculation_runs calculation
                  ON calculation.run_id = spec.calculation_run_id
                 AND calculation.firm_id = spec.firm_id AND calculation.matter_id = spec.matter_id
                 AND calculation.output_hash = spec.calculation_output_hash
                JOIN approvals approval
                  ON approval.approval_id = spec.final_text_approval_id
                 AND approval.firm_id = spec.firm_id AND approval.matter_id = spec.matter_id
                 AND approval.object_hash = spec.final_text_hash
                JOIN matters matter
                  ON matter.matter_id = bundle.matter_id AND matter.firm_id = bundle.firm_id
                WHERE bundle.bundle_id = %s AND bundle.matter_id = %s AND bundle.firm_id = %s
                FOR UPDATE OF bundle
                """,
                (bundle_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(bundle_id)
            if (
                row["lifecycle"] != "QA_READY"
                or row["validity"] != "VALID"
                or row["input_hash"] != expected_input_hash
                or row["manifest_status"] != "LOCKED"
                or row["legal_status"] != "APPROVED"
                or row["calculation_status"] != "VERIFIED"
                or row["revoked_at"] is not None
                or row["stage"] != "READY_TO_EXPORT"
                or row["current_submission_bundle_id"] is not None
            ):
                raise CaseLedgerPersistenceBlocked(
                    "submission bundle or one of its approved dependencies is no longer lockable"
                )
            connection.execute(
                """
                UPDATE submission_bundles
                SET lifecycle = 'LOCKED', locked_at = now()
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (bundle_id, matter_id, actor.firm_id),
            )
            connection.execute(
                """
                UPDATE matters SET current_submission_bundle_id = %s
                WHERE matter_id = %s AND firm_id = %s
                """,
                (bundle_id, matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="SUBMISSION_BUNDLE_LOCKED",
                object_type="SUBMISSION_BUNDLE",
                object_id=bundle_id,
                audit_payload={
                    "bundle_id": bundle_id,
                    "input_hash": expected_input_hash,
                    "lock_approval_hash": lock_approval_hash,
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def register_verified_export(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        bundle_id: str,
        input_hash: str,
        court_zip_object_key: str,
        court_zip_sha256: str,
        court_zip_bytes: int,
        internal_manifest_object_key: str,
        internal_manifest_sha256: str,
        component_count: int,
        verification_hash: str,
    ) -> CaseLedgerCommandReceipt:
        self._validate_command(
            matter_id, actor, expected_version, idempotency_key, self._SYSTEM_ROLES
        )
        _validate_uuid("bundle_id", bundle_id)
        for label, value in (
            ("input_hash", input_hash),
            ("court_zip_sha256", court_zip_sha256),
            ("internal_manifest_sha256", internal_manifest_sha256),
            ("verification_hash", verification_hash),
        ):
            _validate_sha256(label, value)
        _validate_content_addressed_key(court_zip_object_key, court_zip_sha256)
        _validate_content_addressed_key(internal_manifest_object_key, internal_manifest_sha256)
        if court_zip_bytes < 1 or court_zip_bytes > _MAX_EXPORT_BYTES:
            raise CaseLedgerPersistenceBlocked("court ZIP exceeds configured byte limit")
        if component_count < 1 or component_count > 100:
            raise CaseLedgerPersistenceBlocked("submission component count is invalid")
        court_zip = self._read_authenticated_artifact(court_zip_object_key, court_zip_sha256)
        manifest = self._read_authenticated_artifact(
            internal_manifest_object_key, internal_manifest_sha256
        )
        if len(court_zip) != court_zip_bytes or len(manifest) < 2:
            raise CaseLedgerPersistenceBlocked("submission export artifact size is invalid")
        try:
            verify_submission_export_bytes(
                court_zip_bytes=court_zip,
                manifest_bytes=manifest,
                expected_court_zip_sha256=court_zip_sha256,
                expected_manifest_sha256=internal_manifest_sha256,
                expected_bundle_id=bundle_id,
                expected_input_hash=input_hash,
                expected_component_count=component_count,
            )
        except SubmissionBundleCompilationBlocked as error:
            raise CaseLedgerPersistenceBlocked(
                "submission export failed independent ZIP and manifest verification"
            ) from error
        command_name = "REGISTER_VERIFIED_SUBMISSION_EXPORT"
        export_id = str(uuid4())
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "bundle_id": bundle_id,
            "input_hash": input_hash,
            "court_zip_sha256": court_zip_sha256,
            "court_zip_bytes": court_zip_bytes,
            "internal_manifest_sha256": internal_manifest_sha256,
            "component_count": component_count,
            "verification_hash": verification_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                payload_hash=payload_hash,
                allowed_roles=self._SYSTEM_ROLES,
            )
            if prior is not None:
                return prior
            bundle = connection.execute(
                """
                SELECT bundle.lifecycle, bundle.validity, spec.input_hash,
                       matter.current_submission_bundle_id,
                       (SELECT count(*) FROM submission_bundle_components component
                        WHERE component.bundle_id = bundle.bundle_id
                          AND component.matter_id = bundle.matter_id
                          AND component.firm_id = bundle.firm_id) AS component_count
                FROM submission_bundles bundle
                JOIN submission_compilation_specs spec
                  ON spec.bundle_id = bundle.bundle_id AND spec.firm_id = bundle.firm_id
                 AND spec.matter_id = bundle.matter_id
                JOIN matters matter
                  ON matter.matter_id = bundle.matter_id AND matter.firm_id = bundle.firm_id
                WHERE bundle.bundle_id = %s AND bundle.matter_id = %s AND bundle.firm_id = %s
                FOR UPDATE OF bundle
                """,
                (bundle_id, matter_id, actor.firm_id),
            ).fetchone()
            if bundle is None:
                raise KeyError(bundle_id)
            if (
                bundle["lifecycle"] != "LOCKED"
                or bundle["validity"] != "VALID"
                or bundle["input_hash"] != input_hash
                or str(bundle["current_submission_bundle_id"]) != bundle_id
                or bundle["component_count"] != component_count
            ):
                raise CaseLedgerPersistenceBlocked(
                    "only the current valid locked bundle can register its exact verified export"
                )
            connection.execute(
                """
                INSERT INTO submission_compilation_exports (
                    export_id, bundle_id, firm_id, matter_id, input_hash,
                    court_zip_object_key, court_zip_sha256, court_zip_bytes,
                    internal_manifest_object_key, internal_manifest_sha256,
                    component_count, verification_hash, verified_by, verified_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    export_id,
                    bundle_id,
                    actor.firm_id,
                    matter_id,
                    input_hash,
                    court_zip_object_key,
                    court_zip_sha256,
                    court_zip_bytes,
                    internal_manifest_object_key,
                    internal_manifest_sha256,
                    component_count,
                    verification_hash,
                    actor.actor_id,
                ),
            )
            connection.execute(
                """
                UPDATE submission_bundles
                SET lifecycle = 'EXPORTED', exported_at = now()
                WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (bundle_id, matter_id, actor.firm_id),
            )
            connection.execute(
                "UPDATE matters SET stage = 'EXPORTED' WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="SUBMISSION_EXPORT_VERIFIED",
                object_type="SUBMISSION_EXPORT",
                object_id=export_id,
                audit_payload={
                    "export_id": export_id,
                    "bundle_id": bundle_id,
                    "input_hash": input_hash,
                    "court_zip_sha256": court_zip_sha256,
                    "internal_manifest_sha256": internal_manifest_sha256,
                    "component_count": component_count,
                    "verification_hash": verification_hash,
                },
                stale_submission=False,
                stale_calculations=False,
            )

    def get_submission_snapshot(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentSubmissionSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter = connection.execute(
                """
                SELECT version, stage, current_submission_bundle_id
                FROM matters WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            products = connection.execute(
                """
                SELECT work_product_id, document_kind, audience, media_type,
                       artifact_sha256, byte_size, page_count, semantic_text_sha256,
                       status, approved_by, approval_hash, approved_at,
                       stale_at, stale_reason, created_at
                FROM submission_work_products
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY document_kind, created_at DESC, work_product_id
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            bundles = connection.execute(
                """
                SELECT bundle.bundle_id, bundle.lifecycle, bundle.validity,
                       bundle.final_text_hash, bundle.approved_by,
                       bundle.approved_matter_version, bundle.locked_at,
                       bundle.exported_at, bundle.created_at,
                       spec.export_profile, spec.currency, spec.input_hash,
                       spec.required_document_kinds, spec.evidence_manifest_id,
                       spec.evidence_manifest_hash, spec.legal_bundle_id,
                       spec.legal_bundle_hash, spec.calculation_run_id,
                       spec.calculation_output_hash, spec.final_text_approval_id,
                       spec.qa_hash, spec.qa_approved_by, spec.qa_approved_at
                FROM submission_bundles bundle
                JOIN submission_compilation_specs spec
                  ON spec.bundle_id = bundle.bundle_id AND spec.firm_id = bundle.firm_id
                 AND spec.matter_id = bundle.matter_id
                WHERE bundle.matter_id = %s AND bundle.firm_id = %s
                ORDER BY bundle.created_at DESC, bundle.bundle_id
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            current_bundle_id = matter["current_submission_bundle_id"]
            components: list[dict[str, Any]] = []
            export = None
            if current_bundle_id is not None:
                components = connection.execute(
                    """
                    SELECT work_product_id, sequence, document_kind, court_filename,
                           media_type, artifact_sha256, byte_size, approval_hash
                    FROM submission_bundle_components
                    WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                    ORDER BY sequence
                    """,
                    (current_bundle_id, matter_id, actor.firm_id),
                ).fetchall()
                export = connection.execute(
                    """
                    SELECT export_id, bundle_id, input_hash, court_zip_sha256,
                           court_zip_bytes, internal_manifest_sha256, component_count,
                           verification_hash, verified_by, verified_at, created_at
                    FROM submission_compilation_exports
                    WHERE bundle_id = %s AND matter_id = %s AND firm_id = %s
                    """,
                    (current_bundle_id, matter_id, actor.firm_id),
                ).fetchone()
        serialized_bundles = tuple(_serialize_row(row) for row in bundles)
        current_bundle = next(
            (
                item
                for item in serialized_bundles
                if item["bundle_id"] == str(current_bundle_id)
            ),
            None,
        )
        payload = {
            "matter_id": matter_id,
            "matter_version": matter["version"],
            "stage": matter["stage"],
            "work_products": tuple(_serialize_row(row) for row in products),
            "bundles": serialized_bundles,
            "current_bundle": current_bundle,
            "current_components": tuple(_serialize_row(row) for row in components),
            "current_export": _serialize_row(export) if export is not None else None,
        }
        return PersistentSubmissionSnapshot(snapshot_hash=_payload_hash(payload), **payload)

    def get_locked_compilation_snapshot(
        self,
        *,
        matter_id: str,
        bundle_id: str,
        actor: Actor,
    ) -> PersistentLockedSubmissionCompilation:
        """Return storage locators only to the dedicated local system worker."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._SYSTEM_ROLES)
        _validate_uuid("bundle_id", bundle_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._SYSTEM_ROLES,
            )
            row = connection.execute(
                """
                SELECT matter.version AS matter_version, matter.stage,
                       matter.current_submission_bundle_id,
                       bundle.bundle_id, bundle.lifecycle, bundle.validity,
                       bundle.final_text_hash, bundle.approved_by,
                       spec.export_profile, spec.currency, spec.input_hash,
                       spec.required_document_kinds,
                       spec.evidence_manifest_id, spec.evidence_manifest_hash,
                       spec.legal_bundle_id, spec.legal_bundle_hash,
                       spec.calculation_run_id, spec.calculation_output_hash,
                       spec.final_text_approval_id, spec.qa_hash,
                       spec.qa_approved_by, spec.qa_approved_at
                FROM submission_bundles bundle
                JOIN submission_compilation_specs spec
                  ON spec.bundle_id = bundle.bundle_id AND spec.firm_id = bundle.firm_id
                 AND spec.matter_id = bundle.matter_id
                JOIN matters matter
                  ON matter.matter_id = bundle.matter_id AND matter.firm_id = bundle.firm_id
                WHERE bundle.bundle_id = %s AND bundle.matter_id = %s AND bundle.firm_id = %s
                """,
                (bundle_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(bundle_id)
            if (
                row["lifecycle"] != "LOCKED"
                or row["validity"] != "VALID"
                or str(row["current_submission_bundle_id"]) != bundle_id
                or row["stage"] != "READY_TO_EXPORT"
                or row["qa_hash"] != row["input_hash"]
            ):
                raise CaseLedgerPersistenceBlocked(
                    "worker compilation requires the current valid locked submission bundle"
                )
            components = connection.execute(
                """
                SELECT component.work_product_id, component.sequence,
                       component.document_kind, component.court_filename,
                       component.media_type, component.storage_object_key,
                       component.artifact_sha256, component.byte_size,
                       component.approval_hash,
                       product.status AS work_product_status,
                       product.audience AS work_product_audience
                FROM submission_bundle_components component
                JOIN submission_work_products product
                  ON product.work_product_id = component.work_product_id
                 AND product.firm_id = component.firm_id
                 AND product.matter_id = component.matter_id
                 AND product.artifact_sha256 = component.artifact_sha256
                 AND product.approval_hash = component.approval_hash
                WHERE component.bundle_id = %s AND component.matter_id = %s
                  AND component.firm_id = %s
                ORDER BY component.sequence
                """,
                (bundle_id, matter_id, actor.firm_id),
            ).fetchall()
            if not components or any(
                item["work_product_status"] != "APPROVED"
                or item["work_product_audience"] != "COURT_SUBMISSION"
                for item in components
            ):
                raise CaseLedgerPersistenceBlocked(
                    "locked submission contains a work product that is no longer approved"
                )
        bundle = _serialize_row(row)
        bundle.pop("matter_version")
        payload = {
            "matter_id": matter_id,
            "matter_version": row["matter_version"],
            "bundle": bundle,
            "components": tuple(_serialize_row(item) for item in components),
        }
        return PersistentLockedSubmissionCompilation(
            snapshot_hash=_payload_hash(payload), **payload
        )

    def get_verified_export_locator(
        self,
        *,
        matter_id: str,
        export_id: str,
        actor: Actor,
    ) -> VerifiedSubmissionExportLocator:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._EXPORT_READ_ROLES)
        _validate_uuid("export_id", export_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._EXPORT_READ_ROLES,
            )
            row = connection.execute(
                """
                SELECT export.export_id, export.bundle_id,
                       export.court_zip_object_key, export.court_zip_sha256,
                       export.court_zip_bytes, bundle.lifecycle, bundle.validity
                FROM submission_compilation_exports export
                JOIN submission_bundles bundle
                  ON bundle.bundle_id = export.bundle_id AND bundle.firm_id = export.firm_id
                 AND bundle.matter_id = export.matter_id
                JOIN matters matter
                  ON matter.matter_id = export.matter_id AND matter.firm_id = export.firm_id
                 AND matter.current_submission_bundle_id = export.bundle_id
                WHERE export.export_id = %s AND export.matter_id = %s AND export.firm_id = %s
                """,
                (export_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(export_id)
            if row["lifecycle"] != "EXPORTED" or row["validity"] != "VALID":
                raise CaseLedgerPersistenceBlocked(
                    "only the current valid verified submission export can be downloaded"
                )
            return VerifiedSubmissionExportLocator(
                firm_id=actor.firm_id,
                matter_id=matter_id,
                export_id=str(row["export_id"]),
                bundle_id=str(row["bundle_id"]),
                object_key=row["court_zip_object_key"],
                court_zip_sha256=row["court_zip_sha256"],
                court_zip_bytes=row["court_zip_bytes"],
                lifecycle=row["lifecycle"],
                validity=row["validity"],
            )

    def _read_authenticated_artifact(self, object_key: str, expected_hash: str) -> bytes:
        if self._artifact_reader is None:
            raise CaseLedgerPersistenceBlocked(
                "submission artifact registration requires an encrypted-object verifier"
            )
        try:
            plaintext = self._artifact_reader(object_key, expected_hash)
        except Exception as error:
            raise CaseLedgerPersistenceBlocked(
                "submission encrypted-object authentication failed"
            ) from error
        if not isinstance(plaintext, bytes) or sha256(plaintext).hexdigest() != expected_hash:
            raise CaseLedgerPersistenceBlocked(
                "submission encrypted-object plaintext hash verification failed"
            )
        return plaintext

    @staticmethod
    def _validate_command(
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        roles: frozenset[Role],
    ) -> None:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, roles)
        _require_positive_version(expected_version)

    @staticmethod
    def _begin(
        connection: psycopg.Connection,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        command_name: str,
        payload_hash: str,
        allowed_roles: frozenset[Role],
    ) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
        )
        prior = _prior_receipt(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if prior is not None:
            return prior
        _authorize_and_lock_matter(
            connection,
            actor=actor,
            matter_id=matter_id,
            expected_version=expected_version,
            allowed_roles=allowed_roles,
        )
        return None

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _load_selected_products(
    connection: psycopg.Connection,
    *,
    actor: Actor,
    matter_id: str,
    selections: tuple[SubmissionComponentSelection, ...],
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT work_product_id, document_kind, audience, media_type,
               storage_object_key, artifact_sha256, byte_size,
               semantic_text_sha256, status, approval_hash
        FROM submission_work_products
        WHERE work_product_id = ANY(%s) AND matter_id = %s AND firm_id = %s
        FOR SHARE
        """,
        ([item.work_product_id for item in selections], matter_id, actor.firm_id),
    ).fetchall()
    by_id = {str(row["work_product_id"]): row for row in rows}
    if len(by_id) != len(selections):
        raise CaseLedgerPersistenceBlocked("one or more submission work products are unavailable")
    ordered = tuple(by_id[item.work_product_id] for item in selections)
    for row in ordered:
        if (
            row["status"] != "APPROVED"
            or row["audience"] != "COURT_SUBMISSION"
            or row["media_type"] != _PDF_MEDIA_TYPE
            or row["approval_hash"] is None
        ):
            raise CaseLedgerPersistenceBlocked(
                "submission bundle accepts only approved court-facing PDF work products"
            )
    return ordered


def _compilation_input_payload(
    *,
    matter_id: str,
    matter_version: int,
    required_document_kinds: tuple[str, ...],
    selections: tuple[SubmissionComponentSelection, ...],
    products: tuple[dict[str, Any], ...],
    evidence_manifest_id: str,
    evidence_manifest_hash: str,
    legal_bundle_id: str,
    legal_bundle_hash: str,
    calculation_run_id: str,
    calculation_output_hash: str,
    final_text_approval_id: str,
    final_text_hash: str,
    approved_by: str,
) -> dict[str, Any]:
    return {
        "schema_version": "submission-qa-input-v1",
        "matter_id": matter_id,
        "matter_version": matter_version,
        "export_profile": "COURT_PDF_ONLY_V1",
        "currency": "CNY",
        "required_document_kinds": required_document_kinds,
        "components": tuple(
            {
                "sequence": selection.sequence,
                "court_filename": selection.court_filename,
                "work_product_id": str(row["work_product_id"]),
                "document_kind": row["document_kind"],
                "artifact_sha256": row["artifact_sha256"],
                "byte_size": row["byte_size"],
                "approval_hash": row["approval_hash"],
            }
            for selection, row in zip(selections, products, strict=True)
        ),
        "evidence_manifest_id": evidence_manifest_id,
        "evidence_manifest_hash": evidence_manifest_hash,
        "legal_bundle_id": legal_bundle_id,
        "legal_bundle_hash": legal_bundle_hash,
        "calculation_run_id": calculation_run_id,
        "calculation_output_hash": calculation_output_hash,
        "final_text_approval_id": final_text_approval_id,
        "final_text_hash": final_text_hash,
        "approved_by": approved_by,
    }


def _validate_selections(
    values: tuple[SubmissionComponentSelection, ...],
) -> tuple[SubmissionComponentSelection, ...]:
    if not values or len(values) > 100:
        raise CaseLedgerPersistenceBlocked("submission requires 1 to 100 selected files")
    ordered = tuple(sorted(values, key=lambda item: (item.sequence, item.work_product_id)))
    if [item.sequence for item in ordered] != list(range(1, len(ordered) + 1)):
        raise CaseLedgerPersistenceBlocked("submission file sequence must be contiguous from 1")
    if len({item.work_product_id for item in ordered}) != len(ordered):
        raise CaseLedgerPersistenceBlocked("submission work products must be unique")
    normalized_names: set[str] = set()
    for item in ordered:
        _validate_uuid("work_product_id", item.work_product_id)
        name = item.court_filename.strip()
        if (
            not name.endswith(".pdf")
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or any(marker in name.lower() for marker in ("最新", "最终", "修订", "终稿", "定稿", "final"))
        ):
            raise CaseLedgerPersistenceBlocked("court filename is unsafe or version-ambiguous")
        folded = name.casefold()
        if folded in normalized_names:
            raise CaseLedgerPersistenceBlocked("court filenames must be unique")
        normalized_names.add(folded)
    return ordered


def _unique_texts(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
    if len(normalized) != len(values):
        raise CaseLedgerPersistenceBlocked(f"{field_name} must be non-empty and unique")
    return normalized


def _validate_content_addressed_key(object_key: str, artifact_hash: str) -> None:
    expected = f"{artifact_hash[:2]}/{artifact_hash[2:4]}/{artifact_hash}.lca"
    if object_key != expected:
        raise CaseLedgerPersistenceBlocked("submission artifact object key is not content-addressed")


def _validate_pdf(content: bytes) -> None:
    if len(content) < 8 or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise CaseLedgerPersistenceBlocked("submission work product is not a complete PDF")


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, list):
            serialized[key] = tuple(value)
        elif key.endswith("_id") and value is not None:
            serialized[key] = str(value)
        else:
            serialized[key] = value
    return serialized
