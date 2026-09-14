from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_kernel.case_agent_document_exchange_postgres import (
    CaseAgentDocumentExchangeBlocked,
    DeepSeekDocumentRawHttpsTransport,
    PostgresRecoverableDocumentDraftExchange,
    S3DocumentDraftRawResponseStore,
    StoredDocumentDraftResponse,
    preflight_case_agent_document_exchange_runtime_contract,
)
from case_kernel.case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
)
from case_kernel.deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
)
from case_kernel.deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftProvider,
    DeepSeekDocumentDraftRejected,
    DeepSeekDocumentDraftUnknownSubmission,
)
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import S3PrivateObjectStoreConfig
from backend.tests.test_case_agent_document_delivery import (
    CaseAgentDocumentDeliveryTests,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class Result:
    def __init__(self, *, one=None, many=()):
        self.one = one
        self.many = list(many)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class FakeDatabase:
    def __init__(self, boundary, events):
        self.boundary = dict(boundary)
        self.events = events
        self.exchange = None
        self.outcomes = {}
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=()):
        self.executions.append((sql, args))
        normalized = " ".join(sql.split())
        if "SET TRANSACTION" in normalized or "SELECT set_config" in normalized:
            return Result()
        if "FROM case_agent_external_submissions submission" in normalized:
            return Result(one=self.boundary)
        if (
            normalized.startswith("SELECT 1 FROM case_agent_document_draft_exchanges")
        ):
            return Result(one={"exists": 1} if self.exchange is not None else None)
        if normalized.startswith("INSERT INTO case_agent_document_draft_exchanges"):
            self.exchange = {
                "exchange_id": args[0],
                "external_request_id": args[1],
                "run_id": args[2],
                "graph_id": args[3],
                "task_id": args[4],
                "attempt_id": args[5],
                "firm_id": args[6],
                "matter_id": args[7],
                "request_hash": args[9],
                "binding_hash": args[10],
                "source_set_hash": args[11],
                "model_id": args[12],
            }
            self.events.append("insert-exchange")
            return Result()
        if normalized.startswith("INSERT INTO case_agent_document_draft_outcomes"):
            row = {
                "outcome_id": args[0],
                "exchange_id": args[1],
                "external_request_id": args[2],
                "firm_id": args[3],
                "matter_id": args[4],
                "outcome_sequence": args[5],
                "status": args[6],
                "request_hash": args[7],
                "binding_hash": args[8],
                "response_sha256": args[9],
                "response_bytes": args[10],
                "response_object_key": args[11],
                "response_object_version_id": args[12],
                "error_code": args[13],
                "recovered_from_unknown": args[14],
            }
            self.outcomes.setdefault((args[1], args[5]), row)
            self.events.append(f"insert-outcome-{args[6]}-{args[5]}")
            return Result()
        if (
            "SELECT status, request_hash, binding_hash, response_sha256," in normalized
            and "WHERE exchange_id =" in normalized
        ):
            return Result(one=self.outcomes.get((args[0], args[1])))
        if (
            "FROM case_agent_document_draft_exchanges" in normalized
            and "external_request_id =" in normalized
        ):
            if (
                self.exchange is not None
                and self.exchange["firm_id"] == args[0]
                and self.exchange["external_request_id"] == args[1]
            ):
                return Result(one=self.exchange)
            return Result(one=None)
        if (
            "FROM case_agent_document_draft_outcomes" in normalized
            and "ORDER BY outcome_sequence DESC" in normalized
        ):
            values = [
                row
                for (exchange_id, _), row in self.outcomes.items()
                if exchange_id == args[0]
            ]
            return Result(
                one=max(values, key=lambda row: row["outcome_sequence"])
                if values
                else None
            )
        raise AssertionError(f"unexpected SQL: {normalized}")


class RawTransport:
    model_id = "deepseek-chat"

    def __init__(self, response, events, *, failure=None):
        self.response = response
        self.events = events
        self.failure = failure
        self.calls = 0

    def send_raw(self, *, prepared):
        self.calls += 1
        self.events.append("network")
        if self.failure is not None:
            raise self.failure
        return self.response


