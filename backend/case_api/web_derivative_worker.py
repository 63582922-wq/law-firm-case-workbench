"""Server-owned worker for locked Web evidence PDF derivatives.

The worker never accepts browser paths or page selections.  It reads only the
current locked Manifest, obtains SYSTEM_WORKER-only object locators, and keeps
all materialized PDFs and output files below one private worker root.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Protocol
from uuid import UUID

from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_derivative_worker import (
    ApprovedPageAnnotation,
    DerivativeArtifact,
    DerivativeBuildResult,
    IncludedManifestPage,
    LockedDerivativeManifest,
    VerifiedMaterializedPdfSource,
    build_evidence_derivatives_from_materialized_sources,
    verify_evidence_derivatives,
)
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore


class WebDerivativeWorkerBlocked(RuntimeError):
    """A derivative run cannot be completed without violating an invariant."""


class _EvidenceStore(Protocol):
    def claim_derivative_run(self, **kwargs): ...
    def get_evidence_snapshot(self, **kwargs): ...
    def get_web_uploaded_original_source_locator(self, **kwargs): ...
    def register_derivative_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...
    def verify_derivative(self, **kwargs) -> CaseLedgerCommandReceipt: ...
    def complete_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...
    def fail_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class WebEvidenceDerivativeWorker:
    evidence_store: _EvidenceStore
    object_store: S3CompatiblePrivateObjectStore
    worker_root: Path
    system_worker_for_firm: Callable[[str], Actor]

    def __post_init__(self) -> None:
        if not isinstance(self.worker_root, Path) or not self.worker_root.is_absolute():
            raise ValueError("Web derivative worker root must be absolute")
        if not callable(self.system_worker_for_firm):
            raise ValueError("Web derivative worker identity resolver is invalid")
        for method in (
            "claim_derivative_run",
            "get_evidence_snapshot",
            "get_web_uploaded_original_source_locator",
            "register_derivative_candidate",
            "verify_derivative",
            "complete_derivative_run",
            "fail_derivative_run",
        ):
            if not callable(getattr(self.evidence_store, method, None)):
                raise ValueError("Web derivative evidence store is invalid")
        if not callable(getattr(self.object_store, "materialize_verified_pdf", None)) or not callable(
            getattr(self.object_store, "put_verified_derivative", None)
        ):
            raise ValueError("Web derivative object store is invalid")

    def run(self, *, firm_id: str, matter_id: str, run_id: str, expected_version: int) -> None:
        """Process one server-queued run.

        ``firm_id`` is taken from the already authenticated API actor when the
        queue item is created.  It is an internal server envelope value; it is
        never accepted from the browser and is required here so a worker
        cannot guess tenant scope from a matter identifier.
        """
        worker: Actor | None = None
        lease = None
        current_version = expected_version
        try:
            _validate_uuid("firm_id", firm_id)
            worker = self.system_worker_for_firm(firm_id)
            if worker.firm_id != firm_id:
                raise WebDerivativeWorkerBlocked("system worker identity is outside the queued firm")
            if Role.SYSTEM_WORKER not in worker.roles:
                raise WebDerivativeWorkerBlocked("system worker identity is invalid")
            lease = self.evidence_store.claim_derivative_run(
                matter_id=matter_id,
                run_id=run_id,
                actor=worker,
                expected_version=expected_version,
                idempotency_key=f"web-derivative-claim-{run_id}-{expected_version}",
            )
            current_version = lease.matter_version
            snapshot = self.evidence_store.get_evidence_snapshot(matter_id=matter_id, actor=worker)
            manifest = _locked_manifest(snapshot)
            if manifest.manifest_id != lease.manifest_id or manifest.content_hash != lease.manifest_content_hash:
                raise WebDerivativeWorkerBlocked("locked Manifest changed before worker materialization")
            source_file_ids = tuple(dict.fromkeys(page.evidence_file_id for page in manifest.pages))
            original_by_id = {
                str(item["evidence_file_id"]): item for item in snapshot.original_files
            }
            with TemporaryDirectory(prefix=f"web-derivative-{run_id}-", dir=str(self.worker_root)) as temporary:
                root = Path(temporary)
                materialized: list[VerifiedMaterializedPdfSource] = []
                for index, file_id in enumerate(source_file_ids, start=1):
                    original = original_by_id.get(file_id)
                    if not isinstance(original, dict):
                        raise WebDerivativeWorkerBlocked("Manifest source original is missing")
                    locator = self.evidence_store.get_web_uploaded_original_source_locator(
                        matter_id=matter_id,
                        evidence_file_id=file_id,
                        actor=worker,
                    )
                    path = root / f"source-{index}.pdf"
                    self.object_store.materialize_verified_pdf(locator.stored_object(), destination=path)
                    materialized.append(
                        VerifiedMaterializedPdfSource(
                            evidence_file_id=file_id,
                            source_path=path,
                            expected_sha256=str(original["original_file_sha256"]),
                            expected_page_count=int(original["page_count"]),
                            source_reference_hash=locator.source_reference_hash,
                        )
                    )
                output = root / "output"
                result = build_evidence_derivatives_from_materialized_sources(
                    manifest,
                    tuple(materialized),
                    output_directory=output,
                )
                # Re-run the existing structural + rendered-page verifier
                # before anything is uploaded or registered.  This catches a
                # malformed PDF or missing red-box overlay inside the worker,
                # not after a browser has been told that generation succeeded.
                verification_build = DerivativeBuildResult(
                    manifest_id=result.manifest_id,
                    manifest_content_hash=result.manifest_content_hash,
                    related_pages=DerivativeArtifact(
                        artifact_type=result.related_pages.artifact_type,
                        path=output / result.related_pages.file_name,
                        sha256=result.related_pages.sha256,
                        page_count=result.related_pages.page_count,
                    ),
                    annotated_pages=DerivativeArtifact(
                        artifact_type=result.annotated_pages.artifact_type,
                        path=output / result.annotated_pages.file_name,
                        sha256=result.annotated_pages.sha256,
                        page_count=result.annotated_pages.page_count,
                    ),
                    lineage_path=output / result.lineage_file_name,
                    lineage_sha256=result.lineage_sha256,
                )
                verification = verify_evidence_derivatives(verification_build, manifest)
                if not verification.verified:
                    raise WebDerivativeWorkerBlocked("generated evidence PDFs failed worker verification")
                artifact_specs = (
                    (result.related_pages, "RELATED_PAGES_PDF"),
                    (result.annotated_pages, "ANNOTATED_RELATED_PAGES_PDF"),
                )
                derivative_ids: dict[str, str] = {}
                for artifact, artifact_type in artifact_specs:
                    artifact_path = output / artifact.file_name
                    stored = self.object_store.put_verified_derivative(
                        artifact_path,
                        firm_id=worker.firm_id,
                        matter_id=matter_id,
                        artifact_type=artifact_type,
                        artifact_sha256=artifact.sha256,
                        page_count=artifact.page_count,
                    )
                    registered = self.evidence_store.register_derivative_candidate(
                        matter_id=matter_id,
                        manifest_id=manifest.manifest_id,
                        actor=worker,
                        expected_version=current_version,
                        idempotency_key=f"web-derivative-register-{run_id}-{artifact_type}",
                        manifest_content_hash=manifest.content_hash,
                        artifact_type=artifact_type,
                        storage_object_key=stored.object_key,
                        artifact_sha256=artifact.sha256,
                        page_count=artifact.page_count,
                    )
                    current_version = registered.matter_version
                    verification_hash = sha256(
                        f"web-derivative-verification-v1:{manifest.content_hash}:{artifact.sha256}".encode()
                    ).hexdigest()
                    verified = self.evidence_store.verify_derivative(
                        matter_id=matter_id,
                        derivative_id=registered.object_id,
                        actor=worker,
                        expected_version=current_version,
                        idempotency_key=f"web-derivative-verify-{run_id}-{artifact_type}",
                        verification_hash=verification_hash,
                    )
                    current_version = verified.matter_version
                    derivative_ids[artifact_type] = registered.object_id
                self.evidence_store.complete_derivative_run(
                    matter_id=matter_id,
                    run_id=run_id,
                    lease_id=lease.lease_id,
                    related_derivative_id=derivative_ids["RELATED_PAGES_PDF"],
                    annotated_derivative_id=derivative_ids["ANNOTATED_RELATED_PAGES_PDF"],
                    actor=worker,
                    expected_version=current_version,
                    idempotency_key=f"web-derivative-complete-{run_id}",
                )
        except Exception:
            if worker is not None and lease is not None:
                try:
                    self.evidence_store.fail_derivative_run(
                        matter_id=matter_id,
                        run_id=run_id,
                        lease_id=lease.lease_id,
                        failure_code="WEB_DERIVATIVE_BUILD_FAILED",
                        actor=worker,
                        expected_version=current_version,
                        idempotency_key=f"web-derivative-fail-{run_id}-{current_version}",
                    )
                except Exception:
                    # The run remains visible as RUNNING until its lease expires;
                    # the next controlled claim is the recovery path.
                    pass


def _locked_manifest(snapshot: object) -> LockedDerivativeManifest:
    locked = getattr(snapshot, "locked_manifest", None)
    pages = getattr(snapshot, "pages", ())
    originals = getattr(snapshot, "original_files", ())
    if not isinstance(locked, dict) or locked.get("status") != "LOCKED":
        raise WebDerivativeWorkerBlocked("current evidence Manifest is not locked")
    manifest_id = str(locked.get("manifest_id", ""))
    content_hash = str(locked.get("content_hash", ""))
    entries: list[IncludedManifestPage] = []
    original_by_id = {str(item["evidence_file_id"]): item for item in originals if isinstance(item, dict)}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("decision"), dict):
            continue
        decision = page["decision"]
        if decision.get("disposition") != "INCLUDE":
            continue
        file_id = str(page.get("evidence_file_id", ""))
        if file_id not in original_by_id:
            raise WebDerivativeWorkerBlocked("included Manifest page has no original")
        annotations = tuple(
            ApprovedPageAnnotation(
                annotation_id=str(item["annotation_id"]),
                x0=float(item["x0"]),
                y0=float(item["y0"]),
                x1=float(item["x1"]),
                y1=float(item["y1"]),
                label=str(item["label"]),
            )
            for item in page.get("annotations", ())
            if isinstance(item, dict) and item.get("status") == "APPROVED"
        )
        entries.append(
            IncludedManifestPage(
                evidence_page_id=str(page["evidence_page_id"]),
                evidence_file_id=file_id,
                source_page_number=int(page["page_number"]),
                derivative_sequence=int(page["page_number"]),
                annotations=annotations,
            )
        )
    entries.sort(key=lambda item: (item.source_page_number, item.evidence_page_id))
    entries = [
        IncludedManifestPage(
            evidence_page_id=entry.evidence_page_id,
            evidence_file_id=entry.evidence_file_id,
            source_page_number=entry.source_page_number,
            derivative_sequence=index,
            annotations=entry.annotations,
        )
        for index, entry in enumerate(entries, start=1)
    ]
    if not manifest_id or not content_hash or not entries:
        raise WebDerivativeWorkerBlocked("locked Manifest has no included pages")
    return LockedDerivativeManifest(manifest_id=manifest_id, content_hash=content_hash, status="LOCKED", pages=tuple(entries))


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise WebDerivativeWorkerBlocked(f"{label} is invalid") from error
