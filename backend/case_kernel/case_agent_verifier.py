"""Deterministic, independently-identified verification for case-Agent runs.

The verifier does not interpret model prose and does not trust an adapter's
``output_hash`` as proof that an artifact exists.  It replays structural
bindings from the current aggregate, re-reads every registered artifact from a
server-owned access port, invokes an exact format verifier for its kind and
emits one canonical receipt.  Unexpected storage/runtime failures are
indeterminate and must remain recoverable; they are never rewritten as a
successful or known-failed verification.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from hashlib import sha256
from io import BytesIO
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from pypdf.generic import ArrayObject, NullObject

from .case_agent_ledger_extraction import (
    CASE_LEDGER_EXTRACTION_ARTIFACT_KIND,
    CASE_LEDGER_EXTRACTION_SCHEMA,
    CaseLedgerExtractionBlocked,
    extraction_source_refs,
    parse_case_ledger_extraction_candidate,
)
from .case_agent_lawyer_analysis import (
    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
    LAWYER_DECISION_PACKAGE_SCHEMA,
    LawyerAnalysisBlocked,
    lawyer_decision_package_source_refs,
    parse_lawyer_decision_package_candidate,
)
from .case_agent_legal_research_plan import (
    LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
    LEGAL_RESEARCH_PLAN_SCHEMA,
    LegalResearchPlanBlocked,
    legal_research_plan_source_refs,
    parse_legal_research_plan_candidate,
)
from .case_agent_supervisor import (
    AgentRunState,
    AgentRunStatus,
    AgentTaskStatus,
    ArtifactReceipt,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")

# Deliberately repeated here instead of trusting the document adapter's
# schema.  The verifier is an independent release boundary.
_PAYMENT_LEDGER_COLUMNS = (
    ("local_date", "日期", "DATE"),
    ("date_precision", "日期精度", "TEXT"),
    ("amount", "金额（原值）", "TEXT"),
    ("currency", "币种", "TEXT"),
    ("direction", "方向", "TEXT"),
    ("payer_label", "付款方", "TEXT"),
    ("payee_label", "收款方", "TEXT"),
    ("channel", "渠道", "TEXT"),
    ("transaction_reference", "交易参考号", "TEXT"),
    ("nature", "交易性质", "TEXT"),
    ("same_day_sequence", "同日顺序", "INTEGER"),
)

FIRST_RELEASE_VERIFIER_ID = "case-agent-independent-verifier"
FIRST_RELEASE_VERIFIER_VERSION = "1.1.0"
_DISCOVERED_LAWYER_DECISION_PACKAGE_SCHEMA = (
    "agent-discovered-lawyer-analysis-candidate-v1",
)
FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS = (
    (
        "COMMON_DOCUMENT_REVIEW_CANDIDATE",
        "agent-common-document-candidate-v1",
    ),
    ("PDF_TEXT_REVIEW_CANDIDATE", "agent-pdf-text-candidate-v1"),
    (
        "CASE_CONTEXT_REVIEW_CANDIDATE",
        "agent-case-context-review-candidate-v1",
    ),
    (
        CASE_LEDGER_EXTRACTION_ARTIFACT_KIND,
        CASE_LEDGER_EXTRACTION_SCHEMA,
    ),
    (
        LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
        (
            LAWYER_DECISION_PACKAGE_SCHEMA,
            *_DISCOVERED_LAWYER_DECISION_PACKAGE_SCHEMA,
        ),
    ),
    (
        LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
        LEGAL_RESEARCH_PLAN_SCHEMA,
    ),
    (
        "PUBLIC_RESEARCH_LEADS_CANDIDATE",
        "agent-public-research-leads-candidate-v1",
    ),
    (
        "VISUAL_PAGE_REVIEW_CANDIDATE",
        "agent-visual-page-candidate-bundle-v1",
    ),
)
_STRUCTURAL_CHECKS = (
    "CURRENT_GRAPH_HASH",
    "TASK_FINAL_RECEIPT_BINDING",
    "EXTERNAL_RECONCILIATION",
    "RUN_BUDGET",
    "MANAGED_ARTIFACT_BYTES_AND_LINEAGE",
    "CASE_CONTEXT_EXACT_TASK_SOURCE_REFS",
    "LEDGER_EXTRACTION_EXACT_TASK_AND_VISUAL_DEPENDENCY_SOURCE_REFS",
    "LAWYER_DECISION_PACKAGE_EXACT_TASK_SOURCE_REFS",
    "LEGAL_RESEARCH_PLAN_EXACT_TASK_SOURCE_REFS",
)

_PUBLIC_RESEARCH_ARTIFACT_KIND = "PUBLIC_RESEARCH_LEADS_CANDIDATE"
_PUBLIC_RESEARCH_SCHEMA = "agent-public-research-leads-candidate-v1"
_PUBLIC_RESEARCH_PROVIDER_ID = "brave_web_search"
_PUBLIC_RESEARCH_PURPOSES = (
    "CASE_RESEARCH",
    "GENERAL_PUBLIC_RESEARCH",
    "LEGAL_AUTHORITY_DISCOVERY",
)
_PUBLIC_RESEARCH_AUTHORITY_CLASSES = (
    "CENTRAL_GOVERNMENT",
    "JUDICIAL_INTERPRETATION",
    "NON_OFFICIAL",
    "OFFICIAL_CASE_DATABASE",
    "OFFICIAL_COURT_PUBLICATION",
    "OFFICIAL_RATE_DATA",
    "PRIMARY_LEGISLATION",
)
_PUBLIC_RESEARCH_PROMPT_SIGNALS = (
    "IGNORE_PRIOR_INSTRUCTIONS",
    "SYSTEM_OR_DEVELOPER_ROLE_TEXT",
    "TOOL_OR_COMMAND_REQUEST",
    "CREDENTIAL_OR_SECRET_REQUEST",
    "MODEL_CONTROL_TOKEN",
    "BINARY_CONTENT_NOT_TEXT_INSPECTED",
)
_PUBLIC_RESEARCH_OFFICIAL_AUTHORITIES = {
    "CENTRAL_GOVERNMENT_PORTAL": (
        "CENTRAL_GOVERNMENT",
        "中国政府网",
        ("gov.cn", "www.gov.cn"),
    ),
    "CHINA_JUDGMENTS_ONLINE": (
        "OFFICIAL_COURT_PUBLICATION",
        "中国裁判文书网",
        ("wenshu.court.gov.cn",),
    ),
    "NATIONAL_LAWS_DATABASE": (
        "PRIMARY_LEGISLATION",
        "国家法律法规数据库",
        ("flk.npc.gov.cn", "wb.flk.npc.gov.cn"),
    ),
    "OFFICIAL_LPR_DATA": (
        "OFFICIAL_RATE_DATA",
        "全国银行间同业拆借中心",
        (
            "chinamoney.com.cn",
            "shibor.org",
            "www.chinamoney.com.cn",
            "www.shibor.org",
        ),
    ),
    "PEOPLES_COURT_CASE_DATABASE": (
        "OFFICIAL_CASE_DATABASE",
        "人民法院案例库",
        ("rmfyalk.court.gov.cn",),
    ),
    "SUPREME_PEOPLES_COURT": (
        "JUDICIAL_INTERPRETATION",
        "最高人民法院",
        (
            "cicc.court.gov.cn",
            "court.gov.cn",
            "gongbao.court.gov.cn",
            "www.court.gov.cn",
        ),
    ),
}
_PUBLIC_RESEARCH_OFFICIAL_HOSTS = frozenset(
    host
    for _, _, hosts in _PUBLIC_RESEARCH_OFFICIAL_AUTHORITIES.values()
    for host in hosts
)
_PUBLIC_RESEARCH_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "task_input_hash",
        "source_hash",
        "review_status",
        "formal_fact",
        "formal_transaction",
        "legal_conclusion",
        "evidence_decision",
        "legal_effect_confirmed",
        "query",
        "provider_id",
        "external_request_id",
        "leads",
    }
)
_PUBLIC_RESEARCH_QUERY_FIELDS = frozenset(
    {"question_id", "purpose", "query_hash", "public_terms"}
)
_PUBLIC_RESEARCH_LEAD_FIELDS = frozenset(
    {
        "lead_id",
        "title",
        "url",
        "snippet",
        "published_on_candidate",
        "authority_class",
        "official_source_id",
        "publisher",
        "official_domain",
        "prompt_injection_signals",
        "status",
    }
)

_CASE_CONTEXT_ARTIFACT_KIND = "CASE_CONTEXT_REVIEW_CANDIDATE"
_CASE_CONTEXT_SCHEMA = "agent-case-context-review-candidate-v1"
_CASE_CONTEXT_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "task_input_hash",
        "source_hash",
        "review_status",
        "formal_fact",
        "formal_transaction",
        "legal_conclusion",
        "evidence_decision",
        "court_ready",
        "headline",
        "sections",
        "open_questions",
        "summary_counts",
    }
)
_CASE_CONTEXT_SECTION_IDS = (
    "DISPUTES",
    "RISKS",
    "GAPS",
    "PROCEDURE",
    "LEGAL_SOURCES",
    "TRANSACTIONS",
    "CASE_CONTEXT",
)
_CASE_CONTEXT_SECTION_FIELDS = frozenset(
    {"section_id", "title", "severity", "items"}
)
_CASE_CONTEXT_ITEM_REQUIRED_FIELDS = frozenset(
    {"item_id", "title", "detail", "source_refs", "review_reason"}
)
_CASE_CONTEXT_QUESTION_FIELDS = frozenset(
    {"question_id", "question", "source_refs"}
)
_CASE_CONTEXT_SUMMARY_FIELDS = frozenset(
    {
        "total_sources",
        "total_items",
        "high_priority_items",
        "medium_priority_items",
        "low_priority_items",
        "open_questions",
    }
)

_VISUAL_ARTIFACT_KIND = "VISUAL_PAGE_REVIEW_CANDIDATE"
_VISUAL_SCHEMA = "agent-visual-page-candidate-bundle-v1"
_VISUAL_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version", "task_input_hash", "source_hash", "binding_hash",
        "review_status", "formal_fact", "formal_transaction",
        "legal_conclusion", "evidence_decision", "authenticity_confirmed",
        "provenance", "external_request_id", "provider", "pages",
    }
)
_VISUAL_PROVENANCE_FIELDS = frozenset(
    {
        "run_id", "task_id", "attempt_id", "firm_id", "matter_id",
        "matter_version", "input_refs", "external_request_id",
        "processor_region", "workspace_id_hash",
    }
)
_VISUAL_PROVIDER_FIELDS = frozenset(
    {
        "provider_id", "model_id", "provider_version", "processor_region",
        "service_id", "network_capable",
    }
)
_VISUAL_PAGE_FIELDS = frozenset(
    {
        "input_ref", "matter_id", "evidence_page_id", "page_number",
        "source_kind", "source_file_sha256", "source_page_sha256",
        "rendered_page_sha256", "projection_hash", "parser_id",
        "parser_version", "orientation_applied", "source_format",
        "had_transparency", "request_hash", "width", "height", "media_type",
        "provider_id", "model_id",
        "provider_request_ref_hash", "candidate_hash", "review_status",
        "text_blocks", "tables", "fields", "quality_risks",
    }
)
_VISUAL_REGION_FIELDS = frozenset({"x", "y", "width", "height"})
_VISUAL_BLOCK_FIELDS = frozenset(
    {"block_id", "kind", "text", "region", "confidence"}
)
_VISUAL_TABLE_FIELDS = frozenset(
    {"table_id", "region", "row_count", "column_count", "cells", "confidence"}
)
_VISUAL_FIELD_FIELDS = frozenset(
    {"field_id", "kind", "value", "region", "confidence", "currency"}
)
_VISUAL_RISK_FIELDS = frozenset(
    {"code", "severity", "region", "confidence", "note"}
)
_VISUAL_SOURCE_KINDS = (
    "NATIVE_IMAGE", "RENDERED_PDF_PAGE", "RENDERED_OFD_PAGE",
    "RENDERED_OFFICE_PAGE",
)
_VISUAL_BLOCK_KINDS = (
    "TEXT", "HEADING", "HANDWRITING", "STAMP_TEXT", "SIGNATURE_TEXT",
    "PAGE_NUMBER", "UNKNOWN",
)
_VISUAL_FIELD_KINDS = (
    "PERSON_NAME", "ORGANIZATION", "DATE", "TIME", "AMOUNT", "CURRENCY",
    "ACCOUNT_IDENTIFIER", "TRANSACTION_IDENTIFIER", "CASE_IDENTIFIER",
    "COURT_NAME", "PHONE_NUMBER", "OTHER",
)
_VISUAL_QUALITY_CODES = (
    "LOW_RESOLUTION", "BLUR", "GLARE", "SHADOW", "SKEW",
    "ROTATION_UNCERTAIN", "OCCLUSION", "CROPPED_CONTENT",
    "COMPRESSION_ARTIFACTS", "LOW_CONTRAST", "SCREENSHOT_STITCH_RISK",
    "POSSIBLE_EDITING_RISK", "COLOR_PROFILE_NORMALIZED",
    "TRANSPARENCY_FLATTENED",
)


class CaseAgentVerificationIndeterminate(RuntimeError):
    """Verification could not reach a trustworthy terminal observation."""


class ArtifactVerificationRejected(ValueError):
    """A registered format verifier reached a known, safe failure."""

    def __init__(self, error_code: str) -> None:
        _safe_code(error_code, "artifact verification error_code")
        super().__init__(error_code)
        self.error_code = error_code


class VerificationOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ManagedArtifactRead:
    """Bytes and lineage returned only by a server-authorized object reader."""

    artifact_id: str
    artifact_kind: str
    content: bytes = field(repr=False, compare=False)
    source_input_hash: str
    object_receipt_hash: str
    media_type: str

    def validate(self) -> None:
        _uuid(self.artifact_id, "managed artifact_id")
        _code(self.artifact_kind, "managed artifact_kind")
        if not isinstance(self.content, bytes):
            raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID")
        _sha256(self.source_input_hash, "managed artifact source_input_hash")
        _sha256(self.object_receipt_hash, "managed artifact object_receipt_hash")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type
            or len(self.media_type) > 200
            or any(ord(char) < 32 for char in self.media_type)
        ):
            raise ArtifactVerificationRejected("ARTIFACT_MEDIA_TYPE_INVALID")


class ManagedArtifactAccessPort(Protocol):
    """Authorize and read one exact artifact from private managed storage."""

    def read_managed_artifact(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact: ArtifactReceipt,
    ) -> ManagedArtifactRead: ...


@dataclass(frozen=True)
class ArtifactFormatReceipt:
    artifact_kind: str
    format_verifier_id: str
    format_verifier_version: str
    observed_content_hash: str
    format_verification_hash: str
    declared_source_input_hash: str | None = None
    declared_external_request_id: str | None = None

    def validate(self) -> None:
        _code(self.artifact_kind, "format receipt artifact_kind")
        _code(self.format_verifier_id, "format verifier_id")
        _semver(self.format_verifier_version, "format verifier_version")
        _sha256(self.observed_content_hash, "format observed_content_hash")
        _sha256(self.format_verification_hash, "format_verification_hash")
        if self.declared_source_input_hash is not None:
            _sha256(
                self.declared_source_input_hash,
                "format declared_source_input_hash",
            )
        if self.declared_external_request_id is not None:
            _uuid(
                self.declared_external_request_id,
                "format declared_external_request_id",
            )


class ArtifactKindVerifier(Protocol):
    verifier_id: str
    verifier_version: str
    artifact_kind: str

    def verify(self, artifact: ManagedArtifactRead) -> ArtifactFormatReceipt: ...

    def policy_descriptor(self) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class ArtifactLineageReceipt:
    artifact_id: str
    artifact_kind: str
    task_id: str
    content_hash: str
    byte_size: int
    source_input_hash: str
    managed_derivative: bool
    object_receipt_hash: str
    format_verifier_id: str
    format_verifier_version: str
    format_verification_hash: str
    lineage_hash: str

    @classmethod
    def build(
        cls,
        *,
        artifact: ArtifactReceipt,
        task_id: str,
        managed: ManagedArtifactRead,
        format_receipt: ArtifactFormatReceipt,
    ) -> "ArtifactLineageReceipt":
        payload = {
            "schema_version": "case-agent-artifact-lineage-v1",
            "artifact_id": artifact.artifact_id,
            "artifact_kind": artifact.artifact_kind,
            "task_id": task_id,
            "content_hash": artifact.content_hash,
            "byte_size": artifact.byte_size,
            "source_input_hash": artifact.source_input_hash,
            "managed_derivative": artifact.managed_derivative,
            "object_receipt_hash": managed.object_receipt_hash,
            "format_verifier_id": format_receipt.format_verifier_id,
            "format_verifier_version": format_receipt.format_verifier_version,
            "format_verification_hash": format_receipt.format_verification_hash,
        }
        return cls(**{key: value for key, value in payload.items() if key != "schema_version"}, lineage_hash=_canonical_hash(payload))

    def validate(self) -> None:
        _uuid(self.artifact_id, "lineage artifact_id")
        _code(self.artifact_kind, "lineage artifact_kind")
        _uuid(self.task_id, "lineage task_id")
        _sha256(self.content_hash, "lineage content_hash")
        if not isinstance(self.byte_size, int) or self.byte_size < 0:
            raise ValueError("lineage byte_size is invalid")
        _sha256(self.source_input_hash, "lineage source_input_hash")
        if not isinstance(self.managed_derivative, bool):
            raise ValueError("lineage managed_derivative is invalid")
        _sha256(self.object_receipt_hash, "lineage object_receipt_hash")
        _code(self.format_verifier_id, "lineage format_verifier_id")
        _semver(self.format_verifier_version, "lineage format_verifier_version")
        _sha256(self.format_verification_hash, "lineage format_verification_hash")
        _sha256(self.lineage_hash, "lineage_hash")
        expected = ArtifactLineageReceipt.build(
            artifact=ArtifactReceipt(
                artifact_id=self.artifact_id,
                artifact_kind=self.artifact_kind,
                content_hash=self.content_hash,
                byte_size=self.byte_size,
                source_input_hash=self.source_input_hash,
                managed_derivative=self.managed_derivative,
            ),
            task_id=self.task_id,
            managed=ManagedArtifactRead(
                artifact_id=self.artifact_id,
                artifact_kind=self.artifact_kind,
                content=b"",
                source_input_hash=self.source_input_hash,
                object_receipt_hash=self.object_receipt_hash,
                media_type="application/octet-stream",
            ),
            format_receipt=ArtifactFormatReceipt(
                artifact_kind=self.artifact_kind,
                format_verifier_id=self.format_verifier_id,
                format_verifier_version=self.format_verifier_version,
                observed_content_hash=self.content_hash,
                format_verification_hash=self.format_verification_hash,
            ),
        )
        if expected.lineage_hash != self.lineage_hash:
            raise ValueError("lineage receipt hash differs")


@dataclass(frozen=True)
class RunVerificationReceipt:
    verification_attempt_id: str
    run_id: str
    firm_id: str
    matter_id: str
    verifier_id: str
    verifier_version: str
    policy_hash: str
    verifier_actor_id: str
    execution_actor_id: str
    outcome: VerificationOutcome
    graph_hash: str
    snapshot_hash: str
    task_receipts_hash: str
    artifact_manifest_hash: str
    artifact_lineage: tuple[ArtifactLineageReceipt, ...]
    verified_at: datetime
    error_code: str | None
    verification_hash: str

    @classmethod
    def build(
        cls,
        *,
        verification_attempt_id: str,
        state: AgentRunState,
        verifier_id: str,
        verifier_version: str,
        policy_hash: str,
        verifier_actor_id: str,
        execution_actor_id: str,
        outcome: VerificationOutcome,
        artifact_lineage: tuple[ArtifactLineageReceipt, ...],
        verified_at: datetime,
        error_code: str | None,
    ) -> "RunVerificationReceipt":
        _uuid(verification_attempt_id, "verification_attempt_id")
        if state.graph is None:
            raise ValueError("verification receipt requires a graph")
        graph_hash = state.graph.graph_hash
        snapshot_hash = state.snapshot.snapshot_hash
        task_receipts_hash = _task_receipts_hash(state)
        artifact_manifest_hash = _artifact_manifest_hash(state.artifacts)
        normalized_lineage = tuple(sorted(artifact_lineage, key=lambda item: item.artifact_id))
        payload = {
            "schema_version": "case-agent-run-verification-receipt-v1",
            "verification_attempt_id": verification_attempt_id,
            "run_id": state.run_id,
            "firm_id": state.firm_id,
            "matter_id": state.matter_id,
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "policy_hash": policy_hash,
            "verifier_actor_id": verifier_actor_id,
            "execution_actor_id": execution_actor_id,
            "outcome": outcome.value,
            "graph_hash": graph_hash,
            "snapshot_hash": snapshot_hash,
            "task_receipts_hash": task_receipts_hash,
            "artifact_manifest_hash": artifact_manifest_hash,
            "artifact_lineage": [_lineage_payload(item) for item in normalized_lineage],
            "verified_at": _time_text(verified_at),
            "error_code": error_code,
        }
        return cls(
            verification_attempt_id=verification_attempt_id,
            run_id=state.run_id,
            firm_id=state.firm_id,
            matter_id=state.matter_id,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            policy_hash=policy_hash,
            verifier_actor_id=verifier_actor_id,
            execution_actor_id=execution_actor_id,
            outcome=outcome,
            graph_hash=graph_hash,
            snapshot_hash=snapshot_hash,
            task_receipts_hash=task_receipts_hash,
            artifact_manifest_hash=artifact_manifest_hash,
            artifact_lineage=normalized_lineage,
            verified_at=verified_at,
            error_code=error_code,
            verification_hash=_canonical_hash(payload),
        )

    def validate(self) -> None:
        _uuid(self.verification_attempt_id, "verification_attempt_id")
        for value, label in (
            (self.run_id, "verification run_id"),
            (self.firm_id, "verification firm_id"),
            (self.matter_id, "verification matter_id"),
        ):
            _uuid(value, label)
        _code(self.verifier_id, "verifier_id")
        _semver(self.verifier_version, "verifier_version")
        _sha256(self.policy_hash, "verification policy_hash")
        _uuid(self.verifier_actor_id, "verifier_actor_id")
        _uuid(self.execution_actor_id, "execution_actor_id")
        if self.verifier_actor_id == self.execution_actor_id:
            raise ValueError("verification actor must differ from the execution actor")
        for value, label in (
            (self.graph_hash, "verification graph_hash"),
            (self.snapshot_hash, "verification snapshot_hash"),
            (self.task_receipts_hash, "verification task_receipts_hash"),
            (self.artifact_manifest_hash, "verification artifact_manifest_hash"),
            (self.verification_hash, "verification_hash"),
        ):
            _sha256(value, label)
        _aware(self.verified_at, "verified_at")
        if self.outcome is VerificationOutcome.PASSED:
            if self.error_code is not None:
                raise ValueError("passed verification cannot carry an error")
        elif self.outcome is VerificationOutcome.FAILED:
            _safe_code(self.error_code or "", "verification error_code")
            if self.artifact_lineage:
                raise ValueError("failed verification cannot persist partial artifact lineage")
        else:
            raise ValueError("verification outcome is invalid")
        for lineage in self.artifact_lineage:
            lineage.validate()
        if len({item.artifact_id for item in self.artifact_lineage}) != len(self.artifact_lineage):
            raise ValueError("verification lineage contains duplicate artifact ids")
        payload = {
            "schema_version": "case-agent-run-verification-receipt-v1",
            "verification_attempt_id": self.verification_attempt_id,
            "run_id": self.run_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "policy_hash": self.policy_hash,
            "verifier_actor_id": self.verifier_actor_id,
            "execution_actor_id": self.execution_actor_id,
            "outcome": self.outcome.value,
            "graph_hash": self.graph_hash,
            "snapshot_hash": self.snapshot_hash,
            "task_receipts_hash": self.task_receipts_hash,
            "artifact_manifest_hash": self.artifact_manifest_hash,
            "artifact_lineage": [_lineage_payload(item) for item in self.artifact_lineage],
            "verified_at": _time_text(self.verified_at),
            "error_code": self.error_code,
        }
        if _canonical_hash(payload) != self.verification_hash:
            raise ValueError("verification receipt hash differs")

    def validate_against_state(self, state: AgentRunState) -> None:
        """Rebind a persisted receipt to the store's freshly replayed state."""

        self.validate()
        if (
            state.graph is None
            or state.status is not AgentRunStatus.VERIFYING
            or state.run_id != self.run_id
            or state.firm_id != self.firm_id
            or state.matter_id != self.matter_id
            or state.graph.graph_hash != self.graph_hash
            or state.snapshot.snapshot_hash != self.snapshot_hash
            or _task_receipts_hash(state) != self.task_receipts_hash
            or _artifact_manifest_hash(state.artifacts)
            != self.artifact_manifest_hash
        ):
            raise ValueError(
                "verification receipt differs from the current Agent run"
            )
        if self.outcome is VerificationOutcome.FAILED:
            return
        expected: dict[str, tuple[ArtifactReceipt, str]] = {}
        for runtime in state.tasks:
            if not runtime.receipts:
                raise ValueError(
                    "verification receipt references a task without a result"
                )
            for artifact in runtime.receipts[-1].artifacts:
                if artifact.artifact_id in expected:
                    raise ValueError("current Agent run has duplicate artifact ids")
                expected[artifact.artifact_id] = (artifact, runtime.spec.task_id)
        observed = {item.artifact_id: item for item in self.artifact_lineage}
        if set(observed) != set(expected):
            raise ValueError(
                "verification lineage differs from the current artifact manifest"
            )
        for artifact_id, (artifact, task_id) in expected.items():
            lineage = observed[artifact_id]
            if (
                lineage.artifact_kind != artifact.artifact_kind
                or lineage.task_id != task_id
                or lineage.content_hash != artifact.content_hash
                or lineage.byte_size != artifact.byte_size
                or lineage.source_input_hash != artifact.source_input_hash
                or lineage.managed_derivative != artifact.managed_derivative
            ):
                raise ValueError(
                    "verification lineage differs from its current task artifact"
                )


