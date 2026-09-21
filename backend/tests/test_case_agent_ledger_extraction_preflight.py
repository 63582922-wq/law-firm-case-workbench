from __future__ import annotations

import unittest
from unittest.mock import patch
from uuid import uuid4

from case_kernel import case_agent_ledger_extraction_exchange_postgres as exchange_module
from case_kernel import case_agent_ledger_extraction_postgres as staging_module
from case_kernel.case_agent_ledger_extraction_exchange_postgres import (
    CaseAgentLedgerExtractionExchangeBlocked,
    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
    preflight_case_agent_ledger_extraction_runtime_contract,
)
from case_kernel.case_agent_ledger_extraction_postgres import (
    CaseLedgerExtractionStagingBlocked,
    preflight_case_agent_ledger_extraction_staging_runtime_contract,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
)
from case_kernel.models import Actor, Role


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Transport:
    endpoint = DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
    model_id = DEEPSEEK_LEDGER_EXTRACTION_MODEL
    response_schema_hash = DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH

    def send_raw(self, **kwargs):
        raise AssertionError("preflight must not call the network")


class _Store:
    def put_ledger_extraction_response(self, *args, **kwargs):
        raise AssertionError("preflight must not write objects")

    def read_ledger_extraction_response(self, *args, **kwargs):
        raise AssertionError("preflight must not read case objects")

    def recover_ledger_extraction_response(self, *args, **kwargs):
        raise AssertionError("preflight must not recover case objects")


