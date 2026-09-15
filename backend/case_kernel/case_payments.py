"""律师确认的付款性质：付款进入确定性计算的唯一入口。

纪律（与实用模式一致）：

- 付款**只有经律师逐笔确认性质**后才进入计算：还本 / 付息 / 代付 三类进入引擎；
  「争议」「排除」明确不进计算，只在账本与报告中列出，绝不静默当作还款；
- 金额、日期、归属借款全部由律师填写或核对，模型不得写入；
- 未确认性质的付款不进入任何正式数字，正文只写「待律师确认付款性质」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping, Sequence

from case_kernel.shadow_engine import (
    CLASS_DISPUTED,
    CLASS_EXCLUDE,
    CLASS_INTEREST,
    CLASS_REPAY,
    CLASS_THIRD_PARTY,
    ShadowRow,
)

# 进入计算的分类（律师确认后才可选用）
CONFIRMED_CLASSES: tuple[str, ...] = (CLASS_REPAY, CLASS_INTEREST, CLASS_THIRD_PARTY)
# 全部可选分类（含明确不进计算的两种）
PAYMENT_CLASSES: tuple[str, ...] = (*CONFIRMED_CLASSES, CLASS_DISPUTED, CLASS_EXCLUDE)

_AMOUNT_RE = re.compile(r"^-?\d+(?:\.\d{1,2})?$")


class PaymentError(ValueError):
    """付款记录不合法：由调用方转成 422 或引擎阻断，不做静默兜底。"""


@dataclass(frozen=True)
class PaymentConfirmation:
    payment_id: str
    paid_on: date
    amount: Decimal
    classification: str
    debt_id: str | None = None
    memo: str = ""

    @property
    def enters_calculation(self) -> bool:
        return self.classification in CONFIRMED_CLASSES and self.amount > 0

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "paid_on": self.paid_on.isoformat(),
            "amount": f"{self.amount:.2f}",
            "classification": self.classification,
            **({"debt_id": self.debt_id} if self.debt_id else {}),
            **({"memo": self.memo} if self.memo else {}),
        }


def _parse_date(value: object, label: str) -> date:
    text = str(value or "").strip().split(" ")[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PaymentError(f"{label}：日期格式应为 YYYY-MM-DD，当前为「{value}」") from None


def _parse_amount(value: object, label: str) -> Decimal:
    text = re.sub(r"[¥￥,，\s元]", "", str(value or ""))
    if not _AMOUNT_RE.match(text):
        raise PaymentError(f"{label}：金额应为数字（元，最多两位小数），当前为「{value}」")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise PaymentError(f"{label}：金额无法解析：「{value}」") from None


def load_payments(value: Mapping | Sequence | None) -> list[PaymentConfirmation]:
    """解析律师确认的付款清单；任何一条不合法即整体拒绝（fail closed）。"""
    if value is None:
        return []
    if isinstance(value, Mapping):
        raw_items = value.get("payments") or []
    else:
        raw_items = value
    if not isinstance(raw_items, (list, tuple)):
        raise PaymentError("付款清单格式应为数组")
    payments: list[PaymentConfirmation] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise PaymentError(f"第 {index + 1} 笔付款格式应为对象")
        label = f"第 {index + 1} 笔付款"
        payment_id = str(item.get("payment_id") or f"P{index + 1}").strip()[:40]
        if not payment_id:
            raise PaymentError(f"{label}：缺少付款编号")
        if payment_id in seen:
            raise PaymentError(f"{label}：付款编号重复（{payment_id}）")
        seen.add(payment_id)
        classification = str(item.get("classification") or "").strip()
        if classification not in PAYMENT_CLASSES:
            raise PaymentError(
                f"{label}：性质必须为 {'/'.join(PAYMENT_CLASSES)} 之一，当前为「{classification}」")
        debt_id = str(item.get("debt_id") or "").strip()[:40] or None
        payments.append(PaymentConfirmation(
            payment_id=payment_id,
            paid_on=_parse_date(item.get("paid_on"), label),
            amount=_parse_amount(item.get("amount"), label),
            classification=classification,
            debt_id=debt_id,
            memo=str(item.get("memo") or "").strip()[:200],
        ))
    return payments


def payment_rows(payments: Sequence[PaymentConfirmation]) -> list[ShadowRow]:
    """把进入计算的付款转成引擎行；争议/排除不生成行。"""
    rows: list[ShadowRow] = []
    for payment in payments:
        if not payment.enters_calculation:
            continue
        rows.append(ShadowRow(
            row_id=f"PAY-{payment.payment_id}",
            occurred_on=payment.paid_on,
            channel="律师确认",
            amount=payment.amount,
            currency="CNY",
            direction="还款",
            classification=payment.classification,
            debt_id=payment.debt_id,
            memo=payment.memo or f"{payment.payment_id} {payment.classification}（律师确认）",
        ))
    return rows


def payment_summary(payments: Sequence[PaymentConfirmation]) -> dict:
    """按性质汇总，用于报告与界面说明（不参与计算）。"""
    confirmed = [item for item in payments if item.enters_calculation]
    disputed = [item for item in payments if item.classification == CLASS_DISPUTED]
    excluded = [item for item in payments if item.classification == CLASS_EXCLUDE]
    total = sum((item.amount for item in confirmed), Decimal("0"))
    return {
        "count": len(payments),
        "confirmed_count": len(confirmed),
        "confirmed_total": f"{total:.2f}",
        "disputed_count": len(disputed),
        "excluded_count": len(excluded),
        "pending_count": len(disputed) + len(excluded),
    }
