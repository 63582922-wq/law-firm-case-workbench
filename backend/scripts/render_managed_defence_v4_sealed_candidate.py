#!/usr/bin/env python3
"""Render one ephemeral lawyer-review document from the sealed V4 response.

This deliberately narrow acceptance command proves a single controlled chain:
the already-recorded V4 model response is reinterpreted by the current strict
policy, bound to current authoritative sources, compiled into a review-only
defence-statement candidate, and rendered by the isolated Office service.

It accepts no arguments and never creates an Agent run, submits a provider
request, persists a work plan or document, changes a case record, or marks a
document court-ready.  The only outputs are three ephemeral files in this
Worker's private /tmp directory: DOCX, PDF, and a non-sensitive receipt.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_api.case_agent_worker_entrypoint import (  # noqa: E402
    CaseAgentWorkerProcessSettings,
)
from case_kernel.case_agent_case_context_postgres import (  # noqa: E402
    PostgresCaseContextProjectionPort,
)
from case_kernel.case_agent_document_delivery import (  # noqa: E402
    AuthoritativeDocumentSource,
    DocumentSourceKind,
    DynamicDocumentTaskBinding,
    build_deterministic_defence_statement_candidate,
    canonical_document_candidate_bytes,
    first_release_reviewable_document_templates,
    visible_document_source_labels,
)
from case_kernel.case_agent_lawyer_analysis import (  # noqa: E402
    build_lawyer_analysis_contract,
    compile_lawyer_decision_package_candidate,
    parse_lawyer_analysis_provider_response,
    parse_lawyer_decision_package_candidate,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (  # noqa: E402
    LAWYER_ANALYSIS_TOOL_ID,
)
from case_kernel.case_work_plan import (  # noqa: E402
    CaseWorkPlanItem,
    DeliveryTarget,
    ReviewGate,
    WorkPlanItemKind,
    WorkPlanReadiness,
    WorkPlanReference,
    WorkPlanReferenceUse,
    WorkPlanSourceType,
)
from case_kernel.isolated_document_renderer import (  # noqa: E402
    IsolatedDocumentRendererClient,
)
from case_kernel.legal_provision_parser import (  # noqa: E402
    LegalProvisionDocumentProjectionBlocked,
    project_registered_legal_source_for_document,
)
from case_kernel.official_source_private_store import (  # noqa: E402
    OfficialSourceObjectStoreBlocked,
    S3OfficialSourcePrivateObjectStore,
    S3VerifiedOfficialSourceTextPort,
)
from case_kernel.reviewable_draft_worker import (  # noqa: E402
    create_reviewable_docx_draft,
)
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore  # noqa: E402

import replay_managed_defence_v4_sealed_response as _v4  # noqa: E402


_sealed = _v4._sealed


class SealedDocumentReplayBlocked(RuntimeError):
    """The fixed replay boundary or current source projection is unsafe."""


def _sealed_label() -> str:
    label = getattr(_sealed, "_SEALED_LABEL", None)
    if not isinstance(label, str) or label not in {"v4", "m2"}:
        raise SealedDocumentReplayBlocked("封存响应标签不受支持。")
    return label.upper()


def _sealed_tag() -> str:
    return _sealed_label().lower()


def _output_layout() -> tuple[Path, str, str, str]:
    tag = _sealed_tag()
    return (
        Path(f"/tmp/lawcase-sealed-{tag}-document-replay"),
        f"managed-defence-{tag}-review-candidate.docx",
        f"managed-defence-{tag}-review-candidate.pdf",
        f"managed-defence-{tag}-review-candidate-receipt.json",
    )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def _json_hash(value: object) -> str:
    return sha256(_json_text(value).encode("utf-8")).hexdigest()


def _exactly_one(
    rows: Sequence[Mapping[str, object]], *, label: str
) -> dict[str, object]:
    if len(rows) != 1:
        raise SealedDocumentReplayBlocked(f"{label} 必须唯一且当前可用。")
    return dict(rows[0])


def _read_current_authoritative_rows(
    *, settings: CaseAgentWorkerProcessSettings, matter_id: str
) -> tuple[
    dict[str, object],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    """Read only the source set required by the released defence compiler."""

    worker = settings.runtime.actor
    with _sealed._read_only_connection(
        dsn=settings.runtime.postgres_dsn,
        actor_id=worker.actor_id,
        firm_id=worker.firm_id,
    ) as connection:
        posture = _exactly_one(
            connection.execute(
                """
                SELECT profile.profile_id::text AS profile_id,
                       profile.profile_version, profile.profile_hash,
                       profile.case_type_code, profile.procedure_stage,
                       profile.represented_position, profile.authority_scope_code,
                       profile.engagement_state, party.display_label
                  FROM case_posture_profile_heads head
                  JOIN case_posture_profiles profile
                    ON profile.profile_id = head.current_profile_id
                   AND profile.firm_id = head.firm_id
                   AND profile.matter_id = head.matter_id
                  JOIN case_party_versions party
                    ON party.party_version_id = profile.represented_party_version_id
                   AND party.firm_id = profile.firm_id
                   AND party.matter_id = profile.matter_id
                 WHERE head.matter_id = %s::uuid
                   AND head.firm_id = %s::uuid
                   AND profile.status = 'CONFIRMED'
                   AND profile.engagement_state = 'ACTIVE'
                """,
                (matter_id, worker.firm_id),
            ).fetchall(),
            label="当前代理身份",
        )
        facts = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT fact_id::text AS fact_id, decision_hash, original_text
                  FROM case_facts
                 WHERE matter_id = %s::uuid
                   AND firm_id = %s::uuid
                   AND status = 'CONFIRMED'
                 ORDER BY fact_id
                """,
                (matter_id, worker.firm_id),
            ).fetchall()
        )
        claims = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT claim.claim_id::text AS claim_id,
                       claim.confirmation_hash,
                       claim.original_claim_text,
                       claim.claimed_amount,
                       claim.currency,
                       response.position,
                       response.partial_amount,
                       response.currency AS partial_currency
                  FROM case_claims claim
                  LEFT JOIN case_claim_responses response
                    ON response.claim_id = claim.claim_id
                   AND response.firm_id = claim.firm_id
                   AND response.matter_id = claim.matter_id
                 WHERE claim.matter_id = %s::uuid
                   AND claim.firm_id = %s::uuid
                   AND claim.status = 'CONFIRMED_SCOPE'
                 ORDER BY claim.claim_id
                """,
                (matter_id, worker.firm_id),
            ).fetchall()
        )
        legal_sources = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT ON (source.snapshot_id)
                       source.snapshot_id::text AS snapshot_id,
                       source.source_id,
                       source.publisher,
                       source.authority_level,
                       source.official_url,
                       source.provision_locator,
                       source.content_sha256,
                       source.content_media_type,
                       source.storage_object_key
                  FROM case_legal_bundles bundle
                  JOIN case_legal_bundle_segments segment
                    ON segment.bundle_id = bundle.bundle_id
                   AND segment.firm_id = bundle.firm_id
                   AND segment.matter_id = bundle.matter_id
                  JOIN official_legal_source_snapshots source
                    ON source.snapshot_id = segment.source_snapshot_id
                   AND source.firm_id = segment.firm_id
                   AND source.content_sha256 = segment.source_sha256
                 WHERE bundle.matter_id = %s::uuid
                   AND bundle.firm_id = %s::uuid
                   AND bundle.status = 'APPROVED'
                   AND source.verification_status = 'VERIFIED'
                   AND source.license_status = 'ACTIVE'
                 ORDER BY source.snapshot_id, segment.start_date, segment.segment_id
                """,
                (matter_id, worker.firm_id),
            ).fetchall()
        )
        legal_rules = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT ON (rule.rule_version_id)
                       rule.rule_version_id::text AS rule_version_id,
                       rule.rule_id,
                       rule.rule_version,
                       rule.issue_key,
                       rule.effective_from,
                       rule.effective_to,
                       rule.trigger_event_kind,
                       rule.formula_kind,
                       rule.base_annual_rate,
                       rule.rate_multiplier,
                       rule.derived_annual_rate,
                       rule.required_fact_keys,
                       rule.transition_rule_versions,
                       rule.conflict_set,
                       rule.priority,
                       rule.approval_hash
                  FROM case_legal_bundles bundle
                  JOIN case_legal_bundle_segments segment
                    ON segment.bundle_id = bundle.bundle_id
                   AND segment.firm_id = bundle.firm_id
                   AND segment.matter_id = bundle.matter_id
                  JOIN legal_rule_versions rule
                    ON rule.rule_version_id = segment.rule_version_id
                   AND rule.firm_id = segment.firm_id
                 WHERE bundle.matter_id = %s::uuid
                   AND bundle.firm_id = %s::uuid
                   AND bundle.status = 'APPROVED'
                   AND rule.status = 'APPROVED'
                 ORDER BY rule.rule_version_id, segment.start_date, segment.segment_id
                """,
                (matter_id, worker.firm_id),
            ).fetchall()
        )
    if not facts:
        raise SealedDocumentReplayBlocked("当前案件没有可绑定的已确认事实。")
    if not claims:
        raise SealedDocumentReplayBlocked("当前案件没有可绑定的已确认诉请范围。")
    if not legal_sources:
        raise SealedDocumentReplayBlocked("当前案件没有可绑定的已核验法源。")
    if not legal_rules:
        raise SealedDocumentReplayBlocked("当前案件没有可绑定的已批准法律规则。")
    return posture, facts, claims, legal_sources, legal_rules


def _source_reference(source: AuthoritativeDocumentSource) -> WorkPlanReference:
    """Retain source semantics in the in-memory binding without persisting a plan."""

    source_type, source_id = source.input_ref.split(":", 1)
    mapping = {
        "posture-profile": (
            WorkPlanSourceType.POSTURE_PROFILE,
            WorkPlanReferenceUse.POSTURE,
        ),
        "fact": (WorkPlanSourceType.CASE_FACT, WorkPlanReferenceUse.FACT),
        "claim": (WorkPlanSourceType.CLAIM, WorkPlanReferenceUse.CLAIM_SCOPE),
        "legal-source": (
            WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT,
            WorkPlanReferenceUse.LEGAL_AUTHORITY,
        ),
        "legal-rule": (
            WorkPlanSourceType.LEGAL_RULE_VERSION,
            WorkPlanReferenceUse.LEGAL_RULE,
        ),
        "lawyer-decision-package": (
            WorkPlanSourceType.AGENT_TASK_INPUT,
            WorkPlanReferenceUse.WORK_PLAN,
        ),
    }
    resolved = mapping.get(source_type)
    if resolved is None:
        raise SealedDocumentReplayBlocked("离线文书绑定遇到不支持的来源类型。")
    source_kind, use = resolved
    return WorkPlanReference(
        source_type=source_kind,
        source_id=source_id,
        source_version=source.source_version,
        source_hash=source.source_hash,
        use=use,
    )


def _build_document_binding(
    *,
    settings: CaseAgentWorkerProcessSettings,
    sealed_binding: Mapping[str, object],
    candidate_payload: bytes,
) -> DynamicDocumentTaskBinding:
    """Build an in-memory, visibly offline binding for the released compiler.

    ``work_plan_status`` is set to ACTIVE only to exercise the compiler's
    released precondition.  This object is never handed to a staging port or
    persistence boundary; the title, rationale and receipt all explicitly
    identify it as an offline sealed-response acceptance binding.
    """

    sealed_label = _sealed_label()
    sealed_tag = _sealed_tag()
    matter_id = str(sealed_binding["matter_id"])
    posture, fact_rows, claim_rows, legal_source_rows, legal_rule_rows = (
        _read_current_authoritative_rows(settings=settings, matter_id=matter_id)
    )
    official_source_reader = S3VerifiedOfficialSourceTextPort(
        objects=S3OfficialSourcePrivateObjectStore(settings.object_store)
    )
    root = UUID(str(sealed_binding["run_id"]))
    graph_id = str(uuid5(root, f"sealed-{sealed_tag}-document-replay-graph-v1"))
    task_id = str(uuid5(root, f"sealed-{sealed_tag}-document-replay-task-v1"))
    plan_id = str(uuid5(root, f"sealed-{sealed_tag}-document-replay-plan-v1"))
    item_id = str(uuid5(root, f"sealed-{sealed_tag}-document-replay-item-v1"))
    plan_payload = {
        "title": f"封存 {sealed_label} 响应文书复核候选",
        "purpose": "在不改变案件记录的前提下，验证已封存分析能否由受控链路转化为律师复核候选。",
        "rationale": "仅用于离线验收；不等同于已激活的办案计划、律师决定或对外文书。",
        "risk_if_omitted": "无法验证已封存分析能否安全进入可读、可审查的文书候选。",
        "delivery_target": DeliveryTarget.INTERNAL_WORK_PRODUCT.value,
        "deliverable_kind": "DEFENCE_STATEMENT",
        "required_for_delivery": False,
        "is_primary_document": False,
    }
    plan_hash = _json_hash(plan_payload)
    sources: list[AuthoritativeDocumentSource] = [
        AuthoritativeDocumentSource(
            input_ref=f"posture-profile:{posture['profile_id']}",
            source_kind=DocumentSourceKind.POSTURE_PROFILE,
            source_version=f"v{int(posture['profile_version'])}",
            source_hash=str(posture["profile_hash"]),
            label="当前已确认代理身份与程序阶段",
            text=_json_text(
                {
                    "represented_party": posture["display_label"],
                    "represented_position": posture["represented_position"],
                    "procedure_stage": posture["procedure_stage"],
                    "case_type_code": posture["case_type_code"],
                    "authority_scope_code": posture["authority_scope_code"],
                    "engagement_state": posture["engagement_state"],
                }
            ),
        ),
        AuthoritativeDocumentSource(
            input_ref=f"work-plan-item:{item_id}",
            source_kind=DocumentSourceKind.WORK_PLAN_ITEM,
            source_version=f"sealed-{sealed_tag}-offline-v1",
            source_hash=plan_hash,
            label=f"封存 {sealed_label} 响应离线文书复核计划事项（不写入案件）",
            text=_json_text(plan_payload),
        ),
    ]
    for row in fact_rows:
        sources.append(
            AuthoritativeDocumentSource(
                input_ref=f"fact:{row['fact_id']}",
                source_kind=DocumentSourceKind.CONFIRMED_FACT,
                source_version="confirmed-v1",
                source_hash=str(row["decision_hash"]),
                label="已确认事实",
                text=str(row["original_text"]),
            )
        )
    for row in claim_rows:
        sources.append(
            AuthoritativeDocumentSource(
                input_ref=f"claim:{row['claim_id']}",
                source_kind=DocumentSourceKind.CONFIRMED_CLAIM,
                source_version="confirmed-v1",
                source_hash=str(row["confirmation_hash"]),
                label="已确认诉请范围",
                text=_json_text(
                    {
                        "original_claim_text": row["original_claim_text"],
                        "claimed_amount": row["claimed_amount"],
                        "currency": row["currency"],
                        "position": row["position"],
                        "partial_amount": row["partial_amount"],
                        "partial_currency": row["partial_currency"],
                    }
                ),
            )
        )
    for row in legal_source_rows:
        try:
            reviewed_text = official_source_reader.read_verified_source_text(
                firm_id=settings.runtime.actor.firm_id,
                matter_id=matter_id,
                snapshot_id=str(row["snapshot_id"]),
                storage_object_key=str(row["storage_object_key"]),
                content_sha256=str(row["content_sha256"]),
                content_media_type=str(row["content_media_type"]),
                provision_locator=str(row["provision_locator"]),
            )
        except OfficialSourceObjectStoreBlocked as error:
            raise SealedDocumentReplayBlocked(
                "当前已核验法源没有可安全读取的正文；不得生成律师复核文书。"
            ) from error
        try:
            projection = project_registered_legal_source_for_document(
                source_id=str(row["source_id"]),
                provision_locator=str(row["provision_locator"]),
                literal_text=reviewed_text,
            )
        except LegalProvisionDocumentProjectionBlocked as error:
            raise SealedDocumentReplayBlocked(
                "当前已核验法源不能生成受控条款摘录；不得生成律师复核文书。"
            ) from error
        legal_source_metadata: dict[str, object] = {
            "publisher": row["publisher"],
            "authority_level": row["authority_level"],
            "official_url": row["official_url"],
            "provision_locator": row["provision_locator"],
            "reviewed_text": (
                projection.reviewed_text if projection is not None else reviewed_text
            ),
        }
        if projection is not None:
            legal_source_metadata["source_projection"] = {
                "schema_version": projection.schema_version,
                "source_id": projection.source_id,
                "provision_labels": list(projection.provision_labels),
                "reviewed_text_sha256": projection.reviewed_text_sha256,
            }
        sources.append(
            AuthoritativeDocumentSource(
                input_ref=f"legal-source:{row['snapshot_id']}",
                source_kind=DocumentSourceKind.VERIFIED_LEGAL_SOURCE,
                source_version="verified-v1",
                source_hash=str(row["content_sha256"]),
                label="已核验官方法源",
                text=_json_text(legal_source_metadata),
            )
        )
    for row in legal_rule_rows:
        sources.append(
            AuthoritativeDocumentSource(
                input_ref=f"legal-rule:{row['rule_version_id']}",
                source_kind=DocumentSourceKind.APPROVED_LEGAL_RULE,
                source_version="approved-v1",
                source_hash=str(row["approval_hash"]),
                label="已批准法律规则",
                text=_json_text(
                    {
                        "rule_id": row["rule_id"],
                        "rule_version": row["rule_version"],
                        "issue_key": row["issue_key"],
                        "effective_from": row["effective_from"],
                        "effective_to": row["effective_to"],
                        "trigger_event_kind": row["trigger_event_kind"],
                        "formula_kind": row["formula_kind"],
                        "base_annual_rate": row["base_annual_rate"],
                        "rate_multiplier": row["rate_multiplier"],
                        "derived_annual_rate": row["derived_annual_rate"],
                        "required_fact_keys": row["required_fact_keys"],
                        "transition_rule_versions": row["transition_rule_versions"],
                        "conflict_set": row["conflict_set"],
                        "priority": row["priority"],
                    }
                ),
            )
        )
    sources.append(
        AuthoritativeDocumentSource(
            input_ref=f"lawyer-decision-package:{sealed_binding['run_id']}",
            source_kind=DocumentSourceKind.VERIFIED_LAWYER_DECISION_PACKAGE,
            source_version=f"sealed-{sealed_tag}-current-interpreter-v3",
            source_hash=sha256(candidate_payload).hexdigest(),
            label=(
                f"已封存 {sealed_label} 模型响应经当前规则验证的律师决策包候选"
                "（仅供律师复核）"
            ),
            text=candidate_payload.decode("utf-8"),
        )
    )
    references = tuple(
        _source_reference(source)
        for source in sources
        if source.source_kind is not DocumentSourceKind.WORK_PLAN_ITEM
    )
    posture_reference = references[0]
    work_plan_item = CaseWorkPlanItem(
        item_id=item_id,
        sequence=1,
        kind=WorkPlanItemKind.DOCUMENT_CANDIDATE,
        readiness=WorkPlanReadiness.ACTIONABLE,
        title=str(plan_payload["title"]),
        purpose=str(plan_payload["purpose"]),
        rationale=str(plan_payload["rationale"]),
        prerequisites=(),
        trigger_refs=(posture_reference,),
        source_refs=references,
        risk_if_omitted=str(plan_payload["risk_if_omitted"]),
        confidence=1.0,
        review_gate=ReviewGate.LEAD_LAWYER_CONFIRMATION,
        delivery_target=DeliveryTarget.INTERNAL_WORK_PRODUCT,
        deliverable_kind="DEFENCE_STATEMENT",
        required_for_delivery=False,
        is_primary_document=False,
    )
    binding = DynamicDocumentTaskBinding(
        firm_id=settings.runtime.actor.firm_id,
        matter_id=matter_id,
        run_id=_sealed._SEALED_RUN_ID,
        graph_id=graph_id,
        task_id=task_id,
        task_input_hash=sha256(candidate_payload).hexdigest(),
        case_snapshot_hash=str(sealed_binding["input_hash"]),
        work_plan_id=plan_id,
        work_plan_hash=plan_hash,
        work_plan_status="ACTIVE",
        work_plan_item=work_plan_item,
        posture_profile_id=str(posture["profile_id"]),
        posture_profile_hash=str(posture["profile_hash"]),
        template=first_release_reviewable_document_templates().get(
            "DEFENCE_STATEMENT"
        ),
        sources=tuple(sources),
    )
    binding.validate()
    return binding


def _reconstruct_sealed_candidate(
    *, settings: CaseAgentWorkerProcessSettings, sealed_binding: Mapping[str, object]
) -> tuple[bytes, Mapping[str, object]]:
    sealed_label = _sealed_label()
    object_store = S3CompatiblePrivateObjectStore(settings.object_store)
    recovered = object_store.recover_case_agent_lawyer_analysis_response(
        firm_id=str(sealed_binding["firm_id"]),
        matter_id=str(sealed_binding["matter_id"]),
        external_request_id=str(sealed_binding["external_request_id"]),
        request_hash=str(sealed_binding["request_hash"]),
    )
    if recovered is None:
        raise SealedDocumentReplayBlocked(
            f"封存 {sealed_label} 的私有模型响应归档不存在。"
        )
    stored, response, _receipt = recovered
    if sha256(response).hexdigest() != stored.response_sha256:
        raise SealedDocumentReplayBlocked(f"封存 {sealed_label} 的响应哈希不一致。")
    projection = PostgresCaseContextProjectionPort(
        dsn=settings.runtime.postgres_dsn,
        worker_actor=settings.runtime.actor,
        required_tool_id=LAWYER_ANALYSIS_TOOL_ID,
    ).project_case_context(
        run_id=str(sealed_binding["run_id"]),
        task_id=str(sealed_binding["task_id"]),
        task_input_hash=str(sealed_binding["input_hash"]),
        input_refs=tuple(str(item) for item in sealed_binding["input_refs"]),
    )
    contract = build_lawyer_analysis_contract(projection)
    parsed = parse_lawyer_analysis_provider_response(response, contract=contract)
    candidate_payload = compile_lawyer_decision_package_candidate(
        projection=projection,
        contract=contract,
        parsed=parsed,
        external_request_id=str(sealed_binding["external_request_id"]),
        request_hash=str(sealed_binding["request_hash"]),
    )
    candidate = parse_lawyer_decision_package_candidate(candidate_payload)
    return candidate_payload, {
        "response_sha256": stored.response_sha256,
        "response_bytes": stored.response_bytes,
        "archive_sha256": stored.archive_sha256,
        "candidate_sha256": sha256(candidate_payload).hexdigest(),
        "candidate_bytes": len(candidate_payload),
        "candidate_schema": candidate["schema_version"],
        "model_output_normalization": {
            "status": candidate["model_output_normalization"]["status"],
            "count": len(candidate["model_output_normalization"]["items"]),
        },
        "review_status": candidate["review_status"],
        "court_ready": candidate["court_ready"],
        "official_numeric_result_authored_by_model": candidate[
            "official_numeric_result_authored_by_model"
        ],
    }


def _write_ephemeral_outputs(
    *, docx: bytes, pdf: bytes, receipt: Mapping[str, object]
) -> None:
    output_dir, docx_name, pdf_name, receipt_name = _output_layout()
    if output_dir.exists():
        raise SealedDocumentReplayBlocked("本次封存文书回放输出目录已存在，拒绝覆盖。")
    output_dir.mkdir(mode=0o700)
    (output_dir / docx_name).write_bytes(docx)
    (output_dir / pdf_name).write_bytes(pdf)
    (output_dir / receipt_name).write_text(
        _json_text(receipt) + "\n", encoding="utf-8"
    )


def _replay() -> Mapping[str, object]:
    sealed_label = _sealed_label()
    _output_dir, docx_name, pdf_name, _receipt_name = _output_layout()
    settings = CaseAgentWorkerProcessSettings.from_environment(dict(os.environ))
    if (
        settings.runtime.actor.firm_id is None
        or not settings.document_delivery_enabled
        or settings.document_renderer_settings is None
    ):
        raise SealedDocumentReplayBlocked("受管 Worker 的文书渲染能力不可用。")
    before = _sealed._run_snapshot(settings=settings)
    sealed_binding = _sealed._sealed_binding(settings=settings)
    candidate_payload, replay = _reconstruct_sealed_candidate(
        settings=settings, sealed_binding=sealed_binding
    )
    binding = _build_document_binding(
        settings=settings,
        sealed_binding=sealed_binding,
        candidate_payload=candidate_payload,
    )
    candidate = build_deterministic_defence_statement_candidate(binding)
    candidate_bytes = canonical_document_candidate_bytes(candidate)
    converter = IsolatedDocumentRendererClient(
        settings=settings.document_renderer_settings
    )
    generated = create_reviewable_docx_draft(
        candidate.to_docx_input(visible_document_source_labels(binding)),
        converter=converter,
    )
    docx = generated.editable_artifact.content
    pdf = generated.review_pdf.pdf_content
    if (
        not docx
        or sha256(docx).hexdigest() != generated.editable_artifact.content_sha256
        or not pdf.startswith(b"%PDF-")
        or sha256(pdf).hexdigest() != generated.review_pdf.pdf_sha256
        or generated.review_pdf.source_sha256 != generated.editable_artifact.content_sha256
        or generated.review_pdf.page_count < 1
    ):
        raise SealedDocumentReplayBlocked("文书或隔离渲染输出的哈希绑定无效。")
    after = _sealed._run_snapshot(settings=settings)
    if before != after:
        raise SealedDocumentReplayBlocked("文书回放改变了封存运行状态。")
    source_counts = {
        kind.value: sum(1 for source in binding.sources if source.source_kind is kind)
        for kind in DocumentSourceKind
        if any(source.source_kind is kind for source in binding.sources)
    }
    report: dict[str, object] = {
        "mode": f"READ_ONLY_{sealed_label}_SEALED_RESPONSE_DOCUMENT_REPLAY",
        "status": "SUCCEEDED",
        "boundary": {
            "new_agent_run": False,
            "model_requests_sent": 0,
            "case_database_writes": 0,
            "case_object_store_writes": 0,
            "renderer_requests_sent": 1,
            "offline_binding_is_not_a_persisted_work_plan": True,
            "review_status": candidate.review_status,
            "court_ready": False,
        },
        "sealed_run": {
            "run_id": sealed_binding["run_id"],
            "run_status": before["run_status"],
            "event_version": before["event_version"],
            "external_submission_count": before["external_submission_count"],
            "unchanged_after_replay": True,
        },
        "sealed_response": replay,
        "source_counts": source_counts,
        "document_candidate": {
            "candidate_sha256": candidate.candidate_hash,
            "candidate_payload_sha256": sha256(candidate_bytes).hexdigest(),
            "candidate_payload_bytes": len(candidate_bytes),
            "template_id": candidate.template_id,
            "template_version": candidate.template_version,
            "section_count": len(candidate.sections),
            "binding_hash": binding.binding_hash,
            "source_set_hash": binding.source_set_hash,
        },
        "rendered_artifacts": {
            "docx_file": docx_name,
            "docx_sha256": generated.editable_artifact.content_sha256,
            "docx_bytes": len(docx),
            "pdf_file": pdf_name,
            "pdf_sha256": generated.review_pdf.pdf_sha256,
            "pdf_bytes": len(pdf),
            "pdf_page_count": generated.review_pdf.page_count,
            "renderer_id": generated.review_pdf.converter_id,
            "renderer_version": generated.review_pdf.converter_version,
            "render_verification_hash": generated.review_pdf.render_verification_hash,
        },
    }
    _write_ephemeral_outputs(docx=docx, pdf=pdf, receipt=report)
    return report


def main() -> int:
    if len(sys.argv) != 1:
        raise SealedDocumentReplayBlocked("封存 V4 文书回放不接受参数，避免误指向其他案件。")
    print(_json_text(_replay()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SealedDocumentReplayBlocked, _sealed.SealedReplayBlocked) as error:
        print(
            _json_text(
                {
                    "mode": (
                        f"READ_ONLY_{_sealed_label()}_SEALED_RESPONSE_DOCUMENT_REPLAY"
                    ),
                    "status": "BLOCKED",
                    "reason": str(error),
                }
            )
        )
        raise SystemExit(2)
