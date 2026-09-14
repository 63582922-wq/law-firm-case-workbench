"""Durable one-call DeepSeek exchange for review-only ledger extraction.

The Worker first commits the generic 0031 ``STARTED`` marker.  This exchange
then commits an immutable 0045 binding to that exact run/task/attempt before it
may perform one HTTPS call.  Raw provider bytes are written to encrypted
private object storage and authenticated with HEAD/GET/SHA-256 before their
fixed response envelope is inspected or returned to the extraction adapter.

Recovery is lookup-only.  An unknown request is never sent again: the only
permitted recovery source is the deterministic private object key created by
the original call.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from hashlib import sha256
import http.client
import io
from ipaddress import ip_address
import json
import math
import re
import socket
import ssl
from typing import Any, Callable, Iterator, Mapping, Protocol
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_ledger_extraction import ExtractionSourceMode
from .case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_HOST,
    DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS,
    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
    DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
    LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED,
    LEDGER_EXTRACTION_PROVIDER_CONNECT_FAILED,
    LEDGER_EXTRACTION_PROVIDER_DNS_FAILED,
    LedgerExtractionKnownFailure,
    PreparedLedgerExtractionRequest,
    RecoveredLedgerExtraction,
)
from .deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
)
from .models import Actor, Role
from .web_object_store import S3CompatibleClient, S3PrivateObjectStoreConfig


DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT = (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT
)
DEEPSEEK_LEDGER_EXTRACTION_SERVICE_ID = "deepseek-ledger-extraction"
DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_ID = (
    "deepseek-case-ledger-extraction-response-v1"
)
_RESPONSE_SCHEMA_DESCRIPTOR = {
    "schema_id": DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_ID,
    "provider_envelope": {
        "model": DEEPSEEK_LEDGER_EXTRACTION_MODEL,
        "one_choice": True,
        "assistant_json_object": True,
    },
    "candidate_kinds": ("FACT", "TRANSACTION"),
    "source_binding": ("evidence_page_ids", "supporting_excerpts"),
    "review_only": True,
}
DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH = sha256(
    json.dumps(
        _RESPONSE_SCHEMA_DESCRIPTOR,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_REQUEST_BYTES = 32 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_VERSION = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")


class CaseAgentLedgerExtractionExchangeBlocked(RuntimeError):
    """Durable request, private response, or database binding differs."""


class LedgerExtractionUnknownSubmission(RuntimeError):
    """The call may have crossed the provider boundary; never resend it."""


class LedgerExtractionRawResponseUnavailable(RuntimeError):
    """No authenticated raw response is currently discoverable for recovery."""


@dataclass(frozen=True, repr=False)
class DeepSeekLedgerExtractionCredentials:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or self.api_key != self.api_key.strip()
            or not 20 <= len(self.api_key) <= 500
            or "\x00" in self.api_key
        ):
            raise ValueError("DeepSeek ledger extraction API key is invalid")

    def __repr__(self) -> str:
        return "DeepSeekLedgerExtractionCredentials(api_key=<redacted>)"


@dataclass(frozen=True)
class StoredLedgerExtractionResponse:
    object_key: str = field(repr=False)
    request_hash: str
    response_schema_hash: str
    response_sha256: str
    response_bytes: int
    provider_response_id_hash: str | None = None
    object_version_id: str | None = field(default=None, repr=False)


class LedgerExtractionRawResponseStore(Protocol):
    def put_ledger_extraction_response(
        self,
        response_body: bytes,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> StoredLedgerExtractionResponse: ...

    def read_ledger_extraction_response(
        self,
        stored: StoredLedgerExtractionResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> bytes: ...

    def recover_ledger_extraction_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> tuple[StoredLedgerExtractionResponse, bytes]: ...


class LedgerExtractionRawTransport(Protocol):
    endpoint: str
    model_id: str
    response_schema_hash: str

    def send_raw(self, *, request: PreparedLedgerExtractionRequest) -> bytes: ...


class _ChecksumAwareLedgerExtractionS3Client:
    """Require S3-compatible stores to return the persisted SHA-256 receipt."""

    def __init__(self, delegate: S3CompatibleClient) -> None:
        self._delegate = delegate

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        request = dict(kwargs)
        request.setdefault("ChecksumMode", "ENABLED")
        return self._delegate.head_object(**request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class DeepSeekLedgerExtractionRawHttpsTransport:
    """Exactly one POST to the fixed official DeepSeek endpoint."""

    endpoint = DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
    model_id = DEEPSEEK_LEDGER_EXTRACTION_MODEL
    response_schema_hash = DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH

    def __init__(
        self,
        *,
        credentials: DeepSeekLedgerExtractionCredentials,
        timeout_seconds: float = 90.0,
        transport: Callable[[str, Mapping[str, str], bytes, float], bytes]
        | None = None,
    ) -> None:
        if not isinstance(credentials, DeepSeekLedgerExtractionCredentials):
            raise ValueError("DeepSeek ledger extraction credentials are required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 1 <= float(timeout_seconds) <= 120
        ):
            raise ValueError("DeepSeek ledger extraction timeout is invalid")
        self._credentials = credentials
        self._timeout = float(timeout_seconds)
        self._transport = transport or _pinned_raw_transport

    def __repr__(self) -> str:
        return (
            "DeepSeekLedgerExtractionRawHttpsTransport("
            f"endpoint={self.endpoint!r}, model_id={self.model_id!r}, "
            "credentials=<redacted>)"
        )

    def send_raw(self, *, request: PreparedLedgerExtractionRequest) -> bytes:
        _prepared(request)
        headers = {
            "Authorization": f"Bearer {self._credentials.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            body = self._transport(
                self.endpoint,
                headers,
                request.body,
                self._timeout,
            )
        except LedgerExtractionKnownFailure:
            raise
        except LedgerExtractionUnknownSubmission:
            raise
        except (TimeoutError, ConnectionError, OSError) as error:
            # Provider/client exceptions may echo headers or credential-bearing
            # request objects.  Do not chain them into application logs.
            _ = error
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction submission result is unknown"
            ) from None
        if not isinstance(body, bytes) or not 2 <= len(body) <= _MAX_RESPONSE_BYTES:
            raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_RESPONSE_SIZE_INVALID")
        return body


class S3LedgerExtractionRawResponseStore:
    """Encrypted deterministic S3 storage for exact raw provider bytes."""

    def __init__(
        self,
        config: S3PrivateObjectStoreConfig,
        *,
        client: S3CompatibleClient | None = None,
    ) -> None:
        if not isinstance(config, S3PrivateObjectStoreConfig):
            raise ValueError("ledger extraction object-store configuration is required")
        self._config = config
        self._client = _ChecksumAwareLedgerExtractionS3Client(
            client or _new_s3_client(config)
        )
        for method in ("put_object", "head_object", "get_object"):
            if not callable(getattr(self._client, method, None)):
                raise ValueError("ledger extraction object-store client is incomplete")

    def put_ledger_extraction_response(
        self,
        response_body: bytes,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> StoredLedgerExtractionResponse:
        _store_identity(
            response_body=response_body,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=external_request_id,
            request_hash=request_hash,
        )
        response_hash = sha256(response_body).hexdigest()
        key = _response_key(firm_id, matter_id, external_request_id, request_hash)
        checksum = base64.b64encode(bytes.fromhex(response_hash)).decode("ascii")
        metadata = _response_metadata(
            external_request_id=external_request_id,
            request_hash=request_hash,
            response_sha256=response_hash,
            response_bytes=len(response_body),
        )
        write: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
            "Body": io.BytesIO(response_body),
            "ContentLength": len(response_body),
            "ContentType": "application/json",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": metadata,
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            write["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            result = self._client.put_object(**write)
            version = result.get("VersionId") if isinstance(result, dict) else None
            _object_version(version)
            lookup: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": key,
            }
            if version is not None:
                lookup["VersionId"] = version
            _validate_head(
                self._client.head_object(**lookup),
                byte_size=len(response_body),
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
        except CaseAgentLedgerExtractionExchangeBlocked:
            raise
        except Exception as error:
            raise LedgerExtractionUnknownSubmission(
                "ledger extraction response storage result is unknown"
            ) from error
        return StoredLedgerExtractionResponse(
            object_key=key,
            request_hash=request_hash,
            response_schema_hash=DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
            response_sha256=response_hash,
            response_bytes=len(response_body),
            object_version_id=version,
        )

    def read_ledger_extraction_response(
        self,
        stored: StoredLedgerExtractionResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> bytes:
        _stored(stored, firm_id, matter_id, external_request_id)
        lookup: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            lookup["VersionId"] = stored.object_version_id
        checksum = base64.b64encode(
            bytes.fromhex(stored.response_sha256)
        ).decode("ascii")
        metadata = _response_metadata(
            external_request_id=external_request_id,
            request_hash=stored.request_hash,
            response_sha256=stored.response_sha256,
            response_bytes=stored.response_bytes,
        )
        try:
            _validate_head(
                self._client.head_object(**lookup),
                byte_size=stored.response_bytes,
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
            response = self._client.get_object(**lookup)
            stream = response.get("Body") if isinstance(response, dict) else None
            if callable(getattr(stream, "read", None)):
                body = stream.read(stored.response_bytes + 1)
            elif isinstance(stream, (bytes, bytearray)):
                body = bytes(stream)
            else:
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction raw response body is unavailable"
                )
            if (
                not isinstance(body, bytes)
                or len(body) != stored.response_bytes
                or sha256(body).hexdigest() != stored.response_sha256
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction raw response differs from its receipt"
                )
            _validate_head(
                self._client.head_object(**lookup),
                byte_size=stored.response_bytes,
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
            return body
        except CaseAgentLedgerExtractionExchangeBlocked:
            raise
        except Exception as error:
            raise LedgerExtractionUnknownSubmission(
                "ledger extraction raw response could not be authenticated"
            ) from error

    def recover_ledger_extraction_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> tuple[StoredLedgerExtractionResponse, bytes]:
        _tenant_request(firm_id, matter_id, external_request_id, request_hash)
        key = _response_key(firm_id, matter_id, external_request_id, request_hash)
        try:
            head = self._client.head_object(
                Bucket=self._config.bucket,
                Key=key,
            )
        except Exception as error:
            raise LedgerExtractionRawResponseUnavailable(
                "ledger extraction raw response is not discoverable"
            ) from error
        metadata = head.get("Metadata") if isinstance(head, dict) else None
        if not isinstance(metadata, dict):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction raw response metadata is unavailable"
            )
        normalized = {
            str(name).lower(): str(value) for name, value in metadata.items()
        }
        if (
            normalized.get("lawcase-ledger-request-id") != external_request_id
            or normalized.get("lawcase-ledger-request-hash") != request_hash
            or normalized.get("lawcase-ledger-schema-hash")
            != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
        ):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction raw response binding differs"
            )
        version = head.get("VersionId")
        _object_version(version)
        try:
            stored = StoredLedgerExtractionResponse(
                object_key=key,
                request_hash=request_hash,
                response_schema_hash=DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
                response_sha256=str(
                    normalized.get("lawcase-ledger-response-sha256", "")
                ),
                response_bytes=int(
                    normalized.get("lawcase-ledger-response-bytes", "0")
                ),
                object_version_id=version,
            )
        except (TypeError, ValueError) as error:
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction raw response receipt is invalid"
            ) from error
        body = self.read_ledger_extraction_response(
            stored,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=external_request_id,
        )
        return stored, body


class PostgresRecoverableLedgerExtractionExchange:
    """0031 STARTED -> immutable 0045 bind -> one call -> private bytes."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        transport: LedgerExtractionRawTransport,
        response_store: LedgerExtractionRawResponseStore,
    ) -> None:
        _worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("ledger extraction PostgreSQL DSN is required")
        _transport(transport)
        _response_store(response_store)
        self._dsn = dsn
        self._worker = worker_actor
        self._transport = transport
        self._objects = response_store

    def send(self, *, request: PreparedLedgerExtractionRequest) -> bytes:
        _prepared(request)
        try:
            exchange = self._start_exchange(request=request)
        except LedgerExtractionKnownFailure:
            raise
        except Exception:
            # send_raw is ordered strictly after the 0045 transaction commits.
            # If that transaction did not produce an exchange, the provider
            # was definitely not called and the failure is known, not unknown.
            raise LedgerExtractionKnownFailure(
                LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED
            ) from None
        try:
            response = self._transport.send_raw(request=request)
        except LedgerExtractionKnownFailure as failure:
            self._record_known_failure_or_unknown(
                exchange=exchange,
                error_code=failure.error_code,
            )
            raise
        except Exception as error:
            self._record_unknown_best_effort(exchange=exchange)
            if isinstance(error, LedgerExtractionUnknownSubmission):
                raise
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction submission result is unknown"
            ) from error

        try:
            stored = self._objects.put_ledger_extraction_response(
                response,
                firm_id=self._worker.firm_id,
                matter_id=exchange.matter_id,
                external_request_id=exchange.external_request_id,
                request_hash=exchange.request_hash,
            )
            response = self._objects.read_ledger_extraction_response(
                stored,
                firm_id=self._worker.firm_id,
                matter_id=exchange.matter_id,
                external_request_id=exchange.external_request_id,
            )
        except LedgerExtractionKnownFailure as failure:
            self._record_known_failure_or_unknown(
                exchange=exchange,
                error_code=failure.error_code,
            )
            raise
        except Exception as error:
            self._record_unknown_best_effort(exchange=exchange)
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction response durability is unknown"
            ) from error

        bound_stored: StoredLedgerExtractionResponse | None = None
        try:
            provider_id_hash = _provider_response_id_hash(response)
            stored = replace(
                stored, provider_response_id_hash=provider_id_hash
            )
            bound_stored = stored
            _validate_provider_response(
                response,
                expected_provider_response_id_hash=provider_id_hash,
            )
        except LedgerExtractionKnownFailure as failure:
            self._record_outcome(
                exchange=exchange,
                sequence=1,
                status="FAILED",
                stored=bound_stored,
                error_code=failure.error_code,
                recovered=False,
            )
            raise
        try:
            self._record_outcome(
                exchange=exchange,
                sequence=1,
                status="SUCCEEDED",
                stored=stored,
                error_code=None,
                recovered=False,
            )
        except Exception as error:
            self._record_unknown_best_effort(exchange=exchange)
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction durable outcome is unknown"
            ) from error
        return response

    def recover(
        self, *, external_request_id: str
    ) -> RecoveredLedgerExtraction:
        _uuid(external_request_id, "external_request_id")
        exchange, outcome = self._read_exchange_and_outcome(
            external_request_id=external_request_id,
        )
        if exchange is None:
            # The implementation cannot call send_raw before the immutable
            # 0045 exchange commits.  Absence therefore proves no provider
            # submission and must not create an endless reconciliation loop.
            return RecoveredLedgerExtraction(
                status="FAILED",
                error_code=LEDGER_EXTRACTION_EXCHANGE_NOT_CREATED,
            )
        if outcome is not None and outcome["status"] == "FAILED":
            return RecoveredLedgerExtraction(
                status="FAILED",
                error_code=str(outcome["error_code"]),
            )
        if outcome is not None and outcome["status"] == "SUCCEEDED":
            stored = _stored_from_outcome(outcome)
            response = self._objects.read_ledger_extraction_response(
                stored,
                firm_id=self._worker.firm_id,
                matter_id=exchange.matter_id,
                external_request_id=external_request_id,
            )
            _validate_provider_response(
                response,
                expected_provider_response_id_hash=(
                    stored.provider_response_id_hash
                ),
            )
            return RecoveredLedgerExtraction(
                status="SUCCEEDED", response_body=response
            )

        # This branch is lookup-only.  It never invokes transport.send_raw.
        try:
            stored, response = self._objects.recover_ledger_extraction_response(
                firm_id=self._worker.firm_id,
                matter_id=exchange.matter_id,
                external_request_id=external_request_id,
                request_hash=exchange.request_hash,
            )
        except LedgerExtractionRawResponseUnavailable:
            return RecoveredLedgerExtraction(status="UNRESOLVED")
        self._ensure_unknown_outcome(exchange=exchange, outcome=outcome)
        bound_stored = None
        try:
            provider_id_hash = _provider_response_id_hash(response)
            stored = replace(
                stored, provider_response_id_hash=provider_id_hash
            )
            bound_stored = stored
            _validate_provider_response(
                response,
                expected_provider_response_id_hash=provider_id_hash,
            )
        except LedgerExtractionKnownFailure as failure:
            self._record_outcome(
                exchange=exchange,
                sequence=2,
                status="FAILED",
                stored=bound_stored,
                error_code=failure.error_code,
                recovered=True,
            )
            return RecoveredLedgerExtraction(
                status="FAILED", error_code=failure.error_code
            )
        self._record_outcome(
            exchange=exchange,
            sequence=2,
            status="SUCCEEDED",
            stored=stored,
            error_code=None,
            recovered=True,
        )
        return RecoveredLedgerExtraction(
            status="SUCCEEDED", response_body=response
        )

    def _start_exchange(
        self, *, request: PreparedLedgerExtractionRequest
    ) -> "_ExchangeRecord":
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            row = connection.execute(
                """
                SELECT submission.submission_id,
                       submission.external_request_id,
                       submission.run_id, submission.task_id,
                       submission.attempt_id, submission.matter_id,
                       task.graph_id, task.input_hash, task.input_refs,
                       task.sandbox_policy_hash,
                       run.current_graph_id, run.current_graph_hash,
                       graph.graph_hash, graph.snapshot_matter_version,
                       matter.version AS matter_version
                FROM case_agent_external_submissions submission
                JOIN case_agent_task_attempts attempt
                  ON attempt.attempt_id = submission.attempt_id
                 AND attempt.run_id = submission.run_id
                 AND attempt.task_id = submission.task_id
                 AND attempt.firm_id = submission.firm_id
                 AND attempt.matter_id = submission.matter_id
                JOIN case_agent_tasks task
                  ON task.graph_id = attempt.graph_id
                 AND task.task_id = attempt.task_id
                 AND task.run_id = attempt.run_id
                 AND task.firm_id = attempt.firm_id
                 AND task.matter_id = attempt.matter_id
                JOIN case_agent_runs run
                  ON run.run_id = task.run_id AND run.firm_id = task.firm_id
                 AND run.matter_id = task.matter_id
                JOIN case_agent_task_graphs graph
                  ON graph.graph_id = task.graph_id AND graph.run_id = task.run_id
                 AND graph.firm_id = task.firm_id AND graph.matter_id = task.matter_id
                JOIN matters matter
                  ON matter.matter_id = task.matter_id
                 AND matter.firm_id = task.firm_id
                JOIN matter_actor_roles worker_role
                  ON worker_role.firm_id = task.firm_id
                 AND worker_role.matter_id = task.matter_id
                 AND worker_role.user_id = %s
                 AND worker_role.role = 'SYSTEM_WORKER'
                 AND worker_role.revoked_at IS NULL
                JOIN users worker
                  ON worker.user_id = worker_role.user_id
                 AND worker.firm_id = worker_role.firm_id
                 AND worker.status = 'ACTIVE'
                WHERE submission.firm_id = %s
                  AND submission.external_request_id = %s
                  AND submission.request_hash = %s
                  AND submission.destination = 'api.deepseek.com'
                  AND submission.submission_state = 'STARTED'
                  AND submission.recorded_by = %s
                  AND attempt.status = 'RUNNING'
                  AND task.skill_id = 'case_ledger_extraction'
                  AND task.tool_id = 'extract_case_ledger'
                  AND task.adapter_id = 'deepseek-case-ledger-extraction'
                FOR UPDATE OF attempt
                """,
                (
                    self._worker.actor_id,
                    self._worker.firm_id,
                    request.external_request_id,
                    request.request_hash,
                    self._worker.actor_id,
                ),
            ).fetchone()
            if row is None:
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction 0031 STARTED boundary is absent or differs"
                )
            attempt_id = str(row["attempt_id"])
            expected_external_id = str(
                uuid5(UUID(attempt_id), request.request_hash)
            )
            if (
                str(row["external_request_id"])
                != request.external_request_id
                or expected_external_id != request.external_request_id
                or str(row["current_graph_id"]) != str(row["graph_id"])
                or row["current_graph_hash"] != row["graph_hash"]
                or int(row["snapshot_matter_version"])
                != int(row["matter_version"])
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction current run/task/attempt binding changed"
                )
            prior = connection.execute(
                """
                SELECT 1 FROM case_agent_ledger_extraction_exchanges
                 WHERE firm_id = %s
                   AND (external_request_id = %s OR attempt_id = %s)
                """,
                (
                    self._worker.firm_id,
                    request.external_request_id,
                    attempt_id,
                ),
            ).fetchone()
            if prior is not None:
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction request already crossed its one-call boundary"
                )
            exchange_id = str(
                uuid5(
                    UUID(request.external_request_id),
                    "ledger-extraction-exchange-v1",
                )
            )
            input_refs_hash = sha256(
                json.dumps(
                    tuple(row["input_refs"]),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_exchanges (
                    exchange_id, external_request_id, run_id, graph_id,
                    task_id, attempt_id, firm_id, matter_id,
                    submission_record_id, task_input_hash, input_refs_hash,
                    request_hash, endpoint_url, endpoint_host, provider_id,
                    service_id, model_id, response_schema_id,
                    response_schema_hash, started_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,'deepseek',%s,%s,%s,%s,%s
                )
                """,
                (
                    exchange_id,
                    request.external_request_id,
                    str(row["run_id"]),
                    str(row["graph_id"]),
                    str(row["task_id"]),
                    attempt_id,
                    self._worker.firm_id,
                    str(row["matter_id"]),
                    str(row["submission_id"]),
                    str(row["input_hash"]),
                    input_refs_hash,
                    request.request_hash,
                    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
                    DEEPSEEK_LEDGER_EXTRACTION_HOST,
                    DEEPSEEK_LEDGER_EXTRACTION_SERVICE_ID,
                    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
                    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_ID,
                    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
                    self._worker.actor_id,
                ),
            )
        # Exiting the transaction commits the 0045 exchange before send_raw.
        return _ExchangeRecord(
            exchange_id=exchange_id,
            external_request_id=request.external_request_id,
            run_id=str(row["run_id"]),
            graph_id=str(row["graph_id"]),
            task_id=str(row["task_id"]),
            attempt_id=attempt_id,
            matter_id=str(row["matter_id"]),
            task_input_hash=str(row["input_hash"]),
            input_refs_hash=input_refs_hash,
            request_hash=request.request_hash,
        )

    def _read_exchange_and_outcome(
        self, *, external_request_id: str
    ) -> tuple["_ExchangeRecord | None", Mapping[str, Any] | None]:
        with _transaction(self._dsn, self._worker, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT exchange_id, external_request_id, run_id, graph_id,
                       task_id, attempt_id, matter_id, task_input_hash,
                       input_refs_hash, request_hash, endpoint_url,
                       endpoint_host, provider_id, service_id, model_id,
                       response_schema_id, response_schema_hash
                  FROM case_agent_ledger_extraction_exchanges
                 WHERE firm_id = %s AND external_request_id = %s
                """,
                (self._worker.firm_id, external_request_id),
            ).fetchone()
            if row is None:
                return None, None
            exchange = _ExchangeRecord.from_row(row)
            outcome = connection.execute(
                """
                SELECT status, outcome_sequence, request_hash,
                       response_schema_hash, provider_response_id_hash,
                       response_sha256, response_bytes, response_object_key,
                       response_object_version_id, error_code,
                       recovered_from_unknown
                  FROM case_agent_ledger_extraction_outcomes
                 WHERE exchange_id = %s AND external_request_id = %s
                   AND firm_id = %s AND matter_id = %s
                 ORDER BY outcome_sequence DESC LIMIT 1
                """,
                (
                    exchange.exchange_id,
                    external_request_id,
                    self._worker.firm_id,
                    exchange.matter_id,
                ),
            ).fetchone()
        if outcome is not None and (
            outcome["request_hash"] != exchange.request_hash
            or outcome["response_schema_hash"]
            != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
        ):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction recovery outcome binding differs"
            )
        return exchange, outcome

    def _record_known_failure_or_unknown(
        self, *, exchange: "_ExchangeRecord", error_code: str
    ) -> None:
        try:
            self._record_outcome(
                exchange=exchange,
                sequence=1,
                status="FAILED",
                stored=None,
                error_code=error_code,
                recovered=False,
            )
        except Exception as error:
            self._record_unknown_best_effort(exchange=exchange)
            raise LedgerExtractionUnknownSubmission(
                "ledger extraction failure durability is unknown"
            ) from error

    def _record_unknown_best_effort(self, *, exchange: "_ExchangeRecord") -> None:
        try:
            self._record_outcome(
                exchange=exchange,
                sequence=1,
                status="UNKNOWN_SUBMISSION",
                stored=None,
                error_code="LEDGER_EXTRACTION_OUTCOME_UNKNOWN",
                recovered=False,
            )
        except Exception:
            pass

    def _ensure_unknown_outcome(
        self,
        *,
        exchange: "_ExchangeRecord",
        outcome: Mapping[str, Any] | None,
    ) -> None:
        if outcome is not None:
            if outcome["status"] != "UNKNOWN_SUBMISSION":
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction recovery requires an unknown outcome"
                )
            return
        self._record_outcome(
            exchange=exchange,
            sequence=1,
            status="UNKNOWN_SUBMISSION",
            stored=None,
            error_code="LEDGER_EXTRACTION_OUTCOME_UNKNOWN",
            recovered=False,
        )

    def _record_outcome(
        self,
        *,
        exchange: "_ExchangeRecord",
        sequence: int,
        status: str,
        stored: StoredLedgerExtractionResponse | None,
        error_code: str | None,
        recovered: bool,
    ) -> None:
        _exact_exchange(exchange, request_hash=exchange.request_hash)
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION"}:
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction outcome status is invalid"
            )
        if error_code is not None and _ERROR.fullmatch(error_code) is None:
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction outcome error code is invalid"
            )
        if stored is not None:
            _stored(
                stored,
                self._worker.firm_id,
                exchange.matter_id,
                exchange.external_request_id,
            )
            if stored.provider_response_id_hash is None:
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction outcome lacks provider response binding"
                )
        outcome_id = str(
            uuid5(
                UUID(exchange.exchange_id),
                f"ledger-extraction-outcome:{sequence}",
            )
        )
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, outcome_sequence, status, request_hash,
                    response_schema_hash, provider_response_id_hash,
                    response_sha256, response_bytes, response_object_key,
                    response_object_version_id, error_code,
                    recovered_from_unknown, recorded_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                ) ON CONFLICT (exchange_id, outcome_sequence) DO NOTHING
                """,
                (
                    outcome_id,
                    exchange.exchange_id,
                    exchange.external_request_id,
                    self._worker.firm_id,
                    exchange.matter_id,
                    sequence,
                    status,
                    exchange.request_hash,
                    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
                    stored.provider_response_id_hash if stored else None,
                    stored.response_sha256 if stored else None,
                    stored.response_bytes if stored else None,
                    stored.object_key if stored else None,
                    stored.object_version_id if stored else None,
                    error_code,
                    recovered,
                    self._worker.actor_id,
                ),
            )
            row = connection.execute(
                """
                SELECT external_request_id, firm_id, matter_id,
                       outcome_sequence, status, request_hash,
                       response_schema_hash, provider_response_id_hash,
                       response_sha256, response_bytes,
                       response_object_key, response_object_version_id,
                       error_code, recovered_from_unknown
                  FROM case_agent_ledger_extraction_outcomes
                 WHERE exchange_id = %s AND outcome_sequence = %s
                   AND firm_id = %s AND matter_id = %s
                """,
                (
                    exchange.exchange_id,
                    sequence,
                    self._worker.firm_id,
                    exchange.matter_id,
                ),
            ).fetchone()
            if (
                row is None
                or str(row["external_request_id"])
                != exchange.external_request_id
                or str(row["firm_id"]) != self._worker.firm_id
                or str(row["matter_id"]) != exchange.matter_id
                or int(row["outcome_sequence"]) != sequence
                or row["status"] != status
                or row["request_hash"] != exchange.request_hash
                or row["response_schema_hash"]
                != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
                or row["error_code"] != error_code
                or bool(row["recovered_from_unknown"]) != recovered
                or row["provider_response_id_hash"]
                != (
                    stored.provider_response_id_hash
                    if stored is not None
                    else None
                )
                or row["response_sha256"]
                != (stored.response_sha256 if stored is not None else None)
                or row["response_bytes"]
                != (stored.response_bytes if stored is not None else None)
                or row["response_object_key"]
                != (stored.object_key if stored is not None else None)
                or row["response_object_version_id"]
                != (
                    stored.object_version_id
                    if stored is not None
                    else None
                )
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "ledger extraction outcome could not be proven"
                )


@dataclass(frozen=True)
class _ExchangeRecord:
    exchange_id: str
    external_request_id: str
    run_id: str
    graph_id: str
    task_id: str
    attempt_id: str
    matter_id: str
    task_input_hash: str
    input_refs_hash: str
    request_hash: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "_ExchangeRecord":
        if (
            row["endpoint_url"] != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
            or row["endpoint_host"] != DEEPSEEK_LEDGER_EXTRACTION_HOST
            or row["provider_id"] != "deepseek"
            or row["service_id"] != DEEPSEEK_LEDGER_EXTRACTION_SERVICE_ID
            or row["model_id"] != DEEPSEEK_LEDGER_EXTRACTION_MODEL
            or row["response_schema_id"]
            != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_ID
            or row["response_schema_hash"]
            != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
        ):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction fixed provider contract differs"
            )
        return cls(
            exchange_id=str(row["exchange_id"]),
            external_request_id=str(row["external_request_id"]),
            run_id=str(row["run_id"]),
            graph_id=str(row["graph_id"]),
            task_id=str(row["task_id"]),
            attempt_id=str(row["attempt_id"]),
            matter_id=str(row["matter_id"]),
            task_input_hash=str(row["task_input_hash"]),
            input_refs_hash=str(row["input_refs_hash"]),
            request_hash=str(row["request_hash"]),
        )


def preflight_case_agent_ledger_extraction_runtime_contract(
    *,
    dsn: str,
    worker_actor: Actor,
    transport: LedgerExtractionRawTransport,
    response_store: LedgerExtractionRawResponseStore,
) -> None:
    """Prove 0045/transport/store before Worker readiness can be composed."""

    _worker(worker_actor)
    if not isinstance(dsn, str) or not dsn.strip():
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction preflight PostgreSQL DSN is invalid"
        )
    _transport(transport)
    _response_store(response_store)
    required = {
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
    try:
        with _transaction(dsn, worker_actor, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            observed: dict[str, set[str]] = {}
            for row in rows:
                observed.setdefault(str(row["table_name"]), set()).add(
                    str(row["column_name"])
                )
            if any(
                not columns.issubset(observed.get(table, set()))
                for table, columns in required.items()
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "case-Agent ledger extraction migration 0045 is incomplete"
                )
            constraint_rows = connection.execute(
                """
                SELECT relation.relname AS table_name,
                       pg_get_constraintdef(constraint_row.oid) AS definition
                  FROM pg_catalog.pg_constraint constraint_row
                  JOIN pg_catalog.pg_class relation
                    ON relation.oid = constraint_row.conrelid
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                   AND constraint_row.contype IN ('c', 'f', 'u')
                """,
                (list(required),),
            ).fetchall()
            definitions: dict[str, tuple[str, ...]] = {}
            for row in constraint_rows:
                table = str(row["table_name"])
                definitions[table] = definitions.get(table, ()) + (
                    str(row["definition"]),
                )
            exchange_contract = "\n".join(
                definitions.get("case_agent_ledger_extraction_exchanges", ())
            )
            outcome_contract = "\n".join(
                definitions.get("case_agent_ledger_extraction_outcomes", ())
            )
            if not all(
                expected in exchange_contract
                for expected in (
                    "UNIQUE (external_request_id)",
                    "FOREIGN KEY (attempt_id, graph_id, task_id, run_id, firm_id, matter_id)",
                    "REFERENCES case_agent_task_attempts",
                    "FOREIGN KEY (submission_record_id, firm_id, matter_id)",
                    "REFERENCES case_agent_external_submissions",
                    DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT,
                    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
                    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
                )
            ) or not all(
                expected in outcome_contract
                for expected in (
                    "FOREIGN KEY (exchange_id, external_request_id, firm_id, matter_id)",
                    "REFERENCES case_agent_ledger_extraction_exchanges",
                    "UNKNOWN_SUBMISSION",
                    "case-agent-ledger-extractions/v1",
                    DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH,
                )
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "case-Agent ledger extraction fixed 0045 constraints differ"
                )
            trigger_rows = connection.execute(
                """
                SELECT trigger_name FROM information_schema.triggers
                 WHERE trigger_schema = 'public' AND trigger_name = ANY(%s)
                """,
                (list(triggers),),
            ).fetchall()
            if not triggers.issubset(
                {str(row["trigger_name"]) for row in trigger_rows}
            ):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "case-Agent ledger extraction guards are incomplete"
                )
            rls_rows = connection.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                  FROM pg_catalog.pg_class JOIN pg_catalog.pg_namespace
                    ON pg_namespace.oid = pg_class.relnamespace
                 WHERE pg_namespace.nspname = 'public' AND relname = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            rls = {
                str(row["relname"]): (
                    bool(row["relrowsecurity"]),
                    bool(row["relforcerowsecurity"]),
                )
                for row in rls_rows
            }
            if any(rls.get(table) != (True, True) for table in required):
                raise CaseAgentLedgerExtractionExchangeBlocked(
                    "case-Agent ledger extraction exchange requires FORCE RLS"
                )
    except CaseAgentLedgerExtractionExchangeBlocked:
        raise
    except Exception as error:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "case-Agent ledger extraction exchange preflight failed"
        ) from error


def _prepared(value: PreparedLedgerExtractionRequest) -> None:
    if (
        not isinstance(value, PreparedLedgerExtractionRequest)
        or not isinstance(value.body, bytes)
        or not 2 <= len(value.body) <= _MAX_REQUEST_BYTES
        or sha256(value.body).hexdigest() != value.request_hash
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction request hash differs"
        )
    _uuid(value.external_request_id, "external_request_id")
    _hash(value.request_hash, "request_hash")
    try:
        body = json.loads(value.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction request is not JSON"
        ) from error
    if (
        not isinstance(body, dict)
        or set(body) != {
            "model", "temperature", "max_tokens", "response_format", "messages"
        }
        or body["model"] != DEEPSEEK_LEDGER_EXTRACTION_MODEL
        or body["temperature"] != 0
        or body["max_tokens"] != DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS
        or body["response_format"] != {"type": "json_object"}
        or not isinstance(body["messages"], list)
        or len(body["messages"]) != 2
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction fixed request schema differs"
        )
    system, user = body["messages"]
    if system != {
        "role": "system",
        "content": DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
    } or not isinstance(user, dict) or set(user) != {"role", "content"} or user["role"] != "user":
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction message contract differs"
        )
    try:
        content = json.loads(user["content"])
    except (TypeError, json.JSONDecodeError) as error:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction page projection is invalid"
        ) from error
    pages = content.get("pages") if isinstance(content, dict) and set(content) == {"pages"} else None
    if not isinstance(pages, list) or not pages or len(pages) > 2_000:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "prepared ledger extraction page projection is invalid"
        )
    for page in pages:
        if (
            not isinstance(page, dict)
            or set(page)
            != {"evidence_page_id", "page_number", "source_mode", "text"}
            or type(page["page_number"]) is not int
            or page["page_number"] < 1
            or page["source_mode"]
            not in {item.value for item in ExtractionSourceMode}
            or not isinstance(page["text"], str)
            or not page["text"]
            or len(page["text"].encode("utf-8")) > 4 * 1024 * 1024
        ):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "prepared ledger extraction page differs from fixed schema"
            )
        _uuid(page["evidence_page_id"], "evidence_page_id")


def _provider_response_id_hash(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8"))
        response_id = value["id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise LedgerExtractionKnownFailure(
            "LEDGER_PROVIDER_ENVELOPE_INVALID"
        ) from error
    if (
        not isinstance(response_id, str)
        or response_id != response_id.strip()
        or not 1 <= len(response_id) <= 500
        or any(ord(character) < 32 for character in response_id)
    ):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_RESPONSE_ID_INVALID")
    return sha256(response_id.encode("utf-8")).hexdigest()


def _validate_provider_response(
    body: bytes, *, expected_provider_response_id_hash: str | None
) -> str:
    if expected_provider_response_id_hash is not None:
        _hash(expected_provider_response_id_hash, "provider_response_id_hash")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LedgerExtractionKnownFailure(
            "LEDGER_PROVIDER_ENVELOPE_INVALID"
        ) from error
    provider_response_id_hash = _provider_response_id_hash(body)
    if (
        not isinstance(value, dict)
        or value.get("model") != DEEPSEEK_LEDGER_EXTRACTION_MODEL
        or (
            expected_provider_response_id_hash is not None
            and provider_response_id_hash
            != expected_provider_response_id_hash
        )
    ):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_BINDING_INVALID")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_CHOICES_INVALID")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if (
        not isinstance(message, dict)
        or message.get("role") not in {None, "assistant"}
        or not isinstance(message.get("content"), (str, dict))
    ):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_MESSAGE_INVALID")
    content = message["content"]
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as error:
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_CONTENT_INVALID"
            ) from error
    if not isinstance(content, dict) or set(content) != {"candidates"}:
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    candidates = content["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 500:
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    for candidate in candidates:
        _validate_response_candidate(candidate)
    return provider_response_id_hash


def _validate_response_candidate(candidate: Any) -> None:
    if not isinstance(candidate, dict):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    common = {
        "kind", "evidence_page_ids", "confidence", "conflict_codes",
        "risk_codes", "supporting_excerpts",
    }
    kind = candidate.get("kind")
    if kind == "FACT":
        allowed = common | {"fact_text"}
        required = common | {"fact_text"}
    elif kind == "TRANSACTION":
        allowed = common | {
            "local_date", "date_precision", "amount", "currency",
            "direction", "payer_label", "payee_label", "channel",
            "transaction_reference",
        }
        required = common | {
            "date_precision", "amount", "currency", "direction", "channel",
        }
    else:
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    if not required.issubset(candidate) or not set(candidate).issubset(allowed):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    ids = candidate["evidence_page_ids"]
    excerpts = candidate["supporting_excerpts"]
    confidence = candidate["confidence"]
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 100
        or not isinstance(excerpts, list)
        or not excerpts
        or len(excerpts) > 100
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
        or not isinstance(candidate["conflict_codes"], list)
        or not isinstance(candidate["risk_codes"], list)
    ):
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
    for page_id in ids:
        _response_uuid(page_id)
    for excerpt in excerpts:
        if (
            not isinstance(excerpt, dict)
            or set(excerpt) != {"evidence_page_id", "text"}
            or not isinstance(excerpt["text"], str)
            or not 1 <= len(excerpt["text"]) <= 2_000
        ):
            raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_SCHEMA_INVALID")
        _response_uuid(excerpt["evidence_page_id"])


def _response_uuid(value: object) -> None:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise LedgerExtractionKnownFailure(
            "LEDGER_PROVIDER_SCHEMA_INVALID"
        ) from error


def _transport(value: Any) -> None:
    if (
        not callable(getattr(value, "send_raw", None))
        or getattr(value, "endpoint", None)
        != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
        or getattr(value, "model_id", None) != DEEPSEEK_LEDGER_EXTRACTION_MODEL
        or getattr(value, "response_schema_hash", None)
        != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction transport fixed contract differs"
        )


def _response_store(value: Any) -> None:
    for name in (
        "put_ledger_extraction_response",
        "read_ledger_extraction_response",
        "recover_ledger_extraction_response",
    ):
        if not callable(getattr(value, name, None)):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction private response store is incomplete"
            )


def _exact_exchange(exchange: _ExchangeRecord, *, request_hash: str) -> None:
    for value, label in (
        (exchange.exchange_id, "exchange_id"),
        (exchange.external_request_id, "external_request_id"),
        (exchange.run_id, "run_id"),
        (exchange.graph_id, "graph_id"),
        (exchange.task_id, "task_id"),
        (exchange.attempt_id, "attempt_id"),
        (exchange.matter_id, "matter_id"),
    ):
        _uuid(value, label)
    for value, label in (
        (exchange.task_input_hash, "task_input_hash"),
        (exchange.input_refs_hash, "input_refs_hash"),
        (exchange.request_hash, "exchange request_hash"),
        (request_hash, "request_hash"),
    ):
        _hash(value, label)
    if exchange.request_hash != request_hash:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction exchange request hash differs"
        )


def _stored_from_outcome(row: Mapping[str, Any]) -> StoredLedgerExtractionResponse:
    return StoredLedgerExtractionResponse(
        object_key=str(row["response_object_key"]),
        request_hash=str(row["request_hash"]),
        response_schema_hash=str(row["response_schema_hash"]),
        provider_response_id_hash=str(row["provider_response_id_hash"]),
        response_sha256=str(row["response_sha256"]),
        response_bytes=int(row["response_bytes"]),
        object_version_id=(
            str(row["response_object_version_id"])
            if row["response_object_version_id"] is not None
            else None
        ),
    )


def _store_identity(
    *,
    response_body: bytes,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
    request_hash: str,
) -> None:
    _tenant_request(firm_id, matter_id, external_request_id, request_hash)
    if not isinstance(response_body, bytes) or not 2 <= len(response_body) <= _MAX_RESPONSE_BYTES:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction raw response bytes are invalid"
        )


def _tenant_request(
    firm_id: str, matter_id: str, external_request_id: str, request_hash: str
) -> None:
    for value, label in (
        (firm_id, "firm_id"),
        (matter_id, "matter_id"),
        (external_request_id, "external_request_id"),
    ):
        _uuid(value, label)
    _hash(request_hash, "request_hash")


def _stored(
    value: StoredLedgerExtractionResponse,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
) -> None:
    if (
        not isinstance(value, StoredLedgerExtractionResponse)
        or value.object_key
        != _response_key(
            firm_id, matter_id, external_request_id, value.request_hash
        )
        or value.response_schema_hash
        != DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
        or not 2 <= value.response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction raw response locator is invalid"
        )
    for item, label in (
        (value.request_hash, "stored request_hash"),
        (value.response_sha256, "stored response_sha256"),
    ):
        _hash(item, label)
    if value.provider_response_id_hash is not None:
        _hash(
            value.provider_response_id_hash,
            "stored provider_response_id_hash",
        )
    _object_version(value.object_version_id)


def _response_key(
    firm_id: str,
    matter_id: str,
    external_request_id: str,
    request_hash: str,
) -> str:
    return (
        f"case-agent-ledger-extractions/v1/{firm_id}/{matter_id}/"
        f"{external_request_id}/{request_hash}.json"
    )


def _response_metadata(
    *,
    external_request_id: str,
    request_hash: str,
    response_sha256: str,
    response_bytes: int,
) -> dict[str, str]:
    return {
        "lawcase-ledger-request-id": external_request_id,
        "lawcase-ledger-request-hash": request_hash,
        "lawcase-ledger-schema-hash": (
            DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH
        ),
        "lawcase-ledger-response-sha256": response_sha256,
        "lawcase-ledger-response-bytes": str(response_bytes),
    }


def _validate_head(
    value: Any,
    *,
    byte_size: int,
    checksum: str,
    metadata: Mapping[str, str],
    encryption: str,
) -> None:
    if (
        not isinstance(value, dict)
        or value.get("ContentLength") != byte_size
        or value.get("ChecksumSHA256") != checksum
        or value.get("ContentType") != "application/json"
        or value.get("ServerSideEncryption") != encryption
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction raw response remote receipt differs"
        )
    actual = value.get("Metadata")
    if not isinstance(actual, dict) or {
        str(name).lower(): str(item) for name, item in actual.items()
    } != dict(metadata):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction raw response metadata differs"
        )


def _pinned_raw_transport(
    endpoint: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    """Send once over a DNS-pinned TLS connection.

    DNS and TLS connection failures occur before an HTTP request is written,
    so they are safe zero-call failures.  Once ``sendall`` begins, the
    provider may have received some or all of the request; every later I/O
    failure remains unknown and is never resubmitted.
    """
    _validate_pinned_raw_request(
        endpoint=endpoint,
        headers=headers,
        body=body,
        timeout_seconds=timeout_seconds,
    )
    request_headers = _pinned_raw_headers(headers, body)
    raw_head = (
        "POST /chat/completions HTTP/1.1\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
        + "\r\n"
    ).encode("ascii")
    try:
        answers = socket.getaddrinfo(
            DEEPSEEK_LEDGER_EXTRACTION_HOST,
            443,
            type=socket.SOCK_STREAM,
        )
        resolved = tuple(sorted({
            str(answer[4][0])
            for answer in answers
            if isinstance(answer, tuple) and len(answer) >= 5
        }))
        if not resolved:
            raise ValueError("DNS returned no address")
        for value in resolved:
            _require_global_ip(value)
    except Exception as error:
        raise LedgerExtractionKnownFailure(
            LEDGER_EXTRACTION_PROVIDER_DNS_FAILED
        ) from error

    connection: Any | None = None
    try:
        connect_error: Exception | None = None
        for candidate in resolved:
            candidate_connection = None
            try:
                candidate_connection = _open_pinned_tls_connection(
                    address=(candidate, 443),
                    timeout_seconds=timeout_seconds,
                    server_hostname=DEEPSEEK_LEDGER_EXTRACTION_HOST,
                )
                peer = str(candidate_connection.getpeername()[0])
                _require_global_ip(peer)
                if peer != candidate:
                    raise ValueError("connected peer differs from pinned address")
            except Exception as error:
                connect_error = error
                if candidate_connection is not None:
                    try:
                        candidate_connection.close()
                    except Exception:
                        pass
                continue
            connection = candidate_connection
            break
        if connection is None:
            raise LedgerExtractionKnownFailure(
                LEDGER_EXTRACTION_PROVIDER_CONNECT_FAILED
            ) from connect_error

        try:
            connection.sendall(raw_head + body)
        except Exception as error:
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction submission result is unknown"
            ) from error
        try:
            response = http.client.HTTPResponse(connection)
            response.begin()
        except Exception as error:
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction submission result is unknown"
            ) from error
        if 300 <= response.status < 400:
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_HTTP_REJECTED"
            )
        if not 200 <= response.status < 300:
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_HTTP_REJECTED"
            )
        content_type = (response.getheader("Content-Type") or "").split(
            ";", 1
        )[0].strip().lower()
        if content_type != "application/json":
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_HTTP_BINDING_INVALID"
            )
        encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_HTTP_BINDING_INVALID"
            )
        declared = response.getheader("Content-Length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except (TypeError, ValueError):
                raise LedgerExtractionKnownFailure(
                    "LEDGER_PROVIDER_RESPONSE_SIZE_INVALID"
                ) from None
            if not 2 <= declared_bytes <= _MAX_RESPONSE_BYTES:
                raise LedgerExtractionKnownFailure(
                    "LEDGER_PROVIDER_RESPONSE_SIZE_INVALID"
                )
        try:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except Exception as error:
            raise LedgerExtractionUnknownSubmission(
                "DeepSeek ledger extraction submission result is unknown"
            ) from error
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise LedgerExtractionKnownFailure(
                "LEDGER_PROVIDER_RESPONSE_SIZE_INVALID"
            )
        return payload
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _validate_pinned_raw_request(
    *,
    endpoint: object,
    headers: object,
    body: object,
    timeout_seconds: object,
) -> None:
    if not isinstance(endpoint, str):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction endpoint differs"
        )
    parsed = urlsplit(endpoint)
    if (
        endpoint != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
        or parsed.scheme != "https"
        or parsed.hostname != DEEPSEEK_LEDGER_EXTRACTION_HOST
        or parsed.port not in {None, 443}
        or parsed.path != "/chat/completions"
        or parsed.query
        or parsed.fragment
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction endpoint differs"
        )
    if (
        not isinstance(headers, Mapping)
        or not isinstance(body, bytes)
        or not 2 <= len(body) <= _MAX_REQUEST_BYTES
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 1.0 <= float(timeout_seconds) <= 120.0
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction transport request is invalid"
        )
    _safe_request_headers(headers)


def _pinned_raw_headers(headers: Mapping[str, str], body: bytes) -> dict[str, str]:
    caller_headers = {str(name).lower(): str(value) for name, value in headers.items()}
    if set(caller_headers) != {"authorization", "content-type", "accept"}:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction request headers differ"
        )
    return {
        "Host": DEEPSEEK_LEDGER_EXTRACTION_HOST,
        "Authorization": caller_headers["authorization"],
        "Content-Type": caller_headers["content-type"],
        "Accept": caller_headers["accept"],
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Content-Length": str(len(body)),
    }


def _safe_request_headers(headers: Mapping[str, str]) -> None:
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-" for character in name)
            or "\r" in value
            or "\n" in value
            or not value.isascii()
            or not value
            or len(value) > 1024
        ):
            raise CaseAgentLedgerExtractionExchangeBlocked(
                "ledger extraction request header is invalid"
            )


def _open_pinned_tls_connection(
    *, address: tuple[str, int], timeout_seconds: float, server_hostname: str
) -> Any:
    raw = socket.create_connection(address, timeout=timeout_seconds)
    try:
        return ssl.create_default_context().wrap_socket(
            raw, server_hostname=server_hostname
        )
    except Exception:
        raw.close()
        raise


def _require_global_ip(value: object) -> None:
    try:
        parsed = ip_address(str(value))
    except ValueError:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction peer address is invalid"
        ) from None
    if not parsed.is_global:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction peer must be globally routable"
        )


def _urlopen_raw_transport(
    endpoint: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    if endpoint != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction endpoint differs"
        )
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            final_url = response.geturl()
            headers = response.headers
            get_content_type = getattr(headers, "get_content_type", None)
            content_type = (
                get_content_type()
                if callable(get_content_type)
                else str(headers.get("Content-Type", ""))
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if (
                final_url != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT
                or content_type != "application/json"
            ):
                raise LedgerExtractionKnownFailure(
                    "LEDGER_PROVIDER_HTTP_BINDING_INVALID"
                )
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if response.geturl() != DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT:
                raise LedgerExtractionKnownFailure(
                    "LEDGER_PROVIDER_HTTP_BINDING_INVALID"
                )
    except urllib.error.HTTPError as error:
        _ = error
        raise LedgerExtractionKnownFailure(
            "LEDGER_PROVIDER_HTTP_REJECTED"
        ) from None
    except urllib.error.URLError as error:
        _ = error
        raise LedgerExtractionUnknownSubmission(
            "DeepSeek ledger extraction submission result is unknown"
        ) from None
    if not 200 <= status < 300:
        raise LedgerExtractionKnownFailure("LEDGER_PROVIDER_HTTP_REJECTED")
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise LedgerExtractionKnownFailure(
            "LEDGER_PROVIDER_RESPONSE_SIZE_INVALID"
        )
    return payload


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _new_s3_client(config: S3PrivateObjectStoreConfig) -> S3CompatibleClient:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:  # pragma: no cover - deployment dependency.
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction S3 client is not installed"
        ) from error
    try:
        return boto3.session.Session().client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region_name,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=120,
                # The provider call is never retried.  These are only bounded
                # S3 control-plane retries against a deterministic object key.
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    except Exception as error:  # pragma: no cover - environment specific.
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction S3 client could not be configured"
        ) from error


@contextmanager
def _transaction(
    dsn: str, worker: Actor, *, read_only: bool
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            + (" READ ONLY" if read_only else "")
        )
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)",
            (worker.firm_id,),
        )
        connection.execute(
            "SELECT set_config('app.actor_id', %s, true)",
            (worker.actor_id,),
        )
        yield connection


def _worker(value: Actor) -> None:
    if (
        not isinstance(value, Actor)
        or value.roles != frozenset({Role.SYSTEM_WORKER})
    ):
        raise PermissionError(
            "ledger extraction exchange requires one dedicated SYSTEM_WORKER"
        )
    _uuid(value.actor_id, "worker actor_id")
    _uuid(value.firm_id, "worker firm_id")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            f"{label} is invalid"
        ) from error


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CaseAgentLedgerExtractionExchangeBlocked(
            f"{label} is invalid"
        )


def _object_version(value: object) -> None:
    if value is not None and (
        not isinstance(value, str)
        or _VERSION.fullmatch(value) is None
        or value != value.strip()
    ):
        raise CaseAgentLedgerExtractionExchangeBlocked(
            "ledger extraction object version is invalid"
        )


__all__ = (
    "CaseAgentLedgerExtractionExchangeBlocked",
    "DEEPSEEK_LEDGER_EXTRACTION_ENDPOINT",
    "DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_HASH",
    "DEEPSEEK_LEDGER_EXTRACTION_RESPONSE_SCHEMA_ID",
    "DeepSeekLedgerExtractionCredentials",
    "DeepSeekLedgerExtractionRawHttpsTransport",
    "LedgerExtractionRawResponseStore",
    "LedgerExtractionRawTransport",
    "LedgerExtractionRawResponseUnavailable",
    "LedgerExtractionUnknownSubmission",
    "PostgresRecoverableLedgerExtractionExchange",
    "S3LedgerExtractionRawResponseStore",
    "StoredLedgerExtractionResponse",
    "preflight_case_agent_ledger_extraction_runtime_contract",
)
