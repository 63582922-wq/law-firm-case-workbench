"""Firm-scoped current-task source binding for production visual OCR.

This module is the only bridge from opaque ``evidence-page:<uuid>`` task refs
to private image bytes.  It rechecks the current graph, attempt, task input,
matter version, active SYSTEM_WORKER role, immutable page/original row and
private object binding in one repeatable-read RLS transaction.  Storage keys
never enter the returned binding or model payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import mkdtemp
from typing import Any, Iterator, Protocol
from contextlib import contextmanager
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from pypdf import PdfReader

from .case_agent_external_failure import CaseAgentKnownExternalFailure
from .models import Actor, Role
from .qwen_visual_ocr_adapter import (
    AuthorizedVisualOcrBinding,
    QWEN_VISUAL_OCR_POLICY_HASH,
    QwenVisualOcrBlocked,
    VisualOcrBindingPort,
)
from .qwen_visual_ocr_transport import (
    QWEN_VISUAL_OCR_UNKNOWN_CODES,
    QwenVisualOcrEgressBroker,
    QwenVisualOcrNetworkBlocked,
    QwenVisualOcrRecoveryBroker,
    QwenVisualOcrTransportFailure,
    QwenVisualOcrTransportRequest,
    QwenVisualOcrTransportResult,
)
from .visual_page_understanding import (
    VisualSourceKind,
    build_visual_page_projection,
)
from .web_object_store import StoredWebEvidenceOriginal


_PAGE_REF = re.compile(
    r"^evidence-page:(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_IMAGE_OBJECT = re.compile(
    r"^original-images/v1/(?P<firm>[0-9a-f-]{36})/(?P<matter>[0-9a-f-]{36})/"
    r"(?P<prefix>[0-9a-f]{2})/(?P<digest>[0-9a-f]{64})/"
    r"[0-9a-f-]{36}\.(?P<suffix>jpg|png)$"
)


class CaseAgentVisualOcrPersistenceBlocked(RuntimeError):
    """The enabled visual-OCR database contract is absent or untrustworthy."""


@dataclass(frozen=True)
class PrivateNativeImageLocator:
    firm_id: str
    matter_id: str
    evidence_file_id: str
    content_sha256: str
    byte_size: int
    media_type: str
    source_reference_hash: str
    object_key: str = field(repr=False)
    object_version_id: str | None = field(default=None, repr=False)


class VisualOcrPrivateObjectStore(Protocol):
    def materialize_verified_pdf(
        self, stored: StoredWebEvidenceOriginal, *, destination: str | Path
    ) -> Path: ...

    def read_verified_native_image(
        self, locator: PrivateNativeImageLocator
    ) -> bytes: ...


@dataclass(frozen=True)
class VisualOcrProjectionPolicy:
    worker_root: Path = field(repr=False)
    pdftoppm_executable: Path = field(repr=False)
    rendered_dpi: int = 144
    render_timeout_seconds: int = 30
    max_pdf_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        root = _private_directory(self.worker_root)
        executable = _executable(self.pdftoppm_executable)
        if type(self.rendered_dpi) is not int or not 72 <= self.rendered_dpi <= 200:
            raise ValueError("visual OCR PDF DPI is invalid")
        if (
            type(self.render_timeout_seconds) is not int
            or not 1 <= self.render_timeout_seconds <= 60
        ):
            raise ValueError("visual OCR render timeout is invalid")
        if (
            type(self.max_pdf_bytes) is not int
            or not 1 <= self.max_pdf_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("visual OCR PDF byte limit is invalid")
        object.__setattr__(self, "worker_root", root)
        object.__setattr__(self, "pdftoppm_executable", executable)


class PostgresVisualOcrBindingPort(VisualOcrBindingPort):
    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: VisualOcrPrivateObjectStore,
        policy: VisualOcrProjectionPolicy,
        workspace_id: str,
        runner=None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("visual OCR PostgreSQL DSN is required")
        if (
            not isinstance(worker_actor, Actor)
            or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
        ):
            raise PermissionError("visual OCR requires one SYSTEM_WORKER")
        for method in ("materialize_verified_pdf", "read_verified_native_image"):
            if not callable(getattr(object_store, method, None)):
                raise ValueError("visual OCR private object store is incomplete")
        if not isinstance(policy, VisualOcrProjectionPolicy):
            raise ValueError("visual OCR projection policy is required")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", workspace_id or "") is None:
            raise ValueError("Qwen workspace id is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        self._objects = object_store
        self._policy = policy
        self._workspace_id = workspace_id
        self._runner = runner or subprocess.run

    def resolve_visual_ocr(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> AuthorizedVisualOcrBinding:
        page_ids = _page_ids(input_refs)
        if len(page_ids) != 1:
            raise QwenVisualOcrBlocked("visual OCR v1 requires exactly one page")
        record = self._read_current_record(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            evidence_page_id=page_ids[0],
        )
        projection = self._projection(record)
        return AuthorizedVisualOcrBinding.build(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            firm_id=self._worker.firm_id,
            matter_id=record["matter_id"],
            matter_version=record["matter_version"],
            input_refs=input_refs,
            external_request_id=record["external_request_id"],
            processor_region="cn-beijing",
            workspace_id=self._workspace_id,
            projections=(projection,),
        )

    def _read_current_record(self, **values: object) -> dict[str, Any]:
        for key in ("run_id", "task_id", "attempt_id"):
            _uuid(values[key], key)
        _sha(values["task_input_hash"], "task_input_hash")
        with _transaction(
            self._dsn,
            self._worker.firm_id,
            read_only=True,
        ) as connection:
            row = connection.execute(
                """
                SELECT task.matter_id, matter.version AS matter_version,
                       graph.snapshot_matter_version,
                       task.input_refs, task.input_hash, attempt.external_request_id,
                       page.evidence_page_id, page.evidence_file_id, page.page_number,
                       page.rendered_page_sha256 AS registered_render_hash,
                       source.original_file_sha256, source.byte_size,
                       source.media_type, source.page_count,
                       pdf.source_object_key AS pdf_object_key,
                       pdf.source_object_version_id AS pdf_version,
                       pdf.source_reference_hash AS pdf_reference_hash,
                       image.source_object_key AS image_object_key,
                       image.source_object_version_id AS image_version,
                       image.source_reference_hash AS image_reference_hash,
                       image.source_media_type AS image_media_type
                FROM case_agent_task_attempts attempt
                JOIN case_agent_tasks task
                  ON task.graph_id = attempt.graph_id
                 AND task.task_id = attempt.task_id
                 AND task.run_id = attempt.run_id
                 AND task.firm_id = attempt.firm_id
                 AND task.matter_id = attempt.matter_id
                JOIN case_agent_runs run
                  ON run.run_id = task.run_id AND run.firm_id = task.firm_id
                 AND run.matter_id = task.matter_id
                 AND run.current_graph_id = task.graph_id
                JOIN case_agent_task_graphs graph
                  ON graph.graph_id = task.graph_id
                 AND graph.run_id = task.run_id
                 AND graph.firm_id = task.firm_id
                 AND graph.matter_id = task.matter_id
                JOIN matters matter
                  ON matter.matter_id = task.matter_id
                 AND matter.firm_id = task.firm_id
                JOIN matter_actor_roles worker_role
                  ON worker_role.matter_id = task.matter_id
                 AND worker_role.firm_id = task.firm_id
                 AND worker_role.user_id = %s
                 AND worker_role.role = 'SYSTEM_WORKER'
                 AND worker_role.revoked_at IS NULL
                JOIN users worker
                  ON worker.user_id = worker_role.user_id
                 AND worker.firm_id = worker_role.firm_id
                 AND worker.status = 'ACTIVE'
                JOIN evidence_pages page
                  ON page.evidence_page_id = %s
                 AND page.firm_id = task.firm_id
                 AND page.matter_id = task.matter_id
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id
                 AND source.matter_id = page.matter_id
                LEFT JOIN web_evidence_original_source_objects pdf
                  ON pdf.evidence_file_id = source.evidence_file_id
                 AND pdf.firm_id = source.firm_id AND pdf.matter_id = source.matter_id
                LEFT JOIN web_evidence_native_image_source_objects image
                  ON image.evidence_file_id = source.evidence_file_id
                 AND image.firm_id = source.firm_id AND image.matter_id = source.matter_id
                WHERE attempt.attempt_id = %s AND attempt.run_id = %s
                  AND attempt.task_id = %s AND attempt.firm_id = %s
                  AND attempt.input_hash = %s AND task.input_hash = %s
                  AND task.tool_id = 'understand_visual_page'
                  AND task.input_refs = %s::jsonb
                  AND attempt.status IN ('RUNNING', 'RECONCILING')
                """,
                (
                    self._worker.actor_id,
                    values["evidence_page_id"],
                    values["attempt_id"],
                    values["run_id"],
                    values["task_id"],
                    self._worker.firm_id,
                    values["task_input_hash"],
                    values["task_input_hash"],
                    psycopg.types.json.Jsonb(list(values["input_refs"])),
                ),
            ).fetchone()
        if row is None:
            raise QwenVisualOcrBlocked(
                "visual OCR task, tenant, case, page or input binding changed"
            )
        pdf = row["pdf_object_key"] is not None
        image = row["image_object_key"] is not None
        if (
            int(row["matter_version"]) != int(row["snapshot_matter_version"])
            or pdf == image
            or (pdf and row["media_type"] != "application/pdf")
        ):
            raise QwenVisualOcrBlocked("visual OCR private source binding is ambiguous")
        expected_external_request_id = str(
            uuid5(UUID(str(values["attempt_id"])), "qwen-visual-ocr-request-v1")
        )
        durable_external_request_id = row["external_request_id"]
        if (
            durable_external_request_id is not None
            and str(durable_external_request_id) != expected_external_request_id
        ):
            raise QwenVisualOcrBlocked(
                "visual OCR durable external request binding changed"
            )
        return {
            **dict(row),
            "matter_id": str(row["matter_id"]),
            "matter_version": int(row["matter_version"]),
            "external_request_id": expected_external_request_id,
            "evidence_page_id": str(row["evidence_page_id"]),
            "evidence_file_id": str(row["evidence_file_id"]),
        }

    def _projection(self, row: dict[str, Any]):
        workspace = Path(mkdtemp(prefix="visual-ocr-", dir=self._policy.worker_root))
        workspace.chmod(0o700)
        try:
            if row["pdf_object_key"] is not None:
                content = self._render_pdf(workspace, row)
                source_kind = VisualSourceKind.RENDERED_PDF_PAGE
                media_type = "image/png"
                source_page_hash = sha256(content).hexdigest()
            else:
                locator = _native_locator(row, firm_id=self._worker.firm_id)
                content = self._objects.read_verified_native_image(locator)
                if (
                    not isinstance(content, bytes)
                    or len(content) != locator.byte_size
                    or sha256(content).hexdigest() != locator.content_sha256
                ):
                    raise QwenVisualOcrBlocked("native image bytes differ")
                source_kind = VisualSourceKind.NATIVE_IMAGE
                media_type = locator.media_type
                source_page_hash = locator.content_sha256
            projection = build_visual_page_projection(
                matter_id=row["matter_id"],
                evidence_page_id=row["evidence_page_id"],
                page_number=int(row["page_number"]),
                source_kind=source_kind,
                source_file_sha256=row["original_file_sha256"],
                source_page_sha256=source_page_hash,
                source_media_type=media_type,
                source_bytes=content,
            )
            registered = row["registered_render_hash"]
            if registered is not None and registered != projection.rendered_page_sha256:
                raise QwenVisualOcrBlocked("visual render differs from registered page")
            return projection
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _render_pdf(self, workspace: Path, row: dict[str, Any]) -> bytes:
        stored = StoredWebEvidenceOriginal(
            object_key=row["pdf_object_key"],
            content_sha256=row["original_file_sha256"],
            byte_size=int(row["byte_size"]),
            object_version_id=row["pdf_version"],
        )
        source = workspace / "source.pdf"
        materialized = self._objects.materialize_verified_pdf(stored, destination=source)
        if materialized != source or int(row["byte_size"]) > self._policy.max_pdf_bytes:
            raise QwenVisualOcrBlocked("private PDF materialization is invalid")
        if sha256(source.read_bytes()).hexdigest() != row["original_file_sha256"]:
            raise QwenVisualOcrBlocked("private PDF bytes differ")
        with source.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted or len(reader.pages) != int(row["page_count"]):
                raise QwenVisualOcrBlocked("private PDF structure differs")
        output = workspace / "page"
        completed = self._runner(
            [
                str(self._policy.pdftoppm_executable),
                "-f", str(row["page_number"]),
                "-l", str(row["page_number"]),
                "-singlefile", "-r", str(self._policy.rendered_dpi),
                "-png", str(source), str(output),
            ],
            check=False, capture_output=True,
            timeout=self._policy.render_timeout_seconds,
            cwd=str(workspace), stdin=subprocess.DEVNULL, shell=False,
            close_fds=True, start_new_session=True,
        )
        page = output.with_suffix(".png")
        if getattr(completed, "returncode", None) != 0 or not page.is_file():
            raise QwenVisualOcrBlocked("private PDF page render failed")
        content = page.read_bytes()
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise QwenVisualOcrBlocked("private PDF renderer returned non-PNG")
        if sha256(source.read_bytes()).hexdigest() != row["original_file_sha256"]:
            raise QwenVisualOcrBlocked("private PDF changed during rendering")
        return content


class PostgresDurableQwenVisualOcrBroker(
    QwenVisualOcrEgressBroker, QwenVisualOcrRecoveryBroker
):
    """Persist STARTED -> one HTTPS call -> exact private response or UNKNOWN."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        transport: QwenVisualOcrEgressBroker,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("visual OCR PostgreSQL DSN is required")
        if (
            not isinstance(worker_actor, Actor)
            or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
        ):
            raise PermissionError("visual OCR requires one SYSTEM_WORKER")
        if not callable(getattr(transport, "send", None)):
            raise ValueError("visual OCR HTTPS transport is required")
        self._dsn = dsn
        self._worker = worker_actor
        self._transport = transport

    def send(
        self, *, request: QwenVisualOcrTransportRequest
    ) -> QwenVisualOcrTransportResult:
        _validate_transport_identity(request)
        with _transaction(
            self._dsn,
            self._worker.firm_id,
            actor_id=self._worker.actor_id,
            read_only=False,
        ) as connection:
            row = connection.execute(
                """
                SELECT submission.submission_id, submission.run_id,
                       submission.attempt_id, submission.task_id,
                       submission.matter_id, page.evidence_page_id
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
                JOIN evidence_pages page
                  ON task.input_refs->>0 =
                     'evidence-page:' || page.evidence_page_id::text
                 AND page.firm_id = task.firm_id
                 AND page.matter_id = task.matter_id
                WHERE submission.firm_id = %s
                  AND submission.external_request_id = %s
                  AND submission.request_hash = %s
                  AND submission.destination = %s
                  AND submission.submission_state = 'STARTED'
                  AND attempt.status = 'RUNNING'
                  AND task.tool_id = 'understand_visual_page'
                  AND task.adapter_id = 'qwen-visual-ocr-review'
                  AND jsonb_array_length(task.input_refs) = 1
                  AND task.input_refs->>0 ~ '^evidence-page:[0-9a-f-]{36}$'
                FOR UPDATE OF attempt
                """,
                (
                    self._worker.actor_id,
                    self._worker.firm_id,
                    request.external_request_id,
                    request.request_hash,
                    request.endpoint_host,
                ),
            ).fetchone()
            if row is None:
                raise QwenVisualOcrBlocked(
                    "visual OCR 0031 submission boundary is absent or differs"
                )
            prior = connection.execute(
                """
                SELECT 1 FROM case_agent_visual_ocr_exchanges
                WHERE firm_id = %s AND external_request_id = %s
                """,
                (self._worker.firm_id, request.external_request_id),
            ).fetchone()
            if prior is not None:
                raise QwenVisualOcrBlocked(
                    "visual OCR request already crossed its one-call boundary"
                )
            exchange_id = str(
                uuid5(UUID(request.external_request_id), "visual-ocr-exchange-v1")
            )
            connection.execute(
                """
                INSERT INTO case_agent_visual_ocr_exchanges (
                    exchange_id, external_request_id, run_id, task_id,
                    attempt_id, firm_id, matter_id, evidence_page_id,
                    submission_record_id, request_hash, projection_hash,
                    rendered_page_sha256, endpoint_host, provider_id,
                    model_id, service_id, started_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'qwen','qwen3.5-ocr','qwen-visual-ocr',%s
                )
                """,
                (
                    exchange_id,
                    request.external_request_id,
                    str(row["run_id"]),
                    str(row["task_id"]),
                    str(row["attempt_id"]),
                    self._worker.firm_id,
                    str(row["matter_id"]),
                    str(row["evidence_page_id"]),
                    str(row["submission_id"]),
                    request.request_hash,
                    request.projection_hash,
                    request.rendered_page_sha256,
                    request.endpoint_host,
                    self._worker.actor_id,
                ),
            )
        try:
            result = self._transport.send(request=request)
            _validate_transport_result(request, result)
            self._record_success(
                exchange_id=exchange_id,
                matter_id=str(row["matter_id"]),
                request=request,
                result=result,
            )
            return result
        except CaseAgentKnownExternalFailure as error:
            if error.external_request_id != request.external_request_id:
                raise QwenVisualOcrBlocked(
                    "visual OCR known failure differs from its request"
                ) from error
            self._record_failed(
                exchange_id=exchange_id,
                matter_id=str(row["matter_id"]),
                request=request,
                error_code=error.error_code,
            )
            raise
        except QwenVisualOcrNetworkBlocked as error:
            if error.external_request_id != request.external_request_id:
                raise QwenVisualOcrBlocked(
                    "visual OCR unknown outcome differs from its request"
                ) from error
            try:
                self._record_unknown(
                    exchange_id=exchange_id,
                    matter_id=str(row["matter_id"]),
                    request=request,
                    error_code=error.error_code,
                )
            except Exception:
                pass
            raise
        except Exception:
            try:
                self._record_unknown(
                    exchange_id=exchange_id,
                    matter_id=str(row["matter_id"]),
                    request=request,
                    error_code="QWEN_VISUAL_OCR_OUTCOME_UNKNOWN",
                )
            except Exception:
                pass
            raise

    def lookup(
        self, *, external_request_id: str, request_hash: str
    ) -> QwenVisualOcrTransportResult | QwenVisualOcrTransportFailure | None:
        _uuid(external_request_id, "visual external_request_id")
        _sha(request_hash, "visual request_hash")
        with _transaction(
            self._dsn, self._worker.firm_id, read_only=True
        ) as connection:
            row = connection.execute(
                """
                SELECT exchange.request_hash, outcome.status,
                       outcome.provider_request_id, outcome.response_sha256,
                       outcome.response_bytes, outcome.response_body,
                       outcome.error_code,
                       resolution.error_code AS resolution_error_code,
                       resolution.evidence_kind AS resolution_evidence_kind,
                       resolution.evidence_sha256 AS resolution_evidence_sha256
                FROM case_agent_visual_ocr_exchanges exchange
                LEFT JOIN case_agent_visual_ocr_outcomes outcome
                  ON outcome.exchange_id = exchange.exchange_id
                 AND outcome.external_request_id = exchange.external_request_id
                 AND outcome.firm_id = exchange.firm_id
                 AND outcome.matter_id = exchange.matter_id
                LEFT JOIN case_agent_visual_ocr_local_failure_resolutions resolution
                  ON resolution.exchange_id = exchange.exchange_id
                 AND resolution.external_request_id = exchange.external_request_id
                 AND resolution.firm_id = exchange.firm_id
                 AND resolution.matter_id = exchange.matter_id
                WHERE exchange.firm_id = %s
                  AND exchange.external_request_id = %s
                """,
                (self._worker.firm_id, external_request_id),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise QwenVisualOcrBlocked(
                "visual OCR stored request differs from its lookup"
            )
        if row["status"] is None:
            return None
        if row["status"] == "UNKNOWN_SUBMISSION":
            resolution_error = row.get("resolution_error_code")
            if resolution_error is None:
                return None
            if (
                resolution_error
                != "QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED"
                or row.get("resolution_evidence_kind")
                != "POSTGRES_ERROR_LOG"
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(row.get("resolution_evidence_sha256") or ""),
                )
                is None
            ):
                raise QwenVisualOcrBlocked(
                    "visual OCR local failure resolution is invalid"
                )
            return QwenVisualOcrTransportFailure(
                external_request_id=external_request_id,
                request_hash=request_hash,
                error_code=resolution_error,
            )
        if row["status"] == "FAILED":
            error_code = row["error_code"]
            if (
                not isinstance(error_code, str)
                or re.fullmatch(r"QWEN_VISUAL_OCR_[A-Z0-9_]{3,57}", error_code)
                is None
            ):
                raise QwenVisualOcrBlocked(
                    "visual OCR stored failure is invalid"
                )
            return QwenVisualOcrTransportFailure(
                external_request_id=external_request_id,
                request_hash=request_hash,
                error_code=error_code,
            )
        if row["status"] != "SUCCEEDED":
            raise QwenVisualOcrBlocked("visual OCR stored outcome is invalid")
        body = bytes(row["response_body"])
        if (
            len(body) != int(row["response_bytes"])
            or sha256(body).hexdigest() != row["response_sha256"]
        ):
            raise QwenVisualOcrBlocked(
                "visual OCR stored response differs from its receipt"
            )
        result = QwenVisualOcrTransportResult(
            external_request_id=external_request_id,
            request_hash=request_hash,
            provider_request_id=str(row["provider_request_id"]),
            response_body=body,
        )
        return result

    def _record_success(
        self,
        *,
        exchange_id: str,
        matter_id: str,
        request: QwenVisualOcrTransportRequest,
        result: QwenVisualOcrTransportResult,
    ) -> None:
        body_hash = sha256(result.response_body).hexdigest()
        with _transaction(
            self._dsn,
            self._worker.firm_id,
            actor_id=self._worker.actor_id,
            read_only=False,
        ) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_visual_ocr_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, status, request_hash, provider_request_id,
                    response_sha256, response_bytes, response_body,
                    error_code, recorded_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,'SUCCEEDED',%s,%s,%s,%s,%s,NULL,%s
                ) ON CONFLICT (exchange_id) DO NOTHING
                """,
                (
                    str(uuid5(UUID(exchange_id), "visual-ocr-outcome-v1")),
                    exchange_id,
                    request.external_request_id,
                    self._worker.firm_id,
                    matter_id,
                    request.request_hash,
                    result.provider_request_id,
                    body_hash,
                    len(result.response_body),
                    result.response_body,
                    self._worker.actor_id,
                ),
            )


            proven = connection.execute(
                """
                SELECT status, request_hash, response_sha256
                FROM case_agent_visual_ocr_outcomes
                WHERE exchange_id = %s AND firm_id = %s
                """,
                (exchange_id, self._worker.firm_id),
            ).fetchone()
            if (
                proven is None
                or proven["status"] != "SUCCEEDED"
                or proven["request_hash"] != request.request_hash
                or proven["response_sha256"] != body_hash
            ):
                raise QwenVisualOcrBlocked(
                    "visual OCR success outcome could not be proven"
                )

    def _record_failed(
        self,
        *,
        exchange_id: str,
        matter_id: str,
        request: QwenVisualOcrTransportRequest,
        error_code: str,
    ) -> None:
        if re.fullmatch(r"QWEN_VISUAL_OCR_[A-Z0-9_]{3,57}", error_code) is None:
            raise QwenVisualOcrBlocked("visual OCR terminal failure code is invalid")
        with _transaction(
            self._dsn,
            self._worker.firm_id,
            actor_id=self._worker.actor_id,
            read_only=False,
        ) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_visual_ocr_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, status, request_hash, error_code,
                    recorded_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,'FAILED',%s,%s,%s
                ) ON CONFLICT (exchange_id) DO NOTHING
                """,
                (
                    str(uuid5(UUID(exchange_id), "visual-ocr-outcome-v1")),
                    exchange_id,
                    request.external_request_id,
                    self._worker.firm_id,
                    matter_id,
                    request.request_hash,
                    error_code,
                    self._worker.actor_id,
                ),
            )
            proven = connection.execute(
                """
                SELECT status, request_hash, error_code
                FROM case_agent_visual_ocr_outcomes
                WHERE exchange_id = %s AND firm_id = %s
                """,
                (exchange_id, self._worker.firm_id),
            ).fetchone()
            if (
                proven is None
                or proven["status"] != "FAILED"
                or proven["request_hash"] != request.request_hash
                or proven["error_code"] != error_code
            ):
                raise QwenVisualOcrBlocked(
                    "visual OCR failure outcome could not be proven"
                )

    def _record_unknown(
        self,
        *,
        exchange_id: str,
        matter_id: str,
        request: QwenVisualOcrTransportRequest,
        error_code: str,
    ) -> None:
        if error_code not in (
            QWEN_VISUAL_OCR_UNKNOWN_CODES
            | {"QWEN_VISUAL_OCR_OUTCOME_UNKNOWN"}
        ):
            raise QwenVisualOcrBlocked("visual OCR unknown stage code is invalid")
        with _transaction(
            self._dsn,
            self._worker.firm_id,
            actor_id=self._worker.actor_id,
            read_only=False,
        ) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_visual_ocr_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, status, request_hash, error_code,
                    recorded_by_worker
                ) VALUES (
                    %s,%s,%s,%s,%s,'UNKNOWN_SUBMISSION',%s,%s,%s
                ) ON CONFLICT (exchange_id) DO NOTHING
                """,
                (
                    str(uuid5(UUID(exchange_id), "visual-ocr-outcome-v1")),
                    exchange_id,
                    request.external_request_id,
                    self._worker.firm_id,
                    matter_id,
                    request.request_hash,
                    error_code,
                    self._worker.actor_id,
                ),
            )


def preflight_case_agent_visual_ocr_runtime_contract(
    *, dsn: str, worker_actor: Actor
) -> None:
    """Fail startup for an enabled Qwen adapter unless migration 0038 is exact."""

    if (
        not isinstance(worker_actor, Actor)
        or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
    ):
        raise CaseAgentVisualOcrPersistenceBlocked(
            "visual OCR preflight requires one dedicated SYSTEM_WORKER"
        )
    if not isinstance(dsn, str) or not dsn.strip():
        raise CaseAgentVisualOcrPersistenceBlocked(
            "visual OCR preflight PostgreSQL DSN is invalid"
        )
    required = {
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
    triggers = {
        "web_evidence_native_image_source_objects_integrity",
        "web_evidence_native_image_source_objects_append_only",
        "case_agent_visual_ocr_exchange_guard",
        "case_agent_visual_ocr_outcome_guard",
        "case_agent_visual_ocr_exchanges_append_only",
        "case_agent_visual_ocr_outcomes_append_only",
        "case_agent_visual_ocr_local_failure_resolution_guard",
        "case_agent_visual_ocr_local_failure_resolutions_append_only",
    }
    try:
        with _transaction(
            dsn,
            worker_actor.firm_id,
            actor_id=worker_actor.actor_id,
            read_only=True,
        ) as connection:
            column_rows = connection.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            observed: dict[str, set[str]] = {}
            for row in column_rows:
                observed.setdefault(str(row["table_name"]), set()).add(
                    str(row["column_name"])
                )
            if any(
                not columns.issubset(observed.get(table, set()))
                for table, columns in required.items()
            ):
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR migration 0038 is incomplete"
                )
            trigger_rows = connection.execute(
                """
                SELECT trigger_row.tgname AS trigger_name
                FROM pg_catalog.pg_trigger trigger_row
                JOIN pg_catalog.pg_class relation
                  ON relation.oid = trigger_row.tgrelid
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND NOT trigger_row.tgisinternal
                  AND trigger_row.tgname = ANY(%s)
                """,
                (list(triggers),),
            ).fetchall()
            if not triggers.issubset(
                {str(row["trigger_name"]) for row in trigger_rows}
            ):
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR guards are incomplete"
                )
            function_rows = connection.execute(
                """
                SELECT pg_get_functiondef(procedure.oid) AS definition
                FROM pg_catalog.pg_proc procedure
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid = procedure.pronamespace
                WHERE namespace.nspname = 'public'
                  AND procedure.proname =
                      'validate_case_agent_visual_ocr_exchange'
                """
            ).fetchall()
            if (
                len(function_rows) != 1
                or QWEN_VISUAL_OCR_POLICY_HASH
                not in str(function_rows[0]["definition"])
            ):
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR policy guard differs from runtime"
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
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR requires FORCE RLS"
                )
            constraint_rows = connection.execute(
                """
                SELECT pg_get_constraintdef(constraint_row.oid) AS definition
                FROM pg_catalog.pg_constraint constraint_row
                JOIN pg_catalog.pg_class relation
                  ON relation.oid = constraint_row.conrelid
                JOIN pg_catalog.pg_namespace namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'case_agent_visual_ocr_outcomes'
                  AND constraint_row.contype = 'c'
                """
            ).fetchall()
            definitions = tuple(
                str(row["definition"]) for row in constraint_rows
            )
            if not any(
                "FAILED" in definition
                and "QWEN_VISUAL_OCR_" in definition
                for definition in definitions
            ):
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR terminal-failure guard is incomplete"
                )
            if not any(
                all(code in definition for code in QWEN_VISUAL_OCR_UNKNOWN_CODES)
                for definition in definitions
            ):
                raise CaseAgentVisualOcrPersistenceBlocked(
                    "case-Agent visual OCR unknown-phase guard is incomplete"
                )
    except CaseAgentVisualOcrPersistenceBlocked:
        raise
    except Exception as error:
        raise CaseAgentVisualOcrPersistenceBlocked(
            "case-Agent visual OCR preflight failed"
        ) from error


def _native_locator(row: dict[str, Any], *, firm_id: str) -> PrivateNativeImageLocator:
    key = row["image_object_key"]
    match = _IMAGE_OBJECT.fullmatch(key or "")
    if (
        match is None
        or match.group("firm") != firm_id
        or match.group("matter") != row["matter_id"]
        or match.group("digest") != row["original_file_sha256"]
        or match.group("prefix") != row["original_file_sha256"][:2]
        or sha256(key.encode("utf-8")).hexdigest() != row["image_reference_hash"]
        or row["image_media_type"] != row["media_type"]
    ):
        raise QwenVisualOcrBlocked("native image object binding is invalid")
    return PrivateNativeImageLocator(
        firm_id=firm_id,
        matter_id=row["matter_id"],
        evidence_file_id=row["evidence_file_id"],
        content_sha256=row["original_file_sha256"],
        byte_size=int(row["byte_size"]),
        media_type=row["media_type"],
        source_reference_hash=row["image_reference_hash"],
        object_key=key,
        object_version_id=row["image_version"],
    )


def _page_ids(refs: object) -> tuple[str, ...]:
    if not isinstance(refs, tuple):
        raise QwenVisualOcrBlocked("visual input refs are invalid")
    result: list[str] = []
    for value in refs:
        match = _PAGE_REF.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise QwenVisualOcrBlocked("visual input must be an evidence page ref")
        result.append(match.group("id"))
    if len(set(result)) != len(result):
        raise QwenVisualOcrBlocked("visual input page refs are duplicated")
    return tuple(result)


def _validate_transport_identity(request: object) -> None:
    if (
        not isinstance(request, QwenVisualOcrTransportRequest)
        or request.method != "POST"
        or request.endpoint
        != f"https://{request.endpoint_host}/compatible-mode/v1/chat/completions"
        or request.request_hash != sha256(request.body).hexdigest()
        or not 1.0 <= request.timeout_seconds <= 120.0
        or request.max_response_bytes != 2 * 1024 * 1024
        or re.fullmatch(
            r"[a-z0-9][a-z0-9-]{2,62}[.]cn-beijing[.]maas[.]aliyuncs[.]com",
            request.endpoint_host,
        )
        is None
    ):
        raise QwenVisualOcrBlocked("visual OCR transport identity is invalid")
    _uuid(request.external_request_id, "visual external_request_id")
    for value, label in (
        (request.request_hash, "visual request_hash"),
        (request.projection_hash, "visual projection_hash"),
        (request.rendered_page_sha256, "visual rendered_page_sha256"),
    ):
        _sha(value, label)


def _validate_transport_result(
    request: QwenVisualOcrTransportRequest,
    result: object,
) -> None:
    if (
        not isinstance(result, QwenVisualOcrTransportResult)
        or result.external_request_id != request.external_request_id
        or result.request_hash != request.request_hash
        or not isinstance(result.provider_request_id, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,500}", result.provider_request_id)
        is None
        or not isinstance(result.response_body, bytes)
        or not 2 <= len(result.response_body) <= 2 * 1024 * 1024
    ):
        raise QwenVisualOcrBlocked("visual OCR transport result is invalid")


@contextmanager
def _transaction(
    dsn: str,
    firm_id: str,
    *,
    actor_id: str | None = None,
    read_only: bool = True,
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            + (" READ ONLY" if read_only else "")
        )
        connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
        if actor_id is not None:
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)", (actor_id,)
            )
        yield connection


def _private_directory(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("visual OCR worker root must be absolute")
    path = path.resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("visual OCR worker root must be a private directory")
    return path


def _executable(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("visual OCR renderer must be absolute")
    path = path.resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
        raise ValueError("visual OCR renderer is not executable")
    return path


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise QwenVisualOcrBlocked(f"{label} is invalid") from error


def _sha(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise QwenVisualOcrBlocked(f"{label} is invalid")


__all__ = (
    "CaseAgentVisualOcrPersistenceBlocked",
    "PostgresDurableQwenVisualOcrBroker",
    "PostgresVisualOcrBindingPort",
    "PrivateNativeImageLocator",
    "VisualOcrPrivateObjectStore",
    "VisualOcrProjectionPolicy",
    "preflight_case_agent_visual_ocr_runtime_contract",
)
