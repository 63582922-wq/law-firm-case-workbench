from datetime import date
from decimal import Decimal
from hashlib import sha256
import unittest

from case_kernel.calculation_engine import EventKind, PaymentApplication
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.models import Actor, Role
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    DuplicateStatus,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
    TransactionLedger,
    TransactionLedgerBlocked,
)


def evidence_link(*, evidence_id: str = "alpha_transaction_evidence") -> EvidenceLink:
    return EvidenceLink(
        evidence_id=evidence_id,
        original_file_sha256=sha256(evidence_id.encode("utf-8")).hexdigest(),
        page_number=9,
        region_id="alpha_transaction_region",
        original_label="合成交易流水第9页",
    )


class TransactionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
        self.lead = Actor("alpha_lead_lawyer", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
        self.ledger = TransactionLedger()

    def _transaction(self, *, amount: Decimal = Decimal("1000.00"), evidence_id: str = "alpha_transaction_evidence"):
        return self.ledger.add_transaction_candidate(
            self.assistant,
            local_date=date(2020, 9, 4),
            date_precision=DatePrecision.EXACT_DATE,
            amount=amount,
            currency="CNY",
            direction=TransactionDirection.INCOMING,
            payer_label="合成付款方",
            payee_label="合成收款方",
            channel=TransactionChannel.WECHAT,
            transaction_reference="alpha-transfer-reference",
            evidence_links=(evidence_link(evidence_id=evidence_id),),
        )

    def _approved_classification(self, transaction_id: str, *, nature: PaymentNature = PaymentNature.REPAYMENT_UNSPECIFIED):
        proposal = self.ledger.add_payment_classification_candidate(
            self.assistant,
            transaction_id=transaction_id,
            origin=ClassificationOrigin.AGENT_CANDIDATE,
            nature=nature,
            allocations=(ObligationAllocation("obligation_alpha_001", Decimal("1000.00"), "CNY"),),
            same_day_sequence=1,
            evidence_links=(evidence_link(evidence_id="alpha_classification_evidence"),),
        )
        return self.ledger.approve_payment_classification(self.lead, proposal_id=proposal.proposal_id, approval_hash="classification-approval")

    def test_source_transaction_must_be_confirmed_before_payment_classification_is_approved(self) -> None:
        transaction = self._transaction()
        proposal = self.ledger.add_payment_classification_candidate(
            self.assistant,
            transaction_id=transaction.transaction_id,
            origin=ClassificationOrigin.DEFENDANT_STATEMENT,
            nature=PaymentNature.REPAYMENT_UNSPECIFIED,
            allocations=(ObligationAllocation("obligation_alpha_001", Decimal("1000.00"), "CNY"),),
            same_day_sequence=1,
            evidence_links=(evidence_link(evidence_id="alpha_classification_evidence"),),
        )

        with self.assertRaisesRegex(TransactionLedgerBlocked, "source transaction"):
            self.ledger.approve_payment_classification(self.lead, proposal_id=proposal.proposal_id, approval_hash="classification-approval")

    def test_payment_allocations_must_match_the_full_original_transaction_amount(self) -> None:
        transaction = self._transaction()
        with self.assertRaisesRegex(TransactionLedgerBlocked, "must equal"):
            self.ledger.add_payment_classification_candidate(
                self.assistant,
                transaction_id=transaction.transaction_id,
                origin=ClassificationOrigin.AGENT_CANDIDATE,
                nature=PaymentNature.REPAYMENT_UNSPECIFIED,
                allocations=(ObligationAllocation("obligation_alpha_001", Decimal("999.99"), "CNY"),),
                same_day_sequence=1,
                evidence_links=(evidence_link(evidence_id="alpha_classification_evidence"),),
            )

    def test_interest_only_classification_preserves_the_lawyer_decided_application(self) -> None:
        transaction = self._transaction()
        self.ledger.confirm_transaction(self.lead, transaction_id=transaction.transaction_id, confirmation_hash="transaction-confirmation")
        self._approved_classification(transaction.transaction_id, nature=PaymentNature.INTEREST_PAYMENT)

        snapshot = self.ledger.build_calculation_snapshot(self.lead, obligation_id="obligation_alpha_001")

        self.assertEqual(len(snapshot.events), 1)
        self.assertEqual(snapshot.events[0].kind, EventKind.PAYMENT)
        self.assertEqual(snapshot.events[0].payment_application, PaymentApplication.INTEREST_ONLY)
        self.assertEqual(snapshot.events[0].amount, Decimal("1000.00"))

    def test_unresolved_duplicate_blocks_and_same_event_resolution_keeps_only_canonical_transaction(self) -> None:
        first = self._transaction(evidence_id="alpha_first_transaction")
        second = self._transaction(evidence_id="alpha_second_transaction")
        self.ledger.confirm_transaction(self.lead, transaction_id=first.transaction_id, confirmation_hash="first-confirmation")
        self.ledger.confirm_transaction(self.lead, transaction_id=second.transaction_id, confirmation_hash="second-confirmation")
        self._approved_classification(first.transaction_id)
        self._approved_classification(second.transaction_id)
        group = self.ledger.add_duplicate_group_candidate(
            self.assistant,
            transaction_ids=(first.transaction_id, second.transaction_id),
        )

        with self.assertRaisesRegex(TransactionLedgerBlocked, "unresolved duplicate"):
            self.ledger.build_calculation_snapshot(self.lead, obligation_id="obligation_alpha_001")

        resolved = self.ledger.resolve_duplicate_group(
            self.lead,
            group_id=group.group_id,
            same_economic_event=True,
            canonical_transaction_id=first.transaction_id,
            approval_hash="duplicate-resolution",
        )
        snapshot = self.ledger.build_calculation_snapshot(self.lead, obligation_id="obligation_alpha_001")

        self.assertEqual(resolved.status, DuplicateStatus.SAME_ECONOMIC_EVENT)
        self.assertEqual(snapshot.included_transaction_ids, (first.transaction_id,))
        self.assertEqual(len(snapshot.events), 1)

    def test_invalidating_a_transaction_revokes_the_approved_payment_classification(self) -> None:
        transaction = self._transaction()
        self.ledger.confirm_transaction(self.lead, transaction_id=transaction.transaction_id, confirmation_hash="transaction-confirmation")
        self._approved_classification(transaction.transaction_id)
        self.ledger.invalidate_transaction(self.lead, transaction_id=transaction.transaction_id, reason_hash="source-corrected")

        with self.assertRaisesRegex(TransactionLedgerBlocked, "no approved classified"):
            self.ledger.build_calculation_snapshot(self.lead, obligation_id="obligation_alpha_001")


if __name__ == "__main__":
    unittest.main()
