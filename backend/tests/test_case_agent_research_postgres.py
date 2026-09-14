from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_kernel.brave_public_search import (
    BRAVE_SEARCH_HOST,
    BraveSearchTransportRequest,
)
from case_kernel.case_agent_research_postgres import (
    CaseAgentResearchPersistenceBlocked,
    PinnedBraveHttpsTransport,
    PostgresPublicResearchBindingPort,
    PublicSearchNetworkBlocked,
    _brave_request_hash,
    _derive_public_terms,
    _resolve_private_question,
)
from case_kernel.controlled_web_research import (
    ExternalOperationStatus,
    ExternalRequestRef,
)
from case_kernel.models import Actor, Role


def _hash(value: str | bytes) -> str:
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class _Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _QuestionConnection:
    def __init__(self, *, issue_id: str, question: str):
        self.issue_id = issue_id
        self.question = question
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        if "FROM case_dispute_issues" in sql:
            return _Result({"question": self.question})
        raise AssertionError(sql)


class _FakeSocket:
    def __init__(self, response: bytes, *, peer="8.8.8.8"):
        self._buffer = bytearray(response)
        self.peer = peer
        self.sent = b""

    def getpeername(self):
        return (self.peer, 443)

    def sendall(self, content):
        self.sent += content

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
        pass


