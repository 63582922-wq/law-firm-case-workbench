from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_case_posture import (
    WebCasePostureBlocked,
    WebCasePostureService,
    WebCasePostureStatus,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.case_posture_postgres import CasePostureProfileSnapshot
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.current: CasePostureProfileSnapshot | None = None
        self.latest: CasePostureProfileSnapshot | None = None

    def _write(self, name: str, kwargs: dict, object_type: str) -> CaseLedgerCommandReceipt:
        self.calls.append((name, kwargs))
        return CaseLedgerCommandReceipt(
            command_name=name,
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=_id(),
            object_type=object_type,
            object_id=_id(),
        )

    def confirm_party(self, **kwargs):
        return self._write("confirm_party", kwargs, "CASE_PARTY")

    def confirm_proceeding(self, **kwargs):
        return self._write("confirm_proceeding", kwargs, "COURT_PROCEEDING")

    def confirm_position(self, **kwargs):
        return self._write("confirm_position", kwargs, "COURT_PARTY_POSITION")

    def confirm_engagement(self, **kwargs):
        return self._write("confirm_engagement", kwargs, "FIRM_ENGAGEMENT")

    def confirm_current_profile(self, **kwargs):
        return self._write("confirm_current_profile", kwargs, "CASE_POSTURE_PROFILE")

    def get_current_profile(self, **kwargs):
        self.calls.append(("get_current_profile", kwargs))
        return self.current

    def get_latest_profile_state(self, **kwargs):
        self.calls.append(("get_latest_profile_state", kwargs))
        return self.latest


def _identity(*roles: Role, method: AuthenticationMethod = AuthenticationMethod.OIDC_MFA):
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(_id(), _id(), frozenset(roles)),
        session_id=_id(),
        issuer="https://identity.lawfirm.example",
        authentication_method=method,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


def _profile(
    *, matter_id: str | None = None, status: str = "CURRENT"
) -> CasePostureProfileSnapshot:
    return CasePostureProfileSnapshot(
        profile_id=_id(),
        matter_id=matter_id or _id(),
        profile_version=2,
        effective_status=status,
        profile_hash="a" * 64,
        represented_party_id=_id(),
        represented_party_version_id=_id(),
        represented_party_display_label="高某",
        represented_party_kind="NATURAL_PERSON",
        proceeding_id=_id(),
        proceeding_version_id=_id(),
        forum_type="PEOPLE_COURT",
        position_id=_id(),
        position_version_id=_id(),
        engagement_id=_id(),
        engagement_version_id=_id(),
        case_type_code="CIVIL.PRIVATE_LENDING",
        procedure_stage="FIRST_INSTANCE",
        represented_position="DEFENDANT",
        authority_scope_code="SPECIAL_AUTHORITY",
        engagement_state="ACTIVE",
        confirmed_matter_version=12,
        supersedes_profile_id=_id(),
        confirmed_by=_id(),
    )


class WebCasePostureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _Store()
        self.service = WebCasePostureService(store=self.store)
        self.lead = _identity(Role.LEAD_LAWYER)
        self.matter_id = _id()

    def test_party_confirmation_uses_server_identity_and_server_hash(self) -> None:
        receipt = self.service.confirm_party(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=7,
            idempotency_key="posture-party-0001",
            party_kind="NATURAL_PERSON",
            display_label="  高某  ",
        )
        self.assertEqual(receipt.matter_version, 8)
        self.assertEqual(receipt.object_type, "CASE_PARTY")
        _, kwargs = self.store.calls[-1]
        self.assertIs(kwargs["actor"], self.lead.actor)
        self.assertEqual(kwargs["matter_id"], self.matter_id)
        self.assertEqual(kwargs["confirmation"].display_label, "高某")
        self.assertEqual(len(kwargs["confirmation"].basis_hash), 64)
        self.assertNotEqual(kwargs["confirmation"].basis_hash, "posture-party-0001")

    def test_all_upstream_meanings_use_controlled_codes_and_sequential_versions(self) -> None:
        party_id, proceeding_id, position_id, engagement_id = (_id() for _ in range(4))
        proceeding = self.service.confirm_proceeding(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=8,
            idempotency_key="posture-proceed-01",
            proceeding_id=proceeding_id,
            forum_type="PEOPLE_COURT",
            case_type_code="CIVIL.PRIVATE_LENDING",
            procedure_stage="FIRST_INSTANCE",
        )
        self.assertEqual(proceeding.matter_version, 9)
        self.service.confirm_position(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=9,
            idempotency_key="posture-position-01",
            position_id=position_id,
            proceeding_id=proceeding_id,
            party_id=party_id,
            position_code="DEFENDANT",
        )
        self.service.confirm_engagement(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=10,
            idempotency_key="posture-engage-0001",
            engagement_id=engagement_id,
            proceeding_id=proceeding_id,
            represented_party_id=party_id,
            authority_scope_code="SPECIAL_AUTHORITY",
            engagement_state="ACTIVE",
        )
        final = self.service.confirm_current_profile(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=11,
            idempotency_key="posture-profile-0001",
            represented_party_id=party_id,
            proceeding_id=proceeding_id,
            position_id=position_id,
            engagement_id=engagement_id,
        )
        self.assertEqual(final.matter_version, 12)
        self.assertEqual(
            [name for name, _ in self.store.calls],
            [
                "confirm_proceeding",
                "confirm_position",
                "confirm_engagement",
                "confirm_current_profile",
            ],
        )
        for _, kwargs in self.store.calls[:3]:
            self.assertEqual(len(kwargs["confirmation"].basis_hash), 64)

    def test_complete_posture_uses_one_external_key_and_resumable_stage_keys(self) -> None:
        receipt = self.service.confirm_complete_posture(
            identity=self.lead,
            matter_id=self.matter_id,
            expected_version=20,
            idempotency_key="posture-complete-0001",
            party_kind="NATURAL_PERSON",
            display_label="高某",
            forum_type="PEOPLE_COURT",
            case_type_code="CIVIL.PRIVATE_LENDING",
            procedure_stage="FIRST_INSTANCE",
            position_code="DEFENDANT",
            authority_scope_code="SPECIAL_AUTHORITY",
            engagement_state="ACTIVE",
        )
        self.assertEqual(receipt.action, "CONFIRM_COMPLETE_POSTURE")
        self.assertEqual(receipt.matter_version, 25)
        self.assertEqual(
            [name for name, _ in self.store.calls],
            [
                "confirm_party",
                "confirm_proceeding",
                "confirm_position",
                "confirm_engagement",
                "confirm_current_profile",
            ],
        )
        self.assertEqual(
            [kwargs["expected_version"] for _, kwargs in self.store.calls],
            [20, 21, 22, 23, 24],
        )
        stage_keys = [kwargs["idempotency_key"] for _, kwargs in self.store.calls]
        self.assertEqual(len(stage_keys), len(set(stage_keys)))
        self.assertTrue(all(key.startswith("posture-") and len(key) == 72 for key in stage_keys))
        self.assertEqual(
            self.store.calls[2][1]["confirmation"].party_id,
            receipt.party_id,
        )

    def test_collaborating_lawyer_can_read_but_cannot_confirm(self) -> None:
        collaborator = _identity(Role.COLLABORATING_LAWYER)
        state = self.service.state(identity=collaborator, matter_id=self.matter_id)
        self.assertEqual(state.status, WebCasePostureStatus.NOT_CONFIRMED)
        self.assertFalse(state.can_confirm)
        with self.assertRaisesRegex(WebCasePostureBlocked, "只有主办律师"):
            self.service.confirm_party(
                identity=collaborator,
                matter_id=self.matter_id,
                expected_version=1,
                idempotency_key="posture-party-0002",
                party_kind="NATURAL_PERSON",
                display_label="当事人",
            )

    def test_non_oidc_and_system_worker_roles_are_rejected_before_store(self) -> None:
        desktop = _identity(
            Role.LEAD_LAWYER,
            method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
        )
        with self.assertRaisesRegex(WebCasePostureBlocked, "多因素登录"):
            self.service.state(identity=desktop, matter_id=self.matter_id)
        worker = _identity(Role.LEAD_LAWYER, Role.SYSTEM_WORKER)
        with self.assertRaisesRegex(WebCasePostureBlocked, "当前角色"):
            self.service.state(identity=worker, matter_id=self.matter_id)
        self.assertEqual(self.store.calls, [])

    def test_unknown_codes_are_rejected_before_store(self) -> None:
        with self.assertRaisesRegex(WebCasePostureBlocked, "受控选项"):
            self.service.confirm_proceeding(
                identity=self.lead,
                matter_id=self.matter_id,
                expected_version=1,
                idempotency_key="posture-proceed-02",
                forum_type="LOCAL_COURT_FROM_BROWSER",
                case_type_code="CIVIL.PRIVATE_LENDING",
                procedure_stage="FIRST_INSTANCE",
            )
        self.assertEqual(self.store.calls, [])

    def test_current_profile_projection_omits_hashes_actor_and_version_ids(self) -> None:
        self.store.current = _profile(matter_id=self.matter_id, status="CURRENT")
        state = self.service.state(identity=self.lead, matter_id=self.matter_id)
        self.assertEqual(state.status, WebCasePostureStatus.CURRENT)
        self.assertTrue(state.can_confirm)
        self.assertEqual(state.profile.represented_position, "DEFENDANT")
        self.assertEqual(state.profile.represented_party_kind, "NATURAL_PERSON")
        self.assertEqual(state.profile.forum_type, "PEOPLE_COURT")
        fields = set(state.profile.__dataclass_fields__)
        self.assertNotIn("profile_hash", fields)
        self.assertNotIn("represented_party_version_id", fields)
        self.assertNotIn("confirmed_by", fields)
        self.assertEqual([name for name, _ in self.store.calls], ["get_current_profile"])

    def test_stale_profile_is_explicit_when_current_head_was_cleared(self) -> None:
        self.store.latest = _profile(matter_id=self.matter_id, status="STALE")
        state = self.service.state(identity=self.lead, matter_id=self.matter_id)
        self.assertEqual(state.status, WebCasePostureStatus.STALE)
        self.assertIsNotNone(state.profile)
        self.assertEqual(
            [name for name, _ in self.store.calls],
            ["get_current_profile", "get_latest_profile_state"],
        )

    def test_cross_matter_profile_projection_is_blocked(self) -> None:
        self.store.current = _profile(status="CURRENT")
        with self.assertRaisesRegex(WebCasePostureBlocked, "不属于当前案件"):
            self.service.state(identity=self.lead, matter_id=self.matter_id)

    def test_browser_command_signatures_have_no_actor_firm_or_hash_parameters(self) -> None:
        for method_name in (
            "confirm_party",
            "confirm_proceeding",
            "confirm_position",
            "confirm_engagement",
            "confirm_current_profile",
            "confirm_complete_posture",
        ):
            parameters = set(
                inspect.signature(getattr(WebCasePostureService, method_name)).parameters
            )
            self.assertNotIn("actor", parameters)
            self.assertNotIn("firm_id", parameters)
            self.assertNotIn("basis_hash", parameters)
            self.assertNotIn("profile_hash", parameters)

    def test_service_has_no_fixed_position_to_deliverable_mapping(self) -> None:
        source = inspect.getsource(WebCasePostureService).lower()
        for forbidden in (
            "required_material",
            "deliverable_template",
            "complaint_template",
            "defence_template",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
