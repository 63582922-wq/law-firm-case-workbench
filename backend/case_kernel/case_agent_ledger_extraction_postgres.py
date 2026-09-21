"""Private staging command for independently verified ledger extraction.

The command receives only ``run_id`` and ``artifact_id`` from a future server
API.  Candidate prose, hashes and evidence-page identifiers are re-read from
the private immutable artifact.  This deliberately stops at review staging:
it never inserts into ``case_facts`` or ``case_transactions`` and never
approves a fact, transaction, classification or legal conclusion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_agent_ledger_extraction import (
    AUTO_STAGE_CONFIDENCE_MINIMUM,
    CASE_LEDGER_EXTRACTION_ARTIFACT_KIND,
    CaseLedgerExtractionBlocked,
    extraction_candidate_is_eligible,
    extraction_source_refs,
    parse_case_ledger_extraction_candidate,
)
from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_roles,
    _require_positive_version,
    _validate_command_identity,
    _validate_uuid,
)
from .evidence_refs import EvidenceLink, validate_evidence_links
from .errors import VersionConflict
from .models import Actor, Role
from .transaction_ledger import DatePrecision, TransactionChannel, TransactionDirection


class CaseLedgerExtractionStagingBlocked(ValueError):
    """A verified extraction cannot safely enter private review staging."""


_REVIEW_REASON_CODES = (
    "POSSIBLE_DUPLICATE",
    "PARTY_AMBIGUOUS",
    "DATE_AMBIGUOUS",
    "AMOUNT_AMBIGUOUS",
    "CROSS_PAGE_CONFLICT",
    "CONTRADICTS_CASE_LEDGER",
    "OCR_DERIVED",
    "LOW_CONFIDENCE",
    "LEGAL_CONCLUSION_RISK",
    "INCOMPLETE_TRANSACTION",
    "UNTRUSTED_TEXT",
    "BELOW_BULK_CONFIDENCE_THRESHOLD",
    "NON_NATIVE_SOURCE",
    "SOURCE_TEXT_NOT_REVERIFIED",
    "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE",
)


class _CandidateObjectStore(Protocol):
    def read_case_agent_review_candidate(
        self, stored: object, *, artifact_id: str
    ) -> bytes: ...


class _EvidencePageTextReader(Protocol):
    """Server-owned plaintext projection; never a browser text payload."""

    def read_evidence_page_text(
        self, *, firm_id: str, matter_id: str, evidence_page_id: str,
        source_file_sha256: str, page_number: int,
    ) -> str: ...


class TaskBoundEvidencePageTextReader:
    """Re-read a compiled task's authorised page projection on the server.

    The existing Web projection port already proves the human material-scope
    grant and resolves only task-owned ``evidence-page:`` refs.  This adapter
    deliberately exposes no path, object key or browser text.  It is usable
    by staging/promotion only when they supply the immutable task binding.
    """

    def __init__(self, *, projection_port: object) -> None:
        if not callable(getattr(projection_port, "project_ledger_pages", None)):
            raise ValueError("task-bound evidence projection port is invalid")
        self._projection_port = projection_port

    def read_task_evidence_page_texts(
        self, *, run_id: str, task_id: str, task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> dict[str, tuple[str, str, int, str]]:
        pages = self._projection_port.project_ledger_pages(
            run_id=run_id, task_id=task_id, task_input_hash=task_input_hash,
            input_refs=input_refs,
        )
        if (
            not isinstance(pages, tuple)
            or len(pages) < len(input_refs)
            or len(pages) > 200
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "authoritative evidence projection differs from the task"
            )
        result: dict[str, tuple[str, str, int, str]] = {}
        for page in pages:
            page_id = getattr(page, "evidence_page_id", None)
            ref = getattr(page, "input_ref", None)
            file_hash = getattr(page, "source_file_sha256", None)
            page_number = getattr(page, "page_number", None)
            text = getattr(page, "extracted_text", None)
            text_hash = getattr(page, "extracted_text_sha256", None)
            if (
                not isinstance(page_id, str)
                or ref != f"evidence-page:{page_id}"
                or not isinstance(file_hash, str)
                or type(page_number) is not int
                or not isinstance(text, str)
                or not isinstance(text_hash, str)
                or sha256(text.encode("utf-8")).hexdigest() != text_hash
                or page_id in result
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "authoritative evidence projection is invalid"
                )
            result[page_id] = (file_hash, text, page_number, text_hash)
        if not set(input_refs).issubset(
            {f"evidence-page:{page_id}" for page_id in result}
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "authoritative evidence projection omitted a compiled task page"
            )
        return result


@dataclass(frozen=True)
class StagedCaseLedgerExtractionBatch:
    receipt: CaseLedgerCommandReceipt
    source_artifact_id: str
    staged_candidate_count: int


class PostgresCaseLedgerExtractionStagingStore:
    """Stage eligible source-bound extraction proposals in one matter command."""

    _ROLES = frozenset({Role.SYSTEM_WORKER})

    def __init__(
        self, *, dsn: str, worker_actor: Actor, object_store: _CandidateObjectStore,
        evidence_page_text_reader: _EvidencePageTextReader | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent extraction staging PostgreSQL DSN is required")
        if (
            not isinstance(worker_actor, Actor)
            or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
        ):
            raise PermissionError("ledger extraction staging requires a dedicated SYSTEM_WORKER")
        _validate_uuid("worker actor_id", worker_actor.actor_id)
        _validate_uuid("worker firm_id", worker_actor.firm_id)
        if not callable(getattr(object_store, "read_case_agent_review_candidate", None)):
            raise ValueError("case-Agent extraction staging object store is invalid")
        if evidence_page_text_reader is not None and not any(
            callable(getattr(evidence_page_text_reader, method, None))
            for method in ("read_task_evidence_page_texts", "read_evidence_page_text")
        ):
            raise ValueError("case-Agent extraction source reader is invalid")
        self._dsn = dsn.strip()
        self._worker = worker_actor
        self._object_store = object_store
        self._source_reader = evidence_page_text_reader

    def __repr__(self) -> str:
        return "PostgresCaseLedgerExtractionStagingStore(<tenant-scoped>)"

    def discover_verified_artifact_ids(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        run_id: str,
    ) -> tuple[str, ...]:
        """Discover every extraction artifact bound by the current PASSED receipt.

        Discovery itself is read-only and deliberately does not compare the
        *current* matter version.  That lets a commit-lost retry rediscover the
        same immutable artifacts after this run's 0043 promotion has advanced
        V to V+1.  A first-time stage still locks the matter and requires V;
        0043 independently rejects any unrelated version advance.
        """

        _validate_command_identity(
            matter_id=matter_id,
            actor=actor,
            idempotency_key=f"discover-ledger-extraction:{run_id}",
        )
        if actor.actor_id != self._worker.actor_id or actor.firm_id != self._worker.firm_id:
            raise PermissionError("extraction staging identity differs from configured Worker")
        _require_positive_version(expected_version)
        _validate_uuid("run_id", run_id)
        with self._transaction() as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._ROLES,
            )
            run = connection.execute(
                """
                SELECT agent_run.snapshot_matter_version,
                       receipt.artifact_lineage
                FROM case_agent_runs agent_run
                JOIN case_agent_task_graphs graph
                  ON graph.graph_id = agent_run.current_graph_id
                 AND graph.run_id = agent_run.run_id
                 AND graph.firm_id = agent_run.firm_id
                 AND graph.matter_id = agent_run.matter_id
                JOIN case_agent_verification_receipts receipt
                  ON receipt.run_id = agent_run.run_id
                 AND receipt.firm_id = agent_run.firm_id
                 AND receipt.matter_id = agent_run.matter_id
                 AND receipt.verification_hash = agent_run.verification_hash
                 AND receipt.outcome = 'PASSED'
                 AND receipt.graph_hash = graph.graph_hash
                 AND receipt.snapshot_hash = graph.snapshot_hash
                WHERE agent_run.run_id = %s AND agent_run.firm_id = %s
                  AND agent_run.matter_id = %s
                  AND agent_run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
                  AND NOT agent_run.is_stale AND NOT agent_run.is_cancelled
                  AND agent_run.current_graph_hash = graph.graph_hash
                  AND agent_run.current_graph_version = graph.graph_version
                  AND agent_run.snapshot_hash = graph.snapshot_hash
                  AND receipt.verifier_actor_id <> receipt.execution_actor_id
                FOR KEY SHARE OF agent_run, graph, receipt
                """,
                (run_id, actor.firm_id, matter_id),
            ).fetchone()
            if run is None or int(run["snapshot_matter_version"]) != expected_version:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction discovery requires the current independently PASSED run snapshot"
                )
            rows = connection.execute(
                """
                SELECT artifact_id, task_id, content_sha256
                FROM case_agent_review_candidates
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND artifact_kind = %s
                  AND review_status = 'NEEDS_LAWYER_REVIEW'
                ORDER BY artifact_id
                FOR KEY SHARE
                """,
                (
                    run_id,
                    actor.firm_id,
                    matter_id,
                    CASE_LEDGER_EXTRACTION_ARTIFACT_KIND,
                ),
            ).fetchall()
            lineage = run["artifact_lineage"]
            if not isinstance(lineage, list):
                raise CaseLedgerExtractionStagingBlocked(
                    "the PASSED receipt artifact lineage is invalid"
                )
            lineage_extraction_ids = [
                str(item.get("artifact_id"))
                for item in lineage
                if isinstance(item, dict)
                and item.get("artifact_kind")
                == CASE_LEDGER_EXTRACTION_ARTIFACT_KIND
            ]
            artifact_ids: list[str] = []
            for row in rows:
                artifact_id = str(row["artifact_id"])
                if not _lineage_binds_extraction_artifact(
                    lineage,
                    artifact_id=artifact_id,
                    task_id=str(row["task_id"]),
                    content_hash=str(row["content_sha256"]),
                ):
                    raise CaseLedgerExtractionStagingBlocked(
                        "an extraction candidate is not bound by the current PASSED receipt"
                    )
                artifact_ids.append(artifact_id)
            if (
                len(set(artifact_ids)) != len(artifact_ids)
                or len(set(lineage_extraction_ids)) != len(lineage_extraction_ids)
                or set(artifact_ids) != set(lineage_extraction_ids)
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "the PASSED extraction artifact lineage and private candidates differ"
                )
            return tuple(artifact_ids)

    def stage_verified_artifact(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        run_id: str,
        artifact_id: str,
    ) -> StagedCaseLedgerExtractionBatch:
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, self._ROLES)
        if actor.actor_id != self._worker.actor_id or actor.firm_id != self._worker.firm_id:
            raise PermissionError("extraction staging identity differs from configured Worker")
        _require_positive_version(expected_version)
        _validate_uuid("run_id", run_id)
        _validate_uuid("artifact_id", artifact_id)
        command_name = "STAGE_CASE_LEDGER_EXTRACTION"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "run_id": run_id,
            "artifact_id": artifact_id,
        }
        request_hash = _payload_hash(payload)

        with self._transaction() as connection:
            _advisory_lock(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            prior = _prior_receipt(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if prior is not None:
                return StagedCaseLedgerExtractionBatch(
                    receipt=prior, source_artifact_id=artifact_id,
                    staged_candidate_count=self._prior_count(connection, prior.object_id),
                )
            _authorize_and_lock_matter(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, allowed_roles=self._ROLES,
            )
            binding = self._read_verified_binding(
                connection, matter_id=matter_id, run_id=run_id, artifact_id=artifact_id,
                expected_version=expected_version,
            )
            content = self._read_object(binding)
            try:
                artifact = parse_case_ledger_extraction_candidate(content)
                declared_refs = extraction_source_refs(artifact)
            except CaseLedgerExtractionBlocked as error:
                raise CaseLedgerExtractionStagingBlocked(
                    "verified extraction artifact cannot be parsed again"
                ) from error
            compiled_refs = frozenset(binding["input_refs"])
            task_reader = getattr(
                self._source_reader, "read_task_evidence_page_texts", None
            )
            if (
                not compiled_refs.issubset(declared_refs)
                or (declared_refs != compiled_refs and not callable(task_reader))
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source refs are not bound to the task or its dependencies"
                )
            source_text_verified = self._verify_source_pages(
                connection, matter_id=matter_id, artifact=artifact, binding=binding
            )
            staged = list(artifact["candidates"])
            model_eligible = [
                item for item in staged
                if source_text_verified and extraction_candidate_is_eligible(artifact, item)
            ]
            ledger_clear_hashes = self._ledger_clear_candidate_hashes(
                connection,
                matter_id=matter_id,
                candidates=model_eligible,
            )
            review_reasons_by_hash = {
                str(item["candidate_hash"]): _candidate_review_reason_codes(
                    artifact=artifact,
                    candidate=item,
                    source_text_verified=source_text_verified,
                    ledger_clear=(
                        str(item["candidate_hash"]) in ledger_clear_hashes
                    ),
                )
                for item in staged
            }
            eligible_hashes = {
                candidate_hash
                for candidate_hash, reasons in review_reasons_by_hash.items()
                if not reasons
            }
            batch_id = str(uuid5(UUID(artifact_id), "case-ledger-extraction-stage-v1"))
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_batches (
                    extraction_batch_id, run_id, graph_id, task_id, artifact_id,
                    verification_receipt_id, firm_id, matter_id,
                    source_matter_version, staged_matter_version,
                    artifact_content_sha256, source_hash, task_input_hash,
                    candidate_count, eligible_candidate_count, staged_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    batch_id, run_id, binding["graph_id"], binding["task_id"], artifact_id,
                    binding["verification_receipt_id"], actor.firm_id, matter_id,
                    expected_version, expected_version,
                    binding["content_sha256"], artifact["source_hash"], artifact["task_input_hash"],
                    len(staged), len(eligible_hashes), actor.actor_id,
                ),
            )
            source_text_hash_by_page = {
                str(page["evidence_page_id"]): str(page["source_text_sha256"])
                for page in artifact["_source_pages"]
            }
            for item in staged:
                candidate_hash = item["candidate_hash"]
                candidate_id = str(uuid5(UUID(batch_id), candidate_hash))
                can_bulk_promote = candidate_hash in eligible_hashes
                connection.execute(
                    """
                    INSERT INTO case_agent_ledger_extraction_candidates (
                        extraction_candidate_id, extraction_batch_id, firm_id, matter_id,
                        candidate_hash, candidate_kind, confidence, review_lane,
                        eligible_for_bulk_promotion, review_reason_codes,
                        candidate_payload,
                        review_status, formal_fact, formal_transaction,
                        legal_conclusion, evidence_decision, court_ready
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'NEEDS_LAWYER_REVIEW',false,false,false,false,false)
                    """,
                    (
                        candidate_id, batch_id, actor.firm_id, matter_id, candidate_hash,
                        item["kind"], item["confidence"],
                        "BULK_PROMOTION_ELIGIBLE" if can_bulk_promote else "EXCEPTION_REVIEW",
                        can_bulk_promote,
                        list(review_reasons_by_hash[candidate_hash]),
                        Jsonb(item),
                    ),
                )
                for page_id in item["evidence_page_ids"]:
                    connection.execute(
                        """
                        INSERT INTO case_agent_ledger_extraction_candidate_pages (
                            extraction_candidate_id, evidence_page_id, firm_id, matter_id,
                            source_text_sha256
                        ) VALUES (%s,%s,%s,%s,%s)
                        """,
                        (
                            candidate_id, page_id, actor.firm_id, matter_id,
                            source_text_hash_by_page[page_id],
                        ),
                    )
            receipt = self._finish_private_staging_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                batch_id=batch_id,
                artifact_id=artifact_id,
                run_id=run_id,
                audit_payload={
                    "batch_id": batch_id, "artifact_id": artifact_id,
                    "run_id": run_id, "staged_candidate_count": len(staged),
                    "eligible_candidate_count": len(eligible_hashes),
                    "source_text_verified": source_text_verified,
                    "review_only": True,
                },
            )
            return StagedCaseLedgerExtractionBatch(
                receipt=receipt, source_artifact_id=artifact_id,
                staged_candidate_count=len(staged),
            )

    def _read_verified_binding(
        self, connection: Any, *, matter_id: str, run_id: str,
        artifact_id: str, expected_version: int,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT candidate.artifact_id, candidate.run_id, candidate.graph_id, candidate.task_id, candidate.task_input_hash,
                   candidate.content_sha256, candidate.byte_size,
                   candidate.source_object_key, candidate.source_object_version_id,
                   task.input_refs, graph.graph_hash, graph.snapshot_hash,
                   graph.snapshot_matter_version, run.current_graph_id,
                   receipt.verification_receipt_id, receipt.artifact_lineage
            FROM case_agent_review_candidates candidate
            JOIN case_agent_artifacts artifact
              ON artifact.artifact_id = candidate.artifact_id
             AND artifact.run_id = candidate.run_id AND artifact.firm_id = candidate.firm_id
             AND artifact.matter_id = candidate.matter_id
             AND artifact.artifact_kind = candidate.artifact_kind
             AND artifact.content_hash = candidate.content_sha256
             AND artifact.byte_size = candidate.byte_size
             AND artifact.source_input_hash = candidate.task_input_hash
            JOIN case_agent_tasks task
              ON task.graph_id = candidate.graph_id AND task.task_id = candidate.task_id
             AND task.run_id = candidate.run_id AND task.firm_id = candidate.firm_id
             AND task.matter_id = candidate.matter_id
             AND task.input_hash = candidate.task_input_hash
            JOIN case_agent_task_graphs graph
              ON graph.graph_id = candidate.graph_id AND graph.run_id = candidate.run_id
             AND graph.firm_id = candidate.firm_id AND graph.matter_id = candidate.matter_id
            JOIN case_agent_runs run
              ON run.run_id = candidate.run_id AND run.firm_id = candidate.firm_id
             AND run.matter_id = candidate.matter_id
            JOIN case_agent_verification_receipts receipt
              ON receipt.run_id = candidate.run_id AND receipt.firm_id = candidate.firm_id
             AND receipt.matter_id = candidate.matter_id AND receipt.outcome = 'PASSED'
             AND receipt.verification_hash = run.verification_hash
             AND receipt.graph_hash = graph.graph_hash AND receipt.snapshot_hash = graph.snapshot_hash
            WHERE candidate.artifact_id = %s AND candidate.artifact_kind = %s
              AND candidate.review_status = 'NEEDS_LAWYER_REVIEW'
              AND candidate.run_id = %s AND candidate.firm_id = %s
              AND candidate.matter_id = %s AND run.current_graph_id = candidate.graph_id
              AND run.current_graph_version = graph.graph_version
              AND run.current_graph_hash = graph.graph_hash
              AND run.snapshot_hash = graph.snapshot_hash
              AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
              AND NOT run.is_stale AND NOT run.is_cancelled
              AND receipt.verifier_actor_id <> receipt.execution_actor_id
            FOR KEY SHARE OF candidate, artifact, task, graph, run, receipt
            """,
            (artifact_id, CASE_LEDGER_EXTRACTION_ARTIFACT_KIND, run_id, self._worker.firm_id, matter_id),
        ).fetchall()
        if len(rows) != 1:
            raise CaseLedgerExtractionStagingBlocked(
                "verified extraction artifact is unavailable, stale or ambiguous"
            )
        row = dict(rows[0])
        if int(row["snapshot_matter_version"]) != expected_version:
            raise VersionConflict("Agent extraction source snapshot is stale")
        lineage = row["artifact_lineage"]
        if not _lineage_binds_extraction_artifact(
            lineage,
            artifact_id=artifact_id,
            task_id=str(row["task_id"]),
            content_hash=str(row["content_sha256"]),
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "passed verifier receipt does not bind the extraction artifact"
            )
        return row

    def _read_object(self, binding: dict[str, Any]) -> bytes:
        # The actual immutable locator is imported only at the S3 boundary.
        # This keeps the review-staging kernel independently importable while
        # preserving the object store's concrete type check in production.
        from .web_object_store import StoredCaseAgentReviewCandidate

        stored = StoredCaseAgentReviewCandidate(
            object_key=binding["source_object_key"], content_sha256=binding["content_sha256"],
            byte_size=int(binding["byte_size"]),
            object_version_id=binding["source_object_version_id"],
        )
        try:
            content = self._object_store.read_case_agent_review_candidate(
                stored, artifact_id=str(binding.get("artifact_id", ""))
            )
        except Exception as error:
            raise CaseLedgerExtractionStagingBlocked(
                "verified extraction artifact cannot be privately re-read"
            ) from error
        if (
            not isinstance(content, bytes)
            or len(content) != int(binding["byte_size"])
            or sha256(content).hexdigest() != binding["content_sha256"]
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "re-read extraction bytes differ from verified artifact"
            )
        return content

    def _verify_source_pages(
        self, connection: Any, *, matter_id: str, artifact: dict[str, object],
        binding: dict[str, Any] | None = None,
    ) -> bool:
        pages = artifact["_source_pages"]
        page_ids = [item["evidence_page_id"] for item in pages]
        rows = connection.execute(
            """
            SELECT page.evidence_page_id, page.page_number, source.original_file_sha256
            FROM evidence_pages page
            JOIN evidence_original_files source
              ON source.evidence_file_id = page.evidence_file_id
             AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
            WHERE page.evidence_page_id = ANY(%s) AND page.firm_id = %s
              AND page.matter_id = %s
            FOR KEY SHARE OF page, source
            """,
            (page_ids, self._worker.firm_id, matter_id),
        ).fetchall()
        by_id = {str(row["evidence_page_id"]): row for row in rows}
        if set(by_id) != set(page_ids):
            raise CaseLedgerExtractionStagingBlocked(
                "one or more extraction source pages are unavailable"
            )
        task_reader = getattr(self._source_reader, "read_task_evidence_page_texts", None)
        if callable(task_reader):
            if binding is None:
                raise CaseLedgerExtractionStagingBlocked(
                    "authoritative evidence task binding is unavailable"
                )
            try:
                projected = task_reader(
                    run_id=str(binding["run_id"]), task_id=str(binding["task_id"]),
                    task_input_hash=str(binding["task_input_hash"]),
                    input_refs=tuple(binding["input_refs"]),
                )
            except Exception as error:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source text cannot be independently re-read"
                ) from error
            if not isinstance(projected, dict) or set(projected) != set(page_ids):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source text projection differs from source pages"
                )
            for item in pages:
                value = projected.get(str(item["evidence_page_id"]))
                if (
                    not isinstance(value, tuple) or len(value) != 4
                    or value[0] != item["source_file_sha256"]
                    or value[2] != item["page_number"]
                    or value[3] != item["source_text_sha256"]
                    or not isinstance(value[1], str)
                ):
                    raise CaseLedgerExtractionStagingBlocked(
                        "extraction source projection differs from authorized page text"
                    )
                excerpts = [
                    excerpt["text"]
                    for candidate in artifact["candidates"]
                    for excerpt in candidate["supporting_excerpts"]
                    if excerpt["evidence_page_id"] == item["evidence_page_id"]
                ]
                if any(excerpt not in value[1] for excerpt in excerpts):
                    raise CaseLedgerExtractionStagingBlocked(
                        "extraction supporting excerpt is not literal source-page text"
                    )
            return True
        for item in pages:
            row = by_id[str(item["evidence_page_id"])]
            if (
                int(row["page_number"]) != item["page_number"]
                or row["original_file_sha256"] != item["source_file_sha256"]
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source page differs from the authoritative evidence ledger"
                )
        if self._source_reader is None:
            # Preserve every record for lawyer exception review, but without a
            # separate server-owned text reader no record may take the bulk
            # promotion lane.  A confidence number is not source verification.
            return False
        for item in pages:
            try:
                source_text = self._source_reader.read_evidence_page_text(
                    firm_id=self._worker.firm_id, matter_id=matter_id,
                    evidence_page_id=str(item["evidence_page_id"]),
                    source_file_sha256=item["source_file_sha256"],
                    page_number=int(item["page_number"]),
                )
            except Exception as error:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source text cannot be independently re-read"
                ) from error
            if (
                not isinstance(source_text, str)
                or sha256(source_text.encode("utf-8")).hexdigest()
                != item["source_text_sha256"]
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source text differs from the authorized page projection"
                )
            excerpts = [
                excerpt["text"]
                for candidate in artifact["candidates"]
                for excerpt in candidate["supporting_excerpts"]
                if excerpt["evidence_page_id"] == item["evidence_page_id"]
            ]
            if any(excerpt not in source_text for excerpt in excerpts):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction supporting excerpt is not literal source-page text"
                )
        return True

    def _ledger_clear_candidate_hashes(
        self,
        connection: Any,
        *,
        matter_id: str,
        candidates: list[dict[str, object]],
    ) -> set[str]:
        """Keep only records that do not duplicate/conflict with current ledger.

        This is intentionally conservative.  A collision is not discarded or
        merged; it remains in the immutable batch under ``EXCEPTION_REVIEW``.
        The same check is repeated immediately before formal promotion.
        """

        return _ledger_clear_candidate_hashes(
            connection,
            firm_id=self._worker.firm_id,
            matter_id=matter_id,
            candidates=candidates,
        )

    @staticmethod
    def _finish_private_staging_command(
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        expected_version: int,
        command_name: str,
        idempotency_key: str,
        payload_hash: str,
        batch_id: str,
        artifact_id: str,
        run_id: str,
        audit_payload: dict[str, object],
    ) -> CaseLedgerCommandReceipt:
        """Commit a private projection receipt without consuming matter V+1."""

        event_id = str(
            uuid5(UUID(batch_id), "case-ledger-extraction-private-stage-event-v1")
        )
        connection.execute(
            """
            INSERT INTO case_agent_ledger_extraction_staging_events (
                staging_event_id, extraction_batch_id, artifact_id, run_id,
                firm_id, matter_id, event_type, source_matter_version,
                staged_matter_version, actor_id, idempotency_key, request_hash,
                payload
            ) VALUES (
                %s,%s,%s,%s,%s,%s,'VERIFIED_EXTRACTION_STAGED_PRIVATE',
                %s,%s,%s,%s,%s,%s
            )
            """,
            (
                event_id,
                batch_id,
                artifact_id,
                run_id,
                actor.firm_id,
                matter_id,
                expected_version,
                expected_version,
                actor.actor_id,
                idempotency_key,
                payload_hash,
                Jsonb(audit_payload),
            ),
        )
        receipt = CaseLedgerCommandReceipt(
            command_name=command_name,
            idempotency_key=idempotency_key,
            matter_id=matter_id,
            matter_version=expected_version,
            audit_event_id=event_id,
            object_type="CASE_LEDGER_EXTRACTION_BATCH",
            object_id=batch_id,
        )
        connection.execute(
            """
            INSERT INTO command_idempotency (
                firm_id, matter_id, actor_id, command_name, idempotency_key,
                request_hash, response_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                actor.firm_id,
                matter_id,
                actor.actor_id,
                command_name,
                idempotency_key,
                payload_hash,
                Jsonb(asdict(receipt)),
            ),
        )
        return receipt

    @staticmethod
    def _prior_count(connection: Any, batch_id: str) -> int:
        row = connection.execute(
            """
            SELECT candidate_count FROM case_agent_ledger_extraction_batches
            WHERE extraction_batch_id = %s
            """, (batch_id,)
        ).fetchone()
        if row is None:
            raise CaseLedgerExtractionStagingBlocked("idempotent extraction batch is missing")
        return int(row["candidate_count"])

    def _transaction(self):
        return _TenantTransaction(self._dsn, self._worker.firm_id)


class PostgresCaseLedgerExtractionPromotionStore:
    """Lead-lawyer batch promotion from private staging to formal candidates.

    The browser may name a batch but cannot choose a subset, send candidate
    fields or send evidence links.  A staged batch is all-or-nothing: this
    avoids changing the matter version and silently making unselected sibling
    records stale.  Each target remains ``CANDIDATE``.
    """

    _ROLES = frozenset({Role.LEAD_LAWYER})

    def __init__(
        self, dsn: str, *, evidence_page_text_reader: _EvidencePageTextReader | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("case-Agent extraction promotion PostgreSQL DSN is required")
        if evidence_page_text_reader is not None and not any(
            callable(getattr(evidence_page_text_reader, method, None))
            for method in ("read_task_evidence_page_texts", "read_evidence_page_text")
        ):
            raise ValueError("case-Agent extraction promotion source reader is invalid")
        self._dsn = dsn.strip()
        self._source_reader = evidence_page_text_reader

    def promote_batch(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        extraction_batch_id: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, self._ROLES)
        _require_positive_version(expected_version)
        _validate_uuid("extraction_batch_id", extraction_batch_id)
        command_name = "PROMOTE_CASE_LEDGER_EXTRACTION_BATCH"
        request_hash = _payload_hash({
            "matter_id": matter_id, "expected_version": expected_version,
            "extraction_batch_id": extraction_batch_id,
        })
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _advisory_lock(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            prior = _prior_receipt(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, allowed_roles=self._ROLES,
            )
            batch, candidates = self._read_current_batch(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, extraction_batch_id=extraction_batch_id,
            )
            if not candidates:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction batch has no eligible candidates to promote"
                )
            source_links, source_text_hashes = self._read_source_links(
                connection, actor=actor, matter_id=matter_id, candidates=candidates,
            )
            self._verify_promotable_source_text(
                actor=actor, matter_id=matter_id, candidates=candidates,
                source_links=source_links, source_text_hashes=source_text_hashes,
                task_binding=batch,
            )
            promotion_id = str(uuid5(UUID(extraction_batch_id), "formal-candidate-promotion-v1"))
            # The lead's one explicit action accepts this entire *already
            # source-proven low-risk group* for entry as formal CANDIDATE
            # records.  The browser supplies no candidate prose/hashes/pages;
            # this decision hash is built from the server-re-read immutable
            # rows immediately before the one transaction writes them.
            lawyer_batch_decision_hash = _payload_hash(
                {
                    "schema_version": "case-ledger-extraction-batch-decision-v1",
                    "action": "PROMOTE_REVIEWED_LOW_RISK_GROUP_TO_CANDIDATES",
                    "matter_id": matter_id,
                    "extraction_batch_id": extraction_batch_id,
                    "run_id": str(batch["run_id"]),
                    "graph_id": str(batch["graph_id"]),
                    "lawyer_id": actor.actor_id,
                    "candidate_hashes": [
                        str(item["candidate_hash"])
                        for item in candidates
                    ],
                }
            )
            for item in candidates:
                links = source_links[str(item["extraction_candidate_id"])]
                payload = item["candidate_payload"]
                candidate_kind = item["candidate_kind"]
                target_id = str(uuid5(UUID(promotion_id), str(item["candidate_hash"])))
                if candidate_kind == "FACT":
                    text = _fact_text_from_staged_payload(payload)
                    connection.execute(
                        """
                        INSERT INTO case_facts (
                            fact_id, firm_id, matter_id, original_text, origin, status, evidence_links
                        ) VALUES (%s,%s,%s,%s,'AGENT_CANDIDATE','CANDIDATE',%s)
                        """,
                        (target_id, actor.firm_id, matter_id, text, Jsonb(_evidence_payload(links))),
                    )
                    target_type = "FACT"
                elif candidate_kind == "TRANSACTION":
                    transaction = _transaction_from_staged_payload(payload)
                    connection.execute(
                        """
                        INSERT INTO case_transactions (
                            transaction_id, firm_id, matter_id, local_date, date_precision,
                            amount, currency, direction, payer_label, payee_label, channel,
                            transaction_reference, evidence_links, status
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CANDIDATE')
                        """,
                        (
                            target_id, actor.firm_id, matter_id, transaction["local_date"],
                            transaction["date_precision"], transaction["amount"], transaction["currency"],
                            transaction["direction"], transaction["payer_label"], transaction["payee_label"],
                            transaction["channel"], transaction["transaction_reference"],
                            Jsonb(_evidence_payload(links)),
                        ),
                    )
                    target_type = "TRANSACTION"
                else:  # Database CHECK and the read query must agree.
                    raise CaseLedgerExtractionStagingBlocked("staged candidate kind is invalid")
                connection.execute(
                    """
                    INSERT INTO case_agent_ledger_extraction_promotions (
                        extraction_promotion_id, extraction_batch_id, extraction_candidate_id,
                        firm_id, matter_id, target_object_type, target_object_id,
                        promoted_matter_version, lawyer_batch_decision_hash, promoted_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid5(UUID(promotion_id), str(item["extraction_candidate_id"]))),
                        extraction_batch_id, item["extraction_candidate_id"], actor.firm_id,
                        matter_id, target_type, target_id, expected_version + 1,
                        lawyer_batch_decision_hash, actor.actor_id,
                    ),
                )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, command_name=command_name,
                idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="CASE_LEDGER_EXTRACTION_BATCH_PROMOTED",
                object_type="CASE_LEDGER_EXTRACTION_BATCH", object_id=extraction_batch_id,
                audit_payload={
                    "extraction_batch_id": extraction_batch_id,
                    "promotion_id": promotion_id,
                    "lawyer_batch_decision_hash": lawyer_batch_decision_hash,
                    "candidate_count": len(candidates),
                    "target_status": "CANDIDATE",
                    "lawyer_approval_performed": False,
                },
                stale_submission=False, stale_calculations=False,
            )

    def confirm_low_risk_batch(
        self,
        *,
        matter_id: str,
        actor: Actor,
        session_id: str | None = None,
        expected_version: int,
        idempotency_key: str,
        extraction_batch_id: str,
    ) -> CaseLedgerCommandReceipt:
        """One explicit LEAD action: source-proven low-risk group -> CONFIRMED.

        This is the primary lawyer path after the UI has shown the group
        count, summaries and literal source excerpts.  It accepts no item
        body, candidate hash or page id from the browser; the full eligible
        set is re-read and source-proven in this transaction.  Exception lane
        records can never enter this command.
        """

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, self._ROLES)
        _require_positive_version(expected_version)
        _validate_uuid("extraction_batch_id", extraction_batch_id)
        command_name = "CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH"
        request_hash = _payload_hash({
            "matter_id": matter_id,
            "expected_version": expected_version,
            "extraction_batch_id": extraction_batch_id,
        })
        if session_id is not None:
            _validate_uuid("server Web session_id", session_id)
            return self._confirm_low_risk_batch_from_web_session(
                matter_id=matter_id,
                actor=actor,
                session_id=session_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                extraction_batch_id=extraction_batch_id,
                request_hash=request_hash,
            )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _advisory_lock(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            prior = _prior_receipt(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if prior is not None:
                return prior
            _authorize_and_lock_matter(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, allowed_roles=self._ROLES,
            )
            batch, candidates = self._read_current_batch(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, extraction_batch_id=extraction_batch_id,
            )
            if not candidates:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction batch has no eligible candidates to confirm"
                )
            source_links, source_text_hashes = self._read_source_links(
                connection, actor=actor, matter_id=matter_id, candidates=candidates,
            )
            self._verify_promotable_source_text(
                actor=actor, matter_id=matter_id, candidates=candidates,
                source_links=source_links, source_text_hashes=source_text_hashes,
                task_binding=batch,
            )
            decision_hash = _payload_hash(
                {
                    "schema_version": "case-ledger-extraction-batch-decision-v1",
                    "action": "CONFIRM_REVIEWED_LOW_RISK_GROUP",
                    "matter_id": matter_id,
                    "extraction_batch_id": extraction_batch_id,
                    "run_id": str(batch["run_id"]),
                    "graph_id": str(batch["graph_id"]),
                    "lawyer_id": actor.actor_id,
                    "candidate_hashes": [str(item["candidate_hash"]) for item in candidates],
                }
            )
            confirmation_id = str(uuid5(
                UUID(extraction_batch_id), "confirmed-low-risk-group-v1"
            ))
            for item in candidates:
                links = source_links[str(item["extraction_candidate_id"])]
                target_id = str(uuid5(UUID(confirmation_id), str(item["candidate_hash"])))
                if item["candidate_kind"] == "FACT":
                    text = _fact_text_from_staged_payload(item["candidate_payload"])
                    connection.execute(
                        """
                        INSERT INTO case_facts (
                            fact_id, firm_id, matter_id, original_text, origin, status,
                            evidence_links, decision_hash, decided_by
                        ) VALUES (%s,%s,%s,%s,'AGENT_CANDIDATE','CONFIRMED',%s,%s,%s)
                        """,
                        (
                            target_id, actor.firm_id, matter_id, text,
                            Jsonb(_evidence_payload(links)), decision_hash, actor.actor_id,
                        ),
                    )
                    target_type = "FACT"
                elif item["candidate_kind"] == "TRANSACTION":
                    transaction = _transaction_from_staged_payload(item["candidate_payload"])
                    connection.execute(
                        """
                        INSERT INTO case_transactions (
                            transaction_id, firm_id, matter_id, local_date, date_precision,
                            amount, currency, direction, payer_label, payee_label, channel,
                            transaction_reference, evidence_links, status, confirmation_hash,
                            confirmed_by
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CONFIRMED',%s,%s)
                        """,
                        (
                            target_id, actor.firm_id, matter_id, transaction["local_date"],
                            transaction["date_precision"], transaction["amount"], transaction["currency"],
                            transaction["direction"], transaction["payer_label"], transaction["payee_label"],
                            transaction["channel"], transaction["transaction_reference"],
                            Jsonb(_evidence_payload(links)), decision_hash, actor.actor_id,
                        ),
                    )
                    target_type = "TRANSACTION"
                else:
                    raise CaseLedgerExtractionStagingBlocked("staged candidate kind is invalid")
                connection.execute(
                    """
                    INSERT INTO case_agent_ledger_extraction_promotions (
                        extraction_promotion_id, extraction_batch_id, extraction_candidate_id,
                        firm_id, matter_id, target_object_type, target_object_id,
                        promoted_matter_version, lawyer_batch_decision_hash, promoted_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid5(UUID(confirmation_id), str(item["extraction_candidate_id"]))),
                        extraction_batch_id, item["extraction_candidate_id"], actor.firm_id,
                        matter_id, target_type, target_id, expected_version + 1,
                        decision_hash, actor.actor_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO case_agent_ledger_extraction_batch_confirmations (
                    extraction_batch_id, firm_id, matter_id, lawyer_batch_decision_hash,
                    confirmed_candidate_count, confirmed_matter_version, confirmed_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    extraction_batch_id, actor.firm_id, matter_id, decision_hash,
                    len(candidates), expected_version + 1, actor.actor_id,
                ),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id,
                expected_version=expected_version, command_name=command_name,
                idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="CASE_LEDGER_EXTRACTION_LOW_RISK_BATCH_CONFIRMED",
                object_type="CASE_LEDGER_EXTRACTION_BATCH", object_id=extraction_batch_id,
                audit_payload={
                    "extraction_batch_id": extraction_batch_id,
                    "lawyer_batch_decision_hash": decision_hash,
                    "confirmed_candidate_count": len(candidates),
                    "exception_candidates_included": False,
                    "source_text_reverified": True,
                },
                stale_submission=False, stale_calculations=False,
            )

    def _confirm_low_risk_batch_from_web_session(
        self,
        *,
        matter_id: str,
        actor: Actor,
        session_id: str,
        expected_version: int,
        idempotency_key: str,
        extraction_batch_id: str,
        request_hash: str,
    ) -> CaseLedgerCommandReceipt:
        """Use the 0048 session-bound definer command for the Web path.

        The first short transaction creates an opaque, one-time database
        approval only after the database has resolved the live Web session to
        an ACTIVE lead lawyer on this matter.  Source projection bytes are
        then re-read outside that lock.  The final short transaction consumes
        the approval and repeats every authoritative database check before it
        writes the complete low-risk group.  Neither call accepts an actor or
        firm identifier from the SQL caller.
        """

        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            row = connection.execute(
                """
                SELECT public.authorize_case_agent_ledger_extraction_low_risk_confirmation(
                    %s, %s, %s, %s, %s, %s
                ) AS authorization
                """,
                (
                    session_id,
                    matter_id,
                    extraction_batch_id,
                    expected_version,
                    idempotency_key,
                    request_hash,
                ),
            ).fetchone()
        authorization = _json_object_result(row, "authorization")
        status = authorization.get("status")
        if status == "COMPLETED":
            return _case_ledger_command_receipt(
                authorization.get("receipt"),
                command_name="CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
                idempotency_key=idempotency_key,
                matter_id=matter_id,
                extraction_batch_id=extraction_batch_id,
            )
        if status != "AUTHORIZED":
            raise CaseLedgerExtractionStagingBlocked(
                "database did not authorize the Web ledger confirmation"
            )
        approval_id = str(authorization.get("approval_id", ""))
        _validate_uuid("ledger confirmation approval_id", approval_id)

        # Read immutable database bindings in a separate, bounded transaction.
        # The external/object-store source projection is deliberately read only
        # after those locks are released; the finalizer detects any intervening
        # version, batch, candidate, role or session change.
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            batch, candidates = self._read_current_batch(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                extraction_batch_id=extraction_batch_id,
                lock_rows=False,
            )
            if not candidates:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction batch has no eligible candidates to confirm"
                )
            source_links, source_text_hashes = self._read_source_links(
                connection,
                actor=actor,
                matter_id=matter_id,
                candidates=candidates,
                lock_rows=False,
            )
        self._verify_promotable_source_text(
            actor=actor,
            matter_id=matter_id,
            candidates=candidates,
            source_links=source_links,
            source_text_hashes=source_text_hashes,
            task_binding=batch,
        )
        source_verification_hash = _ledger_confirmation_source_binding_hash(
            extraction_batch_id=extraction_batch_id,
            batch=batch,
            candidates=candidates,
            source_text_hashes=source_text_hashes,
        )

        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            row = connection.execute(
                """
                SELECT public.finalize_case_agent_ledger_extraction_low_risk_confirmation(
                    %s, %s
                ) AS receipt
                """,
                (approval_id, source_verification_hash),
            ).fetchone()
        return _case_ledger_command_receipt(
            _json_object_result(row, "receipt"),
            command_name="CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
            idempotency_key=idempotency_key,
            matter_id=matter_id,
            extraction_batch_id=extraction_batch_id,
        )

    @staticmethod
    def _read_current_batch(
        connection: Any, *, actor: Actor, matter_id: str, expected_version: int,
        extraction_batch_id: str, lock_rows: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # The session-bound Web path deliberately has SELECT but no UPDATE
        # privilege on immutable staging rows.  PostgreSQL row-lock clauses
        # require UPDATE privilege even when no mutation is attempted.  The
        # authorizer and finalizer repeat the live batch/version/session
        # checks, so the bounded source-verification read must remain
        # lock-free on that path; any intervening change is rejected by the
        # finalizer.  Direct trusted callers retain the original locks.
        batch_lock = "FOR KEY SHARE OF batch, run" if lock_rows else ""
        batch = connection.execute(
            f"""
            SELECT batch.extraction_batch_id, batch.run_id, batch.graph_id,
                   batch.task_id, batch.artifact_id, batch.candidate_count,
                   batch.eligible_candidate_count, batch.source_matter_version,
                   batch.staged_matter_version,
                   run.current_graph_id, task.input_hash AS task_input_hash,
                   task.input_refs AS task_input_refs,
                   case_agent_ledger_extraction_current_review_version(
                       batch.extraction_batch_id, batch.firm_id, batch.matter_id
                   ) AS current_review_version,
                   case_agent_ledger_extraction_run_staging_complete(
                       batch.run_id, batch.firm_id, batch.matter_id
                   ) AS run_staging_complete
            FROM case_agent_ledger_extraction_batches batch
            JOIN case_agent_runs run
              ON run.run_id = batch.run_id AND run.firm_id = batch.firm_id
             AND run.matter_id = batch.matter_id
            JOIN case_agent_tasks task
              ON task.graph_id = batch.graph_id AND task.task_id = batch.task_id
             AND task.run_id = batch.run_id AND task.firm_id = batch.firm_id
             AND task.matter_id = batch.matter_id
            WHERE batch.extraction_batch_id = %s AND batch.firm_id = %s
              AND batch.matter_id = %s
              AND run.status IN ('READY_FOR_REVIEW', 'COMPLETED')
              AND NOT run.is_stale AND NOT run.is_cancelled
            {batch_lock}
            """,
            (extraction_batch_id, actor.firm_id, matter_id),
        ).fetchone()
        if batch is None:
            raise KeyError(extraction_batch_id)
        batch = dict(batch)
        source_version = int(batch["source_matter_version"])
        if (
            int(batch["staged_matter_version"]) != source_version
            or batch.get("current_review_version") is None
            or int(batch["current_review_version"]) != expected_version
            or batch.get("run_staging_complete") is not True
            or str(batch["current_graph_id"]) != str(batch["graph_id"])
        ):
            raise VersionConflict("extraction batch is stale after a matter or Agent graph change")
        candidate_lock = "FOR KEY SHARE OF candidate" if lock_rows else ""
        rows = connection.execute(
            f"""
            SELECT candidate.extraction_candidate_id, candidate.candidate_hash,
                   candidate.candidate_kind, candidate.confidence, candidate.candidate_payload
            FROM case_agent_ledger_extraction_candidates candidate
            LEFT JOIN case_agent_ledger_extraction_promotions promotion
              ON promotion.extraction_candidate_id = candidate.extraction_candidate_id
            WHERE candidate.extraction_batch_id = %s AND candidate.firm_id = %s
              AND candidate.matter_id = %s AND candidate.eligible_for_bulk_promotion = true
              AND promotion.extraction_candidate_id IS NULL
            ORDER BY candidate.candidate_hash ASC
            {candidate_lock}
            """,
            (extraction_batch_id, actor.firm_id, matter_id),
        ).fetchall()
        candidates = [dict(row) for row in rows]
        if len(candidates) != int(batch["eligible_candidate_count"]):
            raise CaseLedgerExtractionStagingBlocked(
                "extraction bulk-promotion lane is partially promoted or differs from its immutable count"
            )
        ledger_candidates: list[dict[str, object]] = []
        for item in candidates:
            payload = item["candidate_payload"]
            if not isinstance(payload, dict):
                raise CaseLedgerExtractionStagingBlocked(
                    "staged extraction candidate payload is invalid"
                )
            ledger_candidates.append(
                {**payload, "candidate_hash": str(item["candidate_hash"])}
            )
        if _ledger_clear_candidate_hashes(
            connection,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            candidates=ledger_candidates,
        ) != {str(item["candidate_hash"]) for item in candidates}:
            raise CaseLedgerExtractionStagingBlocked(
                "the current case ledger duplicates or conflicts with this extraction batch"
            )
        return batch, candidates

    @staticmethod
    def _read_source_links(
        connection: Any, *, actor: Actor, matter_id: str,
        candidates: list[dict[str, Any]], lock_rows: bool = True,
    ) -> tuple[dict[str, tuple[EvidenceLink, ...]], dict[str, dict[str, str]]]:
        candidate_ids = [str(item["extraction_candidate_id"]) for item in candidates]
        source_lock = (
            "FOR KEY SHARE OF candidate_page, page, source" if lock_rows else ""
        )
        rows = connection.execute(
            f"""
            SELECT candidate_page.extraction_candidate_id, page.evidence_page_id,
                   page.page_number, source.original_file_sha256, source.original_label,
                   candidate_page.source_text_sha256
            FROM case_agent_ledger_extraction_candidate_pages candidate_page
            JOIN evidence_pages page
              ON page.evidence_page_id = candidate_page.evidence_page_id
             AND page.firm_id = candidate_page.firm_id AND page.matter_id = candidate_page.matter_id
            JOIN evidence_original_files source
              ON source.evidence_file_id = page.evidence_file_id
             AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
            WHERE candidate_page.extraction_candidate_id = ANY(%s)
              AND candidate_page.firm_id = %s AND candidate_page.matter_id = %s
            ORDER BY candidate_page.extraction_candidate_id, page.page_number, page.evidence_page_id
            {source_lock}
            """,
            (candidate_ids, actor.firm_id, matter_id),
        ).fetchall()
        grouped: dict[str, list[EvidenceLink]] = {}
        grouped_text_hashes: dict[str, dict[str, str]] = {}
        for row in rows:
            candidate_id = str(row["extraction_candidate_id"])
            page_id = str(row["evidence_page_id"])
            grouped.setdefault(candidate_id, []).append(
                EvidenceLink(
                    evidence_id=page_id,
                    original_file_sha256=row["original_file_sha256"],
                    page_number=int(row["page_number"]), region_id=None,
                    original_label=row["original_label"],
                )
            )
            text_hash = row["source_text_sha256"]
            if not isinstance(text_hash, str) or len(text_hash) != 64:
                raise CaseLedgerExtractionStagingBlocked(
                    "staged extraction source text hash is invalid"
                )
            grouped_text_hashes.setdefault(candidate_id, {})[page_id] = text_hash
        result: dict[str, tuple[EvidenceLink, ...]] = {}
        for item in candidates:
            candidate_id = str(item["extraction_candidate_id"])
            links = tuple(grouped.get(candidate_id, ()))
            payload = item["candidate_payload"]
            if (
                not isinstance(payload, dict)
                or set(str(link.evidence_id) for link in links)
                != set(payload.get("evidence_page_ids", ()))
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "staged extraction candidate source pages differ from the evidence ledger"
                )
            validate_evidence_links(links)
            result[candidate_id] = links
        return result, grouped_text_hashes

    def _verify_promotable_source_text(
        self, *, actor: Actor, matter_id: str, candidates: list[dict[str, Any]],
        source_links: dict[str, tuple[EvidenceLink, ...]],
        source_text_hashes: dict[str, dict[str, str]],
        task_binding: dict[str, Any],
    ) -> None:
        """Repeat the source-projection proof immediately before promotion.

        Staging is deliberately immutable, but the projection backing an
        evidence page is a separate service boundary.  Promotion therefore
        cannot trust a prior boolean or a high confidence score: it must
        re-read the server-owned text and prove every stored literal excerpt
        again.  If that reader is not wired in production, promotion is
        blocked rather than producing a formal ledger candidate.
        """

        if self._source_reader is None:
            raise CaseLedgerExtractionStagingBlocked(
                "formal promotion requires a server-owned evidence page text reader"
            )
        task_reader = getattr(self._source_reader, "read_task_evidence_page_texts", None)
        if callable(task_reader):
            raw_refs = task_binding.get("task_input_refs")
            if not isinstance(raw_refs, list) or not all(isinstance(ref, str) for ref in raw_refs):
                raise CaseLedgerExtractionStagingBlocked(
                    "formal promotion task source refs are invalid"
                )
            try:
                projected = task_reader(
                    run_id=str(task_binding["run_id"]), task_id=str(task_binding["task_id"]),
                    task_input_hash=str(task_binding["task_input_hash"]),
                    input_refs=tuple(raw_refs),
                )
            except Exception as error:
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction source text cannot be re-read for formal promotion"
                ) from error
            if not isinstance(projected, dict):
                raise CaseLedgerExtractionStagingBlocked(
                    "formal promotion source projection is invalid"
                )
            for item in candidates:
                candidate_id = str(item["extraction_candidate_id"])
                payload = item["candidate_payload"]
                links = source_links[candidate_id]
                hashes = source_text_hashes.get(candidate_id, {})
                if not isinstance(payload, dict) or not isinstance(payload.get("supporting_excerpts"), list):
                    raise CaseLedgerExtractionStagingBlocked("staged extraction payload is invalid")
                excerpts = {
                    str(value.get("evidence_page_id")): str(value.get("text"))
                    for value in payload["supporting_excerpts"]
                    if isinstance(value, dict)
                    and isinstance(value.get("evidence_page_id"), str)
                    and isinstance(value.get("text"), str)
                }
                if len(excerpts) != len(payload["supporting_excerpts"]):
                    raise CaseLedgerExtractionStagingBlocked("staged extraction supporting excerpts are invalid")
                for link in links:
                    page_id = str(link.evidence_id)
                    value = projected.get(page_id)
                    if (
                        not isinstance(value, tuple) or len(value) != 4
                        or value[0] != link.original_file_sha256 or value[2] != link.page_number
                        or value[3] != hashes.get(page_id) or excerpts.get(page_id) not in value[1]
                    ):
                        raise CaseLedgerExtractionStagingBlocked(
                            "staged extraction source content cannot be proven for formal promotion"
                        )
            return
        for item in candidates:
            candidate_id = str(item["extraction_candidate_id"])
            payload = item["candidate_payload"]
            links = source_links[candidate_id]
            hashes = source_text_hashes.get(candidate_id, {})
            if not isinstance(payload, dict):
                raise CaseLedgerExtractionStagingBlocked("staged extraction payload is invalid")
            raw_excerpts = payload.get("supporting_excerpts")
            if not isinstance(raw_excerpts, list):
                raise CaseLedgerExtractionStagingBlocked(
                    "staged extraction supporting excerpts are invalid"
                )
            excerpts_by_page: dict[str, list[str]] = {}
            for excerpt in raw_excerpts:
                if (
                    not isinstance(excerpt, dict)
                    or not isinstance(excerpt.get("evidence_page_id"), str)
                    or not isinstance(excerpt.get("text"), str)
                    or not excerpt["text"].strip()
                    or excerpt["text"] != excerpt["text"].strip()
                    or len(excerpt["text"]) > 2_000
                ):
                    raise CaseLedgerExtractionStagingBlocked(
                        "staged extraction supporting excerpt is invalid"
                    )
                excerpts_by_page.setdefault(excerpt["evidence_page_id"], []).append(excerpt["text"])
            link_ids = {str(link.evidence_id) for link in links}
            if set(excerpts_by_page) != link_ids or set(hashes) != link_ids:
                raise CaseLedgerExtractionStagingBlocked(
                    "staged extraction excerpts or text hashes no longer match source pages"
                )
            for link in links:
                page_id = str(link.evidence_id)
                try:
                    source_text = self._source_reader.read_evidence_page_text(
                        firm_id=actor.firm_id, matter_id=matter_id,
                        evidence_page_id=page_id,
                        source_file_sha256=link.original_file_sha256,
                        page_number=link.page_number,
                    )
                except Exception as error:
                    raise CaseLedgerExtractionStagingBlocked(
                        "extraction source text cannot be re-read for formal promotion"
                    ) from error
                if (
                    not isinstance(source_text, str)
                    or sha256(source_text.encode("utf-8")).hexdigest() != hashes[page_id]
                    or any(excerpt not in source_text for excerpt in excerpts_by_page[page_id])
                ):
                    raise CaseLedgerExtractionStagingBlocked(
                        "staged extraction source content cannot be proven for formal promotion"
                    )


def _fact_text_from_staged_payload(value: object) -> str:
    if not isinstance(value, dict) or value.get("kind") != "FACT":
        raise CaseLedgerExtractionStagingBlocked("staged fact payload is invalid")
    text = value.get("fact_text")
    if not isinstance(text, str) or not text.strip() or text != text.strip() or len(text) > 2_000:
        raise CaseLedgerExtractionStagingBlocked("staged fact text is invalid")
    return text


def _transaction_from_staged_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("kind") != "TRANSACTION":
        raise CaseLedgerExtractionStagingBlocked("staged transaction payload is invalid")
    try:
        precision = DatePrecision(value["date_precision"])
        local_date = date.fromisoformat(value["local_date"]) if value.get("local_date") else None
        amount = Decimal(value["amount"])
        currency = value["currency"]
        direction = TransactionDirection(value["direction"])
        channel = TransactionChannel(value["channel"])
    except (KeyError, TypeError, ValueError) as error:
        raise CaseLedgerExtractionStagingBlocked("staged transaction fields are invalid") from error
    if (
        not amount.is_finite() or amount <= 0 or not isinstance(currency, str)
        or len(currency) != 3 or not currency.isalpha()
        or (precision is DatePrecision.EXACT_DATE and local_date is None)
        or (precision is not DatePrecision.EXACT_DATE and local_date is not None)
    ):
        raise CaseLedgerExtractionStagingBlocked("staged transaction fields are invalid")
    return {
        "local_date": local_date, "date_precision": precision.value, "amount": amount,
        "currency": currency, "direction": direction.value,
        "payer_label": _optional_staged_text(value.get("payer_label")),
        "payee_label": _optional_staged_text(value.get("payee_label")),
        "channel": channel.value,
        "transaction_reference": _optional_staged_text(value.get("transaction_reference")),
    }


def _optional_staged_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 500:
        raise CaseLedgerExtractionStagingBlocked("staged transaction optional text is invalid")
    return value


def _lineage_binds_extraction_artifact(
    lineage: object,
    *,
    artifact_id: str,
    task_id: str,
    content_hash: str,
) -> bool:
    return isinstance(lineage, list) and any(
        isinstance(item, dict)
        and str(item.get("artifact_id")) == artifact_id
        and item.get("artifact_kind") == CASE_LEDGER_EXTRACTION_ARTIFACT_KIND
        and item.get("content_hash") == content_hash
        and str(item.get("task_id")) == task_id
        for item in lineage
    )


def _candidate_review_reason_codes(
    *,
    artifact: dict[str, object],
    candidate: dict[str, object],
    source_text_verified: bool,
    ledger_clear: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    source_modes = {
        str(page["evidence_page_id"]): str(page["source_mode"])
        for page in artifact["_source_pages"]  # parsed private projection
    }
    page_ids = candidate.get("evidence_page_ids")
    if not isinstance(page_ids, list) or any(
        not isinstance(page_id, str) for page_id in page_ids
    ):
        raise CaseLedgerExtractionStagingBlocked(
            "extraction candidate source pages are invalid"
        )
    if any(source_modes.get(page_id) != "NATIVE_TEXT" for page_id in page_ids):
        reasons.append("NON_NATIVE_SOURCE")
    confidence = candidate.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or float(confidence) < AUTO_STAGE_CONFIDENCE_MINIMUM
    ):
        reasons.append("BELOW_BULK_CONFIDENCE_THRESHOLD")
    for field in ("conflict_codes", "risk_codes"):
        values = candidate.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or value not in _REVIEW_REASON_CODES
            for value in values
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "extraction candidate review reasons are invalid"
            )
        reasons.extend(values)
    if not source_text_verified:
        reasons.append("SOURCE_TEXT_NOT_REVERIFIED")
    intrinsic_eligible = extraction_candidate_is_eligible(artifact, candidate)
    if source_text_verified and intrinsic_eligible and not ledger_clear:
        reasons.append("CURRENT_LEDGER_CONFLICT_OR_DUPLICATE")
    should_be_eligible = source_text_verified and intrinsic_eligible and ledger_clear
    if bool(reasons) == should_be_eligible:
        raise CaseLedgerExtractionStagingBlocked(
            "extraction eligibility and review reasons disagree"
        )
    return tuple(code for code in _REVIEW_REASON_CODES if code in reasons)


def _staged_transaction_conflicts_with_ledger(
    staged: dict[str, object],
    *,
    candidate: dict[str, object],
    current: Any,
) -> bool:
    """Conservative exact-key/reference/source-page collision screen."""

    staged_reference = staged["transaction_reference"]
    current_reference = current["transaction_reference"]
    if (
        isinstance(staged_reference, str)
        and isinstance(current_reference, str)
        and staged_reference.casefold() == current_reference.casefold()
    ):
        return True
    try:
        current_amount = Decimal(current["amount"])
    except (InvalidOperation, TypeError, ValueError):
        return True
    staged_key = (
        staged["local_date"],
        staged["date_precision"],
        staged["amount"],
        staged["currency"],
        staged["direction"],
        staged["channel"],
    )
    current_key = (
        current["local_date"],
        str(current["date_precision"]),
        current_amount,
        str(current["currency"]),
        str(current["direction"]),
        str(current["channel"]),
    )
    if staged_key == current_key:
        return True
    raw_candidate_pages = candidate.get("evidence_page_ids")
    raw_links = current["evidence_links"]
    if not isinstance(raw_candidate_pages, list) or not isinstance(raw_links, list):
        return True
    candidate_pages = {
        str(page_id) for page_id in raw_candidate_pages if isinstance(page_id, str)
    }
    current_pages = {
        str(link.get("evidence_id"))
        for link in raw_links
        if isinstance(link, dict) and isinstance(link.get("evidence_id"), str)
    }
    return bool(candidate_pages.intersection(current_pages))


def _ledger_clear_candidate_hashes(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    candidates: list[dict[str, object]],
) -> set[str]:
    if not candidates:
        return set()
    fact_rows = connection.execute(
        """
        SELECT original_text, evidence_links
        FROM case_facts
        WHERE firm_id = %s AND matter_id = %s AND status <> 'INVALIDATED'
        FOR KEY SHARE
        """,
        (firm_id, matter_id),
    ).fetchall()
    transaction_rows = connection.execute(
        """
        SELECT local_date, date_precision, amount, currency, direction,
               payer_label, payee_label, channel, transaction_reference,
               evidence_links
        FROM case_transactions
        WHERE firm_id = %s AND matter_id = %s AND status <> 'INVALIDATED'
        FOR KEY SHARE
        """,
        (firm_id, matter_id),
    ).fetchall()
    clear: set[str] = set()
    for candidate in candidates:
        candidate_hash = candidate.get("candidate_hash")
        if not isinstance(candidate_hash, str):
            raise CaseLedgerExtractionStagingBlocked(
                "extraction candidate hash is invalid"
            )
        if candidate.get("kind") == "FACT":
            fact_text = candidate.get("fact_text")
            raw_candidate_pages = candidate.get("evidence_page_ids")
            if not isinstance(raw_candidate_pages, list) or any(
                not isinstance(page_id, str) for page_id in raw_candidate_pages
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "extraction fact evidence pages are invalid"
                )
            candidate_pages = set(raw_candidate_pages)
            if any(
                str(row["original_text"]) == fact_text
                or bool(
                    candidate_pages.intersection(
                        _ledger_evidence_ids(row["evidence_links"])
                    )
                )
                for row in fact_rows
            ):
                continue
        elif candidate.get("kind") == "TRANSACTION":
            transaction = _transaction_from_staged_payload(candidate)
            if any(
                _staged_transaction_conflicts_with_ledger(
                    transaction,
                    candidate=candidate,
                    current=row,
                )
                for row in transaction_rows
            ):
                continue
        else:
            raise CaseLedgerExtractionStagingBlocked(
                "extraction candidate kind is invalid"
            )
        clear.add(candidate_hash)
    return clear


def _ledger_evidence_ids(value: object) -> set[str]:
    if not isinstance(value, list):
        raise CaseLedgerExtractionStagingBlocked(
            "current ledger evidence links are invalid"
        )
    result = {
        str(item.get("evidence_id"))
        for item in value
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    if len(result) != len(value):
        raise CaseLedgerExtractionStagingBlocked(
            "current ledger evidence links are invalid"
        )
    return result


def _ledger_confirmation_source_binding_hash(
    *,
    extraction_batch_id: str,
    batch: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_text_hashes: dict[str, dict[str, str]],
) -> str:
    """Bind one fresh source re-read to the immutable database candidate set.

    This intentionally uses a small delimiter-safe encoding that PostgreSQL
    0048 can reproduce byte-for-byte.  Every variable component is already a
    UUID or lowercase SHA-256 value, so none can contain ``|``, ``,`` or
    ``:``.  The digest is server-internal and is never sent to the browser.
    """

    _validate_uuid("extraction_batch_id", extraction_batch_id)
    run_id = str(batch.get("run_id", ""))
    task_id = str(batch.get("task_id", ""))
    task_input_hash = str(batch.get("task_input_hash", ""))
    _validate_uuid("source verification run_id", run_id)
    _validate_uuid("source verification task_id", task_id)
    if (
        len(task_input_hash) != 64
        or any(character not in "0123456789abcdef" for character in task_input_hash)
    ):
        raise CaseLedgerExtractionStagingBlocked(
            "source verification task input hash is invalid"
        )
    if not candidates:
        raise CaseLedgerExtractionStagingBlocked(
            "source verification requires an eligible candidate set"
        )
    candidate_hash_by_id: dict[str, str] = {}
    for item in candidates:
        candidate_id = str(item.get("extraction_candidate_id", ""))
        candidate_hash = str(item.get("candidate_hash", ""))
        _validate_uuid("source verification candidate_id", candidate_id)
        if (
            candidate_id in candidate_hash_by_id
            or len(candidate_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in candidate_hash
            )
        ):
            raise CaseLedgerExtractionStagingBlocked(
                "source verification candidate binding is invalid"
            )
        candidate_hash_by_id[candidate_id] = candidate_hash
    if set(source_text_hashes) != set(candidate_hash_by_id):
        raise CaseLedgerExtractionStagingBlocked(
            "source verification pages differ from the eligible candidate set"
        )
    page_components: list[str] = []
    for candidate_id, page_hashes in source_text_hashes.items():
        if not isinstance(page_hashes, dict) or not page_hashes:
            raise CaseLedgerExtractionStagingBlocked(
                "source verification page binding is invalid"
            )
        for page_id, text_hash in page_hashes.items():
            _validate_uuid("source verification evidence_page_id", page_id)
            if (
                len(text_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in text_hash
                )
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "source verification page text hash is invalid"
                )
            page_components.append(
                f"{candidate_hash_by_id[candidate_id]}:{page_id}:{text_hash}"
            )
    raw = "|".join(
        (
            "case-agent-ledger-source-verification-v1",
            extraction_batch_id,
            run_id,
            task_id,
            task_input_hash,
            ",".join(sorted(candidate_hash_by_id.values())),
            ",".join(sorted(page_components)),
        )
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _json_object_result(row: object, field: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise CaseLedgerExtractionStagingBlocked(
            "database ledger confirmation result is missing"
        )
    value = row.get(field)
    if not isinstance(value, dict):
        raise CaseLedgerExtractionStagingBlocked(
            "database ledger confirmation result is invalid"
        )
    return value


def _case_ledger_command_receipt(
    value: object,
    *,
    command_name: str,
    idempotency_key: str,
    matter_id: str,
    extraction_batch_id: str,
) -> CaseLedgerCommandReceipt:
    if not isinstance(value, dict):
        raise CaseLedgerExtractionStagingBlocked(
            "database ledger confirmation receipt is invalid"
        )
    expected = {
        "command_name": command_name,
        "idempotency_key": idempotency_key,
        "matter_id": matter_id,
        "object_type": "CASE_LEDGER_EXTRACTION_BATCH",
        "object_id": extraction_batch_id,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise CaseLedgerExtractionStagingBlocked(
            "database ledger confirmation receipt differs from the request"
        )
    matter_version = value.get("matter_version")
    audit_event_id = str(value.get("audit_event_id", ""))
    if (
        isinstance(matter_version, bool)
        or not isinstance(matter_version, int)
        or matter_version < 1
    ):
        raise CaseLedgerExtractionStagingBlocked(
            "database ledger confirmation receipt version is invalid"
        )
    _validate_uuid("ledger confirmation audit_event_id", audit_event_id)
    return CaseLedgerCommandReceipt(
        command_name=command_name,
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=matter_version,
        audit_event_id=audit_event_id,
        object_type="CASE_LEDGER_EXTRACTION_BATCH",
        object_id=extraction_batch_id,
    )


def _evidence_payload(links: tuple[EvidenceLink, ...]) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": link.evidence_id,
            "original_file_sha256": link.original_file_sha256,
            "page_number": link.page_number,
            "region_id": link.region_id,
            "original_label": link.original_label,
        }
        for link in links
    ]


class _TenantTransaction:
    def __init__(self, dsn: str, firm_id: str) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._context: Any = None

    def __enter__(self):
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        connection = self._context.__enter__()
        connection.execute("SELECT set_config('app.firm_id', %s, true)", (self._firm_id,))
        return connection

    def __exit__(self, exc_type, exc, traceback):
        return self._context.__exit__(exc_type, exc, traceback)


def preflight_case_agent_ledger_extraction_staging_runtime_contract(
    *, dsn: str, worker_actor: Actor
) -> None:
    """Prove the 0042 lawyer-review landing path before advertising the Skill.

    The private projection must retain source=staged matter version, publish a
    separate append-only private event and preserve the composite candidate /
    batch plus deferred complete low-risk confirmation invariants.
    """

    if (
        not isinstance(dsn, str)
        or not dsn.strip()
        or not isinstance(worker_actor, Actor)
        or worker_actor.roles != frozenset({Role.SYSTEM_WORKER})
    ):
        raise CaseLedgerExtractionStagingBlocked(
            "ledger extraction staging preflight identity is invalid"
        )
    tables = {
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_staging_events",
        "case_agent_ledger_extraction_candidates",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_extraction_promotions",
        "case_agent_ledger_extraction_batch_confirmations",
    }
    append_only = {
        "case_agent_ledger_extraction_batches_append_only",
        "case_agent_ledger_extraction_staging_events_append_only",
        "case_agent_ledger_extraction_candidates_append_only",
        "case_agent_ledger_extraction_candidate_pages_append_only",
        "case_agent_ledger_extraction_promotions_append_only",
        "case_agent_ledger_extraction_batch_confirmations_append_only",
    }
    required_triggers = append_only | {
        "case_agent_ledger_extraction_promotions_target_integrity",
        "case_agent_ledger_extraction_confirmed_target_integrity",
        "case_agent_ledger_extraction_batch_confirmation_complete",
    }
    required_columns = {
        "case_agent_ledger_extraction_batches": {
            "extraction_batch_id", "run_id", "graph_id", "task_id",
            "artifact_id", "verification_receipt_id", "firm_id", "matter_id",
            "source_matter_version", "staged_matter_version",
            "candidate_count", "eligible_candidate_count",
        },
        "case_agent_ledger_extraction_staging_events": {
            "staging_event_id", "extraction_batch_id", "artifact_id",
            "run_id", "firm_id", "matter_id", "event_type",
            "source_matter_version", "staged_matter_version", "actor_id",
            "idempotency_key", "request_hash", "payload",
        },
        "case_agent_ledger_extraction_candidates": {
            "extraction_candidate_id", "extraction_batch_id", "firm_id",
            "matter_id", "candidate_hash", "candidate_kind", "review_lane",
            "eligible_for_bulk_promotion", "review_reason_codes",
            "review_status", "candidate_payload",
        },
        "case_agent_ledger_extraction_candidate_pages": {
            "extraction_candidate_id", "evidence_page_id", "firm_id",
            "matter_id", "source_text_sha256",
        },
        "case_agent_ledger_extraction_promotions": {
            "extraction_batch_id", "extraction_candidate_id", "firm_id",
            "matter_id", "target_object_type", "target_object_id",
            "promoted_matter_version", "lawyer_batch_decision_hash", "promoted_by",
        },
        "case_agent_ledger_extraction_batch_confirmations": {
            "extraction_batch_id", "firm_id", "matter_id",
            "confirmed_candidate_count", "lawyer_batch_decision_hash",
            "confirmed_matter_version", "confirmed_by",
        },
    }
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (worker_actor.firm_id,),
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)",
                (worker_actor.actor_id,),
            )
            rows = connection.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(tables),),
            ).fetchall()
            observed: dict[str, set[str]] = {}
            for row in rows:
                observed.setdefault(str(row["table_name"]), set()).add(
                    str(row["column_name"])
                )
            if any(
                not columns.issubset(observed.get(table, set()))
                for table, columns in required_columns.items()
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction staging migration 0042 is incomplete"
                )
            constraints = connection.execute(
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
                   AND constraint_row.contype IN ('f', 'c')
                """,
                (list(tables),),
            ).fetchall()
            definitions = {
                (str(row["table_name"]), str(row["definition"]))
                for row in constraints
            }
            candidate_batch_fk = any(
                table == "case_agent_ledger_extraction_candidates"
                and "FOREIGN KEY (extraction_batch_id, firm_id, matter_id)" in definition
                and "REFERENCES case_agent_ledger_extraction_batches" in definition
                for table, definition in definitions
            )
            promotion_candidate_batch_fk = any(
                table == "case_agent_ledger_extraction_promotions"
                and "FOREIGN KEY (extraction_candidate_id, extraction_batch_id, firm_id, matter_id)" in definition
                and "REFERENCES case_agent_ledger_extraction_candidates" in definition
                for table, definition in definitions
            )
            if not candidate_batch_fk or not promotion_candidate_batch_fk:
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction candidate/batch composite FK is absent"
                )
            private_version_constraint = any(
                table == "case_agent_ledger_extraction_batches"
                and "staged_matter_version = source_matter_version" in definition
                for table, definition in definitions
            )
            private_event_batch_fk = any(
                table == "case_agent_ledger_extraction_staging_events"
                and "FOREIGN KEY (extraction_batch_id, artifact_id, run_id, firm_id, matter_id)"
                    in definition
                and "REFERENCES case_agent_ledger_extraction_batches" in definition
                for table, definition in definitions
            )
            if not private_version_constraint or not private_event_batch_fk:
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction private staging version contract is absent"
                )
            triggers = connection.execute(
                """
                SELECT trigger_row.tgname AS trigger_name,
                       trigger_row.tgdeferrable,
                       trigger_row.tginitdeferred
                  FROM pg_catalog.pg_trigger trigger_row
                  JOIN pg_catalog.pg_class relation
                    ON relation.oid = trigger_row.tgrelid
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = 'public'
                   AND trigger_row.tgname = ANY(%s)
                   AND NOT trigger_row.tgisinternal
                """,
                (list(required_triggers),),
            ).fetchall()
            trigger_map = {
                str(row["trigger_name"]): (
                    bool(row["tgdeferrable"]),
                    bool(row["tginitdeferred"]),
                )
                for row in triggers
            }
            if not required_triggers.issubset(trigger_map):
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction staging guards are incomplete"
                )
            if any(
                trigger_map[trigger_name] != (True, True)
                for trigger_name in (
                    "case_agent_ledger_extraction_confirmed_target_integrity",
                    "case_agent_ledger_extraction_batch_confirmation_complete",
                )
            ):
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction confirmation guards are not deferred"
                )
            rls_rows = connection.execute(
                """
                SELECT relation.relname, relation.relrowsecurity,
                       relation.relforcerowsecurity
                  FROM pg_catalog.pg_class relation
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                """,
                (list(tables),),
            ).fetchall()
            rls = {
                str(row["relname"]): (
                    bool(row["relrowsecurity"]),
                    bool(row["relforcerowsecurity"]),
                )
                for row in rls_rows
            }
            if any(rls.get(table) != (True, True) for table in tables):
                raise CaseLedgerExtractionStagingBlocked(
                    "ledger extraction staging requires FORCE RLS"
                )
    except CaseLedgerExtractionStagingBlocked:
        raise
    except Exception as error:
        raise CaseLedgerExtractionStagingBlocked(
            "ledger extraction staging preflight failed"
        ) from error


__all__ = (
    "CaseLedgerExtractionStagingBlocked",
    "PostgresCaseLedgerExtractionPromotionStore",
    "PostgresCaseLedgerExtractionStagingStore",
    "StagedCaseLedgerExtractionBatch",
    "preflight_case_agent_ledger_extraction_staging_runtime_contract",
)
