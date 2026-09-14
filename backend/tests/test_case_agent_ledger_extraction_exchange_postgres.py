from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import io
import json
import unittest
from unittest.mock import patch
import urllib.error
from uuid import UUID, uuid4, uuid5

from case_kernel import case_agent_ledger_extraction_exchange_postgres as exchange_module
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS,
    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
    DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
    LedgerExtractionKnownFailure,
    PreparedLedgerExtractionRequest,
)
from case_kernel.case_agent_ledger_extraction_exchange_postgres import (
    CaseAgentLedgerExtractionExchangeBlocked,
    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
    DeepSeekLedgerExtractionCredentials,
    DeepSeekLedgerExtractionRawHttpsTransport,
    LedgerExtractionRawResponseUnavailable,
    LedgerExtractionUnknownSubmission,
    PostgresRecoverableLedgerExtractionExchange,
    S3LedgerExtractionRawResponseStore,
    StoredLedgerExtractionResponse,
)
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import S3PrivateObjectStoreConfig


def _prepared(attempt_id: str) -> PreparedLedgerExtractionRequest:
    page_id = str(uuid4())
    body = json.dumps(
        {
            "model": DEEPSEEK_LEDGER_EXTRACTION_MODEL,
            "temperature": 0,
            "max_tokens": DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "pages": [
                                {
                                    "evidence_page_id": page_id,
                                    "page_number": 1,
                                    "source_mode": "NATIVE_TEXT",
                                    "text": "2025年1月1日支付100元",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request_hash = sha256(body).hexdigest()
    return PreparedLedgerExtractionRequest(
        external_request_id=str(uuid5(UUID(attempt_id), request_hash)),
        request_hash=request_hash,
        body=body,
    )


def _response(*, model: str = DEEPSEEK_LEDGER_EXTRACTION_MODEL) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-ledger-1",
            "model": model,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"candidates": []}),
                    }
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _Cursor:
    def __init__(self, *, one=None, all_rows=()):
        self._one = one
        self._all = list(all_rows)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _StartConnection:
    def __init__(self, row):
        self.row = row
        self.inserted = None

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if "FROM case_agent_external_submissions submission" in normalized:
            return _Cursor(one=self.row)
        if "SELECT 1 FROM case_agent_ledger_extraction_exchanges" in normalized:
            return _Cursor(one=None)
        if "INSERT INTO case_agent_ledger_extraction_exchanges" in normalized:
            self.inserted = args
            return _Cursor(one=None)
        raise AssertionError(normalized)


class _Transport:
    endpoint = DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
    model_id = DEEPSEEK_LEDGER_EXTRACTION_MODEL
    response_schema_hash = DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH

    def __init__(self, events: list[str], response: bytes | Exception):
        self.events = events
        self.response = response
        self.calls = 0

    def send_raw(self, *, request):
        self.calls += 1
        self.events.append("https-send")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _Store:
    def __init__(self, events: list[str], response: bytes):
        self.events = events
        self.response = response
        self.recoverable = True

    def put_ledger_extraction_response(self, response_body, **kwargs):
        self.events.append("s3-put")
        self.response = response_body
        return StoredLedgerExtractionResponse(
            object_key=(
                "case-agent-ledger-extractions/v1/"
                f"{kwargs['firm_id']}/{kwargs['matter_id']}/"
                f"{kwargs['external_request_id']}/{kwargs['request_hash']}.json"
            ),
            request_hash=kwargs["request_hash"],
            response_schema_hash=DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
            response_sha256=sha256(response_body).hexdigest(),
            response_bytes=len(response_body),
        )

    def read_ledger_extraction_response(self, stored, **kwargs):
        self.events.append("s3-head-get-hash")
        return self.response

    def recover_ledger_extraction_response(self, **kwargs):
        self.events.append("s3-recover-lookup")
        if not self.recoverable:
            raise LedgerExtractionRawResponseUnavailable("absent")
        stored = self.put_ledger_extraction_response(self.response, **kwargs)
        return stored, self.response


class _Exchange(PostgresRecoverableLedgerExtractionExchange):
    def __init__(self, *, events, row, transport, response_store, actor):
        super().__init__(
            dsn="postgresql://unit-test",
            worker_actor=actor,
            transport=transport,
            response_store=response_store,
        )
        self.events = events
        self.row = row
        self.outcomes: list[dict] = []
        self.last_exchange = None

    def _record_outcome(self, **kwargs):
        self.outcomes.append(kwargs)
        self.events.append(f"db-outcome-{kwargs['status']}")

    def _read_exchange_and_outcome(self, **kwargs):
        outcome = self.outcomes[-1] if self.outcomes else None
        if outcome is not None:
            outcome = {
                "status": outcome["status"],
                "error_code": outcome["error_code"],
            }
        return self.last_exchange, outcome

    def _start_exchange(self, *, request):
        exchange = super()._start_exchange(request=request)
        self.last_exchange = exchange
        return exchange


class LedgerExtractionExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.run_id = str(uuid4())
        self.graph_id = str(uuid4())
        self.task_id = str(uuid4())
        self.attempt_id = str(uuid4())
        self.actor = Actor(
            str(uuid4()),
            self.firm_id,
            frozenset({Role.SYSTEM_WORKER}),
        )
        self.request = _prepared(self.attempt_id)
        self.row = {
            "submission_id": str(uuid4()),
            "external_request_id": self.request.external_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "matter_id": self.matter_id,
            "graph_id": self.graph_id,
            "input_hash": "a" * 64,
            "input_refs": [f"evidence-page:{uuid4()}"],
            "sandbox_policy_hash": "b" * 64,
            "current_graph_id": self.graph_id,
            "current_graph_hash": "c" * 64,
            "graph_hash": "c" * 64,
            "snapshot_matter_version": 7,
            "matter_version": 7,
        }

    def test_prepared_request_requires_the_fixed_provider_output_cap(self) -> None:
        payload = json.loads(self.request.body)
        self.assertEqual(
            payload["max_tokens"], DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS
        )
        payload.pop("max_tokens")
        uncapped_body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        uncapped = PreparedLedgerExtractionRequest(
            external_request_id=self.request.external_request_id,
            request_hash=sha256(uncapped_body).hexdigest(),
            body=uncapped_body,
        )
        with self.assertRaisesRegex(
            CaseAgentLedgerExtractionExchangeBlocked,
            "fixed request schema differs",
        ):
            exchange_module._prepared(uncapped)

    def test_system_prompt_contains_exact_json_contract_and_example(self) -> None:
        prompt = DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT
        for required in (
            "示例JSON输出",
            '"kind":"FACT"',
            '"kind":"TRANSACTION"',
            '"evidence_page_ids"',
            '"supporting_excerpts"',
            '"conflict_codes"',
            '"risk_codes"',
            '"date_precision":"EXACT_DATE"',
            '"amount":"100.00"',
        ):
            self.assertIn(required, prompt)
        self.assertIn("禁止使用type、statement、excerpt、page_number等别名", prompt)

    def test_prepared_request_accepts_and_validates_source_mode(self) -> None:
        exchange_module._prepared(self.request)
        payload = json.loads(self.request.body)
        content = json.loads(payload["messages"][1]["content"])
        content["pages"][0]["source_mode"] = "UNBOUND_TEXT"
        payload["messages"][1]["content"] = json.dumps(
            content, ensure_ascii=False
        )
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        invalid = PreparedLedgerExtractionRequest(
            external_request_id=self.request.external_request_id,
            request_hash=sha256(body).hexdigest(),
            body=body,
        )
        with self.assertRaisesRegex(
            CaseAgentLedgerExtractionExchangeBlocked,
            "page differs from fixed schema",
        ):
            exchange_module._prepared(invalid)

    def _runtime(self, response: bytes | Exception):
        events: list[str] = []
        transport = _Transport(events, response)
        store = _Store(events, _response())
        runtime = _Exchange(
            events=events,
            row=self.row,
            transport=transport,
            response_store=store,
            actor=self.actor,
        )
        connection = _StartConnection(self.row)

        @contextmanager
        def transaction(_dsn, _actor, *, read_only):
            self.assertFalse(read_only)
            events.append("db-0045-begin")
            yield connection
            events.append("db-0045-commit")

        return runtime, transport, store, events, transaction

    def test_0031_bound_exchange_commit_precedes_only_https_call_and_raw_parse(self):
        runtime, transport, _store, events, transaction = self._runtime(
            _response()
        )
        with patch.object(exchange_module, "_transaction", transaction):
            result = runtime.send(request=self.request)
        self.assertEqual(result, _response())
        self.assertEqual(transport.calls, 1)
        self.assertLess(events.index("db-0045-commit"), events.index("https-send"))
        self.assertLess(events.index("https-send"), events.index("s3-put"))
        self.assertLess(events.index("s3-put"), events.index("s3-head-get-hash"))
        self.assertLess(
            events.index("s3-head-get-hash"),
            events.index("db-outcome-SUCCEEDED"),
        )

    def test_exchange_creation_failure_is_known_before_any_provider_call(self):
        runtime, transport, _store, events, _transaction = self._runtime(
            _response()
        )
        with patch.object(
            runtime,
            "_start_exchange",
            side_effect=PermissionError("database write denied"),
        ):
            with self.assertRaises(LedgerExtractionKnownFailure) as caught:
                runtime.send(request=self.request)

        self.assertEqual(caught.exception.error_code, "LEDGER_EXCHANGE_NOT_CREATED")
        self.assertEqual(transport.calls, 0)
        self.assertNotIn("https-send", events)

    def test_missing_exchange_recovers_as_known_pre_dispatch_failure(self):
        runtime, transport, _store, _events, _transaction = self._runtime(
            _response()
        )
        with patch.object(
            runtime,
            "_read_exchange_and_outcome",
            return_value=(None, None),
        ):
            recovered = runtime.recover(
                external_request_id=self.request.external_request_id,
            )

        self.assertEqual(recovered.status, "FAILED")
        self.assertEqual(recovered.error_code, "LEDGER_EXCHANGE_NOT_CREATED")
        self.assertEqual(transport.calls, 0)

    def test_timeout_is_unknown_and_recovery_is_lookup_only_never_resend(self):
        runtime, transport, store, events, transaction = self._runtime(
            LedgerExtractionUnknownSubmission("timeout")
        )
        with patch.object(exchange_module, "_transaction", transaction):
            with self.assertRaises(LedgerExtractionUnknownSubmission):
                runtime.send(request=self.request)
        self.assertEqual(transport.calls, 1)
        store.recoverable = False
        recovered = runtime.recover(
            external_request_id=self.request.external_request_id,
        )
        self.assertEqual(recovered.status, "UNRESOLVED")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(events.count("https-send"), 1)
        self.assertIn("s3-recover-lookup", events)

    def test_invalid_provider_binding_is_failed_only_after_private_reread(self):
        runtime, transport, _store, events, transaction = self._runtime(
            _response(model="wrong-model")
        )
        with patch.object(exchange_module, "_transaction", transaction):
            with self.assertRaises(LedgerExtractionKnownFailure):
                runtime.send(request=self.request)
        self.assertEqual(transport.calls, 1)
        self.assertLess(events.index("s3-put"), events.index("s3-head-get-hash"))
        self.assertLess(
            events.index("s3-head-get-hash"),
            events.index("db-outcome-FAILED"),
        )

    def test_commit_lost_outcome_replay_requires_exact_locator_bytes_and_version(self):
        events: list[str] = []
        transport = _Transport(events, _response())
        store = _Store(events, _response())
        runtime = PostgresRecoverableLedgerExtractionExchange(
            dsn="postgresql://unit-test",
            worker_actor=self.actor,
            transport=transport,
            response_store=store,
        )
        exchange = exchange_module._ExchangeRecord(
            exchange_id=str(uuid4()),
            external_request_id=self.request.external_request_id,
            run_id=self.run_id,
            graph_id=self.graph_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            matter_id=self.matter_id,
            task_input_hash="a" * 64,
            input_refs_hash="b" * 64,
            request_hash=self.request.request_hash,
        )
        response = _response()
        stored = StoredLedgerExtractionResponse(
            object_key=(
                "case-agent-ledger-extractions/v1/"
                f"{self.firm_id}/{self.matter_id}/"
                f"{self.request.external_request_id}/"
                f"{self.request.request_hash}.json"
            ),
            request_hash=self.request.request_hash,
            response_schema_hash=DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
            response_sha256=sha256(response).hexdigest(),
            response_bytes=len(response),
            provider_response_id_hash=sha256(
                b"chatcmpl-ledger-1"
            ).hexdigest(),
            object_version_id="version-1",
        )
        exact = {
            "external_request_id": self.request.external_request_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "outcome_sequence": 1,
            "status": "SUCCEEDED",
            "request_hash": self.request.request_hash,
            "response_schema_hash": DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
            "provider_response_id_hash": stored.provider_response_id_hash,
            "response_sha256": stored.response_sha256,
            "response_bytes": stored.response_bytes,
            "response_object_key": stored.object_key,
            "response_object_version_id": stored.object_version_id,
            "error_code": None,
            "recovered_from_unknown": False,
        }

        class Connection:
            def __init__(self, row):
                self.row = row

            def execute(self, sql, args=()):
                if "INSERT INTO case_agent_ledger_extraction_outcomes" in sql:
                    return _Cursor(one=None)
                if "SELECT external_request_id" in sql:
                    return _Cursor(one=self.row)
                raise AssertionError(" ".join(sql.split()))

        def transaction_for(row):
            @contextmanager
            def transaction(_dsn, _actor, *, read_only):
                self.assertFalse(read_only)
                yield Connection(row)

            return transaction

        # Simulates INSERT losing its acknowledgement while the identical row
        # was already committed: ON CONFLICT + exact read-back is accepted.
        with patch.object(
            exchange_module, "_transaction", transaction_for(dict(exact))
        ):
            runtime._record_outcome(
                exchange=exchange,
                sequence=1,
                status="SUCCEEDED",
                stored=stored,
                error_code=None,
                recovered=False,
            )

        for field, wrong in (
            ("response_bytes", stored.response_bytes + 1),
            ("response_object_key", stored.object_key + ".wrong"),
            ("response_object_version_id", "version-2"),
        ):
            with self.subTest(field=field):
                mismatched = dict(exact)
                mismatched[field] = wrong
                with patch.object(
                    exchange_module,
                    "_transaction",
                    transaction_for(mismatched),
                ):
                    with self.assertRaises(
                        CaseAgentLedgerExtractionExchangeBlocked
                    ):
                        runtime._record_outcome(
                            exchange=exchange,
                            sequence=1,
                            status="SUCCEEDED",
                            stored=stored,
                            error_code=None,
                            recovered=False,
                        )

    def test_unknown_outcome_replay_rejects_any_existing_private_locator(self):
        events: list[str] = []
        runtime = PostgresRecoverableLedgerExtractionExchange(
            dsn="postgresql://unit-test",
            worker_actor=self.actor,
            transport=_Transport(events, _response()),
            response_store=_Store(events, _response()),
        )
        exchange = exchange_module._ExchangeRecord(
            exchange_id=str(uuid4()),
            external_request_id=self.request.external_request_id,
            run_id=self.run_id,
            graph_id=self.graph_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            matter_id=self.matter_id,
            task_input_hash="a" * 64,
            input_refs_hash="b" * 64,
            request_hash=self.request.request_hash,
        )
        exact = {
            "external_request_id": self.request.external_request_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "outcome_sequence": 1,
            "status": "UNKNOWN_SUBMISSION",
            "request_hash": self.request.request_hash,
            "response_schema_hash": DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
            "provider_response_id_hash": None,
            "response_sha256": None,
            "response_bytes": None,
            "response_object_key": None,
            "response_object_version_id": None,
            "error_code": "LEDGER_EXTRACTION_OUTCOME_UNKNOWN",
            "recovered_from_unknown": False,
        }

        class Connection:
            def __init__(self, row):
                self.row = row

            def execute(self, sql, args=()):
                if "INSERT INTO case_agent_ledger_extraction_outcomes" in sql:
                    return _Cursor(one=None)
                if "SELECT external_request_id" in sql:
                    return _Cursor(one=self.row)
                raise AssertionError(" ".join(sql.split()))

        def transaction_for(row):
            @contextmanager
            def transaction(_dsn, _actor, *, read_only):
                yield Connection(row)

            return transaction

        for field, wrong in (
            ("provider_response_id_hash", "c" * 64),
            ("response_sha256", "d" * 64),
            ("response_bytes", 2),
            ("response_object_key", "must-not-exist"),
            ("response_object_version_id", "must-not-exist"),
        ):
            with self.subTest(field=field):
                mismatched = dict(exact)
                mismatched[field] = wrong
                with patch.object(
                    exchange_module,
                    "_transaction",
                    transaction_for(mismatched),
                ):
                    with self.assertRaises(
                        CaseAgentLedgerExtractionExchangeBlocked
                    ):
                        runtime._record_outcome(
                            exchange=exchange,
                            sequence=1,
                            status="UNKNOWN_SUBMISSION",
                            stored=None,
                            error_code="LEDGER_EXTRACTION_OUTCOME_UNKNOWN",
                            recovered=False,
                        )


class _S3Client:
    def __init__(self):
        self.objects = {}
        self.head_calls = []

    def put_object(self, **kwargs):
        body = kwargs["Body"].read()
        self.objects[kwargs["Key"]] = (body, dict(kwargs), "version-1")
        return {"VersionId": "version-1"}

    def head_object(self, **kwargs):
        self.head_calls.append(dict(kwargs))
        body, request, version = self.objects[kwargs["Key"]]
        return {
            "ContentLength": len(body),
            "ChecksumSHA256": request["ChecksumSHA256"],
            "ContentType": request["ContentType"],
            "ServerSideEncryption": request["ServerSideEncryption"],
            "Metadata": request["Metadata"],
            "VersionId": version,
        }

    def get_object(self, **kwargs):
        body, _, _ = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(body)}