class CaseAgentRunVerifier:
    """Deterministic verifier with an explicit artifact-kind registry."""

    def __init__(
        self,
        *,
        verifier_id: str,
        verifier_version: str,
        artifact_access: ManagedArtifactAccessPort,
        artifact_verifiers: Mapping[str, ArtifactKindVerifier],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _code(verifier_id, "verifier_id")
        _semver(verifier_version, "verifier_version")
        if not callable(getattr(artifact_access, "read_managed_artifact", None)):
            raise ValueError("managed artifact access port is required")
        normalized: dict[str, ArtifactKindVerifier] = {}
        for kind, verifier in artifact_verifiers.items():
            _code(kind, "registered artifact_kind")
            if (
                getattr(verifier, "artifact_kind", None) != kind
                or not callable(getattr(verifier, "verify", None))
            ):
                raise ValueError("artifact verifier registry binding is invalid")
            _code(getattr(verifier, "verifier_id", ""), "artifact verifier_id")
            _semver(getattr(verifier, "verifier_version", ""), "artifact verifier_version")
            normalized[kind] = verifier
        self.verifier_id = verifier_id
        self.verifier_version = verifier_version
        self._access = artifact_access
        self._artifact_verifiers = normalized
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy_hash = _run_verifier_policy_hash(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            artifact_verifiers=normalized,
        )

    def verify(
        self,
        *,
        verification_attempt_id: str,
        state: AgentRunState,
        verifier_actor_id: str,
        execution_actor_id: str,
    ) -> RunVerificationReceipt:
        verified_at = self._clock()
        _aware(verified_at, "verification clock")
        error_code, artifact_tasks = _structural_failure(state)
        if error_code is not None:
            return self._failed(
                verification_attempt_id=verification_attempt_id,
                state=state,
                verifier_actor_id=verifier_actor_id,
                execution_actor_id=execution_actor_id,
                verified_at=verified_at,
                error_code=error_code,
            )
        assert state.graph is not None
        final_receipts = {
            runtime.spec.task_id: runtime.receipts[-1]
            for runtime in state.tasks
        }
        task_specs = {runtime.spec.task_id: runtime.spec for runtime in state.tasks}
        missing = sorted(
            {artifact.artifact_kind for artifact in state.artifacts}
            - set(self._artifact_verifiers)
        )
        if missing:
            return self._failed(
                verification_attempt_id=verification_attempt_id,
                state=state,
                verifier_actor_id=verifier_actor_id,
                execution_actor_id=execution_actor_id,
                verified_at=verified_at,
                error_code="ARTIFACT_VERIFIER_NOT_REGISTERED",
            )
        lineage: list[ArtifactLineageReceipt] = []
        try:
            for artifact in sorted(state.artifacts, key=lambda item: item.artifact_id):
                managed = self._access.read_managed_artifact(
                    firm_id=state.firm_id,
                    matter_id=state.matter_id,
                    run_id=state.run_id,
                    artifact=artifact,
                )
                if not isinstance(managed, ManagedArtifactRead):
                    raise ArtifactVerificationRejected("ARTIFACT_OBJECT_INVALID")
                managed.validate()
                observed_hash = sha256(managed.content).hexdigest()
                if (
                    managed.artifact_id != artifact.artifact_id
                    or managed.artifact_kind != artifact.artifact_kind
                    or observed_hash != artifact.content_hash
                    or len(managed.content) != artifact.byte_size
                    or managed.source_input_hash != artifact.source_input_hash
                ):
                    raise ArtifactVerificationRejected("ARTIFACT_LINEAGE_MISMATCH")
                verifier = self._artifact_verifiers[artifact.artifact_kind]
                format_receipt = verifier.verify(managed)
                if not isinstance(format_receipt, ArtifactFormatReceipt):
                    raise ArtifactVerificationRejected("ARTIFACT_FORMAT_RECEIPT_INVALID")
                format_receipt.validate()
                if artifact.artifact_kind == _CASE_CONTEXT_ARTIFACT_KIND:
                    try:
                        case_context_value = json.loads(
                            managed.content.decode("utf-8"),
                            object_pairs_hook=_reject_duplicate_json_object,
                            parse_constant=_reject_json_constant,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_CASE_CONTEXT_SOURCE_BINDING_INVALID"
                        ) from None
                    if not isinstance(case_context_value, dict):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_CASE_CONTEXT_SOURCE_BINDING_INVALID"
                        )
                    declared_refs = _validate_case_context_candidate_payload(
                        case_context_value
                    )
                    task_spec = task_specs[
                        artifact_tasks[artifact.artifact_id]
                    ]
                    if declared_refs != frozenset(task_spec.input_refs):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_CASE_CONTEXT_SOURCE_BINDING_INVALID"
                        )
                if artifact.artifact_kind == CASE_LEDGER_EXTRACTION_ARTIFACT_KIND:
                    try:
                        extraction_value = parse_case_ledger_extraction_candidate(
                            managed.content
                        )
                        declared_refs = extraction_source_refs(extraction_value)
                    except CaseLedgerExtractionBlocked:
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LEDGER_EXTRACTION_SOURCE_BINDING_INVALID"
                        ) from None
                    task_spec = task_specs[artifact_tasks[artifact.artifact_id]]
                    if declared_refs != _ledger_extraction_authorized_source_refs(
                        task_spec=task_spec,
                        task_specs=task_specs,
                    ):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LEDGER_EXTRACTION_SOURCE_BINDING_INVALID"
                        )
                if artifact.artifact_kind == LAWYER_DECISION_PACKAGE_ARTIFACT_KIND:
                    try:
                        parse_lawyer_decision_package_candidate(managed.content)
                        declared_refs = lawyer_decision_package_source_refs(
                            managed.content
                        )
                    except LawyerAnalysisBlocked:
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LAWYER_PACKAGE_SOURCE_BINDING_INVALID"
                        ) from None
                    task_spec = task_specs[artifact_tasks[artifact.artifact_id]]
                    if declared_refs != frozenset(task_spec.input_refs):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LAWYER_PACKAGE_SOURCE_BINDING_INVALID"
                        )
                if artifact.artifact_kind == LEGAL_RESEARCH_PLAN_ARTIFACT_KIND:
                    try:
                        declared_refs = legal_research_plan_source_refs(
                            managed.content
                        )
                    except LegalResearchPlanBlocked:
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LEGAL_RESEARCH_PLAN_SOURCE_BINDING_INVALID"
                        ) from None
                    task_spec = task_specs[artifact_tasks[artifact.artifact_id]]
                    if declared_refs != frozenset(task_spec.input_refs):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_LEGAL_RESEARCH_PLAN_SOURCE_BINDING_INVALID"
                        )
                if (
                    format_receipt.artifact_kind != artifact.artifact_kind
                    or format_receipt.format_verifier_id != verifier.verifier_id
                    or format_receipt.format_verifier_version != verifier.verifier_version
                    or format_receipt.observed_content_hash != observed_hash
                    or (
                        format_receipt.declared_source_input_hash is not None
                        and format_receipt.declared_source_input_hash
                        != artifact.source_input_hash
                    )
                    or (
                        format_receipt.declared_external_request_id is not None
                        and format_receipt.declared_external_request_id
                        != final_receipts[
                            artifact_tasks[artifact.artifact_id]
                        ].external_request_id
                    )
                ):
                    raise ArtifactVerificationRejected("ARTIFACT_FORMAT_RECEIPT_MISMATCH")
                lineage.append(
                    ArtifactLineageReceipt.build(
                        artifact=artifact,
                        task_id=artifact_tasks[artifact.artifact_id],
                        managed=managed,
                        format_receipt=format_receipt,
                    )
                )
        except ArtifactVerificationRejected as error:
            return self._failed(
                verification_attempt_id=verification_attempt_id,
                state=state,
                verifier_actor_id=verifier_actor_id,
                execution_actor_id=execution_actor_id,
                verified_at=verified_at,
                error_code=error.error_code,
            )
        except CaseAgentVerificationIndeterminate:
            raise
        except Exception as error:
            raise CaseAgentVerificationIndeterminate(
                "managed artifact verification did not reach a terminal observation"
            ) from error
        receipt = RunVerificationReceipt.build(
            verification_attempt_id=verification_attempt_id,
            state=state,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            policy_hash=self.policy_hash,
            verifier_actor_id=verifier_actor_id,
            execution_actor_id=execution_actor_id,
            outcome=VerificationOutcome.PASSED,
            artifact_lineage=tuple(lineage),
            verified_at=verified_at,
            error_code=None,
        )
        receipt.validate()
        return receipt

    def _failed(
        self,
        *,
        verification_attempt_id: str,
        state: AgentRunState,
        verifier_actor_id: str,
        execution_actor_id: str,
        verified_at: datetime,
        error_code: str,
    ) -> RunVerificationReceipt:
        receipt = RunVerificationReceipt.build(
            verification_attempt_id=verification_attempt_id,
            state=state,
            verifier_id=self.verifier_id,
            verifier_version=self.verifier_version,
            policy_hash=self.policy_hash,
            verifier_actor_id=verifier_actor_id,
            execution_actor_id=execution_actor_id,
            outcome=VerificationOutcome.FAILED,
            artifact_lineage=(),
            verified_at=verified_at,
            error_code=error_code,
        )
        receipt.validate()
        return receipt


