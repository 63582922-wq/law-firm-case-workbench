from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.golden_case_calculation import (
    AUTHORITATIVE_ORACLE_SHA256,
    DEFAULT_RECOMMENDED_CHOICES,
    GoldenCaseCalculationBlocked,
    build_review_packet,
    classify_extracted_rows,
    compare_with_golden,
    load_golden_outputs,
    recommended_choices,
    run_independent_scenarios,
    selected_scenario_id,
    validate_choices,
)
from case_kernel.golden_case_source import (
    deduplicate_pages,
    extract_ledger_rows,
    generate_golden_case,
    load_authoritative_case,
    read_generated_pages,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GoldenCaseCalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_authoritative_case(PROJECT_ROOT)
        cls.temporary = TemporaryDirectory()
        cls.generated = generate_golden_case(Path(cls.temporary.name) / "case", cls.spec)
        cls.dedup = deduplicate_pages(read_generated_pages(cls.generated))
        cls.extracted = extract_ledger_rows(cls.dedup)
        cls.classified = classify_extracted_rows(
            cls.extracted, DEFAULT_RECOMMENDED_CHOICES
        )
        cls.suite = run_independent_scenarios(
            cls.classified,
            spec=PROJECT_ROOT / "docs" / "GOLDEN_CASE_SYNTHETIC.md",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_production_normalizer_uses_extraction_and_ten_choices(self) -> None:
        self.assertEqual(len(self.classified), 47)
        self.assertEqual(self.suite.normalized_event_count, 37)
        self.assertEqual(self.suite.excluded_row_numbers, (36, 37, 40, 41, 42, 43, 44))
        self.assertEqual(self.suite.merged_row_numbers, (45, 46, 47))
        self.assertEqual(self.suite.blocked_row_numbers, (35,))
        self.assertEqual(selected_scenario_id(DEFAULT_RECOMMENDED_CHOICES), "S-A-1")

    def test_black_box_oracle_and_independent_engine_match_every_shared_field(self) -> None:
        oracle = load_golden_outputs(PROJECT_ROOT)
        self.assertEqual(oracle.source_sha256, AUTHORITATIVE_ORACLE_SHA256)
        comparison = compare_with_golden(self.suite, oracle)
        self.assertTrue(comparison.matching, comparison.mismatches)
        self.assertEqual(comparison.scenario_count, 4)
        self.assertGreaterEqual(comparison.checked_fields, 300)
        self.assertEqual(len(comparison.mismatches), 0)

    def test_authoritative_four_scenario_numbers_and_five_trace_anchors(self) -> None:
        expected = {
            "S-A-1": ("310638.59", "123297.31"),
            "S-A-2": ("274451.74", "111138.53"),
            "S-B-1": ("334661.81", "157345.89"),
            "S-B-2": ("303448.31", "141884.81"),
        }
        for scenario_id, totals in expected.items():
            result = self.suite.scenario(scenario_id)
            self.assertEqual((str(result.total_principal), str(result.total_interest_arrears)), totals)
        self.assertEqual(
            tuple(item.event_code for item in self.suite.key_anchors),
            ("Q1", "R1", "Q10", "R2", "R4"),
        )
        self.assertEqual(
            str(self.suite.scenario("S-A-1").loan("L1").principal), "139010.97"
        )

    def test_single_review_packet_has_exactly_ten_real_choices(self) -> None:
        source_refs = {
            row.row_number: [ref.source_ref_id for ref in row.source_refs]
            for row in self.extracted
        }
        packet = build_review_packet(
            PROJECT_ROOT / "docs" / "GOLDEN_CASE_SYNTHETIC.md",
            self.classified,
            self.suite,
            source_refs,
        )
        self.assertEqual(packet["decision_count"], 10)
        self.assertEqual(recommended_choices(packet), DEFAULT_RECOMMENDED_CHOICES)
        decision_hash = validate_choices(packet, DEFAULT_RECOMMENDED_CHOICES)
        self.assertRegex(decision_hash, r"^[0-9a-f]{64}$")
        missing = dict(DEFAULT_RECOMMENDED_CHOICES)
        missing.pop("D10_LIMITATIONS_EVIDENCE")
        with self.assertRaisesRegex(GoldenCaseCalculationBlocked, "exactly ten"):
            validate_choices(packet, missing)

    def test_alternative_cash_and_u2_choices_select_authoritative_scenarios(self) -> None:
        cash = dict(DEFAULT_RECOMMENDED_CHOICES)
        cash["D06_CASH_SWITCH"] = "INCLUDE_CASH_L1"
        u2 = dict(DEFAULT_RECOMMENDED_CHOICES)
        u2["D07_U2_SWITCH"] = "EXCLUDE_U2_AS_EXTERNAL"
        self.assertEqual(selected_scenario_id(cash), "S-A-2")
        self.assertEqual(selected_scenario_id(u2), "S-B-1")
        self.assertNotEqual(
            self.suite.scenario("S-A-1").loan("L1").principal,
            self.suite.scenario("S-A-2").loan("L1").principal,
        )
        self.assertEqual(
            self.suite.scenario("S-A-1").loan("L2"),
            self.suite.scenario("S-B-2").loan("L2"),
        )

    def test_unresolved_required_classification_fails_closed(self) -> None:
        choices = dict(DEFAULT_RECOMMENDED_CHOICES)
        choices["D04_LM_THIRD_PARTY_PAYMENT"] = "DO_NOT_CONFIRM"
        with self.assertRaisesRegex(GoldenCaseCalculationBlocked, "unresolved"):
            classify_extracted_rows(self.extracted, choices)


if __name__ == "__main__":
    unittest.main(verbosity=2)
