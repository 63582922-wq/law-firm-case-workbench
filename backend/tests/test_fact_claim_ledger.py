from decimal import Decimal
from hashlib import sha256
import unittest

from case_kernel.fact_claim_ledger import (
    AssertionOrigin,
    ClaimResponsePosition,
    EvidenceLink,
    FactClaimLedger,
    FactLedgerBlocked,
    FactStatus,
)
from case_kernel.models import Actor, Role


def evidence_link() -> EvidenceLink:
    return EvidenceLink(
        evidence_id="alpha_evidence_001",
        original_file_sha256=sha256(b"synthetic original evidence").hexdigest(),
        page_number=17,
        region_id="alpha_region_001",
        original_label="合成微信交易记录第17页",
    )


class FactClaimLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
        self.lead = Actor("alpha_lead_lawyer", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
        self.ledger = FactClaimLedger()

    def _confirmed_fact(self):
        candidate = self.ledger.add_fact_candidate(
            self.assistant,
            original_text="合成付款记录显示一笔人民币1,000.00元转账。",
            origin=AssertionOrigin.PLAINTIFF_PLEADING,
            evidence_links=(evidence_link(),),
        )
        return self.ledger.decide_fact(self.lead, fact_id=candidate.fact_id, status=FactStatus.CONFIRMED, decision_hash="fact-approval")

    def test_candidate_fact_cannot_support_a_claim_response(self) -> None:
        candidate = self.ledger.add_fact_candidate(
            self.assistant,
            original_text="合成候选事实。",
            origin=AssertionOrigin.AGENT_CANDIDATE,
            evidence_links=(evidence_link(),),
        )
        claim = self.ledger.add_claim_candidate(
            self.assistant,
            original_claim_text="原告主张合成本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=(evidence_link(),),
        )
        self.ledger.confirm_claim_scope(self.lead, claim_id=claim.claim_id, confirmation_hash="claim-scope")
        with self.assertRaisesRegex(FactLedgerBlocked, "lawyer-confirmed"):
            self.ledger.set_claim_response(
                self.lead,
                claim_id=claim.claim_id,
                position=ClaimResponsePosition.DISPUTE,
                confirmed_fact_ids=(candidate.fact_id,),
                partial_amount=None,
                currency=None,
                approval_hash="response-approval",
            )

    def test_partial_admission_requires_currency_and_cannot_exceed_claim(self) -> None:
        fact = self._confirmed_fact()
        claim = self.ledger.add_claim_candidate(
            self.assistant,
            original_claim_text="原告主张合成本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=(evidence_link(),),
        )
        self.ledger.confirm_claim_scope(self.lead, claim_id=claim.claim_id, confirmation_hash="claim-scope")
        with self.assertRaisesRegex(FactLedgerBlocked, "currency"):
            self.ledger.set_claim_response(
                self.lead,
                claim_id=claim.claim_id,
                position=ClaimResponsePosition.PARTIALLY_ADMIT,
                confirmed_fact_ids=(fact.fact_id,),
                partial_amount=Decimal("500.00"),
                currency=None,
                approval_hash="partial-approval",
            )
        with self.assertRaisesRegex(FactLedgerBlocked, "cannot exceed"):
            self.ledger.set_claim_response(
                self.lead,
                claim_id=claim.claim_id,
                position=ClaimResponsePosition.PARTIALLY_ADMIT,
                confirmed_fact_ids=(fact.fact_id,),
                partial_amount=Decimal("1000.01"),
                currency="CNY",
                approval_hash="partial-approval",
            )

    def test_confirmed_fact_claim_response_and_issue_build_a_formal_snapshot(self) -> None:
        fact = self._confirmed_fact()
        claim = self.ledger.add_claim_candidate(
            self.assistant,
            original_claim_text="原告主张合成本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=(evidence_link(),),
        )
        self.ledger.confirm_claim_scope(self.lead, claim_id=claim.claim_id, confirmation_hash="claim-scope")
        self.ledger.set_claim_response(
            self.lead,
            claim_id=claim.claim_id,
            position=ClaimResponsePosition.PARTIALLY_ADMIT,
            confirmed_fact_ids=(fact.fact_id,),
            partial_amount=Decimal("500.00"),
            currency="CNY",
            approval_hash="response-approval",
        )
        issue = self.ledger.add_dispute_issue_candidate(
            self.assistant,
            question="合成付款应否抵扣争议本金？",
            claim_ids=(claim.claim_id,),
            confirmed_fact_ids=(fact.fact_id,),
        )
        self.ledger.confirm_dispute_issue(self.lead, issue_id=issue.issue_id, approval_hash="issue-approval")
        snapshot = self.ledger.build_formal_snapshot(self.lead)

        self.assertEqual(len(snapshot.facts), 1)
        self.assertEqual(snapshot.claims[0].currency, "CNY")
        self.assertEqual(len(snapshot.responses), 1)
        self.assertEqual(snapshot.responses[0].partial_amount, Decimal("500.00"))
        self.assertEqual(snapshot.issues[0].status.value, "CONFIRMED")

    def test_fact_change_removes_dependent_response_and_invalidates_issue(self) -> None:
        fact = self._confirmed_fact()
        claim = self.ledger.add_claim_candidate(
            self.assistant,
            original_claim_text="原告主张合成本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=(evidence_link(),),
        )
        self.ledger.confirm_claim_scope(self.lead, claim_id=claim.claim_id, confirmation_hash="claim-scope")
        self.ledger.set_claim_response(
            self.lead,
            claim_id=claim.claim_id,
            position=ClaimResponsePosition.DISPUTE,
            confirmed_fact_ids=(fact.fact_id,),
            partial_amount=None,
            currency=None,
            approval_hash="response-approval",
        )
        issue = self.ledger.add_dispute_issue_candidate(
            self.assistant,
            question="合成付款性质是否明确？",
            claim_ids=(claim.claim_id,),
            confirmed_fact_ids=(fact.fact_id,),
        )
        self.ledger.confirm_dispute_issue(self.lead, issue_id=issue.issue_id, approval_hash="issue-approval")
        self.ledger.decide_fact(self.lead, fact_id=fact.fact_id, status=FactStatus.DISPUTED, decision_hash="fact-changed")
        with self.assertRaisesRegex(FactLedgerBlocked, "every confirmed claim"):
            self.ledger.build_formal_snapshot(self.lead)


if __name__ == "__main__":
    unittest.main()