class CanonicalJsonArtifactVerifier:
    """Strict JSON-object verifier for review candidates and manifests."""

    def __init__(
        self,
        *,
        artifact_kind: str,
        allowed_schema_versions: tuple[str, ...],
        payload_kind: str = "GENERIC_CANONICAL_JSON",
        verifier_id: str = "canonical-json-artifact-verifier",
        verifier_version: str = "1.0.0",
    ) -> None:
        _code(artifact_kind, "artifact_kind")
        _code(verifier_id, "format verifier_id")
        _semver(verifier_version, "format verifier_version")
        if not allowed_schema_versions or len(set(allowed_schema_versions)) != len(allowed_schema_versions):
            raise ValueError("allowed JSON schemas must be a non-empty unique tuple")
        for value in allowed_schema_versions:
            _code(value, "allowed JSON schema_version")
        if payload_kind not in {
            "GENERIC_CANONICAL_JSON",
            "COMMON_DOCUMENT_REVIEW_CANDIDATE",
            "PDF_TEXT_REVIEW_CANDIDATE",
            "PUBLIC_RESEARCH_LEADS_CANDIDATE",
            "VISUAL_PAGE_REVIEW_CANDIDATE",
            "CASE_CONTEXT_REVIEW_CANDIDATE",
            CASE_LEDGER_EXTRACTION_ARTIFACT_KIND,
            LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
            LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
        }:
            raise ValueError("canonical JSON payload kind is invalid")
        self.artifact_kind = artifact_kind
        self.allowed_schema_versions = allowed_schema_versions
        self.payload_kind = payload_kind
        self.verifier_id = verifier_id
        self.verifier_version = verifier_version

    def policy_descriptor(self) -> Mapping[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "allowed_schema_versions": list(self.allowed_schema_versions),
            "payload_kind": self.payload_kind,
            "requires_canonical_utf8_json_object": True,
            "requires_task_input_hash": self.payload_kind
            != "GENERIC_CANONICAL_JSON",
            "requires_source_hash": self.payload_kind
            != "GENERIC_CANONICAL_JSON",
            "requires_review_only_declarations": self.payload_kind
            != "GENERIC_CANONICAL_JSON",
            "strict_public_research_contract": (
                _public_research_policy_descriptor()
                if self.payload_kind == _PUBLIC_RESEARCH_ARTIFACT_KIND
                else None
            ),
            "strict_visual_page_contract": (
                _visual_page_policy_descriptor()
                if self.payload_kind == _VISUAL_ARTIFACT_KIND
                else None
            ),
            "strict_case_context_contract": (
                {
                    "schema_version": _CASE_CONTEXT_SCHEMA,
                    "exact_top_level_fields": sorted(
                        _CASE_CONTEXT_TOP_LEVEL_FIELDS
                    ),
                    "section_ids": list(_CASE_CONTEXT_SECTION_IDS),
                    "requires_exact_task_source_refs": True,
                    "court_ready": False,
                }
                if self.payload_kind == _CASE_CONTEXT_ARTIFACT_KIND
                else None
            ),
            "strict_lawyer_decision_package_contract": (
                {
                    "schema_version": LAWYER_DECISION_PACKAGE_SCHEMA,
                    "requires_exact_task_source_refs": True,
                    "binds_external_request_id_to_final_task_receipt": True,
                    "court_ready": False,
                    "legal_conclusion": False,
                    "official_numeric_result_authored_by_model": False,
                }
                if self.payload_kind == LAWYER_DECISION_PACKAGE_ARTIFACT_KIND
                else None
            ),
            "strict_legal_research_plan_contract": (
                {
                    "schema_version": LEGAL_RESEARCH_PLAN_SCHEMA,
                    "requires_exact_task_source_refs": True,
                    "network_access": False,
                    "legal_conclusion": False,
                    "external_search_requires_separate_lawyer_approval": True,
                }
                if self.payload_kind == LEGAL_RESEARCH_PLAN_ARTIFACT_KIND
                else None
            ),
        }

    def verify(self, artifact: ManagedArtifactRead) -> ArtifactFormatReceipt:
        if artifact.artifact_kind != self.artifact_kind or artifact.media_type != "application/json":
            raise ArtifactVerificationRejected("ARTIFACT_FORMAT_MISMATCH")
        try:
            decoded = artifact.content.decode("utf-8")
            value = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ArtifactVerificationRejected("ARTIFACT_JSON_INVALID") from None
        if not isinstance(value, dict) or value.get("schema_version") not in self.allowed_schema_versions:
            raise ArtifactVerificationRejected("ARTIFACT_JSON_SCHEMA_INVALID")
        canonical = _json_bytes(value)
        if canonical != artifact.content:
            raise ArtifactVerificationRejected("ARTIFACT_JSON_NOT_CANONICAL")
        declared = value.get("task_input_hash")
        if declared is not None and not isinstance(declared, str):
            raise ArtifactVerificationRejected("ARTIFACT_DECLARED_LINEAGE_INVALID")
        if declared is not None and _SHA256_RE.fullmatch(declared) is None:
            raise ArtifactVerificationRejected("ARTIFACT_DECLARED_LINEAGE_INVALID")
        if self.payload_kind != "GENERIC_CANONICAL_JSON":
            _validate_review_candidate_payload(value, payload_kind=self.payload_kind)
        content_hash = sha256(artifact.content).hexdigest()
        format_hash = _canonical_hash(
            {
                "schema_version": "canonical-json-format-verification-v1",
                "artifact_kind": self.artifact_kind,
                "payload_schema_version": value["schema_version"],
                "content_hash": content_hash,
                "top_level_keys": sorted(value),
            }
        )
        return ArtifactFormatReceipt(
            artifact_kind=self.artifact_kind,
            format_verifier_id=self.verifier_id,
            format_verifier_version=self.verifier_version,
            observed_content_hash=content_hash,
            format_verification_hash=format_hash,
            declared_source_input_hash=declared,
            declared_external_request_id=(
                value.get("external_request_id")
                if self.payload_kind
                in {
                    _PUBLIC_RESEARCH_ARTIFACT_KIND,
                    _VISUAL_ARTIFACT_KIND,
                    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
                }
                else None
            ),
        )


