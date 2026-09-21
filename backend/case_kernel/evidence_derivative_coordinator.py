"""Recoverable orchestration for locked-Manifest PDF derivatives.

The coordinator converts a repeatable-read evidence snapshot into deterministic
worker input, verifies rendered output, encrypts both PDFs in managed local
storage, then records and verifies each artifact through idempotent PostgreSQL
commands. Plaintext derivatives exist only in a restricted staging directory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .evidence_derivative_worker import (
    ApprovedPageAnnotation,
    DerivativeVerification,
    IncludedManifestPage,
    LockedDerivativeManifest,
    SourcePdfBinding,
    build_evidence_derivatives,
    verify_evidence_derivatives,
)
from .evidence_manifest_postgres import DerivativeRunLease, PersistentEvidenceSnapshot
from .managed_artifact_store import LocalEncryptedArtifactStore, StoredArtifactObject
from .models import Actor, Role


class EvidenceDerivativeCoordinationBlocked(ValueError):
    """The snapshot, worker identity, or recovery inputs are incomplete."""


class EvidenceDerivativePersistencePort(Protocol):
    def register_derivative_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def verify_derivative(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def complete_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class CoordinatedDerivativeArtifact:
    artifact_type: str
    artifact_sha256: str
    object_key: str
    derivative_id: str
    registered_version: int
    verified_version: int
    verification_hash: str


@dataclass(frozen=True)
class EvidenceDerivativeCoordinationResult:
    matter_id: str
    manifest_id: str
    manifest_content_hash: str
    final_matter_version: int
    verification: DerivativeVerification
    artifacts: tuple[CoordinatedDerivativeArtifact, ...]


@dataclass(frozen=True)
class CompletedEvidenceDerivativeRun:
    coordination: EvidenceDerivativeCoordinationResult
    completion_receipt: CaseLedgerCommandReceipt


def coordinate_claimed_evidence_derivative_run(
    *,
    lease: DerivativeRunLease,
    snapshot: PersistentEvidenceSnapshot,
    source_bindings: tuple[SourcePdfBinding, ...],
    case_root: str | Path,
    confirmed_case_root_fingerprint: str,
    staging_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore,
    persistence: EvidenceDerivativePersistencePort,
    system_actor: Actor,
) -> CompletedEvidenceDerivativeRun:
    if (
        lease.matter_id != snapshot.matter_id
        or lease.matter_version != snapshot.version
        or snapshot.locked_manifest is None
        or lease.manifest_id != snapshot.locked_manifest.get("manifest_id")
        or lease.manifest_content_hash != snapshot.locked_manifest.get("content_hash")
    ):
        raise EvidenceDerivativeCoordinationBlocked(
            "claimed derivative run requires a fresh matching post-claim evidence snapshot"
        )
    coordination = coordinate_evidence_derivatives(
        snapshot=snapshot,
        source_bindings=source_bindings,
        case_root=case_root,
        confirmed_case_root_fingerprint=confirmed_case_root_fingerprint,
        staging_root=staging_root,
        artifact_store=artifact_store,
        persistence=persistence,
        system_actor=system_actor,
        idempotency_prefix=f"evidence-run:{lease.run_id}",
    )
    artifacts = {item.artifact_type: item for item in coordination.artifacts}
    if set(artifacts) != {"RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"}:
        raise EvidenceDerivativeCoordinationBlocked("derivative run did not produce both required artifact types")
    completion = persistence.complete_derivative_run(
        matter_id=lease.matter_id,
        run_id=lease.run_id,
        lease_id=lease.lease_id,
        related_derivative_id=artifacts["RELATED_PAGES_PDF"].derivative_id,
        annotated_derivative_id=artifacts["ANNOTATED_RELATED_PAGES_PDF"].derivative_id,
        actor=system_actor,
        expected_version=coordination.final_matter_version,
        idempotency_key=f"evidence-run:{lease.run_id}:complete",
    )
    return CompletedEvidenceDerivativeRun(
        coordination=coordination,
        completion_receipt=completion,
    )


def coordinate_evidence_derivatives(
    *,
    snapshot: PersistentEvidenceSnapshot,
    source_bindings: tuple[SourcePdfBinding, ...],
    case_root: str | Path,
    confirmed_case_root_fingerprint: str,
    staging_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore,
    persistence: EvidenceDerivativePersistencePort,
    system_actor: Actor,
    idempotency_prefix: str,
) -> EvidenceDerivativeCoordinationResult:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise EvidenceDerivativeCoordinationBlocked("derivative coordination requires a dedicated SYSTEM_WORKER identity")
    if not idempotency_prefix.strip() or len(idempotency_prefix) > 120:
        raise EvidenceDerivativeCoordinationBlocked("derivative coordination idempotency prefix is required")
    if snapshot.locked_manifest is None:
        raise EvidenceDerivativeCoordinationBlocked("a current locked evidence Manifest is required")
    if snapshot.matter_id.strip() == "" or snapshot.version < 1:
        raise EvidenceDerivativeCoordinationBlocked("evidence snapshot matter identity and version are required")
    manifest = _locked_manifest_from_snapshot(snapshot)
    original_hashes = {
        item["evidence_file_id"]: item["original_file_sha256"] for item in snapshot.original_files
    }
    for binding in source_bindings:
        if original_hashes.get(binding.evidence_file_id) != binding.expected_sha256:
            raise EvidenceDerivativeCoordinationBlocked(
                "source binding hash is not the original hash in the evidence snapshot"
            )

    staging = Path(staging_root).expanduser()
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    staging = staging.resolve(strict=True)
    staging.chmod(0o700)
    original_root = Path(case_root).expanduser().resolve(strict=True)
    if staging == original_root or staging.is_relative_to(original_root):
        raise EvidenceDerivativeCoordinationBlocked("plaintext staging must not be inside the original case folder")
    artifact_store.assert_separate_from_case_root(original_root)

    with TemporaryDirectory(prefix="evidence-derivative-run-", dir=staging) as temporary:
        output = Path(temporary) / "output"
        build = build_evidence_derivatives(
            manifest,
            source_bindings,
            case_root=original_root,
            confirmed_case_root_fingerprint=confirmed_case_root_fingerprint,
            output_directory=output,
        )
        verification = verify_evidence_derivatives(build, manifest)
        encrypted = (
            (
                build.related_pages.artifact_type,
                build.related_pages.sha256,
                build.related_pages.page_count,
                artifact_store.put_file(
                    build.related_pages.path,
                    expected_sha256=build.related_pages.sha256,
                    case_root=original_root,
                ),
            ),
            (
                build.annotated_pages.artifact_type,
                build.annotated_pages.sha256,
                build.annotated_pages.page_count,
                artifact_store.put_file(
                    build.annotated_pages.path,
                    expected_sha256=build.annotated_pages.sha256,
                    case_root=original_root,
                ),
            ),
        )

    current_version = snapshot.version
    coordinated: list[CoordinatedDerivativeArtifact] = []
    for artifact_type, artifact_hash, page_count, stored in encrypted:
        verification_hash = _verification_hash(
            verification,
            manifest_id=manifest.manifest_id,
            manifest_content_hash=manifest.content_hash,
            artifact_type=artifact_type,
            artifact_sha256=artifact_hash,
            stored=stored,
        )
        slug = "related" if artifact_type == "RELATED_PAGES_PDF" else "annotated"
        registered = persistence.register_derivative_candidate(
            matter_id=snapshot.matter_id,
            manifest_id=manifest.manifest_id,
            actor=system_actor,
            expected_version=current_version,
            idempotency_key=f"{idempotency_prefix}:{slug}:register",
            manifest_content_hash=manifest.content_hash,
            artifact_type=artifact_type,
            storage_object_key=stored.object_key,
            artifact_sha256=artifact_hash,
            page_count=page_count,
        )
        verified = persistence.verify_derivative(
            matter_id=snapshot.matter_id,
            derivative_id=registered.object_id,
            actor=system_actor,
            expected_version=registered.matter_version,
            idempotency_key=f"{idempotency_prefix}:{slug}:verify",
            verification_hash=verification_hash,
        )
        coordinated.append(
            CoordinatedDerivativeArtifact(
                artifact_type=artifact_type,
                artifact_sha256=artifact_hash,
                object_key=stored.object_key,
                derivative_id=registered.object_id,
                registered_version=registered.matter_version,
                verified_version=verified.matter_version,
                verification_hash=verification_hash,
            )
        )
        current_version = verified.matter_version

    return EvidenceDerivativeCoordinationResult(
        matter_id=snapshot.matter_id,
        manifest_id=manifest.manifest_id,
        manifest_content_hash=manifest.content_hash,
        final_matter_version=current_version,
        verification=verification,
        artifacts=tuple(coordinated),
    )


def _locked_manifest_from_snapshot(snapshot: PersistentEvidenceSnapshot) -> LockedDerivativeManifest:
    locked = snapshot.locked_manifest
    if locked is None or locked.get("status") != "LOCKED":
        raise EvidenceDerivativeCoordinationBlocked("evidence snapshot does not contain a current locked Manifest")
    pages_by_id = {item["evidence_page_id"]: item for item in snapshot.pages}
    entries = sorted(
        (item for item in locked["entries"] if item["disposition"] == "INCLUDE"),
        key=lambda item: item["derivative_sequence"],
    )
    included_pages: list[IncludedManifestPage] = []
    for entry in entries:
        page = pages_by_id.get(entry["evidence_page_id"])
        if page is None or page.get("decision") is None:
            raise EvidenceDerivativeCoordinationBlocked("locked Manifest page is missing from the evidence snapshot")
        if page["decision"]["decision_id"] != entry["decision_id"]:
            raise EvidenceDerivativeCoordinationBlocked("locked Manifest decision differs from the current page snapshot")
        annotations = tuple(
            ApprovedPageAnnotation(
                annotation_id=item["annotation_id"],
                x0=float(item["x0"]),
                y0=float(item["y0"]),
                x1=float(item["x1"]),
                y1=float(item["y1"]),
                label=item["label"],
            )
            for item in page["annotations"]
            if item["status"] == "APPROVED"
        )
        included_pages.append(
            IncludedManifestPage(
                evidence_page_id=page["evidence_page_id"],
                evidence_file_id=page["evidence_file_id"],
                source_page_number=page["page_number"],
                derivative_sequence=entry["derivative_sequence"],
                annotations=annotations,
            )
        )
    if len(included_pages) != locked["included_pages"]:
        raise EvidenceDerivativeCoordinationBlocked("locked Manifest included-page count is inconsistent")
    return LockedDerivativeManifest(
        manifest_id=locked["manifest_id"],
        content_hash=locked["content_hash"],
        status=locked["status"],
        pages=tuple(included_pages),
    )


def _verification_hash(
    verification: DerivativeVerification,
    *,
    manifest_id: str,
    manifest_content_hash: str,
    artifact_type: str,
    artifact_sha256: str,
    stored: StoredArtifactObject,
) -> str:
    payload = {
        "schema_version": "evidence-derivative-verification-v1",
        "manifest_id": manifest_id,
        "manifest_content_hash": manifest_content_hash,
        "artifact_type": artifact_type,
        "artifact_sha256": artifact_sha256,
        "object_key": stored.object_key,
        "key_id": stored.key_id,
        "verification": asdict(verification),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
