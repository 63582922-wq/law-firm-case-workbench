"""PostgreSQL boundary for immutable Agent document-review packages.

The execution Worker stages one canonical structured candidate together with
its editable DOCX/XLSX and the isolated PDF preview.  PostgreSQL stores only
hashes and private object locators.  It does not update the matter ledger,
approve the candidate, promote it to a formal work product or submit anything.

An independent verifier reads all three objects as one package.  Reading one
artifact therefore cannot accidentally validate an Office file or PDF outside
the structured candidate, dynamic work-plan, task and render lineage that
created it.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import re
from typing import Any, Iterator, Mapping, Protocol
from uuid import UUID, uuid5
from zipfile import BadZipFile, ZipFile

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pypdf import PdfReader

from .case_agent_document_delivery import (
    AuthoritativeDocumentSource,
    PAYMENT_LEDGER_COLUMNS,
    PAYMENT_LEDGER_SOURCE_KEYS,
    DocumentSourceKind,
    ReviewableDocumentFormat,
    ReviewableDocumentTemplate,
    ReviewableDocumentTemplateRegistry,
    build_deterministic_case_review_memo_candidate,
    build_deterministic_payment_ledger_candidate,
    build_deterministic_supplementary_evidence_checklist_candidate,
    first_release_reviewable_document_templates,
)
from .case_agent_lawyer_analysis import LAWYER_DECISION_PACKAGE_ARTIFACT_KIND
from .case_work_plan_postgres import assert_case_work_plan_references_current
from .case_agent_document_adapters import (
    ReviewableDocumentPackageStaging,
    StagedDocumentPackage,
)
from .case_agent_supervisor import ArtifactReceipt
from .case_agent_verifier import (
    ArtifactVerificationRejected,
    CaseAgentVerificationIndeterminate,
    ManagedArtifactRead,
)
from .models import Actor, Role


class CaseAgentDocumentPackageBlocked(RuntimeError):
    """A document package is stale, cross-tenant or not independently bound."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,119}$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_OBJECT_KEY = re.compile(
    r"^case-agent-document-packages/v1/"
    r"(?P<firm>[0-9a-f-]{36})/(?P<matter>[0-9a-f-]{36})/"
    r"(?P<package>[0-9a-f-]{36})/"
    r"(?P<role>candidate|editable|pdf-preview)/"
    r"(?P<digest>[0-9a-f]{64})\.lca$"
)
_ARTIFACT_KINDS = (
    "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
    "REVIEWABLE_DOCUMENT_EDITABLE",
    "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
)
_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_JSON_MEDIA = "application/json"
_PDF_MEDIA = "application/pdf"
_CANDIDATE_LIMIT = 4 * 1024 * 1024
_EDITABLE_LIMIT = 64 * 1024 * 1024
_PDF_LIMIT = 128 * 1024 * 1024
_INITIAL_GENERATION_MODE = "INITIAL_AGENT_TASK"
_REVISION_GENERATION_MODE = "DETERMINISTIC_TEMPLATE_REVISION"
_CONTENT_REVISION_GENERATION_MODE = "LAWYER_CONTENT_REVISION"
_REVISION_GENERATION_MODES = frozenset({_REVISION_GENERATION_MODE, _CONTENT_REVISION_GENERATION_MODE})

_DOCUMENT_DELIVERY_REQUIRED_COLUMNS = frozenset(
    {
        "package_id",
        "idempotency_key",
        "run_id",
        "graph_id",
        "task_id",
        "attempt_id",
        "firm_id",
        "matter_id",
        "task_input_hash",
        "case_snapshot_hash",
        "binding_hash",
        "source_set_hash",
        "authorized_source_refs",
        "authorized_source_refs_hash",
        "authorized_source_manifest",
        "candidate_hash",
        "work_plan_id",
        "work_plan_hash",
        "work_plan_item_id",
        "posture_profile_id",
        "posture_profile_hash",
        "template_id",
        "template_version",
        "template_hash",
        "deliverable_kind",
        "output_format",
        "review_status",
        "candidate_artifact_id",
        "candidate_artifact_kind",
        "candidate_media_type",
        "candidate_content_sha256",
        "candidate_byte_size",
        "candidate_object_key",
        "candidate_object_version_id",
        "editable_artifact_id",
        "editable_artifact_kind",
        "editable_media_type",
        "editable_sha256",
        "editable_byte_size",
        "editable_object_key",
        "editable_object_version_id",
        "review_pdf_artifact_id",
        "review_pdf_artifact_kind",
        "review_pdf_media_type",
        "review_pdf_sha256",
        "review_pdf_byte_size",
        "review_pdf_page_count",
        "review_pdf_object_key",
        "review_pdf_object_version_id",
        "render_verification_hash",
        "review_input_hash",
        "package_receipt_hash",
        "staged_by",
        "created_at",
        "generation_mode",
        "revision_number",
        "root_package_id",
        "supersedes_package_id",
        "revision_request_id",
        "requested_by",
    }
)
_DOCUMENT_DELIVERY_REQUIRED_TRIGGERS = frozenset(
    {
        "case_agent_reviewable_document_packages_initial_insert_guard",
        "case_agent_reviewable_document_packages_revision_insert_guard",
        "case_agent_reviewable_document_packages_append_only",
    }
)


@dataclass(frozen=True)
class PrivateDocumentObjectReceipt:
    """Server-only content-addressed object receipt."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    media_type: str
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AuthorizedDocumentSourceBinding:
    """Persistable, non-text projection of one server-expanded draft source.

    The model-visible source text is deliberately not copied into PostgreSQL.
    Its digest, authoritative object hash and source version are sufficient to
    bind the immutable package to the exact server projection that was sent.
    """

    input_ref: str
    source_kind: str
    source_version: str
    source_hash: str
    label: str
    text_sha256: str


class ReviewableDocumentPrivateObjectStore(Protocol):
    """Injected private storage; no path, URL or browser grant is accepted."""

    def put_reviewable_document_object(
        self,
        content: bytes,
        *,
        firm_id: str,
        matter_id: str,
        package_id: str,
        object_role: str,
        content_sha256: str,
        media_type: str,
    ) -> PrivateDocumentObjectReceipt: ...

    def read_reviewable_document_object(
        self, receipt: PrivateDocumentObjectReceipt
    ) -> bytes: ...


class ReviewableDocumentS3Client(Protocol):
    """Minimal injected S3 surface; no existing store internals are accessed."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ReviewableDocumentS3Config:
    bucket: str
    server_side_encryption: str = "AES256"
    kms_key_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, str) or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?", self.bucket
        ) is None:
            raise ValueError("document package bucket is invalid")
        if self.server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("document package encryption mode is invalid")
        if self.server_side_encryption == "aws:kms":
            if (
                not isinstance(self.kms_key_id, str)
                or not self.kms_key_id.strip()
                or len(self.kms_key_id) > 512
            ):
                raise ValueError("document package KMS key is required")
        elif self.kms_key_id is not None:
            raise ValueError("document package KMS key requires aws:kms")


class S3ReviewableDocumentPrivateObjectStore:
    """Private content-addressed S3 bridge for the exact 0039 object contract."""

    def __init__(
        self, *, config: ReviewableDocumentS3Config, client: ReviewableDocumentS3Client
    ) -> None:
        if not isinstance(config, ReviewableDocumentS3Config):
            raise ValueError("document package S3 config is required")
        for method in ("put_object", "head_object", "get_object", "delete_object"):
            if not callable(getattr(client, method, None)):
                raise ValueError("document package S3 client is invalid")
        self._config = config
        self._client = client

    def put_reviewable_document_object(
        self,
        content: bytes,
        *,
        firm_id: str,
        matter_id: str,
        package_id: str,
        object_role: str,
        content_sha256: str,
        media_type: str,
    ) -> PrivateDocumentObjectReceipt:
        for value, label in (
            (firm_id, "firm_id"),
            (matter_id, "matter_id"),
            (package_id, "package_id"),
        ):
            _uuid(value, label)
        if object_role not in {"candidate", "editable", "pdf-preview"}:
            raise CaseAgentDocumentPackageBlocked("document object role is invalid")
        _sha(content_sha256, "document object hash")
        if (
            not isinstance(content, bytes)
            or not content
            or sha256(content).hexdigest() != content_sha256
            or media_type not in {_JSON_MEDIA, _DOCX_MEDIA, _XLSX_MEDIA, _PDF_MEDIA}
        ):
            raise CaseAgentDocumentPackageBlocked("document object bytes are invalid")
        object_key = (
            f"case-agent-document-packages/v1/{firm_id}/{matter_id}/"
            f"{package_id}/{object_role}/{content_sha256}.lca"
        )
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        metadata = {
            "lawcase-document-package-id": package_id,
            "lawcase-document-role": object_role,
            "lawcase-document-sha256": content_sha256,
            "lawcase-document-bytes": str(len(content)),
            "lawcase-document-media-type": media_type,
        }
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "Body": content,
            "ContentLength": len(content),
            "ContentType": media_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": metadata,
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            response = self._client.put_object(**request)
            head = self._client.head_object(
                Bucket=self._config.bucket, Key=object_key
            )
            _validate_s3_document_head(
                head,
                expected_size=len(content),
                expected_checksum=checksum,
                expected_media_type=media_type,
                expected_metadata=metadata,
            )
        except CaseAgentDocumentPackageBlocked:
            raise
        except Exception as error:
            raise CaseAgentDocumentPackageBlocked(
                "private document object could not be stored or verified"
            ) from error
        version = response.get("VersionId") if isinstance(response, Mapping) else None
        if version is not None and (
            not isinstance(version, str) or not version.strip() or len(version) > 512
        ):
            raise CaseAgentDocumentPackageBlocked(
                "private document object version is invalid"
            )
        return PrivateDocumentObjectReceipt(
            object_key=object_key,
            content_sha256=content_sha256,
            byte_size=len(content),
            media_type=media_type,
            object_version_id=version,
        )

    def read_reviewable_document_object(
        self, receipt: PrivateDocumentObjectReceipt
    ) -> bytes:
        receipt = _coerce_object_receipt(receipt)
        match = _OBJECT_KEY.fullmatch(receipt.object_key)
        if match is None or match.group("digest") != receipt.content_sha256:
            raise CaseAgentDocumentPackageBlocked(
                "private document object receipt is invalid"
            )
        checksum = base64.b64encode(
            bytes.fromhex(receipt.content_sha256)
        ).decode("ascii")
        metadata = {
            "lawcase-document-package-id": match.group("package"),
            "lawcase-document-role": match.group("role"),
            "lawcase-document-sha256": receipt.content_sha256,
            "lawcase-document-bytes": str(receipt.byte_size),
            "lawcase-document-media-type": receipt.media_type,
        }
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": receipt.object_key,
        }
        if receipt.object_version_id is not None:
            request["VersionId"] = receipt.object_version_id
        try:
            head = self._client.head_object(**request)
            _validate_s3_document_head(
                head,
                expected_size=receipt.byte_size,
                expected_checksum=checksum,
                expected_media_type=receipt.media_type,
                expected_metadata=metadata,
            )
            response = self._client.get_object(**request)
            body = response.get("Body") if isinstance(response, Mapping) else None
            if callable(getattr(body, "read", None)):
                content = body.read(receipt.byte_size + 1)
            elif isinstance(body, (bytes, bytearray)):
                content = bytes(body)
            else:
                raise CaseAgentDocumentPackageBlocked(
                    "private document object body is unavailable"
                )
            _require_exact_bytes(
                content,
                expected_hash=receipt.content_sha256,
                expected_size=receipt.byte_size,
                label="private document object",
            )
            return content
        except CaseAgentDocumentPackageBlocked:
            raise
        except Exception as error:
            raise CaseAgentDocumentPackageBlocked(
                "private document object could not be authenticated"
            ) from error

    def _delete_best_effort(self, object_key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._config.bucket, Key=object_key)
        except Exception:
            return


@dataclass(frozen=True)
class ReviewableDocumentPackageStagingRequest:
    idempotency_key: str
    run_id: str
    graph_id: str
    task_id: str
    attempt_id: str
    task_input_hash: str
    case_snapshot_hash: str
    binding_hash: str
    source_set_hash: str
    authorized_source_refs: tuple[str, ...]
    authorized_source_manifest: tuple[AuthorizedDocumentSourceBinding, ...]
    candidate_hash: str
    work_plan_id: str
    work_plan_hash: str
    work_plan_item_id: str
    posture_profile_id: str
    posture_profile_hash: str
    template_id: str
    template_version: str
    template_hash: str
    deliverable_kind: str
    output_format: ReviewableDocumentFormat
    candidate_bytes: bytes = field(repr=False)
    candidate_content_sha256: str = ""
    editable_bytes: bytes = field(default=b"", repr=False)
    editable_sha256: str = ""
    editable_media_type: str = ""
    review_pdf_bytes: bytes = field(default=b"", repr=False)
    review_pdf_sha256: str = ""
    review_pdf_page_count: int = 0
    render_verification_hash: str = ""
    review_input_hash: str = ""
    generation_mode: str = _INITIAL_GENERATION_MODE
    revision_number: int = 1
    root_package_id: str | None = None
    supersedes_package_id: str | None = None
    revision_request_id: str | None = None
    requested_by: str | None = None
    content_generation_claim_version: int | None = None


@dataclass(frozen=True)
class StagedReviewableDocumentPackage:
    package_id: str
    candidate_artifact: ArtifactReceipt
    editable_artifact: ArtifactReceipt
    review_pdf_artifact: ArtifactReceipt
    receipt_hash: str
    review_status: str = "NEEDS_LAWYER_REVIEW"
    generation_mode: str = _INITIAL_GENERATION_MODE
    revision_number: int = 1
    root_package_id: str | None = None
    supersedes_package_id: str | None = None
    revision_request_id: str | None = None

    @property
    def artifacts(self) -> tuple[ArtifactReceipt, ArtifactReceipt, ArtifactReceipt]:
        return (
            self.candidate_artifact,
            self.editable_artifact,
            self.review_pdf_artifact,
        )


@dataclass(frozen=True)
class ReviewableDocumentArtifactRead:
    artifact_id: str
    artifact_kind: str
    media_type: str
    content_sha256: str
    byte_size: int
    content: bytes = field(repr=False)

    def as_artifact_receipt(self, *, source_input_hash: str) -> ArtifactReceipt:
        return ArtifactReceipt(
            artifact_id=self.artifact_id,
            artifact_kind=self.artifact_kind,
            content_hash=self.content_sha256,
            byte_size=self.byte_size,
            source_input_hash=source_input_hash,
            managed_derivative=True,
        )


@dataclass(frozen=True)
class ReviewableDocumentPackageRead:
    package_id: str
    run_id: str
    graph_id: str
    task_id: str
    task_input_hash: str
    case_snapshot_hash: str
    binding_hash: str
    source_set_hash: str
    authorized_source_refs: tuple[str, ...]
    authorized_source_refs_hash: str
    authorized_source_manifest: tuple[AuthorizedDocumentSourceBinding, ...]
    candidate_hash: str
    work_plan_id: str
    work_plan_hash: str
    work_plan_item_id: str
    posture_profile_id: str
    posture_profile_hash: str
    template_id: str
    template_version: str
    template_hash: str
    deliverable_kind: str
    output_format: ReviewableDocumentFormat
    review_input_hash: str
    render_verification_hash: str
    receipt_hash: str
    candidate: ReviewableDocumentArtifactRead
    editable: ReviewableDocumentArtifactRead
    review_pdf: ReviewableDocumentArtifactRead
    selected_artifact_id: str
    review_pdf_page_count: int
    review_status: str = "NEEDS_LAWYER_REVIEW"
    generation_mode: str = _INITIAL_GENERATION_MODE
    revision_number: int = 1
    root_package_id: str | None = None
    supersedes_package_id: str | None = None
    revision_request_id: str | None = None
    requested_by: str | None = None
    content_generation_claim_version: int | None = None

    @property
    def selected_artifact(self) -> ReviewableDocumentArtifactRead:
        for item in (self.candidate, self.editable, self.review_pdf):
            if item.artifact_id == self.selected_artifact_id:
                return item
        raise CaseAgentDocumentPackageBlocked("selected document artifact is unavailable")


def reviewable_document_template_hash(template: ReviewableDocumentTemplate) -> str:
    """Hash every server-controlled template meaning, not only its version."""

    template.validate()
    return template.template_hash