class LedgerExtractionPrivateResponseStoreTests(unittest.TestCase):
    def test_store_accepts_unparsed_raw_bytes_then_proves_head_get_hash(self):
        config = S3PrivateObjectStoreConfig(
            endpoint_url="https://objects.lawfirm.internal",
            region_name="cn-south-1",
            bucket="lawcase-private",
            access_key_id="worker",
            secret_access_key="s" * 32,
            server_side_encryption="AES256",
            kms_key_id=None,
            allow_insecure_internal_endpoint=False,
        )
        client = _S3Client()
        store = S3LedgerExtractionRawResponseStore(config, client=client)
        firm_id, matter_id, request_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        # Deliberately not a provider envelope: storage must not parse it.
        body = b'{"raw":"provider bytes first"}'
        request_hash = "d" * 64
        stored = store.put_ledger_extraction_response(
            body,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=request_id,
            request_hash=request_hash,
        )
        self.assertIsNone(stored.provider_response_id_hash)
        self.assertEqual(
            store.read_ledger_extraction_response(
                stored,
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=request_id,
            ),
            body,
        )
        metadata = next(iter(client.objects.values()))[1]["Metadata"]
        self.assertNotIn("lawcase-ledger-provider-id-hash", metadata)
        self.assertEqual(
            metadata["lawcase-ledger-schema-hash"],
            DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
        )
        self.assertGreaterEqual(len(client.head_calls), 3)
        self.assertTrue(
            all(
                call.get("ChecksumMode") == "ENABLED"
                for call in client.head_calls
            )
        )


class LedgerExtractionHttpsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.attempt_id = str(uuid4())
        self.request = _prepared(self.attempt_id)

    def test_redirect_response_is_known_failure_and_never_followed(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect denied",
                    hdrs=None,
                    fp=None,
                )

        with patch.object(
            exchange_module.urllib.request,
            "build_opener",
            return_value=Opener(),
        ):
            with self.assertRaises(LedgerExtractionKnownFailure) as caught:
                exchange_module._urlopen_raw_transport(
                    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
                    {"Authorization": "Bearer redacted"},
                    self.request.body,
                    30,
                )
        self.assertEqual(caught.exception.error_code, "LEDGER_PROVIDER_HTTP_REJECTED")

    def test_non_json_media_type_is_rejected_before_body_is_used(self):
        class Headers:
            def get_content_type(self):
                return "text/html"

        class Response:
            status = 200
            headers = Headers()
            read_called = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def geturl(self):
                return DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT

            def read(self, maximum):
                self.read_called = True
                return _response()

        response = Response()

        class Opener:
            def open(self, request, timeout):
                return response

        with patch.object(
            exchange_module.urllib.request,
            "build_opener",
            return_value=Opener(),
        ):
            with self.assertRaises(LedgerExtractionKnownFailure) as caught:
                exchange_module._urlopen_raw_transport(
                    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
                    {},
                    self.request.body,
                    30,
                )
        self.assertEqual(
            caught.exception.error_code,
            "LEDGER_PROVIDER_HTTP_BINDING_INVALID",
        )
        self.assertFalse(response.read_called)

    def test_transport_repr_and_clean_exception_never_expose_api_key(self):
        secret = "deepseek-secret-" + "z" * 40

        def uncertain(endpoint, headers, body, timeout):
            self.assertIn(secret, headers["Authorization"])
            raise TimeoutError(f"socket timeout with {secret}")

        transport = DeepSeekLedgerExtractionRawHttpsTransport(
            credentials=DeepSeekLedgerExtractionCredentials(api_key=secret),
            transport=uncertain,
        )
        self.assertNotIn(secret, repr(transport))
        with self.assertRaises(LedgerExtractionUnknownSubmission) as caught:
            transport.send_raw(request=self.request)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_tls_connect_failure_is_known_before_any_http_request(self):
        transport = DeepSeekLedgerExtractionRawHttpsTransport(
            credentials=DeepSeekLedgerExtractionCredentials(
                api_key="deepseek-secret-" + "z" * 40
            )
        )
        with patch.object(
            exchange_module.socket,
            "getaddrinfo",
            return_value=[(None, None, None, None, ("1.1.1.1", 443))],
        ), patch.object(
            exchange_module,
            "_open_pinned_tls_connection",
            side_effect=OSError("TLS unavailable"),
        ):
            with self.assertRaises(LedgerExtractionKnownFailure) as caught:
                transport.send_raw(request=self.request)
        self.assertEqual(
            caught.exception.error_code,
            "LEDGER_PROVIDER_CONNECT_FAILED",
        )


if __name__ == "__main__":
    unittest.main()
