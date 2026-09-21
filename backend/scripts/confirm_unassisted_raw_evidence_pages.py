"""Confirm the exact source pages supporting the synthetic low-risk lane.

This is the page-level lawyer gate required before a source-bound evidence
catalogue may exist.  It selects no model output: the test only includes the
eight original pages that each support one already-confirmed synthetic fact.
"""

from __future__ import annotations

import json
import sys
from uuid import NAMESPACE_URL, uuid5

from run_managed_defence_acceptance import (
    _build_composition,
    _issue_fixture_identity,
    _key,
)


_SCOPES = {
    ("--fresh-v2",): "v2",
}


def main() -> None:
    arguments = tuple(sys.argv[1:])
    if arguments not in _SCOPES:
        raise RuntimeError("only the fixed fresh v2 synthetic acceptance scope is allowed")
    scope = _SCOPES[arguments]
    composition = _build_composition(None)
    identity, session_id = _issue_fixture_identity(composition)
    try:
        matter_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-unassisted-intake-{scope}:{identity.actor.firm_id}",
            )
        )
        store = composition.api_dependencies.matter_store
        matter = store.get(matter_id, firm_id=identity.actor.firm_id)
        if not 8 <= matter.version <= 16:
            raise RuntimeError("synthetic matter changed before page review; inspect rather than confirm")
        service = composition.api_dependencies.evidence_review_service
        if service is None:
            raise RuntimeError("evidence review service is unavailable")
        page = service.pages(
            identity=identity,
            matter_id=matter_id,
            expected_version=matter.version,
            limit=20,
        )
        if page.total_count != 8 or page.has_more or len(page.items) != 8:
            raise RuntimeError("synthetic source page set differs; do not bulk-confirm")
        decision_ids: list[str] = []
        expected_version = matter.version
        for item in page.items:
            pending = item.get("pending_decision")
            approved = item.get("decision")
            if approved is not None:
                raise RuntimeError("a synthetic source page is already approved; do not mix review states")
            if pending is not None:
                if pending.get("status") != "CANDIDATE":
                    raise RuntimeError("existing source page decision is not a candidate")
                decision_ids.append(str(pending["decision_id"]))
                continue
            receipt = service.create_page_decision_candidate(
                identity=identity,
                matter_id=matter_id,
                evidence_page_id=item["evidence_page_id"],
                expected_version=expected_version,
                disposition="INCLUDE",
                reason="全合成验收：该原始页支撑已确认的低风险材料记载，纳入证据目录前仍由律师核对证明目的与三性。",
                idempotency_key=_key(
                    f"unassisted-raw-defence-{scope}:evidence-candidate:{item['evidence_page_id']}"
                ),
            )
            if receipt.matter_version != expected_version + 1:
                raise RuntimeError("source page candidate version differs")
            decision_ids.append(str(receipt.object_id))
            expected_version = receipt.matter_version
        receipt = service.confirm_page_decisions_batch(
            identity=identity,
            matter_id=matter_id,
            decision_ids=tuple(decision_ids),
            expected_version=expected_version,
            idempotency_key=_key(f"unassisted-raw-defence-{scope}:confirm-evidence-pages"),
        )
        if receipt.matter_version != expected_version + 1:
            raise RuntimeError("synthetic evidence page confirmation version differs")
        print(
            json.dumps(
                {
                    "acceptance_scope": scope,
                    "matter_id": matter_id,
                    "confirmed_page_count": len(decision_ids),
                    "matter_version": receipt.matter_version,
                    "model_call_executed_by_this_command": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