class PrivateResponseStore:
    def __init__(self, events, *, body=None, recoverable=True):
        self.events = events
        self.body = body
        self.recoverable = recoverable
        self.stored = None

    def put_document_draft_response(self, response_body, **kwargs):
        self.events.append("store-put")
        self.body = response_body
        self.stored = StoredDocumentDraftResponse(
            object_key=(
                "case-agent-document-drafts/v1/"
                f"{kwargs['firm_id']}/{kwargs['matter_id']}/"
                f"{kwargs['external_request_id']}/{kwargs['request_hash']}.json"
            ),
            request_hash=kwargs["request_hash"],
            binding_hash=kwargs["binding_hash"],
            response_sha256=sha256(response_body).hexdigest(),
            response_bytes=len(response_body),
            object_version_id="v1",
        )
        return self.stored

    def read_document_draft_response(self, stored, **_kwargs):
        self.events.append("store-read")
        if self.body is None or sha256(self.body).hexdigest() != stored.response_sha256:
            raise CaseAgentDocumentExchangeBlocked("stored body differs")
        return self.body

    def recover_document_draft_response(self, **kwargs):
        self.events.append("store-recover")
        if not self.recoverable or self.body is None:
            raise CaseAgentDocumentExchangeBlocked("not found")
        if self.stored is None:
            self.stored = StoredDocumentDraftResponse(
                object_key=(
                    "case-agent-document-drafts/v1/"
                    f"{kwargs['firm_id']}/{kwargs['matter_id']}/"
                    f"{kwargs['external_request_id']}/{kwargs['request_hash']}.json"
                ),
                request_hash=kwargs["request_hash"],
                binding_hash=kwargs["binding_hash"],
                response_sha256=sha256(self.body).hexdigest(),
                response_bytes=len(self.body),
                object_version_id="v1",
            )
        return self.stored, self.body


class PreflightDatabase:
    COLUMNS = {
        "case_agent_document_draft_exchanges": {
            "exchange_id", "external_request_id", "run_id", "graph_id",
            "task_id", "attempt_id", "firm_id", "matter_id",
            "submission_record_id", "request_hash", "binding_hash",
            "source_set_hash", "endpoint_host", "provider_id", "model_id",
            "service_id", "started_by_worker",
        },
        "case_agent_document_draft_outcomes": {
            "outcome_id", "exchange_id", "external_request_id", "firm_id",
            "matter_id", "outcome_sequence", "status", "request_hash",
            "binding_hash", "response_sha256", "response_bytes",
            "response_object_key", "response_object_version_id",
            "error_code", "recovered_from_unknown", "recorded_by_worker",
        },
    }
    TRIGGERS = {
        "case_agent_document_draft_exchange_guard",
        "case_agent_document_draft_outcome_guard",
        "case_agent_document_draft_exchanges_append_only",
        "case_agent_document_draft_outcomes_append_only",
    }

    def __init__(self, *, force_rls=True, omit_trigger=None, policy_hashes_current=True):
        self.force_rls = force_rls
        self.omit_trigger = omit_trigger
        self.policy_hashes_current = policy_hashes_current

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, _args=()):
        if "information_schema.columns" in sql:
            return Result(
                many=(
                    {"table_name": table, "column_name": column}
                    for table, columns in self.COLUMNS.items()
                    for column in columns
                )
            )
        if "information_schema.triggers" in sql:
            return Result(
                many=(
                    {"trigger_name": item}
                    for item in self.TRIGGERS
                    if item != self.omit_trigger
                )
            )
        if "pg_catalog.pg_class" in sql:
            return Result(
                many=(
                    {
                        "relname": table,
                        "relrowsecurity": True,
                        "relforcerowsecurity": self.force_rls,
                    }
                    for table in self.COLUMNS
                )
            )
        if "pg_get_functiondef" in sql:
            definition = (
                DOCX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash
                + XLSX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash
                if self.policy_hashes_current
                else "stale-document-policy"
            )
            return Result(one={"definition": definition})
        return Result()


