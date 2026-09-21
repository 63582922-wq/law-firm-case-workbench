"""Build a lawyer-reviewable rate-anchor plan for private-lending transitions.

This is intentionally a planning boundary, not a calculator and not a legal
opinion.  It implements the event split exposed by the 2020 second revision's
transition provision: a newly accepted first-instance case after 2020-08-20
whose loan contract formed earlier needs an historical pre-boundary review and
then the filing-time LPR protection standard for the later period.  The plan
never invents the historical rate or a missing LPR observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class TransitionPlanBlocked(ValueError):
    """A fact pattern cannot be safely converted to a legal-rule candidate."""


class RateAnchor(str, Enum):
    HISTORICAL_PRIVATE_LENDING_REVIEW = "HISTORICAL_PRIVATE_LENDING_REVIEW"
    CLAIM_FILED_ONE_YEAR_LPR_X4 = "CLAIM_FILED_ONE_YEAR_LPR_X4"
    CONTRACT_FORMED_ONE_YEAR_LPR_X4 = "CONTRACT_FORMED_ONE_YEAR_LPR_X4"


class TransitionPlanStatus(str, Enum):
    AWAITING_LAWYER_RULE_APPROVAL = "AWAITING_LAWYER_RULE_APPROVAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class HistoricalInterestPayment:
    payment_date: date
    amount_cny: str
    classification_confirmed: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProposedRateSegment:
    start_date: date
    end_date: date
    rate_anchor: RateAnchor
    anchor_date: date | None
    required_source_provisions: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class PrivateLendingTransitionPlan:
    status: TransitionPlanStatus
    contract_formed_on: date | None
    claim_filed_on: date | None
    first_instance_accepted_on: date | None
    calculation_start: date
    calculation_end: date
    segments: tuple[ProposedRateSegment, ...]
    review_tasks: tuple[str, ...]
    blockers: tuple[str, ...]


_BOUNDARY = date(2020, 8, 20)


def plan_private_lending_interest_transition(
    *,
    contract_formed_on: date | None,
    claim_filed_on: date | None,
    first_instance_accepted_on: date | None,
    calculation_start: date,
    calculation_end: date,
    historical_interest_payments: tuple[HistoricalInterestPayment, ...] = (),
) -> PrivateLendingTransitionPlan:
    """Propose segments while preserving all legal facts that need review.

    ``contract_formed_on`` must be the lawyer-confirmed legal formation date,
    not blindly the promissory-note signature date.  For natural-person loans,
    the Civil Code formation rule normally makes actual provision of funds a
    material event to review.
    """
    if calculation_start >= calculation_end:
        raise TransitionPlanBlocked("calculation period must be a non-empty [start, end) interval")
    blockers = _missing_required_dates(
        contract_formed_on=contract_formed_on,
        claim_filed_on=claim_filed_on,
        first_instance_accepted_on=first_instance_accepted_on,
    )
    payment_tasks = _historical_payment_tasks(historical_interest_payments)
    if blockers:
        return PrivateLendingTransitionPlan(
            status=TransitionPlanStatus.BLOCKED,
            contract_formed_on=contract_formed_on,
            claim_filed_on=claim_filed_on,
            first_instance_accepted_on=first_instance_accepted_on,
            calculation_start=calculation_start,
            calculation_end=calculation_end,
            segments=(),
            review_tasks=payment_tasks,
            blockers=blockers,
        )

    assert contract_formed_on is not None
    assert claim_filed_on is not None
    assert first_instance_accepted_on is not None
    if claim_filed_on > first_instance_accepted_on:
        return _blocked_plan(
            contract_formed_on, claim_filed_on, first_instance_accepted_on,
            calculation_start, calculation_end, payment_tasks,
            "一审受理日早于起诉日的事实记录不一致，须先核实。",
        )
    if first_instance_accepted_on == _BOUNDARY:
        return _blocked_plan(
            contract_formed_on, claim_filed_on, first_instance_accepted_on,
            calculation_start, calculation_end, payment_tasks,
            "一审受理日恰为 2020-08-20；过渡条款使用“之后”，须由律师核实受理时点和适用版本。",
        )
    if first_instance_accepted_on < _BOUNDARY:
        return PrivateLendingTransitionPlan(
            status=TransitionPlanStatus.AWAITING_LAWYER_RULE_APPROVAL,
            contract_formed_on=contract_formed_on,
            claim_filed_on=claim_filed_on,
            first_instance_accepted_on=first_instance_accepted_on,
            calculation_start=calculation_start,
            calculation_end=calculation_end,
            segments=(
                ProposedRateSegment(
                    calculation_start, calculation_end,
                    RateAnchor.HISTORICAL_PRIVATE_LENDING_REVIEW, None,
                    ("PRIVATE_LENDING_2015_ARTICLE_26", "PRIVATE_LENDING_2015_ARTICLE_31"),
                    "一审受理早于 2020-08-20；不得直接改用 LPR 四倍，需由律师选择并批准历史规则。",
                ),
            ),
            review_tasks=payment_tasks,
            blockers=(),
        )

    if contract_formed_on < _BOUNDARY:
        segments = _pre_boundary_contract_segments(
            calculation_start=calculation_start,
            calculation_end=calculation_end,
            claim_filed_on=claim_filed_on,
        )
        return PrivateLendingTransitionPlan(
            status=TransitionPlanStatus.AWAITING_LAWYER_RULE_APPROVAL,
            contract_formed_on=contract_formed_on,
            claim_filed_on=claim_filed_on,
            first_instance_accepted_on=first_instance_accepted_on,
            calculation_start=calculation_start,
            calculation_end=calculation_end,
            segments=segments,
            review_tasks=payment_tasks + (
                "本计划仅提出时间分段；历史规则、起诉时一年期 LPR 原始记录和四倍上限均须经律师批准后才能进入正式计算。",
            ),
            blockers=(),
        )

    return PrivateLendingTransitionPlan(
        status=TransitionPlanStatus.AWAITING_LAWYER_RULE_APPROVAL,
        contract_formed_on=contract_formed_on,
        claim_filed_on=claim_filed_on,
        first_instance_accepted_on=first_instance_accepted_on,
        calculation_start=calculation_start,
        calculation_end=calculation_end,
        segments=(
            ProposedRateSegment(
                calculation_start, calculation_end,
                RateAnchor.CONTRACT_FORMED_ONE_YEAR_LPR_X4, contract_formed_on,
                ("PRIVATE_LENDING_ARTICLE_25",),
                "合同成立于 2020-08-20 当日或之后；候选上限锚定合同成立时的一年期 LPR，而非按年度滚动替换。",
            ),
        ),
        review_tasks=payment_tasks + (
            "须将合同成立时的一年期 LPR 原始记录、四倍公式和律师批准的规则版本绑定到正式计算。",
        ),
        blockers=(),
    )


def _pre_boundary_contract_segments(*, calculation_start: date, calculation_end: date, claim_filed_on: date) -> tuple[ProposedRateSegment, ...]:
    segments: list[ProposedRateSegment] = []
    if calculation_start < _BOUNDARY:
        segments.append(
            ProposedRateSegment(
                calculation_start, min(calculation_end, _BOUNDARY),
                RateAnchor.HISTORICAL_PRIVATE_LENDING_REVIEW, None,
                ("PRIVATE_LENDING_2015_ARTICLE_26", "PRIVATE_LENDING_2015_ARTICLE_31"),
                "合同成立至 2020-08-19 的期间保持历史规则审查；已付利息不得被自动当作本金冲抵。",
            )
        )
    if calculation_end > _BOUNDARY:
        segments.append(
            ProposedRateSegment(
                max(calculation_start, _BOUNDARY), calculation_end,
                RateAnchor.CLAIM_FILED_ONE_YEAR_LPR_X4, claim_filed_on,
                ("PRIVATE_LENDING_ARTICLE_25", "PRIVATE_LENDING_ARTICLE_31"),
                "2020-08-20 起的候选上限锚定原告起诉时的一年期 LPR 四倍；不按每年 LPR 自动滚动。",
            )
        )
    return tuple(segments)


def _missing_required_dates(*, contract_formed_on: date | None, claim_filed_on: date | None, first_instance_accepted_on: date | None) -> tuple[str, ...]:
    missing: list[str] = []
    if contract_formed_on is None:
        missing.append("缺少经律师确认的合同成立日；不得仅以借条签署日替代。")
    if claim_filed_on is None:
        missing.append("缺少原告起诉日，无法确定过渡规则中的起诉时 LPR 锚点。")
    if first_instance_accepted_on is None:
        missing.append("缺少一审受理日，无法判断是否属于 2020-08-20 后新受理案件。")
    return tuple(missing)


def _historical_payment_tasks(payments: tuple[HistoricalInterestPayment, ...]) -> tuple[str, ...]:
    tasks: list[str] = []
    for payment in payments:
        if payment.payment_date < _BOUNDARY and not payment.classification_confirmed:
            tasks.append(
                f"{payment.payment_date.isoformat()} 的历史付款尚未确认性质；须区分已付利息、本金和其他款项后再作冲抵。"
            )
        if payment.payment_date < _BOUNDARY and not payment.evidence_ids:
            tasks.append(f"{payment.payment_date.isoformat()} 的历史付款缺少证据关联，不能用于历史利息处理。")
    return tuple(tasks)


def _blocked_plan(contract_formed_on: date, claim_filed_on: date, first_instance_accepted_on: date, calculation_start: date, calculation_end: date, payment_tasks: tuple[str, ...], blocker: str) -> PrivateLendingTransitionPlan:
    return PrivateLendingTransitionPlan(
        status=TransitionPlanStatus.BLOCKED,
        contract_formed_on=contract_formed_on,
        claim_filed_on=claim_filed_on,
        first_instance_accepted_on=first_instance_accepted_on,
        calculation_start=calculation_start,
        calculation_end=calculation_end,
        segments=(),
        review_tasks=payment_tasks,
        blockers=(blocker,),
    )