class _ExchangePreflightConnection:
    tables = {
        "case_agent_ledger_extraction_exchanges": {
            "exchange_id", "external_request_id", "run_id", "graph_id",
            "task_id", "attempt_id", "firm_id", "matter_id",
            "submission_record_id", "task_input_hash", "input_refs_hash",
            "request_hash", "endpoint_url", "endpoint_host", "provider_id",
            "service_id", "model_id", "response_schema_id",
            "response_schema_hash", "started_by_worker",
        },
        "case_agent_ledger_extraction_outcomes": {
            "outcome_id", "exchange_id", "external_request_id", "firm_id",
            "matter_id", "outcome_sequence", "status", "request_hash",
            "response_schema_hash", "provider_response_id_hash",
            "response_sha256", "response_bytes", "response_object_key",
            "response_object_version_id", "error_code",
            "recovered_from_unknown", "recorded_by_worker",
        },
    }
    triggers = {
        "case_agent_ledger_extraction_exchange_guard",
        "case_agent_ledger_extraction_outcome_guard",
        "case_agent_ledger_extraction_exchanges_append_only",
        "case_agent_ledger_extraction_outcomes_append_only",
    }

    def __init__(self, *, force_rls=True):
        self.force_rls = force_rls

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SET TRANSACTION") or "set_config" in normalized:
            return _Cursor()
        if "information_schema.columns" in normalized:
            return _Cursor(
                {"table_name": table, "column_name": column}
                for table, columns in self.tables.items()
                for column in columns
            )
        if "pg_get_constraintdef" in normalized:
            return _Cursor(
                (
                    {
                        "table_name": "case_agent_ledger_extraction_exchanges",
                        "definition": "UNIQUE (external_request_id)",
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_exchanges",
                        "definition": (
                            "FOREIGN KEY (attempt_id, graph_id, task_id, run_id, firm_id, matter_id) "
                            "REFERENCES case_agent_task_attempts"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_exchanges",
                        "definition": (
                            "FOREIGN KEY (submission_record_id, firm_id, matter_id) "
                            "REFERENCES case_agent_external_submissions"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_exchanges",
                        "definition": (
                            "CHECK endpoint "
                            f"{DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT} "
                            f"{DEEPSEEK_LEDGER_EXTRACTION_MODEL} "
                            f"{DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH}"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_outcomes",
                        "definition": (
                            "FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id) "
                            "REFERENCES case_agent_ledger_extraction_exchanges "
                            "UNKNOWN_SUBMISSION case-agent-ledger-extractions/v1 "
                            f"{DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH}"
                        ),
                    },
                )
            )
        if "information_schema.triggers" in normalized:
            return _Cursor({"trigger_name": name} for name in self.triggers)
        if "relforcerowsecurity" in normalized:
            return _Cursor(
                {
                    "relname": table,
                    "relrowsecurity": True,
                    "relforcerowsecurity": self.force_rls,
                }
                for table in self.tables
            )
        raise AssertionError(normalized)


class _StagingPreflightConnection:
    tables = {
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_staging_events",
        "case_agent_ledger_extraction_candidates",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_extraction_promotions",
        "case_agent_ledger_extraction_batch_confirmations",
    }
    triggers = {
        "case_agent_ledger_extraction_batches_append_only",
        "case_agent_ledger_extraction_staging_events_append_only",
        "case_agent_ledger_extraction_candidates_append_only",
        "case_agent_ledger_extraction_candidate_pages_append_only",
        "case_agent_ledger_extraction_promotions_append_only",
        "case_agent_ledger_extraction_batch_confirmations_append_only",
        "case_agent_ledger_extraction_promotions_target_integrity",
        "case_agent_ledger_extraction_confirmed_target_integrity",
        "case_agent_ledger_extraction_batch_confirmation_complete",
    }

    def __init__(self, *, nondeferred=()):
        self.nondeferred = frozenset(nondeferred)

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SET TRANSACTION") or "set_config" in normalized:
            return _Cursor()
        if "information_schema.columns" in normalized:
            columns = {
                "case_agent_ledger_extraction_batches": {
                    "extraction_batch_id", "run_id", "graph_id", "task_id",
                    "artifact_id", "verification_receipt_id", "firm_id", "matter_id",
                    "source_matter_version", "staged_matter_version",
                    "candidate_count", "eligible_candidate_count",
                },
                "case_agent_ledger_extraction_staging_events": {
                    "staging_event_id", "extraction_batch_id", "artifact_id",
                    "run_id", "firm_id", "matter_id", "event_type",
                    "source_matter_version", "staged_matter_version",
                    "actor_id", "idempotency_key", "request_hash", "payload",
                },
                "case_agent_ledger_extraction_candidates": {
                    "extraction_candidate_id", "extraction_batch_id",
                    "firm_id", "matter_id", "candidate_hash", "candidate_kind", "review_lane",
                    "eligible_for_bulk_promotion", "review_status",
                    "review_reason_codes", "candidate_payload",
                },
                "case_agent_ledger_extraction_candidate_pages": {
                    "extraction_candidate_id", "evidence_page_id",
                    "firm_id", "matter_id", "source_text_sha256",
                },
                "case_agent_ledger_extraction_promotions": {
                    "extraction_batch_id", "extraction_candidate_id",
                    "firm_id", "matter_id", "target_object_type",
                    "target_object_id", "promoted_matter_version",
                    "lawyer_batch_decision_hash", "promoted_by",
                },
                "case_agent_ledger_extraction_batch_confirmations": {
                    "extraction_batch_id", "firm_id", "matter_id",
                    "confirmed_candidate_count", "lawyer_batch_decision_hash",
                    "confirmed_matter_version", "confirmed_by",
                },
            }
            return _Cursor(
                {"table_name": table, "column_name": column}
                for table, values in columns.items()
                for column in values
            )
        if "pg_get_constraintdef" in normalized:
            return _Cursor(
                (
                    {
                        "table_name": "case_agent_ledger_extraction_candidates",
                        "definition": (
                            "FOREIGN KEY (extraction_batch_id, firm_id, matter_id) "
                            "REFERENCES case_agent_ledger_extraction_batches"
                            "(extraction_batch_id, firm_id, matter_id)"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_promotions",
                        "definition": (
                            "FOREIGN KEY (extraction_candidate_id, "
                            "extraction_batch_id, firm_id, matter_id) "
                            "REFERENCES case_agent_ledger_extraction_candidates"
                            "(extraction_candidate_id, extraction_batch_id, "
                            "firm_id, matter_id)"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_batches",
                        "definition": (
                            "CHECK ((staged_matter_version = source_matter_version))"
                        ),
                    },
                    {
                        "table_name": "case_agent_ledger_extraction_staging_events",
                        "definition": (
                            "FOREIGN KEY (extraction_batch_id, artifact_id, run_id, firm_id, matter_id) "
                            "REFERENCES case_agent_ledger_extraction_batches"
                        ),
                    },
                )
            )
        if "pg_catalog.pg_trigger" in normalized:
            return _Cursor(
                {
                    "trigger_name": name,
                    "tgdeferrable": name not in self.nondeferred and name
                    in {
                        "case_agent_ledger_extraction_confirmed_target_integrity",
                        "case_agent_ledger_extraction_batch_confirmation_complete",
                    },
                    "tginitdeferred": name not in self.nondeferred and name
                    in {
                        "case_agent_ledger_extraction_confirmed_target_integrity",
                        "case_agent_ledger_extraction_batch_confirmation_complete",
                    },
                }
                for name in self.triggers
            )
        if "relforcerowsecurity" in normalized:
            return _Cursor(
                {
                    "relname": table,
                    "relrowsecurity": True,
                    "relforcerowsecurity": True,
                }
                for table in self.tables
            )
        raise AssertionError(normalized)


class LedgerExtractionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.actor = Actor(
            str(uuid4()),
            str(uuid4()),
            frozenset({Role.SYSTEM_WORKER}),
        )

    def test_0045_preflight_is_structural_and_makes_no_network_call(self):
        connection = _ExchangePreflightConnection()
        with patch.object(
            exchange_module.psycopg,
            "connect",
            return_value=_Context(connection),
        ):
            preflight_case_agent_ledger_extraction_runtime_contract(
                dsn="postgresql://unit-test",
                worker_actor=self.actor,
                transport=_Transport(),
                response_store=_Store(),
            )

    def test_0045_preflight_rejects_missing_force_rls(self):
        connection = _ExchangePreflightConnection(force_rls=False)
        with patch.object(
            exchange_module.psycopg,
            "connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(
                CaseAgentLedgerExtractionExchangeBlocked, "FORCE RLS"
            ):
                preflight_case_agent_ledger_extraction_runtime_contract(
                    dsn="postgresql://unit-test",
                    worker_actor=self.actor,
                    transport=_Transport(),
                    response_store=_Store(),
                )

    def test_0042_preflight_proves_composite_fks_deferred_completeness_and_rls(self):
        connection = _StagingPreflightConnection()
        with patch.object(
            staging_module.psycopg,
            "connect",
            return_value=_Context(connection),
        ):
            preflight_case_agent_ledger_extraction_staging_runtime_contract(
                dsn="postgresql://unit-test",
                worker_actor=self.actor,
            )

    def test_0042_preflight_rejects_nonworker_identity(self):
        actor = Actor(
            str(uuid4()),
            str(uuid4()),
            frozenset({Role.LEAD_LAWYER}),
        )
        with self.assertRaises(CaseLedgerExtractionStagingBlocked):
            preflight_case_agent_ledger_extraction_staging_runtime_contract(
                dsn="postgresql://unit-test",
                worker_actor=actor,
            )

    def test_0042_preflight_rejects_nondeferred_target_recheck(self):
        connection = _StagingPreflightConnection(
            nondeferred={
                "case_agent_ledger_extraction_confirmed_target_integrity"
            }
        )
        with (
            patch.object(
                staging_module.psycopg,
                "connect",
                return_value=_Context(connection),
            ),
            self.assertRaisesRegex(
                CaseLedgerExtractionStagingBlocked,
                "confirmation guards are not deferred",
            ),
        ):
            preflight_case_agent_ledger_extraction_staging_runtime_contract(
                dsn="postgresql://unit-test",
                worker_actor=self.actor,
            )


if __name__ == "__main__":
    unittest.main()
