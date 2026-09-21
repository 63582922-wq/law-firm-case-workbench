"""Deterministic, review-only legal research plan for the lawyer Agent.

This is the missing bridge between detecting that law is unverified and
performing any external research.  It never accesses the network and never
creates a legal conclusion.  It organises current governed case objects into
bounded research questions, emits only server-whitelisted public query terms,
and records the approvals still required before search, official-byte capture,
legal-effect review or case application.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Iterable, Mapping

from .case_agent_case_context import (
    BoundCaseContextProjection,
    BoundCaseContextSource,
    CaseContextSourceType,
)
from .case_agent_legal_research_contract import (
    LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
    LEGAL_RESEARCH_PLAN_REVIEW_STATUS,
    LEGAL_RESEARCH_PLAN_SCHEMA,
    LEGAL_RESEARCH_PLANNING_SKILL_ID,
    LEGAL_RESEARCH_PLANNING_TOOL_ID,
)
from .case_agent_planner import PlanningInputStatus
from .public_legal_vocabulary import (
    derive_public_legal_terms,
    validate_public_legal_terms,
)


_MAX_QUESTIONS = 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_ID_RE = re.compile(r"^legal-research-[0-9a-f]{24}$")
_QUESTION_STATUS = frozenset(
    {"READY_FOR_LAWYER_APPROVAL", "NEEDS_ISSUE_CONFIRMATION"}
)
_SOURCE_CLASSES = (
    "NATIONAL_LAWS_DATABASE",
    "SUPREME_PEOPLES_COURT",
    "PEOPLES_COURT_CASE_DATABASE",
)
_REQUIRED_CHECKS = (
    "确认发布机关与官方域名",
    "确认现行效力与历史版本",
    "确认生效、失效和过渡期间",
    "定位到条、款、项或案例编号",
    "区分法源原文、解释和本案适用",
    "记录相反解释与剩余风险",
)


class LegalResearchPlanBlocked(ValueError):
    """The current governed projection cannot form a safe research plan."""


def build_legal_research_plan_candidate(
    projection: BoundCaseContextProjection,
) -> tuple[bytes, str]:
    if not isinstance(projection, BoundCaseContextProjection):
        raise LegalResearchPlanBlocked("legal research projection is invalid")
    projection.validate()
    source_hash = _canonical_hash(
        {
            "schema_version": "agent-legal-research-plan-source-set-v1",
            "case_snapshot_hash": projection.case_snapshot_hash,
            "matter_version": projection.matter_version,
            "sources": [_source_identity(item) for item in projection.sources],
        }
    )
    anchors = _research_anchors(projection.sources)
    posture_refs = tuple(
        item.input_ref
        for item in projection.sources
        if item.source_type is CaseContextSourceType.POSTURE_PROFILE
    )
    if len(posture_refs) != 1:
        raise LegalResearchPlanBlocked(
            "legal research planning requires the exact current posture"
        )
    all_terms = derive_public_legal_terms(
        (
            text
            for source in projection.sources
            for text in (source.primary_text, source.secondary_text)
        )
    )
    questions = []
    for anchor in anchors:
        refs = tuple(dict.fromkeys((posture_refs[0], anchor.input_ref)))
        formal_issue = (
            anchor.source_type is CaseContextSourceType.DISPUTE_ISSUE
            and anchor.status is PlanningInputStatus.CONFIRMED
        )
        terms = derive_public_legal_terms(
            (anchor.primary_text, anchor.secondary_text, *all_terms)
        )
        questions.append(
            {
                "question_id": _stable_id(anchor.input_ref, anchor.content_hash),
                "title": _question_title(anchor, formal_issue=formal_issue),
                "private_question": _private_question(anchor, formal_issue=formal_issue),
                "status": (
                    "READY_FOR_LAWYER_APPROVAL"
                    if formal_issue
                    else "NEEDS_ISSUE_CONFIRMATION"
                ),
                "proposed_public_terms": list(terms),
                "official_source_classes": list(_SOURCE_CLASSES),
                "required_checks": list(_REQUIRED_CHECKS),
                "requires_lawyer_approval": True,
                "source_refs": list(refs),
            }
        )
    ready = sum(
        item["status"] == "READY_FOR_LAWYER_APPROVAL" for item in questions
    )
    payload = {
        "schema_version": LEGAL_RESEARCH_PLAN_SCHEMA,
        "task_input_hash": projection.task_input_hash,
        "source_hash": source_hash,
        "review_status": LEGAL_RESEARCH_PLAN_REVIEW_STATUS,
        "formal_fact": False,
        "formal_transaction": False,
        "legal_conclusion": False,
        "evidence_decision": False,
        "court_ready": False,
        "network_access": False,
        "headline": (
            f"已形成{len(questions)}项官方法源研究问题；"
            f"{ready}项可在律师批准后进入脱敏检索。"
        ),
        "source_refs": list(projection.input_refs),
        "public_terms": list(all_terms),
        "questions": questions,
        "controls": {
            "external_search_separate_approval": True,
            "search_leads_are_not_authority": True,
            "official_bytes_require_separate_capture": True,
            "legal_effect_requires_lawyer_review": True,
            "case_application_requires_lawyer_decision": True,
        },
        "summary_counts": {
            "questions": len(questions),
            "ready_for_search_approval": ready,
            "needs_issue_confirmation": len(questions) - ready,
            "verified_authority_sources": sum(
                item.source_type is CaseContextSourceType.VERIFIED_LEGAL_SOURCE
                for item in projection.sources
            ),
        },
    }
    encoded = _json_bytes(payload)
    if len(encoded) > 4 * 1024 * 1024:
        raise LegalResearchPlanBlocked("legal research plan exceeds hard limit")
    parse_legal_research_plan_candidate(encoded)
    return encoded, source_hash


def parse_legal_research_plan_candidate(payload: bytes) -> Mapping[str, object]:
    if not isinstance(payload, bytes) or not 2 <= len(payload) <= 4 * 1024 * 1024:
        raise LegalResearchPlanBlocked("legal research plan size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LegalResearchPlanBlocked("legal research plan is invalid JSON") from None
    if not isinstance(value, dict) or _json_bytes(value) != payload:
        raise LegalResearchPlanBlocked("legal research plan is not canonical")
    expected = {
        "schema_version",
        "task_input_hash",
        "source_hash",
        "review_status",
        "formal_fact",
        "formal_transaction",
        "legal_conclusion",
        "evidence_decision",
        "court_ready",
        "network_access",
        "headline",
        "source_refs",
        "public_terms",
        "questions",
        "controls",
        "summary_counts",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != LEGAL_RESEARCH_PLAN_SCHEMA
        or value.get("review_status") != LEGAL_RESEARCH_PLAN_REVIEW_STATUS
        or any(
            value.get(field) is not False
            for field in (
                "formal_fact",
                "formal_transaction",
                "legal_conclusion",
                "evidence_decision",
                "court_ready",
                "network_access",
            )
        )
        or not _is_hash(value.get("task_input_hash"))
        or not _is_hash(value.get("source_hash"))
        or not _is_text(value.get("headline"), 240)
    ):
        raise LegalResearchPlanBlocked("legal research plan control fields changed")
    source_refs = _source_refs(value.get("source_refs"), minimum=1, maximum=500)
    public_terms = _public_terms(value.get("public_terms"))
    questions = value.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise LegalResearchPlanBlocked("legal research question count is invalid")
    seen_ids: set[str] = set()
    ready = 0
    cited: set[str] = set()
    for question in questions:
        if not isinstance(question, dict) or set(question) != {
            "question_id",
            "title",
            "private_question",
            "status",
            "proposed_public_terms",
            "official_source_classes",
            "required_checks",
            "requires_lawyer_approval",
            "source_refs",
        }:
            raise LegalResearchPlanBlocked("legal research question fields changed")
        question_id = question.get("question_id")
        if (
            not isinstance(question_id, str)
            or _ID_RE.fullmatch(question_id) is None
            or question_id in seen_ids
            or not _is_text(question.get("title"), 240)
            or not _is_text(question.get("private_question"), 4_000)
            or question.get("status") not in _QUESTION_STATUS
            or question.get("requires_lawyer_approval") is not True
            or question.get("official_source_classes") != list(_SOURCE_CLASSES)
            or question.get("required_checks") != list(_REQUIRED_CHECKS)
        ):
            raise LegalResearchPlanBlocked("legal research question is invalid")
        terms = _public_terms(question.get("proposed_public_terms"))
        if not set(terms).issubset(set(public_terms)):
            raise LegalResearchPlanBlocked(
                "legal research question terms exceed the plan vocabulary"
            )
        refs = _source_refs(question.get("source_refs"), minimum=1, maximum=20)
        if not set(refs).issubset(set(source_refs)):
            raise LegalResearchPlanBlocked(
                "legal research question cites an unauthorized source"
            )
        cited.update(refs)
        seen_ids.add(question_id)
        ready += question.get("status") == "READY_FOR_LAWYER_APPROVAL"
    if not cited:
        raise LegalResearchPlanBlocked("legal research plan has no source binding")
    controls = value.get("controls")
    if not isinstance(controls, dict) or controls != {
        "external_search_separate_approval": True,
        "search_leads_are_not_authority": True,
        "official_bytes_require_separate_capture": True,
        "legal_effect_requires_lawyer_review": True,
        "case_application_requires_lawyer_decision": True,
    }:
        raise LegalResearchPlanBlocked("legal research controls changed")
    summary = value.get("summary_counts")
    if not isinstance(summary, dict) or set(summary) != {
        "questions",
        "ready_for_search_approval",
        "needs_issue_confirmation",
        "verified_authority_sources",
    }:
        raise LegalResearchPlanBlocked("legal research summary fields changed")
    if (
        summary.get("questions") != len(questions)
        or summary.get("ready_for_search_approval") != ready
        or summary.get("needs_issue_confirmation") != len(questions) - ready
        or type(summary.get("verified_authority_sources")) is not int
        or summary["verified_authority_sources"] < 0
    ):
        raise LegalResearchPlanBlocked("legal research summary counts differ")
    return value


def legal_research_plan_source_refs(payload: bytes) -> frozenset[str]:
    value = parse_legal_research_plan_candidate(payload)
    return frozenset(_source_refs(value["source_refs"], minimum=1, maximum=500))


def _research_anchors(
    sources: tuple[BoundCaseContextSource, ...],
) -> tuple[BoundCaseContextSource, ...]:
    priorities = (
        CaseContextSourceType.DISPUTE_ISSUE,
        CaseContextSourceType.CASE_CLAIM,
        CaseContextSourceType.CASE_FACT,
        CaseContextSourceType.POSTURE_PROFILE,
    )
    for source_type in priorities:
        values = tuple(
            item
            for item in sources
            if item.source_type is source_type
            and item.status
            in {
                PlanningInputStatus.CONFIRMED,
                PlanningInputStatus.REVIEW_REQUIRED,
                PlanningInputStatus.OPEN,
            }
        )
        if values:
            return values[:_MAX_QUESTIONS]
    raise LegalResearchPlanBlocked("legal research plan has no governed anchor")


def _question_title(
    anchor: BoundCaseContextSource, *, formal_issue: bool
) -> str:
    if formal_issue:
        return "核验已确认争点的现行法源、时间效力与相反解释"
    return {
        CaseContextSourceType.CASE_CLAIM: "先确认诉请范围，再核验请求权基础与抗辩规则",
        CaseContextSourceType.CASE_FACT: "先由律师形成正式争点，再核验相关法律规则",
        CaseContextSourceType.POSTURE_PROFILE: "先补充正式争点，再按程序阶段核验法律依据",
        CaseContextSourceType.DISPUTE_ISSUE: "先确认争点，再核验现行法源与适用条件",
    }.get(anchor.source_type, "先确认研究问题，再核验官方法源")


def _private_question(
    anchor: BoundCaseContextSource, *, formal_issue: bool
) -> str:
    subject = anchor.primary_text[:1_200].strip()
    if formal_issue:
        return (
            f"围绕已确认争点“{subject}”，核验现行有效法律、司法解释、"
            "时间效力、构成要件、举证责任、相反解释和本案适用条件。"
        )
    return (
        f"当前来源“{subject}”仅作为研究锚点。先由律师确认正式争点，"
        "再核验现行有效法源、时间效力、构成要件和相反解释。"
    )


def _stable_id(input_ref: str, content_hash: str) -> str:
    return "legal-research-" + _canonical_hash(
        {"input_ref": input_ref, "content_hash": content_hash}
    )[:24]


def _source_identity(source: BoundCaseContextSource) -> dict[str, object]:
    return {
        "input_ref": source.input_ref,
        "source_type": source.source_type.value,
        "object_id": source.object_id,
        "object_version": source.object_version,
        "content_hash": source.content_hash,
        "status": source.status.value,
    }


def _public_terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LegalResearchPlanBlocked("legal research public terms are invalid")
    try:
        return validate_public_legal_terms(value)
    except ValueError as error:
        raise LegalResearchPlanBlocked("legal research public terms are unsafe") from error


def _source_refs(value: object, *, minimum: int, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(not isinstance(item, str) or _REF_RE.fullmatch(item) is None for item in value)
        or len(value) != len(set(value))
    ):
        raise LegalResearchPlanBlocked("legal research source refs are invalid")
    return tuple(value)


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _reject_duplicate_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


__all__ = (
    "LEGAL_RESEARCH_PLAN_ARTIFACT_KIND",
    "LEGAL_RESEARCH_PLAN_REVIEW_STATUS",
    "LEGAL_RESEARCH_PLAN_SCHEMA",
    "LEGAL_RESEARCH_PLANNING_SKILL_ID",
    "LEGAL_RESEARCH_PLANNING_TOOL_ID",
    "LegalResearchPlanBlocked",
    "build_legal_research_plan_candidate",
    "legal_research_plan_source_refs",
    "parse_legal_research_plan_candidate",
)
