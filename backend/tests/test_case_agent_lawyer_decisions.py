from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from uuid import uuid4

from case_kernel.case_agent_lawyer_decisions import (
    GovernedLawyerPlanningDecision,
    LawyerPlanningDecisionBlocked,
    LawyerPlanningDecisionCode,
    lawyer_planning_decision_options,
)
from case_kernel.case_agent_planner import PlanningInputStatus, PlanningSignalCategory


class LawyerPlanningDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            "signal_id": str(uuid4()),
            "run_id": str(uuid4()),
            "firm_id": str(uuid4()),
            "matter_id": str(uuid4()),
            "graph_id": str(uuid4()),
            "task_id": str(uuid4()),
            "signal_version": 1,
            "decision_code": LawyerPlanningDecisionCode.WRONG_LEGAL_DIRECTION,
            "note": "应先核对争议期间和规则效力，再决定研究方向。",
            "source_ref_ids": (
                f"legal-source:{uuid4()}",
                f"work-plan-item:{uuid4()}",
            ),
            "task_input_hash": "1" * 64,
            "graph_hash": "2" * 64,
            "recorded_event_sequence": 7,
            "recorded_by": str(uuid4()),
            "decided_at": datetime(2026, 8, 13, 10, 30, tzinfo=timezone.utc),
        }

    def test_build_is_canonical_and_server_maps_policy(self) -> None:
        decision = GovernedLawyerPlanningDecision.build(**self.values)
        self.assertEqual(decision.category, PlanningSignalCategory.LEGAL_GAP)
        self.assertEqual(decision.status, PlanningInputStatus.DISPUTED)
        self.assertEqual(decision.source_ref_ids, tuple(sorted(self.values["source_ref_ids"])))
        self.assertIn("补充说明", decision.summary)
        self.assertEqual(len(decision.decision_hash), 64)
        decision.validate()

    def test_same_input_is_deterministic(self) -> None:
        first = GovernedLawyerPlanningDecision.build(**self.values)
        second = GovernedLawyerPlanningDecision.build(**self.values)
        self.assertEqual(first, second)

    def test_policy_needing_a_note_fails_closed(self) -> None:
        with self.assertRaisesRegex(LawyerPlanningDecisionBlocked, "requires a note"):
            GovernedLawyerPlanningDecision.build(**{**self.values, "note": None})

    def test_browser_cannot_smuggle_source_or_command(self) -> None:
        with self.assertRaisesRegex(LawyerPlanningDecisionBlocked, "source reference"):
            GovernedLawyerPlanningDecision.build(
                **{**self.values, "source_ref_ids": ("file:///etc/passwd",)}
            )

    def test_tampering_is_rejected(self) -> None:
        decision = GovernedLawyerPlanningDecision.build(**self.values)
        with self.assertRaisesRegex(LawyerPlanningDecisionBlocked, "hash differs"):
            replace(decision, summary="换一个没有进入哈希的说明").validate()

    def test_options_expose_no_planning_category_or_tool(self) -> None:
        options = lawyer_planning_decision_options()
        self.assertEqual(len(options), len(LawyerPlanningDecisionCode))
        self.assertTrue(all(code.startswith("LAWYER_REJECT_") for code, _, _ in options))
        self.assertTrue(all(len(item) == 3 for item in options))


if __name__ == "__main__":
    unittest.main()
