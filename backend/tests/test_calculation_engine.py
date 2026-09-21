from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
import unittest

from case_kernel.calculation_engine import (
    AllocationPolicy,
    ApprovedCalculationEvent,
    ApprovedRuleSegment,
    CalculationBlocked,
    CalculationScenario,
    EventKind,
    PaymentApplication,
    calculate,
    independently_check,
)
from case_kernel.legal_rules import ApprovedLegalBundleReference


def legal_bundle_fixture(*, versions: tuple[str, ...] = ("SYNTHETIC-RULE-1", "SYNTHETIC-RULE-2")) -> ApprovedLegalBundleReference:
    return ApprovedLegalBundleReference(
        bundle_id="alpha_legal_bundle_interest_001",
        bundle_hash=sha256(b"synthetic approved legal bundle").hexdigest(),
        approved_rule_versions=versions,
    )


def synthetic_scenario(*, payment_amount: Decimal = Decimal("1000.00")) -> CalculationScenario:
    start = date(2020, 8, 20)
    end = date(2020, 9, 20)
    return CalculationScenario(
        scenario_id="alpha_interest_001",
        version=1,
        start_date=start,
        end_date=end,
        events=(
            ApprovedCalculationEvent(
                event_id="event_disbursement",
                effective_date=start,
                sequence=1,
                kind=EventKind.DISBURSEMENT,
                amount=Decimal("10000.00"),
                currency="CNY",
                evidence_ids=("evidence_disbursement",),
                approved_by="alpha_lead_lawyer",
                approval_hash="approval_disbursement",
            ),
            ApprovedCalculationEvent(
                event_id="event_payment",
                effective_date=date(2020, 9, 4),
                sequence=1,
                kind=EventKind.PAYMENT,
                amount=payment_amount,
                currency="CNY",
                evidence_ids=("evidence_payment",),
                approved_by="alpha_lead_lawyer",
                approval_hash="approval_payment",
            ),
        ),
        rule_segments=(
            ApprovedRuleSegment(
                segment_id="segment_contract_rate",
                start_date=start,
                end_date=date(2020, 9, 5),
                annual_rate=Decimal("0.10"),
                source_rule_version="SYNTHETIC-RULE-1",
                applicability_anchor="CONTRACT_FORMED_AT",
                approved_by="alpha_lead_lawyer",
                approval_hash="approval_rule_1",
            ),
            ApprovedRuleSegment(
                segment_id="segment_changed_rate",
                start_date=date(2020, 9, 5),
                end_date=end,
                annual_rate=Decimal("0.05"),
                source_rule_version="SYNTHETIC-RULE-2",
                applicability_anchor="FILED_AT",
                approved_by="alpha_lead_lawyer",
                approval_hash="approval_rule_2",
            ),
        ),
        legal_bundle=legal_bundle_fixture(),
        allocation_policy=AllocationPolicy.INTEREST_THEN_PRINCIPAL,
        approved_by="alpha_lead_lawyer",
        approval_hash="approval_scenario",
    )


