from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_agent_ledger_extraction_review import (
    CaseLedgerExtractionReviewBatchProjection,
    CaseLedgerExtractionReviewCandidateProjection,
    CaseLedgerExtractionReviewExcerptProjection,
    WebAgentLedgerExtractionReviewBlocked,
    WebAgentLedgerExtractionReviewService,
    WebAgentLedgerReextractionCohortCapacityExceeded,
    WebAgentLedgerReextractionSourceWindowExceeded,
)
from case_kernel.case_agent_ledger_exception_review import (
    LedgerExceptionBatchState,
    LedgerExceptionDecision,
    LedgerExceptionGroup,
    LedgerExceptionGroupMember,
    LedgerExceptionGroupMemberPage,
    LedgerExceptionMemberExcerpt,
    LedgerExceptionReason,
    LedgerExceptionReextractionCohortCapacityExceeded,
    LedgerExceptionReextractionSourceWindowExceeded,
    LedgerExtractionBatchReviewStatus,
    LedgerLowRiskLaneStatus,
    allowed_exception_decisions,
    exception_group_summary,
    exception_risk_policy,
    exception_source_policy,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _identity(role: Role) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(_id(), _id(), frozenset({role})),
        session_id=_id(),
        issuer="https://identity.lawfirm.example",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


def _batch(
    *,
    matter_id: str,
    current_version: int = 8,
    confirmed_version: int | None = None,
    reason_code: str = "OCR_DERIVED",
) -> CaseLedgerExtractionReviewBatchProjection:
    now = datetime.now(timezone.utc)
    page_one = _id()
    page_two = _id()
    return CaseLedgerExtractionReviewBatchProjection(
        extraction_batch_id=_id(),
        run_id=_id(),
        matter_id=matter_id,
        current_matter_version=current_version,
        source_matter_version=8,
        candidate_count=2,
        low_risk_count=1,
        exception_count=1,
        staged_at=now - timedelta(minutes=2),
        is_current=confirmed_version is None,
        exception_review_is_current=True,
        confirmed_matter_version=confirmed_version,
        confirmed_at=now if confirmed_version is not None else None,
        candidates=(
            CaseLedgerExtractionReviewCandidateProjection(
                extraction_candidate_id=_id(),
                candidate_kind="FACT",
                summary="借款本金已通过银行转账交付。",
                confidence=0.995,
                review_lane="BULK_PROMOTION_ELIGIBLE",
                review_reason_codes=(),
                excerpts=(
                    CaseLedgerExtractionReviewExcerptProjection(
                        evidence_page_id=page_one,
                        page_number=3,
                        text="转账人民币壹拾万元整",
                    ),
                ),
            ),
            CaseLedgerExtractionReviewCandidateProjection(
                extraction_candidate_id=_id(),
                candidate_kind="TRANSACTION",
                summary="2024-02-03 · 100000 CNY · 付款人待核对",
                confidence=0.82,
                review_lane="EXCEPTION_REVIEW",
                review_reason_codes=(reason_code,),
                excerpts=(
                    CaseLedgerExtractionReviewExcerptProjection(
                        evidence_page_id=page_two,
                        page_number=4,
                        text="付款人名称部分无法识别",
                    ),
                ),
            ),
        ),
    )


class _Store:
    def __init__(
        self,
        batch: CaseLedgerExtractionReviewBatchProjection,
        *,
        available: bool = True,
    ) -> None:
        self.batch = batch
        self.available = available
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.exception_receipts: dict[str, SimpleNamespace] = {}
        self.concurrent_version_advance = 0
        exception = next(
            item for item in batch.candidates if item.review_lane == "EXCEPTION_REVIEW"
        )
        source_policy = exception_source_policy(exception.review_reason_codes)
        risk_policy = exception_risk_policy(exception.review_reason_codes)
        self.group = LedgerExceptionGroup(
            group_id=_id(),
            extraction_batch_id=batch.extraction_batch_id,
            candidate_kind=exception.candidate_kind,
            reason_codes=exception.review_reason_codes,
            source_policy=source_policy,
            risk_policy=risk_policy,
            candidate_count=1,
            summary=exception_group_summary(
                candidate_kind=exception.candidate_kind,
                candidate_count=1,
                source_policy=source_policy,
                risk_policy=risk_policy,
            ),
            allowed_decisions=allowed_exception_decisions(
                reason_codes=exception.review_reason_codes,
                source_policy=source_policy,
                risk_policy=risk_policy,
            ),
        )

    def is_available_for_actor(self, **kwargs):
        self.calls.append(("availability", kwargs))
        return self.available

    def list_review_batches(self, **kwargs):
        self.calls.append(("list", kwargs))
        return (self.batch,)

    def confirm_low_risk_batch(self, **kwargs):
        self.calls.append(("confirm", kwargs))
        return SimpleNamespace(
            command_name="CONFIRM_LOW_RISK_CASE_LEDGER_EXTRACTION_BATCH",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            object_type="CASE_LEDGER_EXTRACTION_BATCH",
            object_id=kwargs["extraction_batch_id"],
            audit_event_id=_id(),
        )

    def list_exception_groups(self, **kwargs):
        self.calls.append(("groups", kwargs))
        return (self.group,)

    def read_exception_batch_state(self, **kwargs):
        self.calls.append(("state", kwargs))
        confirmed = self.batch.confirmed_matter_version is not None
        decided = self.group.decision is not None
        low_risk_status = (
            LedgerLowRiskLaneStatus.EMPTY
            if self.batch.low_risk_count == 0
            else LedgerLowRiskLaneStatus.CONFIRMED
            if confirmed
            else LedgerLowRiskLaneStatus.OPEN
        )
        batch_status = (
            LedgerExtractionBatchReviewStatus.RESOLVED
            if decided and (confirmed or self.batch.low_risk_count == 0)
            else LedgerExtractionBatchReviewStatus.LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN
            if confirmed
            else LedgerExtractionBatchReviewStatus.EXCEPTIONS_OPEN
            if self.batch.low_risk_count == 0
            else LedgerExtractionBatchReviewStatus.LOW_RISK_OPEN
        )
        return LedgerExceptionBatchState(
            extraction_batch_id=self.batch.extraction_batch_id,
            run_id=self.batch.run_id,
            matter_id=self.batch.matter_id,
            matter_version=self.batch.current_matter_version,
            low_risk_lane_status=low_risk_status,
            batch_status=batch_status,
            exception_group_count=1,
            decided_exception_group_count=(1 if decided else 0),
        )

    def list_exception_group_members(self, **kwargs):
        self.calls.append(("members", kwargs))
        candidate = next(
            item
            for item in self.batch.candidates
            if item.review_lane == "EXCEPTION_REVIEW"
        )
        return LedgerExceptionGroupMemberPage(
            group_id=self.group.group_id,
            total_count=1,
            offset=0,
            next_offset=None,
            members=(
                LedgerExceptionGroupMember(
                    sequence=1,
                    candidate_kind=candidate.candidate_kind,
                    summary=candidate.summary,
                    confidence=candidate.confidence,
                    reason_codes=candidate.review_reason_codes,
                    excerpts=tuple(
                        LedgerExceptionMemberExcerpt(
                            evidence_page_id=item.evidence_page_id,
                            page_number=item.page_number,
                            text=item.text,
                        )
                        for item in candidate.excerpts
                    ),
                ),
            ),
        )

    def decide_exception_group(self, **kwargs):
        self.calls.append(("decide", kwargs))
        replay = self.exception_receipts.get(kwargs["idempotency_key"])
        if replay is not None:
            return replay
        self.group = LedgerExceptionGroup(
            **{
                **self.group.__dict__,
                "decision": kwargs["decision"],
                "decision_reason": kwargs["reason"],
            }
        )
        final_exception_only = self.batch.low_risk_count == 0
        if final_exception_only:
            self.batch = replace(
                self.batch,
                current_matter_version=kwargs["expected_version"] + 1,
                is_current=False,
            )
        receipt = SimpleNamespace(
            command_name="DECIDE_CASE_LEDGER_EXCEPTION_GROUP",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=(
                kwargs["expected_version"] + 1
                if final_exception_only
                else kwargs["expected_version"]
            ),
            object_type=(
                "CASE_LEDGER_EXTRACTION_RUN_REVIEW"
                if final_exception_only
                else "CASE_LEDGER_EXCEPTION_GROUP"
            ),
            object_id=(self.batch.run_id if final_exception_only else kwargs["exception_group_id"]),
            audit_event_id=_id(),
        )
        self.exception_receipts[kwargs["idempotency_key"]] = receipt
        if self.concurrent_version_advance:
            self.batch = replace(
                self.batch,
                current_matter_version=(
                    self.batch.current_matter_version
                    + self.concurrent_version_advance
                ),
            )
        return receipt


class WebAgentLedgerExtractionReviewServiceTests(unittest.TestCase):
    def test_model_ledger_flag_is_not_presented_as_verified_conflict(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        batch = _batch(matter_id=matter_id, reason_code="CONTRADICTS_CASE_LEDGER")
        store = _Store(batch)
        original_group = store.group
        projected = WebAgentLedgerExtractionReviewService(store=store).list_batches(
            identity=identity, matter_id=matter_id,
        )[0]
        group = projected.exception_groups[0]
        self.assertIn("模型提示", group.summary)
        self.assertIn("尚未核实", group.summary)
        self.assertIn("不证明已有对应台账", group.review_reasons[0])
        self.assertIn("待核实", group.risk_label)
        self.assertIn("尚未核实", projected.exception_candidates[0].review_reasons[0])
        self.assertEqual(store.group, original_group)
        self.assertEqual(group.group_id, original_group.group_id)
        self.assertEqual(projected.exception_count, 1)
        self.assertEqual(projected.low_risk_count, 1)
        self.assertFalse(any(name == "confirm" for name, _ in store.calls))

    def test_read_projection_separates_low_risk_from_exceptions(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        batch = _batch(matter_id=matter_id)
        result = WebAgentLedgerExtractionReviewService(
            store=_Store(batch)
        ).list_batches(identity=identity, matter_id=matter_id)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "REVIEW_READY")
        self.assertTrue(result[0].can_confirm_low_risk)
        self.assertEqual(len(result[0].low_risk_candidates), 1)
        self.assertEqual(len(result[0].exception_candidates), 1)
        self.assertEqual(result[0].low_risk_candidates[0].review_reasons, ())
        self.assertIn("OCR", result[0].exception_candidates[0].review_reasons[0])
        self.assertEqual(result[0].low_risk_candidates[0].excerpts[0].page_number, 3)
        self.assertEqual(result[0].exception_review_status, "OPEN")
        self.assertEqual(result[0].exception_group_count, 1)
        self.assertEqual(result[0].decided_exception_group_count, 0)
        self.assertEqual(result[0].exception_groups[0].candidate_count, 1)
        self.assertTrue(result[0].exception_groups[0].can_decide)

    def test_collaborating_lawyer_can_read_but_cannot_confirm(self) -> None:
        identity = _identity(Role.COLLABORATING_LAWYER)
        matter_id = _id()
        batch = _batch(matter_id=matter_id)
        service = WebAgentLedgerExtractionReviewService(store=_Store(batch))
        self.assertFalse(
            service.list_batches(identity=identity, matter_id=matter_id)[0].can_confirm_low_risk
        )
        with self.assertRaisesRegex(WebAgentLedgerExtractionReviewBlocked, "主办律师"):
            service.confirm_low_risk_batch(
                identity=identity,
                matter_id=matter_id,
                batch_id=batch.extraction_batch_id,
                expected_version=8,
                idempotency_key="ledger-extraction-confirm-0001",
            )

    def test_system_worker_cannot_use_the_lawyer_review_surface(self) -> None:
        identity = _identity(Role.SYSTEM_WORKER)
        matter_id = _id()
        service = WebAgentLedgerExtractionReviewService(
            store=_Store(_batch(matter_id=matter_id))
        )
        with self.assertRaisesRegex(WebAgentLedgerExtractionReviewBlocked, "不能查看"):
            service.list_batches(identity=identity, matter_id=matter_id)

    def test_confirm_sends_only_batch_identity_and_returns_group_counts(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        batch = _batch(matter_id=matter_id)
        store = _Store(batch)
        receipt = WebAgentLedgerExtractionReviewService(store=store).confirm_low_risk_batch(
            identity=identity,
            matter_id=matter_id,
            batch_id=batch.extraction_batch_id,
            expected_version=8,
            idempotency_key="ledger-extraction-confirm-0002",
        )
        self.assertEqual(receipt.matter_version, 9)
        self.assertEqual(receipt.confirmed_fact_count, 1)
        self.assertEqual(receipt.confirmed_transaction_count, 0)
        self.assertEqual(receipt.confirmed_total_count, 1)
        _, kwargs = store.calls[-1]
        self.assertEqual(
            set(kwargs),
            {
                "matter_id",
                "actor",
                "session_id",
                "expected_version",
                "idempotency_key",
                "extraction_batch_id",
            },
        )
        self.assertEqual(kwargs["session_id"], identity.session_id)

    def test_exact_response_loss_replay_may_read_a_confirmed_projection(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        batch = _batch(
            matter_id=matter_id,
            current_version=9,
            confirmed_version=9,
        )
        receipt = WebAgentLedgerExtractionReviewService(
            store=_Store(batch)
        ).confirm_low_risk_batch(
            identity=identity,
            matter_id=matter_id,
            batch_id=batch.extraction_batch_id,
            expected_version=8,
            idempotency_key="ledger-extraction-confirm-0003",
        )
        self.assertEqual(receipt.matter_version, 9)

    def test_confirmed_low_risk_group_does_not_hide_open_exceptions(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        result = WebAgentLedgerExtractionReviewService(
            store=_Store(
                _batch(
                    matter_id=matter_id,
                    current_version=9,
                    confirmed_version=9,
                )
            )
        ).list_batches(identity=identity, matter_id=matter_id)
        self.assertEqual(
            result[0].status,
            "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
        )
        self.assertEqual(result[0].exception_count, 1)
        self.assertFalse(result[0].can_confirm_low_risk)

    def test_unrelated_version_change_makes_open_exception_review_read_only(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        batch = replace(
            _batch(
                matter_id=matter_id,
                current_version=10,
                confirmed_version=9,
            ),
            exception_review_is_current=False,
        )
        result = WebAgentLedgerExtractionReviewService(
            store=_Store(batch)
        ).list_batches(identity=identity, matter_id=matter_id)
        self.assertEqual(result[0].status, "STALE")
        self.assertFalse(result[0].exception_groups[0].can_decide)

    def test_unknown_exception_reason_fails_closed(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        with self.assertRaisesRegex(WebAgentLedgerExtractionReviewBlocked, "原因"):
            WebAgentLedgerExtractionReviewService(
                store=_Store(_batch(matter_id=matter_id, reason_code="NEW_UNREVIEWED_CODE"))
            ).list_batches(identity=identity, matter_id=matter_id)

    def test_unconfigured_firm_is_not_advertised_or_read(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        service = WebAgentLedgerExtractionReviewService(
            store=_Store(_batch(matter_id=matter_id), available=False)
        )
        self.assertFalse(service.is_available(identity=identity))
        with self.assertRaisesRegex(WebAgentLedgerExtractionReviewBlocked, "尚未配置"):
            service.list_batches(identity=identity, matter_id=matter_id)

    def test_exception_members_are_complete_and_hide_internal_candidate_identity(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        store = _Store(_batch(matter_id=matter_id))
        page = WebAgentLedgerExtractionReviewService(
            store=store
        ).list_exception_group_members(
            identity=identity,
            matter_id=matter_id,
            batch_id=store.batch.extraction_batch_id,
            group_id=store.group.group_id,
            offset=0,
            limit=50,
        )
        self.assertEqual(page.total_count, 1)
        self.assertIsNone(page.next_offset)
        self.assertEqual(page.members[0].sequence, 1)
        self.assertEqual(page.members[0].excerpts[0].page_number, 4)
        self.assertFalse(hasattr(page.members[0], "candidate_id"))
        self.assertFalse(hasattr(page.members[0], "candidate_hash"))

    def test_exception_decision_uses_server_session_and_never_accepts_member_subset(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        store = _Store(_batch(matter_id=matter_id))
        receipt = WebAgentLedgerExtractionReviewService(
            store=store
        ).decide_exception_group(
            identity=identity,
            matter_id=matter_id,
            batch_id=store.batch.extraction_batch_id,
            group_id=store.group.group_id,
            expected_version=8,
            idempotency_key="ledger-exception-decision-0001",
            decision="REQUEST_REEXTRACTION",
            reason="SOURCE_QUALITY_INSUFFICIENT",
            reason_note="重新核验原始图片",
        )
        self.assertEqual(receipt.decision, "REQUEST_REEXTRACTION")
        self.assertEqual(receipt.matter_version, 8)
        _, kwargs = next(
            item for item in reversed(store.calls) if item[0] == "decide"
        )
        self.assertEqual(kwargs["server_session_id"], identity.session_id)
        self.assertEqual(
            set(kwargs),
            {
                "matter_id",
                "actor",
                "server_session_id",
                "expected_version",
                "idempotency_key",
                "exception_group_id",
                "decision",
                "reason",
                "reason_note",
            },
        )

    def test_exception_decision_translates_only_deterministic_capacity_blocks(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        cases = (
            (
                LedgerExceptionReextractionSourceWindowExceeded("source window"),
                WebAgentLedgerReextractionSourceWindowExceeded,
            ),
            (
                LedgerExceptionReextractionCohortCapacityExceeded("cohort capacity"),
                WebAgentLedgerReextractionCohortCapacityExceeded,
            ),
        )
        for store_error, expected_error in cases:
            with self.subTest(error=type(store_error).__name__):
                store = _Store(_batch(matter_id=matter_id))

                def blocked_decision(**kwargs):
                    store.calls.append(("decide", kwargs))
                    raise store_error

                store.decide_exception_group = blocked_decision  # type: ignore[method-assign]
                with self.assertRaises(expected_error):
                    WebAgentLedgerExtractionReviewService(
                        store=store
                    ).decide_exception_group(
                        identity=identity,
                        matter_id=matter_id,
                        batch_id=store.batch.extraction_batch_id,
                        group_id=store.group.group_id,
                        expected_version=8,
                        idempotency_key="ledger-exception-capacity-0001",
                        decision="REQUEST_REEXTRACTION",
                        reason="SOURCE_QUALITY_INSUFFICIENT",
                        reason_note=None,
                    )

    def test_exception_only_terminal_state_wins_over_stale_after_version_advance(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        mixed = _batch(matter_id=matter_id)
        exception = next(
            item for item in mixed.candidates if item.review_lane == "EXCEPTION_REVIEW"
        )
        pure_exception = replace(
            mixed,
            candidate_count=1,
            low_risk_count=0,
            exception_count=1,
            candidates=(exception,),
        )
        store = _Store(pure_exception)
        service = WebAgentLedgerExtractionReviewService(store=store)
        self.assertEqual(
            service.list_batches(identity=identity, matter_id=matter_id)[0].status,
            "EXCEPTIONS_ONLY",
        )
        receipt = service.decide_exception_group(
            identity=identity,
            matter_id=matter_id,
            batch_id=store.batch.extraction_batch_id,
            group_id=store.group.group_id,
            expected_version=8,
            idempotency_key="ledger-exception-final-0001",
            decision="REQUEST_REEXTRACTION",
            reason="SOURCE_QUALITY_INSUFFICIENT",
            reason_note=None,
        )
        self.assertEqual(receipt.matter_version, 9)
        self.assertTrue(receipt.batch_resolved)
        self.assertEqual(
            service.list_batches(identity=identity, matter_id=matter_id)[0].status,
            "RESOLVED",
        )
        store.batch = replace(store.batch, current_matter_version=10)
        self.assertEqual(
            service.list_batches(identity=identity, matter_id=matter_id)[0].status,
            "RESOLVED",
        )

    def test_exact_replay_survives_sibling_version_advances(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        store = _Store(_batch(matter_id=matter_id))
        service = WebAgentLedgerExtractionReviewService(store=store)
        kwargs = {
            "identity": identity,
            "matter_id": matter_id,
            "batch_id": store.batch.extraction_batch_id,
            "group_id": store.group.group_id,
            "expected_version": 8,
            "idempotency_key": "ledger-exception-replay-0001",
            "decision": "REQUEST_REEXTRACTION",
            "reason": "SOURCE_QUALITY_INSUFFICIENT",
            "reason_note": "重新核验原始图片",
        }
        first = service.decide_exception_group(**kwargs)
        self.assertEqual(first.committed_matter_version, 8)
        store.batch = replace(
            store.batch,
            current_matter_version=10,
            is_current=False,
        )
        replay = service.decide_exception_group(**kwargs)
        self.assertEqual(replay.committed_matter_version, 8)
        self.assertEqual(replay.matter_version, 10)
        self.assertEqual(
            sum(item[0] == "decide" for item in store.calls),
            2,
        )

    def test_post_read_accepts_concurrent_current_version_advance(self) -> None:
        identity = _identity(Role.LEAD_LAWYER)
        matter_id = _id()
        store = _Store(_batch(matter_id=matter_id))
        store.concurrent_version_advance = 1
        receipt = WebAgentLedgerExtractionReviewService(
            store=store
        ).decide_exception_group(
            identity=identity,
            matter_id=matter_id,
            batch_id=store.batch.extraction_batch_id,
            group_id=store.group.group_id,
            expected_version=8,
            idempotency_key="ledger-exception-concurrent-0001",
            decision="REQUEST_REEXTRACTION",
            reason="SOURCE_QUALITY_INSUFFICIENT",
            reason_note=None,
        )
        self.assertEqual(receipt.committed_matter_version, 8)
        self.assertEqual(receipt.matter_version, 9)


if __name__ == "__main__":
    unittest.main()
