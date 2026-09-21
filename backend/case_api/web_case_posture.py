"""Browser-safe case posture confirmation for the lawyer Web workbench.

This module is deliberately route-free.  ``web_app.py`` should resolve the
current OIDC/MFA session and pass only the browser-owned semantic fields,
``expected_version`` and ``Idempotency-Key`` to this service.  The browser is
never allowed to choose a firm, actor, confirmation hash or profile hash.

The five commands remain separate append-only ledger operations because each
one advances the matter version and produces its own audit/outbox receipt:

``party -> proceeding -> position -> engagement -> current profile``

That is not a fixed plaintiff/defendant workflow.  The codes describe the
actual represented party, forum, procedural stage and position; downstream
Agent planning must still reason from the current record and law.  Updating a
version that is bound to the current profile makes that profile stale through
the database triggers in migration 0029, so callers must refresh ``state``
after every successful command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any, Protocol
from unicodedata import normalize
from uuid import UUID

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.case_posture_postgres import (
    CasePartyConfirmation,
    CasePostureProfileSnapshot,
    CourtPartyPositionConfirmation,
    CourtProceedingConfirmation,
    FirmEngagementConfirmation,
)
from case_kernel.models import Actor, Role


__all__ = (
    "AuthorityScopeCode",
    "CasePartyKind",
    "CasePostureStorePort",
    "CaseTypeCode",
    "EngagementState",
    "ForumType",
    "PartyPositionCode",
    "ProcedureStage",
    "WebCasePostureBlocked",
    "WebCasePostureCommandReceipt",
    "WebCasePostureCompleteReceipt",
    "WebCasePostureProfile",
    "WebCasePostureService",
    "WebCasePostureState",
    "WebCasePostureStatus",
)


class CasePartyKind(StrEnum):
    NATURAL_PERSON = "NATURAL_PERSON"
    LEGAL_PERSON = "LEGAL_PERSON"
    UNINCORPORATED_ORGANIZATION = "UNINCORPORATED_ORGANIZATION"
    STATE_OR_PUBLIC_BODY = "STATE_OR_PUBLIC_BODY"
    OTHER_LEGAL_SUBJECT = "OTHER_LEGAL_SUBJECT"


class ForumType(StrEnum):
    PEOPLE_COURT = "PEOPLE_COURT"
    ARBITRATION_COMMISSION = "ARBITRATION_COMMISSION"
    LABOR_ARBITRATION_COMMISSION = "LABOR_ARBITRATION_COMMISSION"
    ADMINISTRATIVE_AUTHORITY = "ADMINISTRATIVE_AUTHORITY"
    PEOPLE_PROCURATORATE = "PEOPLE_PROCURATORATE"
    PUBLIC_SECURITY_OR_SUPERVISORY_AUTHORITY = "PUBLIC_SECURITY_OR_SUPERVISORY_AUTHORITY"
    OTHER_STATUTORY_FORUM = "OTHER_STATUTORY_FORUM"


class CaseTypeCode(StrEnum):
    CIVIL_GENERAL = "CIVIL.GENERAL"
    CIVIL_PRIVATE_LENDING = "CIVIL.PRIVATE_LENDING"
    CIVIL_CONTRACT = "CIVIL.CONTRACT"
    CIVIL_TORT = "CIVIL.TORT"
    CIVIL_MARRIAGE_FAMILY = "CIVIL.MARRIAGE_FAMILY"
    CIVIL_LABOR = "CIVIL.LABOR"
    COMMERCIAL_GENERAL = "COMMERCIAL.GENERAL"
    FINANCIAL_GENERAL = "FINANCIAL.GENERAL"
    ADMINISTRATIVE_GENERAL = "ADMINISTRATIVE.GENERAL"
    CRIMINAL_DEFENSE = "CRIMINAL.DEFENSE"
    CRIMINAL_INCIDENTAL_CIVIL = "CRIMINAL.INCIDENTAL_CIVIL"
    ENFORCEMENT_GENERAL = "ENFORCEMENT.GENERAL"
    ARBITRATION_COMMERCIAL = "ARBITRATION.COMMERCIAL"
    ARBITRATION_LABOR = "ARBITRATION.LABOR"
    OTHER_STATUTORY_PROCEEDING = "OTHER.STATUTORY_PROCEEDING"


class ProcedureStage(StrEnum):
    PRE_ACTION = "PRE_ACTION"
    PRE_ARBITRATION = "PRE_ARBITRATION"
    PRESERVATION = "PRESERVATION"
    FIRST_INSTANCE = "FIRST_INSTANCE"
    SECOND_INSTANCE = "SECOND_INSTANCE"
    RETRIAL_REVIEW = "RETRIAL_REVIEW"
    RETRIAL = "RETRIAL"
    ENFORCEMENT = "ENFORCEMENT"
    ENFORCEMENT_OBJECTION = "ENFORCEMENT_OBJECTION"
    ARBITRATION = "ARBITRATION"
    LABOR_ARBITRATION = "LABOR_ARBITRATION"
    ADMINISTRATIVE_RECONSIDERATION = "ADMINISTRATIVE_RECONSIDERATION"
    CRIMINAL_INVESTIGATION = "CRIMINAL_INVESTIGATION"
    CRIMINAL_PROSECUTION_REVIEW = "CRIMINAL_PROSECUTION_REVIEW"
    CRIMINAL_FIRST_INSTANCE = "CRIMINAL_FIRST_INSTANCE"
    CRIMINAL_SECOND_INSTANCE = "CRIMINAL_SECOND_INSTANCE"
    CLOSED = "CLOSED"


class PartyPositionCode(StrEnum):
    PLAINTIFF = "PLAINTIFF"
    DEFENDANT = "DEFENDANT"
    APPELLANT = "APPELLANT"
    APPELLEE = "APPELLEE"
    RETRIAL_APPLICANT = "RETRIAL_APPLICANT"
    RETRIAL_RESPONDENT = "RETRIAL_RESPONDENT"
    APPLICANT = "APPLICANT"
    RESPONDENT = "RESPONDENT"
    EXECUTION_APPLICANT = "EXECUTION_APPLICANT"
    EXECUTION_RESPONDENT = "EXECUTION_RESPONDENT"
    ARBITRATION_CLAIMANT = "ARBITRATION_CLAIMANT"
    ARBITRATION_RESPONDENT = "ARBITRATION_RESPONDENT"
    LABOR_ARBITRATION_CLAIMANT = "LABOR_ARBITRATION_CLAIMANT"
    LABOR_ARBITRATION_RESPONDENT = "LABOR_ARBITRATION_RESPONDENT"
    ADMINISTRATIVE_APPLICANT = "ADMINISTRATIVE_APPLICANT"
    ADMINISTRATIVE_RESPONDENT = "ADMINISTRATIVE_RESPONDENT"
    CRIMINAL_SUSPECT = "CRIMINAL_SUSPECT"
    CRIMINAL_DEFENDANT = "CRIMINAL_DEFENDANT"
    VICTIM = "VICTIM"
    PRIVATE_PROSECUTOR = "PRIVATE_PROSECUTOR"
    THIRD_PARTY = "THIRD_PARTY"
    INTERESTED_PARTY = "INTERESTED_PARTY"
    OTHER_PARTICIPANT = "OTHER_PARTICIPANT"


class AuthorityScopeCode(StrEnum):
    GENERAL_AUTHORITY = "GENERAL_AUTHORITY"
    SPECIAL_AUTHORITY = "SPECIAL_AUTHORITY"
    LIMITED_AUTHORITY = "LIMITED_AUTHORITY"
    LEGAL_AID = "LEGAL_AID"
    COURT_APPOINTED_DEFENSE = "COURT_APPOINTED_DEFENSE"


class EngagementState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"


class WebCasePostureStatus(StrEnum):
    NOT_CONFIRMED = "NOT_CONFIRMED"
    CURRENT = "CURRENT"
    STALE = "STALE"


class WebCasePostureBlocked(ValueError):
    """The posture operation cannot safely cross the browser boundary."""


class CasePostureStorePort(Protocol):
    """Minimal production wiring contract implemented by PostgresCasePostureStore."""

    def confirm_party(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def confirm_proceeding(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def confirm_position(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def confirm_engagement(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def confirm_current_profile(self, **kwargs: Any) -> CaseLedgerCommandReceipt: ...

    def get_current_profile(
        self, *, matter_id: str, actor: Actor
    ) -> CasePostureProfileSnapshot | None: ...

    def get_latest_profile_state(
        self, *, matter_id: str, actor: Actor
    ) -> CasePostureProfileSnapshot | None: ...


@dataclass(frozen=True)
class WebCasePostureCommandReceipt:
    action: str
    matter_id: str
    matter_version: int
    object_type: str
    object_id: str
    refresh_posture_state: bool = True


@dataclass(frozen=True)
class WebCasePostureCompleteReceipt:
    action: str
    matter_id: str
    matter_version: int
    party_id: str
    proceeding_id: str
    position_id: str
    engagement_id: str
    profile_id: str
    refresh_posture_state: bool = True


@dataclass(frozen=True)
class WebCasePostureProfile:
    profile_id: str
    profile_version: int
    represented_party_id: str
    represented_party_display_label: str
    represented_party_kind: str
    proceeding_id: str
    forum_type: str
    position_id: str
    engagement_id: str
    case_type_code: str
    procedure_stage: str
    represented_position: str
    authority_scope_code: str
    engagement_state: str
    confirmed_matter_version: int


@dataclass(frozen=True)
class WebCasePostureState:
    status: WebCasePostureStatus
    profile: WebCasePostureProfile | None
    can_confirm: bool


_HUMAN_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
        Role.FIRM_ADMIN,
    }
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")


class WebCasePostureService:
    """Confirm and project the actual representation/procedure posture."""

    def __init__(self, *, store: CasePostureStorePort) -> None:
        required = (
            "confirm_party",
            "confirm_proceeding",
            "confirm_position",
            "confirm_engagement",
            "confirm_current_profile",
            "get_current_profile",
            "get_latest_profile_state",
        )
        if any(not callable(getattr(store, method, None)) for method in required):
            raise ValueError("Web case posture store is invalid")
        self._store = store

    @staticmethod
    def code_options() -> dict[str, tuple[str, ...]]:
        """Return the closed server-owned codes used to render Web selectors."""

        return {
            "party_kinds": _values(CasePartyKind),
            "forum_types": _values(ForumType),
            "case_types": _values(CaseTypeCode),
            "procedure_stages": _values(ProcedureStage),
            "party_positions": _values(PartyPositionCode),
            "authority_scopes": _values(AuthorityScopeCode),
            "engagement_states": _values(EngagementState),
        }

    def state(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> WebCasePostureState:
        actor = _require_identity(identity, write=False)
        normalized_matter = _uuid(matter_id, "案件编号")
        current = self._store.get_current_profile(
            matter_id=normalized_matter, actor=actor
        )
        if current is not None:
            if not isinstance(current, CasePostureProfileSnapshot) or current.effective_status != "CURRENT":
                raise WebCasePostureBlocked("当前代理档案状态无效")
            return WebCasePostureState(
                status=WebCasePostureStatus.CURRENT,
                profile=_project_profile(
                    current, expected_matter_id=normalized_matter
                ),
                can_confirm=Role.LEAD_LAWYER in actor.roles,
            )

        latest = self._store.get_latest_profile_state(
            matter_id=normalized_matter, actor=actor
        )
        if latest is None:
            return WebCasePostureState(
                status=WebCasePostureStatus.NOT_CONFIRMED,
                profile=None,
                can_confirm=Role.LEAD_LAWYER in actor.roles,
            )
        if not isinstance(latest, CasePostureProfileSnapshot) or latest.effective_status != "STALE":
            raise WebCasePostureBlocked("代理档案版本状态不一致")
        return WebCasePostureState(
            status=WebCasePostureStatus.STALE,
            profile=_project_profile(latest, expected_matter_id=normalized_matter),
            can_confirm=Role.LEAD_LAWYER in actor.roles,
        )

    def confirm_complete_posture(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        party_kind: str,
        display_label: str,
        forum_type: str,
        case_type_code: str,
        procedure_stage: str,
        position_code: str,
        authority_scope_code: str,
        engagement_state: str,
    ) -> WebCasePostureCompleteReceipt:
        """Idempotently resume the complete posture confirmation sequence.

        The immutable ledgers intentionally remain separate transactions, but
        all five stage keys are derived from one browser idempotency key.  If
        an HTTP response is lost after any stage commits, replaying the exact
        same request resumes from the persisted receipts instead of creating
        another body or asking the lawyer to reconstruct internal IDs.
        """

        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        del actor  # each governed command revalidates the same identity
        party = self.confirm_party(
            identity=identity,
            matter_id=matter,
            expected_version=version,
            idempotency_key=_stage_key(key, "party"),
            party_kind=party_kind,
            display_label=display_label,
        )
        proceeding = self.confirm_proceeding(
            identity=identity,
            matter_id=matter,
            expected_version=party.matter_version,
            idempotency_key=_stage_key(key, "proceeding"),
            forum_type=forum_type,
            case_type_code=case_type_code,
            procedure_stage=procedure_stage,
        )
        position = self.confirm_position(
            identity=identity,
            matter_id=matter,
            expected_version=proceeding.matter_version,
            idempotency_key=_stage_key(key, "position"),
            proceeding_id=proceeding.object_id,
            party_id=party.object_id,
            position_code=position_code,
        )
        engagement = self.confirm_engagement(
            identity=identity,
            matter_id=matter,
            expected_version=position.matter_version,
            idempotency_key=_stage_key(key, "engagement"),
            proceeding_id=proceeding.object_id,
            represented_party_id=party.object_id,
            authority_scope_code=authority_scope_code,
            engagement_state=engagement_state,
        )
        profile = self.confirm_current_profile(
            identity=identity,
            matter_id=matter,
            expected_version=engagement.matter_version,
            idempotency_key=_stage_key(key, "profile"),
            represented_party_id=party.object_id,
            proceeding_id=proceeding.object_id,
            position_id=position.object_id,
            engagement_id=engagement.object_id,
        )
        return WebCasePostureCompleteReceipt(
            action="CONFIRM_COMPLETE_POSTURE",
            matter_id=matter,
            matter_version=profile.matter_version,
            party_id=party.object_id,
            proceeding_id=proceeding.object_id,
            position_id=position.object_id,
            engagement_id=engagement.object_id,
            profile_id=profile.object_id,
        )

    def confirm_party(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        party_kind: str,
        display_label: str,
        party_id: str | None = None,
    ) -> WebCasePostureCommandReceipt:
        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        kind = _enum(CasePartyKind, party_kind, "当事人类型")
        label = _display_label(display_label)
        stable_id = _optional_uuid(party_id, "当事人编号")
        basis_hash = _basis_hash(
            "party", actor, matter, version, key, stable_id, kind.value, label
        )
        receipt = self._store.confirm_party(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=key,
            party_id=stable_id,
            confirmation=CasePartyConfirmation(kind.value, label, basis_hash),
        )
        return _project_receipt(
            receipt,
            action="CONFIRM_PARTY",
            expected_matter_id=matter,
            expected_object_type="CASE_PARTY",
        )

    def confirm_proceeding(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        forum_type: str,
        case_type_code: str,
        procedure_stage: str,
        proceeding_id: str | None = None,
    ) -> WebCasePostureCommandReceipt:
        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        forum = _enum(ForumType, forum_type, "受理机构")
        case_type = _enum(CaseTypeCode, case_type_code, "案件类型")
        stage = _enum(ProcedureStage, procedure_stage, "程序阶段")
        stable_id = _optional_uuid(proceeding_id, "程序编号")
        basis_hash = _basis_hash(
            "proceeding",
            actor,
            matter,
            version,
            key,
            stable_id,
            forum.value,
            case_type.value,
            stage.value,
        )
        receipt = self._store.confirm_proceeding(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=key,
            proceeding_id=stable_id,
            confirmation=CourtProceedingConfirmation(
                forum.value, case_type.value, stage.value, basis_hash
            ),
        )
        return _project_receipt(
            receipt,
            action="CONFIRM_PROCEEDING",
            expected_matter_id=matter,
            expected_object_type="COURT_PROCEEDING",
        )

    def confirm_position(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        proceeding_id: str,
        party_id: str,
        position_code: str,
        position_id: str | None = None,
    ) -> WebCasePostureCommandReceipt:
        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        proceeding = _uuid(proceeding_id, "程序编号")
        party = _uuid(party_id, "当事人编号")
        position = _enum(PartyPositionCode, position_code, "诉讼地位")
        stable_id = _optional_uuid(position_id, "诉讼地位编号")
        basis_hash = _basis_hash(
            "position",
            actor,
            matter,
            version,
            key,
            stable_id,
            proceeding,
            party,
            position.value,
        )
        receipt = self._store.confirm_position(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=key,
            position_id=stable_id,
            confirmation=CourtPartyPositionConfirmation(
                proceeding, party, position.value, basis_hash
            ),
        )
        return _project_receipt(
            receipt,
            action="CONFIRM_POSITION",
            expected_matter_id=matter,
            expected_object_type="COURT_PARTY_POSITION",
        )

    def confirm_engagement(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        proceeding_id: str,
        represented_party_id: str,
        authority_scope_code: str,
        engagement_state: str,
        engagement_id: str | None = None,
    ) -> WebCasePostureCommandReceipt:
        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        proceeding = _uuid(proceeding_id, "程序编号")
        party = _uuid(represented_party_id, "被代理当事人编号")
        authority = _enum(AuthorityScopeCode, authority_scope_code, "代理权限")
        state = _enum(EngagementState, engagement_state, "委托状态")
        stable_id = _optional_uuid(engagement_id, "委托编号")
        basis_hash = _basis_hash(
            "engagement",
            actor,
            matter,
            version,
            key,
            stable_id,
            proceeding,
            party,
            authority.value,
            state.value,
        )
        receipt = self._store.confirm_engagement(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=key,
            engagement_id=stable_id,
            confirmation=FirmEngagementConfirmation(
                proceeding, party, authority.value, state.value, basis_hash
            ),
        )
        return _project_receipt(
            receipt,
            action="CONFIRM_ENGAGEMENT",
            expected_matter_id=matter,
            expected_object_type="FIRM_ENGAGEMENT",
        )

    def confirm_current_profile(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        represented_party_id: str,
        proceeding_id: str,
        position_id: str,
        engagement_id: str,
    ) -> WebCasePostureCommandReceipt:
        actor, matter, version, key = _command_context(
            identity, matter_id, expected_version, idempotency_key
        )
        receipt = self._store.confirm_current_profile(
            matter_id=matter,
            actor=actor,
            expected_version=version,
            idempotency_key=key,
            represented_party_id=_uuid(represented_party_id, "被代理当事人编号"),
            proceeding_id=_uuid(proceeding_id, "程序编号"),
            position_id=_uuid(position_id, "诉讼地位编号"),
            engagement_id=_uuid(engagement_id, "委托编号"),
        )
        return _project_receipt(
            receipt,
            action="CONFIRM_CURRENT_PROFILE",
            expected_matter_id=matter,
            expected_object_type="CASE_POSTURE_PROFILE",
        )


def _require_identity(identity: ServerIdentityContext, *, write: bool) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebCasePostureBlocked("登录身份无效")
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebCasePostureBlocked("代理档案确认需要多因素登录")
    try:
        identity.validate()
    except Exception:
        raise WebCasePostureBlocked("登录身份已失效") from None
    actor = identity.actor
    if (
        not isinstance(actor, Actor)
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_HUMAN_READ_ROLES)
    ):
        raise WebCasePostureBlocked("当前角色不能查看代理档案")
    if write and Role.LEAD_LAWYER not in actor.roles:
        raise WebCasePostureBlocked("只有主办律师可以确认代理档案")
    return actor


def _command_context(
    identity: ServerIdentityContext,
    matter_id: object,
    expected_version: object,
    idempotency_key: object,
) -> tuple[Actor, str, int, str]:
    actor = _require_identity(identity, write=True)
    matter = _uuid(matter_id, "案件编号")
    if type(expected_version) is not int or expected_version < 1:
        raise WebCasePostureBlocked("案件版本无效")
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
        idempotency_key
    ):
        raise WebCasePostureBlocked("幂等请求编号无效")
    return actor, matter, expected_version, idempotency_key


def _enum(enum_type: type[StrEnum], value: object, label: str) -> StrEnum:
    if not isinstance(value, str):
        raise WebCasePostureBlocked(f"{label}无效")
    try:
        return enum_type(value)
    except ValueError:
        raise WebCasePostureBlocked(f"{label}不在受控选项中") from None


def _values(enum_type: type[StrEnum]) -> tuple[str, ...]:
    return tuple(item.value for item in enum_type)


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise WebCasePostureBlocked(f"{label}格式无效") from None


def _optional_uuid(value: object, label: str) -> str | None:
    return None if value is None else _uuid(value, label)


def _display_label(value: object) -> str:
    if not isinstance(value, str):
        raise WebCasePostureBlocked("当事人名称无效")
    result = normalize("NFC", value).strip()
    if (
        not 1 <= len(result) <= 200
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise WebCasePostureBlocked("当事人名称无效")
    return result


def _basis_hash(
    action: str,
    actor: Actor,
    matter_id: str,
    expected_version: int,
    idempotency_key: str,
    stable_id: str | None,
    *values: str,
) -> str:
    payload = {
        "schema": "web-case-posture-confirmation-v1",
        "action": action,
        "actor_id": actor.actor_id,
        "firm_id": actor.firm_id,
        "matter_id": matter_id,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
        "stable_id": stable_id,
        "values": values,
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _stage_key(idempotency_key: str, stage: str) -> str:
    return "posture-" + sha256(
        f"web-case-posture-complete-v1|{idempotency_key}|{stage}".encode("utf-8")
    ).hexdigest()


def _project_receipt(
    receipt: CaseLedgerCommandReceipt,
    *,
    action: str,
    expected_matter_id: str,
    expected_object_type: str,
) -> WebCasePostureCommandReceipt:
    if not isinstance(receipt, CaseLedgerCommandReceipt):
        raise WebCasePostureBlocked("代理档案保存回执无效")
    matter_id = _uuid(receipt.matter_id, "案件编号")
    if matter_id != expected_matter_id or receipt.object_type != expected_object_type:
        raise WebCasePostureBlocked("代理档案保存回执与当前命令不一致")
    if type(receipt.matter_version) is not int or receipt.matter_version < 1:
        raise WebCasePostureBlocked("代理档案保存版本无效")
    return WebCasePostureCommandReceipt(
        action=action,
        matter_id=matter_id,
        matter_version=receipt.matter_version,
        object_type=receipt.object_type,
        object_id=_uuid(receipt.object_id, "代理档案对象编号"),
    )


def _project_profile(
    snapshot: CasePostureProfileSnapshot, *, expected_matter_id: str
) -> WebCasePostureProfile:
    if _uuid(snapshot.matter_id, "案件编号") != expected_matter_id:
        raise WebCasePostureBlocked("代理档案不属于当前案件")
    if type(snapshot.profile_version) is not int or snapshot.profile_version < 1:
        raise WebCasePostureBlocked("代理档案版本无效")
    if (
        type(snapshot.confirmed_matter_version) is not int
        or snapshot.confirmed_matter_version < 1
    ):
        raise WebCasePostureBlocked("代理档案确认版本无效")
    return WebCasePostureProfile(
        profile_id=_uuid(snapshot.profile_id, "代理档案编号"),
        profile_version=snapshot.profile_version,
        represented_party_id=_uuid(snapshot.represented_party_id, "被代理当事人编号"),
        represented_party_display_label=snapshot.represented_party_display_label,
        represented_party_kind=_enum(
            CasePartyKind, snapshot.represented_party_kind, "当事人类型"
        ).value,
        proceeding_id=_uuid(snapshot.proceeding_id, "程序编号"),
        forum_type=_enum(ForumType, snapshot.forum_type, "受理机构").value,
        position_id=_uuid(snapshot.position_id, "诉讼地位编号"),
        engagement_id=_uuid(snapshot.engagement_id, "委托编号"),
        case_type_code=_enum(CaseTypeCode, snapshot.case_type_code, "案件类型").value,
        procedure_stage=_enum(ProcedureStage, snapshot.procedure_stage, "程序阶段").value,
        represented_position=_enum(
            PartyPositionCode, snapshot.represented_position, "诉讼地位"
        ).value,
        authority_scope_code=_enum(
            AuthorityScopeCode, snapshot.authority_scope_code, "代理权限"
        ).value,
        engagement_state=_enum(
            EngagementState, snapshot.engagement_state, "委托状态"
        ).value,
        confirmed_matter_version=snapshot.confirmed_matter_version,
    )
