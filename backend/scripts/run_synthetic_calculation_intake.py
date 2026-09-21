"""Bounded real-service calculation fixture: original intake and payment ledger.

No model calls, official calculation, legal approvals or court submission.
Payment classification is an explicit synthetic test decision, not an inference
that a bank transfer alone establishes the legal nature of a real payment.
Run in the existing loopback acceptance API container with the fixed PDF on stdin.
An existing checkpoint or matter stops execution; inspect receipts before recovery.
"""
import asyncio
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import urllib.request
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/backend/scripts")
from run_managed_defence_acceptance import _build_composition, _issue_fixture_identity
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.models import Matter
from case_kernel.transaction_ledger import (
    ClassificationOrigin, DatePrecision, ObligationAllocation, PaymentNature,
    TransactionChannel, TransactionDirection,
)

SOURCE_HASH = "8cff8981ecbd3293e2941004bee830d514dbc4e7a72e295a4b92a88a61b06c78"
CHECKPOINT = Path("/tmp/lawcase-synthetic-calculation-intake")
SCOPE = "synthetic-calculation-ledger-v1"


def record(name, value):
    with (CHECKPOINT / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())


def approval(stage):
    return sha256(f"{SCOPE}:SIMULATED_TEST_DECISION_NOT_LAWYER_APPROVAL:{stage}".encode()).hexdigest()


async def chunks(content):
    yield content


