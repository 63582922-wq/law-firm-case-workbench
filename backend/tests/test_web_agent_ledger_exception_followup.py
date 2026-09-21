from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_agent_ledger_exception_followup import (
    WebAgentLedgerExceptionFollowupBlocked,
    WebAgentLedgerExceptionFollowupService,
    WebManagedEvidenceSourceSelection,
)
from case_api.web_case_agent_run_identity import derive_web_case_agent_entity_id
from case_kernel.case_agent_ledger_exception_followup import (
    LedgerExceptionControlHealth,
    LedgerExceptionFollowup,
    LedgerExceptionFollowupKind,
    LedgerExceptionFollowupSnapshot,
    LedgerExceptionFollowupState,
    ManagedEvidenceSourceCandidate,
    ManagedEvidenceSourceType,
)
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    ActiveLedgerExceptionFollowupPage,
    ExceptionControlRecoveryIntentReceipt,
    ExceptionControlTransferReceipt,
    ExceptionControlState,
    FollowupEvidencePageIdPage,
    ManagedEvidenceSourceCandidatePage,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _identity(role: Role, *, firm_id: str | None = None) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(
            actor_id=_id(),
            firm_id=firm_id or _id(),
            roles=frozenset({role}),
        ),
        session_id=_id(),
        issuer="https://identity.lawfirm.example",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


def _followup(
    *,
    kind: LedgerExceptionFollowupKind,
    followup_id: str | None = None,
    automation_status: str | None = None,
    control_health: LedgerExceptionControlHealth = LedgerExceptionControlHealth.HEALTHY,
) -> LedgerExceptionFollowupSnapshot:
    return LedgerExceptionFollowupSnapshot(
        followup_id=followup_id or _id(),
        kind=kind,
        state=LedgerExceptionFollowupState.ACTIVE,
        head_sequence=1,
        origin_exception_decision_id=_id(),
        origin_exception_group_id=_id(),
        origin_extraction_batch_id=_id(),
        created_matter_version=8,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=4),
        reason_code=(
            "PARTY_DATE_AMOUNT_UNCLEAR"
            if kind is LedgerExceptionFollowupKind.MORE_EVIDENCE
            else "EXTRACTION_CONFLICT"
        ),
        reason_note="请核对原始材料",
        candidate_count=2,
        canonical_reason_codes=("OCR_DERIVED", "LOW_CONFIDENCE"),
        evidence_page_ids=(_id(), _id()),
        control_health=control_health,
        managed_evidence_request_id=(
            _id() if kind is LedgerExceptionFollowupKind.MORE_EVIDENCE else None
        ),
        acceptance_criteria=(
            {"new_source_required": True, "subject_hash": "a" * 64}
            if kind is LedgerExceptionFollowupKind.MORE_EVIDENCE
            else None
        ),
        automation_status=automation_status,
    )


