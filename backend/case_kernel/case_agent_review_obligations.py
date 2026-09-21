"""Source-bound review obligations, never promoted facts or executable instructions.

Only the authoritative planning repository may construct these from current
lawyer signals after verifying their lifecycle and source pages. This contract
does not itself authorize network access or resolve a review obligation.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .case_agent_planner import PlanningInputStatus
from .case_agent_planning_snapshot import GovernedLawyerPlanningSignal

REVIEW_OBLIGATION_CODES = frozenset({"LAWYER_DEFERRED_LEDGER_EXCEPTION",
    "LAWYER_REQUESTED_MORE_LEDGER_EVIDENCE", "LAWYER_REJECTED_EXTRACTION_DUPLICATE"})


@dataclass(frozen=True)
class BoundReviewObligation:
    obligation_id: str
    object_version: str
    content_hash: str
    decision_hash: str
    status: PlanningInputStatus
    code: str
    review_note: str
    source_ref_ids: tuple[str, ...]
    schema_version: str = "bound-review-obligation-v1"


def bind_review_obligation(*, signal: GovernedLawyerPlanningSignal,
                          authorized_refs: frozenset[str]) -> BoundReviewObligation:
    """Preserve uncertainty and bind the actual note, not only a decision ID.

    Re-extraction is deliberately excluded: reading an obligation is not
    fulfilling its required extraction task. New signal kinds fail closed.
    """
    signal.validate(authorized_refs=authorized_refs)
    expected = {
        "LAWYER_DEFERRED_LEDGER_EXCEPTION": PlanningInputStatus.BLOCKED,
        "LAWYER_REQUESTED_MORE_LEDGER_EVIDENCE": PlanningInputStatus.OPEN,
        "LAWYER_REJECTED_EXTRACTION_DUPLICATE": PlanningInputStatus.DISPUTED,
    }
    if signal.code not in expected or signal.status is not expected[signal.code]:
        raise ValueError("unsupported review obligation kind or state")
    if any(not ref.startswith("evidence-page:") for ref in signal.source_ref_ids):
        raise ValueError("review obligation requires original page provenance")
    refs = tuple(sorted(signal.source_ref_ids))
    payload = dict(schema_version="bound-review-obligation-v1",
        obligation_id=signal.signal_id, object_version=signal.signal_version,
        decision_hash=signal.decision_hash, status=signal.status.value,
        code=signal.code, review_note=signal.summary, source_ref_ids=refs)
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return BoundReviewObligation(content_hash=digest, **{
        **payload, "status": signal.status, "source_ref_ids": refs})
