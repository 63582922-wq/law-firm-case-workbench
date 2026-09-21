"""Bounded synthetic revision qualification; never approves a court submission.

Run in the existing API container. Fixed keys preserve recovery; no model calls.
The injected exception proves real authorization/request/task transaction rollback.
"""
from hashlib import sha256
import json
from unittest.mock import patch

import run_managed_defence_acceptance as h
from case_kernel.case_agent_document_revisions import LawyerParagraphChange

MATTER = "0eb75f87-ccd3-5ae0-9207-c8df529d3657"
RUN = "ca6ccf3a-0b8f-5cb3-8ced-2b1bc0fb83ce"
ARTIFACT = "d450cc66-5f1e-5f38-a192-acd283cdaac0"


def main():
    h._select_acceptance_scenario(h._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = h._build_composition(None)
    identity, session = h._issue_fixture_identity(composition)
    try:
        service = composition.api_dependencies.case_agent_document_review_service
        scope = dict(identity=identity, matter_id=MATTER, run_id=RUN, artifact_id=ARTIFACT)
        review = service.read_review(**scope)
        if review.revision_number != 1 or review.version_status != "CURRENT":
            print(json.dumps({"status": "READ_ONLY_EXISTING_REVISION", "revision": review.revision_number}))
            return
        paragraph = review.sections[1].paragraphs[0]
        change = LawyerParagraphChange(
            section_index=1, paragraph_index=0,
            expected_text_hash=sha256(paragraph.text.encode()).hexdigest(),
            replacement_text="对于原告主张返还第二笔借款205,000.00元且本金未还的诉请，答辩人提出异议。具体抗辩范围以已确认诉请回应及证据核对结果为准；本稿不新增承诺或承认。",
            reason="合成验收：将已登记的争议口径改为直接答辩表达，保持原诉请金额和来源，不新增事实或法律决定。",
            source_refs=tuple(source.source_ref for source in paragraph.sources),
        )
        save = dict(**scope, expected_revision_number=1,
                    idempotency_key="synthetic-content-edit-20260910-worker-context", changes=(change,))
        proposal_id = service.save_content_proposal(**save)
        assert service.save_content_proposal(**save) == proposal_id
        authorize = dict(**scope, proposal_id=proposal_id, expected_revision_number=1,
                         idempotency_key="synthetic-content-generate-20260910-worker-context",
                         review_note="全合成夹具模拟生成授权，仅验证修改后的待复核副本，不作真实律师终审或法院提交。")
        proposal = service.read_content_proposal(**scope, proposal_id=proposal_id)
        print(json.dumps({"proposal_id": proposal_id, "proposal_replay": True,
                          "before": proposal}, ensure_ascii=False, default=str))
        store = service._revisions
        original = store._register_content_revision_request

        class RollbackProbe(Exception):
            pass

        def rollback_after_enqueue(*args, **kwargs):
            original(*args, **kwargs)
            raise RollbackProbe()

        # Only this process's synthetic command is patched; runtime code is unchanged.
        with patch.object(store, "_register_content_revision_request", rollback_after_enqueue):
            try:
                service.authorize_content_generation(**authorize)
            except RollbackProbe:
                print(json.dumps({"rollback_after_enqueue": True}))
        after = service.read_content_proposal(**scope, proposal_id=proposal_id)
        assert after["generation_status"] == "NOT_AUTHORIZED"
        print(json.dumps({"after_rollback": after}, ensure_ascii=False, default=str))
        review_id = service.authorize_content_generation(**authorize)
        assert service.authorize_content_generation(**authorize) == review_id
        print(json.dumps({"review_id": review_id, "authorization_replay": True,
                          "result": service.read_content_proposal(**scope, proposal_id=proposal_id)},
                         ensure_ascii=False, default=str))
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session)


if __name__ == "__main__":
    main()
