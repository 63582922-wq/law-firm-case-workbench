"""One fixed synthetic PDF through real Web intake; no facts, plans or model calls.

Run only inside the existing loopback acceptance API container. Persistent
checkpoint and database records must be inspected before any recovery; never
delete a checkpoint to replay an uncertain upload.
"""
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/backend/scripts")
from run_managed_defence_acceptance import _build_composition, _issue_fixture_identity
from case_kernel.models import Matter

SOURCE_HASH = "1e0933758a7b74d842e71b2e7485634b27056bb10ac86ef4c80d54b3c135ac1b"
_SCOPES = {
    (): ("v1", Path("/tmp/lawcase-unassisted-intake")),
    ("--fresh-v2",): ("v2", Path("/tmp/lawcase-unassisted-intake-v2")),
}


def record(checkpoint, name, value):
    with (checkpoint / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


async def chunks(content):
    yield content


def main():
    arguments = tuple(sys.argv[1:])
    if arguments not in _SCOPES:
        raise RuntimeError("only the fixed v1 or fresh v2 synthetic intake scope is allowed")
    scope, checkpoint = _SCOPES[arguments]
    content = sys.stdin.buffer.read(1024**2 + 1)
    if len(content) > 1024**2 or sha256(content).hexdigest() != SOURCE_HASH:
        raise RuntimeError("only the fixed synthetic court PDF is authorized")
    if checkpoint.exists():
        raise RuntimeError("prior intake checkpoint exists; inspect it, do not replay")
    checkpoint.mkdir(mode=0o700)
    record(checkpoint, "intent.json", dict(scope=f"UNASSISTED_MATERIAL_INTAKE_{scope.upper()}_ONLY", source_sha256=SOURCE_HASH,
        model_calls_allowed=0, facts_preconfirmed=False))
    composition = _build_composition(None)
    identity, session_id = _issue_fixture_identity(composition)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, f"lawcase-unassisted-intake-{scope}:" + identity.actor.firm_id))
        record(checkpoint, "target.json", dict(matter_id=matter_id, source_sha256=SOURCE_HASH))
        store = composition.api_dependencies.matter_store
        try:
            store.get(matter_id, firm_id=identity.actor.firm_id)
        except KeyError:
            pass
        else:
            raise RuntimeError("target already exists; no replay or source replacement")
        created = store.create(matter=Matter(matter_id=matter_id, firm_id=identity.actor.firm_id,
                title="合成读卷验收｜原始法院材料（未预置事实）"), actor=identity.actor,
            idempotency_key=f"unassisted-intake-create-{scope}")
        if created.matter_version != 1:
            raise RuntimeError("unexpected initial matter version")
        service = composition.material_upload_service
        slot = service.create_slot(identity=identity, matter_id=matter_id, expected_version=1,
            client_filename="法院送达材料（合成）.pdf", declared_content_length=len(content))
        record(checkpoint, "slot.json", dict(matter_id=matter_id, upload_id=slot.upload_id, source_sha256=SOURCE_HASH))
        receipt = asyncio.run(service.accept_content(identity=identity, matter_id=matter_id,
            upload_id=slot.upload_id, chunks=chunks(content)))
        record(checkpoint, "receipt.json", asdict(receipt))
        status = service.read_status(identity=identity, matter_id=matter_id, upload_id=slot.upload_id)
        if status.status != "COMPLETED" or receipt.content_sha256 != SOURCE_HASH or receipt.page_count != 8:
            raise RuntimeError("intake receipt/status mismatch")
        current = store.get(matter_id, firm_id=identity.actor.firm_id)
        if current.version != 2 or receipt.matter_version != 2:
            raise RuntimeError("intake did not produce exactly the expected case version")
        record(checkpoint, "result.json", dict(status="WEB_SERVICE_INTAKE_COMPLETED", acceptance_scope=scope, matter_id=matter_id,
            upload_id=slot.upload_id, matter_version=current.version, pages=receipt.page_count,
            source_sha256=SOURCE_HASH, agent_run_executed=False, facts_preconfirmed=False,
            browser_oidc_verified=False))
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session_id)


if __name__ == "__main__":
    main()
