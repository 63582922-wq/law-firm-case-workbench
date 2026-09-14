"""Create one clean synthetic raw-material matter and verify its run budget.

This is a deployment preflight, not a model-quality or document-delivery run.
It uploads a small self-contained PDF, confirms only the defendant posture,
then creates the normal server-owned defence goal and reads its immutable
RUN_CREATED budget.  It never approves an external task, confirms a fact,
creates a claim, or waits for an Agent result.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
import json
import sys
from uuid import NAMESPACE_URL, uuid5

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from case_kernel.case_agent_supervisor import AgentDeliverableKind
from case_kernel.models import Matter
from run_managed_defence_acceptance import (
    _build_composition,
    _issue_fixture_identity,
    _key,
    _safe_now,
)


_SCOPES = {
    (): "raw-defence-stage-budget-preflight-20260910-v1",
    ("--fresh-v2",): "raw-defence-stage-budget-preflight-20260910-v2",
    ("--fresh-v3",): "raw-defence-stage-budget-preflight-20260910-v3",
}


async def _content_chunks(content: bytes):
    yield content


def _synthetic_raw_pdf() -> bytes:
    """Build legible source material without fixture answers or hidden tags."""

    output = BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    page = canvas.Canvas(output, pagesize=A4)
    page.setFont("STSong-Light", 12)
    lines = (
        "民事起诉材料（全合成测试原件）",
        "原告李明主张：2024年2月1日向被告赵强出借人民币50,000元。",
        "被告材料记载：曾有还款沟通，但对应金额、日期和凭证仍待核对。",
        "本页仅为原始材料；不包含事实确认、风险结论或文书答案。",
    )
    y = 780
    for line in lines:
        page.drawString(72, y, line)
        y -= 32
    page.showPage()
    page.save()
    content = output.getvalue()
    extracted = "".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages
    )
    if "50,000" not in extracted or "待核对" not in extracted:
        raise RuntimeError("synthetic raw PDF text is not readable")
    return content


def main() -> None:
    arguments = tuple(sys.argv[1:])
    if arguments not in _SCOPES:
        raise RuntimeError("only the fixed v1, fresh v2, or fresh v3 synthetic preflight scope is allowed")
    scope = _SCOPES[arguments]
    content = _synthetic_raw_pdf()
    composition = _build_composition(None)
    identity, session_id = _issue_fixture_identity(composition)
    try:
        matter_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{scope}:{identity.actor.firm_id}",
            )
        )
        store = composition.api_dependencies.matter_store
        try:
            current = store.get(matter_id, firm_id=identity.actor.firm_id)
        except KeyError:
            receipt = store.create(
                matter=Matter(
                    matter_id=matter_id,
                    firm_id=identity.actor.firm_id,
                    title="合成验收｜原始材料两阶段预算预检",
                ),
                actor=identity.actor,
                idempotency_key=_key(f"{scope}:create-matter"),
            )
            if receipt.matter_version != 1:
                raise RuntimeError("synthetic preflight matter creation mismatch")
            upload = composition.material_upload_service
            slot = upload.create_slot(
                identity=identity,
                matter_id=matter_id,
                expected_version=1,
                client_filename="原始起诉材料（全合成）.pdf",
                declared_content_length=len(content),
            )
            accepted = asyncio.run(
                upload.accept_content(
                    identity=identity,
                    matter_id=matter_id,
                    upload_id=slot.upload_id,
                    chunks=_content_chunks(content),
                )
            )
            if accepted.content_sha256 != sha256(content).hexdigest():
                raise RuntimeError("synthetic raw material receipt hash mismatch")
            current = store.get(matter_id, firm_id=identity.actor.firm_id)
        if current.version == 2:
            posture = composition.case_posture_service.confirm_complete_posture(
                identity=identity,
                matter_id=matter_id,
                expected_version=2,
                idempotency_key=_key(f"{scope}:confirm-posture"),
                party_kind="NATURAL_PERSON",
                display_label="赵强（全合成被告）",
                forum_type="PEOPLE_COURT",
                case_type_code="CIVIL.PRIVATE_LENDING",
                procedure_stage="FIRST_INSTANCE",
                position_code="DEFENDANT",
                authority_scope_code="GENERAL_AUTHORITY",
                engagement_state="ACTIVE",
            )
            # The complete-posture command writes five governed sub-records,
            # so an uploaded matter advances from version 2 to version 7.
            if posture.matter_version != 7:
                raise RuntimeError("synthetic preflight posture confirmation mismatch")
            current = store.get(matter_id, firm_id=identity.actor.firm_id)
        elif current.version != 7:
            raise RuntimeError(
                "synthetic raw budget preflight has an unexpected matter version; inspect instead of replaying"
            )

        control = composition.api_dependencies.case_agent_control_service
        assert control is not None
        current = store.get(matter_id, firm_id=identity.actor.firm_id)
        run = control.create_run(
            identity=identity,
            matter_id=matter_id,
            objective="基于已入卷原始材料形成来源受控的被告应诉办理路径。",
            success_criteria=(
                "先形成可回到原件核对的材料候选。",
                "仅在律师确认事实后形成风险与补证研判候选。",
            ),
            constraints=(
                "全合成材料；不确认事实、不批准法律立场、不对外提交。",
                "累计最多两次外部调用、240分；不自动重试。",
            ),
            expected_matter_version=current.version,
            idempotency_key=_key(f"{scope}:create-run"),
            now=_safe_now(),
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
        )
        state = control._store.replay_run(
            matter_id=matter_id, actor=identity.actor, run_id=run.run_id
        )
        if (
            state.budget.max_external_calls != 2
            or state.budget.max_cost_minor_units != 240
            or state.budget_usage.external_calls != 0
        ):
            raise RuntimeError("persisted raw-material defence budget is not the governed preflight cap")
        snapshot = control._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=identity.actor
        )
        if snapshot.facts or snapshot.claims or snapshot.issues:
            raise RuntimeError("synthetic preflight must not preseed facts, claims, or issues")
        print(
            json.dumps(
                {
                    "result": "PERSISTED_RAW_DEFENCE_BUDGET_PREFLIGHT",
                    "matter_id": matter_id,
                    "run_id": run.run_id,
                    "matter_version": current.version,
                    "raw_material_sha256": sha256(content).hexdigest(),
                    "confirmed_facts": len(snapshot.facts),
                    "confirmed_claims": len(snapshot.claims),
                    "confirmed_issues": len(snapshot.issues),
                    "external_call_cap": state.budget.max_external_calls,
                    "cost_cap_minor_units": state.budget.max_cost_minor_units,
                    "external_calls_used": state.budget_usage.external_calls,
                    "model_call_executed": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