class OpenXmlArtifactVerifier:
    """Bounded DOCX/XLSX container check without executing embedded content."""

    _REQUIRED = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            "[Content_Types].xml",
            "word/document.xml",
        ),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
            "[Content_Types].xml",
            "xl/workbook.xml",
        ),
    }

    def __init__(self, *, artifact_kind: str, verifier_id: str = "openxml-container-verifier", verifier_version: str = "1.0.0") -> None:
        _code(artifact_kind, "artifact_kind")
        _code(verifier_id, "format verifier_id")
        _semver(verifier_version, "format verifier_version")
        self.artifact_kind = artifact_kind
        self.verifier_id = verifier_id
        self.verifier_version = verifier_version

    def policy_descriptor(self) -> Mapping[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "allowed_media_types": sorted(self._REQUIRED),
            "required_members": {
                media_type: list(members)
                for media_type, members in sorted(self._REQUIRED.items())
            },
            "maximum_members": 20_000,
            "maximum_uncompressed_bytes": 512 * 1024 * 1024,
            "rejects_active_content": True,
        }

    def verify(self, artifact: ManagedArtifactRead) -> ArtifactFormatReceipt:
        required = self._REQUIRED.get(artifact.media_type)
        if artifact.artifact_kind != self.artifact_kind or required is None:
            raise ArtifactVerificationRejected("ARTIFACT_FORMAT_MISMATCH")
        try:
            with ZipFile(BytesIO(artifact.content)) as archive:
                infos = archive.infolist()
                names = {item.filename for item in infos}
                if len(infos) > 20_000 or sum(item.file_size for item in infos) > 512 * 1024 * 1024:
                    raise ArtifactVerificationRejected("ARTIFACT_ARCHIVE_LIMIT_EXCEEDED")
                if any(name.startswith("/") or ".." in name.split("/") for name in names):
                    raise ArtifactVerificationRejected("ARTIFACT_ARCHIVE_PATH_INVALID")
                lowered = {name.lower() for name in names}
                if any(
                    name.endswith(("vbaproject.bin", ".exe", ".dll"))
                    or "/externallinks/" in name
                    or name.endswith("/attachedtoolbars.bin")
                    for name in lowered
                ):
                    raise ArtifactVerificationRejected("ARTIFACT_ACTIVE_CONTENT_REJECTED")
                if not all(name in names for name in required):
                    raise ArtifactVerificationRejected("ARTIFACT_OPENXML_INVALID")
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise ArtifactVerificationRejected("ARTIFACT_OPENXML_INVALID")
        except BadZipFile:
            raise ArtifactVerificationRejected("ARTIFACT_OPENXML_INVALID") from None
        content_hash = sha256(artifact.content).hexdigest()
        return ArtifactFormatReceipt(
            artifact_kind=self.artifact_kind,
            format_verifier_id=self.verifier_id,
            format_verifier_version=self.verifier_version,
            observed_content_hash=content_hash,
            format_verification_hash=_canonical_hash(
                {
                    "schema_version": "openxml-format-verification-v1",
                    "artifact_kind": self.artifact_kind,
                    "media_type": artifact.media_type,
                    "content_hash": content_hash,
                    "required_members": required,
                }
            ),
        )


