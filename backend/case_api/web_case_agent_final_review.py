"""Server-side final-review readiness for verified case-Agent artifacts.

The browser is not authoritative about which outputs were actually reviewed.
Immediately before a lawyer completes a run, this coordinator re-opens every
verified artifact through the same safe review surfaces used by the UI.  A
document package is read once per task input; that one read authenticates the
candidate JSON, editable Office file, PDF preview, current source bindings and
the currently installed server template.
"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Protocol, Sequence

from case_kernel.case_agent_supervisor import ArtifactReceipt

from .persistent_identity import ServerIdentityContext
from .web_case_agent_documents import WebCaseAgentDocumentReview


class WebCaseAgentFinalReviewReadinessBlocked(RuntimeError):
    """One or more verified artifacts no longer have a safe review surface."""


class _ArtifactReviewPort(Protocol):
    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> object: ...


class _DocumentReviewPort(Protocol):
    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> WebCaseAgentDocumentReview: ...


_DOCUMENT_KINDS = frozenset(
    {
        "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
        "REVIEWABLE_DOCUMENT_EDITABLE",
        "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
    }
)
_DOCUMENT_CANDIDATE_KIND = "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"


class WebCaseAgentFinalReviewReadiness:
    """Re-read the exact current review surfaces before final completion."""

    def __init__(
        self,
        *,
        artifact_review: _ArtifactReviewPort,
        document_review: _DocumentReviewPort,
    ) -> None:
        if not callable(getattr(artifact_review, "read_review", None)):
            raise ValueError("case Agent artifact review port is invalid")
        if not callable(getattr(document_review, "read_review", None)):
            raise ValueError("case Agent document review port is invalid")
        self._artifact_review = artifact_review
        self._document_review = document_review

    def assert_ready(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifacts: Sequence[ArtifactReceipt],
        expected_document_versions: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        if not artifacts:
            raise WebCaseAgentFinalReviewReadinessBlocked(
                "Agent 没有可供律师终审的成果。"
            )
        document_groups: dict[str, list[ArtifactReceipt]] = defaultdict(list)
        generic: list[ArtifactReceipt] = []
        seen_ids: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, ArtifactReceipt):
                raise WebCaseAgentFinalReviewReadinessBlocked(
                    "Agent 成果清单格式无效。"
                )
            artifact.validate()
            if artifact.artifact_id in seen_ids:
                raise WebCaseAgentFinalReviewReadinessBlocked(
                    "Agent 成果清单包含重复项目。"
                )
            seen_ids.add(artifact.artifact_id)
            if artifact.artifact_kind in _DOCUMENT_KINDS:
                document_groups[artifact.source_input_hash].append(artifact)
            else:
                generic.append(artifact)

        if expected_document_versions is not None:
            expected_ids = {item.artifact_id for item in artifacts if item.artifact_kind == _DOCUMENT_CANDIDATE_KIND}
            if (type(expected_document_versions) is not tuple
                    or len(expected_document_versions) > 128
                    or any(type(item) is not tuple or len(item) != 2
                           or not all(isinstance(value, str) for value in item)
                           or re.fullmatch(r"[a-f0-9]{64}", item[1]) is None for item in expected_document_versions)
                    or len(dict(expected_document_versions)) != len(expected_document_versions)
                    or set(dict(expected_document_versions)) != expected_ids):
                raise WebCaseAgentFinalReviewReadinessBlocked("终审文书版本清单不完整或重复。")
        expected_versions = dict(expected_document_versions or ())
        try:
            for artifact in generic:
                self._artifact_review.read_review(
                    identity=identity,
                    matter_id=matter_id,
                    run_id=run_id,
                    artifact_id=artifact.artifact_id,
                )
            for group in document_groups.values():
                by_kind = {artifact.artifact_kind: artifact for artifact in group}
                if len(group) != len(_DOCUMENT_KINDS) or set(by_kind) != _DOCUMENT_KINDS:
                    raise WebCaseAgentFinalReviewReadinessBlocked(
                        "Agent 文书包缺少候选正文、可编辑源件或 PDF 审阅稿。"
                    )
                candidate_id = by_kind[_DOCUMENT_CANDIDATE_KIND].artifact_id
                review = self._document_review.read_review(
                    identity=identity,
                    matter_id=matter_id,
                    run_id=run_id,
                    artifact_id=candidate_id,
                )
                # A non-throwing read can be a status-only response (for
                # example GENERATING), not an authenticated document surface.
                if (
                    not isinstance(review, WebCaseAgentDocumentReview)
                    or review.artifact_id != candidate_id
                    or review.review_artifact_id != candidate_id
                    or review.version_status != "CURRENT"
                    or review.download_ready is not True
                    or review.template_version != review.installed_template_version
                    or type(review.review_pdf_page_count) is not int
                    or review.review_pdf_page_count < 1
                ):
                    raise WebCaseAgentFinalReviewReadinessBlocked(
                        "文书尚未形成可读取的当前复核稿，不能记录终审完成。"
                    )
                if expected_document_versions is not None and (
                    not isinstance(review.review_version, str)
                    or expected_versions[candidate_id] != review.review_version
                ):
                    raise WebCaseAgentFinalReviewReadinessBlocked("文书已换版，请审阅当前文件后重新终审。")
        except WebCaseAgentFinalReviewReadinessBlocked:
            raise
        except Exception as error:
            raise WebCaseAgentFinalReviewReadinessBlocked(
                "Agent 成果已变化、模板已更新或当前无法完整复核；系统不会记录终审完成。"
            ) from error


__all__ = (
    "WebCaseAgentFinalReviewReadiness",
    "WebCaseAgentFinalReviewReadinessBlocked",
)
