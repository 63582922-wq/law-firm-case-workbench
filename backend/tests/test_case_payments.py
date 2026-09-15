"""律师确认的付款性质 → 引擎净额：契约、校验与失效测试。"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.case_analysis_service import compute_engine_numbers
from case_kernel.case_payments import (
    PAYMENT_CLASSES,
    PaymentError,
    load_payments,
    payment_rows,
    payment_summary,
)


def _config(path: Path, payments: list[dict] | None = None) -> Path:
    payload = {
        "schema": "shadow-case-config-v1",
        "lpr_4x_monthly_rate": "0.01",
        "interest_cutoff": "2025-06-14",
        "debts": [
            {"debt_id": "L1", "principal": "100000.00", "disbursed_on": "2019-10-19",
             "agreed_monthly_rate": "0.015", "due_on": "2019-12-19"},
        ],
    }
    if payments is not None:
        payload["payments"] = payments
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


class PaymentContractTests(unittest.TestCase):
    def test_classifications_and_parsing(self) -> None:
        payments = load_payments([
            {"payment_id": "P1", "paid_on": "2020-01-20", "amount": "2,250.00",
             "classification": "付息", "debt_id": "L1"},
            {"payment_id": "P2", "paid_on": "2020-02-20", "amount": "2250元",
             "classification": "还本"},
            {"payment_id": "P3", "paid_on": "2020-03-20", "amount": "2250",
             "classification": "争议"},
            {"payment_id": "P4", "paid_on": "2020-04-20", "amount": "2250",
             "classification": "排除"},
        ])
        self.assertEqual([item.classification for item in payments],
                         ["付息", "还本", "争议", "排除"])
        self.assertEqual(payments[0].amount, Decimal("2250.00"))
        self.assertTrue(payments[0].enters_calculation)
        self.assertTrue(payments[1].enters_calculation)
        self.assertFalse(payments[2].enters_calculation)  # 争议不进计算
        self.assertFalse(payments[3].enters_calculation)  # 排除不进计算
        summary = payment_summary(payments)
        self.assertEqual(summary["confirmed_count"], 2)
        self.assertEqual(summary["confirmed_total"], "4500.00")
        self.assertEqual(summary["pending_count"], 2)
        # 只有确认的付款生成引擎行
        self.assertEqual([row.row_id for row in payment_rows(payments)], ["PAY-P1", "PAY-P2"])

    def test_invalid_payments_are_rejected_not_dropped(self) -> None:
        cases = [
            ([{"payment_id": "P1", "paid_on": "2020-13-40", "amount": "2250",
               "classification": "付息"}], "日期格式"),
            ([{"payment_id": "P1", "paid_on": "2020-01-20", "amount": "abc",
               "classification": "付息"}], "金额"),
            ([{"payment_id": "P1", "paid_on": "2020-01-20", "amount": "2250",
               "classification": "还款"}], "性质"),
            ([{"payment_id": "P1", "paid_on": "2020-01-20", "amount": "2250",
               "classification": "付息"},
              {"payment_id": "P1", "paid_on": "2020-02-20", "amount": "2250",
               "classification": "付息"}], "重复"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(PaymentError) as caught:
                    load_payments(payload)
                self.assertIn(expected, str(caught.exception))

    def test_empty_or_missing_payments(self) -> None:
        self.assertEqual(load_payments(None), [])
        self.assertEqual(load_payments({"payments": []}), [])
        self.assertEqual(load_payments([]), [])


class NetNumbersTests(unittest.TestCase):
    def test_gross_only_without_payments(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp) / "case_config.json")
            numbers, note = compute_engine_numbers(config)
            self.assertIn("合计本金", numbers)
            self.assertNotIn("冲抵后合计本金", numbers)
            self.assertIn("毛额", note)
            self.assertIn("须待律师确认各笔付款性质", note)

    def test_confirmed_payments_reduce_principal_and_interest(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp) / "case_config.json", payments=[
                {"payment_id": "P1", "paid_on": "2020-01-20", "amount": "2250.00",
                 "classification": "付息", "debt_id": "L1"},
                {"payment_id": "P2", "paid_on": "2020-06-20", "amount": "10000.00",
                 "classification": "还本", "debt_id": "L1"},
            ])
            numbers, note = compute_engine_numbers(config)
            gross_principal = Decimal(numbers["合计本金"])
            gross_interest = Decimal(numbers["合计未付利息挂账"])
            net_principal = Decimal(numbers["冲抵后合计本金"])
            net_interest = Decimal(numbers["冲抵后合计未付利息挂账"])
            self.assertEqual(numbers["已确认付款合计"], "12250.00")
            self.assertEqual(numbers["已确认付款笔数"], "2")
            # 法定顺序先冲利息、后冲本金：本笔冲抵尚未触及本金
            self.assertEqual(net_principal, gross_principal)
            self.assertLess(net_interest, gross_interest)
            self.assertIn("先冲利息、后冲本金", note)

    def test_disputed_and_excluded_payments_never_enter_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config(Path(tmp) / "case_config.json", payments=[
                {"payment_id": "P1", "paid_on": "2020-01-20", "amount": "5000.00",
                 "classification": "争议", "debt_id": "L1"},
                {"payment_id": "P2", "paid_on": "2020-02-20", "amount": "5000.00",
                 "classification": "排除", "debt_id": "L1"},
            ])
            numbers, note = compute_engine_numbers(config)
            self.assertNotIn("冲抵后合计本金", numbers)
            self.assertNotIn("已确认付款合计", numbers)
            self.assertIn("2 笔付款标记为争议/排除，未进入计算", note)

    def test_invalid_payment_does_not_block_gross_numbers(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _config(Path(tmp) / "case_config.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["payments"] = [{"payment_id": "P1", "paid_on": "2020-01-20",
                                    "amount": "2250", "classification": "乱写"}]
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            numbers, note = compute_engine_numbers(path)
            self.assertIn("合计本金", numbers)          # 毛额只由债务参数决定，仍然可用
            self.assertNotIn("冲抵后合计本金", numbers)  # 但绝不给出来路不明的净额
            self.assertIn("付款记录不合法", note)


if __name__ == "__main__":
    unittest.main()