class ReviewableDocumentCandidateArtifactVerifier:
    """Independent strict check for the source-bound DOCX/XLSX candidate JSON."""

    artifact_kind = "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"
    verifier_id = "reviewable-document-candidate-verifier"
    verifier_version = "1.0.0"
    _SCHEMAS = (
        "case-agent-reviewable-docx-candidate-v1",
        "case-agent-reviewable-xlsx-candidate-v1",
    )

    def policy_descriptor(self) -> Mapping[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "allowed_schema_versions": list(self._SCHEMAS),
            "requires_canonical_utf8_json_object": True,
            "requires_nested_task_input_hash": True,
            "requires_source_ref_per_paragraph_or_row": True,
            "requires_review_only_declarations": True,
            "payment_ledger_requires_fixed_columns_and_one_transaction_per_row": True,
        }

    def verify(self, artifact: ManagedArtifactRead) -> ArtifactFormatReceipt:
        if (
            artifact.artifact_kind != self.artifact_kind
            or artifact.media_type != "application/json"
        ):
            raise ArtifactVerificationRejected("ARTIFACT_FORMAT_MISMATCH")
        try:
            value = json.loads(
                artifact.content.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ArtifactVerificationRejected("ARTIFACT_JSON_INVALID") from None
        if (
            not isinstance(value, dict)
            or _json_bytes(value) != artifact.content
            or value.get("schema_version") not in self._SCHEMAS
        ):
            raise ArtifactVerificationRejected("ARTIFACT_JSON_SCHEMA_INVALID")
        _verify_reviewable_document_candidate(value)
        binding = value["binding"]
        content_hash = sha256(artifact.content).hexdigest()
        return ArtifactFormatReceipt(
            artifact_kind=self.artifact_kind,
            format_verifier_id=self.verifier_id,
            format_verifier_version=self.verifier_version,
            observed_content_hash=content_hash,
            format_verification_hash=_canonical_hash(
                {
                    "schema_version": "reviewable-document-candidate-verification-v1",
                    "artifact_kind": self.artifact_kind,
                    "payload_schema_version": value["schema_version"],
                    "content_hash": content_hash,
                    "binding_hash": binding["binding_hash"],
                    "source_set_hash": binding["source_set_hash"],
                    "task_input_hash": binding["task_input_hash"],
                    "template_hash": binding["template_hash"],
                }
            ),
            declared_source_input_hash=binding["task_input_hash"],
        )


class ReviewablePdfArtifactVerifier:
    """Parse a review PDF without executing actions, scripts or attachments."""

    artifact_kind = "REVIEWABLE_DOCUMENT_PDF_PREVIEW"
    verifier_id = "reviewable-pdf-structure-verifier"
    verifier_version = "1.0.1"

    def policy_descriptor(self) -> Mapping[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "media_type": "application/pdf",
            "minimum_pages": 1,
            "maximum_pages": 10_000,
            "rejects_encryption": True,
            "rejects_javascript_actions_attachments_and_forms": True,
            "allows_valid_local_page_open_destination": True,
        }

    def verify(self, artifact: ManagedArtifactRead) -> ArtifactFormatReceipt:
        if (
            artifact.artifact_kind != self.artifact_kind
            or artifact.media_type != "application/pdf"
            or not artifact.content.startswith(b"%PDF-")
        ):
            raise ArtifactVerificationRejected("ARTIFACT_FORMAT_MISMATCH")
        try:
            reader = PdfReader(BytesIO(artifact.content), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 10_000:
                raise ArtifactVerificationRejected("ARTIFACT_PDF_INVALID")
            root = reader.trailer.get("/Root")
            root = root.get_object() if hasattr(root, "get_object") else root
            if not isinstance(root, Mapping) or any(
                key in root for key in ("/AA", "/AcroForm")
            ):
                raise ArtifactVerificationRejected("ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED")
            open_action = root.get("/OpenAction")
            if open_action is not None and not _is_local_pdf_open_destination(
                open_action
            ):
                raise ArtifactVerificationRejected("ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED")
            names = root.get("/Names")
            names = names.get_object() if hasattr(names, "get_object") else names
            if isinstance(names, Mapping) and any(
                key in names for key in ("/JavaScript", "/EmbeddedFiles")
            ):
                raise ArtifactVerificationRejected("ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED")
            for page in reader.pages:
                if any(key in page for key in ("/AA",)):
                    raise ArtifactVerificationRejected(
                        "ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED"
                    )
                annotations = page.get("/Annots")
                if annotations is None:
                    continue
                annotations = (
                    annotations.get_object()
                    if hasattr(annotations, "get_object")
                    else annotations
                )
                for annotation in annotations or ():
                    annotation = (
                        annotation.get_object()
                        if hasattr(annotation, "get_object")
                        else annotation
                    )
                    if isinstance(annotation, Mapping) and any(
                        key in annotation for key in ("/A", "/AA", "/JS")
                    ):
                        raise ArtifactVerificationRejected(
                            "ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED"
                        )
        except ArtifactVerificationRejected:
            raise
        except Exception:
            raise ArtifactVerificationRejected("ARTIFACT_PDF_INVALID") from None
        content_hash = sha256(artifact.content).hexdigest()
        return ArtifactFormatReceipt(
            artifact_kind=self.artifact_kind,
            format_verifier_id=self.verifier_id,
            format_verifier_version=self.verifier_version,
            observed_content_hash=content_hash,
            format_verification_hash=_canonical_hash(
                {
                    "schema_version": "reviewable-pdf-format-verification-v1",
                    "artifact_kind": self.artifact_kind,
                    "content_hash": content_hash,
                    "page_count": len(reader.pages),
                    "active_content_rejected": True,
                }
            ),
        )


def _is_local_pdf_open_destination(value: object) -> bool:
    """Allow only a passive local page destination, never an action object.

    PDF ``/OpenAction`` is overloaded by the specification: an action
    dictionary may execute JavaScript, while an array merely selects the
    initial page and viewport.  LibreOffice emits the latter for ordinary
    Office-to-PDF exports.  Validate that narrow destination grammar instead
    of treating the shared key name as proof of executable content.
    """

    value = value.get_object() if hasattr(value, "get_object") else value
    if not isinstance(value, ArrayObject) or len(value) < 2:
        return False
    page_reference = value[0]
    if not hasattr(page_reference, "get_object"):
        return False
    page = page_reference.get_object()
    if not isinstance(page, Mapping) or str(page.get("/Type")) != "/Page":
        return False
    mode = str(value[1])
    expected_length = {
        "/XYZ": 5,
        "/Fit": 2,
        "/FitH": 3,
        "/FitV": 3,
        "/FitR": 6,
        "/FitB": 2,
        "/FitBH": 3,
        "/FitBV": 3,
    }.get(mode)
    if expected_length is None or len(value) != expected_length:
        return False
    coordinates = value[2:]
    if mode == "/FitR":
        return all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in coordinates
        )
    return all(
        isinstance(item, (int, float, NullObject)) and not isinstance(item, bool)
        for item in coordinates
    )


def _verify_reviewable_document_candidate(value: Mapping[str, object]) -> None:
    common = {
        "schema_version",
        "binding",
        "title",
        "review_status",
        "formal_fact",
        "formal_legal_conclusion",
        "court_ready",
    }
    schema = value.get("schema_version")
    expected = common | ({"sections"} if schema == "case-agent-reviewable-docx-candidate-v1" else {"columns", "rows"})
    if (
        set(value) != expected
        or value.get("review_status") != "NEEDS_LAWYER_REVIEW"
        or value.get("formal_fact") is not False
        or value.get("formal_legal_conclusion") is not False
        or value.get("court_ready") is not False
        or not _bounded_review_text(value.get("title"), 240)
    ):
        raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
    binding = value.get("binding")
    binding_keys = {
        "binding_hash",
        "source_set_hash",
        "task_input_hash",
        "work_plan_item_id",
        "template_id",
        "template_version",
        "template_hash",
        "deliverable_kind",
        "output_format",
    }
    if not isinstance(binding, dict) or set(binding) != binding_keys:
        raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_BINDING_INVALID")
    try:
        UUID(str(binding["work_plan_item_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_BINDING_INVALID") from None
    if (
        any(
            not _is_sha256(binding.get(key))
            for key in (
                "binding_hash",
                "source_set_hash",
                "task_input_hash",
                "template_hash",
            )
        )
        or not isinstance(binding.get("template_id"), str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", str(binding["template_id"])
        ) is None
        or not isinstance(binding.get("template_version"), str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            str(binding["template_version"]),
        ) is None
        or not isinstance(binding.get("deliverable_kind"), str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{1,119}", str(binding["deliverable_kind"]))
        is None
        or binding.get("output_format")
        != ("DOCX" if schema == "case-agent-reviewable-docx-candidate-v1" else "XLSX")
    ):
        raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_BINDING_INVALID")

    if schema == "case-agent-reviewable-docx-candidate-v1":
        sections = value.get("sections")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 200:
            raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
        paragraphs = 0
        characters = 0
        for section in sections:
            if (
                not isinstance(section, dict)
                or set(section) != {"heading", "paragraphs"}
                or not _bounded_review_text(section.get("heading"), 240)
                or not isinstance(section.get("paragraphs"), list)
                or not section["paragraphs"]
            ):
                raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
            for paragraph in section["paragraphs"]:
                if (
                    not isinstance(paragraph, dict)
                    or set(paragraph) != {"text", "source_refs"}
                    or not _bounded_review_text(paragraph.get("text"), 20_000)
                    or not _valid_review_source_refs(paragraph.get("source_refs"))
                ):
                    raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
                paragraphs += 1
                characters += len(paragraph["text"])
                if paragraphs > 20_000 or characters > 1_500_000:
                    raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
        return

    columns = value.get("columns")
    rows = value.get("rows")
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 200
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= 100_000
    ):
        raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
    if binding.get("deliverable_kind") == "PAYMENT_LEDGER":
        expected_columns = [
            {"key": key, "label": label, "value_type": value_type}
            for key, label, value_type in _PAYMENT_LEDGER_COLUMNS
        ]
        if columns != expected_columns:
            raise ArtifactVerificationRejected(
                "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC"
            )
        expected_row_ids: list[str] = []
        for row in rows:
            row_id = row.get("row_id") if isinstance(row, dict) else None
            if (
                not isinstance(row_id, str)
                or not row_id.startswith("transaction:")
                or row.get("source_refs") != [row_id]
            ):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC"
                )
            try:
                UUID(row_id.removeprefix("transaction:"))
            except (ValueError, TypeError, AttributeError):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC"
                ) from None
            expected_row_ids.append(row_id)
        if expected_row_ids != sorted(expected_row_ids):
            raise ArtifactVerificationRejected(
                "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC"
            )
    keys: list[str] = []
    types: dict[str, str] = {}
    for column in columns:
        if (
            not isinstance(column, dict)
            or set(column) != {"key", "label", "value_type"}
            or not isinstance(column.get("key"), str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", column["key"]) is None
            or column["key"] in keys
            or not _bounded_review_text(column.get("label"), 160)
            or column.get("value_type")
            not in {"TEXT", "INTEGER", "DECIMAL", "DATE", "BOOLEAN"}
        ):
            raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
        keys.append(column["key"])
        types[column["key"]] = column["value_type"]
    row_ids: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"row_id", "cells", "source_refs"}
            or not isinstance(row.get("row_id"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", row["row_id"])
            is None
            or row["row_id"] in row_ids
            or not isinstance(row.get("cells"), dict)
            or list(row["cells"]) != sorted(row["cells"])
            or set(row["cells"]) != set(keys)
            or not _valid_review_source_refs(row.get("source_refs"))
        ):
            raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
        for key, cell in row["cells"].items():
            if not _valid_review_cell(cell, types[key]):
                raise ArtifactVerificationRejected("ARTIFACT_DOCUMENT_CANDIDATE_INVALID")
        row_ids.add(row["row_id"])


def _bounded_review_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _valid_review_source_refs(value: object) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        return False
    if not all(
        isinstance(item, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", item)
        is not None
        for item in value
    ):
        return False
    return len(set(value)) == len(value)


def _valid_review_cell(value: object, value_type: str) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value_type == "BOOLEAN"
    if value_type == "INTEGER":
        return isinstance(value, int)
    if value_type == "DECIMAL":
        try:
            return isinstance(value, (int, float)) and math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False
    if value_type in {"TEXT", "DATE"}:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= 20_000
            and value[0] not in "=+-@"
            and "\x00" not in value
        )
    return False


def _run_verifier_policy_hash(
    *,
    verifier_id: str,
    verifier_version: str,
    artifact_verifiers: Mapping[str, ArtifactKindVerifier],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "case-agent-run-verifier-policy-v1",
            "verifier_id": verifier_id,
            "verifier_version": verifier_version,
            "artifact_verifiers": [
                _artifact_verifier_policy_descriptor(kind, verifier)
                for kind, verifier in sorted(artifact_verifiers.items())
            ],
            "structural_checks": list(_STRUCTURAL_CHECKS),
        }
    )


def first_release_review_candidate_verifiers() -> Mapping[str, ArtifactKindVerifier]:
    """Exact registry for artifact kinds executable in the first release.

    Inclusion here proves only that the artifact has a strict independent
    verifier.  Runtime readiness still requires the corresponding concrete
    adapter and managed-object reader to be present in the server composition.
    """

    verifiers: dict[str, ArtifactKindVerifier] = {
        kind: CanonicalJsonArtifactVerifier(
            artifact_kind=kind,
            allowed_schema_versions=(
                schema
                if isinstance(schema, tuple)
                else (schema,)
            ),
            payload_kind=kind,
        )
        for kind, schema in FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS
    }
    verifiers["REVIEWABLE_DOCUMENT_CANDIDATE_JSON"] = (
        ReviewableDocumentCandidateArtifactVerifier()
    )
    verifiers["REVIEWABLE_DOCUMENT_EDITABLE"] = OpenXmlArtifactVerifier(
        artifact_kind="REVIEWABLE_DOCUMENT_EDITABLE"
    )
    verifiers["REVIEWABLE_DOCUMENT_PDF_PREVIEW"] = ReviewablePdfArtifactVerifier()
    return verifiers


def first_release_verifier_policy_hash() -> str:
    """Return the canonical first-release policy without artifact access."""

    return _run_verifier_policy_hash(
        verifier_id=FIRST_RELEASE_VERIFIER_ID,
        verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
        artifact_verifiers=first_release_review_candidate_verifiers(),
    )


def build_first_release_case_agent_run_verifier(
    *,
    artifact_access: ManagedArtifactAccessPort,
    clock: Callable[[], datetime] | None = None,
) -> CaseAgentRunVerifier:
    """Build the only verifier composition advertised by first-release readiness."""

    verifier = CaseAgentRunVerifier(
        verifier_id=FIRST_RELEASE_VERIFIER_ID,
        verifier_version=FIRST_RELEASE_VERIFIER_VERSION,
        artifact_access=artifact_access,
        artifact_verifiers=first_release_review_candidate_verifiers(),
        clock=clock,
    )
    if verifier.policy_hash != FIRST_RELEASE_VERIFIER_POLICY_HASH:
        raise RuntimeError("first-release verifier policy identity drifted")
    return verifier


def _structural_failure(state: AgentRunState) -> tuple[str | None, dict[str, str]]:
    if (
        state.status is not AgentRunStatus.VERIFYING
        or state.graph is None
        or state.cancelled
        or state.stale
        or state.verification_hash is not None
    ):
        return "RUN_NOT_VERIFIABLE", {}
    if state.graph.snapshot != state.snapshot or _graph_hash(state) != state.graph.graph_hash:
        return "CURRENT_GRAPH_HASH_MISMATCH", {}
    if len(state.tasks) != len(state.graph.tasks):
        return "CURRENT_TASK_GRAPH_MISMATCH", {}
    runtime_by_id = {item.spec.task_id: item for item in state.tasks}
    if len(runtime_by_id) != len(state.tasks):
        return "CURRENT_TASK_GRAPH_MISMATCH", {}
    artifact_tasks: dict[str, str] = {}
    final_artifacts: list[ArtifactReceipt] = []
    for spec in state.graph.tasks:
        runtime = runtime_by_id.get(spec.task_id)
        if runtime is None or runtime.spec != spec or runtime.status is not AgentTaskStatus.SUCCEEDED:
            return "TASK_NOT_SUCCEEDED", {}
        if not runtime.receipts:
            return "TASK_FINAL_RECEIPT_MISSING", {}
        final = runtime.receipts[-1]
        if (
            final.status is not ResultStatus.SUCCEEDED
            or final.input_hash != spec.input_hash
            or final.adapter_id != spec.skill.adapter_id
            or final.adapter_version != spec.skill.adapter_version
            or final.output_hash is None
            or _SHA256_RE.fullmatch(final.output_hash) is None
        ):
            return "TASK_FINAL_RECEIPT_BINDING_MISMATCH", {}
        if spec.capability.network_policy is NetworkPolicy.DENY:
            if (
                final.external_submission_state is not ExternalSubmissionState.NOT_APPLICABLE
                or final.external_request_id is not None
                or final.external_calls != 0
            ):
                return "EXTERNAL_RESULT_NOT_RECONCILED", {}
        elif (
            final.external_submission_state is not ExternalSubmissionState.SUBMITTED
            or not final.external_request_id
        ):
            return "EXTERNAL_RESULT_NOT_RECONCILED", {}
        unknown = [receipt for receipt in runtime.receipts[:-1] if receipt.status is ResultStatus.UNKNOWN or receipt.external_submission_state is ExternalSubmissionState.UNKNOWN]
        if unknown and any(item.external_request_id != final.external_request_id for item in unknown):
            return "EXTERNAL_RESULT_NOT_RECONCILED", {}
        for artifact in final.artifacts:
            if artifact.artifact_id in artifact_tasks:
                return "ARTIFACT_MANIFEST_DUPLICATE", {}
            if artifact.source_input_hash != spec.input_hash:
                return "ARTIFACT_LINEAGE_MISMATCH", {}
            artifact_tasks[artifact.artifact_id] = spec.task_id
            final_artifacts.append(artifact)
    if tuple(sorted(final_artifacts, key=lambda item: item.artifact_id)) != tuple(sorted(state.artifacts, key=lambda item: item.artifact_id)):
        return "ARTIFACT_MANIFEST_MISMATCH", {}
    identity_tuples = {
        (item.content_hash, item.byte_size, item.source_input_hash)
        for item in state.artifacts
    }
    if len(identity_tuples) != len(state.artifacts):
        return "ARTIFACT_MANIFEST_DUPLICATE", {}
    usage, budget = state.budget_usage, state.budget
    if (
        usage.attempts > budget.max_total_attempts
        or usage.external_calls > budget.max_external_calls
        or usage.runtime_seconds > budget.max_runtime_seconds
        or usage.cost_minor_units > budget.max_cost_minor_units
        or usage.output_bytes > budget.max_output_bytes
    ):
        return "RUN_BUDGET_EXCEEDED", {}
    return None, artifact_tasks


def _graph_hash(state: AgentRunState) -> str:
    assert state.graph is not None
    return _canonical_hash(
        {
            "schema_version": "lawyer-agent-task-graph-v1",
            "graph_id": state.graph.graph_id,
            "graph_version": state.graph.graph_version,
            "goal_hash": state.graph.goal_hash,
            "snapshot": {
                "matter_id": state.graph.snapshot.matter_id,
                "matter_version": state.graph.snapshot.matter_version,
                "snapshot_hash": state.graph.snapshot.snapshot_hash,
                "schema_version": state.graph.snapshot.schema_version,
            },
            "tasks": [_task_payload(item) for item in state.graph.tasks],
        }
    )


def _ledger_extraction_authorized_source_refs(
    *, task_spec: object, task_specs: Mapping[str, object]
) -> frozenset[str]:
    """Bind extraction to its inputs plus exact direct visual dependencies."""

    refs = set(task_spec.input_refs)
    for dependency_id in task_spec.dependency_ids:
        dependency = task_specs.get(dependency_id)
        if dependency is None:
            raise ArtifactVerificationRejected(
                "ARTIFACT_LEDGER_EXTRACTION_SOURCE_BINDING_INVALID"
            )
        if dependency.skill.tool_id != "understand_visual_page":
            continue
        if (
            len(dependency.input_refs) != 1
            or not dependency.input_refs[0].startswith("evidence-page:")
        ):
            raise ArtifactVerificationRejected(
                "ARTIFACT_LEDGER_EXTRACTION_SOURCE_BINDING_INVALID"
            )
        refs.update(dependency.input_refs)
    return frozenset(refs)


def _task_payload(task: object) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "sequence": task.sequence,
        "title": task.title,
        "purpose": task.purpose,
        "rationale": task.rationale,
        "dependency_ids": task.dependency_ids,
        "input_refs": task.input_refs,
        "input_hash": task.input_hash,
        "skill": {
            "skill_id": task.skill.skill_id,
            "skill_version": task.skill.skill_version,
            "tool_id": task.skill.tool_id,
            "tool_version": task.skill.tool_version,
            "adapter_id": task.skill.adapter_id,
            "adapter_version": task.skill.adapter_version,
        },
        "granted_scopes": sorted(scope.value for scope in task.granted_scopes),
        "capability": {
            "execution_mode": task.capability.execution_mode.value,
            "network_policy": task.capability.network_policy.value,
            "allowed_domains": task.capability.allowed_domains,
            "sandbox_profile": task.capability.sandbox_profile,
            "sandbox_policy_version": task.capability.sandbox_policy_version,
            "sandbox_policy_hash": task.capability.sandbox_policy_hash,
            "reads_case_objects": task.capability.reads_case_objects,
            "writes_managed_derivatives": task.capability.writes_managed_derivatives,
            "external_request_approval_required": task.capability.external_request_approval_required,
        },
        "risk_level": task.risk_level.value,
        "autonomy_level": task.autonomy_level.value,
        "approval_gate": task.approval_gate.value,
        "retry_mode": task.retry_mode.value,
        "budget": {
            "max_attempts": task.budget.max_attempts,
            "timeout_seconds": task.budget.timeout_seconds,
            "max_external_calls": task.budget.max_external_calls,
            "max_cost_minor_units": task.budget.max_cost_minor_units,
            "max_output_bytes": task.budget.max_output_bytes,
        },
    }