class PostgresReviewableDocumentPackageStore:
    """Stage one exact package without touching the formal matter version."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: ReviewableDocumentPrivateObjectStore,
        template_registry: ReviewableDocumentTemplateRegistry | None = None,
        enable_content_revisions: bool = False,
    ) -> None:
        _dsn(dsn)
        _dedicated_worker(worker_actor, "document staging")
        _object_store(object_store)
        if type(enable_content_revisions) is not bool:
            raise ValueError("content revision registration flag must be boolean")
        self._content_revisions_enabled = enable_content_revisions
        self._dsn = dsn.strip()
        self._worker = worker_actor
        self._objects = object_store
        self._templates = template_registry or first_release_reviewable_document_templates()

    def stage_package(
        self, request: ReviewableDocumentPackageStagingRequest
    ) -> StagedReviewableDocumentPackage:
        normalized = _validate_staging_request(request, self._templates)
        if request.generation_mode == _CONTENT_REVISION_GENERATION_MODE and not self._content_revisions_enabled:
            # Keep production writes disabled until migration and runtime
            # qualification explicitly enable the complete content path.
            raise CaseAgentDocumentPackageBlocked("content revision package registration is not enabled")
        package_id = str(uuid5(UUID(request.task_id), request.idempotency_key))
        artifact_ids = _artifact_ids(request.task_id, package_id)

        with _transaction(
            self._dsn, self._worker, read_only=True, repeatable_read=True
        ) as connection:
            binding = _read_staging_binding(
                connection, worker=self._worker, request=request
            )
            prior = _read_package_row(
                connection,
                firm_id=self._worker.firm_id,
                idempotency_key=request.idempotency_key,
                graph_id=request.graph_id,
                task_id=request.task_id,
                generation_mode=request.generation_mode,
                revision_request_id=request.revision_request_id,
            )
        if _binding_hash(
            request,
            firm_id=self._worker.firm_id,
            matter_id=str(binding["matter_id"]),
        ) != request.binding_hash:
            raise CaseAgentDocumentPackageBlocked(
                "binding_hash differs from the current server task coordinates"
            )
        if not _candidate_source_refs(normalized["candidate"]).issubset(
            set(request.authorized_source_refs)
        ):
            raise CaseAgentDocumentPackageBlocked(
                "document candidate cites a source outside the current task"
            )
        if prior is not None:
            return self._verified_prior(prior, request=request, normalized=normalized)

        receipts = {
            "candidate": self._put(
                request.candidate_bytes,
                firm_id=self._worker.firm_id,
                matter_id=binding["matter_id"],
                package_id=package_id,
                object_role="candidate",
                content_sha256=request.candidate_content_sha256,
                media_type=_JSON_MEDIA,
            ),
            "editable": self._put(
                request.editable_bytes,
                firm_id=self._worker.firm_id,
                matter_id=binding["matter_id"],
                package_id=package_id,
                object_role="editable",
                content_sha256=request.editable_sha256,
                media_type=request.editable_media_type,
            ),
            "pdf-preview": self._put(
                request.review_pdf_bytes,
                firm_id=self._worker.firm_id,
                matter_id=binding["matter_id"],
                package_id=package_id,
                object_role="pdf-preview",
                content_sha256=request.review_pdf_sha256,
                media_type=_PDF_MEDIA,
            ),
        }
        template_hash = reviewable_document_template_hash(normalized["template"])
        package_receipt_hash = _package_receipt_hash(
            request=request,
            package_id=package_id,
            artifact_ids=artifact_ids,
            template_hash=template_hash,
            object_receipts=receipts,
        )
        try:
            with _transaction(self._dsn, self._worker, read_only=False) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (
                        "case-agent-document-package:"
                        f"{self._worker.firm_id}:{request.idempotency_key}",
                    ),
                )
                current = _read_staging_binding(
                    connection, worker=self._worker, request=request
                )
                if current != binding:
                    raise CaseAgentDocumentPackageBlocked(
                        "document task changed before package registration"
                    )
                prior = _read_package_row(
                    connection,
                    firm_id=self._worker.firm_id,
                    idempotency_key=request.idempotency_key,
                    graph_id=request.graph_id,
                    task_id=request.task_id,
                    generation_mode=request.generation_mode,
                    revision_request_id=request.revision_request_id,
                )
                if prior is not None:
                    return self._verified_prior(
                        prior, request=request, normalized=normalized
                    )
                connection.execute(
                    _insert_package_sql(request.generation_mode),
                    _insert_parameters(
                        request=request,
                        firm_id=self._worker.firm_id,
                        matter_id=binding["matter_id"],
                        staged_by=self._worker.actor_id,
                        package_id=package_id,
                        artifact_ids=artifact_ids,
                        template_hash=template_hash,
                        object_receipts=receipts,
                        package_receipt_hash=package_receipt_hash,
                    ),
                )
        except psycopg.IntegrityError:
            with _transaction(
                self._dsn, self._worker, read_only=True, repeatable_read=True
            ) as connection:
                prior = _read_package_row(
                    connection,
                    firm_id=self._worker.firm_id,
                    idempotency_key=request.idempotency_key,
                    graph_id=request.graph_id,
                    task_id=request.task_id,
                    generation_mode=request.generation_mode,
                    revision_request_id=request.revision_request_id,
                )
            if prior is None:
                raise
            return self._verified_prior(prior, request=request, normalized=normalized)

        return _staged_receipt(
            package_id=package_id,
            artifact_ids=artifact_ids,
            request=request,
            receipt_hash=package_receipt_hash,
        )

    def stage_document_package(
        self, request: ReviewableDocumentPackageStaging
    ) -> StagedDocumentPackage:
        """Bridge the Worker staging DTO into the immutable PostgreSQL contract."""

        if not isinstance(request, ReviewableDocumentPackageStaging):
            raise CaseAgentDocumentPackageBlocked(
                "document adapter staging request is invalid"
            )
        binding = request.binding
        candidate = request.candidate
        generated = request.generated
        binding.validate()
        if (
            binding.template.deliverable_kind == "CASE_REVIEW_MEMO"
            and candidate != build_deterministic_case_review_memo_candidate(binding)
        ):
            raise CaseAgentDocumentPackageBlocked(
                "case review memo candidate differs from confirmed case sources"
            )
        if (
            binding.template.deliverable_kind == "SUPPLEMENTARY_EVIDENCE_CHECKLIST"
            and candidate
            != build_deterministic_supplementary_evidence_checklist_candidate(binding)
        ):
            raise CaseAgentDocumentPackageBlocked(
                "supplementary evidence checklist differs from verified analysis sources"
            )
        if (
            binding.template.deliverable_kind == "PAYMENT_LEDGER"
            and candidate != build_deterministic_payment_ledger_candidate(binding)
        ):
            raise CaseAgentDocumentPackageBlocked(
                "payment ledger candidate differs from confirmed transactions"
            )
        if (
            request.run_id != binding.run_id
            or request.task_id != binding.task_id
            or request.task_input_hash != binding.task_input_hash
            or candidate.binding_hash != binding.binding_hash
            or candidate.source_set_hash != binding.source_set_hash
            or candidate.candidate_hash != generated.approval_hash
            or candidate.template_hash != binding.template.template_hash
        ):
            raise CaseAgentDocumentPackageBlocked(
                "document adapter staging differs from its dynamic binding"
            )
        editable = generated.editable_artifact
        review_pdf = generated.review_pdf
        normalized = ReviewableDocumentPackageStagingRequest(
            idempotency_key=candidate.candidate_hash,
            run_id=request.run_id,
            graph_id=binding.graph_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            task_input_hash=request.task_input_hash,
            case_snapshot_hash=binding.case_snapshot_hash,
            binding_hash=binding.binding_hash,
            source_set_hash=binding.source_set_hash,
            authorized_source_refs=tuple(
                sorted(source.input_ref for source in binding.sources)
            ),
            authorized_source_manifest=_authorized_source_manifest_from_sources(
                binding.sources
            ),
            candidate_hash=candidate.candidate_hash,
            work_plan_id=binding.work_plan_id,
            work_plan_hash=binding.work_plan_hash,
            work_plan_item_id=binding.work_plan_item.item_id,
            posture_profile_id=binding.posture_profile_id,
            posture_profile_hash=binding.posture_profile_hash,
            template_id=binding.template.template_id,
            template_version=binding.template.template_version,
            template_hash=binding.template.template_hash,
            deliverable_kind=binding.template.deliverable_kind,
            output_format=binding.template.output_format,
            candidate_bytes=request.candidate_content,
            candidate_content_sha256=sha256(request.candidate_content).hexdigest(),
            editable_bytes=editable.content,
            editable_sha256=editable.content_sha256,
            editable_media_type=editable.media_type,
            review_pdf_bytes=review_pdf.pdf_content,
            review_pdf_sha256=review_pdf.pdf_sha256,
            review_pdf_page_count=review_pdf.page_count,
            render_verification_hash=review_pdf.render_verification_hash,
            review_input_hash=generated.review_input_hash,
        )
        staged = self.stage_package(normalized)
        return StagedDocumentPackage(
            package_id=staged.package_id,
            receipt_hash=staged.receipt_hash,
            artifact_receipts=staged.artifacts,
        )

    def _put(self, content: bytes, **kwargs: Any) -> PrivateDocumentObjectReceipt:
        raw = self._objects.put_reviewable_document_object(content, **kwargs)
        receipt = _coerce_object_receipt(raw)
        _validate_object_receipt(
            receipt,
            firm_id=kwargs["firm_id"],
            matter_id=kwargs["matter_id"],
            package_id=kwargs["package_id"],
            object_role=kwargs["object_role"],
            expected_hash=kwargs["content_sha256"],
            expected_size=len(content),
            expected_media_type=kwargs["media_type"],
        )
        read_back = self._objects.read_reviewable_document_object(receipt)
        _require_exact_bytes(
            read_back,
            expected_hash=kwargs["content_sha256"],
            expected_size=len(content),
            label=kwargs["object_role"],
        )
        return receipt

    def _verified_prior(
        self,
        row: Mapping[str, Any],
        *,
        request: ReviewableDocumentPackageStagingRequest,
        normalized: Mapping[str, Any],
    ) -> StagedReviewableDocumentPackage:
        _validate_row_against_request(
            row,
            request=request,
            template_hash=reviewable_document_template_hash(normalized["template"]),
        )
        expected_bytes = {
            "candidate": request.candidate_bytes,
            "editable": request.editable_bytes,
            "pdf-preview": request.review_pdf_bytes,
        }
        object_receipts = {
            role: receipt for role, receipt, _expected in _row_object_receipts(row)
        }
        for role, receipt in object_receipts.items():
            _validate_object_receipt(
                receipt,
                firm_id=self._worker.firm_id,
                matter_id=str(row["matter_id"]),
                package_id=str(row["package_id"]),
                object_role=role,
                expected_hash=receipt.content_sha256,
                expected_size=receipt.byte_size,
                expected_media_type=receipt.media_type,
            )
            content = self._objects.read_reviewable_document_object(receipt)
            _require_exact_bytes(
                content,
                expected_hash=receipt.content_sha256,
                expected_size=receipt.byte_size,
                label=role,
            )
            if content != expected_bytes[role]:
                raise CaseAgentDocumentPackageBlocked(
                    "idempotent document package bytes differ from the request"
                )
        expected_receipt_hash = _package_receipt_hash(
            request=request,
            package_id=str(row["package_id"]),
            artifact_ids={
                "candidate": str(row["candidate_artifact_id"]),
                "editable": str(row["editable_artifact_id"]),
                "pdf-preview": str(row["review_pdf_artifact_id"]),
            },
            template_hash=reviewable_document_template_hash(normalized["template"]),
            object_receipts=object_receipts,
        )
        if expected_receipt_hash != str(row["package_receipt_hash"]):
            raise CaseAgentDocumentPackageBlocked(
                "idempotent document package receipt differs from its lineage"
            )
        return _staged_from_row(row)


class PostgresReviewableDocumentPackageAccessPort:
    """Independently re-authorize and read a complete three-object package."""

    def __init__(
        self,
        *,
        dsn: str,
        verifier_actor: Actor,
        execution_actor_id: str,
        object_store: ReviewableDocumentPrivateObjectStore,
        template_registry: ReviewableDocumentTemplateRegistry | None = None,
    ) -> None:
        _dsn(dsn)
        _dedicated_worker(verifier_actor, "document verification")
        _uuid(execution_actor_id, "execution_actor_id")
        if verifier_actor.actor_id == execution_actor_id:
            raise ValueError("document verifier must differ from the execution Worker")
        _object_store(object_store)
        self._dsn = dsn.strip()
        self._verifier = verifier_actor
        self._execution_actor_id = execution_actor_id
        self._objects = object_store
        self._templates = template_registry or first_release_reviewable_document_templates()

    def read_package(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> ReviewableDocumentPackageRead:
        for value, label in (
            (firm_id, "firm_id"),
            (matter_id, "matter_id"),
            (run_id, "run_id"),
            (artifact_id, "artifact_id"),
        ):
            _uuid(value, label)
        if firm_id != self._verifier.firm_id:
            raise PermissionError("document package belongs to another firm")
        with _transaction(
            self._dsn, self._verifier, read_only=True, repeatable_read=True
        ) as connection:
            row = connection.execute(
                _READ_PACKAGE_FOR_VERIFIER_SQL,
                (
                    self._verifier.actor_id,
                    self._execution_actor_id,
                    artifact_id,
                    artifact_id,
                    artifact_id,
                    firm_id,
                    matter_id,
                    run_id,
                    self._execution_actor_id,
                    self._verifier.actor_id,
                    self._execution_actor_id,
                ),
            ).fetchone()
            if row is not None:
                try:
                    assert_case_work_plan_references_current(
                        connection,
                        actor=self._verifier,
                        matter_id=matter_id,
                        plan_id=str(row["work_plan_id"]),
                    )
                except Exception as error:
                    raise CaseAgentDocumentPackageBlocked(
                        "document package sources are no longer current"
                    ) from error
                _assert_authorized_source_manifest_is_current(
                    connection,
                    firm_id=firm_id,
                    matter_id=matter_id,
                    work_plan_id=str(row["work_plan_id"]),
                    work_plan_item_id=str(row["work_plan_item_id"]),
                    work_plan_version=int(row["current_work_plan_version"]),
                    work_plan_hash=str(row["work_plan_hash"]),
                    posture_profile_id=str(row["posture_profile_id"]),
                    posture_profile_version=int(
                        row["current_posture_profile_version"]
                    ),
                    posture_profile_hash=str(row["posture_profile_hash"]),
                    manifest=_authorized_source_manifest_from_json(
                        row["authorized_source_manifest"]
                    ),
                )
        if row is None:
            raise CaseAgentDocumentPackageBlocked(
                "document package is unavailable to this independent verifier"
            )
        template = self._templates.get(str(row["deliverable_kind"]))
        template_hash = reviewable_document_template_hash(template)
        if (
            template.template_id != row["template_id"]
            or template.template_version != row["template_version"]
            or template.output_format.value != row["output_format"]
            or template_hash != row["template_hash"]
        ):
            raise CaseAgentDocumentPackageBlocked(
                "installed document template differs from the staged package"
            )

        values: dict[str, ReviewableDocumentArtifactRead] = {}
        for role, receipt, _expected in _row_object_receipts(row):
            _validate_object_receipt(
                receipt,
                firm_id=firm_id,
                matter_id=matter_id,
                package_id=str(row["package_id"]),
                object_role=role,
                expected_hash=receipt.content_sha256,
                expected_size=receipt.byte_size,
                expected_media_type=receipt.media_type,
            )
            content = self._objects.read_reviewable_document_object(receipt)
            _require_exact_bytes(
                content,
                expected_hash=receipt.content_sha256,
                expected_size=receipt.byte_size,
                label=role,
            )
            artifact_id_key, artifact_kind_key = _role_columns(role)
            values[role] = ReviewableDocumentArtifactRead(
                artifact_id=str(row[artifact_id_key]),
                artifact_kind=str(row[artifact_kind_key]),
                media_type=receipt.media_type,
                content_sha256=receipt.content_sha256,
                byte_size=receipt.byte_size,
                content=content,
            )

        request = _request_from_row_and_bytes(row, values)
        normalized = _validate_staging_request(request, self._templates)
        if _binding_hash(
            request,
            firm_id=firm_id,
            matter_id=matter_id,
        ) != request.binding_hash:
            raise CaseAgentDocumentPackageBlocked(
                "document package binding differs from the current case"
            )
        authorized_source_refs = row["authorized_source_refs"]
        authorized_source_manifest = _authorized_source_manifest_from_json(
            row["authorized_source_manifest"]
        )
        if (
            not isinstance(authorized_source_refs, list)
            or _authorized_source_refs_hash(tuple(authorized_source_refs))
            != row["authorized_source_refs_hash"]
            or tuple(sorted(item.input_ref for item in authorized_source_manifest))
            != tuple(authorized_source_refs)
            or _source_set_hash_from_manifest(authorized_source_manifest)
            != row["source_set_hash"]
            or not _candidate_source_refs(normalized["candidate"]).issubset(
                set(authorized_source_refs)
            )
        ):
            raise CaseAgentDocumentPackageBlocked(
                "document package cites a source outside the current task"
            )
        expected_receipt_hash = _package_receipt_hash(
            request=request,
            package_id=str(row["package_id"]),
            artifact_ids={
                "candidate": str(row["candidate_artifact_id"]),
                "editable": str(row["editable_artifact_id"]),
                "pdf-preview": str(row["review_pdf_artifact_id"]),
            },
            template_hash=reviewable_document_template_hash(normalized["template"]),
            object_receipts={role: receipt for role, receipt, _ in _row_object_receipts(row)},
        )
        if expected_receipt_hash != row["package_receipt_hash"]:
            raise CaseAgentDocumentPackageBlocked(
                "document package receipt differs from its three-object lineage"
            )
        return ReviewableDocumentPackageRead(
            package_id=str(row["package_id"]),
            run_id=str(row["run_id"]),
            graph_id=str(row["graph_id"]),
            task_id=str(row["task_id"]),
            task_input_hash=str(row["task_input_hash"]),
            case_snapshot_hash=str(row["case_snapshot_hash"]),
            binding_hash=str(row["binding_hash"]),
            source_set_hash=str(row["source_set_hash"]),
            authorized_source_refs=tuple(str(item) for item in authorized_source_refs),
            authorized_source_refs_hash=str(row["authorized_source_refs_hash"]),
            authorized_source_manifest=authorized_source_manifest,
            candidate_hash=str(row["candidate_hash"]),
            work_plan_id=str(row["work_plan_id"]),
            work_plan_hash=str(row["work_plan_hash"]),
            work_plan_item_id=str(row["work_plan_item_id"]),
            posture_profile_id=str(row["posture_profile_id"]),
            posture_profile_hash=str(row["posture_profile_hash"]),
            template_id=str(row["template_id"]),
            template_version=str(row["template_version"]),
            template_hash=str(row["template_hash"]),
            deliverable_kind=str(row["deliverable_kind"]),
            output_format=ReviewableDocumentFormat(str(row["output_format"])),
            review_input_hash=str(row["review_input_hash"]),
            render_verification_hash=str(row["render_verification_hash"]),
            receipt_hash=str(row["package_receipt_hash"]),
            candidate=values["candidate"],
            editable=values["editable"],
            review_pdf=values["pdf-preview"],
            selected_artifact_id=artifact_id,
            content_generation_claim_version=row.get("content_generation_claim_version"),
            review_pdf_page_count=int(row["review_pdf_page_count"]),
            generation_mode=str(row.get("generation_mode", _INITIAL_GENERATION_MODE)),
            revision_number=int(row.get("revision_number", 1)),
            root_package_id=(
                str(row["root_package_id"])
                if row.get("root_package_id") is not None
                else None
            ),
            supersedes_package_id=(
                str(row["supersedes_package_id"])
                if row.get("supersedes_package_id") is not None
                else None
            ),
            revision_request_id=(
                str(row["revision_request_id"])
                if row.get("revision_request_id") is not None
                else None
            ),
            requested_by=(
                str(row["requested_by"])
                if row.get("requested_by") is not None
                else None
            ),
        )

    def read_managed_artifact(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact: ArtifactReceipt,
    ) -> ManagedArtifactRead:
        """Adapt the pair-preserving read to the unified verifier contract."""

        if not isinstance(artifact, ArtifactReceipt):
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID")
        try:
            artifact.validate()
            package = self.read_package(
                firm_id=firm_id,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact.artifact_id,
            )
        except PermissionError:
            raise
        except CaseAgentDocumentPackageBlocked as error:
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_UNAVAILABLE") from error
        except Exception as error:
            raise CaseAgentVerificationIndeterminate(
                "reviewable document package could not be independently read"
            ) from error
        selected = package.selected_artifact
        if (
            selected.artifact_kind != artifact.artifact_kind
            or selected.content_sha256 != artifact.content_hash
            or selected.byte_size != artifact.byte_size
            or package.task_input_hash != artifact.source_input_hash
            or not artifact.managed_derivative
        ):
            raise ArtifactVerificationRejected("ARTIFACT_LINEAGE_MISMATCH")
        return ManagedArtifactRead(
            artifact_id=selected.artifact_id,
            artifact_kind=selected.artifact_kind,
            content=selected.content,
            source_input_hash=package.task_input_hash,
            object_receipt_hash=package.receipt_hash,
            media_type=selected.media_type,
        )


class DocumentAwareManagedArtifactAccessPort:
    """Route document artifacts to 0039 and every other kind to the base reader.

    A document read is never retried through the generic candidate store: a
    missing/tampered three-object package must fail closed as a document error.
    """

    def __init__(self, *, base_access: Any, document_access: Any) -> None:
        if not callable(getattr(base_access, "read_managed_artifact", None)):
            raise ValueError("base managed-artifact access is required")
        if not callable(getattr(document_access, "read_managed_artifact", None)):
            raise ValueError("document managed-artifact access is required")
        self._base = base_access
        self._documents = document_access

    def read_managed_artifact(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact: ArtifactReceipt,
    ) -> ManagedArtifactRead:
        if not isinstance(artifact, ArtifactReceipt):
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID")
        target = (
            self._documents
            if artifact.artifact_kind in _ARTIFACT_KINDS
            else self._base
        )
        return target.read_managed_artifact(
            firm_id=firm_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact=artifact,
        )


def preflight_case_agent_document_delivery_runtime_contract(
    *,
    dsn: str,
    worker_actor: Actor,
    verifier_actor: Actor,
    object_store: ReviewableDocumentPrivateObjectStore,
) -> None:
    """Fail startup unless the immutable 0039 delivery boundary is complete.

    This is deliberately a structural check only.  It validates the two
    independent system principals, the installed release templates, the
    private-object method contract and PostgreSQL catalogue metadata.  It does
    not read a matter or object and does not invoke a drafting model.
    """

    try:
        _dsn(dsn)
        _dedicated_worker(worker_actor, "document delivery execution")
        _dedicated_worker(verifier_actor, "document delivery verification")
        if worker_actor.firm_id != verifier_actor.firm_id:
            raise CaseAgentDocumentPackageBlocked(
                "document delivery Workers must belong to the same firm"
            )
        if worker_actor.actor_id == verifier_actor.actor_id:
            raise CaseAgentDocumentPackageBlocked(
                "document delivery verifier must differ from the execution Worker"
            )
        _object_store(object_store)

        templates = first_release_reviewable_document_templates()
        if not isinstance(templates, ReviewableDocumentTemplateRegistry):
            raise CaseAgentDocumentPackageBlocked(
                "reviewable document template registry is unavailable"
            )
        installed = templates.list_templates()
        if not installed:
            raise CaseAgentDocumentPackageBlocked(
                "reviewable document template registry is empty"
            )
        for template in installed:
            if templates.get(template.deliverable_kind) is not template:
                raise CaseAgentDocumentPackageBlocked(
                    "reviewable document template registry is inconsistent"
                )
            reviewable_document_template_hash(template)

        with _transaction(
            dsn.strip(), worker_actor, read_only=True
        ) as connection:
            column_rows = connection.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'case_agent_reviewable_document_packages'
                ORDER BY ordinal_position
                """
            ).fetchall()
            observed_columns = {
                str(row["column_name"])
                for row in column_rows
                if str(row["table_name"])
                == "case_agent_reviewable_document_packages"
            }
            if not _DOCUMENT_DELIVERY_REQUIRED_COLUMNS.issubset(
                observed_columns
            ):
                raise CaseAgentDocumentPackageBlocked(
                    "case-Agent document delivery migration 0039 is incomplete"
                )

            trigger_rows = connection.execute(
                """
                SELECT trigger_name
                FROM information_schema.triggers
                WHERE trigger_schema = 'public'
                  AND event_object_table =
                      'case_agent_reviewable_document_packages'
                  AND trigger_name = ANY(%s)
                """,
                (list(_DOCUMENT_DELIVERY_REQUIRED_TRIGGERS),),
            ).fetchall()
            observed_triggers = {
                str(row["trigger_name"]) for row in trigger_rows
            }
            if not _DOCUMENT_DELIVERY_REQUIRED_TRIGGERS.issubset(
                observed_triggers
            ):
                raise CaseAgentDocumentPackageBlocked(
                    "case-Agent document delivery guards are incomplete"
                )

            rls_row = connection.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_catalog.pg_class
                JOIN pg_catalog.pg_namespace
                  ON pg_namespace.oid = pg_class.relnamespace
                WHERE pg_namespace.nspname = 'public'
                  AND pg_class.relkind = 'r'
                  AND relname = 'case_agent_reviewable_document_packages'
                """
            ).fetchone()
            if (
                rls_row is None
                or not bool(rls_row["relrowsecurity"])
                or not bool(rls_row["relforcerowsecurity"])
            ):
                raise CaseAgentDocumentPackageBlocked(
                    "case-Agent document delivery requires FORCE RLS"
                )
    except CaseAgentDocumentPackageBlocked:
        raise
    except Exception as error:
        raise CaseAgentDocumentPackageBlocked(
            "case-Agent document delivery preflight failed"
        ) from error


def _validate_staging_request(
    request: ReviewableDocumentPackageStagingRequest,
    templates: ReviewableDocumentTemplateRegistry,
) -> Mapping[str, Any]:
    if not isinstance(request, ReviewableDocumentPackageStagingRequest):
        raise CaseAgentDocumentPackageBlocked("document package request is invalid")
    _sha(request.idempotency_key, "idempotency_key")
    for value, label in (
        (request.run_id, "run_id"),
        (request.graph_id, "graph_id"),
        (request.task_id, "task_id"),
        (request.attempt_id, "attempt_id"),
        (request.work_plan_id, "work_plan_id"),
        (request.work_plan_item_id, "work_plan_item_id"),
        (request.posture_profile_id, "posture_profile_id"),
    ):
        _uuid(value, label)
    if type(request.revision_number) is not int:
        raise CaseAgentDocumentPackageBlocked("document revision number must be an integer")
    if request.generation_mode == _CONTENT_REVISION_GENERATION_MODE:
        if type(request.content_generation_claim_version) is not int or not 1 <= request.content_generation_claim_version <= 3:
            raise CaseAgentDocumentPackageBlocked("content revision render claim is invalid")
    elif request.content_generation_claim_version is not None:
        raise CaseAgentDocumentPackageBlocked("only content revisions carry a render claim")
    if request.generation_mode == _INITIAL_GENERATION_MODE:
        if (
            request.revision_number != 1
            or request.root_package_id is not None
            or request.supersedes_package_id is not None
            or request.revision_request_id is not None
            or request.requested_by is not None
        ):
            raise CaseAgentDocumentPackageBlocked(
                "initial document package revision coordinates are invalid"
            )
    elif request.generation_mode in _REVISION_GENERATION_MODES:
        if not 2 <= request.revision_number <= 1000:
            raise CaseAgentDocumentPackageBlocked(
                "document revision number is invalid"
            )
        for value, label in (
            (request.root_package_id, "root_package_id"),
            (request.supersedes_package_id, "supersedes_package_id"),
            (request.revision_request_id, "revision_request_id"),
            (request.requested_by, "requested_by"),
        ):
            if value is None:
                raise CaseAgentDocumentPackageBlocked(
                    f"{label} is required for a document revision"
                )
            _uuid(value, label)
        if (request.generation_mode == _CONTENT_REVISION_GENERATION_MODE
                and request.output_format is not ReviewableDocumentFormat.DOCX):
            raise CaseAgentDocumentPackageBlocked("content revisions require a DOCX document")
    else:
        raise CaseAgentDocumentPackageBlocked(
            "document package generation mode is invalid"
        )
    for value, label in (
        (request.task_input_hash, "task_input_hash"),
        (request.case_snapshot_hash, "case_snapshot_hash"),
        (request.binding_hash, "binding_hash"),
        (request.source_set_hash, "source_set_hash"),
        (request.candidate_hash, "candidate_hash"),
        (request.work_plan_hash, "work_plan_hash"),
        (request.posture_profile_hash, "posture_profile_hash"),
        (request.template_hash, "template_hash"),
        (request.candidate_content_sha256, "candidate_content_sha256"),
        (request.editable_sha256, "editable_sha256"),
        (request.review_pdf_sha256, "review_pdf_sha256"),
        (request.render_verification_hash, "render_verification_hash"),
        (request.review_input_hash, "review_input_hash"),
    ):
        _sha(value, label)
    _authorized_source_refs(request.authorized_source_refs)
    manifest = _authorized_source_manifest(request.authorized_source_manifest)
    manifest_refs = tuple(sorted(item.input_ref for item in manifest))
    if manifest_refs != request.authorized_source_refs:
        raise CaseAgentDocumentPackageBlocked(
            "authorized document refs differ from the server source manifest"
        )
    if _source_set_hash_from_manifest(manifest) != request.source_set_hash:
        raise CaseAgentDocumentPackageBlocked(
            "source_set_hash differs from the server source manifest"
        )
    if not isinstance(request.output_format, ReviewableDocumentFormat):
        raise CaseAgentDocumentPackageBlocked("document output format is invalid")
    _identifier(request.template_id, "template_id")
    if (
        not isinstance(request.template_version, str)
        or _SEMVER.fullmatch(request.template_version) is None
    ):
        raise CaseAgentDocumentPackageBlocked("template_version is invalid")
    _code(request.deliverable_kind, "deliverable_kind")
    template = templates.get(request.deliverable_kind)
    if (
        template.template_id != request.template_id
        or template.template_version != request.template_version
        or template.output_format is not request.output_format
        or reviewable_document_template_hash(template) != request.template_hash
    ):
        raise CaseAgentDocumentPackageBlocked(
            "document request differs from the installed server template"
        )

    _bounded_bytes(
        request.candidate_bytes,
        request.candidate_content_sha256,
        minimum=2,
        maximum=_CANDIDATE_LIMIT,
        label="structured candidate",
    )
    candidate = _parse_canonical_candidate(request.candidate_bytes)
    expected_binding = {
        "binding_hash": request.binding_hash,
        "source_set_hash": request.source_set_hash,
        "task_input_hash": request.task_input_hash,
        "work_plan_item_id": request.work_plan_item_id,
        "template_id": request.template_id,
        "template_version": request.template_version,
        "template_hash": request.template_hash,
        "deliverable_kind": request.deliverable_kind,
        "output_format": request.output_format.value,
    }
    if candidate["binding"] != expected_binding:
        raise CaseAgentDocumentPackageBlocked(
            "structured candidate differs from the package binding"
        )
    expected_schema = (
        "case-agent-reviewable-docx-candidate-v1"
        if request.output_format is ReviewableDocumentFormat.DOCX
        else "case-agent-reviewable-xlsx-candidate-v1"
    )
    if candidate["schema_version"] != expected_schema:
        raise CaseAgentDocumentPackageBlocked(
            "structured candidate schema differs from its output format"
        )
    if _semantic_candidate_hash(candidate) != request.candidate_hash:
        raise CaseAgentDocumentPackageBlocked("candidate_hash differs from canonical content")
    _assert_payment_ledger_matches_source_manifest(
        candidate=candidate,
        deliverable_kind=request.deliverable_kind,
        output_format=request.output_format,
        manifest=manifest,
    )
    expected_editable_media = (
        _DOCX_MEDIA if request.output_format is ReviewableDocumentFormat.DOCX else _XLSX_MEDIA
    )
    if request.editable_media_type != expected_editable_media:
        raise CaseAgentDocumentPackageBlocked("editable media type differs from output format")
    _bounded_bytes(
        request.editable_bytes,
        request.editable_sha256,
        minimum=1,
        maximum=_EDITABLE_LIMIT,
        label="editable Office artifact",
    )
    _verify_ooxml(request.editable_bytes, request.output_format)
    _bounded_bytes(
        request.review_pdf_bytes,
        request.review_pdf_sha256,
        minimum=5,
        maximum=_PDF_LIMIT,
        label="review PDF",
    )
    _verify_pdf(request.review_pdf_bytes, request.review_pdf_page_count)
    if _review_input_hash(request) != request.review_input_hash:
        raise CaseAgentDocumentPackageBlocked(
            "review_input_hash differs from the editable/PDF render pair"
        )
    return {"template": template, "candidate": candidate}


def _parse_canonical_candidate(content: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CaseAgentDocumentPackageBlocked(
            "structured document candidate is not strict JSON"
        ) from error
    if not isinstance(value, dict) or _json_bytes(value) != content:
        raise CaseAgentDocumentPackageBlocked(
            "structured document candidate is not canonical JSON"
        )
    common = {
        "schema_version", "binding", "title", "review_status",
        "formal_fact", "formal_legal_conclusion", "court_ready",
    }
    if value.get("schema_version") == "case-agent-reviewable-docx-candidate-v1":
        if set(value) != common | {"sections"}:
            raise CaseAgentDocumentPackageBlocked("DOCX candidate schema is invalid")
        _validate_docx_candidate(value)
    elif value.get("schema_version") == "case-agent-reviewable-xlsx-candidate-v1":
        if set(value) != common | {"columns", "rows"}:
            raise CaseAgentDocumentPackageBlocked("XLSX candidate schema is invalid")
        _validate_xlsx_candidate(value)
    else:
        raise CaseAgentDocumentPackageBlocked("document candidate schema is unsupported")
    if (
        value.get("review_status") != "NEEDS_LAWYER_REVIEW"
        or value.get("formal_fact") is not False
        or value.get("formal_legal_conclusion") is not False
        or value.get("court_ready") is not False
    ):
        raise CaseAgentDocumentPackageBlocked(
            "document candidate cannot assert formal or court-ready status"
        )
    binding = value.get("binding")
    if not isinstance(binding, dict) or set(binding) != {
        "binding_hash", "source_set_hash", "task_input_hash",
        "work_plan_item_id", "template_id", "template_version",
        "template_hash", "deliverable_kind", "output_format",
    }:
        raise CaseAgentDocumentPackageBlocked("document candidate binding is invalid")
    return value


def _validate_docx_candidate(value: Mapping[str, Any]) -> None:
    total_characters = len(_text(value.get("title"), "candidate title", 240))
    sections = value.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 200:
        raise CaseAgentDocumentPackageBlocked("DOCX candidate sections are invalid")
    paragraphs = 0
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"heading", "paragraphs"}:
            raise CaseAgentDocumentPackageBlocked("DOCX candidate section is invalid")
        total_characters += len(_text(section.get("heading"), "DOCX heading", 240))
        values = section.get("paragraphs")
        if not isinstance(values, list) or not values:
            raise CaseAgentDocumentPackageBlocked("DOCX paragraphs are invalid")
        for paragraph in values:
            if not isinstance(paragraph, dict) or set(paragraph) != {"text", "source_refs"}:
                raise CaseAgentDocumentPackageBlocked("DOCX paragraph is invalid")
            total_characters += len(
                _text(paragraph.get("text"), "DOCX paragraph", 20_000)
            )
            _source_refs(paragraph.get("source_refs"))
            paragraphs += 1
            if paragraphs > 20_000:
                raise CaseAgentDocumentPackageBlocked("DOCX paragraph count is excessive")
            if total_characters > 1_500_000:
                raise CaseAgentDocumentPackageBlocked("DOCX candidate text is excessive")


def _validate_xlsx_candidate(value: Mapping[str, Any]) -> None:
    _text(value.get("title"), "candidate title", 240)
    columns = value.get("columns")
    rows = value.get("rows")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 200:
        raise CaseAgentDocumentPackageBlocked("XLSX columns are invalid")
    keys: list[str] = []
    column_types: dict[str, str] = {}
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"key", "label", "value_type"}:
            raise CaseAgentDocumentPackageBlocked("XLSX column schema is invalid")
        key = column.get("key")
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key)
            or key in keys
        ):
            raise CaseAgentDocumentPackageBlocked("XLSX column key is invalid")
        _text(column.get("label"), "XLSX column label", 160)
        if column.get("value_type") not in {"TEXT", "INTEGER", "DECIMAL", "DATE", "BOOLEAN"}:
            raise CaseAgentDocumentPackageBlocked("XLSX column type is invalid")
        keys.append(key)
        column_types[key] = str(column["value_type"])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100_000:
        raise CaseAgentDocumentPackageBlocked("XLSX rows are invalid")
    row_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"row_id", "cells", "source_refs"}:
            raise CaseAgentDocumentPackageBlocked("XLSX row schema is invalid")
        row_id = row.get("row_id")
        if (
            not isinstance(row_id, str)
            or _IDENTIFIER.fullmatch(row_id) is None
            or row_id in row_ids
        ):
            raise CaseAgentDocumentPackageBlocked("XLSX row id is invalid")
        cells = row.get("cells")
        if not isinstance(cells, dict) or list(cells) != sorted(cells) or set(cells) != set(keys):
            raise CaseAgentDocumentPackageBlocked("XLSX row cells are invalid")
        for key, item in cells.items():
            _validate_xlsx_cell(item, column_types[key])
        _source_refs(row.get("source_refs"))
        row_ids.add(row_id)


def _validate_xlsx_cell(value: object, value_type: str) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        if value_type != "BOOLEAN":
            raise CaseAgentDocumentPackageBlocked("XLSX cell type is invalid")
        return
    if value_type == "INTEGER" and isinstance(value, int):
        return
    if value_type == "DECIMAL" and isinstance(value, (int, float)):
        return
    if value_type in {"TEXT", "DATE"} and isinstance(value, str):
        if not value or len(value) > 20_000 or value[0] in "=+-@":
            raise CaseAgentDocumentPackageBlocked("XLSX cell is unsafe")
        return
    raise CaseAgentDocumentPackageBlocked("XLSX cell type is invalid")


def _semantic_candidate_hash(value: Mapping[str, Any]) -> str:
    binding = value["binding"]
    sections = value.get("sections", [])
    columns = value.get("columns", [])
    column_keys = [item["key"] for item in columns]
    rows = [
        {
            "row_id": item["row_id"],
            "cells": [item["cells"][key] for key in column_keys],
            "source_refs": item["source_refs"],
        }
        for item in value.get("rows", [])
    ]
    return _canonical_hash(
        {
            "schema_version": "case-agent-reviewable-document-content-v1",
            "binding_hash": binding["binding_hash"],
            "source_set_hash": binding["source_set_hash"],
            "task_input_hash": binding["task_input_hash"],
            "work_plan_item_id": binding["work_plan_item_id"],
            "template_id": binding["template_id"],
            "template_version": binding["template_version"],
            "template_hash": binding["template_hash"],
            "deliverable_kind": binding["deliverable_kind"],
            "output_format": binding["output_format"],
            "title": value["title"],
            "sections": sections,
            "columns": columns,
            "rows": rows,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "formal_fact": False,
            "formal_legal_conclusion": False,
            "court_ready": False,
        }
    )


def _assert_payment_ledger_matches_source_manifest(
    *,
    candidate: Mapping[str, Any],
    deliverable_kind: str,
    output_format: ReviewableDocumentFormat,
    manifest: tuple[AuthorizedDocumentSourceBinding, ...],
) -> None:
    """Independently prove every ledger row is one authorized transaction.

    The verifier process does not receive source text.  The immutable manifest
    retains its SHA-256, so the exact canonical source JSON reconstructed from
    the fixed row fields must match that digest.  This detects omissions,
    additions and any changed date, amount, party, direction or reference.
    """

    if deliverable_kind != "PAYMENT_LEDGER":
        return
    expected_columns = [
        {"key": item.key, "label": item.label, "value_type": item.value_type}
        for item in PAYMENT_LEDGER_COLUMNS
    ]
    transactions = {
        item.input_ref: item
        for item in manifest
        if item.source_kind == DocumentSourceKind.CONFIRMED_TRANSACTION.value
    }
    rows = candidate.get("rows")
    if (
        output_format is not ReviewableDocumentFormat.XLSX
        or candidate.get("columns") != expected_columns
        or not transactions
        or not isinstance(rows, list)
        or len(rows) != len(transactions)
        or [row.get("row_id") for row in rows] != sorted(transactions)
    ):
        raise CaseAgentDocumentPackageBlocked(
            "payment ledger differs from the deterministic transaction set"
        )
    for row in rows:
        row_id = row.get("row_id")
        cells = row.get("cells")
        if (
            row_id not in transactions
            or row.get("source_refs") != [row_id]
            or not isinstance(cells, dict)
            or set(cells) != set(PAYMENT_LEDGER_SOURCE_KEYS)
        ):
            raise CaseAgentDocumentPackageBlocked(
                "payment ledger row is not bound to exactly one transaction"
            )
        payload = {key: cells[key] for key in PAYMENT_LEDGER_SOURCE_KEYS}
        if sha256(_json_bytes(payload)).hexdigest() != transactions[row_id].text_sha256:
            raise CaseAgentDocumentPackageBlocked(
                "payment ledger row values differ from the confirmed transaction"
            )


def _binding_hash(
    request: ReviewableDocumentPackageStagingRequest,
    *,
    firm_id: str,
    matter_id: str,
) -> str:
    return _canonical_hash(
        {
            "schema_version": "case-agent-dynamic-document-binding-v1",
            "firm_id": firm_id,
            "matter_id": matter_id,
            "run_id": request.run_id,
            "graph_id": request.graph_id,
            "task_id": request.task_id,
            "task_input_hash": request.task_input_hash,
            "case_snapshot_hash": request.case_snapshot_hash,
            "work_plan_id": request.work_plan_id,
            "work_plan_hash": request.work_plan_hash,
            "work_plan_item_id": request.work_plan_item_id,
            "posture_profile_id": request.posture_profile_id,
            "posture_profile_hash": request.posture_profile_hash,
            "template_id": request.template_id,
            "template_version": request.template_version,
            "template_hash": request.template_hash,
            "deliverable_kind": request.deliverable_kind,
            "output_format": request.output_format.value,
        }
    )


def _review_input_hash(request: ReviewableDocumentPackageStagingRequest) -> str:
    detected_kind = (
        "WORD_DOCUMENT"
        if request.output_format is ReviewableDocumentFormat.DOCX
        else "SPREADSHEET"
    )
    return _canonical_hash(
        {
            "schema_version": "reviewable-office-draft-v1",
            "approval_hash": request.candidate_hash,
            "detected_kind": detected_kind,
            "editable_media_type": request.editable_media_type,
            "editable_sha256": request.editable_sha256,
            "editable_bytes": len(request.editable_bytes),
            "review_pdf_sha256": request.review_pdf_sha256,
            "review_pdf_bytes": len(request.review_pdf_bytes),
            "review_pdf_page_count": request.review_pdf_page_count,
            "render_verification_hash": request.render_verification_hash,
        }
    )


def _package_receipt_hash(
    *,
    request: ReviewableDocumentPackageStagingRequest,
    package_id: str,
    artifact_ids: Mapping[str, str],
    template_hash: str,
    object_receipts: Mapping[str, PrivateDocumentObjectReceipt],
) -> str:
    if request.generation_mode not in {_INITIAL_GENERATION_MODE, *_REVISION_GENERATION_MODES}:
        raise CaseAgentDocumentPackageBlocked("document package receipt generation mode is invalid")
    payload: dict[str, object] = {
            "schema_version": (
                "case-agent-reviewable-document-package-receipt-v1"
                if request.generation_mode == _INITIAL_GENERATION_MODE
                else "case-agent-reviewable-document-package-receipt-v2"
            ),
            "package_id": package_id,
            "run_id": request.run_id,
            "graph_id": request.graph_id,
            "task_id": request.task_id,
            "attempt_id": request.attempt_id,
            "task_input_hash": request.task_input_hash,
            "case_snapshot_hash": request.case_snapshot_hash,
            "binding_hash": request.binding_hash,
            "source_set_hash": request.source_set_hash,
            "authorized_source_refs": list(request.authorized_source_refs),
            "authorized_source_manifest": _authorized_source_manifest_payload(
                request.authorized_source_manifest
            ),
            "candidate_hash": request.candidate_hash,
            "work_plan_id": request.work_plan_id,
            "work_plan_hash": request.work_plan_hash,
            "work_plan_item_id": request.work_plan_item_id,
            "posture_profile_id": request.posture_profile_id,
            "posture_profile_hash": request.posture_profile_hash,
            "template_id": request.template_id,
            "template_version": request.template_version,
            "template_hash": template_hash,
            "deliverable_kind": request.deliverable_kind,
            "output_format": request.output_format.value,
            "candidate": _receipt_item_payload(
                artifact_id=artifact_ids["candidate"],
                artifact_kind=_ARTIFACT_KINDS[0],
                receipt=object_receipts["candidate"],
            ),
            "editable": _receipt_item_payload(
                artifact_id=artifact_ids["editable"],
                artifact_kind=_ARTIFACT_KINDS[1],
                receipt=object_receipts["editable"],
            ),
            "review_pdf": {
                **_receipt_item_payload(
                    artifact_id=artifact_ids["pdf-preview"],
                    artifact_kind=_ARTIFACT_KINDS[2],
                    receipt=object_receipts["pdf-preview"],
                ),
                "page_count": request.review_pdf_page_count,
            },
            "render_verification_hash": request.render_verification_hash,
            "review_input_hash": request.review_input_hash,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "formal_document": False,
            "court_submitted": False,
        }
    if request.generation_mode in _REVISION_GENERATION_MODES:
        payload.update(
            {
                "generation_mode": request.generation_mode,
                "revision_number": request.revision_number,
                "root_package_id": request.root_package_id,
                "supersedes_package_id": request.supersedes_package_id,
                "revision_request_id": request.revision_request_id,
                "requested_by": request.requested_by,
                "external_calls": 0,
            }
        )
    if request.generation_mode == _CONTENT_REVISION_GENERATION_MODE:
        payload["content_generation_claim_version"] = request.content_generation_claim_version
    return _canonical_hash(payload)


def _receipt_item_payload(
    *, artifact_id: str, artifact_kind: str, receipt: PrivateDocumentObjectReceipt
) -> Mapping[str, object]:
    return {
        "artifact_id": artifact_id,
        "artifact_kind": artifact_kind,
        "media_type": receipt.media_type,
        "content_sha256": receipt.content_sha256,
        "byte_size": receipt.byte_size,
        "object_reference_hash": sha256(receipt.object_key.encode("utf-8")).hexdigest(),
        "object_version_hash": (
            sha256(receipt.object_version_id.encode("utf-8")).hexdigest()
            if receipt.object_version_id is not None
            else None
        ),
    }


def _artifact_ids(task_id: str, package_id: str) -> Mapping[str, str]:
    namespace = UUID(task_id)
    return {
        "candidate": str(uuid5(namespace, f"{package_id}:{_ARTIFACT_KINDS[0]}")),
        "editable": str(uuid5(namespace, f"{package_id}:{_ARTIFACT_KINDS[1]}")),
        "pdf-preview": str(uuid5(namespace, f"{package_id}:{_ARTIFACT_KINDS[2]}")),
    }


def _staged_receipt(
    *,
    package_id: str,
    artifact_ids: Mapping[str, str],
    request: ReviewableDocumentPackageStagingRequest,
    receipt_hash: str,
) -> StagedReviewableDocumentPackage:
    return StagedReviewableDocumentPackage(
        package_id=package_id,
        candidate_artifact=ArtifactReceipt(
            artifact_id=artifact_ids["candidate"],
            artifact_kind=_ARTIFACT_KINDS[0],
            content_hash=request.candidate_content_sha256,
            byte_size=len(request.candidate_bytes),
            source_input_hash=request.task_input_hash,
            managed_derivative=True,
        ),
        editable_artifact=ArtifactReceipt(
            artifact_id=artifact_ids["editable"],
            artifact_kind=_ARTIFACT_KINDS[1],
            content_hash=request.editable_sha256,
            byte_size=len(request.editable_bytes),
            source_input_hash=request.task_input_hash,
            managed_derivative=True,
        ),
        review_pdf_artifact=ArtifactReceipt(
            artifact_id=artifact_ids["pdf-preview"],
            artifact_kind=_ARTIFACT_KINDS[2],
            content_hash=request.review_pdf_sha256,
            byte_size=len(request.review_pdf_bytes),
            source_input_hash=request.task_input_hash,
            managed_derivative=True,
        ),
        receipt_hash=receipt_hash,
        generation_mode=request.generation_mode,
        revision_number=request.revision_number,
        root_package_id=request.root_package_id,
        supersedes_package_id=request.supersedes_package_id,
        revision_request_id=request.revision_request_id,
    )


def _staged_from_row(row: Mapping[str, Any]) -> StagedReviewableDocumentPackage:
    return StagedReviewableDocumentPackage(
        package_id=str(row["package_id"]),
        candidate_artifact=ArtifactReceipt(
            artifact_id=str(row["candidate_artifact_id"]),
            artifact_kind=str(row["candidate_artifact_kind"]),
            content_hash=str(row["candidate_content_sha256"]),
            byte_size=int(row["candidate_byte_size"]),
            source_input_hash=str(row["task_input_hash"]),
            managed_derivative=True,
        ),
        editable_artifact=ArtifactReceipt(
            artifact_id=str(row["editable_artifact_id"]),
            artifact_kind=str(row["editable_artifact_kind"]),
            content_hash=str(row["editable_sha256"]),
            byte_size=int(row["editable_byte_size"]),
            source_input_hash=str(row["task_input_hash"]),
            managed_derivative=True,
        ),
        review_pdf_artifact=ArtifactReceipt(
            artifact_id=str(row["review_pdf_artifact_id"]),
            artifact_kind=str(row["review_pdf_artifact_kind"]),
            content_hash=str(row["review_pdf_sha256"]),
            byte_size=int(row["review_pdf_byte_size"]),
            source_input_hash=str(row["task_input_hash"]),
            managed_derivative=True,
        ),
        receipt_hash=str(row["package_receipt_hash"]),
        generation_mode=str(row.get("generation_mode", _INITIAL_GENERATION_MODE)),
        revision_number=int(row.get("revision_number", 1)),
        root_package_id=(
            str(row["root_package_id"])
            if row.get("root_package_id") is not None
            else None
        ),
        supersedes_package_id=(
            str(row["supersedes_package_id"])
            if row.get("supersedes_package_id") is not None
            else None
        ),
        revision_request_id=(
            str(row["revision_request_id"])
            if row.get("revision_request_id") is not None
            else None
        ),
    )


def _request_from_row_and_bytes(
    row: Mapping[str, Any],
    artifacts: Mapping[str, ReviewableDocumentArtifactRead],
) -> ReviewableDocumentPackageStagingRequest:
    candidate_bytes = artifacts["candidate"].content
    editable_bytes = artifacts["editable"].content
    pdf_bytes = artifacts["pdf-preview"].content
    return ReviewableDocumentPackageStagingRequest(
        idempotency_key=str(row["idempotency_key"]),
        run_id=str(row["run_id"]),
        graph_id=str(row["graph_id"]),
        task_id=str(row["task_id"]),
        attempt_id=str(row["attempt_id"]),
        task_input_hash=str(row["task_input_hash"]),
        case_snapshot_hash=str(row["case_snapshot_hash"]),
        binding_hash=str(row["binding_hash"]),
        source_set_hash=str(row["source_set_hash"]),
        authorized_source_refs=tuple(
            str(item) for item in row["authorized_source_refs"]
        ),
        authorized_source_manifest=_authorized_source_manifest_from_json(
            row["authorized_source_manifest"]
        ),
        candidate_hash=str(row["candidate_hash"]),
        work_plan_id=str(row["work_plan_id"]),
        work_plan_hash=str(row["work_plan_hash"]),
        work_plan_item_id=str(row["work_plan_item_id"]),
        posture_profile_id=str(row["posture_profile_id"]),
        posture_profile_hash=str(row["posture_profile_hash"]),
        template_id=str(row["template_id"]),
        template_version=str(row["template_version"]),
        template_hash=str(row["template_hash"]),
        deliverable_kind=str(row["deliverable_kind"]),
        output_format=ReviewableDocumentFormat(str(row["output_format"])),
        candidate_bytes=candidate_bytes,
        candidate_content_sha256=str(row["candidate_content_sha256"]),
        editable_bytes=editable_bytes,
        editable_sha256=str(row["editable_sha256"]),
        editable_media_type=str(row["editable_media_type"]),
        review_pdf_bytes=pdf_bytes,
        review_pdf_sha256=str(row["review_pdf_sha256"]),
        review_pdf_page_count=int(row["review_pdf_page_count"]),
        render_verification_hash=str(row["render_verification_hash"]),
        review_input_hash=str(row["review_input_hash"]),
        generation_mode=str(row.get("generation_mode", _INITIAL_GENERATION_MODE)),
        revision_number=int(row.get("revision_number", 1)),
        root_package_id=(
            str(row["root_package_id"])
            if row.get("root_package_id") is not None
            else None
        ),
        supersedes_package_id=(
            str(row["supersedes_package_id"])
            if row.get("supersedes_package_id") is not None
            else None
        ),
        revision_request_id=(
            str(row["revision_request_id"])
            if row.get("revision_request_id") is not None
            else None
        ),
        requested_by=(
            str(row["requested_by"])
            if row.get("requested_by") is not None
            else None
        ),
        content_generation_claim_version=row.get("content_generation_claim_version"),
    )


def _validate_row_against_request(
    row: Mapping[str, Any],
    *,
    request: ReviewableDocumentPackageStagingRequest,
    template_hash: str,
) -> None:
    expected: Mapping[str, object] = {
        "idempotency_key": request.idempotency_key,
        "run_id": request.run_id,
        "graph_id": request.graph_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "task_input_hash": request.task_input_hash,
        "case_snapshot_hash": request.case_snapshot_hash,
        "binding_hash": request.binding_hash,
        "source_set_hash": request.source_set_hash,
        "authorized_source_refs": list(request.authorized_source_refs),
        "authorized_source_refs_hash": _authorized_source_refs_hash(
            request.authorized_source_refs
        ),
        "authorized_source_manifest": _authorized_source_manifest_payload(
            request.authorized_source_manifest
        ),
        "candidate_hash": request.candidate_hash,
        "work_plan_id": request.work_plan_id,
        "work_plan_hash": request.work_plan_hash,
        "work_plan_item_id": request.work_plan_item_id,
        "posture_profile_id": request.posture_profile_id,
        "posture_profile_hash": request.posture_profile_hash,
        "template_id": request.template_id,
        "template_version": request.template_version,
        "template_hash": request.template_hash,
        "deliverable_kind": request.deliverable_kind,
        "output_format": request.output_format.value,
        "review_status": "NEEDS_LAWYER_REVIEW",
        "candidate_content_sha256": request.candidate_content_sha256,
        "candidate_byte_size": len(request.candidate_bytes),
        "editable_sha256": request.editable_sha256,
        "editable_byte_size": len(request.editable_bytes),
        "editable_media_type": request.editable_media_type,
        "review_pdf_sha256": request.review_pdf_sha256,
        "review_pdf_byte_size": len(request.review_pdf_bytes),
        "review_pdf_page_count": request.review_pdf_page_count,
        "render_verification_hash": request.render_verification_hash,
        "review_input_hash": request.review_input_hash,
        "generation_mode": request.generation_mode,
        "revision_number": request.revision_number,
        "root_package_id": request.root_package_id,
        "supersedes_package_id": request.supersedes_package_id,
        "revision_request_id": request.revision_request_id,
        "requested_by": request.requested_by,
        "content_generation_claim_version": request.content_generation_claim_version,
    }
    for key, value in expected.items():
        if key == "generation_mode":
            actual = row.get(key, _INITIAL_GENERATION_MODE)
        elif key == "revision_number":
            actual = row.get(key, 1)
        elif key in {
            "root_package_id",
            "supersedes_package_id",
            "revision_request_id",
            "requested_by",
            "content_generation_claim_version",
        }:
            actual = row.get(key)
        else:
            actual = row[key]
        if key.endswith("_id") and actual is not None:
            actual = str(actual)
        if isinstance(value, int):
            actual = int(actual)
        if actual != value:
            raise CaseAgentDocumentPackageBlocked(
                "idempotency key is already bound to another document package"
            )
    if template_hash != request.template_hash:
        raise CaseAgentDocumentPackageBlocked(
            "installed document template differs from the staging request"
        )
    if str(row["candidate_artifact_kind"]) != _ARTIFACT_KINDS[0]:
        raise CaseAgentDocumentPackageBlocked("candidate artifact kind differs")
    if str(row["editable_artifact_kind"]) != _ARTIFACT_KINDS[1]:
        raise CaseAgentDocumentPackageBlocked("editable artifact kind differs")
    if str(row["review_pdf_artifact_kind"]) != _ARTIFACT_KINDS[2]:
        raise CaseAgentDocumentPackageBlocked("PDF artifact kind differs")


def _row_object_receipts(
    row: Mapping[str, Any]
) -> tuple[tuple[str, PrivateDocumentObjectReceipt, bytes], ...]:
    return (
        (
            "candidate",
            PrivateDocumentObjectReceipt(
                object_key=str(row["candidate_object_key"]),
                content_sha256=str(row["candidate_content_sha256"]),
                byte_size=int(row["candidate_byte_size"]),
                media_type=str(row["candidate_media_type"]),
                object_version_id=(
                    str(row["candidate_object_version_id"])
                    if row["candidate_object_version_id"] is not None
                    else None
                ),
            ),
            b"",
        ),
        (
            "editable",
            PrivateDocumentObjectReceipt(
                object_key=str(row["editable_object_key"]),
                content_sha256=str(row["editable_sha256"]),
                byte_size=int(row["editable_byte_size"]),
                media_type=str(row["editable_media_type"]),
                object_version_id=(
                    str(row["editable_object_version_id"])
                    if row["editable_object_version_id"] is not None
                    else None
                ),
            ),
            b"",
        ),
        (
            "pdf-preview",
            PrivateDocumentObjectReceipt(
                object_key=str(row["review_pdf_object_key"]),
                content_sha256=str(row["review_pdf_sha256"]),
                byte_size=int(row["review_pdf_byte_size"]),
                media_type=str(row["review_pdf_media_type"]),
                object_version_id=(
                    str(row["review_pdf_object_version_id"])
                    if row["review_pdf_object_version_id"] is not None
                    else None
                ),
            ),
            b"",
        ),
    )


def _role_columns(role: str) -> tuple[str, str]:
    return {
        "candidate": ("candidate_artifact_id", "candidate_artifact_kind"),
        "editable": ("editable_artifact_id", "editable_artifact_kind"),
        "pdf-preview": ("review_pdf_artifact_id", "review_pdf_artifact_kind"),
    }[role]


def _read_staging_binding(
    connection: Any,
    *,
    worker: Actor,
    request: ReviewableDocumentPackageStagingRequest,
) -> Mapping[str, Any]:
    if request.generation_mode == _INITIAL_GENERATION_MODE:
        return _read_current_staging_binding(
            connection, worker=worker, request=request
        )
    if request.generation_mode in _REVISION_GENERATION_MODES:
        return _read_current_revision_staging_binding(
            connection, worker=worker, request=request
        )
    raise CaseAgentDocumentPackageBlocked(
        "document package generation mode is invalid"
    )


def _read_current_revision_staging_binding(
    connection: Any,
    *,
    worker: Actor,
    request: ReviewableDocumentPackageStagingRequest,
) -> Mapping[str, Any]:
    content_revision = request.generation_mode == _CONTENT_REVISION_GENERATION_MODE
    revision_replay = request.generation_mode in _REVISION_GENERATION_MODES
    inbox_join = """JOIN case_agent_document_revision_inbox inbox
          ON inbox.request_id = revision_request.request_id
         AND inbox.firm_id = revision_request.firm_id
         AND inbox.matter_id = revision_request.matter_id"""
    content_predicates = ""
    extra_parameters: tuple[object, ...] = ()
    if content_revision:
        inbox_join = """JOIN case_agent_document_content_generation_jobs inbox
          ON inbox.review_id = revision_request.content_generation_review_id
         AND inbox.firm_id = revision_request.firm_id AND inbox.matter_id = revision_request.matter_id
         AND inbox.run_id = revision_request.run_id
        JOIN case_agent_document_content_generation_reviews generation_review
          ON generation_review.review_id = inbox.review_id AND generation_review.firm_id = inbox.firm_id
         AND generation_review.matter_id = inbox.matter_id AND generation_review.run_id = inbox.run_id"""
        content_predicates = """AND inbox.state = 'RENDERING' AND inbox.claim_version = %s
          AND generation_review.candidate_hash = %s AND generation_review.binding_hash = %s
          AND predecessor.binding_hash = generation_review.binding_hash
          AND generation_review.reviewed_by = revision_request.requested_by
          AND generation_review.root_package_id = revision_request.root_package_id
          AND generation_review.expected_revision_number = revision_request.expected_revision_number
          AND generation_review.purpose = 'GENERATE_REVIEW_COPY'
          AND EXISTS (SELECT 1 FROM users reviewer JOIN matter_actor_roles authority
            ON authority.user_id = reviewer.user_id AND authority.firm_id = reviewer.firm_id
            WHERE reviewer.user_id = generation_review.reviewed_by AND reviewer.firm_id = inbox.firm_id
              AND reviewer.status = 'ACTIVE' AND authority.matter_id = inbox.matter_id
              AND authority.revoked_at IS NULL AND authority.role IN ('LEAD_LAWYER','REVIEWER'))"""
        extra_parameters = (request.content_generation_claim_version, request.candidate_hash, request.binding_hash)
    row = connection.execute(
        f"""
        SELECT predecessor.matter_id, predecessor.package_receipt_hash,
               predecessor.revision_number AS predecessor_revision_number,
               predecessor.root_package_id AS predecessor_root_package_id,
               predecessor.generation_mode AS predecessor_generation_mode,
               run.snapshot_hash, run.snapshot_matter_version,
               run.current_graph_id, run.current_graph_hash,
               run.status AS run_status, run.is_stale, run.is_cancelled,
               graph.graph_hash, graph.snapshot_hash AS graph_snapshot_hash,
               task.input_hash, task.input_refs, task.skill_id, task.tool_id,
               task.writes_managed_derivatives, task.approval_gate,
               head.status AS head_status, head.is_current,
               attempt.status AS attempt_status, matter.version AS matter_version,
               plan.plan_hash, plan.plan_version, plan.profile_id, plan.profile_hash,
               profile.profile_hash AS actual_profile_hash,
               profile.profile_version, plan.status AS plan_status,
               plan.activated_matter_version, plan_head.current_plan_id,
               profile_head.current_profile_id, item.item_kind, item.readiness,
               item.delivery_target, item.deliverable_kind,
               revision_request.root_package_id AS requested_root_package_id,
               revision_request.predecessor_package_id,
               revision_request.expected_revision_number,
               revision_request.target_template_id,
               revision_request.target_template_version,
               revision_request.target_template_hash,
               revision_request.source_package_receipt_hash,
               revision_request.requested_by AS revision_requested_by,
               inbox.state AS inbox_state, inbox.claimed_by,
               inbox.lease_expires_at, worker_user.status AS worker_status,
               bool_and(worker_role.role = 'SYSTEM_WORKER') AS worker_only,
               count(*) FILTER (WHERE worker_role.revoked_at IS NULL)
                   AS active_role_count
        FROM case_agent_document_revision_requests revision_request
        {inbox_join}
        JOIN case_agent_reviewable_document_packages predecessor
          ON predecessor.package_id = revision_request.predecessor_package_id
         AND predecessor.run_id = revision_request.run_id
         AND predecessor.firm_id = revision_request.firm_id
         AND predecessor.matter_id = revision_request.matter_id
        JOIN case_agent_runs run
          ON run.run_id = predecessor.run_id AND run.firm_id = predecessor.firm_id
         AND run.matter_id = predecessor.matter_id
        JOIN matters matter
          ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = predecessor.graph_id AND graph.run_id = run.run_id
         AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
        JOIN case_agent_tasks task
          ON task.graph_id = graph.graph_id AND task.task_id = predecessor.task_id
         AND task.run_id = run.run_id AND task.firm_id = run.firm_id
         AND task.matter_id = run.matter_id
        JOIN case_agent_task_heads head
          ON head.graph_id = task.graph_id AND head.task_id = task.task_id
         AND head.run_id = task.run_id AND head.firm_id = task.firm_id
         AND head.matter_id = task.matter_id
        JOIN case_agent_task_attempts attempt
          ON attempt.attempt_id = predecessor.attempt_id
         AND attempt.graph_id = task.graph_id AND attempt.task_id = task.task_id
         AND attempt.run_id = task.run_id AND attempt.firm_id = task.firm_id
         AND attempt.matter_id = task.matter_id
        JOIN case_work_plans plan
          ON plan.plan_id = predecessor.work_plan_id AND plan.firm_id = run.firm_id
         AND plan.matter_id = run.matter_id
        JOIN case_work_plan_heads plan_head
          ON plan_head.matter_id = plan.matter_id AND plan_head.firm_id = plan.firm_id
        JOIN case_work_plan_items item
          ON item.item_id = predecessor.work_plan_item_id
         AND item.plan_id = plan.plan_id AND item.firm_id = plan.firm_id
         AND item.matter_id = plan.matter_id
        JOIN case_posture_profiles profile
          ON profile.profile_id = predecessor.posture_profile_id
         AND profile.firm_id = plan.firm_id AND profile.matter_id = plan.matter_id
        JOIN case_posture_profile_heads profile_head
          ON profile_head.matter_id = profile.matter_id
         AND profile_head.firm_id = profile.firm_id
        JOIN users worker_user
          ON worker_user.user_id = %s AND worker_user.firm_id = run.firm_id
        JOIN matter_actor_roles worker_role
          ON worker_role.user_id = worker_user.user_id
         AND worker_role.firm_id = worker_user.firm_id
         AND worker_role.matter_id = run.matter_id
         AND worker_role.revoked_at IS NULL
        WHERE revision_request.request_id = %s
          AND revision_request.firm_id = %s
          AND inbox.lease_expires_at > clock_timestamp()
          {content_predicates}
        GROUP BY predecessor.matter_id, predecessor.package_receipt_hash,
                 predecessor.revision_number, predecessor.root_package_id,
                 predecessor.generation_mode, run.snapshot_hash,
                 run.snapshot_matter_version, run.current_graph_id,
                 run.current_graph_hash, run.status, run.is_stale,
                 run.is_cancelled, graph.graph_hash, graph.snapshot_hash,
                 task.input_hash, task.input_refs, task.skill_id, task.tool_id,
                 task.writes_managed_derivatives, task.approval_gate,
                 head.status, head.is_current, attempt.status, matter.version,
                 plan.plan_hash, plan.plan_version, plan.profile_id,
                 plan.profile_hash, profile.profile_hash,
                 profile.profile_version, plan.status,
                 plan.activated_matter_version, plan_head.current_plan_id,
                 profile_head.current_profile_id, item.item_kind,
                 item.readiness, item.delivery_target, item.deliverable_kind,
                 revision_request.root_package_id,
                 revision_request.predecessor_package_id,
                 revision_request.expected_revision_number,
                 revision_request.target_template_id,
                 revision_request.target_template_version,
                 revision_request.target_template_hash,
                 revision_request.source_package_receipt_hash,
                 revision_request.requested_by, inbox.state,
                 inbox.claimed_by, inbox.lease_expires_at,
                 worker_user.status
        """,
        (worker.actor_id, request.revision_request_id, worker.firm_id, *extra_parameters),
    ).fetchone()
    if row is None:
        raise CaseAgentDocumentPackageBlocked(
            "current document revision binding is unavailable"
        )
    refs = row["input_refs"]
    expected_refs = (f"work-plan-item:{request.work_plan_item_id}",)
    if not isinstance(refs, list) or tuple(refs) != expected_refs:
        raise CaseAgentDocumentPackageBlocked(
            "document revision task references are malformed"
        )
    predecessor_root = (
        str(row["predecessor_root_package_id"])
        if row["predecessor_root_package_id"] is not None
        else str(request.supersedes_package_id)
    )
    if (
        str(row["snapshot_hash"]) != request.case_snapshot_hash
        or str(row["graph_snapshot_hash"]) != request.case_snapshot_hash
        or str(row["input_hash"]) != request.task_input_hash
        or str(row["current_graph_id"]) != request.graph_id
        or str(row["current_graph_hash"]) != str(row["graph_hash"])
        or row["run_status"] != "READY_FOR_REVIEW"
        or bool(row["is_stale"])
        or bool(row["is_cancelled"])
        or int(row["matter_version"]) != int(row["snapshot_matter_version"])
        # A forward-only revision has independent authority: the exact current
        # package, source manifest, active plan, template and worker claim are
        # all checked below.  It must remain possible to re-render a safe
        # template update when the original read-only task produced the
        # package through a later deterministic delivery step.  This does not
        # permit new facts, a changed plan, a changed legal posture, external
        # action, approval, or submission.
        or (not revision_replay and not bool(row["writes_managed_derivatives"]))
        or (not revision_replay and row["approval_gate"] != "LAWYER_REVIEW")
        or row["head_status"] != "SUCCEEDED"
        or not bool(row["is_current"])
        or row["attempt_status"] != "SUCCEEDED"
        or row["worker_status"] != "ACTIVE"
        or not bool(row["worker_only"])
        or int(row["active_role_count"]) != 1
        or row["inbox_state"] != ("RENDERING" if content_revision else "LEASED")
        or str(row["claimed_by"]) != worker.actor_id
        or row["lease_expires_at"] is None
        or str(row["predecessor_package_id"]) != request.supersedes_package_id
        or str(row["requested_root_package_id"]) != request.root_package_id
        or predecessor_root != request.root_package_id
        or int(row["predecessor_revision_number"]) + 1
            != request.revision_number
        or int(row["expected_revision_number"])
            != request.revision_number - 1
        or str(row["source_package_receipt_hash"])
            != str(row["package_receipt_hash"])
        or str(row["revision_requested_by"]) != request.requested_by
        or str(row["target_template_id"]) != request.template_id
        or str(row["target_template_version"]) != request.template_version
        or str(row["target_template_hash"]) != request.template_hash
        or row["plan_status"] != "ACTIVE"
        or str(row["current_plan_id"]) != request.work_plan_id
        or str(row["plan_hash"]) != request.work_plan_hash
        or str(row["profile_id"]) != request.posture_profile_id
        or str(row["profile_hash"]) != request.posture_profile_hash
        or str(row["actual_profile_hash"]) != request.posture_profile_hash
        or str(row["current_profile_id"]) != request.posture_profile_id
        or int(row["activated_matter_version"])
            != int(row["snapshot_matter_version"])
        or row["item_kind"] != "DOCUMENT_CANDIDATE"
        or row["readiness"] != "ACTIONABLE"
        or row["delivery_target"] == "NOT_APPLICABLE"
        or row["deliverable_kind"] != request.deliverable_kind
    ):
        # This is a fail-closed boundary.  Keep the diagnostic limited to the
        # names of failed server-owned guards: it must never disclose a client
        # fact, source label, document paragraph, identifier, or hash in logs.
        guards = {
            "case_snapshot": str(row["snapshot_hash"]) == request.case_snapshot_hash and str(row["graph_snapshot_hash"]) == request.case_snapshot_hash,
            "task_input": str(row["input_hash"]) == request.task_input_hash,
            "current_graph": str(row["current_graph_id"]) == request.graph_id and str(row["current_graph_hash"]) == str(row["graph_hash"]),
            "run": row["run_status"] == "READY_FOR_REVIEW" and not bool(row["is_stale"]) and not bool(row["is_cancelled"]),
            "matter_version": int(row["matter_version"]) == int(row["snapshot_matter_version"]),
            "task": (revision_replay or bool(row["writes_managed_derivatives"])) and (revision_replay or row["approval_gate"] == "LAWYER_REVIEW") and row["head_status"] == "SUCCEEDED" and bool(row["is_current"]) and row["attempt_status"] == "SUCCEEDED",
            "worker": row["worker_status"] == "ACTIVE" and bool(row["worker_only"]) and int(row["active_role_count"]) == 1,
            "claim": row["inbox_state"] == ("RENDERING" if content_revision else "LEASED") and str(row["claimed_by"]) == worker.actor_id and row["lease_expires_at"] is not None,
            "predecessor": str(row["predecessor_package_id"]) == request.supersedes_package_id and str(row["requested_root_package_id"]) == request.root_package_id and predecessor_root == request.root_package_id and int(row["predecessor_revision_number"]) + 1 == request.revision_number,
            "revision": int(row["expected_revision_number"]) == request.revision_number - 1 and str(row["source_package_receipt_hash"]) == str(row["package_receipt_hash"]) and str(row["revision_requested_by"]) == request.requested_by,
            "template": str(row["target_template_id"]) == request.template_id and str(row["target_template_version"]) == request.template_version and str(row["target_template_hash"]) == request.template_hash,
            "plan": row["plan_status"] == "ACTIVE" and str(row["current_plan_id"]) == request.work_plan_id and str(row["plan_hash"]) == request.work_plan_hash and int(row["activated_matter_version"]) == int(row["snapshot_matter_version"]),
            "posture": str(row["profile_id"]) == request.posture_profile_id and str(row["profile_hash"]) == request.posture_profile_hash and str(row["actual_profile_hash"]) == request.posture_profile_hash and str(row["current_profile_id"]) == request.posture_profile_id,
            "deliverable": row["item_kind"] == "DOCUMENT_CANDIDATE" and row["readiness"] == "ACTIONABLE" and row["delivery_target"] != "NOT_APPLICABLE" and row["deliverable_kind"] == request.deliverable_kind,
        }
        mismatch = ",".join(name for name, matches in guards.items() if not matches) or "unclassified"
        raise CaseAgentDocumentPackageBlocked(
            f"document revision differs from the current review state: {mismatch}"
        )
    try:
        assert_case_work_plan_references_current(
            connection,
            actor=worker,
            matter_id=str(row["matter_id"]),
            plan_id=request.work_plan_id,
        )
    except Exception as error:
        raise CaseAgentDocumentPackageBlocked(
            "document revision sources changed after the Agent run"
        ) from error
    _assert_authorized_source_manifest_is_current(
        connection,
        firm_id=worker.firm_id,
        matter_id=str(row["matter_id"]),
        work_plan_id=request.work_plan_id,
        work_plan_item_id=request.work_plan_item_id,
        work_plan_version=int(row["plan_version"]),
        work_plan_hash=request.work_plan_hash,
        posture_profile_id=request.posture_profile_id,
        posture_profile_version=int(row["profile_version"]),
        posture_profile_hash=request.posture_profile_hash,
        manifest=request.authorized_source_manifest,
        independent_artifact_recheck=False,
    )
    return {
        "matter_id": str(row["matter_id"]),
        "graph_hash": str(row["graph_hash"]),
        "input_refs_hash": _canonical_hash(sorted(refs)),
        "input_refs": tuple(sorted(refs)),
        "skill_id": str(row["skill_id"]),
        "tool_id": str(row["tool_id"]),
        "revision_request_id": str(request.revision_request_id),
        "predecessor_package_id": str(request.supersedes_package_id),
        "revision_number": request.revision_number,
    }


def _read_current_staging_binding(
    connection: Any,
    *,
    worker: Actor,
    request: ReviewableDocumentPackageStagingRequest,
) -> Mapping[str, Any]:
    row = connection.execute(
        """
        SELECT run.matter_id, run.snapshot_hash, run.snapshot_matter_version,
               run.current_graph_id, run.current_graph_hash, run.status AS run_status,
               run.is_stale, run.is_cancelled,
               graph.graph_hash, graph.snapshot_hash AS graph_snapshot_hash,
               task.input_hash, task.input_refs, task.skill_id, task.tool_id,
               task.writes_managed_derivatives, task.approval_gate,
               head.status AS head_status, head.is_current,
               head.active_attempt_id,
               attempt.status AS attempt_status, matter.version AS matter_version,
               plan.plan_hash, plan.plan_version, plan.profile_id, plan.profile_hash,
               profile.profile_hash AS actual_profile_hash,
               profile.profile_version,
               plan.status AS plan_status, plan.activated_matter_version,
               plan_head.current_plan_id, profile_head.current_profile_id,
               item.item_kind, item.readiness, item.delivery_target,
               item.deliverable_kind,
               worker_user.status AS worker_status,
               bool_and(worker_role.role = 'SYSTEM_WORKER') AS worker_only,
               count(*) FILTER (WHERE worker_role.revoked_at IS NULL) AS active_role_count
        FROM case_agent_runs run
        JOIN matters matter
          ON matter.matter_id = run.matter_id AND matter.firm_id = run.firm_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = %s AND graph.run_id = run.run_id
         AND graph.firm_id = run.firm_id AND graph.matter_id = run.matter_id
        JOIN case_agent_tasks task
          ON task.graph_id = graph.graph_id AND task.task_id = %s
         AND task.run_id = run.run_id AND task.firm_id = run.firm_id
         AND task.matter_id = run.matter_id
        JOIN case_agent_task_heads head
          ON head.graph_id = task.graph_id AND head.task_id = task.task_id
         AND head.run_id = task.run_id AND head.firm_id = task.firm_id
         AND head.matter_id = task.matter_id
        JOIN case_agent_task_attempts attempt
          ON attempt.attempt_id = %s AND attempt.graph_id = task.graph_id
         AND attempt.task_id = task.task_id AND attempt.run_id = task.run_id
         AND attempt.firm_id = task.firm_id AND attempt.matter_id = task.matter_id
        JOIN case_work_plans plan
          ON plan.plan_id = %s AND plan.firm_id = run.firm_id
         AND plan.matter_id = run.matter_id
        JOIN case_work_plan_heads plan_head
          ON plan_head.matter_id = plan.matter_id AND plan_head.firm_id = plan.firm_id
        JOIN case_work_plan_items item
          ON item.item_id = %s AND item.plan_id = plan.plan_id
         AND item.firm_id = plan.firm_id AND item.matter_id = plan.matter_id
        JOIN case_posture_profiles profile
          ON profile.profile_id = %s AND profile.firm_id = plan.firm_id
         AND profile.matter_id = plan.matter_id
        JOIN case_posture_profile_heads profile_head
          ON profile_head.matter_id = profile.matter_id
         AND profile_head.firm_id = profile.firm_id
        JOIN users worker_user
          ON worker_user.user_id = %s AND worker_user.firm_id = run.firm_id
        JOIN matter_actor_roles worker_role
          ON worker_role.user_id = worker_user.user_id
         AND worker_role.firm_id = worker_user.firm_id
         AND worker_role.matter_id = run.matter_id
         AND worker_role.revoked_at IS NULL
        WHERE run.run_id = %s AND run.firm_id = %s
        GROUP BY run.matter_id, run.snapshot_hash, run.snapshot_matter_version,
                 run.current_graph_id, run.current_graph_hash, run.status,
                 run.is_stale, run.is_cancelled, graph.graph_hash,
                 graph.snapshot_hash, task.input_hash, task.input_refs,
                 task.skill_id, task.tool_id, task.writes_managed_derivatives,
                 task.approval_gate, head.status, head.is_current,
                 head.active_attempt_id, attempt.status, matter.version,
                 plan.plan_hash, plan.plan_version, plan.profile_id, plan.profile_hash,
                 profile.profile_hash, profile.profile_version,
                 plan.status, plan.activated_matter_version,
                 plan_head.current_plan_id, profile_head.current_profile_id,
                 item.item_kind, item.readiness, item.delivery_target,
                 item.deliverable_kind, worker_user.status
        """,
        (
            request.graph_id,
            request.task_id,
            request.attempt_id,
            request.work_plan_id,
            request.work_plan_item_id,
            request.posture_profile_id,
            worker.actor_id,
            request.run_id,
            worker.firm_id,
        ),
    ).fetchone()
    if row is None:
        raise CaseAgentDocumentPackageBlocked(
            "current document task binding is unavailable"
        )
    refs = row["input_refs"]
    if not isinstance(refs, list):
        raise CaseAgentDocumentPackageBlocked("document task references are malformed")
    expected_refs = (f"work-plan-item:{request.work_plan_item_id}",)
    if tuple(refs) != expected_refs:
        raise CaseAgentDocumentPackageBlocked(
            "document task does not contain exactly one active plan aggregate"
        )
    if (
        str(row["snapshot_hash"]) != request.case_snapshot_hash
        or str(row["graph_snapshot_hash"]) != request.case_snapshot_hash
        or str(row["input_hash"]) != request.task_input_hash
        or str(row["current_graph_id"]) != request.graph_id
        or str(row["current_graph_hash"]) != str(row["graph_hash"])
        or row["run_status"] != "EXECUTING"
        or bool(row["is_stale"])
        or bool(row["is_cancelled"])
        or int(row["matter_version"]) != int(row["snapshot_matter_version"])
        or not bool(row["writes_managed_derivatives"])
        # The activated plan authorizes one bounded, local generation of a
        # review candidate.  Lawyer review applies to the resulting package,
        # not to the reversible renderer task itself.
        or row["approval_gate"] != "NONE"
        or row["head_status"] != "RUNNING"
        or not bool(row["is_current"])
        or str(row["active_attempt_id"]) != request.attempt_id
        or row["attempt_status"] != "RUNNING"
        or row["worker_status"] != "ACTIVE"
        or not bool(row["worker_only"])
        or int(row["active_role_count"]) != 1
        or row["plan_status"] != "ACTIVE"
        or str(row["current_plan_id"]) != request.work_plan_id
        or str(row["plan_hash"]) != request.work_plan_hash
        or str(row["profile_id"]) != request.posture_profile_id
        or str(row["profile_hash"]) != request.posture_profile_hash
        or str(row["actual_profile_hash"]) != request.posture_profile_hash
        or str(row["current_profile_id"]) != request.posture_profile_id
        or int(row["activated_matter_version"]) != int(row["snapshot_matter_version"])
        or row["item_kind"] != "DOCUMENT_CANDIDATE"
        or row["readiness"] != "ACTIONABLE"
        or row["delivery_target"] == "NOT_APPLICABLE"
        or row["deliverable_kind"] != request.deliverable_kind
    ):
        raise CaseAgentDocumentPackageBlocked(
            "document package differs from the current task or dynamic work plan"
        )
    try:
        assert_case_work_plan_references_current(
            connection,
            actor=worker,
            matter_id=str(row["matter_id"]),
            plan_id=request.work_plan_id,
        )
    except Exception as error:
        raise CaseAgentDocumentPackageBlocked(
            "document package sources changed after dynamic planning"
        ) from error
    _assert_authorized_source_manifest_is_current(
        connection,
        firm_id=worker.firm_id,
        matter_id=str(row["matter_id"]),
        work_plan_id=request.work_plan_id,
        work_plan_item_id=request.work_plan_item_id,
        work_plan_version=int(row["plan_version"]),
        work_plan_hash=request.work_plan_hash,
        posture_profile_id=request.posture_profile_id,
        posture_profile_version=int(row["profile_version"]),
        posture_profile_hash=request.posture_profile_hash,
        manifest=request.authorized_source_manifest,
        independent_artifact_recheck=False,
    )
    return {
        "matter_id": str(row["matter_id"]),
        "graph_hash": str(row["graph_hash"]),
        "input_refs_hash": _canonical_hash(sorted(refs)),
        "input_refs": tuple(sorted(refs)),
        "skill_id": str(row["skill_id"]),
        "tool_id": str(row["tool_id"]),
    }


_CURRENT_SOURCE_IDENTITY = {
    "CASE_FACT": ("fact", DocumentSourceKind.CONFIRMED_FACT.value),
    "CLAIM": ("claim", DocumentSourceKind.CONFIRMED_CLAIM.value),
    "DISPUTE_ISSUE": ("issue", DocumentSourceKind.CONFIRMED_ISSUE.value),
    "TRANSACTION": (
        "transaction",
        DocumentSourceKind.CONFIRMED_TRANSACTION.value,
    ),
    "LEGAL_EVENT": (
        "legal-event",
        DocumentSourceKind.CONFIRMED_PROCEDURAL_EVENT.value,
    ),
    "LEGAL_RULE_VERSION": (
        "legal-rule",
        DocumentSourceKind.APPROVED_LEGAL_RULE.value,
    ),
    "LEGAL_SOURCE_SNAPSHOT": (
        "legal-source",
        DocumentSourceKind.VERIFIED_LEGAL_SOURCE.value,
    ),
    "CALCULATION_RUN": (
        "calculation",
        DocumentSourceKind.APPROVED_CALCULATION.value,
    ),
    "EVIDENCE_PAGE": (
        "evidence-page",
        DocumentSourceKind.APPROVED_EVIDENCE_ITEM.value,
    ),
}


def _current_promoted_source_identity(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    work_plan_id: str,
    reference: Mapping[str, Any],
) -> tuple[str, tuple[str, str, str]]:
    """Resolve one immutable Agent input binding to its current ledger object.

    Promotion references deliberately expose a binding UUID and binding hash,
    not a client fact/transaction UUID.  The document manifest, however, is
    expressed in the direct source identities disclosed to the renderer.  This
    bridge re-checks the active promotion and unwraps only the two confirmed
    ledger projections supported by the first document release.
    """

    binding = connection.execute(
        """
        SELECT binding.object_type, binding.object_id, binding.object_version,
               binding.source_status, binding.reference_use, binding.content_hash,
               promotion.snapshot_matter_version,
               fact.fact_id, fact.status AS fact_status, fact.decision_hash,
               ledger_transaction.transaction_id,
               ledger_transaction.status AS transaction_status,
               ledger_transaction.confirmation_hash,
               evidence_page.evidence_page_id,
               evidence_original.original_file_sha256,
               evidence_decision.status AS evidence_status,
               evidence_decision.disposition AS evidence_disposition
        FROM case_agent_work_plan_input_bindings binding
        JOIN case_agent_work_plan_promotions promotion
          ON promotion.promotion_id = binding.promotion_id
         AND promotion.plan_id = binding.plan_id
         AND promotion.firm_id = binding.firm_id
         AND promotion.matter_id = binding.matter_id
        JOIN case_work_plans plan
          ON plan.plan_id = binding.plan_id
         AND plan.firm_id = binding.firm_id
         AND plan.matter_id = binding.matter_id
        JOIN case_work_plan_heads head
          ON head.current_plan_id = plan.plan_id
         AND head.firm_id = plan.firm_id
         AND head.matter_id = plan.matter_id
        LEFT JOIN case_facts fact
          ON binding.object_type = 'CASE_FACT'
         AND fact.fact_id = binding.object_id
         AND fact.firm_id = binding.firm_id
         AND fact.matter_id = binding.matter_id
        LEFT JOIN case_transactions ledger_transaction
          ON binding.object_type = 'CASE_TRANSACTION'
         AND ledger_transaction.transaction_id = binding.object_id
         AND ledger_transaction.firm_id = binding.firm_id
         AND ledger_transaction.matter_id = binding.matter_id
        LEFT JOIN evidence_pages evidence_page
          ON binding.object_type = 'EVIDENCE_PAGE'
         AND evidence_page.evidence_page_id = binding.object_id
         AND evidence_page.firm_id = binding.firm_id
         AND evidence_page.matter_id = binding.matter_id
        LEFT JOIN evidence_original_files evidence_original
          ON evidence_original.evidence_file_id = evidence_page.evidence_file_id
         AND evidence_original.firm_id = evidence_page.firm_id
         AND evidence_original.matter_id = evidence_page.matter_id
        LEFT JOIN LATERAL (
            SELECT decision.status, decision.disposition
            FROM evidence_page_decisions decision
            WHERE decision.evidence_page_id = evidence_page.evidence_page_id
              AND decision.firm_id = evidence_page.firm_id
              AND decision.matter_id = evidence_page.matter_id
            ORDER BY CASE WHEN decision.status = 'APPROVED' THEN 0 ELSE 1 END,
                     decision.created_at DESC, decision.decision_id DESC
            LIMIT 1
        ) evidence_decision ON true
        WHERE binding.binding_id = %s
          AND binding.plan_id = %s
          AND binding.firm_id = %s
          AND binding.matter_id = %s
          AND binding.object_version = %s
          AND binding.binding_hash = %s
          AND binding.reference_use = %s
          AND plan.status = 'ACTIVE'
        """,
        (
            reference["source_id"],
            work_plan_id,
            firm_id,
            matter_id,
            reference["source_version"],
            reference["source_hash"],
            reference["reference_use"],
        ),
    ).fetchone()
    if binding is None:
        raise CaseAgentDocumentPackageBlocked(
            "promoted document source is not bound to the current active plan"
        )
    object_type = str(binding["object_type"])
    expected_use = {
        "CASE_FACT": "FACT",
        "CASE_TRANSACTION": "TRANSACTION",
        "CASE_CLAIM": "CLAIM_SCOPE",
        "EVIDENCE_PAGE": "EVIDENCE",
        "VERIFIED_LEGAL_SOURCE": "LEGAL_AUTHORITY",
        "APPROVED_LEGAL_RULE": "LEGAL_RULE",
    }.get(object_type)
    legal = object_type in {"VERIFIED_LEGAL_SOURCE", "APPROVED_LEGAL_RULE"}
    if (
        expected_use is None
        or str(binding["source_status"]) != ("LOCKED" if legal else "CONFIRMED")
        or str(binding["reference_use"]) != expected_use
        or str(binding["object_version"])
        != ("v1" if legal else f"v{int(binding['snapshot_matter_version'])}")
    ):
        raise CaseAgentDocumentPackageBlocked(
            "promoted document source type is not approved for disclosure"
        )
    if object_type == "CASE_CLAIM":
        row = connection.execute(
            """SELECT claim.confirmation_hash
               FROM case_claims claim
               JOIN case_claim_responses response
                 ON response.claim_id=claim.claim_id AND response.firm_id=claim.firm_id
                AND response.matter_id=claim.matter_id
               WHERE claim.claim_id=%s AND claim.firm_id=%s AND claim.matter_id=%s
                 AND claim.status='CONFIRMED_SCOPE' AND claim.confirmation_hash IS NOT NULL
                 AND response.approval_hash IS NOT NULL AND response.approved_by IS NOT NULL
                 AND EXISTS (SELECT 1 FROM case_claim_response_facts f
                     WHERE f.claim_response_id=response.claim_response_id
                       AND f.firm_id=claim.firm_id AND f.matter_id=claim.matter_id)""",
            (binding["object_id"], firm_id, matter_id),
        ).fetchone()
        if row is None:
            raise CaseAgentDocumentPackageBlocked("promoted claim response is no longer approved")
        return (f"claim:{binding['object_id']}",
                (DocumentSourceKind.CONFIRMED_CLAIM.value,
                 str(binding["object_version"]), str(row["confirmation_hash"])))
    if object_type == "EVIDENCE_PAGE":
        if (
            binding.get("evidence_page_id") is None
            or binding.get("original_file_sha256") is None
            or str(binding.get("evidence_status")) != "APPROVED"
            or str(binding.get("evidence_disposition")) != "INCLUDE"
        ):
            raise CaseAgentDocumentPackageBlocked(
                "promoted document source is not approved for disclosure"
            )
        # A catalogue's authoritative identity is its immutable original
        # source file plus the plan-bound page identity. Raster hashes belong
        # to visual-OCR processing and are not required for ordinary PDF
        # intake or a review-only evidence catalogue.
        return (
            f"evidence-page:{binding['evidence_page_id']}",
            (
                DocumentSourceKind.APPROVED_EVIDENCE_ITEM.value,
                str(binding["object_version"]),
                str(binding["original_file_sha256"]),
            ),
        )
    if legal:
        if object_type == "VERIFIED_LEGAL_SOURCE":
            row = connection.execute(
                """SELECT source.content_sha256 AS source_hash, 'v1' AS source_version
                   FROM official_legal_source_snapshots source
                   WHERE source.snapshot_id=%s AND source.firm_id=%s
                     AND source.verification_status='VERIFIED' AND source.license_status='ACTIVE'
                     AND source.license_review_hash IS NOT NULL
                     AND EXISTS (SELECT 1 FROM case_legal_bundles b
                       JOIN case_legal_bundle_segments s ON s.bundle_id=b.bundle_id
                        AND s.firm_id=b.firm_id AND s.matter_id=b.matter_id
                       WHERE b.firm_id=source.firm_id AND b.matter_id=%s AND b.status='APPROVED'
                         AND ((s.source_snapshot_id=source.snapshot_id AND s.source_sha256=source.content_sha256)
                           OR (s.parameter_source_snapshot_id=source.snapshot_id
                             AND s.parameter_source_sha256=source.content_sha256)))""",
                (binding["object_id"], firm_id, matter_id),
            ).fetchone()
            prefix, kind = "legal-source", DocumentSourceKind.VERIFIED_LEGAL_SOURCE.value
        else:
            row = connection.execute(
                """SELECT rule.approval_hash AS source_hash, rule.rule_version AS source_version
                   FROM legal_rule_versions rule
                   WHERE rule.rule_version_id=%s AND rule.firm_id=%s AND rule.status='APPROVED'
                     AND rule.approval_hash IS NOT NULL
                     AND EXISTS (SELECT 1 FROM case_legal_bundles b
                       JOIN case_legal_bundle_segments s ON s.bundle_id=b.bundle_id
                        AND s.firm_id=b.firm_id AND s.matter_id=b.matter_id
                       WHERE b.firm_id=rule.firm_id AND b.matter_id=%s AND b.status='APPROVED'
                         AND s.rule_version_id=rule.rule_version_id AND s.rule_version=rule.rule_version)""",
                (binding["object_id"], firm_id, matter_id),
            ).fetchone()
            prefix, kind = "legal-rule", DocumentSourceKind.APPROVED_LEGAL_RULE.value
        if row is None or str(row["source_hash"]) != str(binding["content_hash"]):
            raise CaseAgentDocumentPackageBlocked("promoted legal source is no longer approved for this matter")
        return (f"{prefix}:{binding['object_id']}",
                (kind, str(row["source_version"]), str(row["source_hash"])))
    if object_type == "CASE_FACT":
        if (
            binding["fact_id"] is None
            or str(binding["fact_status"]) != "CONFIRMED"
            or binding["decision_hash"] is None
        ):
            raise CaseAgentDocumentPackageBlocked(
                "promoted confirmed fact is no longer current"
            )
        return (
            f"fact:{binding['fact_id']}",
            (
                DocumentSourceKind.CONFIRMED_FACT.value,
                str(binding["object_version"]),
                str(binding["decision_hash"]),
            ),
        )
    if (
        binding["transaction_id"] is None
        or str(binding["transaction_status"]) != "CONFIRMED"
        or binding["confirmation_hash"] is None
    ):
        raise CaseAgentDocumentPackageBlocked(
            "promoted confirmed transaction is no longer current"
        )
    return (
        f"transaction:{binding['transaction_id']}",
        (
            DocumentSourceKind.CONFIRMED_TRANSACTION.value,
            str(binding["object_version"]),
            str(binding["confirmation_hash"]),
        ),
    )


def _assert_authorized_source_manifest_is_current(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    work_plan_id: str,
    work_plan_item_id: str,
    work_plan_version: int,
    work_plan_hash: str,
    posture_profile_id: str,
    posture_profile_version: int,
    posture_profile_hash: str,
    manifest: tuple[AuthorizedDocumentSourceBinding, ...],
    independent_artifact_recheck: bool = True,
) -> None:
    """Match the persisted manifest to the current server-owned plan expansion.

    Labels and model-visible text are immutable through ``source_set_hash``.
    Current authorization is checked from the active posture, active plan and
    every governed reference identity; browser/model refs are never accepted.
    """

    manifest = _authorized_source_manifest(manifest)
    expected: dict[str, tuple[str, str, str]] = {
        f"posture-profile:{posture_profile_id}": (
            DocumentSourceKind.POSTURE_PROFILE.value,
            f"v{posture_profile_version}",
            posture_profile_hash,
        ),
        f"work-plan-item:{work_plan_item_id}": (
            DocumentSourceKind.WORK_PLAN_ITEM.value,
            f"v{work_plan_version}",
            work_plan_hash,
        ),
    }
    rows = connection.execute(
        """
        SELECT source_type, source_id, source_version, source_hash,
               reference_use
        FROM case_work_plan_item_references
        WHERE plan_id = %s AND item_id = %s AND firm_id = %s AND matter_id = %s
        ORDER BY source_type, source_id, source_version, source_hash
        """,
        (work_plan_id, work_plan_item_id, firm_id, matter_id),
    ).fetchall()
    if not rows:
        raise CaseAgentDocumentPackageBlocked(
            "current document work-plan source set is unavailable"
        )
    for row in rows:
        source_type = str(row["source_type"])
        if source_type in {"POSTURE_PROFILE", "LAWYER_OBJECTIVE"}:
            continue
        if source_type == "AGENT_TASK_INPUT":
            input_ref, current = _current_promoted_source_identity(
                connection,
                firm_id=firm_id,
                matter_id=matter_id,
                work_plan_id=work_plan_id,
                reference=row,
            )
        else:
            identity = _CURRENT_SOURCE_IDENTITY.get(source_type)
            if identity is None:
                raise CaseAgentDocumentPackageBlocked(
                    "current document work-plan source type is unsupported"
                )
            prefix, source_kind = identity
            input_ref = f"{prefix}:{row['source_id']}"
            current = (
                source_kind,
                str(row["source_version"]),
                str(row["source_hash"]),
            )
        prior = expected.get(input_ref)
        if prior is not None and prior != current:
            raise CaseAgentDocumentPackageBlocked(
                "current document work-plan contains conflicting source identities"
            )
        expected[input_ref] = current
    actual = {
        item.input_ref: (item.source_kind, item.source_version, item.source_hash)
        for item in manifest
    }
    if any(actual.get(input_ref) != current for input_ref, current in expected.items()):
        raise CaseAgentDocumentPackageBlocked(
            "authorized document source manifest differs from the current plan"
        )
    lawyer_packages = tuple(
        item
        for item in manifest
        if item.source_kind
        == DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE.value
    )
    if len(lawyer_packages) > 1:
        raise CaseAgentDocumentPackageBlocked(
            "document package contains multiple lawyer decision packages"
        )
    for item in lawyer_packages:
        if not item.input_ref.startswith("lawyer-decision-package:"):
            raise CaseAgentDocumentPackageBlocked(
                "lawyer decision package source reference is invalid"
            )
        artifact_id = item.input_ref.removeprefix("lawyer-decision-package:")
        _uuid(artifact_id, "lawyer decision package artifact_id")
        if re.fullmatch(
            r"verified-[0-9a-f]{64}", item.source_version
        ) is None:
            raise CaseAgentDocumentPackageBlocked(
                "lawyer decision package verification identity is invalid"
            )
        verification_hash = item.source_version.removeprefix("verified-")
        if not independent_artifact_recheck:
            # The execution principal intentionally cannot read verifier-owned
            # history. The binding port already re-read these exact bytes with
            # the verifier principal; this staging write remains unapproved.
            # The independent package verifier calls the default mode and
            # rechecks PASSED lineage before accepting any artifact.
            expected[item.input_ref] = (
                item.source_kind,
                item.source_version,
                item.source_hash,
            )
            continue
        verified_rows = connection.execute(
            """
            SELECT artifact.content_hash, verification.verification_hash
            FROM case_agent_artifacts artifact
            JOIN case_agent_verification_receipts verification
              ON verification.run_id = artifact.run_id
             AND verification.firm_id = artifact.firm_id
             AND verification.matter_id = artifact.matter_id
            CROSS JOIN LATERAL jsonb_array_elements(
                verification.artifact_lineage
            ) AS lineage(value)
            WHERE artifact.artifact_id = %s
              AND artifact.firm_id = %s
              AND artifact.matter_id = %s
              AND artifact.artifact_kind = %s
              AND artifact.artifact_id::text = lineage.value->>'artifact_id'
              AND artifact.artifact_kind = lineage.value->>'artifact_kind'
              AND artifact.content_hash = lineage.value->>'content_hash'
              AND artifact.source_input_hash =
                  lineage.value->>'source_input_hash'
              AND artifact.byte_size =
                  (lineage.value->>'byte_size')::bigint
              AND verification.outcome = 'PASSED'
              AND verification.verification_hash = %s
              AND verification.verifier_actor_id <>
                  verification.execution_actor_id
            ORDER BY verification.persisted_at DESC
            LIMIT 2
            """,
            (
                artifact_id,
                firm_id,
                matter_id,
                LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
                verification_hash,
            ),
        ).fetchall()
        if (
            len(verified_rows) != 1
            or str(verified_rows[0]["content_hash"]) != item.source_hash
            or str(verified_rows[0]["verification_hash"])
            != verification_hash
        ):
            raise CaseAgentDocumentPackageBlocked(
                "lawyer decision package is no longer independently verified"
            )
        expected[item.input_ref] = (
            item.source_kind,
            item.source_version,
            item.source_hash,
        )
    if actual != expected:
        raise CaseAgentDocumentPackageBlocked(
            "authorized document source manifest differs from the current plan"
        )


def _read_package_row(
    connection: Any,
    *,
    firm_id: str,
    idempotency_key: str,
    graph_id: str,
    task_id: str,
    generation_mode: str,
    revision_request_id: str | None,
) -> Mapping[str, Any] | None:
    if generation_mode == _INITIAL_GENERATION_MODE:
        secondary_sql = "(generation_mode = 'INITIAL_AGENT_TASK' AND graph_id = %s AND task_id = %s)"
        secondary_parameters: tuple[object, ...] = (graph_id, task_id)
    elif generation_mode in _REVISION_GENERATION_MODES and revision_request_id is not None:
        secondary_sql = "revision_request_id = %s"
        secondary_parameters = (revision_request_id,)
    else:
        raise CaseAgentDocumentPackageBlocked("document package generation mode is invalid")
    return connection.execute(
        f"""
        SELECT *
        FROM case_agent_reviewable_document_packages
        WHERE firm_id = %s
          AND (idempotency_key = %s OR {secondary_sql})
        LIMIT 1
        """,
        (firm_id, idempotency_key, *secondary_parameters),
    ).fetchone()


_INSERT_PACKAGE_SQL = """
INSERT INTO case_agent_reviewable_document_packages (
    package_id, idempotency_key, run_id, graph_id, task_id, attempt_id,
    firm_id, matter_id, task_input_hash, case_snapshot_hash, binding_hash,
    source_set_hash, authorized_source_refs, authorized_source_refs_hash,
    authorized_source_manifest,
    candidate_hash, work_plan_id, work_plan_hash,
    work_plan_item_id, posture_profile_id, posture_profile_hash,
    template_id, template_version, template_hash, deliverable_kind,
    output_format, review_status,
    candidate_artifact_id, candidate_artifact_kind, candidate_media_type,
    candidate_content_sha256, candidate_byte_size, candidate_object_key,
    candidate_object_version_id,
    editable_artifact_id, editable_artifact_kind, editable_media_type,
    editable_sha256, editable_byte_size, editable_object_key,
    editable_object_version_id,
    review_pdf_artifact_id, review_pdf_artifact_kind, review_pdf_media_type,
    review_pdf_sha256, review_pdf_byte_size, review_pdf_page_count,
    review_pdf_object_key, review_pdf_object_version_id,
    render_verification_hash, review_input_hash, package_receipt_hash,
    generation_mode, revision_number, root_package_id,
    supersedes_package_id, revision_request_id, requested_by, staged_by
) VALUES (
    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
    %s,
    %s,%s,%s,%s,%s,'NEEDS_LAWYER_REVIEW',%s,%s,'application/json',%s,%s,%s,%s,
    %s,%s,%s,%s,%s,%s,%s,%s,%s,'application/pdf',%s,%s,%s,%s,%s,%s,%s,%s,
    %s,%s,%s,%s,%s,%s,%s
)
"""


def _insert_package_sql(generation_mode: str) -> str:
    if generation_mode in {_INITIAL_GENERATION_MODE, _REVISION_GENERATION_MODE}:
        return _INSERT_PACKAGE_SQL
    if generation_mode == _CONTENT_REVISION_GENERATION_MODE:
        # Only this path needs 0078. Legacy writes retain their existing schema.
        columns, values = _INSERT_PACKAGE_SQL.split(") VALUES (", 1)
        return columns + ", content_generation_claim_version) VALUES (" + values.rstrip().removesuffix(")") + ",%s)"
    raise CaseAgentDocumentPackageBlocked("document package insert mode is invalid")


def _insert_parameters(
    *,
    request: ReviewableDocumentPackageStagingRequest,
    firm_id: str,
    matter_id: str,
    staged_by: str,
    package_id: str,
    artifact_ids: Mapping[str, str],
    template_hash: str,
    object_receipts: Mapping[str, PrivateDocumentObjectReceipt],
    package_receipt_hash: str,
) -> tuple[object, ...]:
    candidate = object_receipts["candidate"]
    editable = object_receipts["editable"]
    pdf = object_receipts["pdf-preview"]
    return (
        package_id, request.idempotency_key, request.run_id, request.graph_id,
        request.task_id, request.attempt_id, firm_id, matter_id,
        request.task_input_hash, request.case_snapshot_hash, request.binding_hash,
        request.source_set_hash, Jsonb(list(request.authorized_source_refs)),
        _authorized_source_refs_hash(request.authorized_source_refs),
        Jsonb(_authorized_source_manifest_payload(request.authorized_source_manifest)),
        request.candidate_hash, request.work_plan_id,
        request.work_plan_hash, request.work_plan_item_id,
        request.posture_profile_id, request.posture_profile_hash,
        request.template_id, request.template_version, template_hash,
        request.deliverable_kind, request.output_format.value,
        artifact_ids["candidate"], _ARTIFACT_KINDS[0],
        candidate.content_sha256, candidate.byte_size, candidate.object_key,
        candidate.object_version_id,
        artifact_ids["editable"], _ARTIFACT_KINDS[1], editable.media_type,
        editable.content_sha256, editable.byte_size, editable.object_key,
        editable.object_version_id,
        artifact_ids["pdf-preview"], _ARTIFACT_KINDS[2], pdf.content_sha256,
        pdf.byte_size, request.review_pdf_page_count, pdf.object_key,
        pdf.object_version_id, request.render_verification_hash,
        request.review_input_hash, package_receipt_hash,
        request.generation_mode, request.revision_number,
        request.root_package_id, request.supersedes_package_id,
        request.revision_request_id, request.requested_by, staged_by,
    ) + ((request.content_generation_claim_version,) if request.generation_mode == _CONTENT_REVISION_GENERATION_MODE else ())


_READ_PACKAGE_FOR_VERIFIER_SQL = """
SELECT package.*, task.input_refs AS current_task_input_refs,
       plan.plan_version AS current_work_plan_version,
       profile.profile_version AS current_posture_profile_version