class _Store:
    def __init__(
        self,
        *,
        matter_id: str,
        followup: LedgerExceptionFollowupSnapshot,
    ) -> None:
        self.matter_id = matter_id
        self.followups = (followup,)
        self.known_followups = {followup.followup_id: followup}
        self.page_ids = followup.evidence_page_ids
        self.terminal_states: dict[str, LedgerExceptionFollowupState] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.source = ManagedEvidenceSourceCandidate(
            object_type=ManagedEvidenceSourceType.MATERIAL_OBJECT,
            object_id=_id(),
            display_label="新增银行流水.xlsx",
            created_at=datetime.now(timezone.utc),
        )
        self.sources = (self.source,)
        self.receipts: dict[str, CaseLedgerCommandReceipt] = {}
        self.intent_receipts: dict[
            str, ExceptionControlRecoveryIntentReceipt
        ] = {}
        self.transfer_receipts: dict[str, ExceptionControlTransferReceipt] = {}

    def list_active_followups(self, **kwargs):
        self.calls.append(("list", kwargs))
        offset = int(kwargs["offset"])
        limit = int(kwargs["limit"])
        return ActiveLedgerExceptionFollowupPage(
            total_count=len(self.followups),
            offset=offset,
            followups=self.followups[offset : offset + limit],
        )

    def get_followup(self, **kwargs):
        self.calls.append(("get", kwargs))
        snapshot = self.known_followups[str(kwargs["followup_id"])]
        return LedgerExceptionFollowup(
            followup_id=snapshot.followup_id,
            origin_exception_decision_id=snapshot.origin_exception_decision_id,
            matter_id=str(kwargs["matter_id"]),
            kind=snapshot.kind,
            state=self.terminal_states.get(
                snapshot.followup_id, LedgerExceptionFollowupState.ACTIVE
            ),
            subject_hash="a" * 64,
            head_sequence=snapshot.head_sequence,
            managed_evidence_request_id=snapshot.managed_evidence_request_id,
        )

    def list_eligible_managed_evidence_sources(self, **kwargs):
        self.calls.append(("sources", kwargs))
        offset = int(kwargs["offset"])
        rows = self.sources[offset : offset + int(kwargs["limit"])]
        return ManagedEvidenceSourceCandidatePage(
            total_count=len(self.sources),
            offset=offset,
            sources=rows,
        )

    def list_followup_evidence_page_ids(self, **kwargs):
        self.calls.append(("pages", kwargs))
        offset = int(kwargs["offset"])
        page_ids = self.page_ids[
            offset : offset + int(kwargs["limit"])
        ]
        return FollowupEvidencePageIdPage(
            total_count=len(self.page_ids),
            offset=offset,
            evidence_page_ids=page_ids,
        )

    def read_current_control_state(self, **kwargs):
        self.calls.append(("control", kwargs))
        if not self.followups:
            return None
        health = self.followups[0].control_health
        return ExceptionControlState(
            matter_id=str(kwargs["matter_id"]),
            control_assignment_id=_id(),
            control_run_id=_id(),
            control_health=health,
            head_sequence=1,
        )

    def resolve_followup(self, **kwargs):
        self.calls.append(("resolve", kwargs))
        key = str(kwargs["idempotency_key"])
        receipt = self.receipts.get(key)
        if receipt is None:
            receipt = CaseLedgerCommandReceipt(
                command_name="RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP",
                idempotency_key=key,
                matter_id=str(kwargs["matter_id"]),
                matter_version=int(kwargs["expected_version"]) + 1,
                audit_event_id=_id(),
                object_type="CASE_LEDGER_EXCEPTION_FOLLOWUP",
                object_id=str(kwargs["followup_id"]),
            )
            self.receipts[key] = receipt
            terminal = {
                "CONFIRM_MORE_EVIDENCE": LedgerExceptionFollowupState.SATISFIED,
                "RESUME": LedgerExceptionFollowupState.RESUMED,
                "WITHDRAW": LedgerExceptionFollowupState.WITHDRAWN,
                "SUPERSEDE": LedgerExceptionFollowupState.SUPERSEDED,
            }[kwargs["action"].value]
            self.terminal_states[str(kwargs["followup_id"])] = terminal
            self.followups = ()
        return receipt

    def transfer_control_to_recovery_run(self, **kwargs):
        self.calls.append(("transfer", kwargs))
        key = str(kwargs["idempotency_key"])
        receipt = self.transfer_receipts.get(key)
        if receipt is None:
            receipt = ExceptionControlTransferReceipt(
                command_name="TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL",
                idempotency_key=key,
                matter_id=str(kwargs["matter_id"]),
                matter_version=int(kwargs["expected_version"]),
                audit_event_id=_id(),
                control_assignment_id=_id(),
                control_health=LedgerExceptionControlHealth.HEALTHY,
            )
            self.transfer_receipts[key] = receipt
            prepared = self.intent_receipts[key]
            self.intent_receipts[key] = replace(
                prepared, recovery_state="TRANSFERRED"
            )
            self.followups = tuple(
                replace(item, control_health=LedgerExceptionControlHealth.HEALTHY)
                for item in self.followups
            )
        return receipt

    def prepare_control_recovery(self, **kwargs):
        self.calls.append(("prepare", kwargs))
        key = str(kwargs["idempotency_key"])
        receipt = self.intent_receipts.get(key)
        if receipt is None:
            resumable = next(
                (
                    item
                    for item in self.intent_receipts.values()
                    if item.recovery_state == "PENDING" and item.run_exists
                ),
                None,
            )
            if resumable is not None:
                return replace(resumable, idempotency_key=key)
            receipt = ExceptionControlRecoveryIntentReceipt(
                command_name=(
                    "PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY"
                ),
                idempotency_key=key,
                matter_id=str(kwargs["matter_id"]),
                matter_version=int(kwargs["expected_version"]),
                recovery_intent_id=_id(),
                recovery_state="PENDING",
                transfer_idempotency_key=key,
                replacement_run_id=str(kwargs["replacement_run_id"]),
                run_exists=False,
            )
            self.intent_receipts[key] = receipt
        return receipt


class _RecoveryRunService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_recovery_run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(run_id=kwargs["replacement_run_id"])