def _task_receipts_hash(state: AgentRunState) -> str:
    return _canonical_hash(
        {
            "schema_version": "case-agent-final-task-receipts-v1",
            "run_id": state.run_id,
            "receipts": [
                {
                    "task_id": runtime.spec.task_id,
                    "task_input_hash": runtime.spec.input_hash,
                    "receipt_id": runtime.receipts[-1].receipt_id,
                    "attempt_id": runtime.receipts[-1].attempt_id,
                    "adapter_id": runtime.receipts[-1].adapter_id,
                    "adapter_version": runtime.receipts[-1].adapter_version,
                    "status": runtime.receipts[-1].status.value,
                    "external_submission_state": runtime.receipts[-1].external_submission_state.value,
                    "external_request_id": runtime.receipts[-1].external_request_id,
                    "output_hash": runtime.receipts[-1].output_hash,
                    "artifacts": [
                        {
                            "artifact_id": item.artifact_id,
                            "artifact_kind": item.artifact_kind,
                            "content_hash": item.content_hash,
                            "byte_size": item.byte_size,
                            "source_input_hash": item.source_input_hash,
                            "managed_derivative": item.managed_derivative,
                        }
                        for item in sorted(runtime.receipts[-1].artifacts, key=lambda value: value.artifact_id)
                    ],
                }
                for runtime in sorted(state.tasks, key=lambda value: value.spec.sequence)
            ],
        }
    )


def _artifact_manifest_hash(artifacts: tuple[ArtifactReceipt, ...]) -> str:
    return _canonical_hash(
        {
            "schema_version": "lawyer-agent-artifact-manifest-v1",
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_kind": item.artifact_kind,
                    "content_hash": item.content_hash,
                    "byte_size": item.byte_size,
                    "source_input_hash": item.source_input_hash,
                    "managed_derivative": item.managed_derivative,
                }
                for item in sorted(artifacts, key=lambda value: value.artifact_id)
            ],
        }
    )


def _lineage_payload(item: ArtifactLineageReceipt) -> dict[str, object]:
    return {
        "artifact_id": item.artifact_id,
        "artifact_kind": item.artifact_kind,
        "task_id": item.task_id,
        "content_hash": item.content_hash,
        "byte_size": item.byte_size,
        "source_input_hash": item.source_input_hash,
        "managed_derivative": item.managed_derivative,
        "object_receipt_hash": item.object_receipt_hash,
        "format_verifier_id": item.format_verifier_id,
        "format_verifier_version": item.format_verifier_version,
        "format_verification_hash": item.format_verification_hash,
        "lineage_hash": item.lineage_hash,
    }


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _artifact_verifier_policy_descriptor(
    artifact_kind: str, verifier: ArtifactKindVerifier
) -> Mapping[str, object]:
    descriptor = getattr(verifier, "policy_descriptor", None)
    if not callable(descriptor):
        raise ValueError("artifact verifier must expose its deterministic policy")
    value = descriptor()
    if not isinstance(value, Mapping):
        raise ValueError("artifact verifier policy is invalid")
    normalized = dict(value)
    if (
        normalized.get("artifact_kind") != artifact_kind
        or normalized.get("verifier_id") != verifier.verifier_id
        or normalized.get("verifier_version") != verifier.verifier_version
    ):
        raise ValueError(
            "artifact verifier policy differs from its registry binding"
        )
    try:
        _json_bytes(normalized)
    except (TypeError, ValueError):
        raise ValueError("artifact verifier policy is not canonical JSON") from None
    return normalized


def _public_research_policy_descriptor() -> dict[str, object]:
    """Hash the exact independent contract, not the producer implementation."""

    return {
        "schema_version": "public-research-leads-verification-policy-v1",
        "exact_top_level_fields": sorted(_PUBLIC_RESEARCH_TOP_LEVEL_FIELDS),
        "exact_query_fields": sorted(_PUBLIC_RESEARCH_QUERY_FIELDS),
        "exact_lead_fields": sorted(_PUBLIC_RESEARCH_LEAD_FIELDS),
        "review_status": "NEEDS_LAWYER_REVIEW",
        "review_only_false_fields": [
            "evidence_decision",
            "formal_fact",
            "formal_transaction",
            "legal_conclusion",
            "legal_effect_confirmed",
        ],
        "allowed_provider_ids": [_PUBLIC_RESEARCH_PROVIDER_ID],
        "allowed_purposes": list(_PUBLIC_RESEARCH_PURPOSES),
        "lead_count": {"minimum": 1, "maximum": 20},
        "allowed_authority_classes": list(_PUBLIC_RESEARCH_AUTHORITY_CLASSES),
        "official_authorities": {
            source_id: {
                "authority_class": values[0],
                "publisher": values[1],
                "exact_hosts": list(values[2]),
            }
            for source_id, values in sorted(
                _PUBLIC_RESEARCH_OFFICIAL_AUTHORITIES.items()
            )
        },
        "lead_status": "PUBLIC_RESEARCH_LEAD",
        "allowed_prompt_injection_signals": list(
            _PUBLIC_RESEARCH_PROMPT_SIGNALS
        ),
        "requires_unique_lead_ids_and_urls": True,
        "requires_public_terms_and_query_sha256": True,
        "binds_external_request_id_to_final_task_receipt": True,
        "requires_canonical_credential_free_https_urls": True,
        "rejects_unknown_fields": True,
    }


def _visual_page_policy_descriptor() -> dict[str, object]:
    return {
        "schema_version": "visual-page-review-verification-policy-v1",
        "exact_top_level_fields": sorted(_VISUAL_TOP_LEVEL_FIELDS),
        "exact_provider_fields": sorted(_VISUAL_PROVIDER_FIELDS),
        "exact_provenance_fields": sorted(_VISUAL_PROVENANCE_FIELDS),
        "exact_page_fields": sorted(_VISUAL_PAGE_FIELDS),
        "exact_nested_fields": {
            "region": sorted(_VISUAL_REGION_FIELDS),
            "text_block": sorted(_VISUAL_BLOCK_FIELDS),
            "table": sorted(_VISUAL_TABLE_FIELDS),
            "field": sorted(_VISUAL_FIELD_FIELDS),
            "quality_risk": sorted(_VISUAL_RISK_FIELDS),
        },
        "fixed_provider": {
            "provider_id": "qwen",
            "model_id": "qwen3.5-ocr",
            "provider_version": "1.0.0",
            "processor_region": "cn-beijing",
            "service_id": "qwen-visual-ocr",
            "network_capable": True,
        },
        "allowed_source_kinds": list(_VISUAL_SOURCE_KINDS),
        "allowed_block_kinds": list(_VISUAL_BLOCK_KINDS),
        "allowed_field_kinds": list(_VISUAL_FIELD_KINDS),
        "allowed_quality_codes": list(_VISUAL_QUALITY_CODES),
        "review_status": "NEEDS_LAWYER_REVIEW",
        "review_only_false_fields": [
            "authenticity_confirmed", "evidence_decision", "formal_fact",
            "formal_transaction", "legal_conclusion",
        ],
        "page_count": {"minimum": 1, "maximum": 1},
        "binds_external_request_id_to_final_task_receipt": True,
        "recomputes_binding_projection_request_candidate_and_source_hashes": True,
        "rejects_unknown_fields": True,
    }


def _validate_review_candidate_payload(
    value: Mapping[str, object], *, payload_kind: str
) -> None:
    if (
        not _is_sha256(value.get("task_input_hash"))
        or not _is_sha256(value.get("source_hash"))
        or value.get("review_status") != "NEEDS_LAWYER_REVIEW"
        or value.get("formal_fact") is not False
        or value.get("formal_transaction") is not False
        or value.get("legal_conclusion") is not False
        or value.get("evidence_decision") is not False
    ):
        raise ArtifactVerificationRejected("ARTIFACT_REVIEW_CONTRACT_INVALID")
    if payload_kind == _PUBLIC_RESEARCH_ARTIFACT_KIND:
        _validate_public_research_candidate_payload(value)
        return
    if payload_kind == _VISUAL_ARTIFACT_KIND:
        _validate_visual_page_candidate_payload(value)
        return
    if payload_kind == _CASE_CONTEXT_ARTIFACT_KIND:
        _validate_case_context_candidate_payload(value)
        return
    if payload_kind == CASE_LEDGER_EXTRACTION_ARTIFACT_KIND:
        try:
            parse_case_ledger_extraction_candidate(_json_bytes(value))
        except CaseLedgerExtractionBlocked:
            raise ArtifactVerificationRejected(
                "ARTIFACT_LEDGER_EXTRACTION_CONTRACT_INVALID"
            ) from None
        return
    if payload_kind == LAWYER_DECISION_PACKAGE_ARTIFACT_KIND:
        try:
            parse_lawyer_decision_package_candidate(_json_bytes(value))
        except LawyerAnalysisBlocked:
            raise ArtifactVerificationRejected(
                "ARTIFACT_LAWYER_PACKAGE_CONTRACT_INVALID"
            ) from None
        return
    if payload_kind == LEGAL_RESEARCH_PLAN_ARTIFACT_KIND:
        try:
            parse_legal_research_plan_candidate(_json_bytes(value))
        except LegalResearchPlanBlocked:
            raise ArtifactVerificationRejected(
                "ARTIFACT_LEGAL_RESEARCH_PLAN_CONTRACT_INVALID"
            ) from None
        return
    if payload_kind == "PDF_TEXT_REVIEW_CANDIDATE":
        pages = value.get("pages")
        if not isinstance(pages, list) or not pages or len(pages) > 200:
            raise ArtifactVerificationRejected("ARTIFACT_REVIEW_CONTENT_INVALID")
        seen: set[str] = set()
        for page in pages:
            if not isinstance(page, dict):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_REVIEW_CONTENT_INVALID"
                )
            evidence_page_id = page.get("evidence_page_id")
            extracted_text = page.get("extracted_text")
            input_ref = page.get("input_ref")
            if (
                not _is_uuid(evidence_page_id)
                or evidence_page_id in seen
                or not isinstance(input_ref, str)
                or _CODE_RE.fullmatch(input_ref) is None
                or not _is_sha256(page.get("source_file_sha256"))
                or type(page.get("page_number")) is not int
                or page["page_number"] < 1
                or not isinstance(extracted_text, str)
                or not _is_sha256(page.get("extracted_text_sha256"))
                or sha256(extracted_text.encode("utf-8")).hexdigest()
                != page["extracted_text_sha256"]
            ):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_REVIEW_CONTENT_INVALID"
                )
            seen.add(evidence_page_id)
        return
    if payload_kind == "COMMON_DOCUMENT_REVIEW_CANDIDATE":
        documents = value.get("documents")
        if (
            not isinstance(documents, list)
            or not documents
            or len(documents) > 500
        ):
            raise ArtifactVerificationRejected("ARTIFACT_REVIEW_CONTENT_INVALID")
        seen: set[str] = set()
        for document in documents:
            if not isinstance(document, dict):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_REVIEW_CONTENT_INVALID"
                )
            source_id = document.get("source_object_id")
            input_ref = document.get("input_ref")
            candidates = document.get("candidates")
            if (
                not _is_uuid(source_id)
                or source_id in seen
                or document.get("review_status") != "NEEDS_LAWYER_REVIEW"
                or not isinstance(input_ref, str)
                or _CODE_RE.fullmatch(input_ref) is None
                or not _is_sha256(document.get("source_sha256"))
                or not _is_sha256(document.get("source_reference_hash"))
                or document.get("detected_format") not in {"DOCX", "XLSX"}
                or not isinstance(candidates, list)
            ):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_REVIEW_CONTENT_INVALID"
                )
            for candidate in candidates:
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("review_status")
                    != "NEEDS_LAWYER_REVIEW"
                    or candidate.get("literal_text_only") is not True
                    or not isinstance(candidate.get("text"), str)
                    or not _is_sha256(candidate.get("content_hash"))
                    or sha256(candidate["text"].encode("utf-8")).hexdigest()
                    != candidate["content_hash"]
                ):
                    raise ArtifactVerificationRejected(
                        "ARTIFACT_REVIEW_CONTENT_INVALID"
                    )
            seen.add(source_id)
        return
    raise ArtifactVerificationRejected("ARTIFACT_JSON_SCHEMA_INVALID")