FROM case_agent_reviewable_document_packages package
JOIN case_agent_runs run
  ON run.run_id = package.run_id AND run.firm_id = package.firm_id
 AND run.matter_id = package.matter_id
JOIN case_agent_task_graphs graph
  ON graph.graph_id = package.graph_id AND graph.run_id = package.run_id
 AND graph.firm_id = package.firm_id AND graph.matter_id = package.matter_id
JOIN case_agent_tasks task
  ON task.graph_id = package.graph_id AND task.task_id = package.task_id
 AND task.run_id = package.run_id AND task.firm_id = package.firm_id
 AND task.matter_id = package.matter_id
JOIN case_agent_task_attempts attempt
  ON attempt.attempt_id = package.attempt_id
 AND attempt.graph_id = package.graph_id AND attempt.task_id = package.task_id
 AND attempt.run_id = package.run_id AND attempt.firm_id = package.firm_id
 AND attempt.matter_id = package.matter_id
JOIN matter_actor_roles verifier_role
  ON verifier_role.matter_id = package.matter_id
 AND verifier_role.firm_id = package.firm_id AND verifier_role.user_id = %s
 AND verifier_role.role = 'SYSTEM_WORKER' AND verifier_role.revoked_at IS NULL
JOIN users verifier_user
  ON verifier_user.user_id = verifier_role.user_id
 AND verifier_user.firm_id = verifier_role.firm_id
 AND verifier_user.status = 'ACTIVE'