class S3Client:
    def __init__(self):
        self.body = None
        self.head = None

    def put_object(self, **kwargs):
        self.body = kwargs["Body"].read()
        self.head = {
            "ContentLength": kwargs["ContentLength"],
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "ContentType": kwargs["ContentType"],
            "ServerSideEncryption": kwargs["ServerSideEncryption"],
            "Metadata": kwargs["Metadata"],
            "VersionId": "version-1",
        }
        return {"VersionId": "version-1"}

    def head_object(self, **_kwargs):
        if self.head is None:
            raise KeyError("missing")
        return self.head

    def get_object(self, **_kwargs):
        return {"Body": io.BytesIO(self.body)}


class CaseAgentDocumentExchangePostgresTests(unittest.TestCase):
    def setUp(self):
        helper = CaseAgentDocumentDeliveryTests()
        self.binding = helper.binding()
        self.candidate_content = helper.response(self.binding)
        self.provider_response = json.dumps(
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": self.candidate_content.decode("utf-8")
                        },
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.provider = DeepSeekDocumentDraftProvider(
            credentials=DeepSeekDocumentDraftCredentials("k" * 40),
            config=DeepSeekDocumentDraftConfig(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model="deepseek-chat",
                allowed_models=("deepseek-chat",),
            ),
            transport=lambda *_: self.provider_response,
        )
        from case_kernel.case_agent_document_delivery import (
            build_document_draft_request,
        )

        self.prepared = self.provider.prepare(
            request=build_document_draft_request(self.binding),
            binding=self.binding,
        )
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=self.binding.firm_id,
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        self.events = []
        self.external_request_id = str(uuid4())
        self.boundary = {
            "submission_id": str(uuid4()),
            "external_request_id": self.external_request_id,
            "run_id": self.binding.run_id,
            "task_id": self.binding.task_id,
            "attempt_id": str(uuid4()),
            "matter_id": self.binding.matter_id,
            "graph_id": self.binding.graph_id,
            "input_hash": self.binding.task_input_hash,
            "tool_id": "draft_reviewable_docx_package",
            "adapter_id": "dynamic-reviewable-docx-delivery",
            "input_refs": [
                f"work-plan-item:{self.binding.work_plan_item.item_id}"
            ],
            "current_graph_id": self.binding.graph_id,
            "current_graph_hash": digest("graph"),
            "graph_hash": digest("graph"),
            "snapshot_matter_version": 4,
            "matter_version": 4,
        }

    def exchange(self, database, transport, store):
        return PostgresRecoverableDocumentDraftExchange(
            dsn="postgresql://server-owned",
            worker_actor=self.actor,
            transport=transport,
            response_store=store,
        )

    def test_success_is_exchange_then_network_then_private_store_then_outcome(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(self.provider_response, self.events)
        store = PrivateResponseStore(self.events)
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            candidate = self.exchange(database, transport, store).send(
                prepared=self.prepared,
                binding=self.binding,
            )
        self.assertEqual(candidate.binding_hash, self.binding.binding_hash)
        self.assertEqual(
            self.events,
            [
                "insert-exchange",
                "network",
                "store-put",
                "store-read",
                "insert-outcome-SUCCEEDED-1",
            ],
        )
        self.assertEqual(transport.calls, 1)
        self.assertNotIn(store.stored.object_key, repr(store.stored))

    def test_timeout_is_unknown_and_recovery_never_resends(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(
            self.provider_response,
            self.events,
            failure=TimeoutError("socket timeout"),
        )
        store = PrivateResponseStore(self.events, recoverable=False)
        broker = self.exchange(database, transport, store)
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            with self.assertRaises(DeepSeekDocumentDraftUnknownSubmission):
                broker.send(prepared=self.prepared, binding=self.binding)
            recovered = broker.recover(
                external_request_id=self.external_request_id,
                request_hash=self.prepared.request_hash,
                binding=self.binding,
            )
        self.assertEqual(recovered.status, "UNRESOLVED")
        self.assertEqual(transport.calls, 1)
        self.assertIn("insert-outcome-UNKNOWN_SUBMISSION-1", self.events)
        self.assertIn("store-recover", self.events)

    def test_unknown_can_reparse_only_an_already_persisted_exact_response(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(self.provider_response, self.events)
        store = PrivateResponseStore(self.events, body=self.provider_response)
        broker = self.exchange(database, transport, store)
        database.exchange = {
            "exchange_id": str(uuid4()),
            "external_request_id": self.external_request_id,
            "run_id": self.binding.run_id,
            "graph_id": self.binding.graph_id,
            "task_id": self.binding.task_id,
            "attempt_id": self.boundary["attempt_id"],
            "firm_id": self.binding.firm_id,
            "matter_id": self.binding.matter_id,
            "request_hash": self.prepared.request_hash,
            "binding_hash": self.binding.binding_hash,
            "source_set_hash": self.binding.source_set_hash,
            "model_id": "deepseek-chat",
        }
        database.outcomes[(database.exchange["exchange_id"], 1)] = {
            "status": "UNKNOWN_SUBMISSION",
            "outcome_sequence": 1,
            "request_hash": self.prepared.request_hash,
            "binding_hash": self.binding.binding_hash,
            "response_sha256": None,
            "response_bytes": None,
            "response_object_key": None,
            "response_object_version_id": None,
            "error_code": "DOCUMENT_DRAFT_OUTCOME_UNKNOWN",
            "recovered_from_unknown": False,
        }
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            recovered = broker.recover(
                external_request_id=self.external_request_id,
                request_hash=self.prepared.request_hash,
                binding=self.binding,
            )
        self.assertEqual(recovered.status, "SUCCEEDED")
        self.assertIsNotNone(recovered.candidate)
        self.assertEqual(transport.calls, 0)
        self.assertIn("insert-outcome-SUCCEEDED-2", self.events)

    def test_missing_outcome_is_append_only_unknown_then_recovered_success(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(self.provider_response, self.events)
        store = PrivateResponseStore(self.events, body=self.provider_response)
        broker = self.exchange(database, transport, store)
        database.exchange = {
            "exchange_id": str(uuid4()),
            "external_request_id": self.external_request_id,
            "run_id": self.binding.run_id,
            "graph_id": self.binding.graph_id,
            "task_id": self.binding.task_id,
            "attempt_id": self.boundary["attempt_id"],
            "firm_id": self.binding.firm_id,
            "matter_id": self.binding.matter_id,
            "request_hash": self.prepared.request_hash,
            "binding_hash": self.binding.binding_hash,
            "source_set_hash": self.binding.source_set_hash,
            "model_id": "deepseek-chat",
        }
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            recovered = broker.recover(
                external_request_id=self.external_request_id,
                request_hash=self.prepared.request_hash,
                binding=self.binding,
            )
        self.assertEqual(recovered.status, "SUCCEEDED")
        self.assertEqual(transport.calls, 0)
        self.assertIn("insert-outcome-UNKNOWN_SUBMISSION-1", self.events)
        self.assertIn("insert-outcome-SUCCEEDED-2", self.events)

    def test_known_provider_rejection_is_failed_and_recoverable_without_send(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(
            self.provider_response,
            self.events,
            failure=DeepSeekDocumentDraftRejected("known rejection"),
        )
        store = PrivateResponseStore(self.events, recoverable=False)
        broker = self.exchange(database, transport, store)
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            with self.assertRaises(DeepSeekDocumentDraftRejected):
                broker.send(prepared=self.prepared, binding=self.binding)
            recovered = broker.recover(
                external_request_id=self.external_request_id,
                request_hash=self.prepared.request_hash,
                binding=self.binding,
            )
        self.assertEqual(recovered.status, "FAILED")
        self.assertEqual(recovered.error_code, "DOCUMENT_PROVIDER_REJECTED")
        self.assertEqual(transport.calls, 1)

    def test_cross_case_cross_firm_task_or_hash_drift_is_blocked(self):
        database = FakeDatabase(self.boundary, self.events)
        transport = RawTransport(self.provider_response, self.events)
        store = PrivateResponseStore(self.events, recoverable=False)
        database.exchange = {
            "exchange_id": str(uuid4()),
            "external_request_id": self.external_request_id,
            "run_id": self.binding.run_id,
            "graph_id": self.binding.graph_id,
            "task_id": self.binding.task_id,
            "attempt_id": self.boundary["attempt_id"],
            "firm_id": self.binding.firm_id,
            "matter_id": self.binding.matter_id,
            "request_hash": self.prepared.request_hash,
            "binding_hash": self.binding.binding_hash,
            "source_set_hash": self.binding.source_set_hash,
            "model_id": "deepseek-chat",
        }
        broker = self.exchange(database, transport, store)
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=database,
        ):
            for changed, request_hash in (
                (replace(self.binding, matter_id=str(uuid4())), self.prepared.request_hash),
                (replace(self.binding, task_id=str(uuid4())), self.prepared.request_hash),
                (self.binding, digest("wrong-request")),
            ):
                with self.subTest(changed=changed, request_hash=request_hash):
                    with self.assertRaises(CaseAgentDocumentExchangeBlocked):
                        broker.recover(
                            external_request_id=self.external_request_id,
                            request_hash=request_hash,
                            binding=changed,
                        )
            other_firm = replace(self.binding, firm_id=str(uuid4()))
            with self.assertRaisesRegex(Exception, "tenant"):
                broker.recover(
                    external_request_id=self.external_request_id,
                    request_hash=self.prepared.request_hash,
                    binding=other_firm,
                )

    def test_raw_transport_repr_redacts_key_and_store_reauthenticates_bytes(self):
        config = DeepSeekDocumentDraftConfig(
            endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
            model="deepseek-chat",
            allowed_models=("deepseek-chat",),
        )
        secret = "s" * 40
        raw = DeepSeekDocumentRawHttpsTransport(
            credentials=DeepSeekDocumentDraftCredentials(secret),
            config=config,
            transport=lambda *_: self.provider_response,
        )
        self.assertNotIn(secret, repr(raw))

        client = S3Client()
        storage = S3DocumentDraftRawResponseStore(
            S3PrivateObjectStoreConfig(
                endpoint_url="https://objects.example.invalid",
                region_name="cn-test-1",
                bucket="lawcase-private",
                access_key_id="access-key",
                secret_access_key="x" * 32,
            ),
            client=client,
        )
        stored = storage.put_document_draft_response(
            self.provider_response,
            firm_id=self.binding.firm_id,
            matter_id=self.binding.matter_id,
            external_request_id=self.external_request_id,
            request_hash=self.prepared.request_hash,
            binding_hash=self.binding.binding_hash,
        )
        self.assertEqual(
            storage.read_document_draft_response(
                stored,
                firm_id=self.binding.firm_id,
                matter_id=self.binding.matter_id,
                external_request_id=self.external_request_id,
            ),
            self.provider_response,
        )
        self.assertNotIn(stored.object_key, repr(stored))
        client.body += b"tampered"
        with self.assertRaises(CaseAgentDocumentExchangeBlocked):
            storage.read_document_draft_response(
                stored,
                firm_id=self.binding.firm_id,
                matter_id=self.binding.matter_id,
                external_request_id=self.external_request_id,
            )

    def test_enabled_runtime_preflight_requires_0040_guards_and_force_rls(self):
        transport = RawTransport(self.provider_response, self.events)
        store = PrivateResponseStore(self.events)
        good = PreflightDatabase()
        with patch(
            "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
            return_value=good,
        ):
            preflight_case_agent_document_exchange_runtime_contract(
                dsn="postgresql://server-owned",
                worker_actor=self.actor,
                transport=transport,
                response_store=store,
            )
        for broken in (
            PreflightDatabase(force_rls=False),
            PreflightDatabase(
                omit_trigger="case_agent_document_draft_outcomes_append_only"
            ),
            PreflightDatabase(policy_hashes_current=False),
        ):
            with self.subTest(broken=broken.__dict__):
                with patch(
                    "case_kernel.case_agent_document_exchange_postgres.psycopg.connect",
                    return_value=broken,
                ):
                    with self.assertRaises(CaseAgentDocumentExchangeBlocked):
                        preflight_case_agent_document_exchange_runtime_contract(
                            dsn="postgresql://server-owned",
                            worker_actor=self.actor,
                            transport=transport,
                            response_store=store,
                        )


if __name__ == "__main__":
    unittest.main()
