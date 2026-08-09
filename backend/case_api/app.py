"""Synthetic-only FastAPI boundary.

This module deliberately contains no production authentication adapter, database
connection, upload endpoint or model/OCR tool. The supplied actor header selects
from a fixed synthetic registry and is not authentication.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from case_kernel.calculation_engine import (
    AllocationPolicy,
    ApprovedCalculationEvent,
    ApprovedRuleSegment,
    CalculationBlocked,
    CalculationScenario,
    EventKind,
    calculate,
    independently_check,
)
from case_kernel.errors import AuthorizationDenied, IdempotencyConflict, InvalidTransition, PreconditionBlocked, VersionConflict
from case_kernel.legal_rules import InMemoryLegalBundleRegistry, LegalRuleBlocked, synthetic_alpha_legal_bundle
from case_kernel.fact_claim_ledger import FactLedgerBlocked, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.store import InMemoryMatterStore
from case_kernel.workflow import MatterWorkflow

from .alpha_review_state import build_alpha_review_state

from .schemas import (
    ApprovalRequest,
    AlphaReviewResponse,
    AlphaFactConfirmationRequest,
    CalculationLineItemResponse,
    CalculationPreviewRequest,
    CalculationPreviewResponse,
    CreateMatterRequest,
    HealthResponse,
    InvalidateRequest,
    LockSubmissionRequest,
    MatterResponse,
    PaymentAllocationResponse,
    ReceiptResponse,
    VersionedCommand,
)


SYNTHETIC_ACTORS = {
    "alpha_lead_lawyer": Actor(
        actor_id="alpha_lead_lawyer",
        firm_id="alpha_firm_001",
        roles=frozenset({Role.LEAD_LAWYER}),
    ),
    "alpha_assistant": Actor(
        actor_id="alpha_assistant",
        firm_id="alpha_firm_001",
        roles=frozenset({Role.ASSISTANT}),
    ),
}


def get_synthetic_actor(
    x_alpha_actor: Annotated[str | None, Header()] = None,
) -> Actor:
    """A test selector, explicitly not a replacement for login or session middleware."""
    actor = SYNTHETIC_ACTORS.get(x_alpha_actor or "")
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="synthetic actor selection is required; real identities are disabled",
        )
    return actor


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header()] = None,
) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
    return idempotency_key


def to_receipt_response(receipt) -> ReceiptResponse:
    return ReceiptResponse(
        command_name=receipt.command_name,
        idempotency_key=receipt.idempotency_key,
        matter_id=receipt.matter_id,
        matter_version=receipt.matter_version,
        audit_event_id=receipt.audit_event_id,
    )


def to_matter_response(matter) -> MatterResponse:
    return MatterResponse(
        matter_id=matter.matter_id,
        title=matter.title,
        stage=matter.stage.value,
        version=matter.version,
        current_submission_bundle_id=matter.current_submission_bundle_id,
    )


def to_calculation_preview(
    body: CalculationPreviewRequest,
    actor: Actor,
    legal_bundles: InMemoryLegalBundleRegistry,
) -> CalculationPreviewResponse:
    if Role.LEAD_LAWYER not in actor.roles:
        raise AuthorizationDenied("only the lead lawyer can request an approved calculation preview")
    scenario = CalculationScenario(
        scenario_id=body.scenario_id,
        version=body.version,
        start_date=body.start_date,
        end_date=body.end_date,
        events=tuple(
            ApprovedCalculationEvent(
                event_id=event.event_id,
                effective_date=event.effective_date,
                sequence=event.sequence,
                kind=EventKind(event.kind),
                amount=event.amount,
                currency=event.currency,
                evidence_ids=tuple(event.evidence_ids),
                approved_by=actor.actor_id,
                approval_hash=event.approval_hash,
            )
            for event in body.events
        ),
        rule_segments=tuple(
            ApprovedRuleSegment(
                segment_id=segment.segment_id,
                start_date=segment.start_date,
                end_date=segment.end_date,
                annual_rate=segment.annual_rate,
                source_rule_version=segment.source_rule_version,
                applicability_anchor=segment.applicability_anchor,
                approved_by=actor.actor_id,
                approval_hash=segment.approval_hash,
            )
            for segment in body.rule_segments
        ),
        legal_bundle=legal_bundles.get_reference(body.legal_bundle_id),
        allocation_policy=AllocationPolicy(body.allocation_policy),
        approved_by=actor.actor_id,
        approval_hash=body.approval_hash,
    )
    run = calculate(scenario)
    independent_check = independently_check(scenario, run)
    if not independent_check.matching:
        raise RuntimeError("independent calculation check did not match")
    return CalculationPreviewResponse(
        run_id=run.run_id,
        engine_version=run.engine_version,
        legal_bundle_id=run.legal_bundle_id,
        legal_bundle_hash=run.legal_bundle_hash,
        input_hash=run.input_hash,
        output_hash=run.output_hash,
        independent_check_match=independent_check.matching,
        total_interest_accrued=run.total_interest_accrued,
        total_interest_paid=run.total_interest_paid,
        remaining_principal=run.remaining_principal,
        remaining_unpaid_interest=run.remaining_unpaid_interest,
        unapplied_payments=run.unapplied_payments,
        line_items=tuple(CalculationLineItemResponse(**item.__dict__) for item in run.line_items),
        payment_allocations=tuple(PaymentAllocationResponse(**item.__dict__) for item in run.payment_allocations),
    )


def create_app(
    store: InMemoryMatterStore | None = None,
    legal_bundles: InMemoryLegalBundleRegistry | None = None,
) -> FastAPI:
    """Create a contract-testable API with an injected test repository."""
    workflow = MatterWorkflow(store or InMemoryMatterStore())
    legal_bundle_registry = legal_bundles or InMemoryLegalBundleRegistry((synthetic_alpha_legal_bundle(),))
    alpha_review_state = build_alpha_review_state()
    app = FastAPI(
        title="律所案件 AI 工作台 · 合成 Alpha API",
        version="0.1.0",
        description="只允许合成数据的内部 API；无生产认证、无真实材料接入。",
        docs_url="/docs",
        redoc_url=None,
    )
    # Synthetic Alpha only. Production origins are configured by the desktop
    # application after authenticated local IPC and must not inherit this list.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://[::1]:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Alpha-Actor", "Idempotency-Key"],
    )

    @app.exception_handler(AuthorizationDenied)
    async def authorization_handler(_: Request, exc: AuthorizationDenied):
        return _error(status.HTTP_403_FORBIDDEN, str(exc))

    @app.exception_handler(VersionConflict)
    async def version_handler(_: Request, exc: VersionConflict):
        return _error(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_handler(_: Request, exc: IdempotencyConflict):
        return _error(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(InvalidTransition)
    @app.exception_handler(PreconditionBlocked)
    async def precondition_handler(_: Request, exc: InvalidTransition | PreconditionBlocked):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.exception_handler(CalculationBlocked)
    async def calculation_handler(_: Request, exc: CalculationBlocked):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.exception_handler(LegalRuleBlocked)
    async def legal_rule_handler(_: Request, exc: LegalRuleBlocked):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.exception_handler(FactLedgerBlocked)
    async def fact_ledger_handler(_: Request, exc: FactLedgerBlocked):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.get("/healthz", response_model=HealthResponse, tags=["system"])
    async def healthz() -> HealthResponse:
        return HealthResponse(service="case-api", mode="synthetic-alpha-only", persistence="in-memory")

    @app.post("/v1/matters", response_model=ReceiptResponse, status_code=status.HTTP_201_CREATED, tags=["matters"])
    async def create_matter(
        body: CreateMatterRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReceiptResponse:
        return to_receipt_response(workflow.create_matter(actor, matter_id=body.matter_id, title=body.title, idempotency_key=idempotency_key))

    @app.get("/v1/matters/{matter_id}", response_model=MatterResponse, tags=["matters"])
    async def get_matter(matter_id: str, actor: Annotated[Actor, Depends(get_synthetic_actor)]) -> MatterResponse:
        matter = workflow.get_matter(actor, matter_id=matter_id)
        return to_matter_response(matter)

    @app.post("/v1/matters/{matter_id}/advance", response_model=ReceiptResponse, tags=["matters"])
    async def advance_matter(
        matter_id: str,
        body: VersionedCommand,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReceiptResponse:
        return to_receipt_response(workflow.advance(actor, matter_id=matter_id, expected_version=body.expected_version, idempotency_key=idempotency_key))

    @app.post("/v1/matters/{matter_id}/approvals", response_model=ReceiptResponse, tags=["approvals"])
    async def record_approval(
        matter_id: str,
        body: ApprovalRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReceiptResponse:
        return to_receipt_response(workflow.record_approval(
            actor,
            matter_id=matter_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            approval_type=body.approval_type,
            approved_object_hash=body.approved_object_hash,
        ))

    @app.post("/v1/matters/{matter_id}/lock-submission", response_model=ReceiptResponse, tags=["submissions"])
    async def lock_submission(
        matter_id: str,
        body: LockSubmissionRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReceiptResponse:
        return to_receipt_response(workflow.lock_submission(
            actor,
            matter_id=matter_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            final_text=body.final_text,
        ))

    @app.post("/v1/matters/{matter_id}/invalidate", response_model=ReceiptResponse, tags=["matters"])
    async def invalidate_matter(
        matter_id: str,
        body: InvalidateRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReceiptResponse:
        return to_receipt_response(workflow.invalidate_from_upstream_change(
            actor,
            matter_id=matter_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            change_kind=body.change_kind,
        ))

    @app.post("/v1/calculation-previews", response_model=CalculationPreviewResponse, tags=["calculation"])
    async def calculation_preview(
        body: CalculationPreviewRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
    ) -> CalculationPreviewResponse:
        """Non-persistent synthetic preview. Formal CalculationRun storage comes after PostgreSQL integration."""
        return to_calculation_preview(body, actor, legal_bundle_registry)

    @app.get("/v1/alpha-review", response_model=AlphaReviewResponse, tags=["synthetic-review"])
    async def alpha_review(actor: Annotated[Actor, Depends(get_synthetic_actor)]) -> AlphaReviewResponse:
        """Read-only synthetic fact/transaction fixture produced through the domain ledgers."""
        if Role.LEAD_LAWYER not in actor.roles:
            raise AuthorizationDenied("only the lead lawyer can inspect the approved synthetic review snapshot")
        fact_snapshot = alpha_review_state["fact_ledger"].build_formal_snapshot(actor)
        transaction_snapshot = alpha_review_state["transaction_snapshot"]
        responses_by_claim = {item.claim_id: item for item in fact_snapshot.responses}
        return AlphaReviewResponse(
            mode="synthetic-alpha-only",
            fact_snapshot_hash=fact_snapshot.input_hash,
            transaction_snapshot_hash=transaction_snapshot.input_hash,
            facts=tuple(
                {"fact_id": item.fact_id, "original_text": item.original_text, "origin": item.origin.value, "evidence_count": len(item.evidence_links)}
                for item in fact_snapshot.facts
            ),
            claims=tuple(
                {
                    "claim_id": item.claim_id,
                    "original_claim_text": item.original_claim_text,
                    "claimed_amount": item.claimed_amount,
                    "currency": item.currency,
                    "response_position": responses_by_claim[item.claim_id].position.value,
                    "response_amount": responses_by_claim[item.claim_id].partial_amount,
                }
                for item in fact_snapshot.claims
            ),
            issues=tuple(
                {"issue_id": item.issue_id, "question": item.question, "claim_count": len(item.claim_ids), "fact_count": len(item.confirmed_fact_ids)}
                for item in fact_snapshot.issues
            ),
            transactions=tuple(
                {
                    "event_id": item.event_id,
                    "effective_date": item.effective_date,
                    "kind": item.kind.value,
                    "amount": item.amount,
                    "currency": item.currency,
                    "payment_application": item.payment_application.value,
                    "evidence_ids": item.evidence_ids,
                }
                for item in transaction_snapshot.events
            ),
            pending_facts=tuple(
                {
                    "fact_id": fact.fact_id,
                    "original_text": fact.original_text,
                    "origin": fact.origin.value,
                    "evidence_count": len(fact.evidence_links),
                }
                for fact_id in alpha_review_state["pending_fact_ids"]
                for fact in (alpha_review_state["fact_ledger"].get_fact(fact_id),)
                if fact.status.value == "CANDIDATE"
            ),
        )

    @app.post("/v1/alpha-review/facts/{fact_id}/confirm", response_model=AlphaReviewResponse, tags=["synthetic-review"])
    async def confirm_alpha_fact(
        fact_id: str,
        body: AlphaFactConfirmationRequest,
        actor: Annotated[Actor, Depends(get_synthetic_actor)],
    ) -> AlphaReviewResponse:
        if Role.LEAD_LAWYER not in actor.roles:
            raise AuthorizationDenied("only the lead lawyer can confirm a synthetic fact candidate")
        if fact_id not in alpha_review_state["pending_fact_ids"]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown synthetic fact candidate")
        alpha_review_state["fact_ledger"].decide_fact(actor, fact_id=fact_id, status=FactStatus.CONFIRMED, decision_hash=body.approval_hash)
        return await alpha_review(actor)

    return app


def _error(code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=code, content={"detail": detail})


app = create_app()