class _MatterReader:
    def __init__(self, matter_id: str, version: int = 9) -> None:
        self.matter_id = matter_id
        self.version = version

    def get_case_snapshot(self, **kwargs):
        return SimpleNamespace(matter_id=kwargs["matter_id"], version=self.version)


class WebAgentLedgerExceptionFollowupTests(unittest.TestCase):
    def _service(
        self,
        *,
        role: Role,
        kind: LedgerExceptionFollowupKind,
        automation_status: str | None = None,
        control_health: LedgerExceptionControlHealth = LedgerExceptionControlHealth.HEALTHY,
    ):
        firm_id = _id()
        matter_id = _id()
        identity = _identity(role, firm_id=firm_id)
        followup = _followup(
            kind=kind,
            automation_status=automation_status,
            control_health=control_health,
        )
        store = _Store(matter_id=matter_id, followup=followup)
        recovery = _RecoveryRunService()
        service = WebAgentLedgerExceptionFollowupService(
            store=store,
            matter_version_reader=_MatterReader(matter_id),
            recovery_run_service=recovery,
            configured_firm_ids=frozenset({firm_id}),
        )
        return service, store, recovery, identity, matter_id, followup

    def test_human_read_projection_is_active_source_linked_and_internal_free(self) -> None:
        service, _, _, identity, matter_id, followup = self._service(
            role=Role.ASSISTANT,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="WAITING_FOR_PLAN",
        )
        self.assertTrue(service.is_available(identity=identity))
        page = service.list_followups(
            identity=identity, matter_id=matter_id, offset=0, limit=50
        )
        self.assertEqual(page.total_count, 1)
        self.assertIsNone(page.next_offset)
        item = page.followups[0]
        self.assertEqual(item.followup_id, followup.followup_id)
        self.assertEqual(item.current_matter_version, 9)
        self.assertEqual(item.state, "ACTIVE")
        self.assertEqual(item.automation_status, "WAITING_FOR_PLAN")
        self.assertFalse(item.can_act)
        self.assertEqual(page.control_health, "HEALTHY")
        self.assertFalse(page.can_recover)
        self.assertEqual(
            {action.code for action in item.allowed_actions},
            {"WITHDRAW", "SUPERSEDE"},
        )
        self.assertFalse(hasattr(item, "run_id"))
        self.assertFalse(hasattr(item, "graph_id"))
        self.assertFalse(hasattr(item, "subject_hash"))
        self.assertFalse(hasattr(item, "managed_evidence_request_id"))

    def test_more_evidence_lists_only_opaque_candidates_and_lead_selects_exact_source(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.MORE_EVIDENCE,
        )
        sources = service.list_eligible_managed_evidence_sources(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            offset=0,
            limit=50,
        )
        self.assertEqual(sources.sources[0].display_label, "新增银行流水.xlsx")
        self.assertFalse(hasattr(sources.sources[0], "object_key"))
        receipt = service.resolve_followup(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            expected_version=9,
            idempotency_key="ledger-followup-confirm-0001",
            action="CONFIRM_MORE_EVIDENCE",
            reason_note="已核对新增银行流水，覆盖本组缺失信息",
            managed_evidence_sources=(
                WebManagedEvidenceSourceSelection(
                    object_type=sources.sources[0].object_type,
                    object_id=sources.sources[0].object_id,
                ),
            ),
        )
        self.assertEqual(receipt.terminal_state, "SATISFIED")
        _, command = store.calls[-1]
        self.assertEqual(command["server_session_id"], identity.session_id)
        self.assertNotIn("managed_evidence_request_id", command)
        self.assertEqual(len(command["managed_evidence_sources"]), 1)

    def test_managed_evidence_sources_beyond_five_hundred_are_paged_without_truncation(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.MORE_EVIDENCE,
        )
        store.sources = tuple(
            replace(store.source, object_id=_id(), display_label=f"新增材料 {index}")
            for index in range(501)
        )
        page = service.list_eligible_managed_evidence_sources(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            offset=500,
            limit=50,
        )
        self.assertEqual(page.total_count, 501)
        self.assertEqual(page.offset, 500)
        self.assertIsNone(page.next_offset)
        self.assertEqual(len(page.sources), 1)

    def test_large_more_evidence_source_set_uses_count_and_page_projection(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.MORE_EVIDENCE,
        )
        store.page_ids = tuple(_id() for _ in range(501))
        summary = replace(followup, evidence_page_count=501)
        store.followups = (summary,)
        store.known_followups[followup.followup_id] = summary

        listed = service.list_followups(
            identity=identity,
            matter_id=matter_id,
            offset=0,
            limit=50,
        )
        self.assertEqual(listed.followups[0].evidence_page_count, 501)
        page = service.list_followup_evidence_page_ids(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            offset=500,
            limit=50,
        )
        self.assertEqual(page.total_count, 501)
        self.assertEqual(page.evidence_page_ids, (store.page_ids[-1],))

    def test_response_loss_replay_uses_same_complete_payload_after_active_head_disappears(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.DEFERRED_REVIEW,
        )
        kwargs = dict(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            expected_version=9,
            idempotency_key="ledger-followup-resume-0001",
            action="RESUME",
            reason_note="当事人材料已经补齐，恢复本组复核",
            managed_evidence_sources=(),
        )
        first = service.resolve_followup(**kwargs)
        self.assertEqual(store.followups, ())
        second = service.resolve_followup(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(
            [name for name, _ in store.calls].count("resolve"),
            2,
        )

    def test_non_lead_cannot_write_and_non_confirm_action_cannot_carry_sources(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.REVIEWER,
            kind=LedgerExceptionFollowupKind.DEFERRED_REVIEW,
        )
        with self.assertRaises(WebAgentLedgerExceptionFollowupBlocked):
            service.resolve_followup(
                identity=identity,
                matter_id=matter_id,
                followup_id=followup.followup_id,
                expected_version=9,
                idempotency_key="ledger-followup-resume-0002",
                action="RESUME",
                reason_note="恢复复核",
                managed_evidence_sources=(),
            )
        lead = _identity(Role.LEAD_LAWYER, firm_id=identity.actor.firm_id)
        with self.assertRaises(WebAgentLedgerExceptionFollowupBlocked):
            service.resolve_followup(
                identity=lead,
                matter_id=matter_id,
                followup_id=followup.followup_id,
                expected_version=9,
                idempotency_key="ledger-followup-resume-0003",
                action="RESUME",
                reason_note="恢复复核",
                managed_evidence_sources=(
                    WebManagedEvidenceSourceSelection(
                        object_type=store.source.object_type.value,
                        object_id=store.source.object_id,
                    ),
                ),
            )

    def test_unconfigured_firm_and_non_mfa_identity_fail_closed(self) -> None:
        service, _, _, identity, matter_id, _ = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="WAITING_FOR_PLAN",
        )
        local_identity = ServerIdentityContext(
            actor=identity.actor,
            session_id=identity.session_id,
            issuer=identity.issuer,
            authentication_method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            authenticated_at=identity.authenticated_at,
            expires_at=identity.expires_at,
        )
        self.assertFalse(service.is_available(identity=local_identity))
        with self.assertRaises(WebAgentLedgerExceptionFollowupBlocked):
            service.list_followups(
                identity=local_identity,
                matter_id=matter_id,
                offset=0,
                limit=50,
            )

    def test_more_than_five_hundred_active_items_remain_reachable_and_exactly_actionable(self) -> None:
        service, store, _, identity, matter_id, followup = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.DEFERRED_REVIEW,
        )
        store.followups = (followup,) + tuple(
            replace(
                followup,
                followup_id=_id(),
                origin_exception_decision_id=_id(),
                origin_exception_group_id=_id(),
            )
            for _ in range(519)
        )
        first = service.list_followups(
            identity=identity,
            matter_id=matter_id,
            offset=0,
            limit=50,
        )
        eleventh = service.list_followups(
            identity=identity,
            matter_id=matter_id,
            offset=500,
            limit=50,
        )
        self.assertEqual(first.total_count, 520)
        self.assertEqual(first.next_offset, 50)
        self.assertEqual(len(first.followups), 50)
        self.assertEqual(eleventh.total_count, 520)
        self.assertIsNone(eleventh.next_offset)
        self.assertEqual(len(eleventh.followups), 20)

        list_calls_before = len([name for name, _ in store.calls if name == "list"])
        receipt = service.resolve_followup(
            identity=identity,
            matter_id=matter_id,
            followup_id=followup.followup_id,
            expected_version=9,
            idempotency_key="ledger-followup-over-500-action-0001",
            action="RESUME",
            reason_note="已完成核对，恢复本组复核",
            managed_evidence_sources=(),
        )
        self.assertEqual(receipt.terminal_state, "RESUMED")
        self.assertEqual(
            len([name for name, _ in store.calls if name == "list"]),
            list_calls_before,
        )

    def test_recovery_creates_server_owned_run_then_transfers_and_replays_without_orphan(self) -> None:
        service, store, recovery, identity, matter_id, _ = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="RECOVERY_REQUIRED",
            control_health=LedgerExceptionControlHealth.RECOVERY_REQUIRED,
        )
        kwargs = dict(
            identity=identity,
            matter_id=matter_id,
            expected_matter_version=9,
            idempotency_key="ledger-followup-recovery-0001",
            now=datetime.now(timezone.utc),
        )
        first = service.recover_exception_followups(**kwargs)
        self.assertEqual(first.control_health, "HEALTHY")
        self.assertTrue(first.recovery_started)
        self.assertEqual(len(recovery.calls), 1)
        self.assertEqual(
            len([name for name, _ in store.calls if name == "prepare"]), 1
        )
        transfer = [call for name, call in store.calls if name == "transfer"][0]
        self.assertEqual(
            recovery.calls[0]["replacement_run_id"],
            transfer["replacement_run_id"],
        )
        self.assertEqual(
            transfer["replacement_run_id"],
            derive_web_case_agent_entity_id(
                actor=identity.actor,
                matter_id=matter_id,
                idempotency_key="ledger-followup-recovery-0001.run",
                entity="run",
            ),
        )
        self.assertNotIn("replacement_run_id", first.__dict__)

        second = service.recover_exception_followups(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(recovery.calls), 1)
        self.assertEqual(
            len([name for name, _ in store.calls if name == "prepare"]), 2
        )
        self.assertEqual(
            len([name for name, _ in store.calls if name == "transfer"]), 2
        )

    def test_recovery_after_reload_resumes_server_pending_run_with_new_key(self) -> None:
        service, store, recovery, identity, matter_id, _ = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="RECOVERY_REQUIRED",
            control_health=LedgerExceptionControlHealth.RECOVERY_REQUIRED,
        )
        old_transfer_key = "lost-browser-recovery-0001.transfer"
        old_run_id = _id()
        store.intent_receipts[old_transfer_key] = (
            ExceptionControlRecoveryIntentReceipt(
                command_name=(
                    "PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY"
                ),
                idempotency_key=old_transfer_key,
                matter_id=matter_id,
                matter_version=9,
                recovery_intent_id=_id(),
                recovery_state="PENDING",
                transfer_idempotency_key=old_transfer_key,
                replacement_run_id=old_run_id,
                run_exists=True,
            )
        )

        receipt = service.recover_exception_followups(
            identity=identity,
            matter_id=matter_id,
            expected_matter_version=9,
            idempotency_key="fresh-browser-recovery-0002",
            now=datetime.now(timezone.utc),
        )

        self.assertTrue(receipt.recovery_started)
        self.assertEqual(recovery.calls, [])
        transfer = [call for name, call in store.calls if name == "transfer"][-1]
        self.assertEqual(transfer["idempotency_key"], old_transfer_key)
        self.assertEqual(transfer["replacement_run_id"], old_run_id)
        self.assertNotIn("replacement_run_id", receipt.__dict__)

    def test_recovery_commit_lost_replay_survives_completed_active_projection(self) -> None:
        service, store, recovery, identity, matter_id, _ = self._service(
            role=Role.LEAD_LAWYER,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="RECOVERY_REQUIRED",
            control_health=LedgerExceptionControlHealth.RECOVERY_REQUIRED,
        )
        kwargs = dict(
            identity=identity,
            matter_id=matter_id,
            expected_matter_version=9,
            idempotency_key="ledger-followup-recovery-commit-lost-0001",
            now=datetime.now(timezone.utc),
        )
        first = service.recover_exception_followups(**kwargs)
        # The replacement worker may finish the final ACTIVE follow-up before
        # the browser receives the committed transfer response.
        store.followups = ()
        second = service.recover_exception_followups(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(recovery.calls), 1)
        self.assertEqual(
            len([name for name, _ in store.calls if name == "transfer"]), 2
        )

    def test_non_lead_cannot_recover_control(self) -> None:
        service, _, _, identity, matter_id, _ = self._service(
            role=Role.REVIEWER,
            kind=LedgerExceptionFollowupKind.REEXTRACTION,
            automation_status="RECOVERY_REQUIRED",
            control_health=LedgerExceptionControlHealth.RECOVERY_REQUIRED,
        )
        with self.assertRaises(WebAgentLedgerExceptionFollowupBlocked):
            service.recover_exception_followups(
                identity=identity,
                matter_id=matter_id,
                expected_matter_version=9,
                idempotency_key="ledger-followup-recovery-0002",
                now=datetime.now(timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
