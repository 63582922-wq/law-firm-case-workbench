"""Continue the existing raw-source synthetic matter; one bounded analysis.

No intake, fact approval, exception withdrawal, retry loop or legal approval.
The fixed idempotency key identifies this analysis across interrupted observers.
"""
import json
import run_managed_defence_acceptance as harness

MATTER_ID = "767fda38-e3de-5a15-816f-510a686c7600"
KEY = "unassisted-source-bound-analysis-20260910-v1"


def main():
    harness._select_acceptance_scenario(harness._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = harness._build_composition(None)
    identity, session = harness._issue_fixture_identity(composition)
    try:
        control = composition.api_dependencies.case_agent_control_service
        matter = composition.api_dependencies.matter_store.get(MATTER_ID, firm_id=identity.actor.firm_id)
        if matter.version != 14:
            raise RuntimeError("case advanced; inspect existing run before continuing")
        snapshot = control._snapshot_reader.get_case_snapshot(
            matter_id=MATTER_ID, actor=identity.actor
        )
        budget = control._run_budget_for(
            (harness.AgentDeliverableKind.DEFENCE_STATEMENT,), snapshot=snapshot
        )
        if budget.max_external_calls != 2 or budget.max_cost_minor_units != 240:
            raise RuntimeError("raw-material two-stage budget is not enforced")
        run = control.create_run(identity=identity, matter_id=MATTER_ID,
            objective="基于本案已入卷原件及其抽取成果推进应诉：分析争点、风险、补证与主备位路径，形成待律师审阅的答辩状、证据目录和补证清单。",
            success_criteria=("每项结论可追溯当前来源，明确区分双方主张、确认事实与待核事项。",
                "保留现有暂缓事项及材料缺口，不把初步研判称为正式提交件。"),
            constraints=("只用现有合成案件，不重新上传或预置事实。",
                "本次累计最多两次模型调用、240分，不自动重发；不批准事实、法源或法律立场。"),
            expected_matter_version=14, idempotency_key=KEY, now=harness._safe_now(),
            requested_deliverables=(harness.AgentDeliverableKind.DEFENCE_STATEMENT,))
        print(json.dumps({"run_id": run.run_id, "matter_id": MATTER_ID,
            "matter_version": 14, "external_call_cap": 2, "cost_cap_minor_units": 240,
            "scope": "EXISTING_RAW_SOURCE_MATTER_ANALYSIS"}), flush=True)
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session)


if __name__ == "__main__":
    main()
