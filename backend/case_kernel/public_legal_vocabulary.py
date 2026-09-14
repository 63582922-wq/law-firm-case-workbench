"""Server-owned vocabulary for privacy-minimized public legal research.

Case text, lawyer instructions and model output are never emitted as public
query terms.  The only strings this module may return are exact entries from
the fixed vocabulary below.  This keeps query minimisation deterministic and
lets the planner, research adapter and durable network binding share one
auditable policy instead of slowly drifting apart.
"""

from __future__ import annotations

from typing import Iterable


PUBLIC_LEGAL_VOCABULARY: tuple[str, ...] = (
    # Contract and civil obligations.
    "民间借贷",
    "借款合同",
    "买卖合同",
    "服务合同",
    "承揽合同",
    "租赁合同",
    "合同成立",
    "合同效力",
    "合同履行",
    "合同解除",
    "违约责任",
    "损害赔偿",
    "继续履行",
    "货款",
    "付款义务",
    "交付",
    "验收",
    "质量异议",
    "格式条款",
    "代理权",
    "无权代理",
    # Lending, security and deterministic amount inputs.
    "利息",
    "利率保护上限",
    "逾期利息",
    "复利",
    "还款抵扣",
    "本金",
    "担保",
    "保证期间",
    "抵押权",
    "质权",
    # Procedure and proof.
    "诉讼时效",
    "举证责任",
    "证据规则",
    "电子数据",
    "管辖",
    "送达",
    "财产保全",
    "反诉",
    "诉讼请求",
    "证明标准",
    # Authority and temporal-effect controls.
    "民法典",
    "民事诉讼法",
    "司法解释",
    "法律规定",
    "过渡规则",
    "生效时间",
    "人民法院案例库",
    "指导性案例",
)

_PUBLIC_LEGAL_TERM_SET = frozenset(PUBLIC_LEGAL_VOCABULARY)
_FALLBACK_TERMS = ("法律规定", "司法解释")


def derive_public_legal_terms(
    texts: Iterable[str], *, maximum: int = 12
) -> tuple[str, ...]:
    """Return only fixed legal concepts found in private text.

    The function does not tokenize or paraphrase private input.  It performs
    exact substring membership against a server-owned dictionary and emits
    only dictionary entries.  More specific concepts sort before shorter
    concepts so a bounded query keeps the most useful terms.
    """

    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 2 <= maximum <= 24:
        raise ValueError("public legal term limit is invalid")
    values: set[str] = set()
    for raw in texts:
        if not isinstance(raw, str):
            raise ValueError("public legal term source must be text")
        if len(raw) > 100_000:
            raise ValueError("public legal term source is oversized")
        values.update(term for term in PUBLIC_LEGAL_VOCABULARY if term in raw)
    values.update(_FALLBACK_TERMS)
    return tuple(sorted(values, key=lambda item: (-len(item), item))[:maximum])


def validate_public_legal_terms(
    values: Iterable[str], *, minimum: int = 2, maximum: int = 24
) -> tuple[str, ...]:
    """Validate a canonical, unique subset of the fixed vocabulary."""

    terms = tuple(values)
    if (
        not minimum <= len(terms) <= maximum
        or len(terms) != len(set(terms))
        or any(term not in _PUBLIC_LEGAL_TERM_SET for term in terms)
        or tuple(sorted(terms, key=lambda item: (-len(item), item))) != terms
    ):
        raise ValueError("public legal terms are not a canonical safe subset")
    return terms


__all__ = (
    "PUBLIC_LEGAL_VOCABULARY",
    "derive_public_legal_terms",
    "validate_public_legal_terms",
)
