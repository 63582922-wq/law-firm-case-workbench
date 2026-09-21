#!/usr/bin/env python3
"""Append the one permitted M2 sealed-response review candidate.

Unlike the M2 replay command this script persists a *separate* review-only
candidate.  It accepts no arguments, never sends/retries a provider request,
and never mutates the historical Agent run, task, receipts, verification,
documents, approvals, or submission state.  Its fixed M2 import and the
database trigger together prevent it from becoming a generic recovery tool.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import replay_managed_defence_m2_sealed_response as _m2  # noqa: E402

from case_kernel.case_agent_sealed_response_recovery import (  # noqa: E402
    PostgresSealedResponseRecoveryStagingPort,
    SealedResponseRecoveryBlocked,
    build_sealed_response_recovery_staging_request,
)
from case_kernel.case_agent_lawyer_analysis import (  # noqa: E402
    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
)
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore  # noqa: E402


def _text(value: object, label: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise SealedResponseRecoveryBlocked(f"封存 M2 的 {label} 无效。")
    return value


def _stage() -> dict[str, object]:
    replay = _m2._sealed._load_sealed_replay()
    binding = replay.binding
    candidate = replay.candidate
    stored = replay.stored
    if (
        binding.get("run_id") != _m2._sealed._SEALED_RUN_ID
        or binding.get("matter_id") != _m2._sealed._SEALED_MATTER_ID
        or binding.get("external_request_id") != _m2._sealed._SEALED_EXTERNAL_REQUEST_ID
        or binding.get("run_status") != "WAITING_INPUT"
        or binding.get("event_version") != 8
    ):
        raise SealedResponseRecoveryBlocked("封存 M2 的固定恢复边界已经变化。")
    request = build_sealed_response_recovery_staging_request(
        run_id=_text(binding.get("run_id"), "运行编号"),
        task_id=_text(binding.get("task_id"), "任务编号"),
        task_input_hash=_text(binding.get("input_hash"), "任务输入哈希"),
        source_hash=_text(candidate.get("source_hash"), "候选来源哈希"),
        artifact_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
        candidate_payload=replay.candidate_payload,
        source_run_event_version=8,
        source_run_snapshot_hash=_text(binding.get("snapshot_hash"), "运行快照哈希"),
        external_request_id=_text(binding.get("external_request_id"), "外发编号"),
        request_hash=_text(binding.get("request_hash"), "请求哈希"),
        response_sha256=_text(getattr(stored, "response_sha256", None), "响应哈希"),
        archive_sha256=_text(getattr(stored, "archive_sha256", None), "归档哈希"),
    )
    object_store = S3CompatiblePrivateObjectStore(replay.settings.object_store)
    receipt = PostgresSealedResponseRecoveryStagingPort(
        dsn=replay.settings.runtime.postgres_dsn,
        worker_actor=replay.settings.runtime.actor,
        object_store=object_store,
    ).stage(request)
    after = _m2._sealed._run_snapshot(settings=replay.settings)
    if dict(after) != dict(replay.before):
        raise SealedResponseRecoveryBlocked("恢复候选不应改变封存 M2 的运行状态。")
    return {
        "mode": "M2_SEALED_RESPONSE_RECOVERY_CANDIDATE",
        "provider_requests_sent": 0,
        "agent_event_or_task_writes": 0,
        "candidate_artifact_id": receipt.candidate.artifact_id,
        "candidate_sha256": sha256(replay.candidate_payload).hexdigest(),
        "candidate_review_status": receipt.candidate.review_status,
        "recovery_kind": receipt.recovery_kind,
        "original_run_unchanged": True,
        "court_ready": False,
    }


def main() -> int:
    if len(sys.argv) != 1:
        raise SealedResponseRecoveryBlocked("封存 M2 恢复候选命令不接受参数。")
    print(json.dumps(_stage(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (_m2._sealed.SealedReplayBlocked, SealedResponseRecoveryBlocked) as error:
        print(
            json.dumps(
                {
                    "mode": "M2_SEALED_RESPONSE_RECOVERY_CANDIDATE",
                    "status": "BLOCKED",
                    "reason": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2)