class CaseAgentResearchPostgresTests(unittest.TestCase):
    def setUp(self):
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )

    def test_private_question_is_resolved_from_governed_issue_not_caller_text(self):
        issue_id = str(uuid4())
        question = "原告：测试用户的民间借贷利息和过渡规则如何适用？"
        connection = _QuestionConnection(issue_id=issue_id, question=question)
        private, private_terms, question_id = _resolve_private_question(
            connection,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            run_id=str(uuid4()),
            graph_hash="a" * 64,
            input_refs=(f"issue:{issue_id}",),
        )
        self.assertEqual(private, question)
        self.assertEqual(question_id, issue_id)
        self.assertTrue(any("测试用户" in value for value in private_terms))
        public = _derive_public_terms(private)
        self.assertIn("民间借贷", public)
        self.assertIn("过渡规则", public)
        self.assertNotIn("测试用户", " ".join(public))

    def test_binding_port_rejects_browser_or_model_supplied_ref_kinds(self):
        port = PostgresPublicResearchBindingPort(
            dsn="postgresql://worker/db", worker_actor=self.actor
        )
        for ref in (
            "url:https://example.com",
            f"material-object:{uuid4()}",
            f"memory:{uuid4()}:v1",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(
                    CaseAgentResearchPersistenceBlocked, "input refs"
                ):
                    port.resolve_public_research(
                        run_id=str(uuid4()),
                        task_id=str(uuid4()),
                        attempt_id=str(uuid4()),
                        task_input_hash="b" * 64,
                        input_refs=(ref,),
                    )

    def test_existing_running_binding_is_reauthorized_before_first_send(self):
        run_id, task_id, attempt_id, issue_id = (
            str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        )
        input_refs = (f"issue:{issue_id}",)
        task_input_hash = "b" * 64
        binding = SimpleNamespace(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            matter_id=self.matter_id,
            confidential_question="民间借贷利息法律规定",
            private_terms=(),
            question_id=issue_id,
            proposed_public_terms=("司法解释", "民间借贷", "法律规定", "利息"),
        )
        task = {
            "matter_id": self.matter_id,
            "approved_by": str(uuid4()),
            "graph_hash": "c" * 64,
        }
        port = PostgresPublicResearchBindingPort(
            dsn="postgresql://worker/db", worker_actor=self.actor
        )
        with (
            patch(
                "case_kernel.case_agent_research_postgres._transaction",
                return_value=nullcontext(object()),
            ),
            patch(
                "case_kernel.case_agent_research_postgres._read_binding",
                return_value={"current_attempt_status": "RUNNING"},
            ),
            patch(
                "case_kernel.case_agent_research_postgres._binding_from_row",
                return_value=binding,
            ),
            patch(
                "case_kernel.case_agent_research_postgres._read_research_task",
                return_value=task,
            ) as read_task,
            patch("case_kernel.case_agent_research_postgres._require_research_task"),
            patch("case_kernel.case_agent_research_postgres._require_dedicated_worker"),
            patch("case_kernel.case_agent_research_postgres._require_current_lawyer_approval"),
            patch(
                "case_kernel.case_agent_research_postgres._resolve_private_question",
                return_value=(binding.confidential_question, (), issue_id),
            ),
        ):
            result = port.resolve_public_research(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
            )
        self.assertIs(result, binding)
        read_task.assert_called_once()

    def test_existing_reconciling_binding_never_reopens_current_send_authority(self):
        run_id, task_id, attempt_id, issue_id = (
            str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        )
        input_refs = (f"issue:{issue_id}",)
        task_input_hash = "b" * 64
        binding = SimpleNamespace(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
        )
        port = PostgresPublicResearchBindingPort(
            dsn="postgresql://worker/db", worker_actor=self.actor
        )
        with (
            patch(
                "case_kernel.case_agent_research_postgres._transaction",
                return_value=nullcontext(object()),
            ),
            patch(
                "case_kernel.case_agent_research_postgres._read_binding",
                return_value={"current_attempt_status": "RECONCILING"},
            ),
            patch(
                "case_kernel.case_agent_research_postgres._binding_from_row",
                return_value=binding,
            ),
            patch(
                "case_kernel.case_agent_research_postgres._read_research_task"
            ) as read_task,
        ):
            result = port.resolve_public_research(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                task_input_hash=task_input_hash,
                input_refs=input_refs,
            )
        self.assertIs(result, binding)
        read_task.assert_not_called()

    def test_pinned_transport_enforces_one_exact_get_and_global_peer(self):
        raw = b'{"type":"search","web":{"results":[]}}'
        http_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(raw)}\r\n\r\n".encode()
            + raw
        )
        sock = _FakeSocket(http_response)
        external = ExternalRequestRef(
            request_id=str(uuid4()),
            authorization_hash="c" * 64,
            egress_grant_id=str(uuid4()),
            egress_grant_hash="d" * 64,
            provider_id="brave_web_search",
            service_id="web_search_v1",
            input_hash="e" * 64,
            allowed_hosts=(BRAVE_SEARCH_HOST,),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status=ExternalOperationStatus.AUTHORIZED,
        )
        endpoint = (
            "https://api.search.brave.com/res/v1/web/search?"
            "q=%E6%B3%95%E5%BE%8B%E8%A7%84%E5%AE%9A&count=1"
        )
        request = BraveSearchTransportRequest(
            endpoint=endpoint,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Api-Version": "2023-01-01",
                "X-Subscription-Token": "x" * 40,
            },
            body=b"",
            max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15.0,
            request_hash=_brave_request_hash(endpoint),
        )
        transport = PinnedBraveHttpsTransport(
            resolver=lambda *_args, **_kwargs: [
                (None, None, None, None, ("8.8.8.8", 443))
            ],
            connection_factory=lambda *_args: sock,
        )
        result = transport.send(request=request, external_request=external)
        self.assertEqual(result.response_body, raw)
        self.assertEqual(result.egress_receipt.request_count, 1)
        self.assertEqual(result.egress_receipt.connected_peer_ip, "8.8.8.8")
        self.assertTrue(sock.sent.startswith(b"GET /res/v1/web/search?"))
        self.assertEqual(sock.sent.count(b"GET "), 1)

    def test_transport_rejects_a_request_hash_not_bound_to_exact_url(self):
        endpoint = "https://api.search.brave.com/res/v1/web/search?q=law&count=1"
        external = ExternalRequestRef(
            request_id=str(uuid4()), authorization_hash="c" * 64,
            egress_grant_id=str(uuid4()), egress_grant_hash="a" * 64,
            provider_id="brave_web_search", service_id="web_search_v1",
            input_hash="d" * 64, allowed_hosts=(BRAVE_SEARCH_HOST,),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status=ExternalOperationStatus.AUTHORIZED,
        )
        request = BraveSearchTransportRequest(
            endpoint=endpoint, method="GET", headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Api-Version": "2023-01-01", "X-Subscription-Token": "x" * 40,
            }, body=b"", max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15.0, request_hash="b" * 64,
        )
        transport = PinnedBraveHttpsTransport(
            resolver=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DNS"))
        )
        with self.assertRaisesRegex(
            CaseAgentResearchPersistenceBlocked, "not exact"
        ):
            transport.send(request=request, external_request=external)

    def test_pinned_transport_blocks_private_dns_and_redirects_without_real_network(self):
        external = ExternalRequestRef(
            request_id=str(uuid4()), authorization_hash="c" * 64,
            egress_grant_id=str(uuid4()), egress_grant_hash="a" * 64,
            provider_id="brave_web_search", service_id="web_search_v1",
            input_hash="d" * 64, allowed_hosts=(BRAVE_SEARCH_HOST,),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status=ExternalOperationStatus.AUTHORIZED,
        )
        endpoint = "https://api.search.brave.com/res/v1/web/search?q=law&count=1"
        request = BraveSearchTransportRequest(
            endpoint=endpoint, method="GET", headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Api-Version": "2023-01-01", "X-Subscription-Token": "x" * 40,
            }, body=b"", max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15.0, request_hash=_brave_request_hash(endpoint),
        )
        transport = PinnedBraveHttpsTransport(
            resolver=lambda *_a, **_k: [(None, None, None, None, ("127.0.0.1", 443))],
            connection_factory=lambda *_a: (_ for _ in ()).throw(AssertionError("socket")),
        )
        with self.assertRaisesRegex(PublicSearchNetworkBlocked, "not global"):
            transport.send(request=request, external_request=external)

    def test_pinned_transport_rejects_compressed_response_bytes(self):
        raw = b"{}"
        http_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Encoding: gzip\r\nContent-Length: 2\r\n\r\n" + raw
        )
        external = ExternalRequestRef(
            request_id=str(uuid4()), authorization_hash="c" * 64,
            egress_grant_id=str(uuid4()), egress_grant_hash="a" * 64,
            provider_id="brave_web_search", service_id="web_search_v1",
            input_hash="d" * 64, allowed_hosts=(BRAVE_SEARCH_HOST,),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            status=ExternalOperationStatus.AUTHORIZED,
        )
        endpoint = "https://api.search.brave.com/res/v1/web/search?q=law&count=1"
        request = BraveSearchTransportRequest(
            endpoint=endpoint, method="GET", headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Api-Version": "2023-01-01", "X-Subscription-Token": "x" * 40,
            }, body=b"", max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15.0, request_hash=_brave_request_hash(endpoint),
        )
        transport = PinnedBraveHttpsTransport(
            resolver=lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 443))],
            connection_factory=lambda *_a: _FakeSocket(http_response),
        )
        with self.assertRaisesRegex(PublicSearchNetworkBlocked, "encoding"):
            transport.send(request=request, external_request=external)

    def test_pinned_transport_rejects_expired_grant_before_dns(self):
        endpoint = "https://api.search.brave.com/res/v1/web/search?q=law&count=1"
        external = ExternalRequestRef(
            request_id=str(uuid4()), authorization_hash="c" * 64,
            egress_grant_id=str(uuid4()), egress_grant_hash="a" * 64,
            provider_id="brave_web_search", service_id="web_search_v1",
            input_hash="d" * 64, allowed_hosts=(BRAVE_SEARCH_HOST,),
            expires_at=datetime(2026, 8, 13, 9, 59, tzinfo=timezone.utc),
            status=ExternalOperationStatus.AUTHORIZED,
        )
        request = BraveSearchTransportRequest(
            endpoint=endpoint, method="GET", headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Api-Version": "2023-01-01", "X-Subscription-Token": "x" * 40,
            }, body=b"", max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15.0, request_hash=_brave_request_hash(endpoint),
        )
        transport = PinnedBraveHttpsTransport(
            clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
            resolver=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DNS")),
        )
        with self.assertRaisesRegex(PublicSearchNetworkBlocked, "expired"):
            transport.send(request=request, external_request=external)


if __name__ == "__main__":
    unittest.main()
