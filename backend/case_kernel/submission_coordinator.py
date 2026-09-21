"""Compile, verify, encrypt and register one locked court submission bundle."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .managed_artifact_store import LocalEncryptedArtifactStore, StoredArtifactObject
from .models import Actor, Role
from .submission_bundle_compiler import (
    SubmissionArtifactBinding,
    SubmissionBundleCompilationResult,
    SubmissionBundleDescriptor,
    SubmissionBundleVerification,
    SubmissionDependency,
    compile_submission_bundle,
    verify_submission_bundle,
)
from .submission_postgres import PersistentLockedSubmissionCompilation


class SubmissionCoordinationBlocked(ValueError):
    """The worker snapshot, storage or persistence boundary is incomplete."""


class SubmissionExportPersistencePort(Protocol):
    def register_verified_export(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class CoordinatedSubmissionExport:
    bundle_id: str
    matter_id: str
    input_hash: str
    verification: SubmissionBundleVerification
    encrypted_court_zip: StoredArtifactObject
    encrypted_internal_manifest: StoredArtifactObject
    verification_hash: str
    registration_receipt: CaseLedgerCommandReceipt


def coordinate_locked_submission_export(
    *,
    snapshot: PersistentLockedSubmissionCompilation,
    artifact_store: LocalEncryptedArtifactStore,
    persistence: SubmissionExportPersistencePort,
    system_actor: Actor,
    case_root: str | Path,
    staging_root: str | Path,
    idempotency_key: str,
) -> CoordinatedSubmissionExport:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise SubmissionCoordinationBlocked(
            "submission export requires a dedicated SYSTEM_WORKER identity"
        )
    if not idempotency_key.strip() or len(idempotency_key) > 160:
        raise SubmissionCoordinationBlocked("submission export idempotency key is invalid")
    if snapshot.matter_id.strip() == "" or snapshot.matter_version < 1:
        raise SubmissionCoordinationBlocked("submission worker snapshot identity is invalid")
    bundle = snapshot.bundle
    if (
        bundle.get("lifecycle") != "LOCKED"
        or bundle.get("validity") != "VALID"
        or bundle.get("current_submission_bundle_id") != bundle.get("bundle_id")
        or bundle.get("qa_hash") != bundle.get("input_hash")
    ):
        raise SubmissionCoordinationBlocked(
            "submission export requires the current valid locked bundle snapshot"
        )
    if not snapshot.components:
        raise SubmissionCoordinationBlocked("locked submission has no court components")
    if any(
        item.get("work_product_status") != "APPROVED"
        or item.get("work_product_audience") != "COURT_SUBMISSION"
        for item in snapshot.components
    ):
        raise SubmissionCoordinationBlocked(
            "locked submission contains a stale or internal-only work product"
        )

    original_root = Path(case_root).expanduser().resolve(strict=True)
    if not original_root.is_dir():
        raise SubmissionCoordinationBlocked("case root must be an existing directory")
    staging = Path(staging_root).expanduser()
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    staging = staging.resolve(strict=True)
    staging.chmod(0o700)
    if staging == original_root or staging.is_relative_to(original_root):
        raise SubmissionCoordinationBlocked(
            "plaintext submission staging must not be inside the original case folder"
        )
    artifact_store.assert_separate_from_case_root(original_root)

    descriptor = _descriptor(snapshot)
    components = tuple(_component(item) for item in snapshot.components)
    with TemporaryDirectory(prefix="submission-export-", dir=staging) as temporary:
        output = Path(temporary) / "output"
        compilation = compile_submission_bundle(
            descriptor,
            components,
            artifact_reader=lambda object_key, expected_hash: artifact_store.read_bytes(
                object_key, expected_sha256=expected_hash
            ),
            output_directory=output,
        )
        verification = verify_submission_bundle(compilation)
        encrypted_zip = artifact_store.put_file(
            compilation.court_zip_path,
            expected_sha256=compilation.court_zip_sha256,
            case_root=original_root,
        )
        encrypted_manifest = artifact_store.put_file(
            compilation.internal_manifest_path,
            expected_sha256=compilation.internal_manifest_sha256,
            case_root=original_root,
        )

    verification_hash = _verification_hash(
        snapshot=snapshot,
        compilation=compilation,
        verification=verification,
        encrypted_zip=encrypted_zip,
        encrypted_manifest=encrypted_manifest,
    )
    receipt = persistence.register_verified_export(
        matter_id=snapshot.matter_id,
        actor=system_actor,
        expected_version=snapshot.matter_version,
        idempotency_key=idempotency_key,
        bundle_id=bundle["bundle_id"],
        input_hash=bundle["input_hash"],
        court_zip_object_key=encrypted_zip.object_key,
        court_zip_sha256=encrypted_zip.plaintext_sha256,
        court_zip_bytes=encrypted_zip.plaintext_bytes,
        internal_manifest_object_key=encrypted_manifest.object_key,
        internal_manifest_sha256=encrypted_manifest.plaintext_sha256,
        component_count=verification.component_count,
        verification_hash=verification_hash,
    )
    return CoordinatedSubmissionExport(
        bundle_id=bundle["bundle_id"],
        matter_id=snapshot.matter_id,
        input_hash=bundle["input_hash"],
        verification=verification,
        encrypted_court_zip=encrypted_zip,
        encrypted_internal_manifest=encrypted_manifest,
        verification_hash=verification_hash,
        registration_receipt=receipt,
    )


def _descriptor(
    snapshot: PersistentLockedSubmissionCompilation,
) -> SubmissionBundleDescriptor:
    bundle = snapshot.bundle
    approved_at = bundle.get("qa_approved_at")
    if not isinstance(approved_at, str):
        raise SubmissionCoordinationBlocked("submission QA approval time is missing")
    from datetime import datetime

    return SubmissionBundleDescriptor(
        bundle_id=bundle["bundle_id"],
        matter_id=snapshot.matter_id,
        matter_version=snapshot.matter_version,
        export_profile=bundle["export_profile"],
        currency=bundle["currency"],
        approved_input_hash=bundle["input_hash"],
        required_document_kinds=tuple(bundle["required_document_kinds"]),
        required_dependency_kinds=(
            "EVIDENCE_MANIFEST",
            "LEGAL_RULE_BUNDLE",
            "CALCULATION_RUN",
            "FINAL_TEXT_APPROVAL",
        ),
        dependencies=(
            SubmissionDependency(
                "EVIDENCE_MANIFEST",
                bundle["evidence_manifest_id"],
                bundle["evidence_manifest_hash"],
            ),
            SubmissionDependency(
                "LEGAL_RULE_BUNDLE",
                bundle["legal_bundle_id"],
                bundle["legal_bundle_hash"],
            ),
            SubmissionDependency(
                "CALCULATION_RUN",
                bundle["calculation_run_id"],
                bundle["calculation_output_hash"],
            ),
            SubmissionDependency(
                "FINAL_TEXT_APPROVAL",
                bundle["final_text_approval_id"],
                bundle["final_text_hash"],
            ),
        ),
        approved_by=bundle["qa_approved_by"],
        approved_at=datetime.fromisoformat(approved_at),
        approval_hash=bundle["qa_hash"],
    )


def _component(item: dict) -> SubmissionArtifactBinding:
    return SubmissionArtifactBinding(
        component_id=item["work_product_id"],
        sequence=item["sequence"],
        document_kind=item["document_kind"],
        court_filename=item["court_filename"],
        media_type=item["media_type"],
        object_key=item["storage_object_key"],
        artifact_sha256=item["artifact_sha256"],
        byte_size=item["byte_size"],
        approval_hash=item["approval_hash"],
        status=item["work_product_status"],
        audience=item["work_product_audience"],
    )


def _verification_hash(
    *,
    snapshot: PersistentLockedSubmissionCompilation,
    compilation: SubmissionBundleCompilationResult,
    verification: SubmissionBundleVerification,
    encrypted_zip: StoredArtifactObject,
    encrypted_manifest: StoredArtifactObject,
) -> str:
    payload = {
        "schema_version": "submission-export-verification-v1",
        "worker_snapshot_hash": snapshot.snapshot_hash,
        "bundle_id": compilation.bundle_id,
        "input_hash": compilation.input_hash,
        "court_zip_sha256": verification.court_zip_sha256,
        "court_zip_object_key": encrypted_zip.object_key,
        "internal_manifest_sha256": verification.internal_manifest_sha256,
        "internal_manifest_object_key": encrypted_manifest.object_key,
        "component_count": verification.component_count,
        "verified": verification.verified,
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
