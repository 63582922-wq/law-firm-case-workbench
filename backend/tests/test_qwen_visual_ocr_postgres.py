from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from case_kernel.models import Actor, Role
from case_kernel.qwen_visual_ocr_postgres import (
    CaseAgentVisualOcrPersistenceBlocked,
    PostgresDurableQwenVisualOcrBroker,
    PostgresVisualOcrBindingPort,
    PrivateNativeImageLocator,
    preflight_case_agent_visual_ocr_runtime_contract,
)
from case_kernel.qwen_visual_ocr_adapter import QWEN_VISUAL_OCR_POLICY_HASH
from case_kernel.qwen_visual_ocr_transport import (
    QwenVisualOcrKnownFailure,
    QwenVisualOcrNetworkBlocked,
    QwenVisualOcrTransportFailure,
    QwenVisualOcrTransportRequest,
    QwenVisualOcrTransportResult,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.executions = []

    def execute(self, sql, args=()):
        self.executions.append((sql, args))
        if "SELECT set_config" in sql or "SET TRANSACTION" in sql:
            return self
        return self

    def fetchone(self):
        return self.row


class SequencedConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []

    def execute(self, sql, args=()):
        self.executions.append((sql, args))
        if "SELECT set_config" in sql or "SET TRANSACTION" in sql:
            return FakeConnection(None)
        row = self.rows.pop(0) if self.rows else None
        return FakeConnection(row)


class FakeConnect:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self.connection
    def __exit__(self, *_): return False


class Rows:
    def __init__(self, rows): self.rows = list(rows)
    def fetchall(self): return self.rows


class PreflightConnection:
    COLUMNS = {
        "web_evidence_native_image_source_objects": {
            "evidence_file_id", "firm_id", "matter_id", "source_object_key",
            "source_object_version_id", "source_object_sha256",
            "source_object_bytes", "source_media_type",
            "source_reference_hash", "admitted_by",
        },
        "case_agent_visual_ocr_exchanges": {
            "exchange_id", "external_request_id", "run_id", "task_id",
            "attempt_id", "firm_id", "matter_id", "evidence_page_id",
            "submission_record_id", "request_hash", "projection_hash",
            "rendered_page_sha256", "endpoint_host", "provider_id",
            "model_id", "service_id", "started_by_worker",
        },
        "case_agent_visual_ocr_outcomes": {
            "outcome_id", "exchange_id", "external_request_id", "firm_id",
            "matter_id", "status", "request_hash", "provider_request_id",
            "response_sha256", "response_bytes", "response_body",
            "error_code", "recorded_by_worker",
        },
        "case_agent_visual_ocr_local_failure_resolutions": {
            "resolution_id", "exchange_id", "external_request_id",
            "firm_id", "matter_id", "error_code", "evidence_kind",
            "evidence_sha256", "recorded_by_worker", "recorded_at",
        },
    }
    TRIGGERS = {
        "web_evidence_native_image_source_objects_integrity",
        "web_evidence_native_image_source_objects_append_only",
        "case_agent_visual_ocr_exchange_guard",
        "case_agent_visual_ocr_outcome_guard",
        "case_agent_visual_ocr_exchanges_append_only",
        "case_agent_visual_ocr_outcomes_append_only",
        "case_agent_visual_ocr_local_failure_resolution_guard",
        "case_agent_visual_ocr_local_failure_resolutions_append_only",
    }
    CONSTRAINTS = ({
        "definition": (
            "CHECK (status IN ('SUCCEEDED','FAILED','UNKNOWN_SUBMISSION') "
            "AND error_code ~ '^QWEN_VISUAL_OCR_' AND "
            "error_code IN ('QWEN_VISUAL_OCR_OUTCOME_UNKNOWN',"
            "'QWEN_VISUAL_OCR_UNKNOWN_DNS',"
            "'QWEN_VISUAL_OCR_UNKNOWN_CONNECT',"
            "'QWEN_VISUAL_OCR_UNKNOWN_SEND',"
            "'QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD',"
            "'QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY'))"
        )
    },)

    def __init__(
        self,
        *,
        omit_trigger=None,
        force_rls=True,
        terminal_failure_guard=True,
        unknown_phase_guard=True,
        policy_hash_guard=True,
    ):
        self.omit_trigger = omit_trigger
        self.force_rls = force_rls
        self.terminal_failure_guard = terminal_failure_guard
        self.unknown_phase_guard = unknown_phase_guard
        self.policy_hash_guard = policy_hash_guard
        self.executions = []

    def execute(self, sql, args=()):
        self.executions.append((sql, args))
        if "information_schema.columns" in sql:
            return Rows(
                {"table_name": table, "column_name": column}
                for table, columns in self.COLUMNS.items()
                for column in columns
            )
        if "pg_catalog.pg_trigger" in sql:
            return Rows(
                {"trigger_name": trigger}
                for trigger in self.TRIGGERS
                if trigger != self.omit_trigger
            )
        if "pg_catalog.pg_proc" in sql:
            return Rows(({
                "definition": (
                    "CREATE FUNCTION validate_case_agent_visual_ocr_exchange() "
                    + (
                        QWEN_VISUAL_OCR_POLICY_HASH
                        if self.policy_hash_guard
                        else "0" * 64
                    )
                )
            },))
        if "pg_catalog.pg_constraint" in sql:
            if not self.terminal_failure_guard:
                return Rows(())
            if not self.unknown_phase_guard:
                return Rows(({
                    "definition": (
                        "CHECK (status IN ('SUCCEEDED','FAILED',"
                        "'UNKNOWN_SUBMISSION') AND "
                        "error_code ~ '^QWEN_VISUAL_OCR_')"
                    )
                },))
            return Rows(self.CONSTRAINTS)
        if "pg_catalog.pg_class" in sql:
            return Rows(
                {
                    "relname": table,
                    "relrowsecurity": True,
                    "relforcerowsecurity": self.force_rls,
                }
                for table in self.COLUMNS
            )
        return Rows(())


class Objects:
    def materialize_verified_pdf(self, *_args, **_kwargs):
        raise AssertionError("native image test must not materialize PDF")

    def read_verified_native_image(self, locator):
        self.locator = locator
        return b"not-an-image-but-binding-test-only"


class Transport:
    def __init__(
        self, *, fail=False, known_error_code=None, unknown_error_code=None
    ):
        self.fail = fail
        self.known_error_code = known_error_code
        self.unknown_error_code = unknown_error_code
        self.calls = 0

    def send(self, *, request):
        self.calls += 1
        if self.known_error_code is not None:
            raise QwenVisualOcrKnownFailure(
                external_request_id=request.external_request_id,
                error_code=self.known_error_code,
            )
        if self.unknown_error_code is not None:
            raise QwenVisualOcrNetworkBlocked(
                external_request_id=request.external_request_id,
                error_code=self.unknown_error_code,
            )
        if self.fail:
            raise RuntimeError("indeterminate network")
        body = b'{"choices":[{"message":{"content":"{}"}}],"id":"provider-1","model":"qwen3.5-ocr"}'
        return QwenVisualOcrTransportResult(
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
            provider_request_id="provider-1",
            response_body=body,
        )


class VisualOcrPostgresTests(unittest.TestCase):
    def setUp(self):
        self.firm = str(uuid4())
        self.matter = str(uuid4())
        self.page = str(uuid4())
        self.file = str(uuid4())
        self.run = str(uuid4())
        self.task = str(uuid4())
        self.attempt = str(uuid4())
        self.input_hash = digest("input")
        self.image_hash = digest("native")
        self.key = (
            f"original-images/v1/{self.firm}/{self.matter}/"
            f"{self.image_hash[:2]}/{self.image_hash}/{uuid4()}.png"
        )
        self.row = {
            "matter_id": self.matter, "matter_version": 4,
            "snapshot_matter_version": 4,
            "input_refs": [f"evidence-page:{self.page}"],
            "input_hash": self.input_hash,
            "external_request_id": None,
            "evidence_page_id": self.page, "evidence_file_id": self.file,
            "page_number": 1, "registered_render_hash": None,
            "original_file_sha256": self.image_hash,
            "byte_size": 32, "media_type": "image/png", "page_count": 1,
            "pdf_object_key": None, "pdf_version": None,
            "pdf_reference_hash": None, "image_object_key": self.key,
            "image_version": "v1",
            "image_reference_hash": sha256(self.key.encode()).hexdigest(),
            "image_media_type": "image/png",
        }
        self.actor = Actor(
            str(uuid4()), self.firm, frozenset({Role.SYSTEM_WORKER})
        )

    def port(self):
        value = object.__new__(PostgresVisualOcrBindingPort)
        value._dsn = "postgresql://server-owned"
        value._worker = self.actor
        value._objects = Objects()
        value._workspace_id = "ws-legal-prod"
        value._policy = None
        value._runner = None
        return value

    def transport_request(self):
        body = b"{}"
        return QwenVisualOcrTransportRequest(
            external_request_id=str(
                uuid5(UUID(self.attempt), "qwen-visual-ocr-request-v1")
            ),
            endpoint="https://ws-legal-prod.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
            endpoint_host="ws-legal-prod.cn-beijing.maas.aliyuncs.com",
            method="POST",
            headers={},
            body=body,
            request_hash=sha256(body).hexdigest(),
            projection_hash=digest("projection"),
            rendered_page_sha256=digest("render"),
            timeout_seconds=45.0,
            max_response_bytes=2 * 1024 * 1024,
        )

    def test_current_task_query_binds_worker_tenant_attempt_page_and_input(self):
        connection = FakeConnection(self.row)
        with patch("case_kernel.qwen_visual_ocr_postgres.psycopg.connect", return_value=FakeConnect(connection)):
            record = self.port()._read_current_record(
                run_id=self.run, task_id=self.task, attempt_id=self.attempt,
                task_input_hash=self.input_hash,
                input_refs=(f"evidence-page:{self.page}",),
                evidence_page_id=self.page,
            )
        query, parameters = next(
            item for item in connection.executions
            if "FROM case_agent_task_attempts" in item[0]
        )
        self.assertIn("run.current_graph_id = task.graph_id", query)
        self.assertIn("graph.snapshot_matter_version", query)
        self.assertIn("worker_role.user_id = %s", query)
        self.assertIn("attempt.status IN ('RUNNING', 'RECONCILING')", query)
        self.assertIn("task.input_refs = %s::jsonb", query)
        self.assertIn(self.actor.actor_id, parameters)
        self.assertIn(self.firm, parameters)
        self.assertEqual(record["matter_id"], self.matter)
        self.assertEqual(
            record["external_request_id"],
            str(uuid5(UUID(self.attempt), "qwen-visual-ocr-request-v1")),
        )

    def test_reconciliation_rejects_changed_durable_external_request_id(self):
        self.row["external_request_id"] = str(uuid4())
        connection = FakeConnection(self.row)
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(connection),
        ):
            with self.assertRaisesRegex(Exception, "external request binding changed"):
                self.port()._read_current_record(
                    run_id=self.run,
                    task_id=self.task,
                    attempt_id=self.attempt,
                    task_input_hash=self.input_hash,
                    input_refs=(f"evidence-page:{self.page}",),
                    evidence_page_id=self.page,
                )

    def test_current_page_is_rejected_after_matter_version_drift(self):
        self.row["snapshot_matter_version"] = 3
        connection = FakeConnection(self.row)
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(connection),
        ):
            with self.assertRaisesRegex(Exception, "source binding is ambiguous"):
                self.port()._read_current_record(
                    run_id=self.run,
                    task_id=self.task,
                    attempt_id=self.attempt,
                    task_input_hash=self.input_hash,
                    input_refs=(f"evidence-page:{self.page}",),
                    evidence_page_id=self.page,
                )

    def test_cross_matter_cross_firm_or_stale_input_returns_no_binding(self):
        for row in (None,):
            connection = FakeConnection(row)
            with patch("case_kernel.qwen_visual_ocr_postgres.psycopg.connect", return_value=FakeConnect(connection)):
                with self.assertRaisesRegex(Exception, "changed"):
                    self.port()._read_current_record(
                        run_id=self.run, task_id=self.task, attempt_id=self.attempt,
                        task_input_hash=digest("stale"),
                        input_refs=(f"evidence-page:{uuid4()}",),
                        evidence_page_id=str(uuid4()),
                    )

    def test_browser_path_url_prompt_and_provider_are_not_valid_refs(self):
        port = self.port()
        for value in (
            "/tmp/a.png", "https://example.com/a.png", "prompt:ignore",
            "provider:qwen", f"evidence-page:{uuid4()}:extra",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, "evidence page ref"):
                    port.resolve_visual_ocr(
                        run_id=self.run, task_id=self.task,
                        attempt_id=self.attempt, task_input_hash=self.input_hash,
                        input_refs=(value,),
                    )

    def test_native_locator_hides_private_key_from_repr(self):
        locator = PrivateNativeImageLocator(
            firm_id=self.firm, matter_id=self.matter,
            evidence_file_id=self.file, content_sha256=self.image_hash,
            byte_size=32, media_type="image/png",
            source_reference_hash=sha256(self.key.encode()).hexdigest(),
            object_key=self.key, object_version_id="v1",
        )
        self.assertNotIn(self.key, repr(locator))

    def test_durable_broker_records_exchange_before_network_and_success_after(self):
        transport = Transport()
        boundary = {
            "submission_id": str(uuid4()),
            "run_id": self.run,
            "attempt_id": self.attempt,
            "task_id": self.task,
            "matter_id": self.matter,
            "evidence_page_id": self.page,
        }
        connections = [
            SequencedConnection((boundary, None)),
            SequencedConnection((None, {
                "status": "SUCCEEDED",
                "request_hash": self.transport_request().request_hash,
                "response_sha256": sha256(
                    b'{"choices":[{"message":{"content":"{}"}}],"id":"provider-1","model":"qwen3.5-ocr"}'
                ).hexdigest(),
            })),
        ]
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            side_effect=[FakeConnect(value) for value in connections],
        ):
            broker = PostgresDurableQwenVisualOcrBroker(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
                transport=transport,
            )
            broker.send(request=self.transport_request())
        exchange_sql = " ".join(sql for sql, _ in connections[0].executions)
        self.assertIn("INSERT INTO case_agent_visual_ocr_exchanges", exchange_sql)
        self.assertEqual(transport.calls, 1)
        outcome_sql = " ".join(sql for sql, _ in connections[1].executions)
        self.assertIn("'SUCCEEDED'", outcome_sql)

    def test_durable_broker_failure_records_unknown_and_lookup_never_sends(self):
        transport = Transport(fail=True)
        boundary = {
            "submission_id": str(uuid4()), "run_id": self.run,
            "attempt_id": self.attempt, "task_id": self.task,
            "matter_id": self.matter, "evidence_page_id": self.page,
        }
        send_connection = SequencedConnection((boundary, None))
        unknown_connection = SequencedConnection((None,))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            side_effect=[FakeConnect(send_connection), FakeConnect(unknown_connection)],
        ):
            broker = PostgresDurableQwenVisualOcrBroker(
                dsn="postgresql://server-owned", worker_actor=self.actor,
                transport=transport,
            )
            with self.assertRaisesRegex(RuntimeError, "indeterminate"):
                broker.send(request=self.transport_request())
        self.assertIn(
            "UNKNOWN_SUBMISSION",
            " ".join(sql for sql, _ in unknown_connection.executions),
        )
        self.assertEqual(transport.calls, 1)

    def test_durable_broker_persists_controlled_unknown_stage_without_resend(self):
        transport = Transport(
            unknown_error_code="QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD"
        )
        boundary = {
            "submission_id": str(uuid4()), "run_id": self.run,
            "attempt_id": self.attempt, "task_id": self.task,
            "matter_id": self.matter, "evidence_page_id": self.page,
        }
        send_connection = SequencedConnection((boundary, None))
        unknown_connection = SequencedConnection((None,))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            side_effect=[FakeConnect(send_connection), FakeConnect(unknown_connection)],
        ):
            broker = PostgresDurableQwenVisualOcrBroker(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
                transport=transport,
            )
            with self.assertRaises(QwenVisualOcrNetworkBlocked):
                broker.send(request=self.transport_request())
        execution_args = [
            args for sql, args in unknown_connection.executions
            if "INSERT INTO case_agent_visual_ocr_outcomes" in sql
        ]
        self.assertEqual(len(execution_args), 1)
        self.assertIn(
            "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
            execution_args[0],
        )
        self.assertEqual(transport.calls, 1)

        lookup_connection = SequencedConnection(({
            "request_hash": self.transport_request().request_hash,
            "status": "UNKNOWN_SUBMISSION",
            "provider_request_id": None,
            "response_sha256": None,
            "response_bytes": None,
            "response_body": None,
            "error_code": "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
        },))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(lookup_connection),
        ):
            self.assertIsNone(
                broker.lookup(
                    external_request_id=self.transport_request().external_request_id,
                    request_hash=self.transport_request().request_hash,
                )
            )
        self.assertEqual(transport.calls, 1)

    def test_durable_broker_records_and_recovers_known_failure_without_resend(self):
        transport = Transport(known_error_code="QWEN_VISUAL_OCR_HTTP_400")
        boundary = {
            "submission_id": str(uuid4()), "run_id": self.run,
            "attempt_id": self.attempt, "task_id": self.task,
            "matter_id": self.matter, "evidence_page_id": self.page,
        }
        send_connection = SequencedConnection((boundary, None))
        failed_connection = SequencedConnection((None, {
            "status": "FAILED",
            "request_hash": self.transport_request().request_hash,
            "error_code": "QWEN_VISUAL_OCR_HTTP_400",
        }))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            side_effect=[
                FakeConnect(send_connection), FakeConnect(failed_connection)
            ],
        ):
            broker = PostgresDurableQwenVisualOcrBroker(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
                transport=transport,
            )
            with self.assertRaises(QwenVisualOcrKnownFailure):
                broker.send(request=self.transport_request())
        self.assertIn("'FAILED'", " ".join(
            sql for sql, _ in failed_connection.executions
        ))
        self.assertNotIn("UNKNOWN_SUBMISSION", " ".join(
            sql for sql, _ in failed_connection.executions
        ))

        lookup_connection = SequencedConnection(({
            "request_hash": self.transport_request().request_hash,
            "status": "FAILED",
            "provider_request_id": None,
            "response_sha256": None,
            "response_bytes": None,
            "response_body": None,
            "error_code": "QWEN_VISUAL_OCR_HTTP_400",
        },))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(lookup_connection),
        ):
            recovered = broker.lookup(
                external_request_id=self.transport_request().external_request_id,
                request_hash=self.transport_request().request_hash,
            )
        self.assertIsInstance(recovered, QwenVisualOcrTransportFailure)
        self.assertEqual(recovered.error_code, "QWEN_VISUAL_OCR_HTTP_400")
        self.assertEqual(transport.calls, 1)

    def test_generic_unknown_can_recover_only_from_evidenced_local_failure(self):
        transport = Transport()
        request = self.transport_request()
        lookup_connection = SequencedConnection(({
            "request_hash": request.request_hash,
            "status": "UNKNOWN_SUBMISSION",
            "provider_request_id": None,
            "response_sha256": None,
            "response_bytes": None,
            "response_body": None,
            "error_code": "QWEN_VISUAL_OCR_OUTCOME_UNKNOWN",
            "resolution_error_code": (
                "QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED"
            ),
            "resolution_evidence_kind": "POSTGRES_ERROR_LOG",
            "resolution_evidence_sha256": digest("controlled-db-error-evidence"),
        },))
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(lookup_connection),
        ):
            broker = PostgresDurableQwenVisualOcrBroker(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
                transport=transport,
            )
            recovered = broker.lookup(
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
            )
        self.assertIsInstance(recovered, QwenVisualOcrTransportFailure)
        self.assertEqual(
            recovered.error_code,
            "QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED",
        )
        self.assertEqual(transport.calls, 0)

    def test_enabled_adapter_preflight_requires_exact_0038_guards_and_force_rls(self):
        connection = PreflightConnection()
        with patch(
            "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
            return_value=FakeConnect(connection),
        ):
            preflight_case_agent_visual_ocr_runtime_contract(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
            )
        self.assertIn(
            (self.actor.firm_id,),
            [args for sql, args in connection.executions if "app.firm_id" in sql],
        )
        self.assertIn(
            (self.actor.actor_id,),
            [args for sql, args in connection.executions if "app.actor_id" in sql],
        )

        for broken in (
            PreflightConnection(
                omit_trigger="case_agent_visual_ocr_outcomes_append_only"
            ),
            PreflightConnection(force_rls=False),
            PreflightConnection(terminal_failure_guard=False),
            PreflightConnection(unknown_phase_guard=False),
            PreflightConnection(policy_hash_guard=False),
        ):
            with self.subTest(broken=broken.__dict__):
                with patch(
                    "case_kernel.qwen_visual_ocr_postgres.psycopg.connect",
                    return_value=FakeConnect(broken),
                ):
                    with self.assertRaises(CaseAgentVisualOcrPersistenceBlocked):
                        preflight_case_agent_visual_ocr_runtime_contract(
                            dsn="postgresql://server-owned",
                            worker_actor=self.actor,
                        )

if __name__ == "__main__":
    unittest.main()
