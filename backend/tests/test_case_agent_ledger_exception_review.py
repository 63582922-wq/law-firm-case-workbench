from __future__ import annotations

from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_ledger_exception_review import (
    LedgerExceptionDecision,
    LedgerExceptionReason,
    LedgerExceptionReviewBlocked,
    LedgerExceptionRiskPolicy,
    LedgerExceptionSourcePolicy,
    allowed_exception_decisions,
    canonical_reason_codes,
    exception_candidate_set_hash,
    exception_decision_request_hash,
    exception_group_key_hash,
    exception_risk_policy,
    exception_source_policy,
    validate_exception_decision,
)


class LedgerExceptionReviewPolicyTests(unittest.TestCase):
    def test_group_hashes_use_the_same_explicit_canonical_byte_contract_as_sql(self) -> None:
        reasons = ("OCR_DERIVED", "POSSIBLE_DUPLICATE")
        source = LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
        risk = LedgerExceptionRiskPolicy.DUPLICATE_REVIEW
        expected_key = sha256(
            "FACT\nOCR_DERIVED,POSSIBLE_DUPLICATE\n"
            "SOURCE_REVERIFICATION_REQUIRED\nDUPLICATE_REVIEW".encode()
        ).hexdigest()
        self.assertEqual(
            exception_group_key_hash(
                candidate_kind="FACT",
                reason_codes=reversed(reasons),
                source_policy=source,
                risk_policy=risk,
            ),
            expected_key,
        )
        candidates = ("a" * 64, "b" * 64)
        self.assertEqual(
            exception_candidate_set_hash(reversed(candidates)),
            sha256(("a" * 64 + "," + "b" * 64).encode("ascii")).hexdigest(),
        )

    def test_fixed_policy_is_order_independent_and_never_opens_freeform_actions(self) -> None:
        reasons = canonical_reason_codes(
            ("PARTY_AMBIGUOUS", "OCR_DERIVED", "PARTY_AMBIGUOUS")
        )
        self.assertEqual(reasons, ("OCR_DERIVED", "PARTY_AMBIGUOUS"))
        source = exception_source_policy(reasons)
        risk = exception_risk_policy(reasons)
        self.assertEqual(
            source, LedgerExceptionSourcePolicy.SOURCE_REVERIFICATION_REQUIRED
        )
        self.assertEqual(
            risk,
            LedgerExceptionRiskPolicy.MISSING_FIELDS_OR_AMBIGUITY_REVIEW,
        )
        self.assertEqual(
            allowed_exception_decisions(
                reason_codes=reasons,
                source_policy=source,
                risk_policy=risk,
            ),
            (
                LedgerExceptionDecision.REQUEST_REEXTRACTION,
                LedgerExceptionDecision.REQUEST_MORE_EVIDENCE,
                LedgerExceptionDecision.DEFER_WITH_REASON,
            ),
        )

    def test_defer_requires_a_bounded_structured_reason(self) -> None:
        with self.assertRaises(LedgerExceptionReviewBlocked):
            validate_exception_decision(
                decision=LedgerExceptionDecision.DEFER_WITH_REASON,
                reason=LedgerExceptionReason.NEEDS_LEAD_REVIEW,
                reason_note=None,
                group_reason_codes=("LOW_CONFIDENCE",),
                source_policy=LedgerExceptionSourcePolicy.NATIVE_SOURCE_REVIEW,
                risk_policy=LedgerExceptionRiskPolicy.LOW_CONFIDENCE_REVIEW,
            )

    def test_session_request_hash_is_length_bound_and_stable(self) -> None:
        matter_id = str(uuid4())
        group_id = str(uuid4())
        note = "等待当事人补充原始流水"
        canonical = "\n".join(
            (
                "case-ledger-exception-group-request-v1",
                matter_id,
                "9",
                group_id,
                "DEFER_WITH_REASON",
                "AWAITING_CLIENT_INPUT",
                f"{len(note.encode('utf-8'))}:{note}",
            )
        )
        self.assertEqual(
            exception_decision_request_hash(
                matter_id=matter_id,
                expected_version=9,
                exception_group_id=group_id,
                decision=LedgerExceptionDecision.DEFER_WITH_REASON,
                reason=LedgerExceptionReason.AWAITING_CLIENT_INPUT,
                reason_note=note,
            ),
            sha256(canonical.encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
