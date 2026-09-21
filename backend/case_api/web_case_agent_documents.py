"""Authenticated Web review and delivery of 0039 Agent document packages.

The browser is allowed to name only a case, a current Agent run and one
artifact id already shown in that run.  Before the document object store is
read, this service proves that the caller is an active MFA-authenticated human
on the matter and that all three package artifacts are present in the PASSED
independent-verification lineage for the current graph.  The injected 0039
access port then independently rechecks the current work plan, posture profile,
authorized source manifest, installed template and all private object bytes.

Only a bounded, lawyer-readable projection or an authenticated Office/PDF
download leaves this boundary.  Object locators, hashes, prompts and provider
payloads never do, and no read can approve or submit a document.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Callable, Literal, Protocol
import unicodedata
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from case_kernel.case_agent_document_delivery import ReviewableDocumentFormat, DynamicDocumentTaskBinding
from case_kernel.case_agent_document_delivery_postgres import (
    ReviewableDocumentPackageRead,
)
from case_kernel.case_agent_document_revisions import (
    CaseAgentDocumentRevisionBlocked,
    DocumentRevisionState,
    LawyerParagraphChange,
    RegisteredContentResult,
)
from case_kernel.case_agent_verifier import ArtifactLineageReceipt
from case_kernel.models import Actor, Role

from .persistent_identity import AuthenticationMethod, ServerIdentityContext


class WebCaseAgentDocumentReviewBlocked(RuntimeError):
    """A review-only Agent document cannot be safely shown or downloaded."""


@dataclass(frozen=True)
class WebCaseAgentDocumentSource:
    source_ref: str
    source_kind: str
    label: str


@dataclass(frozen=True)
class WebCaseAgentDocumentParagraph:
    paragraph_id: str
    text: str
    sources: tuple[WebCaseAgentDocumentSource, ...]


@dataclass(frozen=True)
class WebCaseAgentDocumentSection:
    section_id: str
    heading: str
    paragraphs: tuple[WebCaseAgentDocumentParagraph, ...]


@dataclass(frozen=True)
class WebCaseAgentDocumentColumn:
    key: str
    label: str
    value_type: str


@dataclass(frozen=True)
class WebCaseAgentDocumentRow:
    row_id: str
    cells: tuple[str | int | float | bool | None, ...]
    sources: tuple[WebCaseAgentDocumentSource, ...]


@dataclass(frozen=True)
class WebCaseAgentDocumentReview:
    artifact_id: str
    title: str
    deliverable_kind: str
    deliverable_label: str
    output_format: str
    review_notice: str
    version_status: Literal[
        "CURRENT", "UPDATE_REQUIRED", "GENERATING", "FAILED", "UNKNOWN"
    ]
    revision_number: int
    template_version: str
    installed_template_version: str
    can_request_revision: bool
    request_status: str | None
    request_id: str | None
    download_ready: bool
    review_pdf_page_count: int
    total_item_count: int
    displayed_item_count: int
    preview_truncated: bool
    sections: tuple[WebCaseAgentDocumentSection, ...] = ()
    columns: tuple[WebCaseAgentDocumentColumn, ...] = ()
    rows: tuple[WebCaseAgentDocumentRow, ...] = ()
    review_version: str | None = None
    review_artifact_id: str | None = None


@dataclass(frozen=True)
class WebCaseAgentDocumentDownload:
    file_name: str
    ascii_file_name: str
    media_type: str
    disposition: Literal["inline", "attachment"]
    content: bytes
    review_version: str | None = None


class WebCaseAgentDocumentReviewPort(Protocol):
    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> WebCaseAgentDocumentReview: ...

    def download(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        file_role: Literal["editable", "pdf-preview"],
        expected_review_version: str | None = None,
    ) -> WebCaseAgentDocumentDownload: ...

    def request_revision(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        expected_revision_number: int,
        idempotency_key: str,
    ) -> WebCaseAgentDocumentReview: ...


class _ReviewableDocumentPackageAccess(Protocol):
    def read_package(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> ReviewableDocumentPackageRead: ...


class _DocumentRevisionStore(Protocol):
    def read_state(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> DocumentRevisionState: ...

    def request_revision(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        expected_revision_number: int,
        idempotency_key: str,
    ) -> DocumentRevisionState: ...


_HUMAN_ROLES = frozenset(
    {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
)
_HUMAN_ROLE_VALUES = tuple(sorted(item.value for item in _HUMAN_ROLES))
_DOCUMENT_KINDS = (
    "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
    "REVIEWABLE_DOCUMENT_EDITABLE",
    "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
)
_MAX_PREVIEW_PARAGRAPHS = 500
_MAX_PREVIEW_ROWS = 250
_MAX_TITLE = 240
_MAX_PARAGRAPH = 20_000
_MAX_LABEL = 240

_DELIVERABLE_LABELS = {
    "CASE_REVIEW_MEMO": "案件审阅意见候选",
    "SUPPLEMENTARY_EVIDENCE_CHECKLIST": "补证清单候选",
    "COMPLAINT": "民事起诉状候选",
    "DEFENCE_STATEMENT": "民事答辩状候选",
    "COUNTERCLAIM": "民事反诉状候选",
    "APPEAL_PETITION": "民事上诉状候选",
    "LEGAL_RESEARCH_MEMO": "法律研究意见候选",
    "PAYMENT_LEDGER": "收付款核对表候选",
    "INTEREST_CALCULATION_TABLE": "利息核算表候选",
    "EVIDENCE_CATALOGUE": "证据目录候选",
}


class PostgresWebCaseAgentDocumentReviewService:
    """Expose only current, independently verified 0039 packages to lawyers."""

    def __init__(
        self,
        *,
        dsn: str,
        package_access: _ReviewableDocumentPackageAccess,
        revision_store: _DocumentRevisionStore,
        connection_factory: Callable[..., Any] | None = None,
        content_binding_resolver: Callable[..., DynamicDocumentTaskBinding] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("Agent document-review PostgreSQL DSN is required")
        if not callable(getattr(package_access, "read_package", None)):
            raise ValueError("Agent document package access port is invalid")
        if not all(
            callable(getattr(revision_store, method, None))
            for method in ("read_state", "request_revision")
        ):
            raise ValueError("Agent document revision store is invalid")
        self._dsn = dsn.strip()
        self._packages = package_access
        self._revisions = revision_store
        self._connect = connection_factory or psycopg.connect
        self._content_binding_resolver = content_binding_resolver

    def authorize_content_generation(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str,
        artifact_id: str, proposal_id: str, expected_revision_number: int,
        idempotency_key: str, review_note: str,
    ) -> str:
        actor, state = self._read_revision_state(
            identity=identity, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
        )
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise PermissionError("只有本案主办律师或复核人可授权生成。")
        if state.version_status != "CURRENT" or state.revision_number != expected_revision_number:
            raise WebCaseAgentDocumentReviewBlocked("文书版本已变化，请重新核对修改。")
        package = self._read_verified_package(actor=actor, matter_id=matter_id,
            run_id=run_id, artifact_id=artifact_id, revision_state=state)
        resolver = self._content_binding_resolver
        authorize = getattr(self._revisions, "authorize_content_generation", None)
        if not callable(resolver) or not callable(authorize):
            raise WebCaseAgentDocumentReviewBlocked("修改复核服务尚未配置，未记录授权。")
        try:
            binding = resolver(actor=actor, package=package, matter_id=matter_id, run_id=run_id)
        except PermissionError:
            raise
        except Exception as error:
            raise WebCaseAgentDocumentReviewBlocked("当前来源无法核验，未记录生成授权。") from error
        if not isinstance(binding, DynamicDocumentTaskBinding) or (
            binding.firm_id, binding.matter_id, binding.run_id, binding.task_id, binding.binding_hash
        ) != (actor.firm_id, matter_id, run_id, package.task_id, package.binding_hash):
            raise WebCaseAgentDocumentReviewBlocked("来源或文书绑定已变化，未记录生成授权。")
        return authorize(actor=actor, matter_id=matter_id, run_id=run_id,
            artifact_id=artifact_id, proposal_id=proposal_id, expected_revision_number=expected_revision_number,
            idempotency_key=idempotency_key, review_note=review_note, binding=binding,
            predecessor_content=package.candidate.content)

    def resolve_content_proposal(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str,
        artifact_id: str, idempotency_key: str,
    ) -> str | None:
        actor, _ = self._read_revision_state(
            identity=identity, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
        )
        resolve = getattr(self._revisions, "resolve_content_proposal", None)
        if not callable(resolve):
            raise WebCaseAgentDocumentReviewBlocked("保存结果核对尚未配置。")
        return resolve(actor=actor, matter_id=matter_id, run_id=run_id,
                       artifact_id=artifact_id, idempotency_key=idempotency_key)

    def list_content_proposals(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str,
        artifact_id: str, after: str | None = None,
    ) -> dict[str, object]:
        actor, _ = self._read_revision_state(
            identity=identity, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
        )
        read = getattr(self._revisions, "list_content_proposals", None)
        if not callable(read):
            raise WebCaseAgentDocumentReviewBlocked("修改记录列表尚未配置。")
        return dict(read(actor=actor, matter_id=matter_id, run_id=run_id,
                         artifact_id=artifact_id, after=after))

    def read_content_proposal(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str,
        artifact_id: str, proposal_id: str,
    ) -> dict[str, object]:
        actor, _ = self._read_revision_state(
            identity=identity, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
        )
        read = getattr(self._revisions, "read_content_proposal", None)
        if not callable(read):
            raise WebCaseAgentDocumentReviewBlocked("修改记录读取尚未配置。")
        detail = dict(read(actor=actor, matter_id=matter_id, run_id=run_id,
                           artifact_id=artifact_id, proposal_id=proposal_id))
        resolve = getattr(self._revisions, "read_registered_content_result", None)
        if detail.get("generation_status") == "UNKNOWN_REGISTERED" and callable(resolve):
            try:
                registered = resolve(actor=actor, matter_id=matter_id, run_id=run_id,
                                     artifact_id=artifact_id, proposal_id=proposal_id)
                if isinstance(registered, RegisteredContentResult):
                    package = self._packages.read_package(firm_id=actor.firm_id, matter_id=matter_id,
                        run_id=run_id, artifact_id=registered.candidate_artifact_id)
                    if (isinstance(package, ReviewableDocumentPackageRead)
                            and package.run_id == run_id and package.generation_mode == "LAWYER_CONTENT_REVISION"
                            and package.package_id == registered.package_id
                            and package.candidate.artifact_id == registered.candidate_artifact_id
                            and package.receipt_hash == registered.receipt_hash
                            and package.revision_request_id == registered.request_id
                            and package.root_package_id == registered.root_package_id
                            and package.supersedes_package_id == registered.predecessor_package_id
                            and package.revision_number == registered.revision_number
                            and package.candidate_hash == registered.candidate_hash
                            and package.binding_hash == registered.binding_hash
                            and type(registered.claim_version) is int and 1 <= registered.claim_version <= 3
                            and package.content_generation_claim_version == registered.claim_version):
                        detail["generation_status"] = "UNKNOWN_FILES_VERIFIED"
            except PermissionError:
                raise
            except Exception:
                # No private locator/error or partially verified bytes leave
                # this boundary. Keep the original uncertainty; never retry.
                pass
        return detail

    def save_content_proposal(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str,
        artifact_id: str, expected_revision_number: int, idempotency_key: str,
        changes: tuple[LawyerParagraphChange, ...],
    ) -> str:
        actor, state = self._read_revision_state(
            identity=identity, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
        )
        if state.version_status != "CURRENT" or state.revision_number != expected_revision_number:
            raise WebCaseAgentDocumentReviewBlocked("文书版本已变化，请重新打开当前原稿后修改。")
        package = self._read_verified_package(
            actor=actor, matter_id=matter_id, run_id=run_id,
            artifact_id=artifact_id, revision_state=state,
        )
        if package.output_format is not ReviewableDocumentFormat.DOCX:
            raise WebCaseAgentDocumentReviewBlocked("当前仅支持文字文书的段落修订。")
        resolver = self._content_binding_resolver
        save = getattr(self._revisions, "save_content_proposal", None)
        if not callable(resolver) or not callable(save):
            raise WebCaseAgentDocumentReviewBlocked("正文修订服务尚未配置，修改未保存。")
        try:
            binding = resolver(actor=actor, package=package, matter_id=matter_id, run_id=run_id)
        except PermissionError:
            raise
        except Exception as error:
            raise WebCaseAgentDocumentReviewBlocked("当前来源无法重新核验，修改未保存；请刷新文书状态。") from error
        if not isinstance(binding, DynamicDocumentTaskBinding) or (
            binding.firm_id, binding.matter_id, binding.run_id, binding.task_id, binding.binding_hash
        ) != (actor.firm_id, matter_id, run_id, package.task_id, package.binding_hash):
            raise WebCaseAgentDocumentReviewBlocked("正文来源与当前文书不一致，修改未保存。")
        return save(
            actor=actor, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
            expected_revision_number=expected_revision_number, idempotency_key=idempotency_key,
            binding=binding, current_candidate_bytes=package.candidate.content,
            expected_candidate_hash=sha256(package.candidate.content).hexdigest(), changes=changes,
        )

    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> WebCaseAgentDocumentReview:
        actor, state = self._read_revision_state(
            identity=identity,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        if state.version_status != "CURRENT":
            return _project_revision_state(state)
        package = self._read_verified_package(
            actor=actor,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
            revision_state=state,
        )
        return _project_package(
            package,
            requested_artifact_id=artifact_id,
            revision_state=state,
        )

    def request_revision(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        expected_revision_number: int,
        idempotency_key: str,
    ) -> WebCaseAgentDocumentReview:
        actor = _human_actor(identity)
        try:
            state = self._revisions.request_revision(
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
                expected_revision_number=expected_revision_number,
                idempotency_key=idempotency_key,
            )
        except PermissionError:
            raise
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebCaseAgentDocumentReviewBlocked(
                "当前文书版本不能安全更新；请刷新后核对任务和文书版本。"
            ) from error
        return _project_revision_state(state)

    def download(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        file_role: Literal["editable", "pdf-preview"],
        expected_review_version: str | None = None,
    ) -> WebCaseAgentDocumentDownload:
        if file_role not in {"editable", "pdf-preview"}:
            raise WebCaseAgentDocumentReviewBlocked("请求的文书文件类型无效。")
        if expected_review_version is not None and (
            not isinstance(expected_review_version, str) or re.fullmatch(r"[a-f0-9]{64}", expected_review_version) is None
        ):
            raise WebCaseAgentDocumentReviewBlocked("请求的文书审阅版本无效。")
        actor, state = self._read_revision_state(
            identity=identity,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        if state.version_status != "CURRENT":
            raise WebCaseAgentDocumentReviewBlocked(
                "当前文书尚未完成最新模板生成与独立复核，不能下载旧版本。"
            )
        package = self._read_verified_package(
            actor=actor,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
            revision_state=state,
        )
        observed_version = _document_review_version(package)
        if expected_review_version is not None and expected_review_version != observed_version:
            raise WebCaseAgentDocumentReviewBlocked("文书已换版，本次未下载；请先审阅当前版本。")
        payload = _strict_candidate(package.candidate.content)
        title = _text(payload.get("title"), "文书标题", _MAX_TITLE)
        if file_role == "pdf-preview":
            selected = package.review_pdf
            extension = ".pdf"
            disposition: Literal["inline", "attachment"] = "inline"
            ascii_name = "agent-document-preview.pdf"
        else:
            selected = package.editable
            extension = ".docx" if package.output_format is ReviewableDocumentFormat.DOCX else ".xlsx"
            disposition = "attachment"
            ascii_name = f"agent-document-candidate{extension}"
        expected_media_type = _expected_media_type(package.output_format, file_role)
        if selected.media_type != expected_media_type or not selected.content:
            raise WebCaseAgentDocumentReviewBlocked("文书文件格式与已核验记录不一致。")
        file_name = f"{_safe_file_stem(title)}{extension}"
        return WebCaseAgentDocumentDownload(
            file_name=file_name,
            ascii_file_name=ascii_name,
            media_type=selected.media_type,
            disposition=disposition,
            content=selected.content,
            review_version=observed_version,
        )

    def _read_verified_package(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        artifact_id: str,
        revision_state: DocumentRevisionState,
    ) -> ReviewableDocumentPackageRead:
        matter_id = _uuid(matter_id, "案件编号")
        run_id = _uuid(run_id, "Agent 任务编号")
        artifact_id = _uuid(artifact_id, "Agent 成果编号")
        row = self._read_authority(
            firm_id=actor.firm_id,
            actor_id=actor.actor_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        if row is None:
            raise WebCaseAgentDocumentReviewBlocked(
                "该文书候选不存在、不是当前任务成果、未通过独立复核，或当前律师无权查看。"
            )
        expected = _verified_package_lineage(row)
        # This second read is intentionally after human authorization and the
        # PASSED-lineage gate.  The 0039 access port independently rechecks the
        # current plan/profile/source manifest and authenticates all three
        # object bodies.  An indeterminate result is surfaced, never retried.
        try:
            package = self._packages.read_package(
                firm_id=actor.firm_id,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=revision_state.current_candidate_artifact_id,
            )
        except PermissionError:
            raise
        except Exception as error:
            raise WebCaseAgentDocumentReviewBlocked(
                "文书候选当前无法完成服务端二次核验；系统不会自动重试或改用未核验副本。"
            ) from error
        _match_current_package(
            package,
            expected=expected,
            row=row,
            state=revision_state,
        )
        return package

    def _read_revision_state(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> tuple[Actor, DocumentRevisionState]:
        actor = _human_actor(identity)
        try:
            state = self._revisions.read_state(
                actor=actor,
                matter_id=matter_id,
                run_id=run_id,
                artifact_id=artifact_id,
            )
        except PermissionError:
            raise
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebCaseAgentDocumentReviewBlocked(
                "该文书候选不存在、不是当前已核验成果，或当前律师无权查看。"
            ) from error
        return actor, state

    def _read_authority(
        self,
        *,
        firm_id: str,
        actor_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor_id,))
            return connection.execute(
                """
                SELECT package.package_id, package.run_id, package.graph_id,
                       package.task_id, package.task_input_hash,
                       package.candidate_artifact_id,
                       package.candidate_artifact_kind,
                       package.candidate_content_sha256,
                       package.candidate_byte_size,
                       package.editable_artifact_id,
                       package.editable_artifact_kind,
                       package.editable_sha256,
                       package.editable_byte_size,
                       package.review_pdf_artifact_id,
                       package.review_pdf_artifact_kind,
                       package.review_pdf_sha256,
                       package.review_pdf_byte_size,
                       receipt.artifact_lineage
                FROM case_agent_reviewable_document_packages package
                JOIN case_agent_runs run
                  ON run.run_id = package.run_id
                 AND run.firm_id = package.firm_id
                 AND run.matter_id = package.matter_id
                 AND run.current_graph_id = package.graph_id
                 AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
                 AND NOT run.is_stale AND NOT run.is_cancelled
                JOIN matters current_matter
                  ON current_matter.matter_id = run.matter_id
                 AND current_matter.firm_id = run.firm_id
                 AND current_matter.version = run.snapshot_matter_version
                JOIN case_agent_verification_attempts attempt
                  ON attempt.run_id = run.run_id
                 AND attempt.graph_id = run.current_graph_id
                 AND attempt.firm_id = run.firm_id
                 AND attempt.matter_id = run.matter_id
                JOIN case_agent_verification_receipts receipt
                  ON receipt.verification_attempt_id = attempt.verification_attempt_id
                 AND receipt.run_id = attempt.run_id
                 AND receipt.firm_id = attempt.firm_id
                 AND receipt.matter_id = attempt.matter_id
                 AND receipt.outcome = 'PASSED'
                 AND receipt.verification_hash = run.verification_hash
                 AND receipt.graph_hash = run.current_graph_hash
                 AND receipt.snapshot_hash = run.snapshot_hash
                JOIN users principal
                  ON principal.user_id = %s
                 AND principal.firm_id = package.firm_id
                 AND principal.status = 'ACTIVE'
                WHERE package.firm_id = %s
                  AND package.matter_id = %s
                  AND package.run_id = %s
                  AND package.review_status = 'NEEDS_LAWYER_REVIEW'
                  AND package.generation_mode = 'INITIAL_AGENT_TASK'
                  AND (%s IN (
                        package.candidate_artifact_id,
                        package.editable_artifact_id,
                        package.review_pdf_artifact_id
                  ))
                  AND EXISTS (
                      SELECT 1 FROM matter_actor_roles role
                      WHERE role.user_id = principal.user_id
                        AND role.firm_id = package.firm_id
                        AND role.matter_id = package.matter_id
                        AND role.role = ANY(%s)
                        AND role.revoked_at IS NULL
                  )
                LIMIT 1
                """,
                (
                    actor_id,
                    firm_id,
                    matter_id,
                    run_id,
                    artifact_id,
                    list(_HUMAN_ROLE_VALUES),
                ),
            ).fetchone()


@dataclass(frozen=True)
class _ExpectedArtifact:
    artifact_id: str
    artifact_kind: str
    content_hash: str
    byte_size: int
    source_input_hash: str


def _verified_package_lineage(row: dict[str, Any]) -> dict[str, _ExpectedArtifact]:
    raw = row.get("artifact_lineage")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WebCaseAgentDocumentReviewBlocked("文书独立复核记录无效。") from error
    if not isinstance(raw, list):
        raise WebCaseAgentDocumentReviewBlocked("文书独立复核记录无效。")
    expected_rows = (
        (
            row.get("candidate_artifact_id"),
            row.get("candidate_artifact_kind"),
            row.get("candidate_content_sha256"),
            row.get("candidate_byte_size"),
        ),
        (
            row.get("editable_artifact_id"),
            row.get("editable_artifact_kind"),
            row.get("editable_sha256"),
            row.get("editable_byte_size"),
        ),
        (
            row.get("review_pdf_artifact_id"),
            row.get("review_pdf_artifact_kind"),
            row.get("review_pdf_sha256"),
            row.get("review_pdf_byte_size"),
        ),
    )
    if tuple(item[1] for item in expected_rows) != _DOCUMENT_KINDS:
        raise WebCaseAgentDocumentReviewBlocked("文书包的三项成果类型无效。")
    result: dict[str, _ExpectedArtifact] = {}
    for artifact_id, artifact_kind, content_hash, byte_size in expected_rows:
        artifact_id = _uuid(artifact_id, "文书成果编号")
        matches = [
            item
            for item in raw
            if isinstance(item, dict) and item.get("artifact_id") == artifact_id
        ]
        if len(matches) != 1:
            raise WebCaseAgentDocumentReviewBlocked("文书包未完整出现在独立复核清单中。")
        try:
            lineage = ArtifactLineageReceipt(**matches[0])
            lineage.validate()
        except (TypeError, ValueError) as error:
            raise WebCaseAgentDocumentReviewBlocked("文书独立复核记录无效。") from error
        if (
            lineage.artifact_kind != artifact_kind
            or lineage.content_hash != content_hash
            or lineage.byte_size != byte_size
            or lineage.source_input_hash != row.get("task_input_hash")
            or not lineage.managed_derivative
        ):
            raise WebCaseAgentDocumentReviewBlocked("文书包与独立复核清单不一致。")
        result[str(artifact_kind)] = _ExpectedArtifact(
            artifact_id=artifact_id,
            artifact_kind=str(artifact_kind),
            content_hash=str(content_hash),
            byte_size=int(byte_size),
            source_input_hash=lineage.source_input_hash,
        )
    return result


def _match_current_package(
    package: ReviewableDocumentPackageRead,
    *,
    expected: dict[str, _ExpectedArtifact],
    row: dict[str, Any],
    state: DocumentRevisionState,
) -> None:
    if not isinstance(package, ReviewableDocumentPackageRead):
        raise WebCaseAgentDocumentReviewBlocked("文书服务返回了无效结果。")
    if (
        state.root_package_id != str(row.get("package_id"))
        or package.package_id != state.current_package_id
        or package.run_id != str(row.get("run_id"))
        or package.graph_id != str(row.get("graph_id"))
        or package.task_id != str(row.get("task_id"))
        or package.task_input_hash != str(row.get("task_input_hash"))
        or package.review_status != "NEEDS_LAWYER_REVIEW"
        or package.candidate.artifact_id != state.current_candidate_artifact_id
        or package.receipt_hash != state.package_receipt_hash
        or package.revision_number != state.revision_number
        or package.template_id != state.template_id
        or package.template_version != state.template_version
        or package.template_hash != state.template_hash
        or package.deliverable_kind != state.deliverable_kind
        or package.output_format is not state.output_format
    ):
        raise WebCaseAgentDocumentReviewBlocked("文书包与当前 Agent 任务不一致。")
    if state.revision_number == 1:
        if (
            package.generation_mode != "INITIAL_AGENT_TASK"
            or package.root_package_id is not None
            or package.revision_request_id is not None
        ):
            raise WebCaseAgentDocumentReviewBlocked("文书初始版本链无效。")
    elif (
        package.generation_mode not in {
            "DETERMINISTIC_TEMPLATE_REVISION", "LAWYER_CONTENT_REVISION"
        }
        or package.root_package_id != state.root_package_id
        or package.revision_request_id != state.current_revision_request_id
    ):
        raise WebCaseAgentDocumentReviewBlocked("文书更新版本链无效。")
    if package.generation_mode == "LAWYER_CONTENT_REVISION":
        if (
            package.output_format is not ReviewableDocumentFormat.DOCX
            or type(package.content_generation_claim_version) is not int
            or not 1 <= package.content_generation_claim_version <= 3
        ):
            raise WebCaseAgentDocumentReviewBlocked("正文修订生成记录无效。")
    elif package.content_generation_claim_version is not None:
        raise WebCaseAgentDocumentReviewBlocked("文书生成记录与版本类型不一致。")
    if state.revision_number > 1:
        return
    for artifact in (package.candidate, package.editable, package.review_pdf):
        item = expected.get(artifact.artifact_kind)
        if item is None or (
            artifact.artifact_id != item.artifact_id
            or artifact.content_sha256 != item.content_hash
            or artifact.byte_size != item.byte_size
        ):
            raise WebCaseAgentDocumentReviewBlocked("文书内容与独立复核清单不一致。")


def _document_review_version(package: ReviewableDocumentPackageRead) -> str:
    return sha256(f"lawyer-document-review-v1:{package.package_id}:{package.receipt_hash}".encode("utf-8")).hexdigest()


def _project_package(
    package: ReviewableDocumentPackageRead,
    *,
    requested_artifact_id: str,
    revision_state: DocumentRevisionState,
) -> WebCaseAgentDocumentReview:
    payload = _strict_candidate(package.candidate.content)
    binding = payload.get("binding")
    if not isinstance(binding, dict) or (
        binding.get("deliverable_kind") != package.deliverable_kind
        or binding.get("output_format") != package.output_format.value
        or binding.get("work_plan_item_id") != package.work_plan_item_id
        or binding.get("template_id") != package.template_id
        or binding.get("template_version") != package.template_version
    ):
        raise WebCaseAgentDocumentReviewBlocked("文书候选与当前动态计划不一致。")
    sources = {
        item.input_ref: WebCaseAgentDocumentSource(
            source_ref=item.input_ref,
            source_kind=item.source_kind,
            label=_text(item.label, "来源名称", _MAX_LABEL),
        )
        for item in package.authorized_source_manifest
    }
    if set(sources) != set(package.authorized_source_refs):
        raise WebCaseAgentDocumentReviewBlocked("文书来源清单不完整。")
    title = _text(payload.get("title"), "文书标题", _MAX_TITLE)
    sections: tuple[WebCaseAgentDocumentSection, ...] = ()
    columns: tuple[WebCaseAgentDocumentColumn, ...] = ()
    rows: tuple[WebCaseAgentDocumentRow, ...] = ()
    if package.output_format is ReviewableDocumentFormat.DOCX:
        sections, total, displayed = _project_docx(payload, sources)
    else:
        columns, rows, total, displayed = _project_xlsx(payload, sources)
    return WebCaseAgentDocumentReview(
        artifact_id=_uuid(requested_artifact_id, "Agent 成果编号"),
        title=title,
        deliverable_kind=package.deliverable_kind,
        deliverable_label=_DELIVERABLE_LABELS.get(package.deliverable_kind, "可编辑文书候选"),
        output_format=package.output_format.value,
        review_notice=(
            "这是基于当前已确认材料形成的可编辑草稿。"
            "下载前后仍须由律师核对；它不会自动成为法律意见或可提交法院的文书。"
        ),
        version_status="CURRENT",
        revision_number=revision_state.revision_number,
        template_version=revision_state.template_version,
        installed_template_version=revision_state.installed_template_version,
        can_request_revision=False,
        request_status=revision_state.request_status,
        request_id=revision_state.request_id,
        download_ready=True,
        review_version=_document_review_version(package),
        review_artifact_id=revision_state.root_candidate_artifact_id,
        review_pdf_page_count=package.review_pdf_page_count,
        total_item_count=total,
        displayed_item_count=displayed,
        preview_truncated=displayed < total,
        sections=sections,
        columns=columns,
        rows=rows,
    )


def _project_revision_state(
    state: DocumentRevisionState,
) -> WebCaseAgentDocumentReview:
    if state.version_status == "CURRENT":
        raise WebCaseAgentDocumentReviewBlocked(
            "当前文书版本必须完成服务端内容核验后才能显示。"
        )
    notices = {
        "UPDATE_REQUIRED": (
            "这份草稿仍可查阅，但当前排版需要更新。"
            "更新只调整已确认来源，不会覆盖原版本。"
        ),
        "GENERATING": (
            "正在生成可下载版本。完成前，旧版本仍会保留。"
        ),
        "FAILED": (
            "本次文件更新未完成；原版本仍会保留。"
            "确认后可以重新生成。"
        ),
        "UNKNOWN": (
            "当前文件生成状态待确认。系统不会把未知状态当作成功；请稍后刷新查看。"
        ),
    }
    label = _DELIVERABLE_LABELS.get(
        state.deliverable_kind, "可编辑文书候选"
    )
    return WebCaseAgentDocumentReview(
        artifact_id=_uuid(state.requested_artifact_id, "Agent 成果编号"),
        title=label,
        deliverable_kind=state.deliverable_kind,
        deliverable_label=label,
        output_format=state.output_format.value,
        review_notice=notices[state.version_status],
        version_status=state.version_status,  # type: ignore[arg-type]
        revision_number=state.revision_number,
        template_version=state.template_version,
        installed_template_version=state.installed_template_version,
        can_request_revision=state.can_request_revision,
        request_status=state.request_status,
        request_id=state.request_id,
        download_ready=False,
        review_pdf_page_count=0,
        total_item_count=0,
        displayed_item_count=0,
        preview_truncated=False,
    )


def _project_docx(
    payload: dict[str, Any],
    sources: dict[str, WebCaseAgentDocumentSource],
) -> tuple[tuple[WebCaseAgentDocumentSection, ...], int, int]:
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise WebCaseAgentDocumentReviewBlocked("Word 候选正文结构无效。")
    total = sum(
        len(item.get("paragraphs", ()))
        for item in raw_sections
        if isinstance(item, dict) and isinstance(item.get("paragraphs"), list)
    )
    remaining = _MAX_PREVIEW_PARAGRAPHS
    projected: list[WebCaseAgentDocumentSection] = []
    displayed = 0
    for section_index, raw_section in enumerate(raw_sections, 1):
        if remaining <= 0:
            break
        if not isinstance(raw_section, dict):
            raise WebCaseAgentDocumentReviewBlocked("Word 候选正文结构无效。")
        heading = _text(raw_section.get("heading"), "文书章节", _MAX_TITLE)
        raw_paragraphs = raw_section.get("paragraphs")
        if not isinstance(raw_paragraphs, list) or not raw_paragraphs:
            raise WebCaseAgentDocumentReviewBlocked("Word 候选段落结构无效。")
        paragraphs: list[WebCaseAgentDocumentParagraph] = []
        for paragraph_index, raw_paragraph in enumerate(raw_paragraphs, 1):
            if remaining <= 0:
                break
            if not isinstance(raw_paragraph, dict):
                raise WebCaseAgentDocumentReviewBlocked("Word 候选段落结构无效。")
            paragraphs.append(
                WebCaseAgentDocumentParagraph(
                    paragraph_id=f"section-{section_index}-paragraph-{paragraph_index}",
                    text=_text(raw_paragraph.get("text"), "文书段落", _MAX_PARAGRAPH),
                    sources=_resolve_sources(raw_paragraph.get("source_refs"), sources),
                )
            )
            remaining -= 1
            displayed += 1
        projected.append(
            WebCaseAgentDocumentSection(
                section_id=f"section-{section_index}",
                heading=heading,
                paragraphs=tuple(paragraphs),
            )
        )
    return tuple(projected), total, displayed


def _project_xlsx(
    payload: dict[str, Any],
    sources: dict[str, WebCaseAgentDocumentSource],
) -> tuple[
    tuple[WebCaseAgentDocumentColumn, ...],
    tuple[WebCaseAgentDocumentRow, ...],
    int,
    int,
]:
    raw_columns = payload.get("columns")
    raw_rows = payload.get("rows")
    if not isinstance(raw_columns, list) or not raw_columns or not isinstance(raw_rows, list):
        raise WebCaseAgentDocumentReviewBlocked("Excel 候选结构无效。")
    columns = tuple(
        WebCaseAgentDocumentColumn(
            key=_text(item.get("key"), "表格列标识", 80),
            label=_text(item.get("label"), "表格列名", 160),
            value_type=_text(item.get("value_type"), "表格列类型", 20),
        )
        for item in raw_columns
        if isinstance(item, dict)
    )
    if len(columns) != len(raw_columns):
        raise WebCaseAgentDocumentReviewBlocked("Excel 候选列结构无效。")
    rows = []
    for raw_row in raw_rows[:_MAX_PREVIEW_ROWS]:
        if not isinstance(raw_row, dict) or not isinstance(raw_row.get("cells"), dict):
            raise WebCaseAgentDocumentReviewBlocked("Excel 候选行结构无效。")
        cells = raw_row["cells"]
        if set(cells) != {item.key for item in columns}:
            raise WebCaseAgentDocumentReviewBlocked("Excel 候选单元格结构无效。")
        rows.append(
            WebCaseAgentDocumentRow(
                row_id=_text(raw_row.get("row_id"), "表格行编号", 200),
                cells=tuple(_safe_cell(cells[item.key]) for item in columns),
                sources=_resolve_sources(raw_row.get("source_refs"), sources),
            )
        )
    return columns, tuple(rows), len(raw_rows), len(rows)


def _strict_candidate(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_constant,
        )
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise WebCaseAgentDocumentReviewBlocked("文书候选不是可核验的结构化内容。") from error
    if not isinstance(value, dict) or canonical != content:
        raise WebCaseAgentDocumentReviewBlocked("文书候选不是规范的结构化内容。")
    if value.get("schema_version") not in {
        "case-agent-reviewable-docx-candidate-v1",
        "case-agent-reviewable-xlsx-candidate-v1",
    } or (
        value.get("review_status") != "NEEDS_LAWYER_REVIEW"
        or value.get("formal_fact") is not False
        or value.get("formal_legal_conclusion") is not False
        or value.get("court_ready") is not False
    ):
        raise WebCaseAgentDocumentReviewBlocked("文书候选越过了律师复核边界。")
    return value


def _resolve_sources(
    raw: object, sources: dict[str, WebCaseAgentDocumentSource]
) -> tuple[WebCaseAgentDocumentSource, ...]:
    if not isinstance(raw, list) or not raw:
        raise WebCaseAgentDocumentReviewBlocked("文书内容缺少来源。")
    result = []
    for item in raw:
        if not isinstance(item, str) or item not in sources:
            raise WebCaseAgentDocumentReviewBlocked("文书内容引用了未授权来源。")
        result.append(sources[item])
    if len(set(item.source_ref for item in result)) != len(result):
        raise WebCaseAgentDocumentReviewBlocked("文书内容来源重复。")
    return tuple(result)


def _human_actor(identity: ServerIdentityContext):
    if not isinstance(identity, ServerIdentityContext):
        raise WebCaseAgentDocumentReviewBlocked("律师登录状态无效。")
    try:
        identity.validate()
    except Exception as error:
        raise WebCaseAgentDocumentReviewBlocked("律师登录状态无效。") from error
    actor = identity.actor
    if (
        identity.authentication_method is not AuthenticationMethod.OIDC_MFA
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_HUMAN_ROLES)
    ):
        raise WebCaseAgentDocumentReviewBlocked(
            "只有通过 MFA 且仍有本案权限的律师可以读取文书候选。"
        )
    _uuid(actor.actor_id, "律师编号")
    _uuid(actor.firm_id, "律所编号")
    return actor


def _expected_media_type(
    output_format: ReviewableDocumentFormat,
    file_role: Literal["editable", "pdf-preview"],
) -> str:
    if file_role == "pdf-preview":
        return "application/pdf"
    if output_format is ReviewableDocumentFormat.DOCX:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _safe_file_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]+", "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._")
    return (normalized[:80].rstrip(" ._") or "Agent文书候选")


def _safe_cell(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WebCaseAgentDocumentReviewBlocked("Excel 候选单元格类型无效。")


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise WebCaseAgentDocumentReviewBlocked(f"{label}格式无效。")
    return value.strip()


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebCaseAgentDocumentReviewBlocked(f"{label}格式无效。") from error


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


__all__ = (
    "PostgresWebCaseAgentDocumentReviewService",
    "WebCaseAgentDocumentColumn",
    "WebCaseAgentDocumentDownload",
    "WebCaseAgentDocumentParagraph",
    "WebCaseAgentDocumentReview",
    "WebCaseAgentDocumentReviewBlocked",
    "WebCaseAgentDocumentReviewPort",
    "WebCaseAgentDocumentRow",
    "WebCaseAgentDocumentSection",
    "WebCaseAgentDocumentSource",
)
