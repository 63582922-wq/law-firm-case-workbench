from datetime import date
import unittest

from case_kernel.private_lending_transition_planner import (
    HistoricalInterestPayment,
    RateAnchor,
    TransitionPlanBlocked,
    TransitionPlanStatus,
    plan_private_lending_interest_transition,
)


class PrivateLendingTransitionPlannerTests(unittest.TestCase):
    def test_pre_boundary_contract_in_new_case_splits_at_2020_08_20_and_uses_filing_time_anchor_afterward(self) -> None:
        plan = plan_private_lending_interest_transition(
            contract_formed_on=date(2019, 6, 17),
            claim_filed_on=date(2023, 4, 3),
            first_instance_accepted_on=date(2023, 4, 6),
            calculation_start=date(2019, 6, 17),
            calculation_end=date(2023, 8, 1),
            historical_interest_payments=(
                HistoricalInterestPayment(date(2019, 10, 19), "100.00", False, ("payment-proof-1",)),
            ),
        )
        self.assertEqual(plan.status, TransitionPlanStatus.AWAITING_LAWYER_RULE_APPROVAL)
        self.assertEqual([segment.rate_anchor for segment in plan.segments], [
            RateAnchor.HISTORICAL_PRIVATE_LENDING_REVIEW,
            RateAnchor.CLAIM_FILED_ONE_YEAR_LPR_X4,
        ])
        self.assertEqual(plan.segments[0].end_date, date(2020, 8, 20))
        self.assertEqual(plan.segments[1].anchor_date, date(2023, 4, 3))
        self.assertTrue(any("历史付款尚未确认性质" in task for task in plan.review_tasks))

    def test_post_boundary_contract_uses_contract_formation_anchor_not_annual_rollover(self) -> None:
        plan = plan_private_lending_interest_transition(
            contract_formed_on=date(2021, 1, 5),
            claim_filed_on=date(2023, 4, 3),
            first_instance_accepted_on=date(2023, 4, 6),
            calculation_start=date(2021, 1, 5),
            calculation_end=date(2023, 8, 1),
        )
        self.assertEqual(plan.segments[0].rate_anchor, RateAnchor.CONTRACT_FORMED_ONE_YEAR_LPR_X4)
        self.assertEqual(plan.segments[0].anchor_date, date(2021, 1, 5))
        self.assertIn("非按年度滚动", plan.segments[0].explanation)

    def test_missing_formation_or_procedural_dates_blocks_rule_planning(self) -> None:
        plan = plan_private_lending_interest_transition(
            contract_formed_on=None,
            claim_filed_on=None,
            first_instance_accepted_on=None,
            calculation_start=date(2020, 1, 1),
            calculation_end=date(2020, 2, 1),
        )
        self.assertEqual(plan.status, TransitionPlanStatus.BLOCKED)
        self.assertEqual(len(plan.blockers), 3)
        with self.assertRaisesRegex(TransitionPlanBlocked, "non-empty"):
            plan_private_lending_interest_transition(
                contract_formed_on=date(2020, 1, 1),
                claim_filed_on=date(2020, 1, 2),
                first_instance_accepted_on=date(2020, 1, 3),
                calculation_start=date(2020, 2, 1),
                calculation_end=date(2020, 2, 1),
            )

    def test_boundary_day_acceptance_is_not_silently_classified(self) -> None:
        plan = plan_private_lending_interest_transition(
            contract_formed_on=date(2019, 6, 17),
            claim_filed_on=date(2020, 8, 20),
            first_instance_accepted_on=date(2020, 8, 20),
            calculation_start=date(2019, 6, 17),
            calculation_end=date(2021, 1, 1),
        )
        self.assertEqual(plan.status, TransitionPlanStatus.BLOCKED)
        self.assertIn("恰为 2020-08-20", plan.blockers[0])
