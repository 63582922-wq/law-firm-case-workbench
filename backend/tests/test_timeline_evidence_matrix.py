from datetime import date
from decimal import Decimal
from hashlib import sha256
import unittest

from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import (
    AssertionOrigin,
    ClaimResponsePosition,
    FactClaimLedger,
    FactStatus,
)
from case_kernel.models import Actor, Role
from case_kernel.timeline_evidence_matrix import (
    BurdenParty,
    EventDatePrecision,
    EvidencePurpose,
    TimelineEvidenceLedger,
    TimelineEventType,
    TimelineMatrixBlocked,
)


def evidence_link(*, evidence_id: str) -> EvidenceLink:
    return EvidenceLink(
        evidence_id=evidence_id,
        original_file_sha256=sha256(evidence_id.encode("utf-8")).hexdigest(),
        page_number=11,
        region_id="alpha_timeline_region",
        original_label=f"合成案件材料：{evidence_id}",
    )


class TimelineEvidenceMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
        self.lead = Actor("alpha_lead", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
        self.fact_ledger = FactClaimLedger()
        self.timeline = TimelineEvidenceLedger()

    def _fact_snapshot(self):
        fact = self.fact_ledger.add_fact_candidate(
            self.assistant,
            original_text="合成流水显示2020年9月4日有一笔人民币付款。",
            origin=AssertionOrigin.DEFENDANT_STATEMENT,
            evidence_links=(evidence_link(evidence_id="payment-record"),),
        )
        fact = self.fact_ledger.decide_fact(self.lead, fact_id=fact.fact_id, status=FactStatus.CONFIRMED, decision_hash="fact-approved")
        claim = self.fact_ledger.add_claim_candidate(
            self.assistant,
            original_claim_text="原告主张合成借款本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=(evidence_link(evidence_id="claim-document"),),
        )
        claim = self.fact_ledger.confirm_claim_scope(self.lead, claim_id=claim.claim_id, confirmation_hash="claim-approved")
        self.fact_ledger.set_claim_response(
            self.lead,
            claim_id=claim.claim_id,
            position=ClaimResponsePosition.DISPUTE,
            confirmed_fact_ids=(fact.fact_id,),
            partial_amount=None,
            currency=None,
            approval_hash="response-approved",
        )
        issue = self.fact_ledger.add_dispute_issue_candidate(
            self.assistant,
            question="合成付款能否抵扣本案主张？",
            claim_ids=(claim.claim_id,),
            confirmed_fact_ids=(fact.fact_id,),
        )
        self.fact_ledger.confirm_dispute_issue(self.lead, issue_id=issue.issue_id, approval_hash="issue-approved")
        return self.fact_ledger.build_formal_snapshot(self.lead)

    def test_confirmed_event_and_matrix_entry_bind_to_the_current_fact_snapshot(self) -> None:
        fact_snapshot = self._fact_snapshot()
        fact = fact_snapshot.facts[0]
        issue = fact_snapshot.issues[0]
        event = self.timeline.add_timeline_event_candidate(
            self.assistant,
            event_type=TimelineEventType.PAYMENT,
            date_precision=EventDatePrecision.EXACT_DATE,
            event_date=date(2020, 9, 4),
            range_end_date=None,
            label="合成付款事件",
            fact_ids=(fact.fact_id,),
            evidence_links=(evidence_link(evidence_id="payment-record"),),
        )
        self.timeline.confirm_timeline_event(self.lead, event_id=event.event_id, fact_snapshot=fact_snapshot, confirmation_hash="event-approved")
        matrix_item = self.timeline.add_evidence_matrix_candidate(
            self.assistant,
            fact_snapshot=fact_snapshot,
            fact_ids=(fact.fact_id,),
            claim_ids=(),
            issue_ids=(issue.issue_id,),
            purpose=EvidencePurpose.PROVE_PAYMENT,
            burden_party=BurdenParty.DEFENDANT,
            evidence_links=(evidence_link(evidence_id="payment-record"),),
        )
        self.timeline.confirm_evidence_matrix_item(self.lead, matrix_item_id=matrix_item.matrix_item_id, confirmation_hash="matrix-approved")

        snapshot = self.timeline.build_formal_snapshot(self.lead, fact_snapshot=fact_snapshot)
        self.assertEqual(snapshot.timeline_events[0].event_type, TimelineEventType.PAYMENT)
        self.assertEqual(snapshot.evidence_matrix[0].burden_party, BurdenParty.DEFENDANT)

    def test_uncovered_confirmed_issue_blocks_the_formal_matrix(self) -> None:
        fact_snapshot = self._fact_snapshot()

        with self.assertRaisesRegex(TimelineMatrixBlocked, "every confirmed dispute issue"):
            self.timeline.build_formal_snapshot(self.lead, fact_snapshot=fact_snapshot)

    def test_matrix_is_stale_when_the_approved_fact_snapshot_changes(self) -> None:
        fact_snapshot = self._fact_snapshot()
        fact = fact_snapshot.facts[0]
        issue = fact_snapshot.issues[0]
        matrix_item = self.timeline.add_evidence_matrix_candidate(
            self.assistant,
            fact_snapshot=fact_snapshot,
            fact_ids=(fact.fact_id,),
            claim_ids=(),
            issue_ids=(issue.issue_id,),
            purpose=EvidencePurpose.PROVE_PAYMENT,
            burden_party=BurdenParty.DEFENDANT,
            evidence_links=(evidence_link(evidence_id="payment-record"),),
        )
        self.timeline.confirm_evidence_matrix_item(self.lead, matrix_item_id=matrix_item.matrix_item_id, confirmation_hash="matrix-approved")
        extra_fact = self.fact_ledger.add_fact_candidate(
            self.assistant,
            original_text="合成新增事实。",
            origin=AssertionOrigin.ASSISTANT_ENTRY,
            evidence_links=(evidence_link(evidence_id="extra-fact"),),
        )
        self.fact_ledger.decide_fact(self.lead, fact_id=extra_fact.fact_id, status=FactStatus.CONFIRMED, decision_hash="extra-approved")
        changed_snapshot = self.fact_ledger.build_formal_snapshot(self.lead)

        with self.assertRaisesRegex(TimelineMatrixBlocked, "stale"):
            self.timeline.build_formal_snapshot(self.lead, fact_snapshot=changed_snapshot)

    def test_unknown_date_is_preserved_but_cannot_be_misrepresented_as_precise(self) -> None:
        fact_snapshot = self._fact_snapshot()
        event = self.timeline.add_timeline_event_candidate(
            self.assistant,
            event_type=TimelineEventType.COMMUNICATION,
            date_precision=EventDatePrecision.UNKNOWN,
            event_date=None,
            range_end_date=None,
            label="日期未明的合成沟通",
            fact_ids=(fact_snapshot.facts[0].fact_id,),
            evidence_links=(evidence_link(evidence_id="communication"),),
        )

        self.assertIsNone(event.event_date)
        with self.assertRaisesRegex(TimelineMatrixBlocked, "unknown timeline date"):
            self.timeline.add_timeline_event_candidate(
                self.assistant,
                event_type=TimelineEventType.COMMUNICATION,
                date_precision=EventDatePrecision.UNKNOWN,
                event_date=date(2020, 9, 4),
                range_end_date=None,
                label="错误精确日期",
                fact_ids=(fact_snapshot.facts[0].fact_id,),
                evidence_links=(evidence_link(evidence_id="communication"),),
            )


if __name__ == "__main__":
    unittest.main()
