"""Hard state-machine controller for the synthetic internal Alpha."""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from .errors import AuthorizationDenied, InvalidTransition, PreconditionBlocked
from .models import (
    ApprovalBinding,
    Actor,
    AuditEvent,
    Matter,
    MatterStage,
    Role,
    SubmissionBundle,
    SubmissionLifecycle,
    SubmissionValidity,
)
from .store import InMemoryMatterStore


FORWARD_TRANSITIONS: dict[MatterStage, MatterStage] = {
    MatterStage.CREATED: MatterStage.INGESTING,
    MatterStage.INGESTING: MatterStage.MATERIAL_REVIEW,
    MatterStage.MATERIAL_REVIEW: MatterStage.FACT_REVIEW,
    MatterStage.FACT_REVIEW: MatterStage.CLAIM_REVIEW,
    MatterStage.CLAIM_REVIEW: MatterStage.LEGAL_REVIEW,
    MatterStage.LEGAL_REVIEW: MatterStage.CALCULATION_REVIEW,
    MatterStage.CALCULATION_REVIEW: MatterStage.DRAFT_REVIEW,
    MatterStage.DRAFT_REVIEW: MatterStage.FINAL_QA,
    MatterStage.FINAL_QA: MatterStage.READY_TO_EXPORT,
    MatterStage.READY_TO_EXPORT: MatterStage.EXPORTED,
    MatterStage.EXPORTED: MatterStage.ARCHIVED,
}

INVALIDATION_TARGETS: dict[str, MatterStage] = {
    "SOURCE_CHANGED": MatterStage.MATERIAL_REVIEW,
    "FACT_CHANGED": MatterStage.FACT_REVIEW,
    "CLAIM_CHANGED": MatterStage.CLAIM_REVIEW,
    "LEGAL_RULE_CHANGED": MatterStage.LEGAL_REVIEW,
    "CALCULATION_CHANGED": MatterStage.CALCULATION_REVIEW,
    "DRAFT_CHANGED": MatterStage.DRAFT_REVIEW,
}