def _validate_public_research_candidate_payload(
    value: Mapping[str, object],
) -> None:
    if (
        set(value) != _PUBLIC_RESEARCH_TOP_LEVEL_FIELDS
        or value.get("schema_version") != _PUBLIC_RESEARCH_SCHEMA
        or value.get("legal_effect_confirmed") is not False
        or value.get("provider_id") != _PUBLIC_RESEARCH_PROVIDER_ID
        or not _is_uuid(value.get("external_request_id"))
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_CONTRACT_INVALID"
        )
    query = value.get("query")
    if not isinstance(query, dict) or set(query) != _PUBLIC_RESEARCH_QUERY_FIELDS:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_QUERY_INVALID"
        )
    public_terms = query.get("public_terms")
    if (
        not _is_uuid(query.get("question_id"))
        or query.get("purpose") not in _PUBLIC_RESEARCH_PURPOSES
        or not _is_sha256(query.get("query_hash"))
        or not isinstance(public_terms, list)
        or not 2 <= len(public_terms) <= 24
        or not _valid_public_terms(public_terms)
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_QUERY_INVALID"
        )
    query_text = " ".join(public_terms)
    if len(query_text) > 240:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_QUERY_INVALID"
        )
    leads = value.get("leads")
    if not isinstance(leads, list) or not 1 <= len(leads) <= 20:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_CONTENT_INVALID"
        )
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for lead in leads:
        if not isinstance(lead, dict) or set(lead) != _PUBLIC_RESEARCH_LEAD_FIELDS:
            raise ArtifactVerificationRejected(
                "ARTIFACT_PUBLIC_RESEARCH_CONTENT_INVALID"
            )
        lead_id = lead.get("lead_id")
        title = lead.get("title")
        snippet = lead.get("snippet")
        url = _validated_public_research_url(lead.get("url"))
        signals = lead.get("prompt_injection_signals")
        if (
            not isinstance(lead_id, str)
            or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$", lead_id)
            is None
            or lead_id in seen_ids
            or url in seen_urls
            or not _bounded_safe_text(title, maximum=500)
            or not _bounded_safe_text(snippet, maximum=4_000, allow_empty=True)
            or lead.get("authority_class")
            not in _PUBLIC_RESEARCH_AUTHORITY_CLASSES
            or lead.get("status") != "PUBLIC_RESEARCH_LEAD"
            or not isinstance(signals, list)
            or len(signals) != len(set(signals))
            or any(
                signal not in _PUBLIC_RESEARCH_PROMPT_SIGNALS
                for signal in signals
            )
            or not _valid_optional_iso_date(lead.get("published_on_candidate"))
        ):
            raise ArtifactVerificationRejected(
                "ARTIFACT_PUBLIC_RESEARCH_CONTENT_INVALID"
            )
        _validate_public_research_authority(lead, hostname=urlsplit(url).hostname)
        seen_ids.add(lead_id)
        seen_urls.add(url)


def _validate_case_context_candidate_payload(
    value: Mapping[str, object],
) -> frozenset[str]:
    if (
        set(value) != _CASE_CONTEXT_TOP_LEVEL_FIELDS
        or value.get("schema_version") != _CASE_CONTEXT_SCHEMA
        or value.get("court_ready") is not False
        or not _bounded_case_context_text(value.get("headline"), 240)
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_CASE_CONTEXT_CONTRACT_INVALID"
        )
    sections = value.get("sections")
    questions = value.get("open_questions")
    summary = value.get("summary_counts")
    if (
        not isinstance(sections, list)
        or not 1 <= len(sections) <= len(_CASE_CONTEXT_SECTION_IDS)
        or not isinstance(questions, list)
        or len(questions) > 500
        or not isinstance(summary, dict)
        or set(summary) != _CASE_CONTEXT_SUMMARY_FIELDS
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
        )
    seen_sections: list[str] = []
    seen_items: set[str] = set()
    cited_refs: set[str] = set()
    total_items = 0
    severity_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for section in sections:
        if (
            not isinstance(section, dict)
            or set(section) != _CASE_CONTEXT_SECTION_FIELDS
            or section.get("section_id") not in _CASE_CONTEXT_SECTION_IDS
            or section["section_id"] in seen_sections
            or not _bounded_case_context_text(section.get("title"), 200)
            or section.get("severity") not in severity_counts
            or not isinstance(section.get("items"), list)
            or not section["items"]
        ):
            raise ArtifactVerificationRejected(
                "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
            )
        seen_sections.append(section["section_id"])
        for item in section["items"]:
            if not isinstance(item, dict):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
                )
            keys = set(item)
            if keys not in {
                _CASE_CONTEXT_ITEM_REQUIRED_FIELDS,
                _CASE_CONTEXT_ITEM_REQUIRED_FIELDS | {"confidence"},
            }:
                raise ArtifactVerificationRejected(
                    "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
                )
            item_id = item.get("item_id")
            refs = item.get("source_refs")
            confidence = item.get("confidence")
            if (
                not isinstance(item_id, str)
                or re.fullmatch(r"ctx-item-[0-9a-f]{24}", item_id) is None
                or item_id in seen_items
                or not _bounded_case_context_text(item.get("title"), 500)
                or not _bounded_case_context_text(
                    item.get("detail"), 4_000, allow_empty=True
                )
                or not _bounded_case_context_text(
                    item.get("review_reason"), 1_000
                )
                or not _valid_case_context_source_refs(refs)
                or (
                    "confidence" in item
                    and (
                        isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not math.isfinite(float(confidence))
                        or not 0 <= float(confidence) <= 1
                    )
                )
            ):
                raise ArtifactVerificationRejected(
                    "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
                )
            seen_items.add(item_id)
            cited_refs.update(refs)
            total_items += 1
            if total_items > 500:
                raise ArtifactVerificationRejected(
                    "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
                )
        severity_counts[section["severity"]] += len(section["items"])
    expected_order = sorted(
        seen_sections, key=lambda item: _CASE_CONTEXT_SECTION_IDS.index(item)
    )
    if seen_sections != expected_order:
        raise ArtifactVerificationRejected(
            "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
        )

    seen_questions: set[str] = set()
    for question in questions:
        if (
            not isinstance(question, dict)
            or set(question) != _CASE_CONTEXT_QUESTION_FIELDS
            or not isinstance(question.get("question_id"), str)
            or re.fullmatch(
                r"ctx-question-[0-9a-f]{24}", question["question_id"]
            )
            is None
            or question["question_id"] in seen_questions
            or not _bounded_case_context_text(question.get("question"), 500)
            or not _valid_case_context_source_refs(question.get("source_refs"))
        ):
            raise ArtifactVerificationRejected(
                "ARTIFACT_CASE_CONTEXT_CONTENT_INVALID"
            )
        seen_questions.add(question["question_id"])
        cited_refs.update(question["source_refs"])

    expected_counts = {
        "total_sources": len(cited_refs),
        "total_items": total_items,
        "high_priority_items": severity_counts["HIGH"],
        "medium_priority_items": severity_counts["MEDIUM"],
        "low_priority_items": severity_counts["LOW"],
        "open_questions": len(questions),
    }
    if (
        any(type(value) is not int or value < 0 for value in summary.values())
        or summary != expected_counts
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_CASE_CONTEXT_SUMMARY_INVALID"
        )
    return frozenset(cited_refs)


def _bounded_case_context_text(
    value: object, maximum: int, *, allow_empty: bool = False
) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (allow_empty or bool(value.strip()))
        and value == value.strip()
        and "\x00" not in value
    )


def _valid_case_context_source_refs(value: object) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 100
        and len(set(value)) == len(value)
        and all(
            isinstance(item, str)
            and _CODE_RE.fullmatch(item) is not None
            for item in value
        )
    )


def _validate_visual_page_candidate_payload(value: Mapping[str, object]) -> None:
    if (
        set(value) != _VISUAL_TOP_LEVEL_FIELDS
        or value.get("schema_version") != _VISUAL_SCHEMA
        or value.get("review_status") != "NEEDS_LAWYER_REVIEW"
        or any(
            value.get(field) is not False
            for field in (
                "formal_fact", "formal_transaction", "legal_conclusion",
                "evidence_decision", "authenticity_confirmed",
            )
        )
        or not _is_sha256(value.get("binding_hash"))
        or not _is_uuid(value.get("external_request_id"))
    ):
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTRACT_INVALID")
    provider = value.get("provider")
    if (
        not isinstance(provider, dict)
        or set(provider) != _VISUAL_PROVIDER_FIELDS
        or provider
        != {
            "provider_id": "qwen",
            "model_id": "qwen3.5-ocr",
            "provider_version": "1.0.0",
            "processor_region": "cn-beijing",
            "service_id": "qwen-visual-ocr",
            "network_capable": True,
        }
    ):
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PROVIDER_INVALID")
    provenance = value.get("provenance")
    if (
        not isinstance(provenance, dict)
        or set(provenance) != _VISUAL_PROVENANCE_FIELDS
        or any(
            not _is_uuid(provenance.get(field))
            for field in (
                "run_id", "task_id", "attempt_id", "firm_id", "matter_id"
            )
        )
        or type(provenance.get("matter_version")) is not int
        or provenance["matter_version"] < 1
        or provenance.get("external_request_id") != value["external_request_id"]
        or provenance.get("processor_region") != "cn-beijing"
        or not _is_sha256(provenance.get("workspace_id_hash"))
        or not isinstance(provenance.get("input_refs"), list)
        or len(provenance["input_refs"]) != 1
    ):
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PROVENANCE_INVALID")
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) != 1:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PAGE_INVALID")
    page = pages[0]
    if not isinstance(page, dict) or set(page) != _VISUAL_PAGE_FIELDS:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PAGE_INVALID")
    if (
        provenance["matter_id"] != page.get("matter_id")
        or provenance["input_refs"] != [page.get("input_ref")]
    ):
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PROVENANCE_INVALID")
    _validate_visual_page(page)
    binding_hash = _canonical_hash(
        {
            "schema_version": "authorized-qwen-visual-ocr-binding-v1",
            "run_id": provenance["run_id"],
            "task_id": provenance["task_id"],
            "attempt_id": provenance["attempt_id"],
            "task_input_hash": value["task_input_hash"],
            "firm_id": provenance["firm_id"],
            "matter_id": provenance["matter_id"],
            "matter_version": provenance["matter_version"],
            "input_refs": provenance["input_refs"],
            "external_request_id": provenance["external_request_id"],
            "provider_id": "qwen",
            "model_id": "qwen3.5-ocr",
            "service_id": "qwen-visual-ocr",
            "processor_region": provenance["processor_region"],
            "workspace_id_hash": provenance["workspace_id_hash"],
            "pages": [
                {
                    **{
                        key: page[key]
                        for key in (
                            "input_ref", "evidence_page_id", "page_number",
                            "source_kind", "source_file_sha256",
                            "source_page_sha256", "rendered_page_sha256",
                            "projection_hash", "width", "height", "media_type",
                        )
                    },
                    "request_hash": page["request_hash"],
                }
            ],
        }
    )
    if value["binding_hash"] != binding_hash:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_BINDING_HASH_INVALID")
    source_hash = _canonical_hash(
        {
            "schema_version": "agent-visual-page-source-set-v1",
            "task_input_hash": value["task_input_hash"],
            "binding_hash": value["binding_hash"],
            "external_request_id": value["external_request_id"],
            "pages": [
                {
                    key: page[key]
                    for key in (
                        "input_ref", "evidence_page_id", "page_number",
                        "source_kind", "source_file_sha256",
                        "source_page_sha256", "rendered_page_sha256",
                        "projection_hash", "width", "height", "media_type",
                    )
                }
            ],
        }
    )
    if value.get("source_hash") != source_hash:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_SOURCE_HASH_INVALID")


