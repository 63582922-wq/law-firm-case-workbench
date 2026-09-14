"""Authenticated Web delivery of lawyer-reviewable Office draft pairs.

The browser can identify only a case, a draft pair already listed for that
case, and one fixed delivery purpose.  This module resolves every storage
locator server-side, reauthorizes the current MFA session, and verifies the
private object before a PDF preview or editable Office file leaves the
application boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_access import (
    ReviewableDraftAccessPurpose,
    ReviewableOfficeDraftArtifactLocator,
)

from .persistent_identity import AuthenticationMethod, ServerIdentityContext


class WebDocumentDraftDeliveryBlocked(ValueError):
    """A reviewable draft cannot safely leave the Web application boundary."""


@dataclass(frozen=True)
class WebDocumentDraftDelivery:
    file_name: str
    ascii_file_name: str
    media_type: str
    disposition: Literal["inline", "attachment"]
    artifact_sha256: str
    content: bytes


class _ReviewableDraftStore(Protocol):
    def get_reviewable_office_draft_artifact_locator(
        self,
        *,
        matter_id: str,
        pair_id: str,
        purpose: ReviewableDraftAccessPurpose,
        actor: Actor,
    ) -> ReviewableOfficeDraftArtifactLocator: ...


class _ObjectStore(Protocol):
    def read_verified_review_artifact(
        self, object_key: str, expected_hash: str
    ) -> bytes: ...


_HUMAN_REVIEW_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_MAX_PDF_BYTES = 128 * 1024 * 1024
_MAX_EDITABLE_BYTES = 64 * 1024 * 1024


class WebDocumentDraftDeliveryService:
    """Read only a current, hash-bound draft output for a live Web session."""

    def __init__(self, *, reviewable_store: _ReviewableDraftStore, object_store: _ObjectStore) -> None:
        if not callable(
            getattr(reviewable_store, "get_reviewable_office_draft_artifact_locator", None)
        ):
            raise ValueError("Web reviewable draft store is invalid")
        if not callable(getattr(object_store, "read_verified_review_artifact", None)):
            raise ValueError("Web reviewable draft object store is invalid")
        self._reviewable = reviewable_store
        self._objects = object_store

    def download(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        pair_id: str,
        purpose: Literal["REVIEW_PDF", "DOWNLOAD_EDITABLE"],
    ) -> WebDocumentDraftDelivery:
        actor = _reviewing_human(identity)
        matter = _uuid(matter_id, "案件编号")
        pair = _uuid(pair_id, "文书候选编号")
        try:
            requested_purpose = ReviewableDraftAccessPurpose(purpose)
        except (TypeError, ValueError) as error:
            raise WebDocumentDraftDeliveryBlocked("文书交付用途无效") from error
        try:
            locator = self._reviewable.get_reviewable_office_draft_artifact_locator(
                matter_id=matter,
                pair_id=pair,
                purpose=requested_purpose,
                actor=actor,
            )
        except WebDocumentDraftDeliveryBlocked:
            raise
        except Exception as error:
            raise WebDocumentDraftDeliveryBlocked(
                "该文书候选当前不能提供审阅或下载"
            ) from error
        _validate_locator(
            locator=locator,
            actor=actor,
            matter_id=matter,
            pair_id=pair,
            purpose=requested_purpose,
        )
        try:
            content = self._objects.read_verified_review_artifact(
                locator.object_key, locator.artifact_sha256
            )
        except Exception as error:
            raise WebDocumentDraftDeliveryBlocked(
                "文书候选未能通过服务端完整性核验"
            ) from error
        _validate_content(locator=locator, content=content)
        if requested_purpose is ReviewableDraftAccessPurpose.REVIEW_PDF:
            return WebDocumentDraftDelivery(
                file_name="文书审阅候选.pdf",
                ascii_file_name="reviewable-draft.pdf",
                media_type="application/pdf",
                disposition="inline",
                artifact_sha256=locator.artifact_sha256,
                content=content,
            )
        if locator.media_type == _DOCX_MEDIA_TYPE:
            file_name, ascii_file_name = "文书草稿候选.docx", "reviewable-draft.docx"
        else:
            file_name, ascii_file_name = "收付款核对表候选.xlsx", "reviewable-ledger.xlsx"
        return WebDocumentDraftDelivery(
            file_name=file_name,
            ascii_file_name=ascii_file_name,
            media_type=locator.media_type,
            disposition="attachment",
            artifact_sha256=locator.artifact_sha256,
            content=content,
        )


def _reviewing_human(identity: ServerIdentityContext) -> Actor:
    if (
        not isinstance(identity, ServerIdentityContext)
        or identity.authentication_method is not AuthenticationMethod.OIDC_MFA
    ):
        raise WebDocumentDraftDeliveryBlocked("当前登录状态不能读取文书候选")
    try:
        identity.validate()
        UUID(identity.session_id)
    except Exception as error:
        raise WebDocumentDraftDeliveryBlocked("当前登录状态已失效") from error
    actor = identity.actor
    if (
        not actor.roles.intersection(_HUMAN_REVIEW_ROLES)
        or Role.SYSTEM_WORKER in actor.roles
    ):
        raise WebDocumentDraftDeliveryBlocked("当前角色不能读取文书候选")
    return actor


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebDocumentDraftDeliveryBlocked(f"{label}格式无效") from error


def _validate_locator(
    *,
    locator: object,
    actor: Actor,
    matter_id: str,
    pair_id: str,
    purpose: ReviewableDraftAccessPurpose,
) -> None:
    if not isinstance(locator, ReviewableOfficeDraftArtifactLocator):
        raise WebDocumentDraftDeliveryBlocked("文书候选定位信息无效")
    if (
        locator.firm_id != actor.firm_id
        or locator.matter_id != matter_id
        or locator.pair_id != pair_id
        or locator.purpose is not purpose
        or locator.pair_status not in {"CANDIDATE", "APPROVED"}
    ):
        raise WebDocumentDraftDeliveryBlocked("文书候选不属于当前审阅范围")
    if (
        not isinstance(locator.artifact_sha256, str)
        or len(locator.artifact_sha256) != 64
        or any(char not in "0123456789abcdef" for char in locator.artifact_sha256)
        or locator.object_key
        != f"{locator.artifact_sha256[:2]}/{locator.artifact_sha256[2:4]}/{locator.artifact_sha256}.lca"
    ):
        raise WebDocumentDraftDeliveryBlocked("文书候选完整性绑定无效")
    if purpose is ReviewableDraftAccessPurpose.REVIEW_PDF:
        if locator.media_type != "application/pdf" or not 0 < locator.byte_size <= _MAX_PDF_BYTES:
            raise WebDocumentDraftDeliveryBlocked("文书审阅 PDF 元数据无效")
    elif locator.media_type not in {_DOCX_MEDIA_TYPE, _XLSX_MEDIA_TYPE} or not 0 < locator.byte_size <= _MAX_EDITABLE_BYTES:
        raise WebDocumentDraftDeliveryBlocked("可编辑文书元数据无效")


def _validate_content(*, locator: ReviewableOfficeDraftArtifactLocator, content: object) -> None:
    if not isinstance(content, bytes):
        raise WebDocumentDraftDeliveryBlocked("文书候选内容无效")
    if len(content) != locator.byte_size or sha256(content).hexdigest() != locator.artifact_sha256:
        raise WebDocumentDraftDeliveryBlocked("文书候选完整性核验失败")
    if locator.purpose is ReviewableDraftAccessPurpose.REVIEW_PDF:
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
            raise WebDocumentDraftDeliveryBlocked("文书审阅 PDF 内容无效")
    elif not content.startswith(b"PK"):
        raise WebDocumentDraftDeliveryBlocked("可编辑文书内容无效")
