"""One newly authorized analysis on the existing synthetic case, no intake replay.

This tests the delivery half only: the historical fixture has confirmed facts.
It does not prove autonomous raw-material understanding or lawyer acceptance.
"""
from __future__ import annotations

import json
import time

import run_managed_defence_acceptance as harness


def main() -> None:
    harness._select_acceptance_scenario(harness._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = harness._build_composition(None)
    identity, session_id = harness._issue_fixture_identity(composition)
    try:
        matter = harness._current_matter(composition, identity)
        control = composition.api_dependencies.case_agent_control_service
        assert control is not None
        snapshot = control._snapshot_reader.get_case_snapshot(
            matter_id=matter.matter_id, actor=identity.actor
        )
        policy_budget = control._run_budget_for(
            (harness.AgentDeliverableKind.DEFENCE_STATEMENT,), snapshot=snapshot
        )
        if policy_budget.max_external_calls != 1 or policy_budget.max_cost_minor_units > 120:
            raise RuntimeError("authorized single-call budget is not enforced")
        run = control.create_run(
            identity=identity, matter_id=matter.matter_id,
            objective=harness._FIRST_RUN_OBJECTIVE,
            success_criteria=harness._FIRST_RUN_SUCCESS_CRITERIA,
            constraints=harness._FIRST_RUN_CONSTRAINTS,
            expected_matter_version=matter.version,
            idempotency_key=harness._key("authorized-20260910-analysis"),
            now=harness._safe_now(),
            requested_deliverables=(harness.AgentDeliverableKind.DEFENCE_STATEMENT,),
        )
        print(json.dumps({"stage": "CREATED_OR_EXISTING", "run_id": run.run_id,
            "matter_id": matter.matter_id, "matter_version": matter.version,
            "max_cost_minor_units": 120, "max_external_calls": 1,
            "scope": "EXISTING_SYNTHETIC_CONFIRMED_INPUTS_TO_DOCUMENT"}), flush=True)
        ready = harness._wait_for_run(control, identity, run_id=run.run_id,
            phase="analysis", deadline=time.monotonic() + 600)
        calls = harness._assert_observed_run_boundary(control, identity,
            run_id=run.run_id, phase="analysis")
        print(json.dumps({"stage": "ANALYSIS_READY", "run_id": run.run_id,
            "status": ready.status, "external_calls": calls}), flush=True)
        # Stop at review; do not hide the actual lawyer decision behind a
        # harness approval or overwrite earlier failed runs.
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
