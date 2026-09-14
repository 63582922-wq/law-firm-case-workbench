"""Server-only delivery of verified evidence derivative PDFs.

The browser can request a derivative id, but the service resolves the
tenant-scoped locator from the ledger and re-verifies the private object before
returning bytes.  Original evidence locators are never accepted by this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.models import Actor, Role


class WebDerivativeDeliveryBlocked(ValueError):
    """A verified derivative cannot be safely delivered to this session."""


@dataclass(frozen=True)
class WebDerivativeDownload:
    file_name: str
    media_type: str
    content: bytes


class _EvidenceStore(Protocol):
    def get_verified_derivative_locator(self, *, matter_id: str, derivative_id: str, actor: Actor): ...


class _ObjectStore(Protocol):
    def materialize_verified_derivative(self, **kwargs): ...


class WebDerivativeDeliveryService:
    def __init__(self, *, evidence_store: _EvidenceStore, object_store: _ObjectStore, worker_root: Path) -> None:
        if not callable(getattr(evidence_store, "get_verified_derivative_locator", None)):
            raise ValueError("Web derivative evidence store is invalid")
        if not callable(getattr(object_store, "materialize_verified_derivative", None)):
            raise ValueError("Web derivative object store is invalid")
        if not isinstance(worker_root, Path) or not worker_root.is_absolute():
            raise ValueError("Web derivative delivery root must be absolute")
        self._evidence_store = evidence_store
        self._object_store = object_store
        self._worker_root = worker_root

    def download(self, *, identity: ServerIdentityContext, matter_id: str, derivative_id: str) -> WebDerivativeDownload:
        actor = _human_identity(identity)
        matter = _uuid(matter_id, "案件编号")
        derivative = _uuid(derivative_id, "派生件编号")
        try:
            locator = self._evidence_store.get_verified_derivative_locator(
                matter_id=matter,
                derivative_id=derivative,
                actor=actor,
            )
        except WebDerivativeDeliveryBlocked:
            raise
        except Exception as error:
            raise WebDerivativeDeliveryBlocked("当前派生件不存在或尚未验证") from error
        if getattr(locator, "firm_id", None) != actor.firm_id or getattr(locator, "matter_id", None) != matter:
            raise WebDerivativeDeliveryBlocked("派生件不属于当前案件")
        if getattr(locator, "status", None) != "VERIFIED":
            raise WebDerivativeDeliveryBlocked("派生件尚未完成服务端验证")
        artifact_hash = getattr(locator, "artifact_sha256", None)
        storage_key = getattr(locator, "object_key", None)
        page_count = getattr(locator, "page_count", None)
        if not isinstance(artifact_hash, str) or len(artifact_hash) != 64 or any(c not in "0123456789abcdef" for c in artifact_hash):
            raise WebDerivativeDeliveryBlocked("派生件完整性信息无效")
        if not isinstance(storage_key, str) or not isinstance(page_count, int) or page_count < 1:
            raise WebDerivativeDeliveryBlocked("派生件存储绑定无效")
        with TemporaryDirectory(prefix="web-derivative-download-", dir=str(self._worker_root)) as temporary:
            target = Path(temporary) / "verified-derivative.pdf"
            try:
                self._object_store.materialize_verified_derivative(
                    firm_id=actor.firm_id,
                    matter_id=matter,
                    storage_object_key=storage_key,
                    artifact_sha256=artifact_hash,
                    page_count=page_count,
                    destination=target,
                )
                content = target.read_bytes()
            except Exception as error:
                raise WebDerivativeDeliveryBlocked("派生件无法通过完整性核验") from error
        if not content or len(content) > 256 * 1024 * 1024 or sha256(content).hexdigest() != artifact_hash:
            raise WebDerivativeDeliveryBlocked("派生件完整性核验失败")
        file_name = "相关页面（红框）.pdf" if getattr(locator, "artifact_type", None) == "ANNOTATED_RELATED_PAGES_PDF" else "相关页面.pdf"
        return WebDerivativeDownload(file_name=file_name, media_type="application/pdf", content=content)


def _human_identity(identity: ServerIdentityContext) -> Actor:
    if not isinstance(identity, ServerIdentityContext) or identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebDerivativeDeliveryBlocked("当前登录状态不能下载证据派生件")
    try:
        identity.validate()
    except Exception as error:
        raise WebDerivativeDeliveryBlocked("登录状态已失效") from error
    actor = identity.actor
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}):
        raise WebDerivativeDeliveryBlocked("当前角色不能下载证据派生件")
    return actor


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebDerivativeDeliveryBlocked(f"{label}格式无效") from error
