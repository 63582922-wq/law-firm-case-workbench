from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from case_kernel.case_agent_case_context import (
    BoundCaseContextProjection,
    BoundCaseContextSource,
    CaseContextSourceType,
)
from case_kernel.case_agent_lawyer_analysis import (
    LAWYER_ANALYSIS_MODEL_ID,
    prepare_lawyer_analysis_request,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (
    LAWYER_ANALYSIS_CONNECT_FAILED,
    LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED,
    LAWYER_ANALYSIS_SKILL_ID,
    LAWYER_ANALYSIS_TOOL_ID,
    LawyerAnalysisKnownFailure,
    LawyerAnalysisRecoveryBinding,
    QWEN_LAWYER_ANALYSIS_MANIFEST,
)
from case_kernel.case_agent_lawyer_analysis_transport import (
    LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA,
    LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
    LawyerAnalysisUnknownSubmission,
    LawyerAnalysisTransportBlocked,
    LawyerAnalysisTransportResult,
    PinnedQwenLawyerAnalysisHttpsTransport,
    PostgresBoundRecoverableLawyerAnalysisExchange,
    QwenLawyerAnalysisCredentials,
    _load_bound_submission,
)
from case_kernel.case_agent_planner import PlanningInputStatus
from case_kernel.models import Actor, Role


RUN_ID = "00000000-0000-4000-8000-000000000301"
TASK_ID = "00000000-0000-4000-8000-000000000302"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000303"
FIRM_ID = "00000000-0000-4000-8000-000000000304"
MATTER_ID = "00000000-0000-4000-8000-000000000305"
WORKER_ID = "00000000-0000-4000-8000-000000000306"
WORKSPACE_ID = "ws-commercial-lawyer"
HOST = f"{WORKSPACE_ID}.cn-beijing.maas.aliyuncs.com"


def _digest(value: str | bytes) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _source(
    number: int,
    source_type: CaseContextSourceType,
    title: str,
    *,
    status: PlanningInputStatus = PlanningInputStatus.CONFIRMED,
) -> BoundCaseContextSource:
    prefix = {
        CaseContextSourceType.POSTURE_PROFILE: "posture-profile",
        CaseContextSourceType.CASE_CLAIM: "claim",
        CaseContextSourceType.DISPUTE_ISSUE: "issue",
        CaseContextSourceType.CASE_FACT: "fact",
        CaseContextSourceType.VERIFIED_LEGAL_SOURCE: "legal-source",
    }[source_type]
    object_id = f"00000000-0000-4000-8000-{number:012d}"
    return BoundCaseContextSource(
        input_ref=f"{prefix}:{object_id}",
        source_type=source_type,
        object_id=object_id,
        object_version="v1",
        content_hash=_digest(f"transport-source-{number}"),
        status=status,
        primary_text=title,
        secondary_text="服务器控制的脱敏案件摘要。",
        signals=(status.value,),
    )


def _request():
    sources = (
        _source(401, CaseContextSourceType.POSTURE_PROFILE, "代理被告处理一审争议"),
        _source(402, CaseContextSourceType.CASE_CLAIM, "原告主张款项未清偿"),
        _source(
            403,
            CaseContextSourceType.DISPUTE_ISSUE,
            "付款性质和对应关系如何认定",
            status=PlanningInputStatus.DISPUTED,
        ),
        _source(404, CaseContextSourceType.CASE_FACT, "双方存在多次资金往来"),
        _source(
            405,
            CaseContextSourceType.VERIFIED_LEGAL_SOURCE,
            "已核验的法律条文快照",
        ),
    )
    projection = BoundCaseContextProjection.build(
        run_id=RUN_ID,
        task_id=TASK_ID,
        task_input_hash=_digest("transport-task-input"),
        firm_id=FIRM_ID,
        matter_id=MATTER_ID,
        matter_version=3,
        case_snapshot_hash=_digest("transport-case-snapshot"),
        input_refs=tuple(item.input_ref for item in sources),
        sources=sources,
    )
    _contract, request = prepare_lawyer_analysis_request(
        projection=projection,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        endpoint_host=HOST,
    )
    return request


def _provider_body() -> bytes:
    return json.dumps(
        {"id": "chatcmpl-controlled", "model": LAWYER_ANALYSIS_MODEL_ID},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _receipt(request, body: bytes) -> dict[str, object]:
    return {
        "schema_version": LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA,
        "external_request_id": request.external_request_id,
        "request_hash": request.request_hash,
        "endpoint_host_hash": _digest(request.endpoint_host),
        "method": "POST",
        "redirect_chain": [],
        "resolved_peer_ips": ["8.8.8.8"],
        "connected_peer_ip": "8.8.8.8",
        "response_sha256": _digest(body),
        "response_bytes": len(body),
        "provider_response_id_hash": _digest("chatcmpl-controlled"),
        "model_id": LAWYER_ANALYSIS_MODEL_ID,
    }


def _binding(request, *, status: str = "RUNNING") -> dict[str, object]:
    return {
        "run_id": request.run_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "firm_id": request.firm_id,
        "matter_id": request.matter_id,
        "external_request_id": request.external_request_id,
        "destination": request.endpoint_host,
        "request_hash": request.request_hash,
        "submission_state": "STARTED",
        "recorded_by": WORKER_ID,
        "attempt_status": status,
        "lease_owner": f"case-agent-worker:{FIRM_ID}",
        "lease_expires_at": "server-owned",
        "lease_current": True,
        "input_hash": request.task_input_hash,
        "task_definition_input_hash": request.task_input_hash,
        "input_refs": list(request.input_refs),
        "skill_id": LAWYER_ANALYSIS_SKILL_ID,
        "tool_id": LAWYER_ANALYSIS_TOOL_ID,
        "adapter_id": QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_id,
        "adapter_version": QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_version,
        "attempt_adapter_id": QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_id,
        "attempt_adapter_version": QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_version,
        "execution_mode": "NETWORK_CONNECTOR",
        "network_policy": "EXACT_ALLOWLIST",
        "allowed_domains": [request.endpoint_host],
        "external_request_approval_required": True,
        "sandbox_policy_version": QWEN_LAWYER_ANALYSIS_MANIFEST.sandbox_policy_version,
        "sandbox_policy_hash": QWEN_LAWYER_ANALYSIS_MANIFEST.sandbox_policy_hash,
        "risk_level": "HIGH",
        "autonomy_level": "A3_LAWYER_APPROVAL",
        "approval_gate": "LAWYER_REVIEW",
        "retry_mode": "NEVER_AUTOMATIC",
        "attempt_retry_mode": "NEVER_AUTOMATIC",
        "resource_budget": {
            "max_attempts": 1,
            "timeout_seconds": LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
            "max_external_calls": 1,
            "max_cost_minor_units": 120,
            "max_output_bytes": 4 * 1024 * 1024,
        },
        "approval_kind": "TASK",
        "task_input_hash": request.task_input_hash,
        "approved_gate": "LAWYER_REVIEW",
        "approval_graph_hash": _digest("current-graph"),
        "current_graph_hash": _digest("current-graph"),
    }


def _recovery_binding(request) -> LawyerAnalysisRecoveryBinding:
    return LawyerAnalysisRecoveryBinding(
        run_id=request.run_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        task_input_hash=request.task_input_hash,
        input_refs=request.input_refs,
        external_request_id=request.external_request_id,
    )


class _FakeSocket:
    def __init__(self, response: bytes, *, peer: str = "8.8.8.8") -> None:
        self._buffer = bytearray(response)
        self.peer = peer
        self.sent = b""
        self.closed = False

    def getpeername(self):
        return (self.peer, 443)

    def sendall(self, value):
        self.sent += value

    def makefile(self, *_args, **_kwargs):
        sock = self

        class _File:
            def readline(self, limit=-1):
                if not sock._buffer:
                    return b""
                index = sock._buffer.find(b"\n") + 1
                if index <= 0:
                    index = len(sock._buffer)
                value = bytes(sock._buffer[:index])
                del sock._buffer[:index]
                return value

            def read(self, amount=-1):
                if amount < 0:
                    amount = len(sock._buffer)
                value = bytes(sock._buffer[:amount])
                del sock._buffer[:amount]
                return value

            def close(self):
                pass

        return _File()

    def close(self):
        self.closed = True


class _Transport:
    def __init__(self, result: LawyerAnalysisTransportResult) -> None:
        self.result = result
        self.calls = []

    def send(self, *, request):
        self.calls.append(request)
        return self.result


class _ObjectStore:
    def __init__(self) -> None:
        self.puts = []
        self.recovered = None

    def put_case_agent_lawyer_analysis_response(self, body, **kwargs):
        self.puts.append((body, kwargs))
        return SimpleNamespace(archive_sha256=_digest(body))

    def recover_case_agent_lawyer_analysis_response(self, **_kwargs):
        return self.recovered


class _QueryConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        return self

    def fetchone(self):
        return None


class LawyerAnalysisTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = _request()
        self.credentials = QwenLawyerAnalysisCredentials(
            api_key="sk-" + "x" * 48,
            workspace_id=WORKSPACE_ID,
        )
        self.actor = Actor(
            WORKER_ID, FIRM_ID, frozenset({Role.SYSTEM_WORKER})
        )

    def test_pinned_https_sends_one_exact_request_and_returns_bound_receipt(self):
        body = _provider_body()
        raw_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Encoding: identity\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        socket_value = _FakeSocket(raw_response)
        transport = PinnedQwenLawyerAnalysisHttpsTransport(
            credentials=self.credentials,
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: socket_value,
        )
        result = transport.send(request=self.request)
        self.assertEqual(result.response_body, body)
        self.assertEqual(result.request_hash, self.request.request_hash)
        self.assertIn(
            b"POST /compatible-mode/v1/chat/completions HTTP/1.1",
            socket_value.sent,
        )
        self.assertIn(b"Authorization: Bearer sk-", socket_value.sent)
        self.assertTrue(socket_value.sent.endswith(self.request.body))
        self.assertEqual(result.transport_receipt["redirect_chain"], [])
        self.assertTrue(socket_value.closed)
        self.assertNotIn(self.credentials.api_key, repr(self.credentials))

    def test_pinned_https_connect_failure_is_known_before_send(self):
        transport = PinnedQwenLawyerAnalysisHttpsTransport(
            credentials=self.credentials,
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: (_ for _ in ()).throw(
                OSError("connect failed")
            ),
        )

        with self.assertRaises(LawyerAnalysisKnownFailure) as raised:
            transport.send(request=self.request)

        self.assertEqual(
            raised.exception.error_code, LAWYER_ANALYSIS_CONNECT_FAILED
        )

    def test_pinned_https_response_head_failure_logs_only_fixed_category(self):
        socket_value = _FakeSocket(b"")
        transport = PinnedQwenLawyerAnalysisHttpsTransport(
            credentials=self.credentials,
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: socket_value,
        )

        with patch("case_kernel.case_agent_lawyer_analysis_transport._logger.warning") as warning:
            with self.assertRaises(LawyerAnalysisUnknownSubmission) as raised:
                transport.send(request=self.request)

        self.assertEqual(
            raised.exception.error_code,
            "LAWYER_ANALYSIS_UNKNOWN_RESPONSE_HEAD",
        )
        warning.assert_called_once()
        message, phase, request_hash, category = warning.call_args.args
        self.assertIn("response-head failure", message)
        self.assertEqual(phase, "response_head")
        self.assertEqual(request_hash, self.request.request_hash)
        self.assertEqual(category, "REMOTE_DISCONNECTED")

    def test_pinned_https_uses_next_verified_address_before_any_send(self):
        body = _provider_body()
        raw_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Encoding: identity\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        socket_value = _FakeSocket(raw_response, peer="8.8.8.8")
        connection_attempts = []

        def connect(address, *_args):
            connection_attempts.append(address)
            if address[0] == "1.1.1.1":
                raise OSError("first provider edge unavailable")
            return socket_value

        transport = PinnedQwenLawyerAnalysisHttpsTransport(
            credentials=self.credentials,
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("8.8.8.8", 443)),
                (2, 1, 6, "", ("1.1.1.1", 443)),
            ],
            connection_factory=connect,
        )

        result = transport.send(request=self.request)

        self.assertEqual(
            connection_attempts,
            [("1.1.1.1", 443), ("8.8.8.8", 443)],
        )
        self.assertEqual(result.response_body, body)
        self.assertEqual(result.transport_receipt["connected_peer_ip"], "8.8.8.8")
        self.assertEqual(socket_value.sent.count(self.request.body), 1)

    def test_exchange_refuses_missing_durable_marker_before_network(self):
        body = _provider_body()
        result = LawyerAnalysisTransportResult(
            self.request.external_request_id,
            self.request.request_hash,
            body,
            _receipt(self.request, body),
        )
        transport = _Transport(result)
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=_ObjectStore(),
            transport=transport,
        )
        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=None,
        ):
            with self.assertRaises(LawyerAnalysisKnownFailure) as raised:
                exchange.send(request=self.request)
        self.assertEqual(
            raised.exception.error_code, LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED
        )
        self.assertEqual(transport.calls, [])

    def test_exchange_archives_complete_response_before_returning(self):
        body = _provider_body()
        result = LawyerAnalysisTransportResult(
            self.request.external_request_id,
            self.request.request_hash,
            body,
            _receipt(self.request, body),
        )
        transport = _Transport(result)
        objects = _ObjectStore()
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=objects,
            transport=transport,
        )
        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=_binding(self.request),
        ):
            self.assertEqual(exchange.send(request=self.request), body)
        self.assertEqual(transport.calls, [self.request])
        self.assertEqual(len(objects.puts), 1)
        self.assertEqual(objects.puts[0][0], body)
        self.assertEqual(
            objects.puts[0][1]["request_hash"], self.request.request_hash
        )

    def test_exchange_accepts_native_postgres_uuid_submission_actor(self):
        body = _provider_body()
        result = LawyerAnalysisTransportResult(
            self.request.external_request_id,
            self.request.request_hash,
            body,
            _receipt(self.request, body),
        )
        binding = _binding(self.request)
        binding["recorded_by"] = UUID(WORKER_ID)
        transport = _Transport(result)
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=_ObjectStore(),
            transport=transport,
        )

        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=binding,
        ):
            self.assertEqual(exchange.send(request=self.request), body)

        self.assertEqual(transport.calls, [self.request])

    def test_exchange_fails_closed_on_any_durable_policy_or_lease_drift(self):
        body = _provider_body()
        result = LawyerAnalysisTransportResult(
            self.request.external_request_id,
            self.request.request_hash,
            body,
            _receipt(self.request, body),
        )
        mutations = (
            ("expired lease", lambda row: row.update(lease_current=False)),
            (
                "different task hash",
                lambda row: row.update(
                    task_definition_input_hash=_digest("different-task")
                ),
            ),
            (
                "different approval graph",
                lambda row: row.update(
                    approval_graph_hash=_digest("different-graph")
                ),
            ),
            (
                "reordered refs",
                lambda row: row.update(input_refs=list(reversed(row["input_refs"]))),
            ),
            (
                "larger external budget",
                lambda row: row.update(
                    resource_budget={
                        **row["resource_budget"],
                        "max_external_calls": 2,
                    }
                ),
            ),
        )
        for label, mutate in mutations:
            binding = _binding(self.request)
            mutate(binding)
            transport = _Transport(result)
            exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
                dsn="postgresql://worker/db",
                worker_actor=self.actor,
                credentials=self.credentials,
                object_store=_ObjectStore(),
                transport=transport,
            )
            with self.subTest(label=label):
                with patch(
                    "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
                    return_value=binding,
                ):
                    with self.assertRaises(LawyerAnalysisKnownFailure) as raised:
                        exchange.send(request=self.request)
                    self.assertEqual(
                        raised.exception.error_code,
                        "LAWYER_ANALYSIS_BINDING_REJECTED",
                    )
                self.assertEqual(transport.calls, [])

    def test_database_lookup_fences_current_graph_lease_actor_and_approval(self):
        connection = _QueryConnection()

        @contextmanager
        def transaction(_dsn, _worker, *, read_only):
            self.assertTrue(read_only)
            yield connection

        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._transaction",
            transaction,
        ):
            self.assertIsNone(
                _load_bound_submission(
                    dsn="postgresql://worker/db",
                    worker=self.actor,
                    external_request_id=self.request.external_request_id,
                )
            )
        for required_sql in (
            "run.current_graph_id = task.graph_id",
            "attempt.lease_expires_at > pg_catalog.clock_timestamp()",
            "submission.recorded_by = %s",
            "worker_role.revoked_at IS NULL",
            "worker_user.status = 'ACTIVE'",
            "approval.graph_hash AS approval_graph_hash",
        ):
            self.assertIn(required_sql, connection.sql)
        self.assertEqual(
            connection.params,
            (
                WORKER_ID,
                FIRM_ID,
                self.request.external_request_id,
                WORKER_ID,
            ),
        )

    def test_reconciliation_reads_archive_and_never_calls_transport(self):
        body = _provider_body()
        receipt = _receipt(self.request, body)
        transport = _Transport(
            LawyerAnalysisTransportResult(
                self.request.external_request_id,
                self.request.request_hash,
                body,
                receipt,
            )
        )
        objects = _ObjectStore()
        objects.recovered = (SimpleNamespace(), body, receipt)
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=objects,
            transport=transport,
        )
        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=_binding(self.request, status="RECONCILING"),
        ):
            recovered = exchange.recover(
                external_request_id=self.request.external_request_id,
                request_hash=self.request.request_hash,
            )
        self.assertEqual(recovered.status, "SUCCEEDED")
        self.assertEqual(recovered.response_body, body)
        self.assertEqual(transport.calls, [])

    def test_bound_recovery_uses_stored_hash_without_rebuilding_request(self):
        binding = _binding(self.request, status="RECONCILING")
        objects = _ObjectStore()
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=objects,
            transport=_Transport(AssertionError("send must not run")),
        )

        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=binding,
        ):
            recovered = exchange.recover_bound(
                binding=_recovery_binding(self.request)
            )

        self.assertEqual(recovered.status, "UNRESOLVED")
        self.assertEqual(recovered.request_hash, self.request.request_hash)

    def test_bound_recovery_rejects_task_identity_drift(self):
        binding = _binding(self.request, status="RECONCILING")
        wrong = _recovery_binding(self.request)
        wrong = LawyerAnalysisRecoveryBinding(
            run_id=wrong.run_id,
            task_id="00000000-0000-4000-8000-000000009999",
            attempt_id=wrong.attempt_id,
            task_input_hash=wrong.task_input_hash,
            input_refs=wrong.input_refs,
            external_request_id=wrong.external_request_id,
        )
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=_ObjectStore(),
            transport=_Transport(AssertionError("send must not run")),
        )

        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=binding,
        ):
            with self.assertRaisesRegex(
                LawyerAnalysisTransportBlocked, "identity differs"
            ):
                exchange.recover_bound(binding=wrong)

    def test_legacy_budget_is_accepted_only_for_lookup_recovery(self):
        objects = _ObjectStore()
        binding = _binding(self.request, status="RECONCILING")
        binding["resource_budget"] = {
            **binding["resource_budget"],
            "timeout_seconds": 180,
        }
        exchange = PostgresBoundRecoverableLawyerAnalysisExchange(
            dsn="postgresql://worker/db",
            worker_actor=self.actor,
            credentials=self.credentials,
            object_store=objects,
            transport=_Transport(AssertionError("send must not run")),
        )

        with patch(
            "case_kernel.case_agent_lawyer_analysis_transport._load_bound_submission",
            return_value=binding,
        ):
            recovered = exchange.recover_bound(
                binding=_recovery_binding(self.request)
            )

        self.assertEqual(recovered.status, "UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