def _validate_visual_page(page: Mapping[str, object]) -> None:
    if (
        not isinstance(page.get("input_ref"), str)
        or re.fullmatch(r"evidence-page:[0-9a-f-]{36}", page["input_ref"])
        is None
        or not _is_uuid(page.get("matter_id"))
        or not _is_uuid(page.get("evidence_page_id"))
        or page["input_ref"] != f"evidence-page:{page['evidence_page_id']}"
        or type(page.get("page_number")) is not int
        or page["page_number"] < 1
        or page.get("source_kind") not in _VISUAL_SOURCE_KINDS
        or page.get("parser_id") != "deterministic_visual_page_normalizer"
        or page.get("parser_version") != "1.0.0"
        or type(page.get("orientation_applied")) is not int
        or page["orientation_applied"] not in range(1, 9)
        or page.get("source_format")
        not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP", "HEIF", "HEIC"}
        or not isinstance(page.get("had_transparency"), bool)
        or not all(
            _is_sha256(page.get(field))
            for field in (
                "source_file_sha256", "source_page_sha256",
                "rendered_page_sha256", "projection_hash", "request_hash",
                "provider_request_ref_hash", "candidate_hash",
            )
        )
        or type(page.get("width")) is not int
        or type(page.get("height")) is not int
        or not 1 <= page["width"] <= 20_000
        or not 1 <= page["height"] <= 20_000
        or page["width"] * page["height"] > 100_000_000
        or page.get("media_type") != "image/png"
        or page.get("provider_id") != "qwen"
        or page.get("model_id") != "qwen3.5-ocr"
        or page.get("review_status") != "NEEDS_LAWYER_REVIEW"
    ):
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PAGE_INVALID")
    request_hash = _canonical_hash(
        {
            "schema_version": "visual-page-model-input-v1",
            "projection_hash": page["projection_hash"],
            "rendered_page_sha256": page["rendered_page_sha256"],
            "width": page["width"],
            "height": page["height"],
        }
    )
    if page["request_hash"] != request_hash:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_REQUEST_HASH_INVALID")
    projection_hash = _canonical_hash(
        {
            "schema_version": "visual-page-projection-v1",
            "matter_id": page["matter_id"],
            "evidence_page_id": page["evidence_page_id"],
            "page_number": page["page_number"],
            "source_kind": page["source_kind"],
            "source_file_sha256": page["source_file_sha256"],
            "source_page_sha256": page["source_page_sha256"],
            "rendered_page_sha256": page["rendered_page_sha256"],
            "width": page["width"],
            "height": page["height"],
            "media_type": page["media_type"],
            "parser_id": page["parser_id"],
            "parser_version": page["parser_version"],
            "orientation_applied": page["orientation_applied"],
            "source_format": page["source_format"],
            "had_transparency": page["had_transparency"],
        }
    )
    if page["projection_hash"] != projection_hash:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_PROJECTION_HASH_INVALID")
    blocks = _visual_collection(
        page.get("text_blocks"), maximum=2_000, exact=_VISUAL_BLOCK_FIELDS,
        id_field="block_id", allowed_kinds=_VISUAL_BLOCK_KINDS,
    )
    tables = _validate_visual_tables(page.get("tables"))
    fields = _visual_collection(
        page.get("fields"), maximum=2_000, exact=_VISUAL_FIELD_FIELDS,
        id_field="field_id", allowed_kinds=_VISUAL_FIELD_KINDS,
    )
    risks = _validate_visual_risks(page.get("quality_risks"))
    candidate_hash = _canonical_hash(
        {
            "schema_version": "visual-page-candidate-v1",
            **{
                key: page[key]
                for key in (
                    "matter_id", "evidence_page_id", "source_file_sha256",
                    "source_page_sha256", "rendered_page_sha256",
                    "projection_hash", "provider_id", "model_id",
                    "provider_request_ref_hash",
                )
            },
            "text_blocks": [_visual_hash_block(item) for item in blocks],
            "tables": [_visual_hash_table(item) for item in tables],
            "fields": [_visual_hash_field(item) for item in fields],
            "quality_risks": [_visual_hash_risk(item) for item in risks],
            "status": "NEEDS_REVIEW",
        }
    )
    if page["candidate_hash"] != candidate_hash:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CANDIDATE_HASH_INVALID")


def _visual_collection(
    value: object,
    *,
    maximum: int,
    exact: frozenset[str],
    id_field: str,
    allowed_kinds: tuple[str, ...],
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != exact
            or not isinstance(item.get(id_field), str)
            or _CODE_RE.fullmatch(item[id_field]) is None
            or item[id_field] in seen
            or item.get("kind") not in allowed_kinds
            or not _visual_region(item.get("region"))
            or not _visual_confidence(item.get("confidence"))
        ):
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        text_key = "text" if id_field == "block_id" else "value"
        text = item.get(text_key)
        if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        if id_field == "field_id":
            currency = item.get("currency")
            if (
                currency is not None
                and (
                    item.get("kind") != "AMOUNT"
                    or not isinstance(currency, str)
                    or re.fullmatch(r"[A-Z]{3}", currency) is None
                )
            ):
                raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        seen.add(item[id_field])
        result.append(dict(item))
    return result


def _validate_visual_tables(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 200:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _VISUAL_TABLE_FIELDS:
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        rows, columns, cells = (
            item.get("row_count"), item.get("column_count"), item.get("cells")
        )
        if (
            not isinstance(item.get("table_id"), str)
            or _CODE_RE.fullmatch(item["table_id"]) is None
            or item["table_id"] in seen
            or type(rows) is not int or not 1 <= rows <= 2_000
            or type(columns) is not int or not 1 <= columns <= 100
            or not isinstance(cells, list) or len(cells) != rows
            or any(
                not isinstance(row, list) or len(row) != columns
                or any(not isinstance(cell, str) or len(cell) > 4_000 for cell in row)
                for row in cells
            )
            or not _visual_region(item.get("region"))
            or not _visual_confidence(item.get("confidence"))
        ):
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        seen.add(item["table_id"])
        result.append(dict(item))
    return result


def _validate_visual_risks(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 100:
        raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _VISUAL_RISK_FIELDS:
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        note = item.get("note")
        identity = (str(item.get("code")), str(note).casefold())
        if (
            item.get("code") not in _VISUAL_QUALITY_CODES
            or item.get("severity") not in {"LOW", "MEDIUM", "HIGH"}
            or not isinstance(note, str) or not note.strip() or len(note) > 1_000
            or identity in seen
            or (item.get("region") is not None and not _visual_region(item.get("region")))
            or not _visual_confidence(item.get("confidence"))
        ):
            raise ArtifactVerificationRejected("ARTIFACT_VISUAL_CONTENT_INVALID")
        seen.add(identity)
        result.append(dict(item))
    return result


def _visual_region(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _VISUAL_REGION_FIELDS:
        return False
    numbers = tuple(value.get(key) for key in ("x", "y", "width", "height"))
    if any(
        type(item) not in {int, float} or not math.isfinite(float(item))
        for item in numbers
    ):
        return False
    x, y, width, height = (float(item) for item in numbers)
    return bool(
        0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1
        and x + width <= 1.000000001 and y + height <= 1.000000001
    )


def _visual_confidence(value: object) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(float(value))
        and 0 <= float(value) <= 1
    )


def _visual_hash_region(value: Mapping[str, object]) -> dict[str, str]:
    return {
        key: format(float(value[key]), ".8f")
        for key in ("x", "y", "width", "height")
    }


def _visual_hash_block(value: Mapping[str, object]) -> dict[str, object]:
    return {
        **{key: value[key] for key in ("block_id", "kind", "text")},
        "region": _visual_hash_region(value["region"]),
        "confidence": format(float(value["confidence"]), ".8f"),
    }


def _visual_hash_table(value: Mapping[str, object]) -> dict[str, object]:
    return {
        **{key: value[key] for key in ("table_id", "row_count", "column_count")},
        "region": _visual_hash_region(value["region"]),
        "cells": value["cells"],
        "confidence": format(float(value["confidence"]), ".8f"),
    }


def _visual_hash_field(value: Mapping[str, object]) -> dict[str, object]:
    return {
        **{key: value[key] for key in ("field_id", "kind", "value", "currency")},
        "region": _visual_hash_region(value["region"]),
        "confidence": format(float(value["confidence"]), ".8f"),
    }


def _visual_hash_risk(value: Mapping[str, object]) -> dict[str, object]:
    return {
        **{key: value[key] for key in ("code", "severity", "note")},
        "region": (
            _visual_hash_region(value["region"])
            if value["region"] is not None else None
        ),
        "confidence": format(float(value["confidence"]), ".8f"),
    }


def _valid_public_terms(values: list[object]) -> bool:
    identities: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or value != " ".join(value.split())
            or not 2 <= len(value) <= 60
            or any(ord(char) < 32 for char in value)
            or _contains_private_public_query_data(value)
        ):
            return False
        identity = value.casefold()
        if identity in identities:
            return False
        identities.add(identity)
    return True


def _contains_private_public_query_data(value: str) -> bool:
    patterns = (
        r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        r"(?<!\d)(?:0\d{2,3}[- ]?)?\d{7,8}(?!\d)",
        r"(?<!\d)\d{12,24}(?!\d)",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        r"[（(]\d{4}[）)][\u4e00-\u9fffA-Za-z0-9]{2,30}号",
        r"(?:原告|被告|上诉人|被上诉人|申请人|被申请人|借款人|出借人|当事人|联系人|姓名)\s*[：:]?\s*[\u4e00-\u9fff]{2,4}",
        r"(?:身份证|手机号|电话|微信号|微信昵称|银行卡|银行账号|账户|账号)\s*[：:]?",
    )
    if any(re.search(pattern, value) for pattern in patterns):
        return True
    if any(marker in value for marker in ('"', "'", "“", "”", "《原文》")) and len(value) > 30:
        return True
    return len(value) >= 16 and any(
        marker in value for marker in ("，", "。", "！", "？", "；", "\n", "\r")
    )


def _validated_public_research_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 9 <= len(value) <= 4_096
        or any(char.isspace() or ord(char) < 32 for char in value)
        or "\\" in value
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        ) from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname != hostname.casefold()
        or re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
            hostname,
        )
        is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not (parsed.path or "/").startswith("/")
        or (parsed.path or "/").startswith("//")
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        )
    canonical_url = f"https://{hostname}{parsed.path or '/'}"
    if parsed.query:
        canonical_url += f"?{parsed.query}"
    if value != canonical_url:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        )
    decoded_query = unquote(parsed.query)
    blocked_keys = {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        ) from None
    if (
        "@" in decoded_query
        or _contains_private_public_query_data(decoded_query)
        or any(key.casefold() in blocked_keys for key, _ in query_pairs)
        or any(_contains_private_public_query_data(item) for _, item in query_pairs)
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_URL_INVALID"
        )
    return value


def _validate_public_research_authority(
    lead: Mapping[str, object], *, hostname: str | None
) -> None:
    source_id = lead.get("official_source_id")
    publisher = lead.get("publisher")
    official_domain = lead.get("official_domain")
    authority_class = lead.get("authority_class")
    if type(official_domain) is not bool:
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_AUTHORITY_INVALID"
        )
    if not official_domain:
        if (
            authority_class != "NON_OFFICIAL"
            or source_id is not None
            or publisher is not None
            or hostname in _PUBLIC_RESEARCH_OFFICIAL_HOSTS
        ):
            raise ArtifactVerificationRejected(
                "ARTIFACT_PUBLIC_RESEARCH_AUTHORITY_INVALID"
            )
        return
    registered = _PUBLIC_RESEARCH_OFFICIAL_AUTHORITIES.get(str(source_id))
    if (
        registered is None
        or authority_class != registered[0]
        or publisher != registered[1]
        or hostname not in registered[2]
    ):
        raise ArtifactVerificationRejected(
            "ARTIFACT_PUBLIC_RESEARCH_AUTHORITY_INVALID"
        )


def _bounded_safe_text(
    value: object, *, maximum: int, allow_empty: bool = False
) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and len(value) <= maximum
        and (allow_empty or bool(value))
        and not any(
            ord(char) < 32 and char not in "\n\r\t" for char in value
        )
    )


def _valid_optional_iso_date(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return isinstance(value, str)


def _reject_duplicate_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _time_text(value: datetime) -> str:
    _aware(value, "time")
    return value.astimezone(timezone.utc).isoformat()


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{label} is invalid") from None


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _code(value: str, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _safe_code(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"^[A-Z][A-Z0-9_]{2,79}$", value) is None:
        raise ValueError(f"{label} is invalid")


def _semver(value: str, label: str) -> None:
    if not isinstance(value, str) or _SEMVER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


FIRST_RELEASE_VERIFIER_POLICY_HASH = first_release_verifier_policy_hash()


__all__ = [
    "ArtifactFormatReceipt",
    "ArtifactKindVerifier",
    "ArtifactLineageReceipt",
    "ArtifactVerificationRejected",
    "FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS",
    "FIRST_RELEASE_VERIFIER_ID",
    "FIRST_RELEASE_VERIFIER_POLICY_HASH",
    "FIRST_RELEASE_VERIFIER_VERSION",
    "CanonicalJsonArtifactVerifier",
    "CaseAgentRunVerifier",
    "CaseAgentVerificationIndeterminate",
    "ManagedArtifactAccessPort",
    "ManagedArtifactRead",
    "OpenXmlArtifactVerifier",
    "ReviewableDocumentCandidateArtifactVerifier",
    "ReviewablePdfArtifactVerifier",
    "build_first_release_case_agent_run_verifier",
    "first_release_review_candidate_verifiers",
    "first_release_verifier_policy_hash",
    "RunVerificationReceipt",
    "VerificationOutcome",
]
