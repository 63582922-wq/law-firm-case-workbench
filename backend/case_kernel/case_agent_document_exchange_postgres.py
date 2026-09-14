"""Recoverable DeepSeek exchange for dynamic reviewable document packages.

The dynamic document adapter commits the generic 0031 ``SUBMISSION_STARTED``
boundary before calling this exchange.  This module then binds that boundary
to the exact current task, performs at most one HTTPS submission, stores the
raw provider response in private encrypted object storage, and only after the
storage receipt is verified parses and returns a review-only candidate.

Recovery is deliberately lookup-only.  DeepSeek chat-completions exposes no
provider result lookup contract used by this product, so an absent private
response remains ``UNRESOLVED`` and is never submitted again.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import io
import re
from typing import Any, Callable, Iterator, Mapping, Protocol
import urllib.error
import urllib.request
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
    RecoveredDocumentDraft,
)
from .case_agent_document_delivery import (
    DynamicDocumentTaskBinding,
    ReviewableDocumentCandidate,
    ReviewableDocumentFormat,
    parse_reviewable_document_candidate,
)
from .deepseek_case_agent_planner import DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT
from .deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftNotSubmitted,
    DeepSeekDocumentDraftRejected,
    DeepSeekDocumentDraftUnknownSubmission,
    PreparedDeepSeekDocumentRequest,
    parse_deepseek_document_response,
)
from .models import Actor, Role
from .web_object_store import S3CompatibleClient, S3PrivateObjectStoreConfig


DEEPSEEK_DOCUMENT_HOST = "api.deepseek.com"
_MAX_RESPONSE_BYTES = 6 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


class CaseAgentDocumentExchangeBlocked(RuntimeError):
    """The request identity, private response or database contract differs."""


@dataclass(frozen=True)
class StoredDocumentDraftResponse:
    """Private object receipt; its locator and version never enter repr/Web."""

    object_key: str = field(repr=False)
    request_hash: str
    binding_hash: str
    response_sha256: str
    response_bytes: int
    object_version_id: str | None = field(default=None, repr=False)


class DocumentDraftRawResponseStore(Protocol):
    """Private, deterministic storage used before any candidate is returned."""

    def put_document_draft_response(
        self,
        response_body: bytes,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
        binding_hash: str,
    ) -> StoredDocumentDraftResponse: ...

    def read_document_draft_response(
        self,
        stored: StoredDocumentDraftResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> bytes: ...

    def recover_document_draft_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
        binding_hash: str,
    ) -> tuple[StoredDocumentDraftResponse, bytes]: ...


class DocumentDraftRawTransport(Protocol):
    model_id: str

    def send_raw(self, *, prepared: PreparedDeepSeekDocumentRequest) -> bytes: ...


class DeepSeekDocumentRawHttpsTransport:
    """One exact official chat-completions call returning unparsed bytes."""

    provider_id = "deepseek"
    service_id = "deepseek-document-drafting"

    def __init__(
        self,
        *,
        credentials: DeepSeekDocumentDraftCredentials,
        config: DeepSeekDocumentDraftConfig,
        transport: Callable[[str, Mapping[str, str], bytes, float], bytes]
        | None = None,
    ) -> None:
        if not isinstance(credentials, DeepSeekDocumentDraftCredentials):
            raise ValueError("DeepSeek document credentials are required")
        if not isinstance(config, DeepSeekDocumentDraftConfig):
            raise ValueError("DeepSeek document configuration is required")
        self._credentials = credentials
        self._config = config
        self._transport = transport or _urlopen_raw_transport
        self.model_id = config.model

    def __repr__(self) -> str:
        return (
            "DeepSeekDocumentRawHttpsTransport("
            f"model_id={self.model_id!r}, credentials=<redacted>)"
        )

    def send_raw(self, *, prepared: PreparedDeepSeekDocumentRequest) -> bytes:
        _prepared(prepared, expected_model=self.model_id)
        headers = {
            "Authorization": f"Bearer {self._credentials.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            body = self._transport(
                prepared.endpoint,
                headers,
                prepared.body,
                self._config.timeout_seconds,
            )
        except DeepSeekDocumentDraftRejected:
            raise
        except DeepSeekDocumentDraftUnknownSubmission:
            raise
        except (TimeoutError, ConnectionError, OSError) as error:
            raise DeepSeekDocumentDraftUnknownSubmission(
                "DeepSeek document submission result is unknown"
            ) from error
        if not isinstance(body, bytes) or not 2 <= len(body) <= _MAX_RESPONSE_BYTES:
            raise DeepSeekDocumentDraftRejected(
                "DeepSeek document raw response size is invalid"
            )
        return body


class S3DocumentDraftRawResponseStore:
    """Content-addressed private S3 storage for exact provider response bytes."""

    def __init__(
        self,
        config: S3PrivateObjectStoreConfig,
        *,
        client: S3CompatibleClient | None = None,
    ) -> None:
        if not isinstance(config, S3PrivateObjectStoreConfig):
            raise ValueError("document response object-store configuration is required")
        self._config = config
        self._client = client or _new_s3_client(config)
        for method in ("put_object", "head_object", "get_object"):
            if not callable(getattr(self._client, method, None)):
                raise ValueError("document response object-store client is incomplete")

    def put_document_draft_response(
        self,
        response_body: bytes,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
        binding_hash: str,
    ) -> StoredDocumentDraftResponse:
        _store_identity(
            response_body=response_body,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=external_request_id,
            request_hash=request_hash,
            binding_hash=binding_hash,
        )
        response_hash = sha256(response_body).hexdigest()
        key = _response_key(
            firm_id, matter_id, external_request_id, request_hash
        )
        checksum = base64.b64encode(bytes.fromhex(response_hash)).decode("ascii")
        metadata = _response_metadata(
            external_request_id=external_request_id,
            request_hash=request_hash,
            binding_hash=binding_hash,
            response_sha256=response_hash,
            response_bytes=len(response_body),
        )
        request: dict[str, Any] = {
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
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            result = self._client.put_object(**request)
            version = result.get("VersionId") if isinstance(result, dict) else None
            _version(version)
            head_request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": key,
            }
            if version is not None:
                head_request["VersionId"] = version
            _validate_head(
                self._client.head_object(**head_request),
                byte_size=len(response_body),
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
        except CaseAgentDocumentExchangeBlocked:
            raise
        except Exception as error:
            # Do not delete on an indeterminate object-store result.  Recovery
            # may be able to authenticate the deterministic key without a
            # second provider call.
            raise CaseAgentDocumentExchangeBlocked(
                "document provider response storage result is indeterminate"
            ) from error
        return StoredDocumentDraftResponse(
            object_key=key,
            request_hash=request_hash,
            binding_hash=binding_hash,
            response_sha256=response_hash,
            response_bytes=len(response_body),
            object_version_id=version,
        )

    def read_document_draft_response(
        self,
        stored: StoredDocumentDraftResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> bytes:
        _stored(stored, firm_id, matter_id, external_request_id)
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        metadata = _response_metadata(
            external_request_id=external_request_id,
            request_hash=stored.request_hash,
            binding_hash=stored.binding_hash,
            response_sha256=stored.response_sha256,
            response_bytes=stored.response_bytes,
        )
        checksum = base64.b64encode(
            bytes.fromhex(stored.response_sha256)
        ).decode("ascii")
        try:
            _validate_head(
                self._client.head_object(**request),
                byte_size=stored.response_bytes,
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
            response = self._client.get_object(**request)
            stream = response.get("Body") if isinstance(response, dict) else None
            if callable(getattr(stream, "read", None)):
                body = stream.read(stored.response_bytes + 1)
            elif isinstance(stream, (bytes, bytearray)):
                body = bytes(stream)
            else:
                raise CaseAgentDocumentExchangeBlocked(
                    "document provider response body is unavailable"
                )
            if (
                not isinstance(body, bytes)
                or len(body) != stored.response_bytes
                or sha256(body).hexdigest() != stored.response_sha256
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document provider response bytes differ from their receipt"
                )
            _validate_head(
                self._client.head_object(**request),
                byte_size=stored.response_bytes,
                checksum=checksum,
                metadata=metadata,
                encryption=self._config.server_side_encryption,
            )
            return body
        except CaseAgentDocumentExchangeBlocked:
            raise
        except Exception as error:
            raise CaseAgentDocumentExchangeBlocked(
                "document provider response could not be authenticated"
            ) from error

    def recover_document_draft_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
        binding_hash: str,
    ) -> tuple[StoredDocumentDraftResponse, bytes]:
        for value, label in (
            (firm_id, "firm_id"),
            (matter_id, "matter_id"),
            (external_request_id, "external_request_id"),
        ):
            _uuid(value, label)
        _hash(request_hash, "request_hash")
        _hash(binding_hash, "binding_hash")
        key = _response_key(
            firm_id, matter_id, external_request_id, request_hash
        )
        try:
            head = self._client.head_object(
                Bucket=self._config.bucket,
                Key=key,
            )
            metadata = head.get("Metadata") if isinstance(head, dict) else None
            if not isinstance(metadata, dict):
                raise CaseAgentDocumentExchangeBlocked(
                    "document provider response metadata is unavailable"
                )
            normalized = {
                str(name).lower(): str(value) for name, value in metadata.items()
            }
            if (
                normalized.get("lawcase-document-request-id")
                != external_request_id
                or normalized.get("lawcase-document-request-hash")
                != request_hash
                or normalized.get("lawcase-document-binding-hash")
                != binding_hash
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document provider response binding differs"
                )
            version = head.get("VersionId")
            _version(version)
            stored = StoredDocumentDraftResponse(
                object_key=key,
                request_hash=request_hash,
                binding_hash=binding_hash,
                response_sha256=str(
                    normalized.get("lawcase-document-response-sha256", "")
                ),
                response_bytes=int(
                    normalized.get("lawcase-document-response-bytes", "0")
                ),
                object_version_id=version,
            )
            body = self.read_document_draft_response(
                stored,
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=external_request_id,
            )
            return stored, body
        except CaseAgentDocumentExchangeBlocked:
            raise
        except Exception as error:
            raise CaseAgentDocumentExchangeBlocked(
                "document provider response is not recoverable"
            ) from error


class PostgresRecoverableDocumentDraftExchange:
    """0031 boundary -> one HTTPS call -> private bytes -> append-only outcome."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        transport: DocumentDraftRawTransport,
        response_store: DocumentDraftRawResponseStore,
    ) -> None:
        _worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("document draft PostgreSQL DSN is required")
        if (
            not callable(getattr(transport, "send_raw", None))
            or _MODEL.fullmatch(getattr(transport, "model_id", "")) is None
        ):
            raise ValueError("document draft raw transport is invalid")
        for name in (
            "put_document_draft_response",
            "read_document_draft_response",
            "recover_document_draft_response",
        ):
            if not callable(getattr(response_store, name, None)):
                raise ValueError("document draft private response store is incomplete")
        self._dsn = dsn
        self._worker = worker_actor
        self._transport = transport
        self._objects = response_store

    def send(
        self,
        *,
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
    ) -> ReviewableDocumentCandidate:
        self._validate_identity(prepared=prepared, binding=binding)
        try:
            exchange = self._start_exchange(prepared=prepared, binding=binding)
        except Exception as error:
            # Transport is invoked only after this method returns a committed
            # exchange row.  Any caught failure here is therefore provably
            # local even though the generic 0031 "about to submit" boundary
            # has already been recorded.
            raise DeepSeekDocumentDraftNotSubmitted(
                "document exchange did not start before provider transport"
            ) from error
        try:
            response_body = self._transport.send_raw(prepared=prepared)
        except DeepSeekDocumentDraftRejected:
            self._record_known_failure_or_unknown(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
                error_code="DOCUMENT_PROVIDER_REJECTED",
            )
            raise
        except Exception as error:
            self._record_unknown_best_effort(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
            )
            if isinstance(error, DeepSeekDocumentDraftUnknownSubmission):
                raise
            raise DeepSeekDocumentDraftUnknownSubmission(
                "DeepSeek document submission result is unknown"
            ) from error

        try:
            stored = self._objects.put_document_draft_response(
                response_body,
                firm_id=binding.firm_id,
                matter_id=binding.matter_id,
                external_request_id=exchange.external_request_id,
                request_hash=prepared.request_hash,
                binding_hash=binding.binding_hash,
            )
            _stored(
                stored,
                binding.firm_id,
                binding.matter_id,
                exchange.external_request_id,
            )
            # Parse only bytes re-read through the authenticated private
            # locator.  A transport return value alone is never evidence of a
            # durable provider result.
            response_body = self._objects.read_document_draft_response(
                stored,
                firm_id=binding.firm_id,
                matter_id=binding.matter_id,
                external_request_id=exchange.external_request_id,
            )
        except Exception as error:
            self._record_unknown_best_effort(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
            )
            raise DeepSeekDocumentDraftUnknownSubmission(
                "DeepSeek document response durability is unknown"
            ) from error

        try:
            candidate = _parse_response(
                response_body,
                expected_model=prepared.model,
                binding=binding,
            )
        except DeepSeekDocumentDraftRejected as rejected:
            try:
                self._record_outcome(
                    exchange=exchange,
                    prepared=prepared,
                    binding=binding,
                    sequence=1,
                    status="FAILED",
                    stored=stored,
                    error_code="DOCUMENT_PROVIDER_RESPONSE_INVALID",
                    recovered=False,
                )
            except Exception as error:
                self._record_unknown_best_effort(
                    exchange=exchange,
                    prepared=prepared,
                    binding=binding,
                )
                raise DeepSeekDocumentDraftUnknownSubmission(
                    "DeepSeek document invalid-response outcome is unknown"
                ) from error
            raise rejected

        try:
            self._record_outcome(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
                sequence=1,
                status="SUCCEEDED",
                stored=stored,
                error_code=None,
                recovered=False,
            )
        except Exception as error:
            # The private response remains discoverable by the deterministic
            # request identity.  Do not return a candidate until the durable
            # DB outcome is proven.
            self._record_unknown_best_effort(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
            )
            raise DeepSeekDocumentDraftUnknownSubmission(
                "DeepSeek document durable outcome is unknown"
            ) from error
        return candidate

    def recover(
        self,
        *,
        external_request_id: str,
        request_hash: str,
        binding: DynamicDocumentTaskBinding,
    ) -> RecoveredDocumentDraft:
        _uuid(external_request_id, "external_request_id")
        _hash(request_hash, "request_hash")
        binding.validate()
        if binding.firm_id != self._worker.firm_id:
            raise CaseAgentDocumentExchangeBlocked(
                "document recovery tenant binding differs"
            )
        with _transaction(
            self._dsn,
            self._worker,
            read_only=True,
        ) as connection:
            exchange_row = connection.execute(
                """
                SELECT exchange_id, external_request_id, run_id, graph_id,
                       task_id, attempt_id, firm_id, matter_id, request_hash,
                       binding_hash, source_set_hash, model_id
                FROM case_agent_document_draft_exchanges
                WHERE firm_id = %s AND external_request_id = %s
                """,
                (self._worker.firm_id, external_request_id),
            ).fetchone()
            if exchange_row is None:
                return RecoveredDocumentDraft(status="UNRESOLVED")
            _exact_exchange_binding(
                exchange_row,
                request_hash=request_hash,
                binding=binding,
                expected_model=self._transport.model_id,
            )
            outcome = connection.execute(
                """
                SELECT status, outcome_sequence, request_hash, binding_hash,
                       response_sha256, response_bytes, response_object_key,
                       response_object_version_id, error_code
                FROM case_agent_document_draft_outcomes
                WHERE exchange_id = %s AND external_request_id = %s
                  AND firm_id = %s AND matter_id = %s
                ORDER BY outcome_sequence DESC LIMIT 1
                """,
                (
                    str(exchange_row["exchange_id"]),
                    external_request_id,
                    self._worker.firm_id,
                    binding.matter_id,
                ),
            ).fetchone()

        exchange = _ExchangeRecord.from_row(exchange_row)
        if outcome is not None:
            if (
                outcome["request_hash"] != request_hash
                or outcome["binding_hash"] != binding.binding_hash
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document recovery outcome binding differs"
                )
            if outcome["status"] == "FAILED":
                return RecoveredDocumentDraft(
                    status="FAILED", error_code=str(outcome["error_code"])
                )
            if outcome["status"] == "SUCCEEDED":
                stored = _stored_from_outcome(outcome)
                response_body = self._objects.read_document_draft_response(
                    stored,
                    firm_id=binding.firm_id,
                    matter_id=binding.matter_id,
                    external_request_id=external_request_id,
                )
                candidate = _parse_response(
                    response_body,
                    expected_model=self._transport.model_id,
                    binding=binding,
                )
                return RecoveredDocumentDraft(
                    status="SUCCEEDED", candidate=candidate
                )

        if outcome is None:
            # A crash may occur after the immutable exchange row and object
            # write but before an outcome row.  First append UNKNOWN, then the
            # normal sequence-2 recovery result; never rewrite history.
            recovered_prepared = PreparedDeepSeekDocumentRequest(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model=self._transport.model_id,
                body=b"{}",
                request_hash=request_hash,
            )
            try:
                self._record_outcome(
                    exchange=exchange,
                    prepared=recovered_prepared,
                    binding=binding,
                    sequence=1,
                    status="UNKNOWN_SUBMISSION",
                    stored=None,
                    error_code="DOCUMENT_DRAFT_OUTCOME_UNKNOWN",
                    recovered=False,
                    validate_prepared_body=False,
                )
            except Exception:
                return RecoveredDocumentDraft(status="UNRESOLVED")

        # No provider lookup occurs here.  Only a response already persisted
        # under this exact deterministic request identity may resolve UNKNOWN.
        try:
            stored, response_body = (
                self._objects.recover_document_draft_response(
                    firm_id=binding.firm_id,
                    matter_id=binding.matter_id,
                    external_request_id=external_request_id,
                    request_hash=request_hash,
                    binding_hash=binding.binding_hash,
                )
            )
        except Exception:
            return RecoveredDocumentDraft(status="UNRESOLVED")
        try:
            candidate = _parse_response(
                response_body,
                expected_model=self._transport.model_id,
                binding=binding,
            )
        except DeepSeekDocumentDraftRejected:
            self._record_outcome(
                exchange=exchange,
                prepared=PreparedDeepSeekDocumentRequest(
                    endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                    model=self._transport.model_id,
                    body=b"{}",
                    request_hash=request_hash,
                ),
                binding=binding,
                sequence=2,
                status="FAILED",
                stored=stored,
                error_code="DOCUMENT_PROVIDER_RESPONSE_INVALID",
                recovered=True,
                validate_prepared_body=False,
            )
            return RecoveredDocumentDraft(
                status="FAILED",
                error_code="DOCUMENT_PROVIDER_RESPONSE_INVALID",
            )
        self._record_outcome(
            exchange=exchange,
            prepared=PreparedDeepSeekDocumentRequest(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model=self._transport.model_id,
                body=b"{}",
                request_hash=request_hash,
            ),
            binding=binding,
            sequence=2,
            status="SUCCEEDED",
            stored=stored,
            error_code=None,
            recovered=True,
            validate_prepared_body=False,
        )
        return RecoveredDocumentDraft(status="SUCCEEDED", candidate=candidate)

    def _validate_identity(
        self,
        *,
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
    ) -> None:
        binding.validate()
        _prepared(prepared, expected_model=self._transport.model_id)
        if binding.firm_id != self._worker.firm_id:
            raise CaseAgentDocumentExchangeBlocked(
                "document exchange tenant binding differs"
            )

    def _start_exchange(
        self,
        *,
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
    ) -> "_ExchangeRecord":
        expected_tool, expected_adapter = _tool_adapter(binding)
        with _transaction(
            self._dsn,
            self._worker,
            read_only=False,
        ) as connection:
            row = connection.execute(
                """
                SELECT submission.submission_id, submission.external_request_id,
                       submission.run_id, submission.task_id,
                       submission.attempt_id, submission.matter_id,
                       task.graph_id, task.input_hash, task.tool_id,
                       task.adapter_id, task.input_refs,
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
                 AND graph.firm_id = task.firm_id
                 AND graph.matter_id = task.matter_id
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
                  AND submission.matter_id = %s
                  AND submission.run_id = %s
                  AND submission.task_id = %s
                  AND submission.request_hash = %s
                  AND submission.destination = 'api.deepseek.com'
                  AND submission.submission_state = 'STARTED'
                  AND submission.recorded_by = %s
                  AND attempt.status = 'RUNNING'
                  AND task.input_hash = %s
                  AND task.tool_id = %s
                  AND task.adapter_id = %s
                FOR UPDATE OF attempt
                """,
                (
                    self._worker.actor_id,
                    self._worker.firm_id,
                    binding.matter_id,
                    binding.run_id,
                    binding.task_id,
                    prepared.request_hash,
                    self._worker.actor_id,
                    binding.task_input_hash,
                    expected_tool,
                    expected_adapter,
                ),
            ).fetchone()
            if row is None:
                raise CaseAgentDocumentExchangeBlocked(
                    "document 0031 submission boundary is absent or differs"
                )
            external_request_id = str(row["external_request_id"])
            _uuid(external_request_id, "external_request_id")
            if (
                str(row["run_id"]) != binding.run_id
                or str(row["task_id"]) != binding.task_id
                or str(row["matter_id"]) != binding.matter_id
                or str(row["graph_id"]) != binding.graph_id
                or row["input_hash"] != binding.task_input_hash
                or tuple(row["input_refs"])
                != (f"work-plan-item:{binding.work_plan_item.item_id}",)
                or str(row["current_graph_id"]) != binding.graph_id
                or row["current_graph_hash"] != row["graph_hash"]
                or int(row["snapshot_matter_version"])
                != int(row["matter_version"])
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document current task or matter binding changed"
                )
            prior = connection.execute(
                """
                SELECT 1 FROM case_agent_document_draft_exchanges
                WHERE firm_id = %s
                  AND (external_request_id = %s OR attempt_id = %s)
                """,
                (
                    self._worker.firm_id,
                    external_request_id,
                    str(row["attempt_id"]),
                ),
            ).fetchone()
            if prior is not None:
                raise CaseAgentDocumentExchangeBlocked(
                    "document request already crossed its one-call boundary"
                )
            exchange_id = str(
                uuid5(UUID(external_request_id), "document-draft-exchange-v1")
            )
            connection.execute(
                """
                INSERT INTO case_agent_document_draft_exchanges (
                    exchange_id, external_request_id, run_id, graph_id,
                    task_id, attempt_id, firm_id, matter_id,
                    submission_record_id, request_hash, binding_hash,
                    source_set_hash, endpoint_host, provider_id, model_id,
                    service_id, started_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'api.deepseek.com','deepseek',%s,
                    'deepseek-document-drafting',%s
                )
                """,
                (
                    exchange_id,
                    external_request_id,
                    binding.run_id,
                    binding.graph_id,
                    binding.task_id,
                    str(row["attempt_id"]),
                    self._worker.firm_id,
                    binding.matter_id,
                    str(row["submission_id"]),
                    prepared.request_hash,
                    binding.binding_hash,
                    binding.source_set_hash,
                    prepared.model,
                    self._worker.actor_id,
                ),
            )
        return _ExchangeRecord(
            exchange_id=exchange_id,
            external_request_id=external_request_id,
            run_id=binding.run_id,
            graph_id=binding.graph_id,
            task_id=binding.task_id,
            attempt_id=str(row["attempt_id"]),
            matter_id=binding.matter_id,
            request_hash=prepared.request_hash,
            binding_hash=binding.binding_hash,
            source_set_hash=binding.source_set_hash,
            model_id=prepared.model,
        )

    def _record_known_failure_or_unknown(
        self,
        *,
        exchange: "_ExchangeRecord",
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
        error_code: str,
    ) -> None:
        try:
            self._record_outcome(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
                sequence=1,
                status="FAILED",
                stored=None,
                error_code=error_code,
                recovered=False,
            )
        except Exception as error:
            self._record_unknown_best_effort(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
            )
            raise DeepSeekDocumentDraftUnknownSubmission(
                "DeepSeek document failure durability is unknown"
            ) from error

    def _record_unknown_best_effort(
        self,
        *,
        exchange: "_ExchangeRecord",
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
    ) -> None:
        try:
            self._record_outcome(
                exchange=exchange,
                prepared=prepared,
                binding=binding,
                sequence=1,
                status="UNKNOWN_SUBMISSION",
                stored=None,
                error_code="DOCUMENT_DRAFT_OUTCOME_UNKNOWN",
                recovered=False,
            )
        except Exception:
            pass

    def _record_outcome(
        self,
        *,
        exchange: "_ExchangeRecord",
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
        sequence: int,
        status: str,
        stored: StoredDocumentDraftResponse | None,
        error_code: str | None,
        recovered: bool,
        validate_prepared_body: bool = True,
    ) -> None:
        if validate_prepared_body:
            _prepared(prepared, expected_model=exchange.model_id)
        elif prepared.request_hash != exchange.request_hash:
            raise CaseAgentDocumentExchangeBlocked(
                "document recovered request hash differs"
            )
        _exact_exchange_record(exchange, binding=binding)
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION"}:
            raise CaseAgentDocumentExchangeBlocked("document outcome status is invalid")
        if error_code is not None and _ERROR.fullmatch(error_code) is None:
            raise CaseAgentDocumentExchangeBlocked("document error code is invalid")
        if stored is not None:
            _stored(
                stored,
                binding.firm_id,
                binding.matter_id,
                exchange.external_request_id,
            )
        outcome_id = str(
            uuid5(UUID(exchange.exchange_id), f"document-draft-outcome:{sequence}")
        )
        with _transaction(
            self._dsn,
            self._worker,
            read_only=False,
        ) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_document_draft_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, outcome_sequence, status, request_hash,
                    binding_hash, response_sha256, response_bytes,
                    response_object_key, response_object_version_id,
                    error_code, recovered_from_unknown, recorded_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                ) ON CONFLICT (exchange_id, outcome_sequence) DO NOTHING
                """,
                (
                    outcome_id,
                    exchange.exchange_id,
                    exchange.external_request_id,
                    self._worker.firm_id,
                    binding.matter_id,
                    sequence,
                    status,
                    prepared.request_hash,
                    binding.binding_hash,
                    stored.response_sha256 if stored is not None else None,
                    stored.response_bytes if stored is not None else None,
                    stored.object_key if stored is not None else None,
                    stored.object_version_id if stored is not None else None,
                    error_code,
                    recovered,
                    self._worker.actor_id,
                ),
            )
            row = connection.execute(
                """
                SELECT status, request_hash, binding_hash, response_sha256,
                       error_code, recovered_from_unknown
                FROM case_agent_document_draft_outcomes
                WHERE exchange_id = %s AND outcome_sequence = %s
                  AND firm_id = %s AND matter_id = %s
                """,
                (
                    exchange.exchange_id,
                    sequence,
                    self._worker.firm_id,
                    binding.matter_id,
                ),
            ).fetchone()
            if (
                row is None
                or row["status"] != status
                or row["request_hash"] != prepared.request_hash
                or row["binding_hash"] != binding.binding_hash
                or row["error_code"] != error_code
                or bool(row["recovered_from_unknown"]) != recovered
                or (
                    stored is not None
                    and row["response_sha256"] != stored.response_sha256
                )
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document draft outcome could not be proven"
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
    request_hash: str
    binding_hash: str
    source_set_hash: str
    model_id: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "_ExchangeRecord":
        return cls(
            exchange_id=str(row["exchange_id"]),
            external_request_id=str(row["external_request_id"]),
            run_id=str(row["run_id"]),
            graph_id=str(row["graph_id"]),
            task_id=str(row["task_id"]),
            attempt_id=str(row["attempt_id"]),
            matter_id=str(row["matter_id"]),
            request_hash=str(row["request_hash"]),
            binding_hash=str(row["binding_hash"]),
            source_set_hash=str(row["source_set_hash"]),
            model_id=str(row["model_id"]),
        )


def preflight_case_agent_document_exchange_runtime_contract(
    *,
    dsn: str,
    worker_actor: Actor,
    transport: DocumentDraftRawTransport,
    response_store: DocumentDraftRawResponseStore,
) -> None:
    """Fail startup when an enabled document exchange is not production-ready.

    Production composition must provide one dedicated ``SYSTEM_WORKER``, the
    0040 migration with all guards and FORCE RLS, an official DeepSeek raw
    transport, and a private response store with put/read/recover operations.
    This preflight performs no model call and does not expose credentials.
    """

    _worker(worker_actor)
    if not isinstance(dsn, str) or not dsn.strip():
        raise CaseAgentDocumentExchangeBlocked(
            "document exchange preflight PostgreSQL DSN is invalid"
        )
    if (
        not callable(getattr(transport, "send_raw", None))
        or _MODEL.fullmatch(getattr(transport, "model_id", "")) is None
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document exchange preflight transport is incomplete"
        )
    for name in (
        "put_document_draft_response",
        "read_document_draft_response",
        "recover_document_draft_response",
    ):
        if not callable(getattr(response_store, name, None)):
            raise CaseAgentDocumentExchangeBlocked(
                "document exchange preflight private store is incomplete"
            )
    required = {
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
    triggers = {
        "case_agent_document_draft_exchange_guard",
        "case_agent_document_draft_outcome_guard",
        "case_agent_document_draft_exchanges_append_only",
        "case_agent_document_draft_outcomes_append_only",
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
                raise CaseAgentDocumentExchangeBlocked(
                    "case-Agent document exchange migration 0040 is incomplete"
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
                raise CaseAgentDocumentExchangeBlocked(
                    "case-Agent document exchange guards are incomplete"
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
                raise CaseAgentDocumentExchangeBlocked(
                    "case-Agent document exchange requires FORCE RLS"
                )
            policy_row = connection.execute(
                """
                SELECT pg_get_functiondef(proc.oid) AS definition
                FROM pg_catalog.pg_proc proc
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid = proc.pronamespace
                WHERE namespace.nspname = 'public'
                  AND proc.proname = 'validate_case_agent_document_draft_exchange'
                """
            ).fetchone()
            policy_definition = (
                None if policy_row is None else policy_row["definition"]
            )
            expected_policy_hashes = {
                DOCX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash,
                XLSX_DOCUMENT_DELIVERY_MANIFEST.sandbox_policy_hash,
            }
            if (
                not isinstance(policy_definition, str)
                or any(
                    policy_hash not in policy_definition
                    for policy_hash in expected_policy_hashes
                )
            ):
                raise CaseAgentDocumentExchangeBlocked(
                    "document exchange database policy differs from runtime manifests"
                )
    except CaseAgentDocumentExchangeBlocked:
        raise
    except Exception as error:
        raise CaseAgentDocumentExchangeBlocked(
            "case-Agent document exchange preflight failed"
        ) from error


def _parse_response(
    body: bytes,
    *,
    expected_model: str,
    binding: DynamicDocumentTaskBinding,
) -> ReviewableDocumentCandidate:
    content = parse_deepseek_document_response(body, expected_model=expected_model)
    try:
        return parse_reviewable_document_candidate(content, binding=binding)
    except Exception as error:
        raise DeepSeekDocumentDraftRejected(
            "DeepSeek document candidate is outside the authorized schema"
        ) from error


def _tool_adapter(binding: DynamicDocumentTaskBinding) -> tuple[str, str]:
    if binding.template.output_format is ReviewableDocumentFormat.DOCX:
        return (
            "draft_reviewable_docx_package",
            "dynamic-reviewable-docx-delivery",
        )
    if binding.template.output_format is ReviewableDocumentFormat.XLSX:
        return (
            "draft_reviewable_xlsx_package",
            "dynamic-reviewable-xlsx-delivery",
        )
    raise CaseAgentDocumentExchangeBlocked("document output format is invalid")


def _exact_exchange_binding(
    row: Mapping[str, Any],
    *,
    request_hash: str,
    binding: DynamicDocumentTaskBinding,
    expected_model: str,
) -> None:
    if (
        str(row["run_id"]) != binding.run_id
        or str(row["graph_id"]) != binding.graph_id
        or str(row["task_id"]) != binding.task_id
        or str(row["firm_id"]) != binding.firm_id
        or str(row["matter_id"]) != binding.matter_id
        or row["request_hash"] != request_hash
        or row["binding_hash"] != binding.binding_hash
        or row["source_set_hash"] != binding.source_set_hash
        or row["model_id"] != expected_model
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document recovery run, task, attempt, tenant or hash differs"
        )


def _exact_exchange_record(
    exchange: _ExchangeRecord, *, binding: DynamicDocumentTaskBinding
) -> None:
    if (
        exchange.run_id != binding.run_id
        or exchange.graph_id != binding.graph_id
        or exchange.task_id != binding.task_id
        or exchange.matter_id != binding.matter_id
        or exchange.binding_hash != binding.binding_hash
        or exchange.source_set_hash != binding.source_set_hash
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document outcome exchange binding differs"
        )


def _stored_from_outcome(row: Mapping[str, Any]) -> StoredDocumentDraftResponse:
    return StoredDocumentDraftResponse(
        object_key=str(row["response_object_key"]),
        request_hash=str(row["request_hash"]),
        binding_hash=str(row["binding_hash"]),
        response_sha256=str(row["response_sha256"]),
        response_bytes=int(row["response_bytes"]),
        object_version_id=(
            str(row["response_object_version_id"])
            if row["response_object_version_id"] is not None
            else None
        ),
    )


def _prepared(
    value: PreparedDeepSeekDocumentRequest, *, expected_model: str
) -> None:
    if (
        not isinstance(value, PreparedDeepSeekDocumentRequest)
        or value.endpoint != DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT
        or value.model != expected_model
        or sha256(value.body).hexdigest() != value.request_hash
        or _SHA256.fullmatch(value.request_hash) is None
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "prepared DeepSeek document request identity differs"
        )


def _store_identity(
    *,
    response_body: bytes,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
    request_hash: str,
    binding_hash: str,
) -> None:
    for value, label in (
        (firm_id, "firm_id"),
        (matter_id, "matter_id"),
        (external_request_id, "external_request_id"),
    ):
        _uuid(value, label)
    _hash(request_hash, "request_hash")
    _hash(binding_hash, "binding_hash")
    if not isinstance(response_body, bytes) or not 2 <= len(response_body) <= _MAX_RESPONSE_BYTES:
        raise CaseAgentDocumentExchangeBlocked(
            "document provider response bytes are invalid"
        )


def _stored(
    value: StoredDocumentDraftResponse,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
) -> None:
    expected = _response_key(
        firm_id, matter_id, external_request_id, value.request_hash
    )
    if (
        not isinstance(value, StoredDocumentDraftResponse)
        or value.object_key != expected
        or _SHA256.fullmatch(value.request_hash) is None
        or _SHA256.fullmatch(value.binding_hash) is None
        or _SHA256.fullmatch(value.response_sha256) is None
        or not 2 <= value.response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document provider response locator is invalid"
        )
    _version(value.object_version_id)


def _response_key(
    firm_id: str,
    matter_id: str,
    external_request_id: str,
    request_hash: str,
) -> str:
    return (
        f"case-agent-document-drafts/v1/{firm_id}/{matter_id}/"
        f"{external_request_id}/{request_hash}.json"
    )


def _response_metadata(
    *,
    external_request_id: str,
    request_hash: str,
    binding_hash: str,
    response_sha256: str,
    response_bytes: int,
) -> dict[str, str]:
    return {
        "lawcase-document-request-id": external_request_id,
        "lawcase-document-request-hash": request_hash,
        "lawcase-document-binding-hash": binding_hash,
        "lawcase-document-response-sha256": response_sha256,
        "lawcase-document-response-bytes": str(response_bytes),
    }


def _validate_head(
    value: Any,
    *,
    byte_size: int,
    checksum: str,
    metadata: dict[str, str],
    encryption: str,
) -> None:
    if (
        not isinstance(value, dict)
        or value.get("ContentLength") != byte_size
        or value.get("ChecksumSHA256") != checksum
        or value.get("ContentType") != "application/json"
        or value.get("ServerSideEncryption") != encryption
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document provider response remote receipt differs"
        )
    actual = value.get("Metadata")
    if not isinstance(actual, dict) or {
        str(name).lower(): str(item) for name, item in actual.items()
    } != metadata:
        raise CaseAgentDocumentExchangeBlocked(
            "document provider response metadata differs"
        )


def _urlopen_raw_transport(
    endpoint: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise DeepSeekDocumentDraftRejected(
            f"DeepSeek document request was rejected with HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise DeepSeekDocumentDraftUnknownSubmission(
            "DeepSeek document submission result is unknown"
        ) from error
    if not 200 <= status < 300:
        raise DeepSeekDocumentDraftRejected(
            f"DeepSeek document request was rejected with HTTP {status}"
        )
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise DeepSeekDocumentDraftRejected(
            "DeepSeek document response exceeded the byte limit"
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
        raise CaseAgentDocumentExchangeBlocked(
            "document response S3 client is not installed"
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
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    except Exception as error:  # pragma: no cover - environment specific.
        raise CaseAgentDocumentExchangeBlocked(
            "document response S3 client could not be configured"
        ) from error


@contextmanager
def _transaction(
    dsn: str,
    worker: Actor,
    *,
    read_only: bool,
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
            "document exchange requires one dedicated SYSTEM_WORKER"
        )
    _uuid(value.actor_id, "worker actor_id")
    _uuid(value.firm_id, "worker firm_id")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CaseAgentDocumentExchangeBlocked(f"{label} is invalid") from error


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CaseAgentDocumentExchangeBlocked(f"{label} is invalid")


def _version(value: object) -> None:
    if value is not None and (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 512
        or any(ord(character) < 32 for character in value)
    ):
        raise CaseAgentDocumentExchangeBlocked(
            "document provider response object version is invalid"
        )


__all__ = (
    "CaseAgentDocumentExchangeBlocked",
    "DeepSeekDocumentRawHttpsTransport",
    "DocumentDraftRawResponseStore",
    "DocumentDraftRawTransport",
    "PostgresRecoverableDocumentDraftExchange",
    "S3DocumentDraftRawResponseStore",
    "StoredDocumentDraftResponse",
    "preflight_case_agent_document_exchange_runtime_contract",
)
