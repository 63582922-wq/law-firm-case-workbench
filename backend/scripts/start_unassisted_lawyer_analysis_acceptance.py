"""Create and explicitly approve one bounded analysis round for the raw fixture.

The source fixture has passed the separate synthetic lawyer gates for its
low-risk facts and page inclusion.  This command still cannot approve a legal
position, an answer, evidence three-nature judgment, or a court submission.
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
    ("--fresh-v2", "--approve-analysis"): "v2",
}


def main() -> None:
    arguments = tuple(sys.argv[1:])
    if arguments not in _SCOPES:
        raise RuntimeError("only the fixed fresh v2 synthetic acceptance scope is allowed")
    scope = _SCOPES[arguments]
    approve_analysis = arguments[-1:] == ("--approve-analysis",)
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
        matter = store.get(matter_id, firm_id=identity.actor.firm_id)
        if matter.version != 17:
            raise RuntimeError("synthetic matter is not at the reviewed input version; inspect rather than re-run")
        control = composition.api_dependencies.case_agent_control_service
        assert control is not None
        snapshot = control._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=identity.actor
        )
        if len(snapshot.facts) != 8 or snapshot.claims or snapshot.issues:
            raise RuntimeError("reviewed raw fixture input differs; do not send analysis")
        run = control.create_run(
            identity=identity,
            matter_id=matter_id,
            objective="基于已确认的材料记载分析本案争点、证据风险、补证方向和应诉路径；将结论、依据和待律师决定事项集中供审阅。",
            success_criteria=(
                "每项判断区分已确认事实、待核事项和律师判断，并可回到当前来源。",
                "形成风险与补证清单、案件审阅意见和可用的内部工作成果；未满足前提的文书必须明确缺口。",
            ),
            constraints=(
                "只使用当前已确认的材料记载和已纳入范围的证据页。",
                "本轮最多一次外部分析、120分；不自动重试、不批准法律立场、不提交法院。",
            ),
            expected_matter_version=matter.version,
            idempotency_key=_key(f"unassisted-lawyer-analysis-{scope}:run"),
            now=_safe_now(),
            requested_deliverables=(
                AgentDeliverableKind.CASE_REVIEW_MEMO,
                AgentDeliverableKind.DEFENCE_STATEMENT,
                AgentDeliverableKind.EVIDENCE_CATALOGUE,
                AgentDeliverableKind.PAYMENT_LEDGER,
            ),
        )
        state = control._store.replay_run(
            matter_id=matter_id, actor=identity.actor, run_id=run.run_id
        )
        if (
            state.budget.max_external_calls != 1
            or state.budget.max_cost_minor_units != 120
            or state.budget_usage.external_calls != 0
            or state.snapshot.matter_version != matter.version
        ):
            raise RuntimeError("reviewed-input analysis budget or snapshot differs")
        if approve_analysis:
            approvals = control.list_approvals(
                identity=identity, matter_id=matter_id, run_id=run.run_id
            )
            if len(approvals) != 1:
                raise RuntimeError("analysis approval is not uniquely available")
            approval = approvals[0]
            task = next(
                item.spec for item in state.tasks if item.spec.task_id == approval.approval_id
            )
            if (
                task.skill.skill_id != "lawyer_decision_package"
                or task.budget.max_external_calls != 1
                or task.budget.max_attempts != 1
                or task.budget.max_cost_minor_units > 120
            ):
                raise RuntimeError("analysis approval exceeds the governed one-call boundary")
            control.submit_approval(
                identity=identity,
                matter_id=matter_id,
                run_id=run.run_id,
                approval_id=task.task_id,
                approved=True,
                note="按既有授权执行一次全合成案件研判；结果仅供律师审阅，不确认法律立场、终审或对外提交。",
                expected_run_version=state.event_version,
                idempotency_key=_key(f"unassisted-lawyer-analysis-{scope}:approve"),
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
                        "result": "SINGLE_LAWYER_ANALYSIS_APPROVED",
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
                    "matter_version": matter.version,
                    "confirmed_facts": len(snapshot.facts),
                    "confirmed_claims": len(snapshot.claims),
                    "confirmed_issues": len(snapshot.issues),
                    "requested_deliverables": [item.value for item in state.goal.requested_deliverables],
                    "external_call_cap": state.budget.max_external_calls,
                    "cost_cap_minor_units": state.budget.max_cost_minor_units,
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