class MatterWorkflow:
    """Application service; agents must use it rather than mutating a Matter directly."""

    def __init__(self, store: InMemoryMatterStore) -> None:
        self._store = store

    @staticmethod
    def _require_firm(actor: Actor, matter: Matter) -> None:
        if actor.firm_id != matter.firm_id:
            raise AuthorizationDenied("actor does not belong to the matter's firm")

    @staticmethod
    def _require_lead_lawyer(actor: Actor) -> None:
        if Role.LEAD_LAWYER not in actor.roles:
            raise AuthorizationDenied("only the lead lawyer can perform this protected command")

    def create_matter(
        self,
        actor: Actor,
        *,
        matter_id: str,
        title: str,
        idempotency_key: str,
    ):
        self._require_lead_lawyer(actor)
        return self._store.create(
            matter=Matter(matter_id=matter_id, firm_id=actor.firm_id, title=title),
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def advance(
        self,
        actor: Actor,
        *,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
    ):
        payload = {"command": "ADVANCE_MATTER", "expected_version": str(expected_version)}

        def change(matter: Matter) -> AuditEvent:
            self._require_firm(actor, matter)
            self._require_lead_lawyer(actor)
            target = FORWARD_TRANSITIONS.get(matter.stage)
            if target is None:
                raise InvalidTransition(f"matter cannot advance from {matter.stage.value}")
            if target is MatterStage.EXPORTED:
                bundle_id = matter.current_submission_bundle_id
                if bundle_id is None:
                    raise PreconditionBlocked("a current locked submission is required before export")
                bundle = matter.bundles[bundle_id]
                if bundle.lifecycle is not SubmissionLifecycle.LOCKED or bundle.validity is not SubmissionValidity.VALID:
                    raise PreconditionBlocked("only a valid locked submission can be exported")
            input_version = matter.version
            matter.stage = target
            if target is MatterStage.EXPORTED:
                matter.bundles[matter.current_submission_bundle_id].lifecycle = SubmissionLifecycle.EXPORTED
            matter.version += 1
            return AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="MATTER_ADVANCED",
                input_version=input_version,
                payload={"from": FORWARD_TRANSITIONS_REVERSE[target].value, "to": target.value},
            )

        return self._store.mutate(
            matter_id=matter_id,
            actor_id=actor.actor_id,
            command_name="ADVANCE_MATTER",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
            mutate_matter=change,
        )

    def record_approval(
        self,
        actor: Actor,
        *,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        approval_type: str,
        approved_object_hash: str,
    ):
        if not approved_object_hash.strip():
            raise ValueError("approved_object_hash is required")
        payload = {
            "command": "RECORD_APPROVAL",
            "approval_type": approval_type,
            "approved_object_hash": approved_object_hash,
        }

        def change(matter: Matter) -> AuditEvent:
            self._require_firm(actor, matter)
            self._require_lead_lawyer(actor)
            input_version = matter.version
            matter.version += 1
            matter.approvals[approval_type] = ApprovalBinding(
                approval_type=approval_type,
                object_hash=approved_object_hash,
                approved_matter_version=matter.version,
                approved_by=actor.actor_id,
            )
            return AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="APPROVAL_RECORDED",
                input_version=input_version,
                payload={"approval_type": approval_type},
            )

        return self._store.mutate(
            matter_id=matter_id,
            actor_id=actor.actor_id,
            command_name="RECORD_APPROVAL",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
            mutate_matter=change,
        )

    def lock_submission(
        self,
        actor: Actor,
        *,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        final_text: str,
    ):
        final_text_hash = sha256(final_text.encode("utf-8")).hexdigest()
        payload = {"command": "LOCK_SUBMISSION", "final_text_hash": final_text_hash}

        def change(matter: Matter) -> AuditEvent:
            self._require_firm(actor, matter)
            self._require_lead_lawyer(actor)
            if matter.stage is not MatterStage.READY_TO_EXPORT:
                raise PreconditionBlocked("submission can only be locked from READY_TO_EXPORT")
            final_approval = matter.approvals.get("FINAL_TEXT")
            if final_approval is None or final_approval.approved_matter_version != matter.version:
                raise PreconditionBlocked("final text must be approved at the current matter version")
            if final_approval.object_hash != final_text_hash:
                raise PreconditionBlocked("final text differs from the approved final-text hash")
            if matter.current_submission_bundle_id is not None:
                raise PreconditionBlocked("a current submission bundle already exists")
            input_version = matter.version
            bundle = SubmissionBundle(
                bundle_id=f"bundle_{uuid4().hex}",
                lifecycle=SubmissionLifecycle.LOCKED,
                validity=SubmissionValidity.VALID,
                final_text_hash=final_text_hash,
                approved_by=actor.actor_id,
                approved_matter_version=matter.version,
            )
            matter.bundles[bundle.bundle_id] = bundle
            matter.current_submission_bundle_id = bundle.bundle_id
            matter.version += 1
            return AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="SUBMISSION_LOCKED",
                input_version=input_version,
                payload={"bundle_id": bundle.bundle_id, "final_text_hash": final_text_hash},
            )

        return self._store.mutate(
            matter_id=matter_id,
            actor_id=actor.actor_id,
            command_name="LOCK_SUBMISSION",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
            mutate_matter=change,
        )

    def invalidate_from_upstream_change(
        self,
        actor: Actor,
        *,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        change_kind: str,
    ):
        if change_kind not in INVALIDATION_TARGETS:
            raise ValueError(f"unsupported change_kind: {change_kind}")
        payload = {"command": "INVALIDATE_FROM_UPSTREAM_CHANGE", "change_kind": change_kind}

        def change(matter: Matter) -> AuditEvent:
            self._require_firm(actor, matter)
            self._require_lead_lawyer(actor)
            input_version = matter.version
            target = INVALIDATION_TARGETS[change_kind]
            prior_stage = matter.stage
            matter.stage = target
            invalidated_bundle_id = matter.current_submission_bundle_id
            if invalidated_bundle_id is not None:
                matter.bundles[invalidated_bundle_id].validity = SubmissionValidity.STALE
                matter.current_submission_bundle_id = None
            matter.approvals.clear()
            matter.version += 1
            return AuditEvent.create(
                matter=matter,
                actor=actor,
                event_type="UPSTREAM_CHANGE_INVALIDATED_DEPENDENTS",
                input_version=input_version,
                payload={
                    "change_kind": change_kind,
                    "from_stage": prior_stage.value,
                    "to_stage": target.value,
                    "invalidated_bundle_id": invalidated_bundle_id or "",
                },
            )

        return self._store.mutate(
            matter_id=matter_id,
            actor_id=actor.actor_id,
            command_name="INVALIDATE_FROM_UPSTREAM_CHANGE",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
            mutate_matter=change,
        )


FORWARD_TRANSITIONS_REVERSE = {target: source for source, target in FORWARD_TRANSITIONS.items()}
