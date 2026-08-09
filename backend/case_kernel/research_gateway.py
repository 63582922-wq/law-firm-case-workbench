"""Policy boundary for public legal research.

The gateway is intentionally not a general web browser. It prepares and records
only approved, minimized queries against registered public sources. A later
adapter can perform the network fetch after this boundary grants it a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from urllib.parse import urlparse
from uuid import uuid4


class ResearchBlocked(ValueError):
    """A research request violates the public-research policy."""


@dataclass(frozen=True)
class PublicSource:
    source_id: str
    publisher: str
    source_tier: str
    allowed_domains: frozenset[str]
    topics: frozenset[str]


@dataclass(frozen=True)
class ResearchPlan:
    plan_id: str
    issue: str
    minimized_query: str
    candidate_source_ids: tuple[str, ...]
    generated_at: datetime


@dataclass(frozen=True)
class ExternalResearchRequest:
    request_id: str
    plan_id: str
    source_id: str
    target_url: str
    query_hash: str
    requested_by: str
    authorized_at: datetime


@dataclass(frozen=True)
class ExternalResearchReceipt:
    request_id: str
    response_sha256: str
    retrieved_at: datetime
    source_url: str


PUBLIC_SOURCES: tuple[PublicSource, ...] = (
    PublicSource(
        source_id="LAW-CN-CIVIL-CODE-680",
        publisher="最高人民法院 / 国家法律法规数据库",
        source_tier="PRIMARY_LAW",
        allowed_domains=frozenset({"court.gov.cn", "www.court.gov.cn", "flk.npc.gov.cn", "wb.flk.npc.gov.cn"}),
        topics=frozenset({"民法典", "借款利息", "高利放贷"}),
    ),
    PublicSource(
        source_id="JI-MINJIAN-2020-SECOND",
        publisher="国家法律法规数据库 / 最高人民法院",
        source_tier="JUDICIAL_INTERPRETATION",
        allowed_domains=frozenset({"court.gov.cn", "www.court.gov.cn", "gongbao.court.gov.cn", "wb.flk.npc.gov.cn"}),
        topics=frozenset({"民间借贷", "利率保护", "过渡规则", "预扣利息", "逾期利息"}),
    ),
    PublicSource(
        source_id="RATE-LPR-HISTORY",
        publisher="全国银行间同业拆借中心",
        source_tier="OFFICIAL_RATE_DATA",
        allowed_domains=frozenset({"shibor.org", "www.shibor.org", "chinamoney.com.cn", "www.chinamoney.com.cn"}),
        topics=frozenset({"LPR", "贷款市场报价利率", "一年期贷款市场报价利率"}),
    ),
    PublicSource(
        source_id="CASE-PUBLIC-RESEARCH",
        publisher="最高人民法院及依法公开裁判文书站点",
        source_tier="PUBLIC_CASE_RESEARCH",
        allowed_domains=frozenset({"court.gov.cn", "www.court.gov.cn", "gongbao.court.gov.cn", "cicc.court.gov.cn", "pccz.court.gov.cn"}),
        topics=frozenset({"民间借贷", "证据", "本金", "已付利息"}),
    ),
)


# These patterns are guardrails, not a claim to identify every possible personal datum.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CN_MOBILE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("CN_ID", re.compile(r"(?<!\d)\d{17}[\dXx](?![\dA-Za-z])")),
    ("BANK_OR_ACCOUNT_NUMBER", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)


class PublicResearchGateway:
    """Creates reviewable research plans and external request receipts in memory."""

    def __init__(self, sources: tuple[PublicSource, ...] = PUBLIC_SOURCES) -> None:
        self._sources = {source.source_id: source for source in sources}
        self._plans: dict[str, ResearchPlan] = {}
        self._requests: dict[str, ExternalResearchRequest] = {}
        self._receipts: dict[str, ExternalResearchReceipt] = {}

    def prepare_plan(self, *, issue: str, proposed_query: str) -> ResearchPlan:
        normalized_issue = _required_text(issue, "issue")
        query = _required_text(proposed_query, "proposed_query")
        _raise_if_sensitive(query)
        candidates = tuple(
            source.source_id
            for source in self._sources.values()
            if _topic_matches(source.topics, f"{normalized_issue} {query}")
        )
        if not candidates:
            raise ResearchBlocked("no registered public source matches this research issue")
        plan = ResearchPlan(
            plan_id=f"research_plan_{uuid4().hex}",
            issue=normalized_issue,
            minimized_query=query,
            candidate_source_ids=candidates,
            generated_at=datetime.now(timezone.utc),
        )
        self._plans[plan.plan_id] = plan
        return plan

    def authorize_public_request(
        self,
        *,
        plan_id: str,
        source_id: str,
        target_url: str,
        requested_by: str,
        lawyer_confirmed: bool,
    ) -> ExternalResearchRequest:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise ResearchBlocked("unknown research plan")
        if not lawyer_confirmed:
            raise ResearchBlocked("a lawyer must explicitly authorize public research")
        if source_id not in plan.candidate_source_ids:
            raise ResearchBlocked("source is not a candidate for this research plan")
        source = self._sources[source_id]
        host = urlparse(target_url).hostname
        if urlparse(target_url).scheme != "https" or host not in source.allowed_domains:
            raise ResearchBlocked("target URL is not an approved HTTPS source domain")
        actor = _required_text(requested_by, "requested_by")
        request = ExternalResearchRequest(
            request_id=f"external_research_{uuid4().hex}",
            plan_id=plan.plan_id,
            source_id=source_id,
            target_url=target_url,
            query_hash=sha256(plan.minimized_query.encode("utf-8")).hexdigest(),
            requested_by=actor,
            authorized_at=datetime.now(timezone.utc),
        )
        self._requests[request.request_id] = request
        return request

    def record_response(self, *, request_id: str, source_url: str, response_body: bytes) -> ExternalResearchReceipt:
        request = self._requests.get(request_id)
        if request is None:
            raise ResearchBlocked("unknown external research request")
        if source_url != request.target_url:
            raise ResearchBlocked("response URL does not match the authorized request")
        if not response_body:
            raise ResearchBlocked("empty public-source response cannot be cited")
        receipt = ExternalResearchReceipt(
            request_id=request_id,
            response_sha256=sha256(response_body).hexdigest(),
            retrieved_at=datetime.now(timezone.utc),
            source_url=source_url,
        )
        self._receipts[request_id] = receipt
        return receipt

    def request(self, request_id: str) -> ExternalResearchRequest:
        return self._requests[request_id]

    def receipt(self, request_id: str) -> ExternalResearchReceipt:
        return self._receipts[request_id]


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ResearchBlocked(f"{field_name} is required")
    return normalized


def _raise_if_sensitive(query: str) -> None:
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(query):
            raise ResearchBlocked(f"public research query contains blocked sensitive pattern: {label}")


def _topic_matches(topics: frozenset[str], full_query: str) -> bool:
    return any(topic.lower() in full_query.lower() for topic in topics)
