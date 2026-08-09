from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import unittest

from case_kernel.legal_rules import (
    ApplicabilityRule,
    ApprovedLegalEvent,
    CandidateStatus,
    InMemoryLegalBundleRegistry,
    LegalBundleApproval,
    LegalBundleStatus,
    LegalEventKind,
    LegalRuleBlocked,
    OfficialSourceSnapshot,
    RuleResolution,
    RuleReviewStatus,
    SourceLicenseStatus,
    SourceVerificationStatus,
    approve_case_legal_bundle,
    prepare_case_legal_bundle,
)


def source_fixture() -> OfficialSourceSnapshot:
    return OfficialSourceSnapshot(
        snapshot_id="alpha_snapshot_001",
        source_id="SYNTHETIC-LEGAL-SOURCE-001",
        official_url="https://synthetic.invalid/rule-source",
        source_tier="SYNTHETIC_FIXTURE",
        retrieved_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        content_sha256=sha256(b"synthetic source snapshot").hexdigest(),
        verification_status=SourceVerificationStatus.VERIFIED,
        license_status=SourceLicenseStatus.ACTIVE,
    )


def event_fixture() -> ApprovedLegalEvent:
    return ApprovedLegalEvent(
        event_id="alpha_contract_001",
        kind=LegalEventKind.CONTRACT_SIGNED,
        local_date=date(2020, 8, 20),
        source_evidence_ids=("alpha_evidence_contract",),
        approved_by="alpha_lead_lawyer",
        approval_hash="contract-approval",
    )


def rule_fixture(*, rule_id: str = "alpha_interest_rule_a", version: str = "SYNTHETIC-RULE-A", snapshot: OfficialSourceSnapshot | None = None) -> ApplicabilityRule:
    return ApplicabilityRule(
        rule_id=rule_id,
        version=version,
        issue_key="interest_cap",
        source_snapshot=snapshot or source_fixture(),
        effective_from=date(2020, 8, 20),
        effective_to=None,
        trigger_event_kind=LegalEventKind.CONTRACT_SIGNED,
        required_fact_keys=("rate_basis_confirmed",),
        transition_rule_ids=(),
        conflict_set="interest_cap_transition",
        priority=1,
        review_status=RuleReviewStatus.LAWYER_APPROVED,
    )


class LegalRuleBundleTests(unittest.TestCase):
    def _draft(self, *, rules: tuple[ApplicabilityRule, ...], resolutions: tuple[RuleResolution, ...] = ()): 
        return prepare_case_legal_bundle(
            matter_id="alpha_matter_legal_001",
            version=1,
            required_issue_keys=("interest_cap",),
            approved_events=(event_fixture(),),
            confirmed_fact_keys=frozenset({"rate_basis_confirmed"}),
            rules=rules,
            resolutions=resolutions,
        )

    def test_competing_candidates_block_until_lawyer_selects_one(self) -> None:
        first = rule_fixture()
        second = rule_fixture(rule_id="alpha_interest_rule_b", version="SYNTHETIC-RULE-B")
        blocked = self._draft(rules=(first, second))

        self.assertEqual(blocked.status, LegalBundleStatus.BLOCKED)
        self.assertIn("multiple rule candidates", blocked.blockers[0])

        resolution = RuleResolution(
            issue_key="interest_cap",
            rule_id=first.rule_id,
            version=first.version,
            selected_by="alpha_lead_lawyer",
            selection_reason="synthetic conflict review",
            approval_hash="resolution-approval",
        )
        ready = self._draft(rules=(first, second), resolutions=(resolution,))
        self.assertEqual(ready.status, LegalBundleStatus.AWAITING_LAWYER_APPROVAL)
        self.assertEqual(ready.selections[0].version, "SYNTHETIC-RULE-A")

    def test_approval_binds_current_draft_and_registry_exposes_only_approved_reference(self) -> None:
        draft = self._draft(rules=(rule_fixture(),))
        with self.assertRaisesRegex(LegalRuleBlocked, "bind"):
            approve_case_legal_bundle(
                draft,
                LegalBundleApproval("alpha_lead_lawyer", "wrong-hash", "approval-hash", datetime(2026, 8, 9, tzinfo=timezone.utc)),
            )

        bundle = approve_case_legal_bundle(
            draft,
            LegalBundleApproval("alpha_lead_lawyer", draft.input_hash, "approval-hash", datetime(2026, 8, 9, tzinfo=timezone.utc)),
        )
        registry = InMemoryLegalBundleRegistry((bundle,))
        reference = registry.get_reference(bundle.bundle_id)
        self.assertEqual(reference.bundle_hash, bundle.bundle_hash)
        self.assertEqual(reference.approved_rule_versions, ("SYNTHETIC-RULE-A",))
        with self.assertRaisesRegex(LegalRuleBlocked, "unknown"):
            registry.get_reference("alpha_missing_bundle")

    def test_missing_conditions_and_source_verification_block_candidate(self) -> None:
        missing_conditions = prepare_case_legal_bundle(
            matter_id="alpha_matter_legal_001",
            version=1,
            required_issue_keys=("interest_cap",),
            approved_events=(event_fixture(),),
            confirmed_fact_keys=frozenset(),
            rules=(rule_fixture(),),
        )
        self.assertEqual(missing_conditions.status, LegalBundleStatus.BLOCKED)
        self.assertEqual(missing_conditions.candidates[0].status, CandidateStatus.MISSING_CONDITION)

        unavailable_source = replace(source_fixture(), verification_status=SourceVerificationStatus.UNAVAILABLE)
        unavailable = self._draft(rules=(rule_fixture(snapshot=unavailable_source),))
        self.assertEqual(unavailable.candidates[0].status, CandidateStatus.SOURCE_NOT_READY)

    def test_rule_or_event_change_changes_candidate_input_identity(self) -> None:
        original = self._draft(rules=(rule_fixture(),))
        changed_event_draft = prepare_case_legal_bundle(
            matter_id="alpha_matter_legal_001",
            version=1,
            required_issue_keys=("interest_cap",),
            approved_events=(replace(event_fixture(), local_date=date(2020, 8, 21)),),
            confirmed_fact_keys=frozenset({"rate_basis_confirmed"}),
            rules=(rule_fixture(),),
        )
        self.assertNotEqual(original.input_hash, changed_event_draft.input_hash)

    def test_unordered_rule_input_has_a_stable_candidate_identity(self) -> None:
        first = rule_fixture()
        second = rule_fixture(rule_id="alpha_interest_rule_b", version="SYNTHETIC-RULE-B")
        resolution = RuleResolution(
            issue_key="interest_cap",
            rule_id=first.rule_id,
            version=first.version,
            selected_by="alpha_lead_lawyer",
            selection_reason="synthetic conflict review",
            approval_hash="resolution-approval",
        )
        forward = self._draft(rules=(first, second), resolutions=(resolution,))
        reverse = self._draft(rules=(second, first), resolutions=(resolution,))
        self.assertEqual(forward.input_hash, reverse.input_hash)


if __name__ == "__main__":
    unittest.main()
