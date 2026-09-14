"""Fixed synthetic supplement through normal intake; never approve repayment.

The persistent upload slot is authoritative. A prior checkpoint stops execution:
inspect the slot rather than resending an uncertain upload.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

import run_managed_defence_acceptance as harness

MATTER_ID = "767fda38-e3de-5a15-816f-510a686c7600"
SOURCE_HASH = "1e160b9265b85d23a03421471885dd951ae6a06258f1a6f3fa85e9e762103a42"
CHECKPOINT = Path("/tmp/lawcase-wechat-supplement-20260910")


def record(name: str, value: object) -> None:
    with (CHECKPOINT / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


async def chunks(content: bytes):
    yield content


def main() -> None:
    content = sys.stdin.buffer.read(2 * 1024**2 + 1)
    if sha256(content).hexdigest() != SOURCE_HASH:
        raise RuntimeError("fixed synthetic source hash mismatch")
    CHECKPOINT.mkdir(mode=0o700)  # Never replay a prior/uncertain attempt.
    record("intent.json", {"matter_id": MATTER_ID, "source_sha256": SOURCE_HASH,
        "model_calls": 0, "confirm_facts": False, "scope": "SUPPLEMENT_INTAKE_ONLY"})
    harness._select_acceptance_scenario(harness._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = harness._build_composition(None)
    identity, session_id = harness._issue_fixture_identity(composition)
    try:
        matter = composition.api_dependencies.matter_store.get(
            MATTER_ID, firm_id=identity.actor.firm_id)
        if matter.version != 12:
            raise RuntimeError("case advanced; inspect existing intake before continuing")
        service = composition.material_upload_service
        slot = service.create_slot(identity=identity, matter_id=MATTER_ID,
            expected_version=12, client_filename="微信支付账单导出（合成补证）.pdf",
            declared_content_length=len(content))
        record("slot.json", {"matter_id": MATTER_ID, "upload_id": slot.upload_id})
        receipt = asyncio.run(service.accept_content(identity=identity, matter_id=MATTER_ID,
            upload_id=slot.upload_id, chunks=chunks(content)))
        record("receipt.json", asdict(receipt))
        status = service.read_status(identity=identity, matter_id=MATTER_ID, upload_id=slot.upload_id)
        if status.status != "COMPLETED" or receipt.page_count != 12 or receipt.content_sha256 != SOURCE_HASH:
            raise RuntimeError("receipt requires inspection; do not re-upload")
        record("result.json", {"status": "SUPPLEMENT_INTAKE_COMPLETED",
            "matter_version": receipt.matter_version, "model_calls": 0,
            "facts_approved": False, "followup_resolved": False})
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
