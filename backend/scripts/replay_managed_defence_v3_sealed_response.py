#!/usr/bin/env python3
"""Read-only replay of the one sealed managed-defence v3 provider response.

This command is deliberately narrower than the managed acceptance runner. It
never creates a run, claims a task, sends a provider request, writes an audit
event, stages a candidate, or persists a document. It only authenticates the
existing private response archive, reconstructs the exact frozen server
projection, and asks the current deterministic interpreter whether that
already-recorded response can form a review-only candidate.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Iterator, Mapping

import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_api.case_agent_worker_entrypoint import (  # noqa: E402
    CaseAgentWorkerProcessSettings,
)
from case_kernel.case_agent_case_context_postgres import (  # noqa: E402
    PostgresCaseContextProjectionPort,
)
from case_kernel.case_agent_lawyer_analysis import (  # noqa: E402
    build_lawyer_analysis_contract,
    compile_lawyer_decision_package_candidate,
    parse_lawyer_analysis_provider_response,
    parse_lawyer_decision_package_candidate,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (  # noqa: E402
    LAWYER_ANALYSIS_TOOL_ID,
)
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore  # noqa: E402


_SEALED_RUN_ID = "5b842fe3-1c09-5874-8712-07955ddaf642"
_SEALED_MATTER_ID = "d05c77af-82f2-5139-9782-116e15a248b7"
_SEALED_EXTERNAL_REQUEST_ID = "1174303a-b36e-564e-9296-ed958df8326f"
_SEALED_LABEL = "v3"


class SealedReplayBlocked(RuntimeError):
    """The fixed archival boundary differs; no replay is attempted."""


@dataclass(frozen=True)
class SealedResponseReplay:
    """Authenticated, in-memory result of the no-write sealed replay.

    This is deliberately private to the maintenance scripts.  It contains no
    browser projection and cannot stage, promote, verify, render or submit
    anything by itself.  The M2 recovery command passes it only to the narrow
    recovery port, which independently authenticates its archive again.
    """

    settings: CaseAgentWorkerProcessSettings = field(repr=False, compare=False)
    binding: Mapping[str, object]
    stored: object = field(repr=False, compare=False)
    archive_receipt: Mapping[str, object] = field(repr=False, compare=False)
    candidate_payload: bytes = field(repr=False, compare=False)
    candidate: Mapping[str, object]
    parsed: object = field(repr=False, compare=False)
    before: Mapping[str, object]


@contextmanager
def _read_only_connection(
    *, dsn: str, actor_id: str, firm_id: str
) -> Iterator[psycopg.Connection[dict[str, object]]]:
    connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    try:
        connection.execute("BEGIN READ ONLY")
        connection.execute(
            "SELECT pg_catalog.set_config('app.actor_id', %s, true)", (actor_id,)
        )
        connection.execute(
            "SELECT pg_catalog.set_config('app.firm_id', %s, true)", (firm_id,)
        )
        yield connection
    finally:
        try:
            connection.execute("ROLLBACK")
        finally:
            connection.close()


def _sealed_binding(
    *, settings: CaseAgentWorkerProcessSettings
) -> Mapping[str, object]:
    worker = settings.runtime.actor
    with _read_only_connection(
        dsn=settings.runtime.postgres_dsn,
        actor_id=worker.actor_id,
        firm_id=worker.firm_id,
    ) as connection:
        rows = connection.execute(
            """
            SELECT run.run_id::text AS run_id,
                   run.firm_id::text AS firm_id,
                   run.matter_id::text AS matter_id,
                   run.status AS run_status,
                   run.current_event_version AS event_version,
                   run.snapshot_hash,
                   task.task_id::text AS task_id,
                   task.input_hash,
                   task.input_refs,
                   submission.external_request_id::text AS external_request_id,
                   submission.request_hash,
                   submission.recorded_by::text AS recorded_by,
                   (
                       SELECT COUNT(*)
                         FROM case_agent_external_submissions counted
                        WHERE counted.run_id = run.run_id
                          AND counted.firm_id = run.firm_id
                          AND counted.matter_id = run.matter_id
                   ) AS external_submission_count
              FROM case_agent_runs run
              JOIN case_agent_tasks task
                ON task.run_id = run.run_id
               AND task.graph_id = run.current_graph_id
               AND task.firm_id = run.firm_id
               AND task.matter_id = run.matter_id
              JOIN case_agent_external_submissions submission
                ON submission.run_id = task.run_id
               AND submission.task_id = task.task_id
               AND submission.firm_id = task.firm_id
               AND submission.matter_id = task.matter_id
             WHERE run.run_id = %s::uuid
               AND run.firm_id = %s::uuid
               AND run.matter_id = %s::uuid
               AND task.tool_id = %s
             ORDER BY task.sequence
            """,
            (_SEALED_RUN_ID, worker.firm_id, _SEALED_MATTER_ID, LAWYER_ANALYSIS_TOOL_ID),
        ).fetchall()
    if len(rows) != 1:
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 的受控分析绑定不唯一。")
    row = dict(rows[0])
    if (
        row.get("run_id") != _SEALED_RUN_ID
        or row.get("matter_id") != _SEALED_MATTER_ID
        or row.get("run_status") != "WAITING_INPUT"
        or row.get("event_version") != 8
        or row.get("external_request_id") != _SEALED_EXTERNAL_REQUEST_ID
        or row.get("external_submission_count") != 1
        or row.get("recorded_by") != worker.actor_id
    ):
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 的不可变运行边界已经变化。")
    if (
        not isinstance(row.get("input_hash"), str)
        or len(str(row["input_hash"])) != 64
        or not isinstance(row.get("request_hash"), str)
        or len(str(row["request_hash"])) != 64
        or not isinstance(row.get("snapshot_hash"), str)
        or len(str(row["snapshot_hash"])) != 64
        or not isinstance(row.get("input_refs"), list)
        or not row["input_refs"]
        or not all(isinstance(value, str) for value in row["input_refs"])
    ):
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 的任务输入绑定无效。")
    return row


def _run_snapshot(
    *, settings: CaseAgentWorkerProcessSettings
) -> Mapping[str, object]:
    worker = settings.runtime.actor
    with _read_only_connection(
        dsn=settings.runtime.postgres_dsn,
        actor_id=worker.actor_id,
        firm_id=worker.firm_id,
    ) as connection:
        row = connection.execute(
            """
            SELECT run.status AS run_status,
                   run.current_event_version AS event_version,
                   (
                       SELECT COUNT(*)
                         FROM case_agent_external_submissions submission
                        WHERE submission.run_id = run.run_id
                          AND submission.firm_id = run.firm_id
                          AND submission.matter_id = run.matter_id
                   ) AS external_submission_count
              FROM case_agent_runs run
             WHERE run.run_id = %s::uuid
               AND run.firm_id = %s::uuid
               AND run.matter_id = %s::uuid
            """,
            (_SEALED_RUN_ID, worker.firm_id, _SEALED_MATTER_ID),
        ).fetchone()
    if row is None:
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 运行不存在。")
    return dict(row)


def _load_sealed_replay() -> SealedResponseReplay:
    """Authenticate and deterministically interpret the fixed archive only."""

    settings = CaseAgentWorkerProcessSettings.from_environment(dict(os.environ))
    if settings.runtime.actor.firm_id is None:
        raise SealedReplayBlocked("受管 Worker 身份不可用。")
    before = _run_snapshot(settings=settings)
    binding = _sealed_binding(settings=settings)
    object_store = S3CompatiblePrivateObjectStore(settings.object_store)
    recovered = object_store.recover_case_agent_lawyer_analysis_response(
        firm_id=str(binding["firm_id"]),
        matter_id=str(binding["matter_id"]),
        external_request_id=str(binding["external_request_id"]),
        request_hash=str(binding["request_hash"]),
    )
    if recovered is None:
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 的私有模型响应归档不存在。")
    stored, response, receipt = recovered
    if sha256(response).hexdigest() != stored.response_sha256:
        raise SealedReplayBlocked(f"封存 {_SEALED_LABEL} 的响应哈希不一致。")

    projection_port = PostgresCaseContextProjectionPort(
        dsn=settings.runtime.postgres_dsn,
        worker_actor=settings.runtime.actor,
        required_tool_id=LAWYER_ANALYSIS_TOOL_ID,
    )
    projection = projection_port.project_case_context(
        run_id=str(binding["run_id"]),
        task_id=str(binding["task_id"]),
        task_input_hash=str(binding["input_hash"]),
        input_refs=tuple(str(value) for value in binding["input_refs"]),
    )
    contract = build_lawyer_analysis_contract(projection)
    parsed = parse_lawyer_analysis_provider_response(response, contract=contract)
    candidate_payload = compile_lawyer_decision_package_candidate(
        projection=projection,
        contract=contract,
        parsed=parsed,
        external_request_id=str(binding["external_request_id"]),
        request_hash=str(binding["request_hash"]),
    )
    candidate = parse_lawyer_decision_package_candidate(candidate_payload)
    after = _run_snapshot(settings=settings)
    if before != after:
        raise SealedReplayBlocked("只读回放改变了封存运行状态。")
    return SealedResponseReplay(
        settings=settings,
        binding=binding,
        stored=stored,
        archive_receipt=receipt,
        candidate_payload=candidate_payload,
        candidate=candidate,
        parsed=parsed,
        before=before,
    )


def _replay() -> Mapping[str, object]:
    replay = _load_sealed_replay()
    stored = replay.stored
    parsed = replay.parsed
    candidate = replay.candidate
    return {
        "mode": f"READ_ONLY_{_SEALED_LABEL.upper()}_SEALED_RESPONSE_REPLAY",
        "provider_requests_sent": 0,
        "persistent_writes": 0,
        "sealed_run": {
            "run_id": _SEALED_RUN_ID,
            "run_status": replay.before["run_status"],
            "event_version": replay.before["event_version"],
            "external_submission_count": replay.before["external_submission_count"],
            "unchanged_after_replay": True,
        },
        "archive": {
            "response_sha256": getattr(stored, "response_sha256"),
            "response_bytes": getattr(stored, "response_bytes"),
            "archive_sha256": getattr(stored, "archive_sha256"),
        },
        "interpreter": {
            "candidate_schema": candidate["schema_version"],
            "candidate_sha256": sha256(replay.candidate_payload).hexdigest(),
            "candidate_bytes": len(replay.candidate_payload),
            "numeric_fact_binding_count": len(candidate["numeric_fact_bindings"]),
            "model_output_normalization": {
                "status": candidate["model_output_normalization"]["status"],
                "count": len(candidate["model_output_normalization"]["items"]),
            },
            "review_status": candidate["review_status"],
            "court_ready": candidate["court_ready"],
            "official_numeric_result_authored_by_model": candidate[
                "official_numeric_result_authored_by_model"
            ],
            "provider_response_sha256": getattr(parsed, "provider_response_sha256"),
            "provider_usage": {
                "prompt_tokens": getattr(parsed, "prompt_tokens"),
                "completion_tokens": getattr(parsed, "completion_tokens"),
                "total_tokens": getattr(parsed, "total_tokens"),
                "cost_minor_units": getattr(parsed, "cost_minor_units"),
            },
        },
        "archive_receipt_response_sha256": replay.archive_receipt.get("response_sha256"),
    }


def main() -> int:
    if len(sys.argv) != 1:
        raise SealedReplayBlocked(
            f"封存 {_SEALED_LABEL} 回放不接受参数，避免误指向其他案件。"
        )
    print(json.dumps(_replay(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SealedReplayBlocked as error:
        print(
            json.dumps(
                {
                    "mode": f"READ_ONLY_{_SEALED_LABEL.upper()}_SEALED_RESPONSE_REPLAY",
                    "status": "BLOCKED",
                    "reason": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2)
