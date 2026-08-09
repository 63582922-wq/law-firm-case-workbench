from datetime import date, datetime, timezone
from hashlib import sha256
import unittest

from case_kernel.evidence_refs import EvidenceLink
from case_kernel.legal_rules import OfficialSourceSnapshot, SourceLicenseStatus, SourceVerificationStatus
from case_kernel.models import Actor, Role
from case_kernel.procedure_deadline_ledger import (
    DayBasis,
    DueDateAdjustment,
    ProcedureDeadlineBlocked,
    ProcedureDeadlineLedger,
    ServiceMethod,
    StartCounting,
)


def evidence_link(*, evidence_id: str) -> EvidenceLink:
    return EvidenceLink(
        evidence_id=evidence_id,
        original_file_sha256=sha256(evidence_id.encode("utf-8")).hexdigest(),
        page_number=1,
        region_id="alpha_procedure_region",
        original_label=f"合成法院材料：{evidence_id}",
    )


def official_source() -> OfficialSourceSnapshot:
    return OfficialSourceSnapshot(
        snapshot_id="alpha_official_deadline_source",
        source_id="alpha_court_source",
        official_url="https://court.example.invalid/alpha-procedure-rule",
        source_tier="SYNTHETIC_OFFICIAL_SOURCE",
        retrieved_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        content_sha256=sha256(b"synthetic procedural source").hexdigest(),
        verification_status=SourceVerificationStatus.VERIFIED,
        license_status=SourceLicenseStatus.ACTIVE,
    )


class ProcedureDeadlineLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = Actor("alpha_assistant", "alpha_firm_001", frozenset({Role.ASSISTANT}))
        self.lead = Actor("alpha_lead", "alpha_firm_001", frozenset({Role.LEAD_LAWYER}))
        self.ledger = ProcedureDeadlineLedger()

    def _confirmed_case(self):
        court_case = self.ledger.add_court_case_candidate(
            self.assistant,
            court_name="合成市合成区人民法院",
            case_number="（2026）合成0101民初001号",
            evidence_links=(evidence_link(evidence_id="court-document"),),
        )
        return self.ledger.confirm_court_case(self.lead, court_case_id=court_case.court_case_id, confirmation_hash="court-approved")

    def _approved_rule_and_calendar(self):
        rule = self.ledger.add_deadline_rule_candidate(
            self.assistant,
            rule_version="SYNTHETIC-DEADLINE-RULE-1",
            label="合成答辩期限规则",
            applicable_service_methods=(ServiceMethod.ELECTRONIC,),
            period_count=2,
            day_basis=DayBasis.WORKING_DAYS,
            start_counting=StartCounting.NEXT_DAY_IS_DAY_ONE,
            due_date_adjustment=DueDateAdjustment.NEXT_WORKING_DAY,
            source=official_source(),
            source_rule_locator="synthetic-section-1",
        )
        self.ledger.approve_deadline_rule(self.lead, rule_id=rule.rule_id, approval_hash="rule-approved")
        calendar = self.ledger.add_holiday_calendar_candidate(
            self.assistant,
            calendar_version="SYNTHETIC-CALENDAR-2026-1",
            jurisdiction_label="合成地域",
            non_working_dates=(),
            working_override_dates=(),
            source=official_source(),
        )
        return rule, self.ledger.approve_holiday_calendar(self.lead, calendar_id=calendar.calendar_id, approval_hash="calendar-approved")

    def test_service_requires_confirmed_court_case(self) -> None:
        court_case = self.ledger.add_court_case_candidate(
            self.assistant,
            court_name="合成市合成区人民法院",
            case_number="（2026）合成0101民初001号",
            evidence_links=(evidence_link(evidence_id="court-document"),),
        )
        service = self.ledger.add_service_candidate(
            self.assistant,
            court_case_id=court_case.court_case_id,
            method=ServiceMethod.ELECTRONIC,
            served_on=date(2026, 8, 7),
            is_exact_date=True,
            evidence_links=(evidence_link(evidence_id="service-receipt"),),
        )

        with self.assertRaisesRegex(ProcedureDeadlineBlocked, "confirmed court case"):
            self.ledger.confirm_service(self.lead, service_id=service.service_id, confirmation_hash="service-approved")

    def test_working_day_candidate_is_not_formal_until_lawyer_confirms_it(self) -> None:
        court_case = self._confirmed_case()
        service = self.ledger.add_service_candidate(
            self.assistant,
            court_case_id=court_case.court_case_id,
            method=ServiceMethod.ELECTRONIC,
            served_on=date(2026, 8, 7),  # Friday
            is_exact_date=True,
            evidence_links=(evidence_link(evidence_id="service-receipt"),),
        )
        self.ledger.confirm_service(self.lead, service_id=service.service_id, confirmation_hash="service-approved")
        rule, calendar = self._approved_rule_and_calendar()
        candidate = self.ledger.create_deadline_candidate(
            self.assistant,
            service_id=service.service_id,
            rule_id=rule.rule_id,
            calendar_id=calendar.calendar_id,
            time_zone="Asia/Shanghai",
        )

        self.assertEqual(candidate.start_date, date(2026, 8, 8))
        self.assertEqual(candidate.candidate_due_date, date(2026, 8, 11))
        self.ledger.confirm_deadline(self.lead, deadline_id=candidate.deadline_id, confirmation_hash="deadline-approved")
        snapshot = self.ledger.build_formal_snapshot(self.lead)
        self.assertEqual(snapshot.deadlines[0].candidate_due_date, date(2026, 8, 11))

    def test_inexact_service_date_cannot_generate_a_deadline_candidate(self) -> None:
        court_case = self._confirmed_case()
        service = self.ledger.add_service_candidate(
            self.assistant,
            court_case_id=court_case.court_case_id,
            method=ServiceMethod.ELECTRONIC,
            served_on=None,
            is_exact_date=False,
            evidence_links=(evidence_link(evidence_id="service-month-only"),),
        )
        self.ledger.confirm_service(self.lead, service_id=service.service_id, confirmation_hash="service-approved")
        rule, calendar = self._approved_rule_and_calendar()

        with self.assertRaisesRegex(ProcedureDeadlineBlocked, "exact service date"):
            self.ledger.create_deadline_candidate(
                self.assistant,
                service_id=service.service_id,
                rule_id=rule.rule_id,
                calendar_id=calendar.calendar_id,
                time_zone="Asia/Shanghai",
            )

    def test_changing_the_court_case_invalidates_its_confirmed_deadline(self) -> None:
        court_case = self._confirmed_case()
        service = self.ledger.add_service_candidate(
            self.assistant,
            court_case_id=court_case.court_case_id,
            method=ServiceMethod.ELECTRONIC,
            served_on=date(2026, 8, 7),
            is_exact_date=True,
            evidence_links=(evidence_link(evidence_id="service-receipt"),),
        )
        self.ledger.confirm_service(self.lead, service_id=service.service_id, confirmation_hash="service-approved")
        rule, calendar = self._approved_rule_and_calendar()
        deadline = self.ledger.create_deadline_candidate(
            self.assistant,
            service_id=service.service_id,
            rule_id=rule.rule_id,
            calendar_id=calendar.calendar_id,
            time_zone="Asia/Shanghai",
        )
        self.ledger.confirm_deadline(self.lead, deadline_id=deadline.deadline_id, confirmation_hash="deadline-approved")
        self.ledger.invalidate_court_case(self.lead, court_case_id=court_case.court_case_id, reason_hash="case-number-corrected")

        with self.assertRaisesRegex(ProcedureDeadlineBlocked, "confirmed court case"):
            self.ledger.build_formal_snapshot(self.lead)


if __name__ == "__main__":
    unittest.main()