JOIN matter_actor_roles execution_role
  ON execution_role.matter_id = package.matter_id
 AND execution_role.firm_id = package.firm_id AND execution_role.user_id = %s
 AND execution_role.role = 'SYSTEM_WORKER' AND execution_role.revoked_at IS NULL
JOIN users execution_user
  ON execution_user.user_id = execution_role.user_id
 AND execution_user.firm_id = execution_role.firm_id
 AND execution_user.status = 'ACTIVE'
JOIN matters matter
  ON matter.matter_id = package.matter_id AND matter.firm_id = package.firm_id
JOIN case_work_plans plan
  ON plan.plan_id = package.work_plan_id AND plan.firm_id = package.firm_id
 AND plan.matter_id = package.matter_id
JOIN case_work_plan_heads plan_head
  ON plan_head.matter_id = plan.matter_id AND plan_head.firm_id = plan.firm_id
JOIN case_work_plan_items item
  ON item.item_id = package.work_plan_item_id AND item.plan_id = plan.plan_id
 AND item.firm_id = plan.firm_id AND item.matter_id = plan.matter_id
JOIN case_posture_profiles profile
  ON profile.profile_id = package.posture_profile_id
 AND profile.firm_id = package.firm_id AND profile.matter_id = package.matter_id
JOIN case_posture_profile_heads profile_head
  ON profile_head.matter_id = profile.matter_id
 AND profile_head.firm_id = profile.firm_id
