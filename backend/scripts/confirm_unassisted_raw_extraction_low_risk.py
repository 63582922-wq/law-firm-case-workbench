"""Perform the one synthetic lawyer confirmation gate after raw extraction.

The command is intentionally narrow: it confirms the complete server-selected
low-risk lane and leaves every exception untouched.  It does not approve an
analysis task, choose a legal position, or request another model call.
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
        if matter.version != 7:
            raise RuntimeError("synthetic matter changed before review; inspect rather than confirm")
        service = composition.api_dependencies.agent_ledger_extraction_review_service
        if service is None:
            raise RuntimeError("ledger extraction review service is unavailable")
        batches = service.list_batches(identity=identity, matter_id=matter_id)
        if len(batches) != 1:
            raise RuntimeError("raw extraction batch is not uniquely available")
        batch = batches[0]
        if (
            not batch.can_confirm_low_risk
            or batch.current_matter_version != matter.version
            or batch.low_risk_count < 1
            or batch.confirmed_at is not None
        ):
            raise RuntimeError("raw extraction low-risk lane is not safely confirmable")
        receipt = service.confirm_low_risk_batch(
            identity=identity,
            matter_id=matter_id,
            batch_id=batch.batch_id,
            expected_version=matter.version,
            idempotency_key=_key(f"unassisted-raw-defence-{scope}:confirm-low-risk"),
        )
        if receipt.matter_version != matter.version + 1:
            raise RuntimeError("raw extraction confirmation version differs")
        print(
            json.dumps(
                {
                    "acceptance_scope": scope,
                    "matter_id": matter_id,
                    "batch_id": batch.batch_id,
                    "confirmed_fact_count": receipt.confirmed_fact_count,
                    "confirmed_transaction_count": receipt.confirmed_transaction_count,
                    "confirmed_total_count": receipt.confirmed_total_count,
                    "exception_count_left_for_review": batch.exception_count,
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
