"""Execute one claimed official-source capture without sending case material.

The PostgreSQL lease is the lawyer-authorized network boundary.  This
coordinator reconstructs only a fixed public query, verifies its hash against
the lease, performs a credential-free HTTPS capture, encrypts the exact bytes,
runs a deterministic source-specific parser, and records either
REVIEW_REQUIRED or a bounded failure code.  It never registers a legal source
or approves a rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .legal_provision_parser import (
    LegalProvisionParseBlocked,
    ParsedLegalProvisionSnapshot,
    parse_civil_code_borrowing_provisions,
    parse_private_lending_2015_original,
    parse_private_lending_first_revision,
    parse_private_lending_second_revision,
)
from .lpr_source_parser import LprSourceParseBlocked, ParsedLprSnapshot, parse_captured_lpr_snapshot
from .managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked
from .models import Actor, Role
from .official_source_capture import (
    DirectHttpsOfficialSourceTransport,
    OfficialSourceCaptureBlocked,
    OfficialSourceTransport,
    capture_authorized_official_source,
)
from .official_source_capture_postgres import OfficialSourceCaptureRunLease
from .research_gateway import PublicResearchGateway, ResearchBlocked


_LOGGER = logging.getLogger(__name__)


class OfficialSourceCaptureCoordinationBlocked(ValueError):
    """The claimed run cannot be executed within its authorized boundary."""


class OfficialSourceCapturePersistencePort(Protocol):
    def complete_capture(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def fail_capture(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class OfficialSourceCaptureCoordinationResult:
    run_id: str
    matter_id: str
    status: str
    final_matter_version: int
    parser_kind: str | None
    content_sha256: str | None
    parsed_output_hash: str | None
    failure_code: str | None
    receipt: CaseLedgerCommandReceipt


_RESEARCH_INPUTS: dict[str, tuple[str, str]] = {
    "CN-CIVIL-CODE-680": (
        "民法典借款利息",
        "中华人民共和国民法典 第六百七十九条 第六百八十条",
    ),
    "SPC-PRIVATE-LENDING-2020-SECOND-REVISION": (
        "民间借贷利率保护与过渡规则",
        "民间借贷司法解释 2020年第二次修正 第二十四条至第三十一条 利率保护 过渡规则",
    ),
    "SPC-PRIVATE-LENDING-2020-FIRST-REVISION": (
        "民间借贷2020年第一次修正历史文本",
        "民间借贷司法解释 2020年第一次修正 第二十六条 第三十二条",
    ),
    "SPC-PRIVATE-LENDING-2015-ORIGINAL": (
        "民间借贷2015年司法解释已付利息",
        "法释2015 18号 第二十六条 第三十一条",
    ),
    "CFETS-LPR-HISTORY": (
        "LPR",
        "一年期贷款市场报价利率 历史数据",
    ),
}


def execute_claimed_official_source_capture(
    *,
    lease: OfficialSourceCaptureRunLease,
    case_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore,
    persistence: OfficialSourceCapturePersistencePort,
    system_actor: Actor,
    transport: OfficialSourceTransport | None = None,
) -> OfficialSourceCaptureCoordinationResult:
    """Execute exactly one claimed run and persist a terminal review/failure state."""

    _validate_worker_boundary(lease=lease, case_root=case_root, system_actor=system_actor)
    try:
        issue, query = _research_input(lease.source_id)
        query_hash = sha256(query.encode("utf-8")).hexdigest()
        if query_hash != lease.query_sha256:
            raise OfficialSourceCaptureCoordinationBlocked(
                "claimed query hash differs from the registered minimized query"
            )
        gateway = PublicResearchGateway()
        plan = gateway.prepare_plan(issue=issue, proposed_query=query)
        request = gateway.authorize_public_request(
            plan_id=plan.plan_id,
            source_id=lease.source_id,
            target_url=lease.target_url,
            requested_by=lease.authorized_by,
            lawyer_confirmed=True,
        )
        if request.query_hash != lease.query_sha256:
            raise OfficialSourceCaptureCoordinationBlocked(
                "research gateway query hash differs from the claimed run"
            )
        capture = capture_authorized_official_source(
            request=request,
            gateway=gateway,
            artifact_store=artifact_store,
            case_root=str(case_root),
            transport=transport or DirectHttpsOfficialSourceTransport(),
            max_bytes=lease.max_response_bytes,
        )
        parser_kind, parsed_output_hash, parsed_summary = _parse_capture(
            source_id=lease.source_id,
            capture=capture,
            artifact_store=artifact_store,
        )
        capture_verification_hash = _persistent_verification_hash(
            lease=lease,
            capture_verification_hash=capture.verification_hash,
            content_sha256=capture.content_sha256,
            parser_kind=parser_kind,
            parsed_output_hash=parsed_output_hash,
        )
    except (
        OfficialSourceCaptureCoordinationBlocked,
        OfficialSourceCaptureBlocked,
        LegalProvisionParseBlocked,
        LprSourceParseBlocked,
        ManagedArtifactBlocked,
        ResearchBlocked,
    ) as error:
        failure_code = _failure_code(error)
        # Keep the database and browser on a stable, non-sensitive failure
        # code. The worker log retains only the bounded implementation reason
        # (never source bytes, case text, credentials or request payload), so
        # an operator can distinguish transport, storage and parser failures.
        _LOGGER.warning(
            "official source capture failed run=%s source=%s code=%s reason=%s",
            lease.run_id,
            lease.source_id,
            failure_code,
            str(error),
        )
        receipt = persistence.fail_capture(
            matter_id=lease.matter_id,
            run_id=lease.run_id,
            lease_id=lease.lease_id,
            actor=system_actor,
            expected_version=lease.matter_version,
            idempotency_key=f"official-source:{lease.run_id}:fail:{failure_code.lower()}",
            failure_code=failure_code,
        )
        return OfficialSourceCaptureCoordinationResult(
            run_id=lease.run_id,
            matter_id=lease.matter_id,
            status="FAILED",
            final_matter_version=receipt.matter_version,
            parser_kind=None,
            content_sha256=None,
            parsed_output_hash=None,
            failure_code=failure_code,
            receipt=receipt,
        )

    receipt = persistence.complete_capture(
        matter_id=lease.matter_id,
        run_id=lease.run_id,
        lease_id=lease.lease_id,
        actor=system_actor,
        expected_version=lease.matter_version,
        idempotency_key=f"official-source:{lease.run_id}:complete",
        final_url=capture.final_url,
        retrieved_at=capture.retrieved_at,
        peer_ip=capture.peer_ip,
        content_media_type=capture.media_type,
        content_sha256=capture.content_sha256,
        content_bytes=capture.content_bytes,
        storage_object_key=capture.encrypted_object.object_key,
        capture_verification_hash=capture_verification_hash,
        parser_kind=parser_kind,
        parsed_output_hash=parsed_output_hash,
        parsed_summary=parsed_summary,
    )
    return OfficialSourceCaptureCoordinationResult(
        run_id=lease.run_id,
        matter_id=lease.matter_id,
        status="REVIEW_REQUIRED",
        final_matter_version=receipt.matter_version,
        parser_kind=parser_kind,
        content_sha256=capture.content_sha256,
        parsed_output_hash=parsed_output_hash,
        failure_code=None,
        receipt=receipt,
    )


def _validate_worker_boundary(
    *,
    lease: OfficialSourceCaptureRunLease,
    case_root: str | Path,
    system_actor: Actor,
) -> None:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise OfficialSourceCaptureCoordinationBlocked(
            "official source capture requires a dedicated SYSTEM_WORKER identity"
        )
    if system_actor.firm_id.strip() == "" or lease.matter_version < 1:
        raise OfficialSourceCaptureCoordinationBlocked("official source capture lease identity is invalid")
    root = Path(case_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise OfficialSourceCaptureCoordinationBlocked("selected case root must be an existing directory")
    if lease.authorized_at.tzinfo is None:
        raise OfficialSourceCaptureCoordinationBlocked("lawyer authorization time must include timezone")
    if len(lease.authorization_hash) != 64:
        raise OfficialSourceCaptureCoordinationBlocked("lawyer authorization binding is missing")


def _research_input(source_id: str) -> tuple[str, str]:
    try:
        return _RESEARCH_INPUTS[source_id]
    except KeyError as error:
        raise OfficialSourceCaptureCoordinationBlocked(
            "claimed source has no fixed public research input"
        ) from error


def _parse_capture(*, source_id: str, capture, artifact_store: LocalEncryptedArtifactStore):
    if source_id == "CFETS-LPR-HISTORY":
        parsed = parse_captured_lpr_snapshot(capture=capture, artifact_store=artifact_store)
        parser_kind = "CFETS_LPR_JSON" if capture.media_type == "application/json" else "CFETS_LPR_ANNOUNCEMENT"
        return parser_kind, parsed.parsed_output_hash, _lpr_summary(parsed)
    parsers = {
        "CN-CIVIL-CODE-680": ("CIVIL_CODE_BORROWING", parse_civil_code_borrowing_provisions),
        "SPC-PRIVATE-LENDING-2020-SECOND-REVISION": (
            "PRIVATE_LENDING_SECOND_REVISION",
            parse_private_lending_second_revision,
        ),
        "SPC-PRIVATE-LENDING-2020-FIRST-REVISION": (
            "PRIVATE_LENDING_FIRST_REVISION",
            parse_private_lending_first_revision,
        ),
        "SPC-PRIVATE-LENDING-2015-ORIGINAL": (
            "PRIVATE_LENDING_2015_ORIGINAL",
            parse_private_lending_2015_original,
        ),
    }
    try:
        parser_kind, parser = parsers[source_id]
    except KeyError as error:
        raise OfficialSourceCaptureCoordinationBlocked("claimed source has no deterministic parser") from error
    parsed = parser(capture=capture, artifact_store=artifact_store)
    return parser_kind, parsed.parsed_output_hash, _legal_summary(parsed)


def _legal_summary(parsed: ParsedLegalProvisionSnapshot) -> dict[str, Any]:
    return {
        "document_title": parsed.document_title,
        "version_label": parsed.version_label,
        "provisions": [
            {
                "provision_key": item.provision_key,
                "provision_label": item.provision_label,
                "semantic_sha256": item.semantic_sha256,
                "source_locator": item.source_locator,
                "required_markers": list(item.required_markers),
            }
            for item in parsed.provisions
        ],
        "review_status": parsed.review_status,
    }


def _lpr_summary(parsed: ParsedLprSnapshot) -> dict[str, Any]:
    return {
        "observations": [
            {
                "publication_date": item.publication_date.isoformat(),
                "effective_from": item.effective_from.isoformat(),
                "effective_until": item.effective_until.isoformat() if item.effective_until else None,
                "one_year_rate": format(item.one_year_rate, "f"),
                "five_year_plus_rate": format(item.five_year_plus_rate, "f"),
                "source_locator": item.source_locator,
            }
            for item in parsed.observations
        ],
        "review_status": parsed.review_status,
    }


def _persistent_verification_hash(
    *,
    lease: OfficialSourceCaptureRunLease,
    capture_verification_hash: str,
    content_sha256: str,
    parser_kind: str,
    parsed_output_hash: str,
) -> str:
    payload = {
        "schema_version": "persistent-official-source-capture-v1",
        "run_id": lease.run_id,
        "matter_id": lease.matter_id,
        "source_id": lease.source_id,
        "target_url": lease.target_url,
        "query_sha256": lease.query_sha256,
        "authorization_hash": lease.authorization_hash,
        "authorized_by": lease.authorized_by,
        "authorized_at": lease.authorized_at.isoformat(),
        "capture_verification_hash": capture_verification_hash,
        "content_sha256": content_sha256,
        "parser_kind": parser_kind,
        "parsed_output_hash": parsed_output_hash,
        "review_status": "HUMAN_REVIEW_REQUIRED",
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _failure_code(error: Exception) -> str:
    if isinstance(error, OfficialSourceCaptureBlocked):
        return "OFFICIAL_FETCH_BLOCKED"
    if isinstance(error, LegalProvisionParseBlocked):
        return "LEGAL_PROVISION_PARSE_BLOCKED"
    if isinstance(error, LprSourceParseBlocked):
        return "LPR_PARSE_BLOCKED"
    if isinstance(error, ManagedArtifactBlocked):
        return "ENCRYPTED_STORE_BLOCKED"
    if isinstance(error, ResearchBlocked):
        return "RESEARCH_POLICY_BLOCKED"
    return "CAPTURE_INPUT_BLOCKED"
