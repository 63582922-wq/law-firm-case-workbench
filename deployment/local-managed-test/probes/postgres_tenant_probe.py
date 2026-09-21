#!/usr/bin/env python3
"""Prove that the Web role cannot forge an active-plan execution tenant."""

from __future__ import annotations

import os
from uuid import UUID

import psycopg


EXPECTED_ROLE = "lawcase_web_application"
EXPECTED_SQLSTATE = "42501"
EXPECTED_MESSAGE = "active-plan execution tenant context differs from row"
OTHER_FIRM_ID = UUID("99999999-9999-4999-8999-999999999999")


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip():
        raise RuntimeError(f"missing:{name}")
    return value


def main() -> int:
    dsn = _required("LAWCASE_WEB_APP_POSTGRES_DSN")
    expected_firm_id = UUID(_required("LAWCASE_TEST_FIRM_ID"))
    if expected_firm_id == OTHER_FIRM_ID:
        raise RuntimeError("tenant probe ids overlap")

    caught: psycopg.Error | None = None
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=10,
            options="-c statement_timeout=15000",
        ) as connection:
            role, tls = connection.execute(
                """
                SELECT current_user, ssl
                  FROM pg_catalog.pg_stat_ssl
                 WHERE pid = pg_catalog.pg_backend_pid()
                """
            ).fetchone()
            if role != EXPECTED_ROLE or tls is not True:
                raise RuntimeError("tenant probe is not using the TLS Web principal")
            connection.execute(
                "SELECT pg_catalog.set_config('app.firm_id', %s, true)",
                (str(expected_firm_id),),
            )
            connection.execute(
                """
                INSERT INTO public.case_agent_active_plan_execution_runs (
                    execution_id,
                    firm_id,
                    matter_id,
                    plan_id,
                    plan_hash,
                    activated_matter_version,
                    source_run_id,
                    run_id,
                    requested_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    UUID("99999999-9999-4999-8999-999999999991"),
                    OTHER_FIRM_ID,
                    UUID("99999999-9999-4999-8999-999999999992"),
                    UUID("99999999-9999-4999-8999-999999999993"),
                    "0" * 64,
                    1,
                    UUID("99999999-9999-4999-8999-999999999994"),
                    UUID("99999999-9999-4999-8999-999999999995"),
                    UUID("99999999-9999-4999-8999-999999999996"),
                ),
            )
    except psycopg.Error as error:
        caught = error

    if caught is None:
        raise RuntimeError("cross-tenant execution insert unexpectedly succeeded")
    if caught.sqlstate != EXPECTED_SQLSTATE:
        raise RuntimeError("cross-tenant execution insert failed for the wrong reason")
    if caught.diag.message_primary != EXPECTED_MESSAGE:
        raise RuntimeError("cross-tenant execution guard returned an unexpected denial")

    print("PostgreSQL TLS Web-role cross-tenant execution INSERT: DENIED/PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
