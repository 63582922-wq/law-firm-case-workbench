"""Start exactly one new raw-source acceptance run after fixed synthetic intake.

This creates neither facts nor legal conclusions.  The running worker receives
the server-owned two-call budget and must stop after material extraction for
the synthetic lawyer review; it cannot automatically spend the second call.
"""

from __future__ import annotations

import json
import sys
from uuid import NAMESPACE_URL, uuid5

from case_kernel.case_agent_supervisor import AgentDeliverableKind
from run_managed_defence_acceptance import (
    _build_composition,
    _issue_fixture_identity,
    _key,
    _safe_now,
)


_SCOPES = {
    ("--fresh-v2",): "v2",
    ("--fresh-v2", "--approve-extraction"): "v2",
}


def main() -> None:
    arguments = tuple(sys.argv[1:])
    if arguments not in _SCOPES:
        raise RuntimeError("only the fixed fresh v2 synthetic acceptance scope is allowed")
    scope = _SCOPES[arguments]
    approve_extraction = arguments[-1:] == ("--approve-extraction",)
    composition = _build_composition(None)
    identity, session_id = _issue_fixture_identity(composition)
    try:
        matter_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-unassisted-intake-{scope}:{identity.actor.firm_id}",
            )
        )
        store = composition.api_dependencies.matter_store
        current = store.get(matter_id, firm_id=identity.actor.firm_id)
        if current.version == 2:
            posture = composition.case_posture_service.confirm_complete_posture(
                identity=identity,
                matter_id=matter_id,
                expected_version=2,
                idempotency_key=_key(f"unassisted-raw-defence-{scope}:posture"),
                party_kind="NATURAL_PERSON",
                display_label="赵强（全合成被告）",
                forum_type="PEOPLE_COURT",
                case_type_code="CIVIL.PRIVATE_LENDING",
                procedure_stage="FIRST_INSTANCE",
                position_code="DEFENDANT",
                authority_scope_code="GENERAL_AUTHORITY",
                engagement_state="ACTIVE",
            )
            if posture.matter_version != 7:
                raise RuntimeError("synthetic posture transition differs; do not create a run")
            current = store.get(matter_id, firm_id=identity.actor.firm_id)
        if current.version != 7:
            raise RuntimeError("synthetic matter is not at the fixed pre-run version; inspect it instead of replaying")

        control = composition.api_dependencies.case_agent_control_service
        assert control is not None
        run = control.create_run(
            identity=identity,
            matter_id=matter_id,
            objective="基于已入卷原始材料形成可审阅的应诉工作方案：先整理候选，再在律师确认后分析争点、风险、补证和应诉路径。",
            success_criteria=(
                "所有候选都可回到原件页核对，并明确区分待核内容与已确认内容。",
                "只在律师确认后形成风险、补证、答辩状和独立证据目录候选。",
            ),
            constraints=(
                "仅使用本次全合成原件；不得预置事实、诉请、争点、金额或法律结论。",
                "累计最多两次外部调用、240分；不自动重试、不提交法院。",
            ),
            expected_matter_version=current.version,
            idempotency_key=_key(f"unassisted-raw-defence-{scope}:run"),
            now=_safe_now(),
            requested_deliverables=(
                AgentDeliverableKind.DEFENCE_STATEMENT,
                AgentDeliverableKind.EVIDENCE_CATALOGUE,
            ),
        )
        state = control._store.replay_run(
            matter_id=matter_id, actor=identity.actor, run_id=run.run_id
        )
        snapshot = control._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=identity.actor
        )
        if (
            state.budget.max_external_calls != 2
            or state.budget.max_cost_minor_units != 240
            or state.budget_usage.external_calls != 0
            or snapshot.version != 7
            or snapshot.facts
            or snapshot.claims
            or snapshot.issues
        ):
            raise RuntimeError("raw acceptance run budget or input boundary differs")
        if approve_extraction:
            approvals = control.list_approvals(
                identity=identity, matter_id=matter_id, run_id=run.run_id
            )
            if len(approvals) != 1:
                raise RuntimeError("raw extraction approval is not uniquely available")
            approval = approvals[0]
            task = next(
                item.spec for item in state.tasks if item.spec.task_id == approval.approval_id
            )
            if (
                task.skill.skill_id != "case_ledger_extraction"
                or task.budget.max_external_calls != 1
                or task.budget.max_attempts != 1
                or task.budget.max_cost_minor_units > 120
            ):
                raise RuntimeError("raw extraction approval exceeds the governed one-call boundary")
            receipt = control.submit_approval(
                identity=identity,
                matter_id=matter_id,
                run_id=run.run_id,
                approval_id=task.task_id,
                approved=True,
                note="按既有授权执行一次全合成材料提取；仅形成待律师核对候选，不确认事实、法律立场或提交材料。",
                expected_run_version=state.event_version,
                idempotency_key=_key(f"unassisted-raw-defence-{scope}:approve-extraction"),
                now=_safe_now(),
            )
            print(
                json.dumps(
                    {
                        "acceptance_scope": scope,
                        "run_id": run.run_id,
                        "approved_task_id": task.task_id,
                        "approved_skill": task.skill.skill_id,
                        "approved_external_call_cap": task.budget.max_external_calls,
                        "approved_cost_cap_minor_units": task.budget.max_cost_minor_units,
                        "result": "SINGLE_RAW_EXTRACTION_APPROVED",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        print(
            json.dumps(
                {
                    "acceptance_scope": scope,
                    "matter_id": matter_id,
                    "run_id": run.run_id,
                    "matter_version": snapshot.version,
                    "confirmed_facts": len(snapshot.facts),
                    "confirmed_claims": len(snapshot.claims),
                    "confirmed_issues": len(snapshot.issues),
                    "requested_deliverables": [
                        item.value for item in state.goal.requested_deliverables
                    ],
                    "external_call_cap": state.budget.max_external_calls,
                    "cost_cap_minor_units": state.budget.max_cost_minor_units,
                    "external_calls_used": state.budget_usage.external_calls,
                    "model_call_executed_by_this_command": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