WHERE (package.candidate_artifact_id = %s
       OR package.editable_artifact_id = %s
       OR package.review_pdf_artifact_id = %s)
  AND package.firm_id = %s AND package.matter_id = %s AND package.run_id = %s
  AND package.review_status = 'NEEDS_LAWYER_REVIEW'
  AND task.input_hash = package.task_input_hash
  AND task.input_refs = jsonb_build_array(
      'work-plan-item:' || package.work_plan_item_id::text
  )
  AND run.snapshot_hash = package.case_snapshot_hash
  AND graph.snapshot_hash = package.case_snapshot_hash
  AND run.current_graph_id = package.graph_id
  AND run.current_graph_hash = graph.graph_hash
  AND NOT run.is_stale AND NOT run.is_cancelled
  AND matter.version = run.snapshot_matter_version
  AND package.staged_by = %s
  AND plan.status = 'ACTIVE' AND plan_head.current_plan_id = plan.plan_id
  AND plan.plan_hash = package.work_plan_hash
  AND plan.profile_id = package.posture_profile_id
  AND plan.profile_hash = package.posture_profile_hash
  AND profile.profile_hash = package.posture_profile_hash
  AND plan.activated_matter_version = run.snapshot_matter_version
  AND item.item_kind = 'DOCUMENT_CANDIDATE'
  AND item.readiness = 'ACTIONABLE'
  AND item.delivery_target <> 'NOT_APPLICABLE'
  AND item.deliverable_kind = package.deliverable_kind
  AND profile.status = 'CONFIRMED'
  AND profile_head.current_profile_id = profile.profile_id
  AND NOT EXISTS (
      SELECT 1 FROM matter_actor_roles extra
      WHERE extra.firm_id = package.firm_id AND extra.matter_id = package.matter_id
        AND extra.user_id IN (%s, %s) AND extra.role <> 'SYSTEM_WORKER'
        AND extra.revoked_at IS NULL
  )
