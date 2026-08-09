"""A read-only, evidence-bound synthetic review fixture for the Alpha workbench."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256

from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactClaimLedger, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
    TransactionLedger,
)


def build_alpha_review_state() -> dict[str, object]:
    """Build a fresh internal fixture through the same domain gates as later UI data.

    IDs and records are synthetic and never include a real person, court, account,
    or case. The endpoint is read-only so it cannot be mistaken for case intake.
    """
    lead = Actor("alpha_lead_lawyer", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
    assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
    fact_ledger = FactClaimLedger()
    payment_evidence = _evidence("alpha_evidence_payment", "合成微信流水第17页", 17)
    claim_evidence = _evidence("alpha_evidence_claim", "合成起诉材料第3页", 3)

    fact = fact_ledger.add_fact_candidate(
        assistant,
        original_text="合成流水记录显示一笔人民币1,000.00元付款。",
        origin=AssertionOrigin.DEFENDANT_STATEMENT,
        evidence_links=(payment_evidence,),
    )
    fact = fact_ledger.decide_fact(lead, fact_id=fact.fact_id, status=FactStatus.CONFIRMED, decision_hash="alpha-fact-approved")
    claim = fact_ledger.add_claim_candidate(
        assistant,
        original_claim_text="原告主张合成本金10,000.00元及相应利息。",
        claimed_amount=Decimal("10000.00"),
        currency="CNY",
        evidence_links=(claim_evidence,),
    )
    claim = fact_ledger.confirm_claim_scope(lead, claim_id=claim.claim_id, confirmation_hash="alpha-claim-approved")
    fact_ledger.set_claim_response(
        lead,
        claim_id=claim.claim_id,
        position=ClaimResponsePosition.PARTIALLY_ADMIT,
        confirmed_fact_ids=(fact.fact_id,),
        partial_amount=Decimal("9000.00"),
        currency="CNY",
        approval_hash="alpha-response-approved",
    )
    issue = fact_ledger.add_dispute_issue_candidate(
        assistant,
        question="合成付款在本案中应如何分配与抵扣？",
        claim_ids=(claim.claim_id,),
        confirmed_fact_ids=(fact.fact_id,),
    )
    fact_ledger.confirm_dispute_issue(lead, issue_id=issue.issue_id, approval_hash="alpha-issue-approved")
    fact_snapshot = fact_ledger.build_formal_snapshot(lead)

    transactions = TransactionLedger()
    disbursement = _transaction(
        transactions, assistant, date(2020, 8, 20), Decimal("10000.00"), "alpha_evidence_disbursement", "合成借款凭证第1页"
    )
    payment = _transaction(
        transactions, assistant, date(2020, 9, 4), Decimal("1000.00"), "alpha_evidence_payment", "合成微信流水第17页"
    )
    for transaction, nature, sequence in (
        (disbursement, PaymentNature.DISBURSEMENT, 1),
        (payment, PaymentNature.INTEREST_PAYMENT, 1),
    ):
        transactions.confirm_transaction(lead, transaction_id=transaction.transaction_id, confirmation_hash=f"{transaction.transaction_id}-confirmed")
        proposal = transactions.add_payment_classification_candidate(
            assistant,
            transaction_id=transaction.transaction_id,
            origin=ClassificationOrigin.ASSISTANT_ENTRY,
            nature=nature,
            allocations=(ObligationAllocation("alpha_obligation_001", transaction.amount, "CNY"),),
            same_day_sequence=sequence,
            evidence_links=transaction.evidence_links,
        )
        transactions.approve_payment_classification(lead, proposal_id=proposal.proposal_id, approval_hash=f"{proposal.proposal_id}-approved")
    transaction_snapshot = transactions.build_calculation_snapshot(lead, obligation_id="alpha_obligation_001")
    return {"fact_snapshot": fact_snapshot, "transaction_snapshot": transaction_snapshot}


def _transaction(
    ledger: TransactionLedger,
    actor: Actor,
    local_date: date,
    amount: Decimal,
    evidence_id: str,
    original_label: str,
):
    return ledger.add_transaction_candidate(
        actor,
        local_date=local_date,
        date_precision=DatePrecision.EXACT_DATE,
        amount=amount,
        currency="CNY",
        direction=TransactionDirection.INCOMING,
        payer_label="合成付款方",
        payee_label="合成收款方",
        channel=TransactionChannel.WECHAT,
        transaction_reference=f"SYNTHETIC-{evidence_id}",
        evidence_links=(_evidence(evidence_id, original_label, 17),),
    )


def _evidence(evidence_id: str, original_label: str, page_number: int) -> EvidenceLink:
    return EvidenceLink(
        evidence_id=evidence_id,
        original_file_sha256=sha256(evidence_id.encode("utf-8")).hexdigest(),
        page_number=page_number,
        region_id=f"{evidence_id}_region",
        original_label=original_label,
    )
