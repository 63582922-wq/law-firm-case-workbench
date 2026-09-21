#!/usr/bin/env python3
"""Exercise the real Web and Agent startup database fences against a fresh DB."""

from __future__ import annotations

import os

from case_api.web_agent_ledger_extraction_review_postgres import (
    preflight_web_ledger_confirmation_session_authority,
)
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    preflight_case_agent_ledger_exception_followup_schema,
)
from case_kernel.case_agent_runtime_postgres import (
    preflight_case_agent_runtime_contract,
)


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip() or "\x00" in value:
        raise RuntimeError(f"managed PostgreSQL preflight setting is missing: {name}")
    return value


def main() -> None:
    web_dsn = _required("LAWCASE_PREFLIGHT_WEB_DSN")
    worker_dsn = _required("LAWCASE_PREFLIGHT_WORKER_DSN")
    verifier_dsn = _required("LAWCASE_PREFLIGHT_VERIFIER_DSN")
    firm_id = _required("LAWCASE_TEST_FIRM_ID")
    worker_actor_id = _required("LAWCASE_TEST_WORKER_ACTOR_ID")
    verifier_actor_id = _required("LAWCASE_TEST_VERIFIER_ACTOR_ID")

    # These are the same mandatory database fences executed before the Web
    # routes and Worker heartbeat can become available in a production process.
    preflight_web_ledger_confirmation_session_authority(dsn=web_dsn)
    preflight_case_agent_ledger_exception_followup_schema(
        dsn=web_dsn,
        firm_id=firm_id,
    )
    preflight_case_agent_ledger_exception_followup_schema(
        dsn=worker_dsn,
        firm_id=firm_id,
    )
    preflight_case_agent_runtime_contract(
        execution_dsn=worker_dsn,
        verifier_dsn=verifier_dsn,
        firm_id=firm_id,
        execution_actor_id=worker_actor_id,
        verifier_actor_id=verifier_actor_id,
    )
    print("Web and Agent PostgreSQL startup preflights: PASS")


if __name__ == "__main__":
    main()
