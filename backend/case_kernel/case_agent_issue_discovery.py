"""Source-bound proposed issues, distinct from source records and approved issues.

Every input is accounted for, but a date, docket number or unrelated payment
need not become a risk. This contract grants no legal or monetary authority.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Mapping


class IssueDiscoveryBlocked(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveredIssue:
    issue_id: str
    title: str
    question: str
    our_position: str
    opponent_position: str
    source_refs: tuple[str, ...]
    needs_lawyer_decision: bool


@dataclass(frozen=True)
class SourceDisposition:
    source_ref: str
    disposition: str
    issue_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class IssueDiscovery:
    source_hash: str
    issues: tuple[DiscoveredIssue, ...]
    source_dispositions: tuple[SourceDisposition, ...]
    content_hash: str


def _text(value, field, maximum=400):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise IssueDiscoveryBlocked(f"{field} is invalid")
    return value.strip()


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise IssueDiscoveryBlocked(f"{label} fields differ")


def _refs(value, allowed, label, minimum=1, maximum=20):
    if (not isinstance(value, list) or not minimum <= len(value) <= maximum
            or any(not isinstance(ref, str) for ref in value)
            or len(set(value)) != len(value) or not set(value).issubset(allowed)):
        raise IssueDiscoveryBlocked(f"{label} references differ")
    return tuple(value)


def parse_issue_discovery(payload, *, source_hash: str, sources: Mapping[str, object]):
    """Validate attribution and coverage, not whether proposed legal reasoning is true.

    Stable IDs are compiler-owned, derived from the input version and proposed
    question; model-local keys never masquerade as existing approved issue IDs.
    Numeric/legal assertions remain subject to downstream analysis validation.
    """
    if (not isinstance(source_hash, str) or len(source_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in source_hash)
            or not sources or len(sources) > 500):
        raise IssueDiscoveryBlocked("source version or set is invalid")
    _keys(payload, ("issues", "source_dispositions"), "discovery")
    rows = payload["issues"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise IssueDiscoveryBlocked("proposed issue count is invalid")
    local, result, questions = {}, [], set()
    for row in rows:
        _keys(row, ("key", "title", "question", "our_position", "opponent_position",
                    "source_refs", "needs_lawyer_decision"), "issue")
        key = _text(row["key"], "local key", 40)
        title = _text(row["title"], "title", 120)
        question = _text(row["question"], "question")
        our = _text(row["our_position"], "our position")
        opponent = _text(row["opponent_position"], "opponent position")
        refs = _refs(row["source_refs"], sources, "issue")
        if key in local or question in questions or type(row["needs_lawyer_decision"]) is not bool:
            raise IssueDiscoveryBlocked("duplicate issue or invalid decision flag")
        binding = json.dumps([source_hash, question, sorted(refs)], ensure_ascii=False, separators=(",", ":"))
        issue_id = "proposed-issue:" + sha256(binding.encode()).hexdigest()
        local[key] = issue_id
        questions.add(question)
        result.append(DiscoveredIssue(issue_id, title, question, our, opponent, refs, row["needs_lawyer_decision"]))
    dispositions = payload["source_dispositions"]
    if not isinstance(dispositions, list) or len(dispositions) != len(sources):
        raise IssueDiscoveryBlocked("every source needs a disposition")
    seen, normalized = set(), []
    for row in dispositions:
        _keys(row, ("source_ref", "disposition", "issue_keys", "reason"), "source disposition")
        ref = row["source_ref"]
        if not isinstance(ref, str) or ref not in sources or ref in seen:
            raise IssueDiscoveryBlocked("source disposition is duplicated or foreign")
        seen.add(ref)
        kind = row["disposition"]
        if kind not in {"ISSUE_RELEVANT", "BACKGROUND", "UNRESOLVED_RELEVANCE"}:
            raise IssueDiscoveryBlocked("source disposition type is invalid")
        keys = _refs(row["issue_keys"], local, "disposition", 1 if kind == "ISSUE_RELEVANT" else 0)
        ids = tuple(local[key] for key in keys)
        if kind != "ISSUE_RELEVANT" and keys:
            raise IssueDiscoveryBlocked("background is not an implicit issue")
        linked = {item.issue_id for item in result if ref in item.source_refs}
        if set(ids) != linked:
            raise IssueDiscoveryBlocked("issue and source disposition links disagree")
        normalized.append(SourceDisposition(ref, kind, ids, _text(row["reason"], "disposition reason")))
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    digest = sha256((source_hash + canonical).encode()).hexdigest()
    return IssueDiscovery(source_hash, tuple(result), tuple(normalized), digest)


def issue_discovery_schema(source_refs):
    """Strict provider schema; source coverage is independently checked after return."""
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def text(maximum=400):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def array(items, low, high):
        return {"type": "array", "items": items, "minItems": low, "maxItems": high}
    ref = {**text(200), "enum": list(source_refs)}
    return obj({"issues": array(obj({"key": text(40), "title": text(120), "question": text(),
        "our_position": text(), "opponent_position": text(), "source_refs": array(ref, 1, 20),
        "needs_lawyer_decision": {"type": "boolean"}}), 1, 20),
        "source_dispositions": array(obj({"source_ref": ref,
            "disposition": {"type": "string", "enum": ["ISSUE_RELEVANT", "BACKGROUND", "UNRESOLVED_RELEVANCE"]},
            "issue_keys": array(text(40), 0, 20), "reason": text()}), len(source_refs), len(source_refs))})