def main():
    content = sys.stdin.buffer.read(1024**2 + 1)
    if len(content) > 1024**2 or sha256(content).hexdigest() != SOURCE_HASH:
        raise RuntimeError("fixed synthetic bank original required")
    if CHECKPOINT.exists():
        raise RuntimeError("existing checkpoint; inspect before recovery, do not replay")
    CHECKPOINT.mkdir(mode=0o700)
    record("intent.json", dict(scope=SCOPE, source_sha256=SOURCE_HASH, model_calls_allowed=0,
        simulated_decisions=True, lawyer_accepted=False, court_ready=False,
        assumption="First disbursement and unlabelled repayment allocated to loan A; second disbursement to loan B. Test assumption only."))
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, SCOPE + ":" + identity.actor.firm_id))
        record("target.json", dict(matter_id=matter_id))
        matter_store = c.api_dependencies.matter_store
        try:
            matter_store.get(matter_id, firm_id=identity.actor.firm_id)
        except KeyError:
            pass
        else:
            raise RuntimeError("target exists; no automatic recovery or replacement")
        created = matter_store.create(matter=Matter(matter_id=matter_id, firm_id=identity.actor.firm_id,
            title="合成计算验收｜模拟决定，非律师批准，禁止提交"), actor=identity.actor,
            idempotency_key=SCOPE + ":create")
        record("created.json", asdict(created))
        slot = c.material_upload_service.create_slot(identity=identity, matter_id=matter_id,
            expected_version=created.matter_version, client_filename="银行流水（全合成计算验收）.pdf",
            declared_content_length=len(content))
        record("slot.json", dict(upload_id=slot.upload_id, matter_id=matter_id))
        intake = asyncio.run(c.material_upload_service.accept_content(identity=identity, matter_id=matter_id,
            upload_id=slot.upload_id, chunks=chunks(content)))
        record("intake.json", asdict(intake))
        if intake.content_sha256 != SOURCE_HASH or intake.page_count != 10:
            raise RuntimeError("unexpected source intake receipt")
        snapshot = c.evidence_manifest_store.get_evidence_snapshot(matter_id=matter_id, actor=identity.actor)
        originals = [r for r in snapshot.original_files if r["original_file_sha256"] == SOURCE_HASH]
        if len(originals) != 1:
            raise RuntimeError("original source binding must be unique")
        original = originals[0]
        version = snapshot.version
        ledger = c.case_ledger_store
        entries = (
            (3, date(2019, 6, 3), "300000.00", "周建国", "王强", "loan-A", PaymentNature.DISBURSEMENT),
            (4, date(2019, 11, 15), "200000.00", "周建国", "王强", "loan-B", PaymentNature.DISBURSEMENT),
            (5, date(2019, 10, 3), "6000.00", "王强", "周建国", "loan-A", PaymentNature.REPAYMENT_UNSPECIFIED),
        )
        for page_no, local_date, amount, payer, payee, obligation, nature in entries:
            pages = [p for p in snapshot.pages if str(p["evidence_file_id"]) == str(original["evidence_file_id"])
                     and p["page_number"] == page_no]
            if len(pages) != 1:
                raise RuntimeError("source page binding must be unique")
            link = EvidenceLink(str(pages[0]["evidence_page_id"]), SOURCE_HASH, page_no, None, original["original_label"])
            key = f"{SCOPE}:page-{page_no}"
            tx = ledger.create_transaction_candidate(matter_id=matter_id, actor=identity.actor,
                expected_version=version, idempotency_key=key+":candidate", local_date=local_date,
                date_precision=DatePrecision.EXACT_DATE, amount=Decimal(amount), currency="CNY",
                direction=TransactionDirection.OUTGOING if payer == "周建国" else TransactionDirection.INCOMING,
                payer_label=payer+"（全合成）", payee_label=payee+"（全合成）", channel=TransactionChannel.BANK,
                transaction_reference=key, evidence_links=(link,))
            record(f"page-{page_no}-candidate.json", asdict(tx))
            confirmed = ledger.confirm_transaction(matter_id=matter_id, transaction_id=tx.object_id,
                actor=identity.actor, expected_version=tx.matter_version, idempotency_key=key+":confirm",
                confirmation_hash=approval(key+":confirm"))
            record(f"page-{page_no}-confirmed.json", asdict(confirmed))
            classification = ledger.create_payment_classification_candidate(matter_id=matter_id,
                transaction_id=tx.object_id, actor=identity.actor, expected_version=confirmed.matter_version,
                idempotency_key=key+":classification", origin=ClassificationOrigin.ASSISTANT_ENTRY, nature=nature,
                allocations=(ObligationAllocation(SCOPE+":"+obligation, Decimal(amount), "CNY"),),
                same_day_sequence=1, evidence_links=(link,))
            record(f"page-{page_no}-classification.json", asdict(classification))
            decided = ledger.approve_payment_classification(matter_id=matter_id,
                classification_id=classification.object_id, actor=identity.actor,
                expected_version=classification.matter_version, idempotency_key=key+":test-decision",
                approval_hash=approval(key+":test-decision"))
            record(f"page-{page_no}-decision.json", asdict(decided))
            version = decided.matter_version
        current = ledger.get_case_snapshot(matter_id=matter_id, actor=identity.actor)
        if current.version != version or len(current.transactions) != 3 or len(current.payment_classifications) != 3:
            raise RuntimeError("final ledger readback mismatch")
        result = dict(matter_id=matter_id, matter_version=version, transactions=3, classifications=3,
                      model_calls=0, lawyer_accepted=False, court_ready=False)
        record("result.json", result)
        print(json.dumps(result), flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


def capture_historical_source(*, gazette=False):
    """Archive a fixed public historical source, not an applicable rate approval."""
    from run_managed_defence_acceptance import _store_or_recover_official_source
    from case_kernel.official_source_private_store import extract_literal_official_source_text
    from case_kernel.legal_source_postgres import LegalAuthorityLevel

    class RejectRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError("historical source redirect refused")

    if not (CHECKPOINT / "result.json").exists():
        raise RuntimeError("completed ledger checkpoint required")
    url = ("https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html"
           if gazette else "https://www.court.gov.cn/zixun/xiangqing/15146.html")
    prefix = "historical-gazette" if gazette else "historical-source"
    record(prefix+"-intent.json", dict(url=url, max_bytes=1048576,
        purpose="HISTORICAL_SOURCE_ONLY_NOT_APPLICABILITY_APPROVAL", model_calls_allowed=0))
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, SCOPE + ":" + identity.actor.firm_id))
        current = c.api_dependencies.matter_store.get(matter_id, firm_id=identity.actor.firm_id)
        if current.version != 14:
            raise RuntimeError("expected ledger version 14; inspect before recovery")
        request = urllib.request.Request(url, headers={"Accept": "text/html", "Accept-Encoding": "identity"})
        with urllib.request.build_opener(RejectRedirect()).open(request, timeout=25) as response:
            if response.status != 200 or response.geturl() != url or response.headers.get_content_type() != "text/html":
                raise RuntimeError("historical source response mismatch")
            content = response.read(1048577)
        if not content or len(content) > 1048576:
            raise RuntimeError("historical source size limit")
        digest = sha256(content).hexdigest()
        retrieved = datetime.now(timezone.utc)
        with (CHECKPOINT / (prefix+".html")).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        record(prefix+"-download.json", dict(url=url, sha256=digest, bytes=len(content), retrieved_at=retrieved))
        # Keep the exact response before parsing. A 200 page may be a challenge
        # rather than legal text; failure must not destroy the diagnostic source.
        try:
            text = extract_literal_official_source_text(body=content, content_media_type="text/html")
            markers = ("法释〔2015〕18号", "2015年9月1日起施行", "第二十五条", "第二十六条", "年利率24%")
            if not all(marker in text for marker in markers):
                raise RuntimeError("historical text markers missing; do not register")
        except Exception as error:
            record(prefix+"-rejected.json", dict(status="SOURCE_NOT_REGISTERED",
                error_type=type(error).__name__, source_sha256=digest))
            raise
        stored = _store_or_recover_official_source(c.official_source_adapters.objects, content=content,
            firm_id=identity.actor.firm_id, matter_id=matter_id, content_sha256=digest, content_media_type="text/html")
        record(prefix+"-object.json", asdict(stored))
        receipt = c.legal_store.register_official_source_snapshot(matter_id=matter_id, actor=identity.actor,
            expected_version=14, idempotency_key=SCOPE+":"+prefix+"-2015",
            source_id="SPC-PRIVATE-LENDING-2015-18-"+("GAZETTE" if gazette else "HISTORICAL"), publisher="最高人民法院",
            authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION, official_url=url,
            provision_locator="法释〔2015〕18号，2015年原始版本；第九条、第二十五至二十九条。历史原文登记，不证明当前或本案适用。",
            retrieved_at=retrieved, content_sha256=digest, content_media_type="text/html",
            storage_object_key=stored.ledger_object_key,
            verification_hash=sha256((url+":"+digest+":"+"|".join(markers)).encode()).hexdigest(),
            license_basis="最高人民法院公开司法解释公文；仅内部合成研发来源复核，不再分发网站其他内容。",
            license_review_hash=approval("public-judicial-text-internal-source-review"))
        record(prefix+"-receipt.json", asdict(receipt))
        print(json.dumps(dict(matter_id=matter_id, version=receipt.matter_version,
            source_snapshot_id=receipt.object_id, source_sha256=digest, rule_approved=False)), flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


def intake_visible_loan():
    """Admit one reviewed v2 synthetic JPEG, without OCR or fact approval."""
    content = sys.stdin.buffer.read(1048577)
    digest = "8f276d79067e7f8d6cb34f98608f486d9cbfb9d670874f17dfc545aef36d7d4b"
    if sha256(content).hexdigest() != digest:
        raise RuntimeError("reviewed v2 loan original required")
    record("visible-loan-intent.json", dict(sha256=digest, model_calls_allowed=0, facts_approved=False))
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, SCOPE + ":" + identity.actor.firm_id))
        if c.api_dependencies.matter_store.get(matter_id, firm_id=identity.actor.firm_id).version != 14:
            raise RuntimeError("expected case version 14; inspect before recovery")
        service = c.common_material_upload_service
        slot = service.create_slot(identity=identity, matter_id=matter_id, expected_version=14,
            client_filename="借条1（全合成正文v2）.jpg", declared_byte_size=len(content),
            declared_media_type="image/jpeg", idempotency_key=SCOPE+":visible-loan-slot")
        record("visible-loan-slot.json", asdict(slot))
        receipt = asyncio.run(service.accept_content(identity=identity, matter_id=matter_id,
            upload_id=slot.upload_id, idempotency_key=SCOPE+":visible-loan-content", chunks=chunks(content)))
        record("visible-loan-receipt.json", asdict(receipt))
        print(json.dumps(asdict(receipt), default=str), flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


def prepare_visual_posture():
    """Confirm test engagement context only; no run or external request."""
    record("visual-posture-command-intent.json", dict(expected_version=15, simulated_engagement=True,
        model_calls_allowed=0, objective="Read the admitted v2 loan image into source-bound review candidates",
        blocked_before_dispatch="OCR cost receipts currently report zero; budget enforcement not established"))
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, SCOPE + ":" + identity.actor.firm_id))
        receipt = c.case_posture_service.confirm_complete_posture(identity=identity, matter_id=matter_id,
            expected_version=15, idempotency_key=SCOPE+"-visual-test-posture",
            party_kind="NATURAL_PERSON", display_label="王强（全合成验收被告）",
            forum_type="PEOPLE_COURT", case_type_code="CIVIL.PRIVATE_LENDING", procedure_stage="FIRST_INSTANCE",
            position_code="DEFENDANT", authority_scope_code="GENERAL_AUTHORITY", engagement_state="ACTIVE")
        record("visual-posture-receipt.json", asdict(receipt))
        print(json.dumps(asdict(receipt),default=str),flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


def create_visual_run():
    """Create one bounded goal through the actual control service; do not dispatch."""
    from case_kernel.case_agent_supervisor import RunResourceBudget
    # Independent directory survives as an archived stage; prior intake must not
    # be rerun merely because a container restart cleared its tmpfs checkpoint.
    global CHECKPOINT
    CHECKPOINT = Path("/tmp/lawcase-visible-loan-run")
    CHECKPOINT.mkdir(mode=0o700, exist_ok=False)
    page_ref = "evidence-page:0ed71e7c-8f9e-46e2-b3e9-ca382d9f0fc7"
    record("intent.json", dict(matter_version=20, page_ref=page_ref, dispatch_allowed=False,
        planning_calls_allowed=1, ocr_calls_allowed=1, automatic_retries_allowed=0))
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        matter_id = str(uuid5(NAMESPACE_URL, SCOPE + ":" + identity.actor.firm_id))
        control = c.api_dependencies.case_agent_control_service
        # Server-owned policy for this isolated test goal, not browser supplied.
        control._policy = replace(control._policy, run_budget=RunResourceBudget(
            max_tasks=6, max_total_attempts=6, max_external_calls=1,
            max_runtime_seconds=660, max_cost_minor_units=100, max_output_bytes=8*1024*1024))
        receipt = control.create_run(identity=identity, matter_id=matter_id,
            objective="识别已登记的第一张借条图片，形成可回到原图核验的逐字文字候选；保留月息原始表述，不换算利率，不作正式事实或法律批准。",
            success_criteria=("仅消费指定借条图片页，输出源页绑定的视觉识别候选。", "保留无法辨认处，不用已有台账或英文摘要补写图片内容。"),
            constraints=("本次唯一图片来源："+page_ref, "只允许一次视觉OCR外发，结果不明不得重发；不研究法律、不计算、不生成文书、不处理其他案卷。",
                "图片为全合成测试材料，候选不是律师认可或法院提交材料。"),
            expected_matter_version=20, idempotency_key=SCOPE+"-visible-loan-run",
            now=datetime.now(timezone.utc), requested_deliverables=())
        record("created.json", asdict(receipt))
        print(json.dumps(asdict(receipt), default=str), flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


def approve_visual_task():
    """Approve execution only for the exact synthetic single-page task."""
    global CHECKPOINT
    CHECKPOINT = Path("/tmp/lawcase-visible-loan-approval")
    CHECKPOINT.mkdir(mode=0o700, exist_ok=False)
    run_id = "781a290d-7588-5e12-93a0-6e7545cd5ea3"
    matter_id = "ad70242f-0a41-540b-a125-d4f809f0309e"
    page_ref = "evidence-page:0ed71e7c-8f9e-46e2-b3e9-ca382d9f0fc7"
    c = _build_composition(None)
    identity, sid = _issue_fixture_identity(c)
    try:
        control = c.api_dependencies.case_agent_control_service
        state = control._store.replay_run(matter_id=matter_id, actor=identity.actor, run_id=run_id)
        if (state.event_version, state.status.value, state.snapshot.matter_version) != (5, "WAITING_APPROVAL", 20):
            raise RuntimeError("visual task approval state changed; do not replay")
        if state.graph.graph_hash != "aec1c00289fedf1b5e91833be44f9c2bc0a48aadafcb4b746e85f673d8f33736" or len(state.tasks) != 1:
            raise RuntimeError("visual task graph differs")
        task = state.tasks[0]
        if (task.attempt_count != 0 or task.receipts or task.spec.skill.skill_id != "image_visual_ocr"
                or task.spec.input_refs != (page_ref,) or task.spec.budget.max_attempts != 1
                or task.spec.budget.max_external_calls != 1 or task.spec.budget.max_cost_minor_units != 6
                or task.spec.retry_mode.value != "NEVER_AUTOMATIC"):
            raise RuntimeError("visual execution boundary differs")
        record("intent.json", dict(run_id=run_id, page_ref=page_ref, task_id=task.spec.task_id,
            graph_hash=state.graph.graph_hash, model_calls_allowed=1, reserved_cny="0.06",
            approval_kind="SIMULATED_TEST_EXECUTION_NOT_LAWYER_FACT_APPROVAL"))
        response = control.submit_approval(identity=identity, matter_id=matter_id, run_id=run_id,
            approval_id=task.spec.task_id, approved=True,
            note="合成测试：仅授权既有百炼北京供应商识别指定借条一次，不批准事实、利率、文书或提交。",
            expected_run_version=5, idempotency_key="synthetic-visible-loan-ocr-approval-v1",
            now=datetime.now(timezone.utc))
        record("approved.json", asdict(response))
        print(json.dumps(asdict(response), default=str, ensure_ascii=False), flush=True)
    finally:
        c.api_dependencies.session_authority.revoke(session_id=sid)


if __name__ == "__main__":
    if sys.argv[1:] == ["--historical-source"]:
        capture_historical_source()
    elif sys.argv[1:] == ["--historical-gazette"]:
        capture_historical_source(gazette=True)
    elif sys.argv[1:] == ["--visible-loan"]:
        intake_visible_loan()
    elif sys.argv[1:] == ["--visual-posture"]:
        prepare_visual_posture()
    elif sys.argv[1:] == ["--create-visual-run"]:
        create_visual_run()
    elif sys.argv[1:] == ["--approve-visual-task"]:
        approve_visual_task()
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit("unsupported stage")
