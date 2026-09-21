"""Browser-safe review projections for independently verified Agent artifacts.

The unified Agent stores its candidate bytes in a private object store.  A
browser must never receive an object key, prompt, provider response, hash, or
raw execution receipt.  This module re-authorises the current human case
reader, proves that the artifact belongs to the current graph and a PASSED
independent verification receipt, re-reads the exact private bytes, and then
projects only lawyer-readable fields from explicitly supported schemas.

The projection remains a review candidate.  Reading it cannot promote a fact,
transaction, evidence decision, legal conclusion, or court-ready document.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import re
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from case_kernel.case_agent_verifier import ArtifactLineageReceipt
from case_kernel.case_agent_lawyer_analysis import (
    LawyerAnalysisBlocked,
    parse_lawyer_decision_package_candidate,
)
from case_kernel.case_agent_sealed_response_recovery import (
    SEALED_RESPONSE_RECOVERY_POLICY_HASH,
)
from case_kernel.case_agent_legal_research_plan import (
    LegalResearchPlanBlocked,
    parse_legal_research_plan_candidate,
)
from case_kernel.models import Role
from case_kernel.web_object_store import StoredCaseAgentReviewCandidate

from .persistent_identity import (
    AuthenticationMethod,
    ServerIdentityContext,
)
from .ocr_text_display import ocr_display_text


class WebCaseAgentArtifactReviewBlocked(RuntimeError):
    """The requested artifact cannot be safely shown to this lawyer."""


@dataclass(frozen=True)
class WebCaseAgentArtifactSource:
    source_kind: str
    source_id: str
    label: str
    evidence_page_id: str | None = None


@dataclass(frozen=True)
class WebCaseAgentArtifactReviewItem:
    item_id: str
    title: str
    detail: str
    badge: str | None
    confidence: float | None
    sources: tuple[WebCaseAgentArtifactSource, ...]
    external_url: str | None = None


@dataclass(frozen=True)
class WebCaseAgentArtifactReviewSection:
    section_id: str
    title: str
    severity: str
    items: tuple[WebCaseAgentArtifactReviewItem, ...]


@dataclass(frozen=True)
class WebCaseAgentArtifactReview:
    artifact_id: str
    artifact_type: str
    title: str
    review_notice: str
    sections: tuple[WebCaseAgentArtifactReviewSection, ...]


@dataclass(frozen=True)
class WebCaseAgentSealedRecoveryArtifact:
    """Browser-safe list item for a non-promoting historical recovery."""

    artifact_id: str
    title: str
    artifact_kind: str


class WebCaseAgentArtifactReviewPort(Protocol):
    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> WebCaseAgentArtifactReview: ...


class WebCaseAgentSealedRecoveryArtifactReader(Protocol):
    def list_sealed_recovery_artifacts(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
    ) -> tuple[WebCaseAgentSealedRecoveryArtifact, ...]: ...


class _PrivateCandidateObjectStore(Protocol):
    def read_case_agent_review_candidate(
        self,
        stored: StoredCaseAgentReviewCandidate,
        *,
        artifact_id: str,
    ) -> bytes: ...


_HUMAN_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_HUMAN_ROLE_VALUES = tuple(sorted(role.value for role in _HUMAN_ROLES))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REF = re.compile(
    r"^(evidence-page|material-object|fact|claim|issue|transaction|"
    r"posture-profile|work-plan-item|legal-source|legal-rule|legal-event|review-obligation|transaction-candidate|fact-candidate):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
_MAX_SECTIONS = 40
_MAX_ITEMS = 500
_DIRECT_EVIDENCE_SOURCE_KINDS = frozenset({"fact", "claim", "transaction"})
_RESOLVABLE_EVIDENCE_SOURCE_KINDS = frozenset(
    {*_DIRECT_EVIDENCE_SOURCE_KINDS, "issue", "review-obligation", "transaction-candidate", "fact-candidate"}
)
_SEALED_RECOVERY_KIND = "SEALED_RESPONSE_REPARSE"
_SEALED_RECOVERY_NOTICE = (
    "这是从已认证的历史模型响应中按当前规则只读恢复的律师审阅候选。"
    "原 Agent 任务仍处于受控阻断状态；它不是任务成功、正式事实、法律结论、文书、"
    "终审成果或可提交法院的材料。"
)


_SOURCE_LABELS = {
    "review-obligation": "待核事项（非确认事实）",
    "transaction-candidate": "未确认交易候选",
    "fact-candidate": "未确认材料记载",
    "evidence-page": "证据页",
    "material-object": "案件材料",
    "fact": "案件事实",
    "claim": "诉请与回应",
    "issue": "争议焦点",
    "transaction": "收付款记录",
    "posture-profile": "代理身份与程序阶段",
    "work-plan-item": "办案计划事项",
    "legal-source": "法律依据",
    "legal-rule": "已核准法律规则",
    "legal-event": "程序事件",
}

_PARTY_KIND_LABELS = {
    "PLAINTIFF": "代理原告",
    "DEFENDANT": "代理被告",
    "APPLICANT": "代理申请人",
    "RESPONDENT": "代理被申请人",
    "OTHER": "其他代理身份",
}
_PROCEDURE_STAGE_LABELS = {
    "PRE_ACTION": "诉前阶段",
    "FIRST_INSTANCE": "一审阶段",
    "SECOND_INSTANCE": "二审阶段",
    "RETRIAL": "再审阶段",
    "ENFORCEMENT": "执行阶段",
    "ARBITRATION": "仲裁阶段",
}
_LAWYER_PRIORITY_LABELS = {
    "CRITICAL": "立即核对",
    "HIGH": "优先核对",
    "MEDIUM": "后续核对",
}
_EVIDENCE_STATUS_LABELS = {
    "SUPPORTED": "证据支持较强",
    "PARTIALLY_SUPPORTED": "证据仅部分支持",
    "PARTIAL": "证据仅部分支持",
    "CONTRADICTED": "存在直接冲突",
    "INSUFFICIENT": "证据不足",
}


class PostgresWebCaseAgentArtifactReviewService:
    """Re-authorise and project one verified candidate for a human reviewer."""

    def __init__(
        self,
        *,
        dsn: str,
        object_store: _PrivateCandidateObjectStore,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case Agent artifact-review PostgreSQL DSN is required")
        if not callable(
            getattr(object_store, "read_case_agent_review_candidate", None)
        ):
            raise ValueError("case Agent artifact-review object store is invalid")
        self._dsn = dsn
        self._object_store = object_store
        self._connect = connection_factory or psycopg.connect

    def read_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> WebCaseAgentArtifactReview:
        actor = _human_actor(identity)
        _uuid(matter_id, "matter_id")
        _uuid(run_id, "run_id")
        _uuid(artifact_id, "artifact_id")
        row = self._read_authoritative_row(
            firm_id=actor.firm_id,
            actor_id=actor.actor_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        if row is None:
            review = self._read_visual_recovery_review(actor=actor, matter_id=matter_id,
                run_id=run_id, recovery_id=artifact_id)
            if review is None:
                raise WebCaseAgentArtifactReviewBlocked(
                    "该成果不存在、尚未通过独立复核，或当前律师无权查看。"
                )
        else:
            content, lineage = self._read_bound_content(row, artifact_id=artifact_id)
            payload = _canonical_object(content)
            review = _project_payload(
                artifact_id=artifact_id,
                artifact_kind=lineage.artifact_kind,
                payload=payload,
            )
            if lineage.recovery_review_only:
                review = replace(review, review_notice=_SEALED_RECOVERY_NOTICE)
        source_page_map = self._read_source_page_map(
            firm_id=actor.firm_id,
            actor_id=actor.actor_id,
            matter_id=matter_id,
            review=review,
        )
        return _bind_structured_sources_to_evidence_pages(
            review=review,
            source_page_map=source_page_map,
        )

    def _read_visual_recovery_review(self, *, actor, matter_id, run_id, recovery_id):
        from case_kernel.visual_candidate_recovery import (
            VisualRecoverySource, VisualCandidateRecoveryBlocked, prepare_visual_candidate_recovery,
        )
        args = dict(actor=actor, matter_id=matter_id, run_id=run_id, recovery_id=recovery_id)
        row = self._load_visual_recovery_row(**args)
        if row is None:
            return None
        try:
            c, recovery = row["source_candidate"], row["recovery"]
            source = VisualRecoverySource(
                firm_id=c["firm_id"], matter_id=c["matter_id"], matter_version=row["snapshot_matter_version"],
                run_id=c["run_id"], run_event_version=row["current_event_version"], run_status=row["run_status"],
                failure_code=row["failure_code"], task_id=c["task_id"], task_status=row["result_status"],
                attempt_id=str(row["attempt_id"]), artifact_id=c["artifact_id"],
                content_sha256=c["content_sha256"], byte_size=c["byte_size"], task_input_hash=c["task_input_hash"],
                external_request_id=row["external_request_id"], cost_minor_units=row["cost_minor_units"],
                input_refs=tuple(row["input_refs"]))
            stored = StoredCaseAgentReviewCandidate(object_key=c["source_object_key"],
                content_sha256=c["content_sha256"],byte_size=c["byte_size"],
                object_version_id=c["source_object_version_id"])
            raw = self._object_store.read_case_agent_review_candidate(stored, artifact_id=source.artifact_id)
            expected = prepare_visual_candidate_recovery(source=source, original=raw)
            if (expected.recovery_id != recovery_id or expected.content_sha256 != recovery["content_sha256"]
                    or len(expected.payload) != recovery["byte_size"]):
                raise VisualCandidateRecoveryBlocked("recovery metadata differs")
            stored_recovery = StoredCaseAgentReviewCandidate(object_key=recovery["object_key"],
                content_sha256=recovery["content_sha256"],byte_size=recovery["byte_size"],
                object_version_id=recovery["object_version_id"])
            content = self._object_store.read_case_agent_review_candidate(stored_recovery, artifact_id=recovery_id)
            if content != expected.payload:
                raise VisualCandidateRecoveryBlocked("recovery bytes differ")
            # Re-authorize after object I/O; never return a revoked/stale candidate.
            if self._load_visual_recovery_row(**args) != row:
                raise VisualCandidateRecoveryBlocked("recovery access or source changed")
            review = _project_payload(artifact_id=recovery_id, artifact_kind="VISUAL_PAGE_REVIEW_CANDIDATE",
                payload=json.loads(content)["interpreted_candidate"])
            return replace(review, title="图片识别恢复件（待复核）", review_notice=(
                "原 Agent 任务仍为历史失败。本件仅由已保存结果在本地重新解释，未重新调用模型；"
                "结构复核不证明全文完整，不是已确认事实、正式文书或法院提交材料。请结合原页核对遗漏。"))
        except (ValueError, TypeError, KeyError, VisualCandidateRecoveryBlocked) as error:
            raise WebCaseAgentArtifactReviewBlocked("图片识别恢复件的原件、版本或恢复链不一致。") from error

    def _load_visual_recovery_row(self, *, actor, matter_id, run_id, recovery_id):
        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SELECT set_config('app.firm_id',%s,true),set_config('app.actor_id',%s,true)",
                               (actor.firm_id,actor.actor_id))
            rows = connection.execute("""
                SELECT to_jsonb(c) AS source_candidate,to_jsonb(recovery) AS recovery,
                       r.status AS run_status,r.current_event_version,r.failure_code,r.snapshot_matter_version,
                       receipt.attempt_id,receipt.result_status,receipt.external_request_id,
                       receipt.cost_minor_units,task.input_refs
                FROM case_agent_visual_recovery_candidates recovery
                JOIN case_agent_review_candidates c ON c.artifact_id=recovery.source_artifact_id
                 AND c.run_id=recovery.run_id AND c.firm_id=recovery.firm_id AND c.matter_id=recovery.matter_id
                JOIN case_agent_runs r ON r.run_id=c.run_id AND r.firm_id=c.firm_id AND r.matter_id=c.matter_id
                JOIN matters m ON m.matter_id=r.matter_id AND m.firm_id=r.firm_id
                JOIN case_agent_artifacts a ON a.artifact_id=c.artifact_id AND a.run_id=c.run_id
                 AND a.firm_id=c.firm_id AND a.matter_id=c.matter_id
                JOIN case_agent_task_receipts receipt ON receipt.receipt_id=a.receipt_id AND receipt.run_id=c.run_id
                 AND receipt.firm_id=c.firm_id AND receipt.matter_id=c.matter_id AND receipt.task_id=c.task_id
                JOIN case_agent_tasks task ON task.task_id=c.task_id AND task.graph_id=c.graph_id AND task.run_id=c.run_id
                JOIN case_agent_task_heads head ON head.task_id=task.task_id AND head.graph_id=task.graph_id
                 AND head.run_id=task.run_id AND head.is_current
                WHERE recovery.recovery_id=%s AND recovery.run_id=%s AND recovery.matter_id=%s AND recovery.firm_id=%s
                 AND recovery.interpretation_policy='visual-candidate-cost-separation-v1'
                 AND recovery.review_status='NEEDS_LAWYER_REVIEW'
                 AND recovery.source_event_version=r.current_event_version
                 AND recovery.source_content_sha256=c.content_sha256
                 AND r.status='FAILED' AND r.failure_code='ARTIFACT_VISUAL_CONTRACT_INVALID'
                 AND receipt.result_status='SUCCEEDED' AND receipt.cost_minor_units=6
                 AND r.snapshot_matter_version=m.version AND NOT r.is_stale AND NOT r.is_cancelled
                 AND c.graph_id=r.current_graph_id AND c.artifact_kind='VISUAL_PAGE_REVIEW_CANDIDATE'
                 AND c.review_status='NEEDS_LAWYER_REVIEW' AND c.media_type='application/json'
                 AND a.content_hash=c.content_sha256 AND a.byte_size=c.byte_size
                 AND a.source_input_hash=c.task_input_hash AND receipt.input_hash=c.task_input_hash
                 AND receipt.external_calls=1 AND receipt.external_submission_state='SUBMITTED'
                 AND task.skill_id='image_visual_ocr' AND head.status='SUCCEEDED'
                 AND EXISTS (SELECT 1 FROM matter_actor_roles role
                   JOIN users principal ON principal.user_id=role.user_id AND principal.firm_id=role.firm_id
                   WHERE role.matter_id=r.matter_id AND role.firm_id=r.firm_id AND role.user_id=%s
                    AND role.role IN ('LEAD_LAWYER','COLLABORATING_LAWYER','REVIEWER','ASSISTANT')
                    AND role.revoked_at IS NULL AND principal.status='ACTIVE')
            """, (recovery_id,run_id,matter_id,actor.firm_id,actor.actor_id)).fetchall()
        return rows[0] if len(rows) == 1 else None

    def _read_bound_content(self, row, *, artifact_id):
        lineage = _authorised_lineage(row, artifact_id=artifact_id)
        stored = StoredCaseAgentReviewCandidate(
            object_key=_private_text(row.get("source_object_key"), "object key"),
            content_sha256=_hash(row.get("content_sha256"), "content hash"),
            byte_size=_positive_int(row.get("byte_size"), "byte size", _MAX_CANDIDATE_BYTES),
            object_version_id=_optional_private_text(row.get("source_object_version_id")),
        )
        if (
            stored.content_sha256 != lineage.content_hash
            or stored.byte_size != lineage.byte_size
            or row.get("artifact_kind") != lineage.artifact_kind
            or row.get("task_input_hash") != lineage.source_input_hash
            or row.get("review_status") != "NEEDS_LAWYER_REVIEW"
        ):
            raise WebCaseAgentArtifactReviewBlocked("Agent 成果的复核链不一致。")
        try:
            content = self._object_store.read_case_agent_review_candidate(
                stored,
                artifact_id=artifact_id,
            )
        except Exception as error:
            raise WebCaseAgentArtifactReviewBlocked(
                "Agent 成果暂时无法从律所案卷库读取。"
            ) from error
        if (
            not isinstance(content, bytes)
            or len(content) != stored.byte_size
            or sha256(content).hexdigest() != stored.content_sha256
        ):
            raise WebCaseAgentArtifactReviewBlocked("Agent 成果内容与接收记录不一致。")
        return content, lineage

    def read_verified_extraction_original(self, *, actor, matter_id: str, artifact_id: str) -> bytes:
        """Server-only original bytes for correction; never an HTTP response.

        Only normal verified/staged extraction artifacts qualify. Historical
        sealed recovery candidates cannot acquire fact-correction authority.
        """
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise WebCaseAgentArtifactReviewBlocked("候选纠正需要本案律师权限。")
        for value, label in ((matter_id,"matter_id"),(artifact_id,"artifact_id"),
                             (actor.actor_id,"actor_id"),(actor.firm_id,"firm_id")):
            _uuid(value,label)
        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SELECT set_config('app.firm_id',%s,true)",(actor.firm_id,))
            connection.execute("SELECT set_config('app.actor_id',%s,true)",(actor.actor_id,))
            batch = connection.execute("""
                SELECT run_id FROM case_agent_ledger_extraction_batches
                WHERE artifact_id=%s AND matter_id=%s AND firm_id=%s
                """,(artifact_id,matter_id,actor.firm_id)).fetchone()
        if batch is None:
            raise WebCaseAgentArtifactReviewBlocked("原提取成果不可用于纠正。")
        row = self._read_verified_authoritative_row(firm_id=actor.firm_id,actor_id=actor.actor_id,
            matter_id=matter_id,run_id=str(batch["run_id"]),artifact_id=artifact_id)
        if row is None:
            raise WebCaseAgentArtifactReviewBlocked("原提取成果的独立复核或当前权限不可证明。")
        content, lineage = self._read_bound_content(row,artifact_id=artifact_id)
        if lineage.artifact_kind != "CASE_LEDGER_EXTRACTION_CANDIDATE" or lineage.recovery_review_only:
            raise WebCaseAgentArtifactReviewBlocked("该成果不是普通材料提取候选。")
        return content

    def _read_authoritative_row(
        self,
        *,
        firm_id: str,
        actor_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        verified = self._read_verified_authoritative_row(
            firm_id=firm_id,
            actor_id=actor_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )
        if verified is not None:
            return verified
        return self._read_sealed_recovery_authoritative_row(
            firm_id=firm_id,
            actor_id=actor_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )

    def _read_verified_authoritative_row(
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
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor_id,)
            )
            return connection.execute(
                """
                SELECT candidate.artifact_id, candidate.artifact_kind,
                       candidate.content_sha256, candidate.byte_size,
                       candidate.task_input_hash, candidate.review_status,
                       candidate.source_object_key,
                       candidate.source_object_version_id,
                       receipt.artifact_lineage
                FROM case_agent_review_candidates candidate
                JOIN case_agent_artifacts artifact
                  ON artifact.artifact_id = candidate.artifact_id
                 AND artifact.run_id = candidate.run_id
                 AND artifact.firm_id = candidate.firm_id
                 AND artifact.matter_id = candidate.matter_id
                 AND artifact.artifact_kind = candidate.artifact_kind
                 AND artifact.content_hash = candidate.content_sha256
                 AND artifact.byte_size = candidate.byte_size
                 AND artifact.source_input_hash = candidate.task_input_hash
                JOIN case_agent_runs run
                  ON run.run_id = candidate.run_id
                 AND run.firm_id = candidate.firm_id
                 AND run.matter_id = candidate.matter_id
                 AND run.current_graph_id = candidate.graph_id
                 AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
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
                JOIN users principal
                  ON principal.user_id = %s
                 AND principal.firm_id = candidate.firm_id
                 AND principal.status = 'ACTIVE'
                WHERE candidate.artifact_id = %s
                  AND candidate.run_id = %s
                  AND candidate.firm_id = %s
                  AND candidate.matter_id = %s
                  AND EXISTS (
                      SELECT 1 FROM matter_actor_roles role
                      WHERE role.user_id = principal.user_id
                        AND role.firm_id = candidate.firm_id
                        AND role.matter_id = candidate.matter_id
                        AND role.role = ANY(%s)
                        AND role.revoked_at IS NULL
                  )
                """,
                (
                    actor_id,
                    artifact_id,
                    run_id,
                    firm_id,
                    matter_id,
                    list(_HUMAN_ROLE_VALUES),
                ),
            ).fetchone()

    def _read_sealed_recovery_authoritative_row(
        self,
        *,
        firm_id: str,
        actor_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        """Read a recovery only while its original blocked boundary is live."""

        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor_id,)
            )
            return connection.execute(
                """
                SELECT candidate.artifact_id, candidate.artifact_kind,
                       candidate.content_sha256, candidate.byte_size,
                       candidate.task_input_hash, candidate.review_status,
                       candidate.source_object_key,
                       candidate.source_object_version_id,
                       recovery.recovery_kind,
                       recovery.source_run_event_version,
                       recovery.source_snapshot_hash,
                       recovery.candidate_content_sha256,
                       recovery.recovery_policy_hash
                FROM case_agent_review_candidates candidate
                JOIN case_agent_sealed_response_recovery_candidates recovery
                  ON recovery.artifact_id = candidate.artifact_id
                 AND recovery.run_id = candidate.run_id
                 AND recovery.graph_id = candidate.graph_id
                 AND recovery.task_id = candidate.task_id
                 AND recovery.firm_id = candidate.firm_id
                 AND recovery.matter_id = candidate.matter_id
                JOIN case_agent_runs run
                  ON run.run_id = candidate.run_id
                 AND run.firm_id = candidate.firm_id
                 AND run.matter_id = candidate.matter_id
                 AND run.current_graph_id = candidate.graph_id
                 AND run.status = 'WAITING_INPUT'
                 AND NOT run.is_stale
                 AND NOT run.is_cancelled
                 AND run.current_event_version = recovery.source_run_event_version
                 AND run.snapshot_hash = recovery.source_snapshot_hash
                JOIN case_agent_tasks task
                  ON task.graph_id = candidate.graph_id
                 AND task.task_id = candidate.task_id
                 AND task.run_id = candidate.run_id
                 AND task.firm_id = candidate.firm_id
                 AND task.matter_id = candidate.matter_id
                 AND task.input_hash = candidate.task_input_hash
                 AND task.tool_id = 'analyze_lawyer_decision_package'
                JOIN case_agent_task_heads head
                  ON head.graph_id = candidate.graph_id
                 AND head.task_id = candidate.task_id
                 AND head.run_id = candidate.run_id
                 AND head.firm_id = candidate.firm_id
                 AND head.matter_id = candidate.matter_id
                 AND head.is_current
                 AND head.status = 'FAILED'
                JOIN case_agent_external_submissions submission
                  ON submission.run_id = candidate.run_id
                 AND submission.task_id = candidate.task_id
                 AND submission.firm_id = candidate.firm_id
                 AND submission.matter_id = candidate.matter_id
                 AND submission.external_request_id = recovery.external_request_id
                 AND submission.request_hash = recovery.request_hash
                 AND submission.recorded_by = recovery.recovered_by
                JOIN case_agent_task_receipts receipt
                  ON receipt.attempt_id = submission.attempt_id
                 AND receipt.run_id = submission.run_id
                 AND receipt.task_id = submission.task_id
                 AND receipt.firm_id = submission.firm_id
                 AND receipt.matter_id = submission.matter_id
                 AND receipt.input_hash = candidate.task_input_hash
                 AND receipt.result_status = 'FAILED'
                 AND receipt.external_submission_state = 'SUBMITTED'
                 AND receipt.error_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'
                 AND receipt.external_request_id = recovery.external_request_id
                 AND receipt.external_calls = 1
                JOIN users principal
                  ON principal.user_id = %s
                 AND principal.firm_id = candidate.firm_id
                 AND principal.status = 'ACTIVE'
                WHERE candidate.artifact_id = %s
                  AND candidate.run_id = %s
                  AND candidate.firm_id = %s
                  AND candidate.matter_id = %s
                  AND recovery.recovery_kind = 'SEALED_RESPONSE_REPARSE'
                  AND recovery.recovery_policy_hash = %s
                  AND recovery.failure_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'
                  AND (
                      SELECT COUNT(*)
                        FROM case_agent_external_submissions counted
                       WHERE counted.run_id = candidate.run_id
                         AND counted.firm_id = candidate.firm_id
                         AND counted.matter_id = candidate.matter_id
                  ) = 1
                  AND EXISTS (
                      SELECT 1 FROM matter_actor_roles role
                      WHERE role.user_id = principal.user_id
                        AND role.firm_id = candidate.firm_id
                        AND role.matter_id = candidate.matter_id
                        AND role.role = ANY(%s)
                        AND role.revoked_at IS NULL
                  )
                """,
                (
                    actor_id,
                    artifact_id,
                    run_id,
                    firm_id,
                    matter_id,
                    SEALED_RESPONSE_RECOVERY_POLICY_HASH,
                    list(_HUMAN_ROLE_VALUES),
                ),
            ).fetchone()

    def list_sealed_recovery_artifacts(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
    ) -> tuple[WebCaseAgentSealedRecoveryArtifact, ...]:
        """List only the safe, still-current recovery candidate identifiers."""

        actor = _human_actor(identity)
        _uuid(matter_id, "matter_id")
        _uuid(run_id, "run_id")
        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (actor.firm_id,)
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor.actor_id,)
            )
            rows = connection.execute(
                """
                SELECT candidate.artifact_id, candidate.artifact_kind
                FROM case_agent_review_candidates candidate
                JOIN case_agent_sealed_response_recovery_candidates recovery
                  ON recovery.artifact_id = candidate.artifact_id
                 AND recovery.run_id = candidate.run_id
                 AND recovery.graph_id = candidate.graph_id
                 AND recovery.task_id = candidate.task_id
                 AND recovery.firm_id = candidate.firm_id
                 AND recovery.matter_id = candidate.matter_id
                JOIN case_agent_runs run
                  ON run.run_id = candidate.run_id
                 AND run.firm_id = candidate.firm_id
                 AND run.matter_id = candidate.matter_id
                 AND run.current_graph_id = candidate.graph_id
                 AND run.status = 'WAITING_INPUT'
                 AND NOT run.is_stale
                 AND NOT run.is_cancelled
                 AND run.current_event_version = recovery.source_run_event_version
                 AND run.snapshot_hash = recovery.source_snapshot_hash
                JOIN case_agent_tasks task
                  ON task.graph_id = candidate.graph_id
                 AND task.task_id = candidate.task_id
                 AND task.run_id = candidate.run_id
                 AND task.firm_id = candidate.firm_id
                 AND task.matter_id = candidate.matter_id
                 AND task.input_hash = candidate.task_input_hash
                 AND task.tool_id = 'analyze_lawyer_decision_package'
                JOIN case_agent_task_heads head
                  ON head.graph_id = candidate.graph_id
                 AND head.task_id = candidate.task_id
                 AND head.run_id = candidate.run_id
                 AND head.firm_id = candidate.firm_id
                 AND head.matter_id = candidate.matter_id
                 AND head.is_current
                 AND head.status = 'FAILED'
                JOIN case_agent_external_submissions submission
                  ON submission.run_id = candidate.run_id
                 AND submission.task_id = candidate.task_id
                 AND submission.firm_id = candidate.firm_id
                 AND submission.matter_id = candidate.matter_id
                 AND submission.external_request_id = recovery.external_request_id
                 AND submission.request_hash = recovery.request_hash
                 AND submission.recorded_by = recovery.recovered_by
                JOIN case_agent_task_receipts receipt
                  ON receipt.attempt_id = submission.attempt_id
                 AND receipt.run_id = submission.run_id
                 AND receipt.task_id = submission.task_id
                 AND receipt.firm_id = submission.firm_id
                 AND receipt.matter_id = submission.matter_id
                 AND receipt.input_hash = candidate.task_input_hash
                 AND receipt.result_status = 'FAILED'
                 AND receipt.external_submission_state = 'SUBMITTED'
                 AND receipt.error_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'
                 AND receipt.external_request_id = recovery.external_request_id
                 AND receipt.external_calls = 1
                JOIN users principal
                  ON principal.user_id = %s
                 AND principal.firm_id = candidate.firm_id
                 AND principal.status = 'ACTIVE'
                WHERE candidate.run_id = %s
                  AND candidate.firm_id = %s
                  AND candidate.matter_id = %s
                  AND candidate.artifact_kind = 'LAWYER_DECISION_PACKAGE_CANDIDATE'
                  AND candidate.review_status = 'NEEDS_LAWYER_REVIEW'
                  AND recovery.recovery_kind = 'SEALED_RESPONSE_REPARSE'
                  AND recovery.recovery_policy_hash = %s
                  AND recovery.failure_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'
                  AND (
                      SELECT COUNT(*)
                        FROM case_agent_external_submissions counted
                       WHERE counted.run_id = candidate.run_id
                         AND counted.firm_id = candidate.firm_id
                         AND counted.matter_id = candidate.matter_id
                  ) = 1
                  AND EXISTS (
                      SELECT 1 FROM matter_actor_roles role
                      WHERE role.user_id = principal.user_id
                        AND role.firm_id = candidate.firm_id
                        AND role.matter_id = candidate.matter_id
                        AND role.role = ANY(%s)
                        AND role.revoked_at IS NULL
                  )
                ORDER BY candidate.artifact_id
                """,
                (
                    actor.actor_id,
                    run_id,
                    actor.firm_id,
                    matter_id,
                    SEALED_RESPONSE_RECOVERY_POLICY_HASH,
                    list(_HUMAN_ROLE_VALUES),
                ),
            ).fetchall()
            visual_rows = connection.execute("""
                SELECT recovery_id FROM case_agent_visual_recovery_candidates
                WHERE firm_id=%s AND matter_id=%s AND run_id=%s
                ORDER BY recovery_id LIMIT 201
            """, (actor.firm_id,matter_id,run_id)).fetchall()
        if len(visual_rows) > 200:
            raise WebCaseAgentArtifactReviewBlocked("恢复件数量超出当前安全读取范围。")
        visual_items = []
        for visual_row in visual_rows:
            recovery_id = _uuid_text(visual_row.get("recovery_id"), "recovery id")
            if self._load_visual_recovery_row(actor=actor,matter_id=matter_id,
                                             run_id=run_id,recovery_id=recovery_id) is not None:
                visual_items.append(WebCaseAgentSealedRecoveryArtifact(
                    artifact_id=recovery_id, title="图片识别恢复件（待复核）",
                    artifact_kind="VISUAL_PAGE_REVIEW_CANDIDATE"))
        return (*visual_items, *tuple(
            WebCaseAgentSealedRecoveryArtifact(
                artifact_id=_uuid_text(row.get("artifact_id"), "artifact id"),
                title="封存模型响应恢复 · 律师决策包候选",
                artifact_kind=_text(
                    row.get("artifact_kind"), "artifact kind", 200
                ),
            )
            for row in rows
        ))

    def _read_source_page_map(
        self,
        *,
        firm_id: str,
        actor_id: str,
        matter_id: str,
        review: WebCaseAgentArtifactReview,
    ) -> dict[tuple[str, str], str]:
        requested: dict[str, list[str]] = {
            "fact": [],
            "claim": [],
            "issue": [],
            "transaction": [],
            "review-obligation": [],
            "transaction-candidate": [],
            "fact-candidate": [],
        }
        seen: set[tuple[str, str]] = set()
        for section in review.sections:
            for item in section.items:
                for source in item.sources:
                    key = (source.source_kind, source.source_id)
                    if source.source_kind in requested and key not in seen:
                        requested[source.source_kind].append(source.source_id)
                        seen.add(key)
        if not seen:
            return {}

        with self._connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)", (firm_id,)
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor_id,)
            )
            rows = connection.execute(
                """
                WITH requested_pages AS (
                    SELECT 'fact'::text AS source_kind,
                           fact.fact_id AS source_id,
                           link.value ->> 'evidence_id' AS evidence_page_id
                    FROM case_facts fact
                    CROSS JOIN LATERAL jsonb_array_elements(fact.evidence_links) link(value)
                    WHERE fact.firm_id = %s
                      AND fact.matter_id = %s
                      AND fact.fact_id = ANY(%s::uuid[])
                    UNION ALL
                    SELECT 'claim'::text,
                           claim.claim_id,
                           link.value ->> 'evidence_id'
                    FROM case_claims claim
                    CROSS JOIN LATERAL jsonb_array_elements(claim.evidence_links) link(value)
                    WHERE claim.firm_id = %s
                      AND claim.matter_id = %s
                      AND claim.claim_id = ANY(%s::uuid[])
                    UNION ALL
                    SELECT 'transaction'::text,
                           txn.transaction_id,
                           link.value ->> 'evidence_id'
                    FROM case_transactions txn
                    CROSS JOIN LATERAL jsonb_array_elements(txn.evidence_links) link(value)
                    WHERE txn.firm_id = %s
                      AND txn.matter_id = %s
                      AND txn.transaction_id = ANY(%s::uuid[])
                    UNION ALL
                    SELECT 'issue'::text,
                           issue.issue_id,
                           link.value ->> 'evidence_id'
                    FROM case_dispute_issues issue
                    JOIN case_dispute_issue_facts issue_fact
                      ON issue_fact.issue_id = issue.issue_id
                     AND issue_fact.firm_id = issue.firm_id
                     AND issue_fact.matter_id = issue.matter_id
                    JOIN case_facts fact
                      ON fact.fact_id = issue_fact.fact_id
                     AND fact.firm_id = issue_fact.firm_id
                     AND fact.matter_id = issue_fact.matter_id
                    CROSS JOIN LATERAL jsonb_array_elements(fact.evidence_links) link(value)
                    WHERE issue.firm_id = %s
                      AND issue.matter_id = %s
                      AND issue.issue_id = ANY(%s::uuid[])
                    UNION ALL
                    SELECT 'issue'::text,
                           issue.issue_id,
                           link.value ->> 'evidence_id'
                    FROM case_dispute_issues issue
                    JOIN case_dispute_issue_claims issue_claim
                      ON issue_claim.issue_id = issue.issue_id
                     AND issue_claim.firm_id = issue.firm_id
                     AND issue_claim.matter_id = issue.matter_id
                    JOIN case_claims claim
                      ON claim.claim_id = issue_claim.claim_id
                     AND claim.firm_id = issue_claim.firm_id
                     AND claim.matter_id = issue_claim.matter_id
                    CROSS JOIN LATERAL jsonb_array_elements(claim.evidence_links) link(value)
                    WHERE issue.firm_id = %s
                      AND issue.matter_id = %s
                      AND issue.issue_id = ANY(%s::uuid[])
                )
                SELECT DISTINCT ON (requested.source_kind, requested.source_id)
                       requested.source_kind,
                       requested.source_id::text,
                       page.evidence_page_id::text
                FROM requested_pages requested
                JOIN evidence_pages page
                  ON page.evidence_page_id::text = requested.evidence_page_id
                 AND page.firm_id = %s
                 AND page.matter_id = %s
                JOIN evidence_original_files original
                  ON original.evidence_file_id = page.evidence_file_id
                 AND original.firm_id = page.firm_id
                 AND original.matter_id = page.matter_id
                ORDER BY requested.source_kind, requested.source_id,
                         original.original_label, page.page_number,
                         page.evidence_page_id
                """,
                (
                    firm_id,
                    matter_id,
                    requested["fact"],
                    firm_id,
                    matter_id,
                    requested["claim"],
                    firm_id,
                    matter_id,
                    requested["transaction"],
                    firm_id,
                    matter_id,
                    requested["issue"],
                    firm_id,
                    matter_id,
                    requested["issue"],
                    firm_id,
                    matter_id,
                ),
            ).fetchall()
            obligation_rows = []
            for candidate_prefix, candidate_kind in (("transaction-candidate", "TRANSACTION"), ("fact-candidate", "FACT")):
                if not requested[candidate_prefix]:
                    continue
                rows.extend(connection.execute("""
                    SELECT %s AS source_kind,
                           candidate.extraction_candidate_id::text AS source_id,
                           page.evidence_page_id::text
                    FROM case_agent_ledger_extraction_candidates candidate
                    JOIN case_agent_ledger_extraction_candidate_pages link
                      ON link.extraction_candidate_id=candidate.extraction_candidate_id
                     AND link.firm_id=candidate.firm_id AND link.matter_id=candidate.matter_id
                    JOIN evidence_pages page ON page.evidence_page_id=link.evidence_page_id
                     AND page.firm_id=link.firm_id AND page.matter_id=link.matter_id
                    WHERE candidate.firm_id=%s AND candidate.matter_id=%s
                      AND candidate.candidate_kind=%s
                      AND candidate.extraction_candidate_id=ANY(%s::uuid[])
                    ORDER BY candidate.extraction_candidate_id,page.page_number,page.evidence_page_id
                    """, (candidate_prefix, firm_id, matter_id, candidate_kind, requested[candidate_prefix])).fetchall())
            if requested["review-obligation"]:
                obligation_rows = connection.execute(
                    """
                    SELECT f.followup_id::text,
                           array_agg(DISTINCT page.evidence_page_id::text
                                     ORDER BY page.evidence_page_id::text) AS page_ids
                    FROM case_agent_ledger_exception_followups f
                    JOIN case_agent_ledger_exception_followup_heads head
                      ON head.followup_id=f.followup_id AND head.firm_id=f.firm_id
                     AND head.matter_id=f.matter_id AND head.current_state='ACTIVE'
                    JOIN case_agent_ledger_exception_group_members member
                      ON member.exception_group_id=f.origin_exception_group_id
                     AND member.extraction_batch_id=f.origin_extraction_batch_id
                     AND member.firm_id=f.firm_id AND member.matter_id=f.matter_id
                    JOIN case_agent_ledger_extraction_candidate_pages link
                      ON link.extraction_candidate_id=member.extraction_candidate_id
                     AND link.firm_id=member.firm_id AND link.matter_id=member.matter_id
                    JOIN evidence_pages page
                      ON page.evidence_page_id=link.evidence_page_id
                     AND page.firm_id=f.firm_id AND page.matter_id=f.matter_id
                    WHERE f.firm_id=%s AND f.matter_id=%s
                      AND f.followup_kind IN ('DEFERRED_REVIEW','MORE_EVIDENCE')
                    GROUP BY f.followup_id
                    """, (firm_id, matter_id)).fetchall()
        result: dict[tuple[str, str], str] = {}
        for row in obligation_rows:
            lifecycle_id = _uuid_text(row.get("followup_id"), "review followup")
            page_ids = tuple(_uuid_text(value, "review source page") for value in row["page_ids"])
            for offset in range(0, len(page_ids), 100):
                source_id = str(uuid5(UUID(lifecycle_id),
                    f"ledger-exception-current-signal-v2:ACTIVE_FOLLOWUP:{offset // 100 + 1}"))
                if source_id in requested["review-obligation"]:
                    # The browser opens the first page of this exact source
                    # shard, not an invented page or a different followup.
                    result[("review-obligation", source_id)] = page_ids[offset]
        for row in rows:
            source_kind = _text(row.get("source_kind"), "source kind", 80)
            source_id = _uuid_text(row.get("source_id"), "source id")
            evidence_page_id = _uuid_text(
                row.get("evidence_page_id"), "evidence page id"
            )
            result[(source_kind, source_id)] = evidence_page_id
        return result


def _bind_structured_sources_to_evidence_pages(
    *,
    review: WebCaseAgentArtifactReview,
    source_page_map: dict[tuple[str, str], str],
) -> WebCaseAgentArtifactReview:
    sections: list[WebCaseAgentArtifactReviewSection] = []
    for section in review.sections:
        items: list[WebCaseAgentArtifactReviewItem] = []
        for item in section.items:
            sources: list[WebCaseAgentArtifactSource] = []
            for source in item.sources:
                if source.source_kind not in _RESOLVABLE_EVIDENCE_SOURCE_KINDS:
                    sources.append(source)
                    continue
                page_id = source_page_map.get(
                    (source.source_kind, source.source_id)
                )
                if (
                    page_id is None
                    and source.source_kind in _DIRECT_EVIDENCE_SOURCE_KINDS
                ):
                    raise WebCaseAgentArtifactReviewBlocked(
                        "Agent 成果引用的案件对象无法回到本案原始证据页。"
                    )
                sources.append(replace(source, evidence_page_id=page_id))
            items.append(replace(item, sources=tuple(sources)))
        sections.append(replace(section, items=tuple(items)))
    return replace(review, sections=tuple(sections))


def _human_actor(identity: ServerIdentityContext):
    if not isinstance(identity, ServerIdentityContext):
        raise WebCaseAgentArtifactReviewBlocked("律师登录状态无效。")
    identity.validate()
    actor = identity.actor
    if (
        identity.authentication_method is not AuthenticationMethod.OIDC_MFA
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_HUMAN_ROLES)
    ):
        raise WebCaseAgentArtifactReviewBlocked(
            "只有通过 MFA 且仍有本案权限的律师可以读取 Agent 成果。"
        )
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "firm_id")
    return actor


@dataclass(frozen=True)
class _ReviewArtifactLineage:
    artifact_kind: str
    content_hash: str
    byte_size: int
    source_input_hash: str
    recovery_review_only: bool


def _authorised_lineage(
    row: dict[str, Any], *, artifact_id: str
) -> _ReviewArtifactLineage:
    if row.get("recovery_kind") == _SEALED_RECOVERY_KIND:
        if (
            row.get("artifact_kind") != "LAWYER_DECISION_PACKAGE_CANDIDATE"
            or row.get("candidate_content_sha256") != row.get("content_sha256")
            or row.get("recovery_policy_hash")
            != SEALED_RESPONSE_RECOVERY_POLICY_HASH
        ):
            raise WebCaseAgentArtifactReviewBlocked("封存响应恢复链不一致。")
        _positive_int(
            row.get("source_run_event_version"),
            "sealed recovery event version",
            10_000_000,
        )
        return _ReviewArtifactLineage(
            artifact_kind=_text(row.get("artifact_kind"), "artifact kind", 200),
            content_hash=_hash(row.get("content_sha256"), "content hash"),
            byte_size=_positive_int(
                row.get("byte_size"), "byte size", _MAX_CANDIDATE_BYTES
            ),
            source_input_hash=_hash(row.get("task_input_hash"), "task input hash"),
            recovery_review_only=True,
        )
    if row.get("recovery_kind") is not None:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果恢复类型无效。")
    lineage = _verified_lineage(row, artifact_id=artifact_id)
    return _ReviewArtifactLineage(
        artifact_kind=lineage.artifact_kind,
        content_hash=lineage.content_hash,
        byte_size=lineage.byte_size,
        source_input_hash=lineage.source_input_hash,
        recovery_review_only=False,
    )


def _verified_lineage(row: dict[str, Any], *, artifact_id: str) -> ArtifactLineageReceipt:
    value = row.get("artifact_lineage")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise WebCaseAgentArtifactReviewBlocked("Agent 成果复核记录无效。") from error
    if not isinstance(value, list):
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果复核记录无效。")
    matches = [item for item in value if isinstance(item, dict) and item.get("artifact_id") == artifact_id]
    if len(matches) != 1:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果未出现在独立复核清单中。")
    try:
        lineage = ArtifactLineageReceipt(**matches[0])
        lineage.validate()
    except (TypeError, ValueError) as error:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果复核记录无效。") from error
    return lineage


def _canonical_object(content: bytes) -> dict[str, Any]:
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
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果不是可核验的结构化内容。") from error
    if not isinstance(value, dict) or canonical != content:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果不是规范的结构化内容。")
    if value.get("review_status") != "NEEDS_LAWYER_REVIEW":
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果的律师复核状态无效。")
    for field in (
        "formal_fact",
        "formal_transaction",
        "legal_conclusion",
        "evidence_decision",
        "court_ready",
    ):
        if field in value and value[field] is not False:
            raise WebCaseAgentArtifactReviewBlocked("Agent 成果越过了律师确认边界。")
    return value


def _project_payload(
    *, artifact_id: str, artifact_kind: str, payload: dict[str, Any]
) -> WebCaseAgentArtifactReview:
    if artifact_kind == "PDF_TEXT_REVIEW_CANDIDATE":
        title = "PDF 文本读取结果"
        sections = (_pdf_section(payload),)
    elif artifact_kind == "COMMON_DOCUMENT_REVIEW_CANDIDATE":
        title = "Word / Excel 材料读取结果"
        sections = _office_sections(payload)
    elif artifact_kind == "VISUAL_PAGE_REVIEW_CANDIDATE":
        title = "图片与扫描页识别结果"
        sections = _visual_sections(payload)
    elif artifact_kind == "PUBLIC_RESEARCH_LEADS_CANDIDATE":
        title = "网络检索线索"
        sections = (_research_section(payload),)
    elif artifact_kind == "LEGAL_RESEARCH_PLAN_CANDIDATE":
        try:
            validated = parse_legal_research_plan_candidate(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except LegalResearchPlanBlocked as error:
            raise WebCaseAgentArtifactReviewBlocked(
                "法源研究计划未通过完整结构复核。"
            ) from error
        title, sections = _legal_research_plan_sections(dict(validated))
    elif artifact_kind == "CASE_CONTEXT_REVIEW_CANDIDATE":
        title, sections = _case_context_sections(payload)
    elif artifact_kind == "CASE_LEDGER_EXTRACTION_CANDIDATE":
        title, sections = _ledger_extraction_sections(payload)
    elif artifact_kind == "LAWYER_DECISION_PACKAGE_CANDIDATE":
        try:
            validated = parse_lawyer_decision_package_candidate(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except LawyerAnalysisBlocked as error:
            raise WebCaseAgentArtifactReviewBlocked(
                "律师决策包未通过完整结构复核。"
            ) from error
        title, sections = _lawyer_decision_package_sections(dict(validated))
    else:
        raise WebCaseAgentArtifactReviewBlocked("该类成果尚无安全的律师阅读视图。")
    if not sections or len(sections) > _MAX_SECTIONS:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果分组数量无效。")
    if sum(len(section.items) for section in sections) > _MAX_ITEMS:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果项目过多，需由 Worker 重新分批。")
    notice = (
        "这是待律师复核的案件研判。请结合每项来源核对；它不会自动成为"
        "已确认事实、证据结论、法律意见或可提交法院的文件。"
    )
    if artifact_kind == "CASE_CONTEXT_REVIEW_CANDIDATE":
        # Derive coverage from parsed references, never model-authored counts.
        references = {(source.source_kind, source.source_id)
            for section in sections for item in section.items for source in item.sources}
        posture_only = bool(references) and all(kind == "posture-profile" for kind, _ in references)
        notice = (
            f"本成果仅汇总本次任务引用的 {len(references)} 个不同来源对象，不是整案分析。"
            + ("本次仅覆盖代理档案，未覆盖证据内容、诉请攻防、法律适用或应诉文书。" if posture_only else
               "引用对象数量不代表全部案卷覆盖率，也不证明已完成证据分析、法律适用或文书起草。")
            + "待确认项为零不代表案件无风险、无缺口或可以提交。"
            + "请核对原始来源与实际案卷范围，再决定需要补充的材料和后续分析。"
            + "本成果不是正式事实、法律结论或法院提交文件。"
        )
        title = "案件台账核对（非整案分析）"
    return WebCaseAgentArtifactReview(
        artifact_id=artifact_id,
        artifact_type=artifact_kind,
        title=title,
        review_notice=notice,
        sections=sections,
    )


def _ledger_extraction_sections(payload: dict[str, Any]):
    from case_kernel.case_agent_ledger_extraction import (
        parse_case_ledger_extraction_candidate, CaseLedgerExtractionBlocked,
    )
    try:
        validated = parse_case_ledger_extraction_candidate(json.dumps(payload,
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (CaseLedgerExtractionBlocked, ValueError, TypeError) as error:
        raise WebCaseAgentArtifactReviewBlocked("材料提取候选未通过来源结构复核。") from error
    candidates = validated["candidates"]
    # A matching date/amount/party is only a collision signal, never authority
    # to merge original records or count a payment once/twice in a legal sum.
    def collision_key(item):
        if (item["kind"] != "TRANSACTION" or item.get("date_precision") != "EXACT_DATE"
                or not item.get("payer_label") or not item.get("payee_label")):
            return None
        return tuple(item.get(key) for key in ("local_date", "currency", "amount", "channel", "payer_label", "payee_label"))
    counts = {}
    for item in candidates:
        key = collision_key(item)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    facts, transactions = [], []
    for item in candidates:
        excerpts = "\n".join(excerpt["text"] for excerpt in item["supporting_excerpts"])
        badge = "待核对，未确认"
        if item["kind"] == "FACT":
            title = item["fact_text"]
            detail = f"原文摘录：\n{excerpts}"
            target = facts
        else:
            title = f"{item.get('local_date') or '日期待核'} · {item['currency']} {item['amount']}"
            detail = f"付款方：{item.get('payer_label') or '待核'} → 收款方：{item.get('payee_label') or '待核'}\n原文摘录：\n{excerpts}"
            if counts.get(collision_key(item), 0) > 1:
                badge = "可能重复，待核对"
                detail += "\n同日、同额、同交易双方的记录不止一条。保留各条原件，核对是否同笔交易后再决定是否计入。"
            if item.get("direction") == "UNKNOWN":
                detail += "\n交易方向未确认，不作为还款自动计入。"
            target = transactions
        target.append(WebCaseAgentArtifactReviewItem(item_id=f"extracted-{item['candidate_hash']}",
            title=title, detail=detail, badge=badge, confidence=item["confidence"],
            sources=tuple(_source(ref) for ref in item["source_refs"])))
    sections = []
    if facts:
        sections.append(WebCaseAgentArtifactReviewSection("extracted-facts", "材料记载", "MEDIUM", tuple(facts)))
    if transactions:
        sections.append(WebCaseAgentArtifactReviewSection("extracted-transactions", "逐笔收付款", "HIGH",
            tuple(sorted(transactions, key=lambda item: (item.title, item.item_id)))))
    if not sections:
        sections.append(WebCaseAgentArtifactReviewSection("extracted-empty", "提取结果", "MEDIUM", (
            WebCaseAgentArtifactReviewItem("no-candidates", "未提取到事实或交易候选",
                "这不代表材料没有证明价值，请回到原页核对。", "待核对", None, ()),)))
    return "材料与收付款核对", tuple(sections)


def _pdf_section(payload: dict[str, Any]) -> WebCaseAgentArtifactReviewSection:
    if payload.get("schema_version") != "agent-pdf-text-candidate-v1":
        raise WebCaseAgentArtifactReviewBlocked("PDF 成果版本不受支持。")
    pages = _list(payload.get("pages"), "PDF pages", 1, 200)
    items = []
    for index, page in enumerate(pages, 1):
        page = _object(page, "PDF page")
        page_id = _uuid_text(page.get("evidence_page_id"), "evidence page")
        page_number = _positive_int(page.get("page_number"), "page number", 100_000)
        text = _text(page.get("extracted_text"), "page text", 200_000, allow_empty=True)
        source = _source(f"evidence-page:{page_id}")
        items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=f"pdf-page-{index}",
                title=f"第 {page_number} 页",
                detail=text[:8_000] if text else "该页没有可读取的文本层，需进入图片/OCR 队列。",
                badge="需 OCR" if not text else None,
                confidence=None,
                sources=(source,),
            )
        )
    return WebCaseAgentArtifactReviewSection("pdf-pages", "逐页读取", "MEDIUM", tuple(items))


def _office_sections(payload: dict[str, Any]) -> tuple[WebCaseAgentArtifactReviewSection, ...]:
    if payload.get("schema_version") != "agent-common-document-candidate-v1":
        raise WebCaseAgentArtifactReviewBlocked("Office 成果版本不受支持。")
    documents = _list(payload.get("documents"), "documents", 1, 100)
    sections = []
    for index, value in enumerate(documents, 1):
        document = _object(value, "document")
        source = _source(_text(document.get("input_ref"), "input ref", 200))
        candidates = _list(document.get("candidates"), "document candidates", 0, 500)
        items = []
        for item_index, candidate_value in enumerate(candidates, 1):
            candidate = _object(candidate_value, "document candidate")
            kind = _text(candidate.get("kind"), "candidate kind", 80)
            risks = _string_list(candidate.get("risk_flags"), "risk flags", 50)
            items.append(
                WebCaseAgentArtifactReviewItem(
                    item_id=_safe_id(candidate.get("candidate_id"), f"office-{index}-{item_index}"),
                    title=_office_kind_label(kind),
                    detail=_text(candidate.get("text"), "candidate text", 50_000, allow_empty=True)[:8_000],
                    badge="、".join(risks[:3]) if risks else None,
                    confidence=None,
                    sources=(source,),
                )
            )
        risk_flags = _string_list(document.get("document_risk_flags"), "document risks", 50)
        sections.append(
            WebCaseAgentArtifactReviewSection(
                section_id=f"office-{index}",
                title=f"{_format_label(document.get('detected_format'))} 材料 {index}",
                severity="HIGH" if risk_flags else "MEDIUM",
                items=tuple(items) or (
                    WebCaseAgentArtifactReviewItem(
                        item_id=f"office-{index}-empty",
                        title="未提取到可复核文本",
                        detail="该材料需要人工打开或进入图片识别队列。",
                        badge="需进一步处理",
                        confidence=None,
                        sources=(source,),
                    ),
                ),
            )
        )
    return tuple(sections)


def _visual_sections(payload: dict[str, Any]) -> tuple[WebCaseAgentArtifactReviewSection, ...]:
    if payload.get("schema_version") not in {
        "agent-visual-page-candidate-bundle-v1",
        "agent-visual-page-candidate-v1",
    }:
        raise WebCaseAgentArtifactReviewBlocked("视觉识别成果版本不受支持。")
    pages = _list(payload.get("pages"), "visual pages", 1, 100)
    sections = []
    for page_index, page_value in enumerate(pages, 1):
        page = _object(page_value, "visual page")
        page_id = _uuid_text(page.get("evidence_page_id"), "evidence page")
        source = _source(f"evidence-page:{page_id}")
        items: list[WebCaseAgentArtifactReviewItem] = []
        for block_index, block_value in enumerate(
            _list(page.get("text_blocks"), "text blocks", 0, 500), 1
        ):
            block = _object(block_value, "text block")
            raw = _text(block.get("text"), "visual text", 50_000, allow_empty=True)
            display, converted = ocr_display_text(raw)
            block_id = _safe_id(block.get("block_id"), f"visual-{page_index}-text-{block_index}")
            confidence = _confidence(block.get("confidence"))
            server_bound = block.get("block_id") == "ocr-full-page"
            kind = _text(block.get("kind"), "visual kind", 80)
            # Paginate presentation, never silently lose the tail of a long page.
            chunks = [display[i:i + 8_000] for i in range(0, len(display), 8_000)] or [""]
            for chunk_index, chunk in enumerate(chunks, 1):
                items.append(WebCaseAgentArtifactReviewItem(
                    item_id=block_id if len(chunks) == 1 else f"{block_id}-part-{chunk_index}",
                    title="识别文字" if len(chunks) == 1 else f"识别文字（{chunk_index}/{len(chunks)}）",
                    detail=chunk,
                    badge="格式已转为纯文字" if converted else kind,
                    confidence=None if server_bound else confidence,
                    sources=(source,),
                ))
        items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=f"visual-{page_index}-completeness",
                title="全文完整性待核对",
                detail="结构校验通过不代表逐字识别完整，页眉、印章、手写内容仍可能遗漏。请结合原页核对；系统不会补写未识别文字，也不会将其自动确认为事实。",
                badge="待律师复核",
                confidence=None,
                sources=(source,),
            )
        )
        for field_index, field_value in enumerate(
            _list(page.get("fields"), "visual fields", 0, 500), 1
        ):
            field = _object(field_value, "visual field")
            items.append(
                WebCaseAgentArtifactReviewItem(
                    item_id=_safe_id(field.get("field_id"), f"visual-{page_index}-field-{field_index}"),
                    title=_text(field.get("kind"), "field kind", 80),
                    detail=_text(field.get("value"), "field value", 10_000, allow_empty=True),
                    badge=_optional_text(field.get("currency"), 16),
                    confidence=_confidence(field.get("confidence")),
                    sources=(source,),
                )
            )
        for risk_index, risk_value in enumerate(
            _list(page.get("quality_risks"), "quality risks", 0, 100), 1
        ):
            risk = _object(risk_value, "quality risk")
            items.append(
                WebCaseAgentArtifactReviewItem(
                    item_id=f"visual-{page_index}-risk-{risk_index}",
                    title="识别质量风险",
                    detail=_optional_text(risk.get("note"), 1_000) or _text(risk.get("code"), "risk code", 80),
                    badge=_text(risk.get("severity"), "risk severity", 40),
                    confidence=_confidence(risk.get("confidence")),
                    sources=(source,),
                )
            )
        if len(items) == 1 and items[0].title == "全文完整性待核对":
            items.insert(0, WebCaseAgentArtifactReviewItem(
                f"visual-page-{page_index}-empty", "未识别到可靠内容",
                "请回到原页人工核对。", "需人工复核", None, (source,),
            ))
        sections.append(
            WebCaseAgentArtifactReviewSection(
                f"visual-page-{page_index}",
                f"扫描页 {page_index}",
                "HIGH" if any(item.title == "识别质量风险" for item in items) else "MEDIUM",
                tuple(items) or (
                    WebCaseAgentArtifactReviewItem(
                        f"visual-page-{page_index}-empty",
                        "未识别到可靠内容",
                        "请回到原页人工核对。",
                        "需人工复核",
                        None,
                        (source,),
                    ),
                ),
            )
        )
    return tuple(sections)


def _research_section(payload: dict[str, Any]) -> WebCaseAgentArtifactReviewSection:
    if payload.get("schema_version") != "agent-public-research-leads-candidate-v1":
        raise WebCaseAgentArtifactReviewBlocked("网络检索成果版本不受支持。")
    leads = _list(payload.get("leads"), "research leads", 1, 20)
    items = []
    for index, value in enumerate(leads, 1):
        lead = _object(value, "research lead")
        url = _safe_public_https_url(lead.get("url"))
        injection = _string_list(
            lead.get("prompt_injection_signals"), "prompt-injection signals", 20
        )
        items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(lead.get("lead_id"), f"research-{index}"),
                title=_text(lead.get("title"), "research title", 500),
                detail=_text(lead.get("snippet"), "research snippet", 4_000, allow_empty=True),
                badge=("疑似网页指令污染" if injection else _authority_label(lead.get("authority_class"))),
                confidence=None,
                sources=(),
                external_url=url,
            )
        )
    return WebCaseAgentArtifactReviewSection(
        "public-research-leads",
        "待核验的公开网络线索",
        "HIGH",
        tuple(items),
    )


def _legal_research_plan_sections(
    payload: dict[str, Any],
) -> tuple[str, tuple[WebCaseAgentArtifactReviewSection, ...]]:
    questions = _list(payload.get("questions"), "legal research questions", 1, 20)
    question_items: list[WebCaseAgentArtifactReviewItem] = []
    for index, value in enumerate(questions, 1):
        row = _object(value, f"legal research question {index}")
        terms = _string_list(
            row.get("proposed_public_terms"), "legal research public terms", 24
        )
        checks = _string_list(
            row.get("required_checks"), "legal research checks", 20
        )
        source_values = _string_list(
            row.get("source_refs"), "legal research source refs", 20
        )
        status_value = _text(
            row.get("status"), "legal research question status", 80
        )
        question_items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(
                    row.get("question_id"), f"legal-research-question-{index}"
                ),
                title=_text(row.get("title"), "legal research title", 240),
                detail=(
                    _text(
                        row.get("private_question"),
                        "legal research private question",
                        4_000,
                    )
                    + "\n允许出网的脱敏法律词："
                    + "、".join(terms)
                    + "\n复核清单："
                    + "；".join(checks)
                ),
                badge=(
                    "可在律师批准后检索"
                    if status_value == "READY_FOR_LAWYER_APPROVAL"
                    else "先由律师确认正式争点"
                ),
                confidence=None,
                sources=tuple(_source(ref) for ref in source_values),
            )
        )
    controls = _object(payload.get("controls"), "legal research controls")
    control_items = (
        WebCaseAgentArtifactReviewItem(
            item_id="legal-research-control-search",
            title="公网搜索只是线索发现",
            detail=(
                "只有上面列出的固定法律词可以出网；案件原文不会作为检索词。"
                "即使命中官方域名，搜索摘要也不能直接成为法律依据。"
            ),
            badge=(
                "需单独批准"
                if controls.get("external_search_separate_approval") is True
                else "控制状态异常"
            ),
            confidence=None,
            sources=(),
        ),
        WebCaseAgentArtifactReviewItem(
            item_id="legal-research-control-authority",
            title="官方原文、效力和本案适用分三步确认",
            detail=(
                "先逐条抓取并保存官方原文快照，再核验发布机关、版本和时间效力；"
                "最后仍由律师决定该规则是否适用于本案。"
            ),
            badge="不能自动形成法律结论",
            confidence=None,
            sources=(),
        ),
    )
    return (
        _text(payload.get("headline"), "legal research headline", 240),
        (
            WebCaseAgentArtifactReviewSection(
                section_id="legal-research-questions",
                title="待办法律研究问题",
                severity="HIGH",
                items=tuple(question_items),
            ),
            WebCaseAgentArtifactReviewSection(
                section_id="legal-research-controls",
                title="研究边界",
                severity="MEDIUM",
                items=control_items,
            ),
        ),
    )


def _case_context_sections(
    payload: dict[str, Any],
) -> tuple[str, tuple[WebCaseAgentArtifactReviewSection, ...]]:
    if payload.get("schema_version") != "agent-case-context-review-candidate-v1":
        raise WebCaseAgentArtifactReviewBlocked("整案研判成果版本不受支持。")
    title = _text(payload.get("headline"), "case context headline", 240)
    raw_sections = _list(payload.get("sections"), "case context sections", 1, 40)
    sections = []
    for index, section_value in enumerate(raw_sections, 1):
        section = _object(section_value, "case context section")
        items = []
        for item_index, item_value in enumerate(
            _list(section.get("items"), "case context items", 1, 200), 1
        ):
            item = _object(item_value, "case context item")
            sources = tuple(
                _source(value)
                for value in _string_list(item.get("source_refs"), "source refs", 100)
            )
            items.append(
                WebCaseAgentArtifactReviewItem(
                    item_id=_safe_id(item.get("item_id"), f"context-{index}-{item_index}"),
                    # Match the producer's bounded display text; verified
                    # long Chinese case titles must remain readable.
                    title=_text(item.get("title"), "context title", 500),
                    detail=_text(item.get("detail"), "context detail", 8_000, allow_empty=True),
                    badge=_optional_text(item.get("review_reason"), 240),
                    confidence=_confidence(item.get("confidence"), optional=True),
                    sources=sources,
                )
            )
        sections.append(
            WebCaseAgentArtifactReviewSection(
                section_id=_safe_id(section.get("section_id"), f"context-{index}"),
                title=_text(section.get("title"), "context section title", 240),
                severity=_severity(section.get("severity")),
                items=tuple(items),
            )
        )
    questions = []
    for question_index, question_value in enumerate(
        _list(payload.get("open_questions"), "case context questions", 0, 500),
        1,
    ):
        question = _object(question_value, "case context question")
        sources = tuple(
            _source(value)
            for value in _string_list(
                question.get("source_refs"), "question source refs", 100
            )
        )
        if not sources:
            raise WebCaseAgentArtifactReviewBlocked(
                "整案研判中的待确认问题没有可核对来源。"
            )
        questions.append(
            WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(
                    question.get("question_id"),
                    f"context-question-{question_index}",
                ),
                title="需要律师决定",
                detail=_text(
                    question.get("question"),
                    "case context question",
                    500,
                ),
                badge="阻断后续自动处理",
                confidence=None,
                sources=sources,
            )
        )
    if questions:
        sections.insert(
            0,
            WebCaseAgentArtifactReviewSection(
                section_id="context-open-questions",
                title="待律师决定",
                severity="HIGH",
                items=tuple(questions),
            ),
        )
    return title, tuple(sections)


def _discovered_analysis_sections(payload):
    source_index = {}
    for row in payload["source_catalog"]:
        source = _source(row["source_ref"])
        source_index[row["source_ref"]] = WebCaseAgentArtifactSource(
            source_kind=source.source_kind, source_id=source.source_id,
            label=row["title"][:240], evidence_page_id=source.evidence_page_id)
    issues, actions, gaps = [], [], []
    for row, discovered in zip(payload["analysis"]["issues"], payload["discovery"]["issues"], strict=True):
        refs = tuple(source_index[ref] for ref in row["source_refs"])
        issue_id = discovered["issue_id"]
        detail = row["question"] + "\n我方立场（待确认）：" + row["our_position"]
        detail += "\n对方立场或可能反驳：" + row["opponent_position"]
        if row["strengths"]: detail += "\n有利证据：" + "；".join(row["strengths"])
        if row["weaknesses"]: detail += "\n不利证据：" + "；".join(row["weaknesses"])
        detail += "\n反制路径：" + row["rebuttal_route"] + "\n仍存风险：" + row["residual_risk"]
        issues.append(WebCaseAgentArtifactReviewItem(item_id=issue_id, title=row["title"], detail=detail,
            badge="待律师取舍" if row["needs_lawyer_decision"] else "初步研判", confidence=None, sources=refs))
        actions.append(WebCaseAgentArtifactReviewItem(item_id=issue_id + ":action", title=row["next_action"],
            detail="对应问题：" + row["title"], badge="建议下一步", confidence=None, sources=refs))
        if row["missing_evidence"]:
            gaps.append(WebCaseAgentArtifactReviewItem(item_id=issue_id + ":gap", title=row["title"],
                detail="；".join(row["missing_evidence"]), badge="待核实缺口", confidence=None, sources=refs))
    sections = [WebCaseAgentArtifactReviewSection(section_id="discovered-issues", title="需要处理的问题",
        severity="HIGH", items=tuple(issues)),
        WebCaseAgentArtifactReviewSection(section_id="discovered-actions", title="下一步工作",
        severity="MEDIUM", items=tuple(actions))]
    if gaps:
        sections.append(WebCaseAgentArtifactReviewSection(section_id="discovered-gaps", title="待补证据",
            severity="MEDIUM", items=tuple(gaps)))
    unresolved = [row for row in payload["discovery"]["source_dispositions"]
                  if row["disposition"] == "UNRESOLVED_RELEVANCE"]
    if unresolved:
        sections.append(WebCaseAgentArtifactReviewSection(section_id="discovered-unresolved", title="关联性仍待核对",
            severity="MEDIUM", items=(WebCaseAgentArtifactReviewItem(item_id="unresolved-sources",
                title=f"{len(unresolved)}项来源尚未确定关联性", detail="这些材料已保留，尚不能排除或直接作为结论依据。",
                badge="未自动排除", confidence=None,
                sources=tuple(source_index[row["source_ref"]] for row in unresolved)),)))
    return "争点与办案建议", tuple(sections)


def _lawyer_decision_package_sections(
    payload: dict[str, Any],
) -> tuple[str, tuple[WebCaseAgentArtifactReviewSection, ...]]:
    """Project one strictly parsed 0040 package into a decision-first view.

    Provider payloads, request hashes and model-authored control fields stay
    private.  The browser receives only reviewable analysis, server-bound
    source links and a compact usage receipt proving that a real model call
    occurred.  None of these sections records a lawyer approval.
    """

    if payload.get("schema_version") == "agent-discovered-lawyer-analysis-candidate-v1":
        return _discovered_analysis_sections(payload)
    catalog = _list(payload.get("source_catalog"), "lawyer source catalog", 1, 500)
    source_index: dict[str, WebCaseAgentArtifactSource] = {}
    for index, value in enumerate(catalog, 1):
        row = _object(value, f"lawyer source {index}")
        source_ref = _text(row.get("source_ref"), "lawyer source ref", 200)
        base = _source(source_ref)
        source_index[source_ref] = WebCaseAgentArtifactSource(
            source_kind=base.source_kind,
            source_id=base.source_id,
            label=_text(row.get("title"), "lawyer source title", 4_000)[:240],
            evidence_page_id=base.evidence_page_id,
        )

    def sources(raw: object, *, label: str) -> tuple[WebCaseAgentArtifactSource, ...]:
        refs = _string_list(raw, label, 100)
        result: list[WebCaseAgentArtifactSource] = []
        for ref in refs:
            source = source_index.get(ref)
            if source is None:
                raise WebCaseAgentArtifactReviewBlocked(
                    "律师决策包含有无法回链的来源。"
                )
            if source.source_id not in {item.source_id for item in result}:
                result.append(source)
        return tuple(result)

    issue_values = _list(payload.get("issues"), "lawyer issues", 1, 20)
    missing_evidence = tuple(
        dict.fromkeys(
            gap
            for value in issue_values
            for gap in _string_list(
                _object(value, "lawyer issue readiness").get("missing_evidence"),
                "missing evidence",
                3,
            )
        )
    )
    has_formal_issues = any(
        source.source_kind == "issue" for source in source_index.values()
    )
    has_verified_authorities = any(
        source.source_kind == "legal-source" for source in source_index.values()
    )

    readiness_items = [
        WebCaseAgentArtifactReviewItem(
            item_id="lawyer-readiness-stage",
            title=("已进入争点研判" if has_formal_issues else "当前仅为初步风险研判"),
            detail=(
                "服务器已登记正式争点，可按争点继续核对证据和攻防。"
                if has_formal_issues
                else "本案尚未登记正式争点；以下内容只用于帮助律师发现风险，不得直接写入诉讼方案。"
            ),
            badge=("可继续研判" if has_formal_issues else "需律师整理争点"),
            confidence=None,
            sources=tuple(
                source
                for source in source_index.values()
                if source.source_kind in {"issue", "fact"}
            ),
        ),
        WebCaseAgentArtifactReviewItem(
            item_id="lawyer-readiness-authority",
            title=("已有可回链法律依据" if has_verified_authorities else "法律依据尚未核验"),
            detail=(
                "涉及法律规则的判断仍须逐条核对来源、效力和适用条件。"
                if has_verified_authorities
                else "当前没有已核验法源；文中的法律路径只能视为待研究假设，不能形成法律结论。"
            ),
            badge=("仍需律师复核" if has_verified_authorities else "阻断法律结论"),
            confidence=None,
            sources=tuple(
                source
                for source in source_index.values()
                if source.source_kind == "legal-source"
            ),
        ),
    ]
    if missing_evidence:
        readiness_items.append(
            WebCaseAgentArtifactReviewItem(
                item_id="lawyer-readiness-evidence",
                title=f"仍有 {len(missing_evidence)} 类关键材料待补",
                detail="；".join(missing_evidence[:8]),
                badge="补齐后再定行动路径",
                confidence=None,
                sources=tuple(
                    source
                    for source in source_index.values()
                    if source.source_kind in {"fact", "transaction", "issue"}
                ),
            )
        )

    executive = _object(payload.get("executive_assessment"), "executive assessment")
    executive_items = (
        WebCaseAgentArtifactReviewItem(
            item_id="lawyer-executive-posture",
            title="案件态势",
            detail=_lawyer_posture_label(
                _text(executive.get("case_posture"), "case posture", 240),
                tuple(source_index.values()),
            ),
            badge="服务器案情状态·待律师复核",
            confidence=None,
            sources=tuple(
                source
                for source in source_index.values()
                if source.source_kind == "posture-profile"
            ),
        ),
        WebCaseAgentArtifactReviewItem(
            item_id="lawyer-executive-direction",
            title="建议工作方向",
            detail=_controlled_working_direction(
                raw=_text(
                    executive.get("working_direction"), "working direction", 240
                ),
                has_verified_authorities=has_verified_authorities,
                missing_evidence=missing_evidence,
            ),
            badge="不构成法律结论",
            confidence=None,
            sources=tuple(
                source_index[ref]
                for ref in _string_list(
                    executive.get("top_risk_issue_refs"), "top risks", 5
                )
                if ref in source_index
            ),
        ),
    )
    sections: list[WebCaseAgentArtifactReviewSection] = [
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-readiness",
            title="当前可用范围",
            severity=(
                "HIGH"
                if not has_formal_issues or not has_verified_authorities or missing_evidence
                else "MEDIUM"
            ),
            items=tuple(readiness_items),
        ),
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-executive",
            title="律师先看",
            severity="HIGH",
            items=executive_items,
        )
    ]

    issue_items: list[WebCaseAgentArtifactReviewItem] = []
    for index, value in enumerate(issue_values, 1):
        row = _object(value, f"lawyer issue {index}")
        missing = _string_list(row.get("missing_evidence"), "missing evidence", 3)
        detail = _text(row.get("assessment"), "issue assessment", 4_000)
        if missing:
            detail += "\n待补材料：" + "；".join(missing)
        refs = tuple(
            dict.fromkeys(
                (
                    *_string_list(
                        row.get("supporting_source_refs"), "supporting sources", 12
                    ),
                    *_string_list(
                        row.get("adverse_source_refs"), "adverse sources", 12
                    ),
                    *_string_list(
                        row.get("authority_refs"), "authority sources", 4
                    ),
                )
            )
        )
        issue_items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(row.get("issue_ref"), f"lawyer-issue-{index}"),
                title=_lawyer_topic_title(
                    _text(row.get("title"), "issue title", 4_000)
                ),
                detail=detail,
                badge=(
                    f"{_LAWYER_PRIORITY_LABELS.get(_text(row.get('priority'), 'issue priority', 20), '需律师核对')} · "
                    f"{_EVIDENCE_STATUS_LABELS.get(_text(row.get('evidence_status'), 'evidence status', 40), '证据状态待核对')}"
                    + (
                        " · 法源待核验"
                        if not _string_list(
                            row.get("authority_refs"), "authority sources", 4
                        )
                        else ""
                    )
                ),
                confidence=None,
                sources=sources(list(refs), label="issue sources"),
            )
        )
    sections.append(
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-issues",
            title="争点与证据风险",
            severity="HIGH",
            items=tuple(issue_items),
        )
    )

    adversarial_items = []
    for index, value in enumerate(
        _list(payload.get("adversarial_analysis"), "adversarial analysis", 1, 20),
        1,
    ):
        row = _object(value, f"adversarial item {index}")
        detail = (
            "为什么可能成立："
            + _text(row.get("why_it_may_work"), "opponent rationale", 1_000)
            + "\n反制路径："
            + _text(row.get("rebuttal_route"), "rebuttal route", 1_000)
            + "\n剩余风险："
            + _text(row.get("residual_risk"), "residual risk", 1_000)
        )
        refs = tuple(
            dict.fromkeys(
                (
                    *_string_list(row.get("source_refs"), "opponent sources", 12),
                    *_string_list(
                        row.get("authority_refs"), "opponent authority sources", 4
                    ),
                )
            )
        )
        adversarial_items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=f"lawyer-opponent-{index}",
                title=_opponent_position_title(
                    _text(
                        row.get("opponent_position"), "opponent position", 4_000
                    )
                )[:240],
                detail=detail,
                badge=(
                    (
                        "对方已提出"
                        if row.get("position_status") == "ASSERTED_SOURCE_POSITION"
                        else "可预见但尚未提出"
                    )
                    + (
                        " · 法律路径待核验"
                        if not _string_list(
                            row.get("authority_refs"),
                            "opponent authority sources",
                            4,
                        )
                        else ""
                    )
                ),
                confidence=None,
                sources=sources(list(refs), label="opponent sources"),
            )
        )
    sections.append(
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-adversarial",
            title="对方可能主张与反制",
            severity="HIGH",
            items=tuple(adversarial_items),
        )
    )

    strategy_items = []
    for index, value in enumerate(
        _list(payload.get("strategy_options"), "strategy options", 2, 2), 1
    ):
        row = _object(value, f"strategy {index}")
        detail = (
            _text(row.get("objective"), "strategy objective", 1_000)
            + "\n适用条件："
            + "；".join(_string_list(row.get("conditions"), "strategy conditions", 4))
            + "\n执行风险："
            + "；".join(
                _string_list(row.get("execution_risks"), "execution risks", 4)
            )
            + "\n取舍："
            + _text(row.get("tradeoff_note"), "strategy tradeoff", 1_000)
        )
        strategy_items.append(
            WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(
                    row.get("strategy_id"), f"lawyer-strategy-{index}"
                ),
                title=_text(row.get("title"), "strategy title", 240),
                detail=detail,
                badge=(
                    "由律师选择，不自动执行"
                    + (" · 法律路径待核验" if not has_verified_authorities else "")
                ),
                confidence=None,
                sources=sources(row.get("issue_refs"), label="strategy issues"),
            )
        )
    sections.append(
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-strategies",
            title="策略路径与取舍",
            severity="MEDIUM",
            items=tuple(strategy_items),
        )
    )

    question_values = _list(
        payload.get("client_questions"), "client questions", 0, 30
    )
    # Legacy candidates classified every missing item as a client request.
    # Route legal research to the firm without rewriting the stored candidate.
    research_questions = [value for value in question_values
        if _is_legal_research_question(_object(value, "client question").get("question"))]
    question_values = [value for value in question_values if value not in research_questions]
    if research_questions:
        sections.append(WebCaseAgentArtifactReviewSection(
            section_id="lawyer-research-questions", title="由律师与 Agent 核验", severity="HIGH",
            items=tuple(WebCaseAgentArtifactReviewItem(
                item_id=_safe_id(row.get("question_id"), f"lawyer-research-{index}"),
                title=_client_question_title(_text(row.get("question"), "research question", 4_000)).replace("补充材料：", "核验：", 1)[:240],
                detail=_text(row.get("why_it_matters"), "research reason", 4_000),
                badge="内部研究，不向当事人索取", confidence=None,
                sources=sources(row.get("source_refs"), label="research sources"))
                for index, value in enumerate(research_questions, 1)
                for row in (_object(value, "research question"),))))
    if question_values:
        sections.append(
            WebCaseAgentArtifactReviewSection(
                section_id="lawyer-client-questions",
                title="需要当事人补充",
                severity="HIGH",
                items=tuple(
                    WebCaseAgentArtifactReviewItem(
                        item_id=_safe_id(
                            row.get("question_id"), f"lawyer-question-{index}"
                        ),
                        title=_client_question_title(
                            _text(row.get("question"), "client question", 4_000)
                        )[:240],
                        detail=_text(
                            row.get("why_it_matters"), "question reason", 4_000
                        ),
                        badge="补充后再判断",
                        confidence=None,
                        sources=sources(
                            row.get("source_refs"), label="question sources"
                        ),
                    )
                    for index, value in enumerate(question_values, 1)
                    for row in (_object(value, f"client question {index}"),)
                ),
            )
        )

    action_values = _list(payload.get("action_plan"), "action plan", 1, 20)
    sections.append(
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-actions",
            title="律师行动清单",
            severity="HIGH",
            items=tuple(
                WebCaseAgentArtifactReviewItem(
                    item_id=_safe_id(
                        row.get("action_id"), f"lawyer-action-{index}"
                    ),
                    title=_action_title(row)[:240],
                    detail=_text(row.get("reason"), "action reason", 4_000)
                    + (
                        "\n受阻于："
                        + "；".join(
                            _string_list(row.get("blocked_by"), "action blockers", 3)
                        )
                        if row.get("blocked_by")
                        else ""
                    ),
                    badge=(
                        "现在处理"
                        if row.get("priority") == "NOW"
                        else "下一步处理"
                    ),
                    confidence=None,
                    sources=sources(row.get("source_refs"), label="action sources"),
                )
                for index, value in enumerate(action_values, 1)
                for row in (_object(value, f"action {index}"),)
            ),
        )
    )

    decision_values = _list(
        payload.get("decision_requests"), "decision requests", 1, 10
    )
    lean_labels = {
        None: "Agent 未给倾向",
        "SELECT_A": "Agent 倾向方案 A",
        "SELECT_B": "Agent 倾向方案 B",
        "DEFER": "Agent 倾向暂缓",
        "FOLLOW_UP_EVIDENCE": "Agent 倾向补证后再决定",
        "PRESERVE_ALTERNATIVE": "Agent 倾向保留主备位",
        "DO_NOT_TAKE_POSITION_YET": "Agent 倾向暂不形成正式立场",
    }
    sections.append(
        WebCaseAgentArtifactReviewSection(
            section_id="lawyer-decisions",
            title="必须由律师决定",
            severity="HIGH",
            items=tuple(
                WebCaseAgentArtifactReviewItem(
                    item_id=_safe_id(
                        row.get("decision_id"), f"lawyer-decision-{index}"
                    ),
                    title=_decision_title(
                        _text(row.get("question"), "lawyer decision", 4_000),
                        has_formal_issues=has_formal_issues,
                    )[:240],
                    detail=_text(row.get("reason"), "decision reason", 4_000),
                    badge=(
                        lean_labels.get(row.get("agent_lean"), "Agent 倾向待复核")
                        + (
                            " · 法律依据待核验"
                            if not _string_list(
                                row.get("authority_refs", []),
                                "decision authority sources",
                                4,
                            )
                            else ""
                        )
                    ),
                    confidence=None,
                    sources=sources(row.get("source_refs"), label="decision sources"),
                )
                for index, value in enumerate(decision_values, 1)
                for row in (_object(value, f"decision {index}"),)
            ),
        )
    )

    return "律师决策包候选", tuple(sections)


def _lawyer_posture_label(
    raw: str,
    sources: tuple[WebCaseAgentArtifactSource, ...],
) -> str:
    for source in sources:
        if source.source_kind != "posture-profile":
            continue
        matched = re.search(
            r"代理身份：([A-Z_]+)｜程序阶段：([A-Z_]+)", source.label
        )
        if matched is not None:
            party, stage = matched.groups()
            return (
                f"{_PARTY_KIND_LABELS.get(party, party)} · "
                f"{_PROCEDURE_STAGE_LABELS.get(stage, stage)}"
            )
    return _PARTY_KIND_LABELS.get(raw, raw)


def _controlled_working_direction(
    *,
    raw: str,
    has_verified_authorities: bool,
    missing_evidence: tuple[str, ...],
) -> str:
    if missing_evidence and not has_verified_authorities:
        return "先补齐关键证据并完成法源核验，再由律师确定是否形成外部行动方案。"
    if not has_verified_authorities:
        return "先完成争点整理与法源核验，再由律师确定下一步行动路径。"
    if missing_evidence:
        return "先补齐关键证据，再由律师结合已核验法源确定行动路径。"
    return raw


def _clean_lawyer_subject(value: str) -> str:
    result = value.replace("围绕已确认事项的风险分析：", "")
    result = result.replace("已确认事项风险：", "")
    result = result.replace("围绕“围绕", "围绕“")
    return result


def _opponent_position_title(value: str) -> str:
    result = _clean_lawyer_subject(value)
    match = re.fullmatch(
        r"围绕“(.+)”可能提出与本方相反的事实或法律解释", result
    )
    if match is not None:
        return f"对方可能否认或重新解释：{_lawyer_topic_title(match.group(1))}"
    return _lawyer_topic_title(result)


def _decision_title(value: str, *, has_formal_issues: bool) -> str:
    result = _clean_lawyer_subject(value)
    match = re.fullmatch(r"律师如何处理“(.+)”", result)
    if match is None:
        return _lawyer_topic_title(result)
    subject = _lawyer_topic_title(match.group(1))
    if not has_formal_issues:
        return f"请律师决定：是否把“{subject}”纳入正式争点整理"
    return f"请律师决定：围绕“{subject}”先补证、保留主备位或暂不表态"


def _client_question_title(value: str) -> str:
    prefix = "请当事人补充核实并提供原始材料："
    return "补充材料：" + value.removeprefix(prefix)


def _is_legal_research_question(value: object) -> bool:
    text = _text(value, "client question", 4_000)
    return any(term in text for term in ("法源", "法律依据", "司法解释", "适用法条", "法律检索"))


def _action_title(row: dict[str, Any]) -> str:
    blockers = _string_list(row.get("blocked_by"), "action blockers", 3)
    if blockers:
        return "补齐并核验：" + "；".join(blockers)
    action = _clean_lawyer_subject(_text(row.get("action"), "lawyer action", 4_000))
    match = re.fullmatch(r"围绕“(.+)”完成证据对应、相反材料核对和律师取舍记录", action)
    if match is not None:
        return f"围绕“{_lawyer_topic_title(match.group(1))}”完成证据核对与取舍记录"
    return _lawyer_topic_title(action)


def _lawyer_topic_title(value: str) -> str:
    """Turn an extracted source sentence into a scannable lawyer work topic.

    The source sentence remains available in the detail and the source link.
    A work-list title must instead tell a lawyer what to decide or verify; it
    must not reproduce a full page of an uploaded pleading.
    """

    result = _clean_lawyer_subject(value)
    if "证据目录" in result:
        return "证据目录所列材料的真实性、关联性与证明力"
    if "案件受理" in result or "开庭" in result or "送达" in result:
        return "应诉程序与期限核对"
    if "被告陈述" in result or "当事人陈述" in result:
        return "当事人抗辩及支撑证据"
    if "起诉状" in result:
        if "借款" in result or "利息" in result:
            return "借款交付、利率与期限"
        return "对方起诉事实与证据核验"
    if "判决" in result or "裁定" in result:
        return "既有裁判文书及其影响核验"
    return result[:120]


def _source(value: object) -> WebCaseAgentArtifactSource:
    text = _text(value, "source reference", 200)
    match = _SOURCE_REF.fullmatch(text)
    if match is None:
        raise WebCaseAgentArtifactReviewBlocked("Agent 成果含有无法回链的来源。")
    kind, source_id = match.groups()
    return WebCaseAgentArtifactSource(
        source_kind=kind,
        source_id=source_id,
        label=_SOURCE_LABELS[kind],
        evidence_page_id=source_id if kind == "evidence-page" else None,
    )


def _human_uuid(value: object) -> str:
    return _uuid_text(value, "identifier")


def _uuid_text(value: object, label: str) -> str:
    # psycopg decodes PostgreSQL UUID columns as ``UUID`` instances. Keep the
    # browser boundary strict while accepting that database-native form.
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    _uuid(value, label)
    return value


def _uuid(value: object, label: str) -> None:
    try:
        normalized = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid") from error
    if normalized != value:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is not canonical")


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    return value


def _text(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is empty")
    return normalized


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, "optional text", maximum, allow_empty=True) or None


def _private_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2_048 or "\x00" in value:
        raise WebCaseAgentArtifactReviewBlocked(f"private {label} is invalid")
    return value


def _optional_private_text(value: object) -> str | None:
    if value is None:
        return None
    return _private_text(value, "version")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    return value


def _list(value: object, label: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    return value


def _string_list(value: object, label: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise WebCaseAgentArtifactReviewBlocked(f"{label} is invalid")
    return tuple(_text(item, label, 500) for item in value)


def _confidence(value: object, *, optional: bool = False) -> float | None:
    if optional and value is None:
        return None
    if type(value) not in {float, int}:
        raise WebCaseAgentArtifactReviewBlocked("confidence is invalid")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise WebCaseAgentArtifactReviewBlocked("confidence is invalid")
    return result


def _safe_id(value: object, fallback: str) -> str:
    if isinstance(value, str) and value and len(value) <= 200 and re.fullmatch(
        r"[A-Za-z0-9._:-]+", value
    ):
        return value
    return fallback


def _safe_public_https_url(value: object) -> str:
    text = _text(value, "public URL", 2_048)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise WebCaseAgentArtifactReviewBlocked("public research URL is invalid")
    return text


def _severity(value: object) -> str:
    text = _text(value, "severity", 20)
    if text not in {"LOW", "MEDIUM", "HIGH"}:
        raise WebCaseAgentArtifactReviewBlocked("severity is invalid")
    return text


def _format_label(value: object) -> str:
    labels = {"DOCX": "Word", "XLSX": "Excel"}
    return labels.get(_text(value, "document format", 20), "Office")


def _office_kind_label(value: str) -> str:
    labels = {
        "PARAGRAPH": "正文候选",
        "HEADING": "标题候选",
        "TABLE_CELL": "表格内容候选",
        "SHEET_CELL": "表格单元格候选",
        "NOTE": "备注候选",
    }
    return labels.get(value, "材料内容候选")


def _authority_label(value: object) -> str:
    labels = {
        "PUBLIC_OFFICIAL": "官方来源线索",
        "PUBLIC_SECONDARY": "公开参考线索",
        "UNVERIFIED": "尚未核验来源",
    }
    return labels.get(_text(value, "authority class", 80), "尚未核验来源")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


__all__ = (
    "PostgresWebCaseAgentArtifactReviewService",
    "WebCaseAgentArtifactReview",
    "WebCaseAgentArtifactReviewBlocked",
    "WebCaseAgentArtifactReviewItem",
    "WebCaseAgentArtifactReviewPort",
    "WebCaseAgentArtifactReviewSection",
    "WebCaseAgentArtifactSource",
)