class CalculationEngineTests(unittest.TestCase):
    def test_interest_then_principal_reduces_later_period_principal(self) -> None:
        scenario = synthetic_scenario()
        run = calculate(scenario)

        self.assertEqual(run.total_interest_accrued, Decimal("62.16"))
        self.assertEqual(run.total_interest_paid, Decimal("41.10"))
        self.assertEqual(run.remaining_principal, Decimal("9041.10"))
        self.assertEqual(run.remaining_unpaid_interest, Decimal("21.06"))
        self.assertEqual(run.unapplied_payments, Decimal("0.00"))
        self.assertEqual(run.payment_allocations[0].allocated_interest, Decimal("41.10"))
        self.assertEqual(run.payment_allocations[0].allocated_principal, Decimal("958.90"))
        self.assertEqual(run.line_items[1].opening_principal, Decimal("9041.10"))
        self.assertTrue(independently_check(scenario, run).matching)

    def test_overpayment_never_makes_principal_negative(self) -> None:
        scenario = synthetic_scenario(payment_amount=Decimal("20000.00"))
        run = calculate(scenario)

        self.assertEqual(run.remaining_principal, Decimal("0.00"))
        self.assertGreater(run.unapplied_payments, Decimal("0.00"))
        self.assertTrue(independently_check(scenario, run).matching)

    def test_ambiguous_same_day_sequence_and_non_cny_are_blocked(self) -> None:
        scenario = synthetic_scenario()
        duplicate_sequence = replace(
            scenario.events[1],
            event_id="event_payment_2",
            sequence=1,
            effective_date=scenario.events[0].effective_date,
        )
        with self.assertRaisesRegex(CalculationBlocked, "same-day event sequence"):
            calculate(replace(scenario, events=(scenario.events[0], duplicate_sequence)))

        foreign_currency = replace(scenario.events[0], currency="USD")
        with self.assertRaisesRegex(CalculationBlocked, "not CNY"):
            calculate(replace(scenario, events=(foreign_currency, scenario.events[1])))

        payment_only = replace(scenario, events=(scenario.events[1],))
        with self.assertRaisesRegex(CalculationBlocked, "disbursement"):
            calculate(payment_only)

    def test_rule_version_change_changes_input_identity(self) -> None:
        scenario = synthetic_scenario()
        original = calculate(scenario)
        updated_segment = replace(scenario.rule_segments[1], annual_rate=Decimal("0.06"), approval_hash="approval_rule_3")
        updated = calculate(replace(scenario, rule_segments=(scenario.rule_segments[0], updated_segment)))

        self.assertNotEqual(original.input_hash, updated.input_hash)
        self.assertNotEqual(original.output_hash, updated.output_hash)
        self.assertTrue(independently_check(scenario, original).matching)
        self.assertTrue(independently_check(replace(scenario, rule_segments=(scenario.rule_segments[0], updated_segment)), updated).matching)

    def test_rule_segment_must_belong_to_the_approved_legal_bundle(self) -> None:
        scenario = synthetic_scenario()
        with self.assertRaisesRegex(CalculationBlocked, "not included"):
            calculate(replace(scenario, legal_bundle=legal_bundle_fixture(versions=("SYNTHETIC-RULE-1",))))

    def test_principal_first_policy_is_explicit_and_recomputable(self) -> None:
        scenario = replace(synthetic_scenario(), allocation_policy=AllocationPolicy.PRINCIPAL_THEN_INTEREST)
        run = calculate(scenario)

        self.assertEqual(run.payment_allocations[0].allocated_principal, Decimal("1000.00"))
        self.assertEqual(run.payment_allocations[0].allocated_interest, Decimal("0.00"))
        self.assertEqual(run.line_items[1].opening_principal, Decimal("9000.00"))
        self.assertTrue(independently_check(scenario, run).matching)

    def test_confirmed_interest_only_payment_does_not_silently_reduce_principal(self) -> None:
        scenario = synthetic_scenario()
        interest_only = replace(
            scenario.events[1],
            payment_application=PaymentApplication.INTEREST_ONLY,
        )
        run = calculate(replace(scenario, events=(scenario.events[0], interest_only)))

        self.assertEqual(run.payment_allocations[0].allocated_interest, Decimal("41.10"))
        self.assertEqual(run.payment_allocations[0].allocated_principal, Decimal("0.00"))
        self.assertEqual(run.payment_allocations[0].unapplied_amount, Decimal("958.90"))
        self.assertEqual(run.line_items[1].opening_principal, Decimal("10000.00"))
        self.assertTrue(independently_check(replace(scenario, events=(scenario.events[0], interest_only)), run).matching)

    def test_leap_day_is_counted_under_actual_365_fixed_basis(self) -> None:
        start = date(2020, 2, 28)
        end = date(2020, 3, 2)
        scenario = CalculationScenario(
            scenario_id="alpha_leap_day",
            version=1,
            start_date=start,
            end_date=end,
            events=(
                ApprovedCalculationEvent(
                    event_id="leap_disbursement",
                    effective_date=start,
                    sequence=1,
                    kind=EventKind.DISBURSEMENT,
                    amount=Decimal("3650.00"),
                    currency="CNY",
                    evidence_ids=("leap_evidence",),
                    approved_by="alpha_lead_lawyer",
                    approval_hash="leap_event_approval",
                ),
            ),
            rule_segments=(
                ApprovedRuleSegment(
                    segment_id="leap_segment",
                    start_date=start,
                    end_date=end,
                    annual_rate=Decimal("0.10"),
                    source_rule_version="SYNTHETIC-RULE-LEAP",
                    applicability_anchor="CONTRACT_FORMED_AT",
                    approved_by="alpha_lead_lawyer",
                    approval_hash="leap_rule_approval",
                ),
            ),
            legal_bundle=ApprovedLegalBundleReference(
                bundle_id="alpha_legal_bundle_leap_001",
                bundle_hash=sha256(b"synthetic leap bundle").hexdigest(),
                approved_rule_versions=("SYNTHETIC-RULE-LEAP",),
            ),
            allocation_policy=AllocationPolicy.INTEREST_THEN_PRINCIPAL,
            approved_by="alpha_lead_lawyer",
            approval_hash="leap_scenario_approval",
        )
        run = calculate(scenario)

        self.assertEqual(run.line_items[0].day_count, 3)
        self.assertEqual(run.total_interest_accrued, Decimal("3.00"))
        self.assertTrue(independently_check(scenario, run).matching)


if __name__ == "__main__":
    unittest.main()
