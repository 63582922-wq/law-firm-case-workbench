"""Join a claimed intake lease to an OS-bound folder grant and evidence registration."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .evidence_intake_postgres import EvidenceIntakeItemLease
from .evidence_intake_worker import FileSafetyScanner, inspect_authorized_original
from .evidence_normalization_worker import EvidenceNormalizationBlocked, normalize_authorized_material
from .local_access_grants import AuthorizedOriginalFile, LocalFolderGrantRegistry, LocalSessionProof
from .managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked
from .models import Actor, Role


class EvidenceIntakeCoordinationBlocked(ValueError):
    """The claimed item lacks current authorization or a safe worker result."""


class EvidenceIntakePersistencePort(Protocol):
    def register_original_file(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def register_normalized_original_file(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def complete_evidence_intake_item(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def finalize_evidence_intake_item(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class CoordinatedEvidenceIntakeResult:
    run_id: str
    item_id: str
    outcome: str
    reason_code: str | None
    evidence_file_id: str | None
    inspection_hash: str
    final_matter_version: int


def coordinate_claimed_evidence_intake_item(
    *,
    lease: EvidenceIntakeItemLease,
    folder_grants: LocalFolderGrantRegistry,
    folder_grant_id: str,
    grant_actor: Actor,
    grant_session: LocalSessionProof,
    scanner: FileSafetyScanner,
    persistence: EvidenceIntakePersistencePort,
    system_actor: Actor,
    artifact_store: LocalEncryptedArtifactStore | None = None,
) -> CoordinatedEvidenceIntakeResult:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}) or system_actor.firm_id != grant_actor.firm_id:
        raise EvidenceIntakeCoordinationBlocked("material intake requires a dedicated same-firm SYSTEM_WORKER")
    if Role.SYSTEM_WORKER in grant_actor.roles or lease.matter_version < 1:
        raise EvidenceIntakeCoordinationBlocked("the folder grant must belong to an authenticated human case member")
    source = folder_grants.resolve_scanned_original(
        grant_id=folder_grant_id,
        actor=grant_actor,
        matter_id=lease.matter_id,
        session=grant_session,
        relative_path=lease.relative_path,
        expected_sha256=lease.expected_sha256,
        expected_byte_size=lease.expected_byte_size,
    )
    inspection = inspect_authorized_original(source, detected_kind=lease.detected_kind, scanner=scanner)
    prefix = f"evidence-intake:{lease.run_id}:{lease.item_id}"
    evidence_file_id = None
    result_outcome = inspection.outcome
    result_reason_code = inspection.reason_code
    result_inspection_hash = inspection.inspection_hash
    if inspection.outcome == "REGISTERABLE":
        if inspection.media_type is None or inspection.page_count is None:
            raise EvidenceIntakeCoordinationBlocked("registerable intake inspection is incomplete")
        registered = persistence.register_original_file(
            matter_id=lease.matter_id,
            actor=system_actor,
            expected_version=lease.matter_version,
            idempotency_key=f"{prefix}:register",
            original_label=lease.relative_path,
            original_file_sha256=lease.expected_sha256,
            byte_size=lease.expected_byte_size,
            media_type=inspection.media_type,
            page_count=inspection.page_count,
            source_scan_fingerprint=lease.scan_manifest_hash,
        )
        evidence_file_id = registered.object_id
        completed = persistence.complete_evidence_intake_item(
            matter_id=lease.matter_id,
            run_id=lease.run_id,
            item_id=lease.item_id,
            lease_id=lease.lease_id,
            evidence_file_id=evidence_file_id,
            inspection_hash=inspection.inspection_hash,
            scanner_name=inspection.scanner_name,
            scanner_definitions_version=inspection.scanner_definitions_version,
            actor=system_actor,
            expected_version=registered.matter_version,
            idempotency_key=f"{prefix}:complete",
        )
        final_version = completed.matter_version
    elif inspection.outcome == "REVIEW_REQUIRED" and lease.detected_kind in {"IMAGE", "TEXT"} and artifact_store:
        try:
            normalized = normalize_authorized_material(source, detected_kind=lease.detected_kind)
            stored = artifact_store.put_bytes(
                normalized.pdf_content,
                expected_sha256=normalized.pdf_sha256,
                case_root=_case_root_for(source),
            )
        except (EvidenceNormalizationBlocked, ManagedArtifactBlocked) as error:
            raise EvidenceIntakeCoordinationBlocked("eligible evidence normalization failed safely") from error
        registered = persistence.register_normalized_original_file(
            matter_id=lease.matter_id,
            actor=system_actor,
            expected_version=lease.matter_version,
            idempotency_key=f"{prefix}:register-normalized",
            original_label=lease.relative_path,
            original_file_sha256=lease.expected_sha256,
            byte_size=lease.expected_byte_size,
            source_media_type=normalized.source_media_type,
            page_count=normalized.page_count,
            source_scan_fingerprint=lease.scan_manifest_hash,
            normalizer_id=normalized.normalizer_id,
            normalizer_version=normalized.normalizer_version,
            transform_hash=normalized.transform_hash,
            normalized_pdf_sha256=normalized.pdf_sha256,
            normalized_pdf_bytes=normalized.pdf_bytes,
            normalized_pdf_object_key=stored.object_key,
        )
        evidence_file_id = registered.object_id
        completed = persistence.complete_evidence_intake_item(
            matter_id=lease.matter_id,
            run_id=lease.run_id,
            item_id=lease.item_id,
            lease_id=lease.lease_id,
            evidence_file_id=evidence_file_id,
            inspection_hash=_normalization_receipt_hash(inspection_hash=inspection.inspection_hash, normalized_pdf_sha256=normalized.pdf_sha256, transform_hash=normalized.transform_hash),
            scanner_name=inspection.scanner_name,
            scanner_definitions_version=inspection.scanner_definitions_version,
            actor=system_actor,
            expected_version=registered.matter_version,
            idempotency_key=f"{prefix}:complete-normalized",
        )
        final_version = completed.matter_version
        result_outcome = "REGISTERABLE"
        result_reason_code = None
        result_inspection_hash = _normalization_receipt_hash(
            inspection_hash=inspection.inspection_hash,
            normalized_pdf_sha256=normalized.pdf_sha256,
            transform_hash=normalized.transform_hash,
        )
    else:
        if inspection.reason_code is None:
            raise EvidenceIntakeCoordinationBlocked("non-registerable intake inspection lacks a stable reason")
        completed = persistence.finalize_evidence_intake_item(
            matter_id=lease.matter_id,
            run_id=lease.run_id,
            item_id=lease.item_id,
            lease_id=lease.lease_id,
            outcome=inspection.outcome,
            outcome_code=inspection.reason_code,
            inspection_hash=inspection.inspection_hash,
            scanner_name=inspection.scanner_name,
            scanner_definitions_version=inspection.scanner_definitions_version,
            actor=system_actor,
            expected_version=lease.matter_version,
            idempotency_key=f"{prefix}:finalize",
        )
        final_version = completed.matter_version
    return CoordinatedEvidenceIntakeResult(
        run_id=lease.run_id,
        item_id=lease.item_id,
        outcome=result_outcome,
        reason_code=result_reason_code,
        evidence_file_id=evidence_file_id,
        inspection_hash=result_inspection_hash,
        final_matter_version=final_version,
    )


def _case_root_for(source: AuthorizedOriginalFile) -> Path:
    """Recover the granted root from an already-authorized relative path only."""
    relative = PurePosixPath(source.relative_path)
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise EvidenceIntakeCoordinationBlocked("authorized source relative path is unsafe")
    root = source.path
    for _ in relative.parts:
        root = root.parent
    try:
        resolved_root = root.resolve(strict=True)
        if not source.path.resolve(strict=True).is_relative_to(resolved_root):
            raise EvidenceIntakeCoordinationBlocked("authorized source is outside its derived case root")
    except OSError as error:
        raise EvidenceIntakeCoordinationBlocked("authorized case root is unavailable") from error
    return resolved_root


def _normalization_receipt_hash(*, inspection_hash: str, normalized_pdf_sha256: str, transform_hash: str) -> str:
    payload = {
        "schema_version": "evidence-normalization-receipt-v1",
        "inspection_hash": inspection_hash,
        "normalized_pdf_sha256": normalized_pdf_sha256,
        "transform_hash": transform_hash,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
