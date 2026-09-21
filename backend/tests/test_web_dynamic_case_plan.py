from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from case_api.persistent_identity import (
    AuthenticationMethod,
    ServerIdentityContext,
)
from case_api.web_dynamic_case_plan import (
    WebDynamicCasePlanBlocked,
    WebDynamicCasePlanService,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _identity(role: Role = Role.LEAD_LAWYER) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(_id(), _id(), frozenset({role})),
        session_id=_id(),
        issuer="https://identity.lawfirm.example",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class _Store:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_snapshot(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self.snapshot

    def review_item(self, **kwargs):
        self.calls.append(("review", kwargs))
        return SimpleNamespace(
            review_id=_id(),
            plan_id=kwargs["plan_id"],
            item_id=kwargs["item_id"],
            decision=kwargs["decision"],
            matter_version=kwargs["expected_version"],
        )

    def activate_current_plan(self, **kwargs):
        self.calls.append(("activate", kwargs))
        return SimpleNamespace(
            command_name="ACTIVATE_CURRENT_CASE_WORK_PLAN",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            object_type="CASE_WORK_PLAN",
            object_id=self.snapshot.plans[0]["plan_id"],
            matter_version=kwargs["expected_version"] + 1,
        )


def _snapshot(
    *,
    matter_id: str,
    matter_version: int = 10,
    review: str | None = None,
    source_kind: str = "VERIFIED_LEGAL_SOURCE",
):
    plan_id = _id()
    item_id = _id()
    binding_id = _id()
    source_id = _id()
    generated_at = datetime.now(timezone.utc)
    item_reviews = ()
    if review is not None:
        item_reviews = (
            {
                "review_id": _id(),
                "plan_id": plan_id,
                "item_id": item_id,
                "decision": review,
            },
        )
    return SimpleNamespace(
        matter_id=matter_id,
        matter_version=matter_version,
        current_plan=None,
        plans=(
            {
                "plan_id": plan_id,
                "plan_version": 2,
                "status": "CANDIDATE",
                "planned_matter_version": 9,
                "generated_at": generated_at,
                "activated_matter_version": None,
                "stale_reason_code": None,
            },
        ),
        items=(
            {
                "plan_id": plan_id,
                "item_id": item_id,
                "sequence": 1,
                "item_kind": "RESEARCH_TASK",
                "readiness": "ACTIONABLE",
                "title": "核验本案期间适用的官方规则",
                "purpose": "确定下一步计算与文书引用的现行法依据。",
                "rationale": "本案期间跨越规则变化节点，需要以已核验来源为准。",
                "risk_if_omitted": "可能使用错误规则并影响后续交付。",
                "confidence": 0.5,
                "review_gate": "LEGAL_AUTHORITY_REVIEW",
                "delivery_target": "NOT_APPLICABLE",
                "deliverable_kind": None,
                "required_for_delivery": False,
            },
        ),
        references=(),
        prerequisites=(),
        item_references=(
            {
                "plan_id": plan_id,
                "item_id": item_id,
                "reference_role": "SOURCE",
                "source_type": "AGENT_TASK_INPUT",
                "source_id": binding_id,
                "source_version": "v1",
                "display_source_kind": source_kind,
                "display_source_id": source_id,
                "display_locator": "官方法源快照 v1",
            },
        ),
        item_reviews=item_reviews,
        snapshot_hash="a" * 64,
    )


class WebDynamicCasePlanServiceTests(unittest.TestCase):
    def test_all_planning_source_types_have_user_facing_labels(self) -> None:
        from case_kernel.case_agent_planning_snapshot import PlanningProjectionObjectType
        from case_api.web_dynamic_case_plan import _project_reference
        for kind in PlanningProjectionObjectType:
            with self.subTest(kind=kind.value):
                ref = _project_reference({"display_source_kind": kind.value,
                                          "display_source_id": _id(),
                                          "source_version": "v1"})
                self.assertEqual(ref.source_kind, kind.value)
                self.assertTrue(ref.label)

    def test_current_candidate_exposes_purpose_sources_and_one_click_gate(self) -> None:
        identity = _identity()
        matter_id = _id()
        store = _Store(_snapshot(matter_id=matter_id))
        plan = WebDynamicCasePlanService(store=store).current_plan(
            identity=identity, matter_id=matter_id
        )
        assert plan is not None
        self.assertEqual(plan.status, "CANDIDATE")
        self.assertTrue(plan.inputs_current)
        self.assertTrue(plan.can_activate)
        self.assertEqual(plan.reviewed_item_count, 0)
        self.assertIn("确定下一步计算", plan.items[0].purpose)
        self.assertEqual(
            plan.items[0].source_refs[0].source_kind,
            "VERIFIED_LEGAL_SOURCE",
        )
        self.assertNotEqual(
            plan.items[0].source_refs[0].source_id,
            store.snapshot.item_references[0]["source_id"],
        )

    def test_approved_rule_is_projected_as_a_reviewable_authority_source(self) -> None:
        identity = _identity()
        matter_id = _id()
        plan = WebDynamicCasePlanService(
            store=_Store(
                _snapshot(
                    matter_id=matter_id,
                    source_kind="APPROVED_LEGAL_RULE",
                )
            )
        ).current_plan(identity=identity, matter_id=matter_id)

        assert plan is not None
        reference = plan.items[0].source_refs[0]
        self.assertEqual(reference.source_kind, "APPROVED_LEGAL_RULE")
        self.assertEqual(reference.label, "已批准规则版本")

    def test_adverse_item_review_is_not_presented_as_an_applied_edit(self) -> None:
        identity = _identity()
        matter_id = _id()
        plan = WebDynamicCasePlanService(
            store=_Store(_snapshot(matter_id=matter_id, review="REQUEST_CHANGE"))
        ).current_plan(identity=identity, matter_id=matter_id)
        assert plan is not None
        self.assertFalse(plan.can_activate)
        self.assertEqual(plan.items[0].status, "CHANGE_REQUESTED")
        self.assertTrue(any("重新研判" in item for item in plan.activation_blockers))

    def test_optional_non_actionable_deliverable_does_not_block_ready_review_outputs(self) -> None:
        identity = _identity()
        matter_id = _id()
        snapshot = _snapshot(matter_id=matter_id)
        snapshot.items = (
            {
                **snapshot.items[0],
                "item_kind": "DOCUMENT_CANDIDATE",
                "title": "生成案件审阅意见候选",
                "purpose": "根据已确认事实形成律师审阅意见。",
                "delivery_target": "INTERNAL_WORK_PRODUCT",
                "deliverable_kind": "CASE_REVIEW_MEMO",
            },
            {
                **snapshot.items[0],
                "item_id": _id(),
                "sequence": 2,
                "item_kind": "DOCUMENT_CANDIDATE",
                "readiness": "NEEDS_INFORMATION",
                "title": "生成收付款核对表候选",
                "purpose": "将已确认交易编入可复核台账。",
                "delivery_target": "INTERNAL_WORK_PRODUCT",
                "deliverable_kind": "PAYMENT_LEDGER",
            },
        )
        plan = WebDynamicCasePlanService(store=_Store(snapshot)).current_plan(
            identity=identity,
            matter_id=matter_id,
        )
        assert plan is not None
        self.assertTrue(plan.can_activate)
        self.assertFalse(plan.activation_blockers)
        self.assertEqual(plan.items[-1].readiness, "NEEDS_INFORMATION")
        self.assertEqual(plan.items[-1].title, "生成收付款核对表候选")

    def test_no_ready_structured_deliverable_blocks_activation_projection(self) -> None:
        identity = _identity()
        matter_id = _id()
        snapshot = _snapshot(matter_id=matter_id)
        snapshot.items = (
            {
                **snapshot.items[0],
                "item_kind": "DOCUMENT_CANDIDATE",
                "readiness": "NEEDS_INFORMATION",
                "title": "生成收付款核对表候选",
                "purpose": "将已确认交易编入可复核台账。",
                "delivery_target": "INTERNAL_WORK_PRODUCT",
                "deliverable_kind": "PAYMENT_LEDGER",
            },
        )
        plan = WebDynamicCasePlanService(store=_Store(snapshot)).current_plan(
            identity=identity,
            matter_id=matter_id,
        )
        assert plan is not None
        self.assertFalse(plan.can_activate)
        self.assertTrue(any("暂未具备" in item for item in plan.activation_blockers))

    def test_item_review_does_not_advance_matter_and_maps_change_request(self) -> None:
        identity = _identity()
        matter_id = _id()
        snapshot = _snapshot(matter_id=matter_id)
        store = _Store(snapshot)
        service = WebDynamicCasePlanService(store=store)
        receipt = service.decide_item(
            identity=identity,
            matter_id=matter_id,
            plan_id=snapshot.plans[0]["plan_id"],
            item_id=snapshot.items[0]["item_id"],
            expected_version=10,
            idempotency_key="dynamic-plan-review-0001",
            decision="MODIFY",
            reason_code="REQUIRES_FURTHER_RESEARCH",
            readiness_override="NEEDS_RESEARCH",
            required_for_delivery_override=None,
        )
        self.assertEqual(receipt.decision_status, "CHANGE_REQUESTED")
        self.assertTrue(receipt.requires_replanning)
        self.assertEqual(receipt.matter_version, 10)
        _, kwargs = store.calls[-1]
        self.assertEqual(kwargs["decision"], "REQUEST_CHANGE")
        self.assertIs(kwargs["actor"], identity.actor)

    def test_item_review_fails_closed_if_store_reports_a_version_advance(self) -> None:
        identity = _identity()
        matter_id = _id()
        snapshot = _snapshot(matter_id=matter_id)
        store = _Store(snapshot)
        store.review_item = lambda **kwargs: SimpleNamespace(
            review_id=_id(),
            plan_id=kwargs["plan_id"],
            item_id=kwargs["item_id"],
            decision="APPROVE",
            matter_version=kwargs["expected_version"] + 1,
        )
        with self.assertRaisesRegex(WebDynamicCasePlanBlocked, "不得推进"):
            WebDynamicCasePlanService(store=store).decide_item(
                identity=identity,
                matter_id=matter_id,
                plan_id=snapshot.plans[0]["plan_id"],
                item_id=snapshot.items[0]["item_id"],
                expected_version=10,
                idempotency_key="dynamic-plan-review-0004",
                decision="APPROVE",
                reason_code="VERIFIED_BY_COUNSEL",
                readiness_override=None,
                required_for_delivery_override=None,
            )

    def test_activation_passes_no_browser_plan_or_confirmation_hash_to_store(self) -> None:
        identity = _identity()
        matter_id = _id()
        store = _Store(_snapshot(matter_id=matter_id))
        receipt = WebDynamicCasePlanService(store=store).activate_current_plan(
            identity=identity,
            matter_id=matter_id,
            expected_version=10,
            idempotency_key="dynamic-plan-activate-0001",
        )
        self.assertEqual(receipt.status, "ACTIVE")
        self.assertEqual(receipt.matter_version, 11)
        _, kwargs = store.calls[-1]
        self.assertNotIn("plan_id", kwargs)
        self.assertNotIn("confirmation_hash", kwargs)

    def test_activation_fails_closed_on_cross_matter_receipt(self) -> None:
        identity = _identity()
        matter_id = _id()
        store = _Store(_snapshot(matter_id=matter_id))
        store.activate_current_plan = lambda **kwargs: SimpleNamespace(
            command_name="ACTIVATE_CURRENT_CASE_WORK_PLAN",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=_id(),
            object_type="CASE_WORK_PLAN",
            object_id=store.snapshot.plans[0]["plan_id"],
            matter_version=kwargs["expected_version"] + 1,
        )
        with self.assertRaisesRegex(WebDynamicCasePlanBlocked, "回执类型"):
            WebDynamicCasePlanService(store=store).activate_current_plan(
                identity=identity,
                matter_id=matter_id,
                expected_version=10,
                idempotency_key="dynamic-plan-activate-0003",
            )

    def test_non_lead_can_read_but_cannot_review_or_activate(self) -> None:
        identity = _identity(Role.COLLABORATING_LAWYER)
        matter_id = _id()
        snapshot = _snapshot(matter_id=matter_id)
        service = WebDynamicCasePlanService(store=_Store(snapshot))
        plan = service.current_plan(identity=identity, matter_id=matter_id)
        assert plan is not None
        self.assertFalse(plan.can_activate)
        with self.assertRaisesRegex(WebDynamicCasePlanBlocked, "主办律师"):
            service.activate_current_plan(
                identity=identity,
                matter_id=matter_id,
                expected_version=10,
                idempotency_key="dynamic-plan-activate-0002",
            )

    def test_changed_matter_projects_stale_and_blocks_activation(self) -> None:
        identity = _identity()
        matter_id = _id()
        plan = WebDynamicCasePlanService(
            store=_Store(_snapshot(matter_id=matter_id, matter_version=11))
        ).current_plan(identity=identity, matter_id=matter_id)
        assert plan is not None
        self.assertEqual(plan.status, "STALE")
        self.assertFalse(plan.can_activate)
        self.assertTrue(plan.stale_reasons)


if __name__ == "__main__":
    unittest.main()
