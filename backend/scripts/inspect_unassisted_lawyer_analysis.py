"""Read the browser-safe projection of the fresh synthetic decision package."""

from __future__ import annotations

import json
import sys
from uuid import NAMESPACE_URL, uuid5

from run_managed_defence_acceptance import _build_composition, _issue_fixture_identity


_SCOPES = {("--fresh-v2",): "v2"}
_RUN_ID = "773e696a-7478-5bfa-8104-cebe8c5679e5"
_ARTIFACT_ID = "46c72ea1-4541-5628-9984-1dd7042f3dff"


def main() -> None:
    arguments = tuple(sys.argv[1:])
    if arguments not in {("--fresh-v2",), ("--fresh-v2", "--state")}:
        raise RuntimeError("only the fixed fresh v2 synthetic acceptance scope is allowed")
    scope = "v2"
    composition = _build_composition(None)
    identity, session_id = _issue_fixture_identity(composition)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, f"lawcase-unassisted-intake-{scope}:{identity.actor.firm_id}"))
        if arguments[-1] == "--state":
            matter = composition.api_dependencies.matter_store.get(
                matter_id, firm_id=identity.actor.firm_id
            )
            control = composition.api_dependencies.case_agent_control_service
            if control is None:
                raise RuntimeError("case agent control service is unavailable")
            snapshot = control._snapshot_reader.get_case_snapshot(
                matter_id=matter_id, actor=identity.actor
            )
            print(
                json.dumps(
                    {
                        "matter_id": matter_id,
                        "matter_version": matter.version,
                        "confirmed_fact_count": len(snapshot.facts),
                        "confirmed_claim_count": len(snapshot.claims),
                        "confirmed_issue_count": len(snapshot.issues),
                        "confirmed_transaction_count": len(snapshot.transactions),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        service = composition.api_dependencies.case_agent_artifact_review_service
        if service is None:
            raise RuntimeError("artifact review service is unavailable")
        review = service.read_review(
            identity=identity,
            matter_id=matter_id,
            run_id=_RUN_ID,
            artifact_id=_ARTIFACT_ID,
        )
        print(
            json.dumps(
                {
                    "artifact_type": review.artifact_type,
                    "title": review.title,
                    "review_notice": review.review_notice,
                    "sections": [
                        {
                            "title": section.title,
                            "severity": section.severity,
                            "item_count": len(section.items),
                            "items": [
                                {
                                    "title": item.title,
                                    "detail": item.detail,
                                    "source_count": len(item.sources),
                                }
                                for item in section.items[:4]
                            ],
                        }
                        for section in review.sections
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
