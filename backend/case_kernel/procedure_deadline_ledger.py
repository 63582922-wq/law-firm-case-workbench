"""Court, service, and lawyer-confirmed procedural-deadline candidates for synthetic Alpha.

The module calculates only a declared rule configuration against confirmed input
dates.  It never chooses the applicable procedure rule, service date, holiday
calendar, or court practice; those remain explicit lawyer decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from enum import Enum
from hashlib import sha256
import json
from uuid import uuid4

from .evidence_refs import EvidenceLink, EvidenceReferenceBlocked, validate_evidence_links
from .legal_rules import OfficialSourceSnapshot, SourceLicenseStatus, SourceVerificationStatus
from .models import Actor, Role


class ProcedureDeadlineBlocked(ValueError):
    """A court, service, rule, calendar, or deadline is not safe to rely on."""


class RecordStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"


class ServiceMethod(str, Enum):
    ELECTRONIC = "ELECTRONIC"
    POSTAL = "POSTAL"
    PERSONAL = "PERSONAL"
    PUBLIC_NOTICE = "PUBLIC_NOTICE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class DayBasis(str, Enum):
    CALENDAR_DAYS = "CALENDAR_DAYS"
    WORKING_DAYS = "WORKING_DAYS"


class StartCounting(str, Enum):
    EVENT_DAY_IS_DAY_ONE = "EVENT_DAY_IS_DAY_ONE"
    NEXT_DAY_IS_DAY_ONE = "NEXT_DAY_IS_DAY_ONE"


class DueDateAdjustment(str, Enum):
    NONE = "NONE"
    NEXT_WORKING_DAY = "NEXT_WORKING_DAY"


@dataclass(frozen=True)
class CourtCase:
    court_case_id: str
    court_name: str
    case_number: str
    evidence_links: tuple[EvidenceLink, ...]
    status: RecordStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class ServiceRecord:
    service_id: str
    court_case_id: str
    method: ServiceMethod
    served_on: date | None
    is_exact_date: bool
    evidence_links: tuple[EvidenceLink, ...]
    status: RecordStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class DeadlineRule:
    rule_id: str
    rule_version: str
    label: str
    applicable_service_methods: tuple[ServiceMethod, ...]
    period_count: int
    day_basis: DayBasis
    start_counting: StartCounting
    due_date_adjustment: DueDateAdjustment
    source_snapshot_id: str
    source_official_url: str
    source_content_sha256: str
    source_rule_locator: str
    status: RecordStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class HolidayCalendar:
    calendar_id: str
    calendar_version: str
    jurisdiction_label: str
    non_working_dates: tuple[date, ...]
    working_override_dates: tuple[date, ...]
    source_snapshot_id: str
    source_official_url: str
    source_content_sha256: str
    status: RecordStatus
    approved_by: str | None
    approval_hash: str | None


@dataclass(frozen=True)
class DeadlineCandidate:
    deadline_id: str
    court_case_id: str
    service_id: str
    rule_id: str
    rule_version: str
    calendar_id: str
    calendar_version: str
    time_zone: str
    start_date: date
    candidate_due_date: date
    input_hash: str
    status: RecordStatus
    confirmed_by: str | None
    confirmation_hash: str | None


@dataclass(frozen=True)
class ProcedureDeadlineSnapshot:
    snapshot_id: str
    ledger_version: int
    input_hash: str
    court_cases: tuple[CourtCase, ...]
    services: tuple[ServiceRecord, ...]
    deadlines: tuple[DeadlineCandidate, ...]


class ProcedureDeadlineLedger:
    """Stores deadline candidates; a candidate is not a reliance-ready countdown."""

    def __init__(self) -> None:
        self._court_cases: dict[str, CourtCase] = {}
        self._services: dict[str, ServiceRecord] = {}
        self._rules: dict[str, DeadlineRule] = {}
        self._calendars: dict[str, HolidayCalendar] = {}
        self._deadlines: dict[str, DeadlineCandidate] = {}
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def add_court_case_candidate(
        self,
        actor: Actor,
        *,
        court_name: str,
        case_number: str,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> CourtCase:
        _require_candidate_role(actor)
        _require_text(court_name, "court name")
        _require_text(case_number, "case number")
        _validate_evidence(evidence_links)
        record = CourtCase(
            court_case_id=f"court_case_{uuid4().hex}",
            court_name=court_name.strip(),
            case_number=case_number.strip(),
            evidence_links=evidence_links,
            status=RecordStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._court_cases[record.court_case_id] = record
        self._version += 1
        return record

    def confirm_court_case(self, actor: Actor, *, court_case_id: str, confirmation_hash: str) -> CourtCase:
        _require_lead(actor)
        _require_text(confirmation_hash, "court case confirmation hash")
        record = self._require_court_case(court_case_id)
        if record.status is not RecordStatus.CANDIDATE:
            raise ProcedureDeadlineBlocked("only a court case candidate can be confirmed")
        confirmed = CourtCase(
            court_case_id=record.court_case_id,
            court_name=record.court_name,
            case_number=record.case_number,
            evidence_links=record.evidence_links,
            status=RecordStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._court_cases[court_case_id] = confirmed
        self._version += 1
        return confirmed

    def invalidate_court_case(self, actor: Actor, *, court_case_id: str, reason_hash: str) -> CourtCase:
        _require_lead(actor)
        _require_text(reason_hash, "court case invalidation hash")
        record = self._require_court_case(court_case_id)
        if record.status is RecordStatus.INVALIDATED:
            raise ProcedureDeadlineBlocked("an invalidated court case must be rebuilt from original evidence")
        invalidated = CourtCase(
            court_case_id=record.court_case_id,
            court_name=record.court_name,
            case_number=record.case_number,
            evidence_links=record.evidence_links,
            status=RecordStatus.INVALIDATED,
            confirmed_by=actor.actor_id,
            confirmation_hash=reason_hash,
        )
        self._court_cases[court_case_id] = invalidated
        self._invalidate_case_dependents({court_case_id})
        self._version += 1
        return invalidated

    def add_service_candidate(
        self,
        actor: Actor,
        *,
        court_case_id: str,
        method: ServiceMethod,
        served_on: date | None,
        is_exact_date: bool,
        evidence_links: tuple[EvidenceLink, ...],
    ) -> ServiceRecord:
        _require_candidate_role(actor)
        court_case = self._require_court_case(court_case_id)
        if court_case.status is RecordStatus.INVALIDATED:
            raise ProcedureDeadlineBlocked("a service record cannot attach to an invalidated court case")
        if is_exact_date and served_on is None:
            raise ProcedureDeadlineBlocked("an exact service date requires served_on")
        if not is_exact_date and served_on is not None:
            raise ProcedureDeadlineBlocked("an inexact service date cannot be represented as an exact date")
        _validate_evidence(evidence_links)
        service = ServiceRecord(
            service_id=f"service_{uuid4().hex}",
            court_case_id=court_case_id,
            method=method,
            served_on=served_on,
            is_exact_date=is_exact_date,
            evidence_links=evidence_links,
            status=RecordStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._services[service.service_id] = service
        self._version += 1
        return service

    def confirm_service(self, actor: Actor, *, service_id: str, confirmation_hash: str) -> ServiceRecord:
        _require_lead(actor)
        _require_text(confirmation_hash, "service confirmation hash")
        service = self._require_service(service_id)
        if service.status is not RecordStatus.CANDIDATE:
            raise ProcedureDeadlineBlocked("only a service candidate can be confirmed")
        if self._require_court_case(service.court_case_id).status is not RecordStatus.CONFIRMED:
            raise ProcedureDeadlineBlocked("a service record requires a confirmed court case")
        confirmed = ServiceRecord(
            service_id=service.service_id,
            court_case_id=service.court_case_id,
            method=service.method,
            served_on=service.served_on,
            is_exact_date=service.is_exact_date,
            evidence_links=service.evidence_links,
            status=RecordStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._services[service_id] = confirmed
        self._version += 1
        return confirmed

    def invalidate_service(self, actor: Actor, *, service_id: str, reason_hash: str) -> ServiceRecord:
        _require_lead(actor)
        _require_text(reason_hash, "service invalidation hash")
        service = self._require_service(service_id)
        if service.status is RecordStatus.INVALIDATED:
            raise ProcedureDeadlineBlocked("an invalidated service record must be rebuilt from original evidence")
        invalidated = ServiceRecord(
            service_id=service.service_id,
            court_case_id=service.court_case_id,
            method=service.method,
            served_on=service.served_on,
            is_exact_date=service.is_exact_date,
            evidence_links=service.evidence_links,
            status=RecordStatus.INVALIDATED,
            confirmed_by=actor.actor_id,
            confirmation_hash=reason_hash,
        )
        self._services[service_id] = invalidated
        self._invalidate_deadlines_for_services({service_id})
        self._version += 1
        return invalidated

    def add_deadline_rule_candidate(
        self,
        actor: Actor,
        *,
        rule_version: str,
        label: str,
        applicable_service_methods: tuple[ServiceMethod, ...],
        period_count: int,
        day_basis: DayBasis,
        start_counting: StartCounting,
        due_date_adjustment: DueDateAdjustment,
        source: OfficialSourceSnapshot,
        source_rule_locator: str,
    ) -> DeadlineRule:
        _require_candidate_role(actor)
        _require_text(rule_version, "deadline rule version")
        _require_text(label, "deadline rule label")
        _require_text(source_rule_locator, "deadline rule locator")
        _validate_official_source(source)
        if not applicable_service_methods or ServiceMethod.UNKNOWN in applicable_service_methods:
            raise ProcedureDeadlineBlocked("a deadline rule must declare known applicable service methods")
        if period_count < 1:
            raise ProcedureDeadlineBlocked("deadline rule period count must be positive")
        rule = DeadlineRule(
            rule_id=f"deadline_rule_{uuid4().hex}",
            rule_version=rule_version.strip(),
            label=label.strip(),
            applicable_service_methods=tuple(sorted(set(applicable_service_methods), key=lambda item: item.value)),
            period_count=period_count,
            day_basis=day_basis,
            start_counting=start_counting,
            due_date_adjustment=due_date_adjustment,
            source_snapshot_id=source.snapshot_id,
            source_official_url=source.official_url,
            source_content_sha256=source.content_sha256,
            source_rule_locator=source_rule_locator.strip(),
            status=RecordStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._rules[rule.rule_id] = rule
        self._version += 1
        return rule

    def approve_deadline_rule(self, actor: Actor, *, rule_id: str, approval_hash: str) -> DeadlineRule:
        _require_lead(actor)
        _require_text(approval_hash, "deadline rule approval hash")
        rule = self._rules.get(rule_id)
        if rule is None or rule.status is not RecordStatus.CANDIDATE:
            raise ProcedureDeadlineBlocked("only a deadline rule candidate can be approved")
        approved = DeadlineRule(
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            label=rule.label,
            applicable_service_methods=rule.applicable_service_methods,
            period_count=rule.period_count,
            day_basis=rule.day_basis,
            start_counting=rule.start_counting,
            due_date_adjustment=rule.due_date_adjustment,
            source_snapshot_id=rule.source_snapshot_id,
            source_official_url=rule.source_official_url,
            source_content_sha256=rule.source_content_sha256,
            source_rule_locator=rule.source_rule_locator,
            status=RecordStatus.CONFIRMED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._rules[rule_id] = approved
        self._version += 1
        return approved

    def add_holiday_calendar_candidate(
        self,
        actor: Actor,
        *,
        calendar_version: str,
        jurisdiction_label: str,
        non_working_dates: tuple[date, ...],
        working_override_dates: tuple[date, ...],
        source: OfficialSourceSnapshot,
    ) -> HolidayCalendar:
        _require_candidate_role(actor)
        _require_text(calendar_version, "holiday calendar version")
        _require_text(jurisdiction_label, "holiday calendar jurisdiction")
        _validate_official_source(source)
        normalized_non_working = tuple(sorted(set(non_working_dates)))
        normalized_overrides = tuple(sorted(set(working_override_dates)))
        if set(normalized_non_working) & set(normalized_overrides):
            raise ProcedureDeadlineBlocked("a calendar date cannot be both non-working and a working override")
        calendar = HolidayCalendar(
            calendar_id=f"holiday_calendar_{uuid4().hex}",
            calendar_version=calendar_version.strip(),
            jurisdiction_label=jurisdiction_label.strip(),
            non_working_dates=normalized_non_working,
            working_override_dates=normalized_overrides,
            source_snapshot_id=source.snapshot_id,
            source_official_url=source.official_url,
            source_content_sha256=source.content_sha256,
            status=RecordStatus.CANDIDATE,
            approved_by=None,
            approval_hash=None,
        )
        self._calendars[calendar.calendar_id] = calendar
        self._version += 1
        return calendar

    def approve_holiday_calendar(self, actor: Actor, *, calendar_id: str, approval_hash: str) -> HolidayCalendar:
        _require_lead(actor)
        _require_text(approval_hash, "holiday calendar approval hash")
        calendar = self._calendars.get(calendar_id)
        if calendar is None or calendar.status is not RecordStatus.CANDIDATE:
            raise ProcedureDeadlineBlocked("only a holiday calendar candidate can be approved")
        approved = HolidayCalendar(
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.calendar_version,
            jurisdiction_label=calendar.jurisdiction_label,
            non_working_dates=calendar.non_working_dates,
            working_override_dates=calendar.working_override_dates,
            source_snapshot_id=calendar.source_snapshot_id,
            source_official_url=calendar.source_official_url,
            source_content_sha256=calendar.source_content_sha256,
            status=RecordStatus.CONFIRMED,
            approved_by=actor.actor_id,
            approval_hash=approval_hash,
        )
        self._calendars[calendar_id] = approved
        self._version += 1
        return approved

    def create_deadline_candidate(
        self,
        actor: Actor,
        *,
        service_id: str,
        rule_id: str,
        calendar_id: str,
        time_zone: str,
    ) -> DeadlineCandidate:
        _require_candidate_role(actor)
        _require_text(time_zone, "deadline time zone")
        service = self._require_service(service_id)
        rule = self._require_rule(rule_id)
        calendar = self._require_calendar(calendar_id)
        if service.status is not RecordStatus.CONFIRMED or not service.is_exact_date or service.served_on is None:
            raise ProcedureDeadlineBlocked("deadline candidate requires a lawyer-confirmed exact service date")
        if rule.status is not RecordStatus.CONFIRMED or calendar.status is not RecordStatus.CONFIRMED:
            raise ProcedureDeadlineBlocked("deadline candidate requires lawyer-approved rule and holiday calendar")
        if service.method not in rule.applicable_service_methods:
            raise ProcedureDeadlineBlocked("the approved deadline rule does not apply to the confirmed service method")
        start_date = service.served_on if rule.start_counting is StartCounting.EVENT_DAY_IS_DAY_ONE else service.served_on + timedelta(days=1)
        due_date = _count_due_date(start_date, rule, calendar)
        payload = {"service": service, "rule": rule, "calendar": calendar, "time_zone": time_zone.strip()}
        deadline = DeadlineCandidate(
            deadline_id=f"deadline_{uuid4().hex}",
            court_case_id=service.court_case_id,
            service_id=service.service_id,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.calendar_version,
            time_zone=time_zone.strip(),
            start_date=start_date,
            candidate_due_date=due_date,
            input_hash=_hash_payload(payload),
            status=RecordStatus.CANDIDATE,
            confirmed_by=None,
            confirmation_hash=None,
        )
        self._deadlines[deadline.deadline_id] = deadline
        self._version += 1
        return deadline

    def confirm_deadline(self, actor: Actor, *, deadline_id: str, confirmation_hash: str) -> DeadlineCandidate:
        _require_lead(actor)
        _require_text(confirmation_hash, "deadline confirmation hash")
        deadline = self._deadlines.get(deadline_id)
        if deadline is None or deadline.status is not RecordStatus.CANDIDATE:
            raise ProcedureDeadlineBlocked("only a deadline candidate can be confirmed")
        service = self._require_service(deadline.service_id)
        if service.status is not RecordStatus.CONFIRMED:
            raise ProcedureDeadlineBlocked("a deadline cannot be confirmed after its service record changed")
        confirmed = DeadlineCandidate(
            deadline_id=deadline.deadline_id,
            court_case_id=deadline.court_case_id,
            service_id=deadline.service_id,
            rule_id=deadline.rule_id,
            rule_version=deadline.rule_version,
            calendar_id=deadline.calendar_id,
            calendar_version=deadline.calendar_version,
            time_zone=deadline.time_zone,
            start_date=deadline.start_date,
            candidate_due_date=deadline.candidate_due_date,
            input_hash=deadline.input_hash,
            status=RecordStatus.CONFIRMED,
            confirmed_by=actor.actor_id,
            confirmation_hash=confirmation_hash,
        )
        self._deadlines[deadline_id] = confirmed
        self._version += 1
        return confirmed

    def build_formal_snapshot(self, actor: Actor) -> ProcedureDeadlineSnapshot:
        _require_lead(actor)
        court_cases = tuple(sorted((item for item in self._court_cases.values() if item.status is RecordStatus.CONFIRMED), key=lambda item: item.court_case_id))
        if not court_cases:
            raise ProcedureDeadlineBlocked("a procedure snapshot requires at least one confirmed court case")
        case_ids = {item.court_case_id for item in court_cases}
        services = tuple(sorted((item for item in self._services.values() if item.status is RecordStatus.CONFIRMED), key=lambda item: item.service_id))
        if any(item.court_case_id not in case_ids for item in services):
            raise ProcedureDeadlineBlocked("a formal service record cannot point to an unconfirmed court case")
        deadlines = tuple(sorted((item for item in self._deadlines.values() if item.status is RecordStatus.CONFIRMED), key=lambda item: item.deadline_id))
        if any(item.court_case_id not in case_ids for item in deadlines):
            raise ProcedureDeadlineBlocked("a formal deadline cannot point to an unconfirmed court case")
        payload = {"version": self._version, "court_cases": court_cases, "services": services, "deadlines": deadlines}
        return ProcedureDeadlineSnapshot(
            snapshot_id=f"procedure_deadline_snapshot_{uuid4().hex}",
            ledger_version=self._version,
            input_hash=_hash_payload(payload),
            court_cases=court_cases,
            services=services,
            deadlines=deadlines,
        )

    def _invalidate_case_dependents(self, court_case_ids: set[str]) -> None:
        affected_services = {
            service_id
            for service_id, service in self._services.items()
            if service.court_case_id in court_case_ids and service.status is not RecordStatus.INVALIDATED
        }
        for service_id in affected_services:
            service = self._services[service_id]
            self._services[service_id] = ServiceRecord(
                service_id=service.service_id,
                court_case_id=service.court_case_id,
                method=service.method,
                served_on=service.served_on,
                is_exact_date=service.is_exact_date,
                evidence_links=service.evidence_links,
                status=RecordStatus.INVALIDATED,
                confirmed_by=None,
                confirmation_hash=None,
            )
        self._invalidate_deadlines_for_services(affected_services)

    def _invalidate_deadlines_for_services(self, service_ids: set[str]) -> None:
        for deadline_id, deadline in list(self._deadlines.items()):
            if deadline.service_id in service_ids and deadline.status is not RecordStatus.INVALIDATED:
                self._deadlines[deadline_id] = DeadlineCandidate(
                    deadline_id=deadline.deadline_id,
                    court_case_id=deadline.court_case_id,
                    service_id=deadline.service_id,
                    rule_id=deadline.rule_id,
                    rule_version=deadline.rule_version,
                    calendar_id=deadline.calendar_id,
                    calendar_version=deadline.calendar_version,
                    time_zone=deadline.time_zone,
                    start_date=deadline.start_date,
                    candidate_due_date=deadline.candidate_due_date,
                    input_hash=deadline.input_hash,
                    status=RecordStatus.INVALIDATED,
                    confirmed_by=None,
                    confirmation_hash=None,
                )

    def _require_court_case(self, court_case_id: str) -> CourtCase:
        court_case = self._court_cases.get(court_case_id)
        if court_case is None:
            raise ProcedureDeadlineBlocked("unknown court case")
        return court_case

    def _require_service(self, service_id: str) -> ServiceRecord:
        service = self._services.get(service_id)
        if service is None:
            raise ProcedureDeadlineBlocked("unknown service record")
        return service

    def _require_rule(self, rule_id: str) -> DeadlineRule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise ProcedureDeadlineBlocked("unknown deadline rule")
        return rule

    def _require_calendar(self, calendar_id: str) -> HolidayCalendar:
        calendar = self._calendars.get(calendar_id)
        if calendar is None:
            raise ProcedureDeadlineBlocked("unknown holiday calendar")
        return calendar


def _count_due_date(start_date: date, rule: DeadlineRule, calendar: HolidayCalendar) -> date:
    if rule.day_basis is DayBasis.CALENDAR_DAYS:
        due_date = start_date + timedelta(days=rule.period_count - 1)
    else:
        remaining = rule.period_count
        due_date = start_date
        while True:
            if _is_working_day(due_date, calendar):
                remaining -= 1
                if remaining == 0:
                    break
            due_date += timedelta(days=1)
    if rule.due_date_adjustment is DueDateAdjustment.NEXT_WORKING_DAY:
        while not _is_working_day(due_date, calendar):
            due_date += timedelta(days=1)
    return due_date


def _is_working_day(day: date, calendar: HolidayCalendar) -> bool:
    if day in calendar.working_override_dates:
        return True
    if day in calendar.non_working_dates:
        return False
    return day.weekday() < 5


def _validate_official_source(source: OfficialSourceSnapshot) -> None:
    if source.verification_status is not SourceVerificationStatus.VERIFIED:
        raise ProcedureDeadlineBlocked("deadline rule/calendar source must be a verified official snapshot")
    if source.license_status is not SourceLicenseStatus.ACTIVE:
        raise ProcedureDeadlineBlocked("deadline rule/calendar source must have active recorded permission")
    if not source.snapshot_id.strip() or not source.official_url.startswith("https://"):
        raise ProcedureDeadlineBlocked("deadline rule/calendar source must retain an official HTTPS snapshot")
    if len(source.content_sha256) != 64 or any(character not in "0123456789abcdef" for character in source.content_sha256):
        raise ProcedureDeadlineBlocked("deadline rule/calendar source must retain a SHA-256 content hash")


def _validate_evidence(links: tuple[EvidenceLink, ...]) -> None:
    try:
        validate_evidence_links(links)
    except EvidenceReferenceBlocked as error:
        raise ProcedureDeadlineBlocked(str(error)) from error


def _require_candidate_role(actor: Actor) -> None:
    if not actor.roles & {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER}:
        raise ProcedureDeadlineBlocked("actor does not have a permitted role for this procedural action")


def _require_lead(actor: Actor) -> None:
    if Role.LEAD_LAWYER not in actor.roles:
        raise ProcedureDeadlineBlocked("lead lawyer role is required")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ProcedureDeadlineBlocked(f"{label} is required")


def _hash_payload(value: object) -> str:
    def normalize(item: object):
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return {key: normalize(val) for key, val in asdict(item).items()}
        if isinstance(item, dict):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(normalize(value) for value in item)
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        return item

    encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
