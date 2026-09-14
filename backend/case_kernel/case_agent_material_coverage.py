"""Read-only page processing coverage, separate from evidentiary approval.

Call inside the caller's authorized same-matter snapshot transaction. A page
counts only when it was an input of a successful extraction task whose graph
passed independent verification and whose run is current. Candidate count is
deliberately irrelevant: a correctly read page may produce no facts. A
cancelled run normally remains audit history and cannot suppress a new
material-reading round. The narrow exception is a batch that the lawyer has
already confirmed: that immutable decision remains usable case work even when
the obsolete enclosing plan is later cancelled. Merely uploading, reading a
PDF, approving one fact, or seeing a model request is not extraction coverage.
"""
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class MaterialExtractionCoverage:
    evidence_file_id: str
    original_sha256: str
    page_ids: tuple[str, ...]
    extracted_page_ids: tuple[str, ...]

    @property
    def pending_page_ids(self) -> tuple[str, ...]:
        covered = frozenset(self.extracted_page_ids)
        return tuple(page for page in self.page_ids if page not in covered)


def read_material_extraction_coverage(connection: Any, *, firm_id: str,
                                      matter_id: str) -> tuple[MaterialExtractionCoverage, ...]:
    UUID(firm_id)
    UUID(matter_id)
    rows = connection.execute("""
        SELECT original.evidence_file_id::text, original.original_file_sha256,
               page.evidence_page_id::text, page.page_number,
               EXISTS (
                   SELECT 1 FROM case_agent_tasks task
                   JOIN case_agent_task_graphs graph
                     ON graph.graph_id=task.graph_id AND graph.run_id=task.run_id
                    AND graph.firm_id=task.firm_id AND graph.matter_id=task.matter_id
                   JOIN case_agent_runs run
                     ON run.run_id=task.run_id AND run.firm_id=task.firm_id
                    AND run.matter_id=task.matter_id
                   JOIN case_agent_task_attempts attempt
                     ON attempt.task_id=task.task_id AND attempt.graph_id=task.graph_id
                    AND attempt.run_id=task.run_id AND attempt.firm_id=task.firm_id
                    AND attempt.matter_id=task.matter_id AND attempt.status='SUCCEEDED'
                   JOIN case_agent_verification_receipts verification
                     ON verification.run_id=task.run_id AND verification.firm_id=task.firm_id
                    AND verification.matter_id=task.matter_id
                    AND verification.graph_hash=graph.graph_hash AND verification.outcome='PASSED'
                    WHERE task.firm_id=page.firm_id AND task.matter_id=page.matter_id
                     AND task.tool_id='extract_case_ledger'
                     AND task.input_refs ? ('evidence-page:' || page.evidence_page_id::text)
                     AND run.is_stale = false
                     AND (
                         run.is_cancelled = false
                         OR EXISTS (
                             SELECT 1
                               FROM case_agent_ledger_extraction_batches batch
                               JOIN case_agent_ledger_extraction_batch_confirmations confirmation
                                 ON confirmation.extraction_batch_id = batch.extraction_batch_id
                                AND confirmation.firm_id = batch.firm_id
                                AND confirmation.matter_id = batch.matter_id
                              WHERE batch.run_id = task.run_id
                                AND batch.graph_id = task.graph_id
                                AND batch.task_id = task.task_id
                                AND batch.firm_id = task.firm_id
                                AND batch.matter_id = task.matter_id
                         )
                     )
               ) AS extracted
        FROM evidence_original_files original
        JOIN evidence_pages page ON page.evidence_file_id=original.evidence_file_id
          AND page.firm_id=original.firm_id AND page.matter_id=original.matter_id
        WHERE original.firm_id=%s AND original.matter_id=%s
        ORDER BY original.evidence_file_id, page.page_number, page.evidence_page_id
        """, (firm_id, matter_id)).fetchall()
    groups: dict[str, tuple[str, list[str], list[str]]] = {}
    for row in rows:
        file_id = str(row["evidence_file_id"])
        digest = str(row["original_file_sha256"])
        page_id = str(row["evidence_page_id"])
        UUID(file_id)
        UUID(page_id)
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("coverage source digest is invalid")
        group = groups.setdefault(file_id, (digest, [], []))
        if group[0] != digest or page_id in group[1] or type(row["extracted"]) is not bool:
            raise ValueError("coverage rows are inconsistent")
        group[1].append(page_id)
        if row["extracted"]:
            group[2].append(page_id)
    return tuple(MaterialExtractionCoverage(key, digest, tuple(pages), tuple(covered))
        for key, (digest, pages, covered) in groups.items())
