"""买卖合同（货款）口径：律师选口径、引擎算数的契约与计算测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.case_analysis_service import compute_engine_numbers
from case_kernel.case_sales_claim import (
    LOSS_BASE_IDS,
    SalesClaimError,
    annual_rate,
    compute_sales_numbers,
    load_sales_claim,
)


def _config(path: Path, block: dict, payments: list[dict] | None = None) -> Path:
    payload = {
        "schema": "shadow-case-config-v1",
        "lpr_4x_monthly_rate": "0.01",
        "interest_cutoff": "2026-06-22",
        "debts": [{"debt_id": "L1", "principal": "10000.00", "disbursed_on": "2025-01-01",
                   "agreed_monthly_rate": "0.0"}],
        "sales_claim": block,
    }
    if payments is not None:
        payload["payments"] = payments
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


BASE_BLOCK = {
    "kind": "GOODS_PAYMENT",
    "claim_amount": "10000.00",
    "confirmed_principal": "10000.00",
    "overdue_from": "2025-10-25",
    "cutoff": "2026-06-22",
    "loss_basis": "LPR",
    "lpr_annual": "0.03",
}


class LoadTests(unittest.TestCase):
    def test_missing_block_returns_none(self) -> None:
        self.assertIsNone(load_sales_claim({}))
        self.assertIsNone(load_sales_claim(None))

    def test_basis_must_be_known(self) -> None:
        with self.assertRaises(SalesClaimError) as caught:
            load_sales_claim({**BASE_BLOCK, "loss_basis": "LPR_4"})
        self.assertIn("口径", str(caught.exception))

    def test_lpr_required_for_lpr_bases(self) -> None:
        with self.assertRaises(SalesClaimError):
            load_sales_claim({**BASE_BLOCK, "lpr_annual": None})

    def test_agreed_basis_requires_agreed_rate(self) -> None:
        with self.assertRaises(SalesClaimError):
            load_sales_claim({**BASE_BLOCK, "loss_basis": "AGREED", "lpr_annual": None})
        claim = load_sales_claim({**BASE_BLOCK, "loss_basis": "AGREED",
                                  "agreed_annual": "0.18"})
        self.assertEqual(annual_rate(claim), Decimal("0.18"))

    def test_percent_and_decimal_rates_both_accepted(self) -> None:
        claim = load_sales_claim({**BASE_BLOCK, "lpr_annual": "3"})
        self.assertEqual(annual_rate(claim), Decimal("0.03"))
        claim = load_sales_claim({**BASE_BLOCK, "lpr_annual": "0.03"})
        self.assertEqual(annual_rate(claim), Decimal("0.03"))

    def test_cutoff_before_overdue_is_rejected(self) -> None:
        with self.assertRaises(SalesClaimError):
            load_sales_claim({**BASE_BLOCK, "overdue_from": "2026-01-01",
                              "cutoff": "2025-01-01"})

    def test_empty_confirmed_principal_means_claim_amount(self) -> None:
        """界面上留空的「确认应付货款」= 与主张金额一致，不是错误。"""
        claim = load_sales_claim({**BASE_BLOCK, "confirmed_principal": ""})
        self.assertEqual(claim.confirmed_principal, claim.claim_amount)
        claim = load_sales_claim({k: v for k, v in BASE_BLOCK.items()
                                  if k != "confirmed_principal"})
        self.assertEqual(claim.confirmed_principal, claim.claim_amount)

    def test_bad_payment_classification_is_rejected(self) -> None:
        with self.assertRaises(SalesClaimError):
            load_sales_claim({
                **BASE_BLOCK,
                "payments": [{"paid_on": "2025-12-01", "amount": "1000",
                              "classification": "抵扣"}],
            })


class CalculationTests(unittest.TestCase):
    def _claim(self, **overrides):
        return load_sales_claim({**BASE_BLOCK, **overrides})

    def test_lpr_basis_uses_actual_days_single_interest(self) -> None:
        claim = self._claim()
        numbers, note = compute_sales_numbers(claim)
        days = (date(2026, 6, 22) - date(2025, 10, 25)).days   # 240 天
        expected = (Decimal("10000") * Decimal("0.03") / Decimal("365") * days) \
            .quantize(Decimal("0.01"))
        self.assertEqual(numbers["未付货款本金"], "10000.00")
        self.assertEqual(numbers["逾期付款损失（净额）"], f"{expected:.2f}")
        self.assertIn("÷365", note)
        self.assertIn("按一年期 LPR", note)

    def test_lpr_1_5_is_one_and_a_half_times(self) -> None:
        base = compute_sales_numbers(self._claim())[0]
        one_five = compute_sales_numbers(self._claim(loss_basis="LPR_1_5"))[0]
        self.assertLess(Decimal(base["逾期付款损失（净额）"]),
                        Decimal(one_five["逾期付款损失（净额）"]))
        self.assertAlmostEqual(
            float(Decimal(one_five["逾期付款损失（净额）"]))
            / float(Decimal(base["逾期付款损失（净额）"])), 1.5, places=2)

    def test_none_basis_gives_no_loss(self) -> None:
        numbers, note = compute_sales_numbers(self._claim(loss_basis="NONE"))
        self.assertEqual(numbers["逾期付款损失（净额）"], "0.00")
        self.assertEqual(numbers["本金与损失合计"], "10000.00")
        self.assertIn("不主张逾期付款损失", note)

    def test_principal_payment_reduces_balance_and_future_interest(self) -> None:
        claim = self._claim(payments=[
            {"paid_on": "2025-12-25", "amount": "4000.00", "classification": "还本"},
        ])
        numbers, note = compute_sales_numbers(claim)
        self.assertEqual(numbers["未付货款本金"], "6000.00")
        self.assertEqual(numbers["已确认还本合计"], "4000.00")
        # 前段按 10000 计 61 天，后段按 6000 计 179 天
        first = Decimal("10000") * Decimal("0.03") / Decimal("365") * 61
        second = Decimal("6000") * Decimal("0.03") / Decimal("365") * 179
        self.assertEqual(numbers["逾期付款损失（毛额）"],
                         f"{(first + second).quantize(Decimal('0.01')):.2f}")
        self.assertIn("2 段", note)

    def test_loss_payment_offsets_loss_only(self) -> None:
        claim = self._claim(payments=[
            {"paid_on": "2025-12-25", "amount": "50.00", "classification": "付息"},
        ])
        numbers, note = compute_sales_numbers(claim)
        self.assertEqual(numbers["未付货款本金"], "10000.00")     # 付息不动本金
        gross = Decimal(numbers["逾期付款损失（毛额）"])
        net = Decimal(numbers["逾期付款损失（净额）"])
        self.assertEqual(gross - net, Decimal("50.00"))
        self.assertIn("付息", note)

    def test_disputed_payments_never_enter_numbers(self) -> None:
        claim = self._claim(payments=[
            {"paid_on": "2025-12-25", "amount": "9000.00", "classification": "争议"},
            {"paid_on": "2026-01-25", "amount": "500.00", "classification": "排除"},
        ])
        numbers, note = compute_sales_numbers(claim)
        self.assertEqual(numbers["未付货款本金"], "10000.00")
        self.assertNotIn("已确认还本合计", {k: v for k, v in numbers.items() if v != "0.00"})
        self.assertIn("2 笔付款标记为争议/排除", note)

    def test_claim_amount_and_confirmed_principal_can_differ(self) -> None:
        numbers, _ = compute_sales_numbers(self._claim(confirmed_principal="8000.00"))
        self.assertEqual(numbers["原告主张货款"], "10000.00")
        self.assertEqual(numbers["律师确认应付货款"], "8000.00")
        self.assertEqual(numbers["未付货款本金"], "8000.00")


class RouteThroughEngineTests(unittest.TestCase):
    def test_case_config_with_sales_block_uses_sales_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _config(Path(tmp) / "case_config.json", BASE_BLOCK)
            numbers, note = compute_engine_numbers(path)
            self.assertIn("未付货款本金", numbers)
            self.assertIn("逾期付款损失（净额）", numbers)
            self.assertNotIn("合计本金", numbers)       # 不套用借贷口径
            self.assertIn("模型未参与任何计算", note)

    def test_case_config_without_sales_block_keeps_loan_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "case_config.json"
            path.write_text(json.dumps({
                "schema": "shadow-case-config-v1",
                "lpr_4x_monthly_rate": "0.01",
                "interest_cutoff": "2025-06-14",
                "debts": [{"debt_id": "L1", "principal": "100000.00",
                           "disbursed_on": "2019-10-19",
                           "agreed_monthly_rate": "0.015", "due_on": "2019-12-19"}],
            }, ensure_ascii=False), encoding="utf-8")
            numbers, _ = compute_engine_numbers(path)
            self.assertIn("合计本金", numbers)
            self.assertNotIn("未付货款本金", numbers)

    def test_invalid_sales_block_reports_reason_without_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _config(Path(tmp) / "case_config.json",
                           {**BASE_BLOCK, "loss_basis": "随便"})
            numbers, note = compute_engine_numbers(path)
            self.assertEqual(numbers, {})
            self.assertIn("货款口径参数不合法", note)

    def test_sales_config_without_loan_debts_still_computes(self) -> None:
        """买卖合同案由不需要借贷 debts：缺 debts 也必须能算出货款数字。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "case_config.json"
            path.write_text(json.dumps({
                "schema": "shadow-case-config-v1",
                "lpr_4x_monthly_rate": "0.01",
                "interest_cutoff": "2026-06-22",
                "debts": [],
                "sales_claim": BASE_BLOCK,
            }, ensure_ascii=False), encoding="utf-8")
            numbers, note = compute_engine_numbers(path)
            self.assertEqual(numbers["未付货款本金"], "10000.00")
            self.assertIn("模型未参与任何计算", note)

    def test_all_bases_are_documented(self) -> None:
        self.assertEqual(set(LOSS_BASE_IDS), {"LPR", "LPR_1_5", "AGREED", "NONE"})


if __name__ == "__main__":
    unittest.main()