LIMIT 1
"""


@contextmanager
def _transaction(
    dsn: str,
    actor: Actor,
    *,
    read_only: bool,
    repeatable_read: bool = False,
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        if repeatable_read:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ "
                + ("READ ONLY" if read_only else "READ WRITE")
            )
        elif read_only:
            connection.execute("SET TRANSACTION READ ONLY")
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)", (actor.firm_id,)
        )
        connection.execute(
            "SELECT set_config('app.actor_id', %s, true)", (actor.actor_id,)
        )
        yield connection


def _coerce_object_receipt(value: object) -> PrivateDocumentObjectReceipt:
    if isinstance(value, PrivateDocumentObjectReceipt):
        return value
    if not all(
        hasattr(value, name)
        for name in ("object_key", "content_sha256", "byte_size", "media_type")
    ):
        raise CaseAgentDocumentPackageBlocked(
            "private object store returned an invalid receipt"
        )
    return PrivateDocumentObjectReceipt(
        object_key=str(getattr(value, "object_key")),
        content_sha256=str(getattr(value, "content_sha256")),
        byte_size=int(getattr(value, "byte_size")),
        media_type=str(getattr(value, "media_type")),
        object_version_id=(
            str(getattr(value, "object_version_id"))
            if getattr(value, "object_version_id", None) is not None
            else None
        ),
    )


def _validate_object_receipt(
    receipt: PrivateDocumentObjectReceipt,
    *,
    firm_id: str,
    matter_id: str,
    package_id: str,
    object_role: str,
    expected_hash: str,
    expected_size: int,
    expected_media_type: str,
) -> None:
    match = _OBJECT_KEY.fullmatch(receipt.object_key)
    if (
        match is None
        or match.group("firm") != firm_id
        or match.group("matter") != matter_id
        or match.group("package") != package_id
        or match.group("role") != object_role
        or match.group("digest") != expected_hash
        or receipt.content_sha256 != expected_hash
        or receipt.byte_size != expected_size
        or receipt.media_type != expected_media_type
    ):
        raise CaseAgentDocumentPackageBlocked(
            "private object receipt differs from the document package"
        )
    if receipt.object_version_id is not None and (
        not receipt.object_version_id.strip()
        or len(receipt.object_version_id) > 512
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in receipt.object_version_id
        )
    ):
        raise CaseAgentDocumentPackageBlocked("private object version is invalid")


def _validate_s3_document_head(
    value: object,
    *,
    expected_size: int,
    expected_checksum: str,
    expected_media_type: str,
    expected_metadata: Mapping[str, str],
) -> None:
    if not isinstance(value, Mapping):
        raise CaseAgentDocumentPackageBlocked(
            "private document object metadata is unavailable"
        )
    if (
        value.get("ContentLength") != expected_size
        or value.get("ChecksumSHA256") != expected_checksum
        or value.get("ContentType") != expected_media_type
    ):
        raise CaseAgentDocumentPackageBlocked(
            "private document object remote receipt differs"
        )
    metadata = value.get("Metadata")
    if not isinstance(metadata, Mapping):
        raise CaseAgentDocumentPackageBlocked(
            "private document object metadata is unavailable"
        )
    normalized = {str(key).lower(): str(item) for key, item in metadata.items()}
    if normalized != dict(expected_metadata):
        raise CaseAgentDocumentPackageBlocked(
            "private document object metadata differs"
        )


def _require_exact_bytes(
    content: object, *, expected_hash: str, expected_size: int, label: str
) -> None:
    if (
        not isinstance(content, bytes)
        or len(content) != expected_size
        or sha256(content).hexdigest() != expected_hash
    ):
        raise CaseAgentDocumentPackageBlocked(
            f"{label} bytes differ from the private object receipt"
        )


def _bounded_bytes(
    content: object,
    expected_hash: str,
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> None:
    if (
        not isinstance(content, bytes)
        or not minimum <= len(content) <= maximum
        or sha256(content).hexdigest() != expected_hash
    ):
        raise CaseAgentDocumentPackageBlocked(f"{label} is not hash-bound")


def _verify_ooxml(content: bytes, output_format: ReviewableDocumentFormat) -> None:
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            names = set(archive.namelist())
            if (
                len(infos) > 20_000
                or sum(item.file_size for item in infos) > 512 * 1024 * 1024
                or any(
                    name.startswith("/") or ".." in name.split("/")
                    for name in names
                )
                or archive.testzip() is not None
            ):
                raise CaseAgentDocumentPackageBlocked(
                    "editable Office artifact exceeds its safe container boundary"
                )
            required = (
                "word/document.xml"
                if output_format is ReviewableDocumentFormat.DOCX
                else "xl/workbook.xml"
            )
            if required not in names or "[Content_Types].xml" not in names:
                raise CaseAgentDocumentPackageBlocked(
                    "editable Office artifact does not match its format"
                )
            lowered = {value.lower() for value in names}
            if any(
                value.endswith("vbaproject.bin")
                or "/externallinks/" in value
                or value.endswith("/attachedtoolbars.bin")
                for value in lowered
            ):
                raise CaseAgentDocumentPackageBlocked(
                    "editable Office artifact contains active or external content"
                )
    except (BadZipFile, OSError) as error:
        raise CaseAgentDocumentPackageBlocked(
            "editable Office artifact is not a valid OOXML container"
        ) from error


def _verify_pdf(content: bytes, expected_page_count: int) -> None:
    if not isinstance(expected_page_count, int) or not 1 <= expected_page_count <= 10_000:
        raise CaseAgentDocumentPackageBlocked("review PDF page count is invalid")
    if not content.startswith(b"%PDF-"):
        raise CaseAgentDocumentPackageBlocked("review PDF header is invalid")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted or len(reader.pages) != expected_page_count:
            raise CaseAgentDocumentPackageBlocked(
                "review PDF differs from its render receipt"
            )
    except CaseAgentDocumentPackageBlocked:
        raise
    except Exception as error:
        raise CaseAgentDocumentPackageBlocked("review PDF is structurally invalid") from error


def _candidate_source_refs(candidate: Mapping[str, Any]) -> frozenset[str]:
    refs: set[str] = set()
    for section in candidate.get("sections", []):
        for paragraph in section["paragraphs"]:
            refs.update(paragraph["source_refs"])
    for row in candidate.get("rows", []):
        refs.update(row["source_refs"])
    return frozenset(refs)


def _authorized_source_manifest_from_sources(
    sources: tuple[AuthoritativeDocumentSource, ...],
) -> tuple[AuthorizedDocumentSourceBinding, ...]:
    if not isinstance(sources, tuple):
        raise CaseAgentDocumentPackageBlocked(
            "server-expanded document sources are invalid"
        )
    manifest: list[AuthorizedDocumentSourceBinding] = []
    for source in sources:
        if not isinstance(source, AuthoritativeDocumentSource):
            raise CaseAgentDocumentPackageBlocked(
                "server-expanded document source is invalid"
            )
        source.validate()
        manifest.append(
            AuthorizedDocumentSourceBinding(
                input_ref=source.input_ref,
                source_kind=source.source_kind.value,
                source_version=source.source_version,
                source_hash=source.source_hash,
                label=source.label,
                text_sha256=sha256(source.text.encode("utf-8")).hexdigest(),
            )
        )
    return _authorized_source_manifest(tuple(manifest))


def authorized_document_source_manifest(
    sources: tuple[AuthoritativeDocumentSource, ...],
) -> tuple[AuthorizedDocumentSourceBinding, ...]:
    """Project server-expanded sources into the persisted non-text manifest."""

    return _authorized_source_manifest_from_sources(sources)


def _authorized_source_manifest(
    value: object,
) -> tuple[AuthorizedDocumentSourceBinding, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 400:
        raise CaseAgentDocumentPackageBlocked(
            "authorized document source manifest is invalid"
        )
    refs: set[str] = set()
    for item in value:
        if not isinstance(item, AuthorizedDocumentSourceBinding):
            raise CaseAgentDocumentPackageBlocked(
                "authorized document source manifest entry is invalid"
            )
        _identifier(item.input_ref, "authorized source input_ref")
        try:
            DocumentSourceKind(item.source_kind)
        except ValueError:
            raise CaseAgentDocumentPackageBlocked(
                "authorized document source kind is invalid"
            ) from None
        _identifier(item.source_version, "authorized source version")
        _sha(item.source_hash, "authorized source hash")
        _text(item.label, "authorized source label", 240)
        _sha(item.text_sha256, "authorized source text hash")
        if item.input_ref in refs:
            raise CaseAgentDocumentPackageBlocked(
                "authorized document source refs must be unique"
            )
        refs.add(item.input_ref)
    return value


def _authorized_source_manifest_payload(
    value: tuple[AuthorizedDocumentSourceBinding, ...],
) -> list[dict[str, str]]:
    manifest = _authorized_source_manifest(value)
    return [
        {
            "input_ref": item.input_ref,
            "source_kind": item.source_kind,
            "source_version": item.source_version,
            "source_hash": item.source_hash,
            "label": item.label,
            "text_sha256": item.text_sha256,
        }
        for item in manifest
    ]


def _source_set_hash_from_manifest(
    value: tuple[AuthorizedDocumentSourceBinding, ...],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "case-agent-document-source-set-v1",
            "sources": _authorized_source_manifest_payload(value),
        }
    )


def _authorized_source_manifest_from_json(
    value: object,
) -> tuple[AuthorizedDocumentSourceBinding, ...]:
    if not isinstance(value, list):
        raise CaseAgentDocumentPackageBlocked(
            "persisted document source manifest is invalid"
        )
    result: list[AuthorizedDocumentSourceBinding] = []
    expected_keys = {
        "input_ref",
        "source_kind",
        "source_version",
        "source_hash",
        "label",
        "text_sha256",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise CaseAgentDocumentPackageBlocked(
                "persisted document source manifest entry is invalid"
            )
        result.append(
            AuthorizedDocumentSourceBinding(
                input_ref=str(item["input_ref"]),
                source_kind=str(item["source_kind"]),
                source_version=str(item["source_version"]),
                source_hash=str(item["source_hash"]),
                label=str(item["label"]),
                text_sha256=str(item["text_sha256"]),
            )
        )
    return _authorized_source_manifest(tuple(result))


def _authorized_source_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 400:
        raise CaseAgentDocumentPackageBlocked(
            "authorized document source refs are invalid"
        )
    if tuple(sorted(value)) != value or len(set(value)) != len(value):
        raise CaseAgentDocumentPackageBlocked(
            "authorized document source refs must be sorted and unique"
        )
    for item in value:
        if not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None:
            raise CaseAgentDocumentPackageBlocked(
                "authorized document source ref is invalid"
            )
    return value


def _authorized_source_refs_hash(value: tuple[str, ...]) -> str:
    refs = _authorized_source_refs(value)
    return sha256("\n".join(refs).encode("utf-8")).hexdigest()


def _source_refs(value: object) -> None:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise CaseAgentDocumentPackageBlocked("candidate source refs are invalid")
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or _IDENTIFIER.fullmatch(item) is None
            or item in seen
        ):
            raise CaseAgentDocumentPackageBlocked("candidate source ref is invalid")
        seen.add(item)


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise CaseAgentDocumentPackageBlocked(f"{label} is invalid")
    return value.strip()


def _object_store(value: object) -> None:
    if not callable(getattr(value, "put_reviewable_document_object", None)) or not callable(
        getattr(value, "read_reviewable_document_object", None)
    ):
        raise ValueError("private reviewable-document object store is required")


def _dedicated_worker(actor: Actor, purpose: str) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError(f"{purpose} requires a dedicated SYSTEM_WORKER")
    _uuid(actor.actor_id, "worker actor_id")
    _uuid(actor.firm_id, "worker firm_id")


def _dsn(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("PostgreSQL DSN is required")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise CaseAgentDocumentPackageBlocked(f"{label} is invalid") from None


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CaseAgentDocumentPackageBlocked(f"{label} is invalid")


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CaseAgentDocumentPackageBlocked(f"{label} is invalid")


def _code(value: str, label: str) -> None:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise CaseAgentDocumentPackageBlocked(f"{label} is invalid")


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


__all__ = [
    "CaseAgentDocumentPackageBlocked",
    "DocumentAwareManagedArtifactAccessPort",
    "PostgresReviewableDocumentPackageAccessPort",
    "PostgresReviewableDocumentPackageStore",
    "PrivateDocumentObjectReceipt",
    "ReviewableDocumentS3Client",
    "ReviewableDocumentS3Config",
    "ReviewableDocumentArtifactRead",
    "ReviewableDocumentPackageRead",
    "ReviewableDocumentPackageStagingRequest",
    "ReviewableDocumentPrivateObjectStore",
    "S3ReviewableDocumentPrivateObjectStore",
    "StagedReviewableDocumentPackage",
    "authorized_document_source_manifest",
    "preflight_case_agent_document_delivery_runtime_contract",
    "reviewable_document_template_hash",
]
