"""Assemble the internal Agent Word/Excel draft executor for one desktop.

This is deliberately not an HTTP service and has no task polling loop.  A
future local Agent supervisor passes already-approved structured content to it
in process.  The runtime derives the SYSTEM_WORKER identity from the enrolled
desktop and refuses to exist unless the isolated Office converter, encrypted
artifact store, persistent Agent ledger, and review-pair store are all present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
from typing import Mapping

from case_kernel.agent_reviewable_draft_coordinator import (
    AgentReviewableDraftExecution,
    execute_agent_reviewable_docx_draft,
    execute_agent_reviewable_xlsx_ledger,
)
from case_kernel.approved_draft_worker import ApprovedDraft
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor
from case_kernel.office_pdf_conversion_worker import SandboxedOfficePdfConverter
from case_kernel.skill_registry import default_case_skill_registry
from case_kernel.skill_tool_gateway import CaseSkillToolGateway

from .desktop_identity_runtime import DesktopIdentityRuntime
from .desktop_persistent_runtime import DesktopPersistentRuntime
from .desktop_system_worker import DesktopSystemWorkerBlocked, load_desktop_system_worker


AGENT_DRAFT_WORKER_ENABLED_ENV = "CASE_WORKBENCH_ENABLE_AGENT_DRAFT_WORKER"
AGENT_DRAFT_WORK_ROOT_ENV = "CASE_WORKBENCH_AGENT_DRAFT_WORK_ROOT"


class DesktopAgentDraftRuntimeBlocked(RuntimeError):
    """The optional Agent Office-draft worker failed a local safeguard."""


@dataclass(frozen=True)
class DesktopAgentDraftRuntime:
    """In-process executor; it cannot be reached through the browser or HTTP."""

    worker: Actor
    work_root: Path
    _gateway: CaseSkillToolGateway
    _persistent_runtime: DesktopPersistentRuntime

    def execute_docx(
        self,
        *,
        matter_id: str,
        expected_version: int,
        proposal_id: str,
        draft: ApprovedDraft,
    ) -> AgentReviewableDraftExecution:
        services = self._persistent_runtime.services
        return execute_agent_reviewable_docx_draft(
            matter_id=matter_id,
            expected_version=expected_version,
            proposal_id=proposal_id,
            worker=self.worker,
            draft=draft,
            case_root=self.work_root,
            gateway=self._gateway,
            proposal_reader=services.agent_execution_store,
            draft_persistence=services.reviewable_draft_store,
            artifact_store=services.artifact_store,
            receipt_writer=services.agent_execution_store,
        )

    def execute_xlsx(
        self,
        *,
        matter_id: str,
        expected_version: int,
        proposal_id: str,
        approval_hash: str,
        sheet_name: str,
        columns: tuple[str, ...],
        rows: tuple[tuple[str | int | float | None, ...], ...],
    ) -> AgentReviewableDraftExecution:
        services = self._persistent_runtime.services
        return execute_agent_reviewable_xlsx_ledger(
            matter_id=matter_id,
            expected_version=expected_version,
            proposal_id=proposal_id,
            worker=self.worker,
            approval_hash=approval_hash,
            sheet_name=sheet_name,
            columns=columns,
            rows=rows,
            case_root=self.work_root,
            gateway=self._gateway,
            proposal_reader=services.agent_execution_store,
            draft_persistence=services.reviewable_draft_store,
            artifact_store=services.artifact_store,
            receipt_writer=services.agent_execution_store,
        )


def build_desktop_agent_draft_runtime(
    *,
    identity: DesktopIdentityRuntime,
    environ: Mapping[str, str],
    persistent_runtime: DesktopPersistentRuntime,
) -> DesktopAgentDraftRuntime | None:
    """Return an explicitly enabled internal executor, otherwise ``None``.

    The enable flag cannot be inferred from Office settings: an operator must
    opt in after installing the isolated converter and configuring the worker
    account.  This function performs no document generation.
    """

    enabled = environ.get(AGENT_DRAFT_WORKER_ENABLED_ENV)
    if enabled is None or not enabled.strip():
        return None
    if enabled != "YES":
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker requires explicit YES enablement")
    services = persistent_runtime.services
    if services.agent_execution_store is None or services.reviewable_draft_store is None:
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker requires persistent Agent and review-draft stores")
    if not isinstance(services.artifact_store, LocalEncryptedArtifactStore):
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker requires encrypted artifact storage")
    if not isinstance(services.office_pdf_converter, SandboxedOfficePdfConverter):
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker requires the isolated Office converter")
    try:
        worker = load_desktop_system_worker(identity=identity, environ=environ)
    except DesktopSystemWorkerBlocked as error:
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker identity is unavailable") from error
    work_root = _configured_private_work_root(environ, services.artifact_store)
    gateway = CaseSkillToolGateway(
        registry=default_case_skill_registry(reviewable_office_drafts_enabled=True),
        office_pdf_converter=services.office_pdf_converter,
    )
    return DesktopAgentDraftRuntime(
        worker=worker,
        work_root=work_root,
        _gateway=gateway,
        _persistent_runtime=persistent_runtime,
    )


def _configured_private_work_root(
    environ: Mapping[str, str], artifact_store: LocalEncryptedArtifactStore
) -> Path:
    raw_root = environ.get(AGENT_DRAFT_WORK_ROOT_ENV, "").strip()
    if not raw_root:
        raise DesktopAgentDraftRuntimeBlocked("Agent draft worker requires a pre-created private work root")
    root = Path(raw_root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise DesktopAgentDraftRuntimeBlocked("Agent draft work root must be an absolute non-symlink directory")
    try:
        resolved = root.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise DesktopAgentDraftRuntimeBlocked("Agent draft work root is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or resolved == Path("/"):
        raise DesktopAgentDraftRuntimeBlocked("Agent draft work root must be a private directory")
    if metadata.st_mode & 0o077:
        raise DesktopAgentDraftRuntimeBlocked("Agent draft work root must not be readable by group or other users")
    managed_root = artifact_store.managed_root.resolve(strict=True)
    if (
        resolved == managed_root
        or resolved.is_relative_to(managed_root)
        or managed_root.is_relative_to(resolved)
    ):
        raise DesktopAgentDraftRuntimeBlocked("Agent draft work root must be separate from encrypted artifacts")
    artifact_store.assert_separate_from_case_root(resolved)
    return resolved
