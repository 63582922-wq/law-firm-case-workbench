"""买卖合同（货款）口径的确定性计算：律师选口径，代码算数。

为什么单独一套：民间借贷的 LPR×4 上限、先息后本冲抵不能套到买卖合同的货款上。
买卖合同争议通常是「货款本金 + 逾期付款损失」，损失口径由**律师选择**，
引擎只负责按所选口径逐段计算并说明算法，不替律师决定该按哪一种主张：

- ``LPR``：按起算时一年期 LPR 计算；
- ``LPR_1_5``：按一年期 LPR 的 1.5 倍计算；
- ``AGREED``：按合同/对账单约定的年化违约金率计算（律师填数值）；
- ``NONE``：不主张逾期损失（只算本金）。

计算规则（全部写进报告口径说明，可复核）：

1. 本金：律师确认的应付货款 − 律师确认性质为「还本」的付款；
2. 逐段计息：还款会使本金减少，因此在每个还款日切开分段计算，段内按实际天数；
3. 日利率 = 年利率 ÷ 365（按实际天数、单利，不含复利）；
4. 「付息」类付款冲减已计算的逾期损失，冲减不为负；
5. 「争议」「排除」的付款不进入任何计算。

利率本身由律师填写（一年期 LPR、约定年化），引擎不联网取数、不猜测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Mapping, Sequence

CENT = Decimal("0.01")
DAY_DIVISOR = Decimal("365")

CLAIM_KIND_LOAN = "LOAN_INTEREST"
CLAIM_KIND_GOODS = "GOODS_PAYMENT"

LOSS_BASES: tuple[tuple[str, str, str], ...] = (
    ("LPR", "按一年期 LPR", "以逾期起算时的一年期 LPR 计算逾期付款损失"),
    ("LPR_1_5", "按一年期 LPR 的 1.5 倍", "以一年期 LPR × 1.5 计算"),
    ("AGREED", "按约定年化违约金率", "按合同或对账单约定的年化比率计算（需律师填写）"),
    ("NONE", "不主张逾期损失", "只计算货款本金，不计算逾期付款损失"),
)
LOSS_BASE_IDS = tuple(item[0] for item in LOSS_BASES)


class SalesClaimError(ValueError):
    """货款口径参数不合法：由调用方转成 422 或写进口径说明，绝不静默兜底。"""


def _money(value: object, label: str) -> Decimal:
    text = re.sub(r"[¥￥,，\s元]", "", str(value or ""))
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise SalesClaimError(f"{label}：金额应为数字（元，最多两位小数），当前为「{value}」")
    return Decimal(text)


def _rate(value: object, label: str) -> Decimal:
    text = re.sub(r"[%\s]", "", str(value or ""))
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise SalesClaimError(f"{label}：比率应为数字，当前为「{value}」") from None
    if number <= 0:
        raise SalesClaimError(f"{label}：比率必须大于 0")
    # 允许按百分比填写（3 表示 3%）；小数（0.03）同样接受
    return number / Decimal("100") if number >= Decimal("0.5") else number


def _day(value: object, label: str) -> date:
    text = str(value or "").strip().split(" ")[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SalesClaimError(f"{label}：日期格式应为 YYYY-MM-DD，当前为「{value}」") from None


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _canonical(value: Decimal) -> str:
    return f"{_q(value):.2f}"


@dataclass
class SalesPayment:
    paid_on: date
    amount: Decimal
    classification: str        # 还本 / 付息 / 争议 / 排除

    @property
    def reduces_principal(self) -> bool:
        return self.classification == "还本" and self.amount > 0

    @property
    def reduces_loss(self) -> bool:
        return self.classification == "付息" and self.amount > 0


@dataclass
class SalesClaim:
    """买卖合同的确定性计算输入（全部由律师填写/确认）。"""

    claim_amount: Decimal                 # 原告主张的货款
    confirmed_principal: Decimal          # 律师确认的应付货款
    overdue_from: date                    # 逾期起算日
    cutoff: date                          # 暂计截止日
    loss_basis: str = "LPR"
    lpr_annual: Decimal | None = None     # 一年期 LPR（年化，小数）
    agreed_annual: Decimal | None = None  # 约定年化违约金率（小数）
    payments: list[SalesPayment] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": CLAIM_KIND_GOODS,
            "claim_amount": _canonical(self.claim_amount),
            "confirmed_principal": _canonical(self.confirmed_principal),
            "overdue_from": self.overdue_from.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "loss_basis": self.loss_basis,
            **({"lpr_annual": str(self.lpr_annual)} if self.lpr_annual is not None else {}),
            **({"agreed_annual": str(self.agreed_annual)} if self.agreed_annual is not None else {}),
            "note": self.note,
        }


def load_sales_claim(value: Mapping | None) -> SalesClaim | None:
    """解析 case_config 里的货款口径；未配置或非货款案由返回 None。"""
    if not isinstance(value, Mapping):
        return None
    # 既接受外层 case_config（含 sales_claim 块），也接受已取出的货款块本身
    block = value.get("sales_claim")
    if not isinstance(block, Mapping):
        block = value if any(
            key in value for key in ("claim_amount", "confirmed_principal", "loss_basis")
        ) else None
    if not isinstance(block, Mapping):
        return None
    kind = str(block.get("kind") or CLAIM_KIND_GOODS)
    if kind != CLAIM_KIND_GOODS:
        return None

    basis = str(block.get("loss_basis") or "LPR").strip().upper()
    if basis not in LOSS_BASE_IDS:
        raise SalesClaimError(
            f"逾期损失口径必须为 {'/'.join(LOSS_BASE_IDS)} 之一，当前为「{block.get('loss_basis')}」")

    claim_amount = _money(block.get("claim_amount"), "原告主张货款")
    confirmed_principal = _money(
        block.get("confirmed_principal", claim_amount), "律师确认应付货款")
    overdue_from = _day(block.get("overdue_from"), "逾期起算日")
    cutoff = _day(block.get("cutoff"), "暂计截止日")
    if cutoff < overdue_from:
        raise SalesClaimError("暂计截止日不能早于逾期起算日")

    lpr_annual = None
    agreed_annual = None
    if basis in ("LPR", "LPR_1_5"):
        lpr_annual = _rate(block.get("lpr_annual"), "一年期 LPR（年化）")
    if basis == "AGREED":
        agreed_annual = _rate(block.get("agreed_annual"), "约定年化违约金率")

    payments: list[SalesPayment] = []
    for index, raw in enumerate(value.get("payments") or [], start=1):
        if not isinstance(raw, Mapping):
            raise SalesClaimError(f"第 {index} 笔付款格式不正确")
        classification = str(raw.get("classification") or "").strip()
        if classification not in ("还本", "付息", "争议", "排除"):
            raise SalesClaimError(f"第 {index} 笔付款性质不合法：{classification}")
        payments.append(SalesPayment(
            paid_on=_day(raw.get("paid_on"), f"第 {index} 笔付款日"),
            amount=_money(raw.get("amount"), f"第 {index} 笔付款金额"),
            classification=classification,
        ))
    payments.sort(key=lambda item: item.paid_on)

    return SalesClaim(
        claim_amount=claim_amount,
        confirmed_principal=confirmed_principal,
        overdue_from=overdue_from,
        cutoff=cutoff,
        loss_basis=basis,
        lpr_annual=lpr_annual,
        agreed_annual=agreed_annual,
        payments=payments,
        note=str(block.get("note") or "")[:200],
    )


def annual_rate(claim: SalesClaim) -> Decimal:
    """按所选口径给出年化利率；NONE 视为 0。"""
    if claim.loss_basis == "NONE":
        return Decimal("0")
    if claim.loss_basis == "LPR":
        return claim.lpr_annual or Decimal("0")
    if claim.loss_basis == "LPR_1_5":
        return (claim.lpr_annual or Decimal("0")) * Decimal("1.5")
    if claim.loss_basis == "AGREED":
        return claim.agreed_annual or Decimal("0")
    return Decimal("0")


def compute_sales_numbers(claim: SalesClaim) -> tuple[dict[str, str], str]:
    """逐段计算货款本金与逾期付款损失；返回 (数字, 口径说明)。"""
    rate = annual_rate(claim)
    daily = rate / DAY_DIVISOR
    basis_label = dict((item[0], item[1]) for item in LOSS_BASES)[claim.loss_basis]

    principal_payments = [item for item in claim.payments if item.reduces_principal]
    loss_payments = [item for item in claim.payments if item.reduces_loss]
    pending = [item for item in claim.payments
               if item.classification in ("争议", "排除")]

    total_paid_principal = sum((item.amount for item in principal_payments), Decimal("0"))
    unpaid_principal = max(Decimal("0"), claim.confirmed_principal - total_paid_principal)

    # 逐段计息：每个「还本」付款日切开，段内按实际天数单利
    segments: list[tuple[date, date, Decimal, Decimal]] = []
    loss = Decimal("0")
    cursor = claim.overdue_from
    balance = claim.confirmed_principal
    events = sorted(principal_payments + loss_payments, key=lambda item: item.paid_on)
    for event in events:
        if event.reduces_principal and cursor < event.paid_on <= claim.cutoff:
            days = (event.paid_on - cursor).days
            amount = _q(balance * daily * days)
            loss += amount
            segments.append((cursor, event.paid_on, balance, amount))
            balance = max(Decimal("0"), balance - event.amount)
            cursor = event.paid_on
    days = (claim.cutoff - cursor).days
    if days > 0:
        amount = _q(balance * daily * days)
        loss += amount
        segments.append((cursor, claim.cutoff, balance, amount))

    total_loss = _q(loss)
    loss_paid = sum((item.amount for item in loss_payments), Decimal("0"))
    net_loss = max(Decimal("0"), total_loss - loss_paid)

    numbers = {
        "原告主张货款": _canonical(claim.claim_amount),
        "律师确认应付货款": _canonical(claim.confirmed_principal),
        "已确认还本合计": _canonical(total_paid_principal),
        "未付货款本金": _canonical(unpaid_principal),
        "逾期付款损失（毛额）": _canonical(total_loss),
        "已确认付息合计": _canonical(loss_paid),
        "逾期付款损失（净额）": _canonical(net_loss),
        "本金与损失合计": _canonical(unpaid_principal + net_loss),
        "逾期起算日": claim.overdue_from.isoformat(),
        "暂计截止日": claim.cutoff.isoformat(),
        "损失口径": basis_label,
    }

    note = (
        f"以上数字由确定性引擎按律师填写的参数计算：损失口径为「{basis_label}」，"
        f"年化 {rate * 100:.4f}%，日利率 = 年利率÷365，按实际天数单利分段计算"
        f"（{len(segments)} 段，还款日切分）；模型未参与任何计算。"
    )
    if claim.loss_basis == "NONE":
        note = "律师选择不主张逾期付款损失，因此只计算货款本金；模型未参与任何计算。"
    if total_paid_principal:
        note += f" 已按律师确认为「还本」的 {len(principal_payments)} 笔付款冲减本金。"
    if loss_payments:
        note += f" 已按律师确认为「付息」的 {len(loss_payments)} 笔付款冲减逾期损失。"
    if pending:
        note += f" 另有 {len(pending)} 笔付款标记为争议/排除，未进入计算。"
    note += " 未付货款本金与逾期损失均以计算表为准，律师可改口径后重新计算。"
    return numbers, note
