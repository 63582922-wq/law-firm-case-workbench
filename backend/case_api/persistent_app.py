"""Independent, fail-closed persistent preview API.

The synthetic Alpha application never imports or mounts these routes. Without
explicit dependencies this factory exposes only a disabled health response.
"""

from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError

from case_kernel.case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    PostgresCaseLedgerStore,
)
from case_kernel.artifact_access import (
    ArtifactAccessPurpose,
    EphemeralArtifactAccessBroker,
)
from case_kernel.evidence_refs import EvidenceLink, EvidenceReferenceBlocked
from case_kernel.evidence_manifest import PageDisposition
from case_kernel.evidence_manifest_postgres import (
    PersistentEvidenceSnapshot,
    PostgresEvidenceManifestStore,
)
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.models import Actor
from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked
from case_kernel.runtime import RuntimeMode, RuntimeSettings
from case_kernel.request_context import current_request_id, reset_request_id, set_request_id
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
)

from .persistent_identity import (
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
    ServerIdentityResolver,
)
from .schemas import (
    CaseLedgerReceiptResponse,
    PersistentArtifactAccessRequest,
    PersistentArtifactAccessResponse,
    PersistentCaseSnapshotResponse,
    PersistentApprovalRequest,
    PersistentClaimCandidateRequest,
    PersistentClaimResponseRequest,
    PersistentConfirmationRequest,
    PersistentDisputeIssueCandidateRequest,
    PersistentDuplicateGroupCandidateRequest,
    PersistentDuplicateGroupResolutionRequest,
    PersistentEvidenceAnnotationRequest,
    PersistentEvidenceDuplicateGroupRequest,
    PersistentEvidenceDuplicateResolutionRequest,
    PersistentEvidenceDerivativeCandidateRequest,
    PersistentEvidenceDerivativeVerificationRequest,
    PersistentEvidenceOriginalRequest,
    PersistentEvidencePageDecisionRequest,
    PersistentEvidenceSnapshotResponse,
    PersistentFactCandidateRequest,
    PersistentFactDecisionRequest,
    PersistentFactResponse,
    PersistentPaymentClassificationCandidateRequest,
    PersistentTransactionCandidateRequest,
)


class PersistentFactLedgerPort(Protocol):
    def create_fact_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def decide_fact(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def list_facts(self, *, matter_id: str, actor: Actor): ...

    def create_claim_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_claim_scope(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def set_claim_response(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_dispute_issue_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_dispute_issue(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_transaction_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_transaction(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_payment_classification_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_payment_classification(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_duplicate_group_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def resolve_duplicate_group(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_case_snapshot(self, *, matter_id: str, actor: Actor): ...


class PersistentEvidenceManifestPort(Protocol):
    def register_original_file(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_page_decision_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_page_decision(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_annotation_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_annotation(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_duplicate_group_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def resolve_duplicate_group(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def lock_manifest(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def register_derivative_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def verify_derivative(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_evidence_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentEvidenceSnapshot: ...

    def get_verified_derivative_locator(self, *, matter_id: str, derivative_id: str, actor: Actor): ...


class PersistentRequestBlocked(ValueError):
    pass


class PersistentEvidenceServiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistentApiDependencies:
    settings: RuntimeSettings
    case_ledger_store: PersistentFactLedgerPort
    identity_resolver: ServerIdentityResolver
    evidence_manifest_store: PersistentEvidenceManifestPort | None = None
    artifact_access_broker: EphemeralArtifactAccessBroker | None = None
    artifact_store: LocalEncryptedArtifactStore | None = None

    def validate(self) -> None:
        if self.settings.mode is not RuntimeMode.POSTGRES_INTERNAL_PREVIEW:
            raise ValueError("persistent API requires postgres-internal-preview runtime settings")
        if not isinstance(self.case_ledger_store, PostgresCaseLedgerStore):
            # Test doubles must explicitly opt in via the marker; arbitrary
            # objects cannot accidentally become a production persistence port.
            if not getattr(self.case_ledger_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL case ledger store")
        if self.evidence_manifest_store is not None and not isinstance(
            self.evidence_manifest_store, PostgresEvidenceManifestStore
        ):
            if not getattr(self.evidence_manifest_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL evidence Manifest store")
        if (self.artifact_access_broker is None) != (self.artifact_store is None):
            raise ValueError("artifact access broker and encrypted artifact store must be configured together")
        if self.artifact_access_broker is not None and self.evidence_manifest_store is None:
            raise ValueError("artifact access requires the guarded evidence Manifest store")


def create_persistent_app(dependencies: PersistentApiDependencies | None = None) -> FastAPI:
    enabled = dependencies is not None
    if dependencies is not None:
        dependencies.validate()
    app = FastAPI(
        title="律所案件 AI 工作台 · 持久化预览 API" if enabled else "律所案件 AI 工作台 · 持久化 API 已禁用",
        version="0.1.0",
        docs_url="/docs" if enabled else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = str(uuid4())
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_request_id(token)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        if not enabled:
            return {"service": "persistent-case-api", "mode": "disabled", "persistence": "not-configured"}
        return {
            "service": "persistent-case-api",
            "mode": "postgres-internal-preview",
            "persistence": "configured-not-probed",
            "evidence_manifest": "configured" if dependencies.evidence_manifest_store else "not-configured",
            "artifact_access": "configured" if dependencies.artifact_access_broker else "not-configured",
        }

    if dependencies is None:
        return app

    async def get_identity(request: Request) -> ServerIdentityContext:
        identity = await dependencies.identity_resolver.resolve(request)
        identity.validate()
        return identity

    def get_idempotency_key(
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> str:
        if idempotency_key is None or not idempotency_key.strip():
            raise PersistentRequestBlocked("Idempotency-Key header is required")
        return idempotency_key.strip()

    def get_evidence_store() -> PersistentEvidenceManifestPort:
        if dependencies.evidence_manifest_store is None:
            raise PersistentEvidenceServiceUnavailable("evidence Manifest persistence is not configured")
        return dependencies.evidence_manifest_store

    def require_artifact_services() -> tuple[EphemeralArtifactAccessBroker, LocalEncryptedArtifactStore]:
        if dependencies.artifact_access_broker is None or dependencies.artifact_store is None:
            raise PersistentEvidenceServiceUnavailable("encrypted artifact access is not configured")
        return dependencies.artifact_access_broker, dependencies.artifact_store

    @app.exception_handler(PersistentAuthenticationBlocked)
    async def authentication_handler(_: Request, exc: PersistentAuthenticationBlocked):
        del exc
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "AUTHENTICATION_REQUIRED",
            "登录状态无效或已过期，请重新登录。",
        )

    @app.exception_handler(PermissionError)
    async def permission_handler(_: Request, exc: PermissionError):
        del exc
        return _error(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED", "你没有执行该案件操作的权限。")

    @app.exception_handler(VersionConflict)
    @app.exception_handler(IdempotencyConflict)
    async def conflict_handler(_: Request, exc: VersionConflict | IdempotencyConflict):
        del exc
        return _error(
            status.HTTP_409_CONFLICT,
            "VERSION_OR_IDEMPOTENCY_CONFLICT",
            "案件已发生变化，请刷新后重新确认本次操作。",
        )

    @app.exception_handler(CaseLedgerPersistenceBlocked)
    @app.exception_handler(EvidenceReferenceBlocked)
    async def ledger_handler(_: Request, exc: CaseLedgerPersistenceBlocked | EvidenceReferenceBlocked):
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "LEDGER_PRECONDITION_BLOCKED",
            "当前事实、证据或审批状态不满足该操作条件。",
        )

    @app.exception_handler(KeyError)
    async def missing_handler(_: Request, exc: KeyError):
        del exc
        return _error(status.HTTP_404_NOT_FOUND, "OBJECT_NOT_FOUND", "未找到本案中的对应记录。")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "REQUEST_VALIDATION_FAILED",
            "提交内容格式不完整或不符合要求，请检查后重试。",
        )

    @app.exception_handler(PersistentRequestBlocked)
    async def request_handler(_: Request, exc: PersistentRequestBlocked):
        del exc
        return _error(
            status.HTTP_400_BAD_REQUEST,
            "REQUEST_PRECONDITION_BLOCKED",
            "本次操作缺少必要的请求标识，请重新提交。",
        )

    @app.exception_handler(PersistentEvidenceServiceUnavailable)
    async def evidence_service_handler(_: Request, exc: PersistentEvidenceServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EVIDENCE_SERVICE_UNAVAILABLE",
            "证据持久化服务尚未启用，未回退到合成数据。",
        )

    @app.exception_handler(ManagedArtifactBlocked)
    async def managed_artifact_handler(_: Request, exc: ManagedArtifactBlocked):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ARTIFACT_INTEGRITY_BLOCKED",
            "证据派生件完整性核验未通过，系统已停止读取。",
        )

    @app.get(
        "/v1/matters/{matter_id}/snapshot",
        response_model=PersistentCaseSnapshotResponse,
        tags=["matters"],
    )
    async def get_case_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentCaseSnapshotResponse:
        snapshot = dependencies.case_ledger_store.get_case_snapshot(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentCaseSnapshotResponse.model_validate(snapshot.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/facts",
        response_model=tuple[PersistentFactResponse, ...],
        tags=["facts"],
    )
    async def list_facts(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> tuple[PersistentFactResponse, ...]:
        facts = dependencies.case_ledger_store.list_facts(matter_id=str(matter_id), actor=identity.actor)
        return tuple(
            PersistentFactResponse(
                fact_id=fact.fact_id,
                original_text=fact.original_text,
                origin=fact.origin.value,
                status=fact.status.value,
                evidence_count=len(fact.evidence_links),
                decision_hash=fact.decision_hash,
            )
            for fact in facts
        )

    @app.post(
        "/v1/matters/{matter_id}/facts",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["facts"],
    )
    async def create_fact_candidate(
        matter_id: UUID,
        body: PersistentFactCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.create_fact_candidate(
            matter_id=str(matter_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            original_text=body.original_text,
            origin=AssertionOrigin(body.origin),
            evidence_links=_evidence_links(body.evidence_links),
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/facts/{fact_id}/decision",
        response_model=CaseLedgerReceiptResponse,
        tags=["facts"],
    )
    async def decide_fact(
        matter_id: UUID,
        fact_id: UUID,
        body: PersistentFactDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.decide_fact(
            matter_id=str(matter_id),
            fact_id=str(fact_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            status=FactStatus(body.status),
            decision_hash=body.decision_hash,
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/claims",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["claims"],
    )
    async def create_claim_candidate(
        matter_id: UUID,
        body: PersistentClaimCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_claim_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                original_claim_text=body.original_claim_text,
                claimed_amount=body.claimed_amount,
                currency=body.currency,
                evidence_links=_evidence_links(body.evidence_links),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/claims/{claim_id}/confirm-scope",
        response_model=CaseLedgerReceiptResponse,
        tags=["claims"],
    )
    async def confirm_claim_scope(
        matter_id: UUID,
        claim_id: UUID,
        body: PersistentConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_claim_scope(
                matter_id=str(matter_id),
                claim_id=str(claim_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                confirmation_hash=body.confirmation_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/claims/{claim_id}/response",
        response_model=CaseLedgerReceiptResponse,
        tags=["claims"],
    )
    async def set_claim_response(
        matter_id: UUID,
        claim_id: UUID,
        body: PersistentClaimResponseRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.set_claim_response(
                matter_id=str(matter_id),
                claim_id=str(claim_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                position=ClaimResponsePosition(body.position),
                confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
                partial_amount=body.partial_amount,
                currency=body.currency,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/issues",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["issues"],
    )
    async def create_dispute_issue_candidate(
        matter_id: UUID,
        body: PersistentDisputeIssueCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_dispute_issue_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                question=body.question,
                claim_ids=tuple(str(value) for value in body.claim_ids),
                confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/issues/{issue_id}/confirm",
        response_model=CaseLedgerReceiptResponse,
        tags=["issues"],
    )
    async def confirm_dispute_issue(
        matter_id: UUID,
        issue_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_dispute_issue(
                matter_id=str(matter_id),
                issue_id=str(issue_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_transaction_candidate(
        matter_id: UUID,
        body: PersistentTransactionCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_transaction_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                local_date=body.local_date,
                date_precision=DatePrecision(body.date_precision),
                amount=body.amount,
                currency=body.currency,
                direction=TransactionDirection(body.direction),
                payer_label=body.payer_label,
                payee_label=body.payee_label,
                channel=TransactionChannel(body.channel),
                transaction_reference=body.transaction_reference,
                evidence_links=_evidence_links(body.evidence_links),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions/{transaction_id}/confirm",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def confirm_transaction(
        matter_id: UUID,
        transaction_id: UUID,
        body: PersistentConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_transaction(
                matter_id=str(matter_id),
                transaction_id=str(transaction_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                confirmation_hash=body.confirmation_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions/{transaction_id}/payment-classifications",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_payment_classification_candidate(
        matter_id: UUID,
        transaction_id: UUID,
        body: PersistentPaymentClassificationCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_payment_classification_candidate(
                matter_id=str(matter_id),
                transaction_id=str(transaction_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                origin=ClassificationOrigin(body.origin),
                nature=PaymentNature(body.nature),
                allocations=tuple(
                    ObligationAllocation(
                        obligation_id=item.obligation_id,
                        amount=item.amount,
                        currency=item.currency,
                    )
                    for item in body.allocations
                ),
                same_day_sequence=body.same_day_sequence,
                evidence_links=_evidence_links(body.evidence_links),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/payment-classifications/{classification_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def approve_payment_classification(
        matter_id: UUID,
        classification_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.approve_payment_classification(
                matter_id=str(matter_id),
                classification_id=str(classification_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transaction-duplicate-groups",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_duplicate_group_candidate(
        matter_id: UUID,
        body: PersistentDuplicateGroupCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_duplicate_group_candidate(
                matter_id=str(matter_id),
                transaction_ids=tuple(str(value) for value in body.transaction_ids),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transaction-duplicate-groups/{duplicate_group_id}/resolve",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def resolve_duplicate_group(
        matter_id: UUID,
        duplicate_group_id: UUID,
        body: PersistentDuplicateGroupResolutionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.resolve_duplicate_group(
                matter_id=str(matter_id),
                duplicate_group_id=str(duplicate_group_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                same_economic_event=body.same_economic_event,
                canonical_transaction_id=(str(body.canonical_transaction_id) if body.canonical_transaction_id else None),
                approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-snapshot",
        response_model=PersistentEvidenceSnapshotResponse,
        tags=["evidence"],
    )
    async def get_evidence_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceSnapshotResponse:
        snapshot = evidence_store.get_evidence_snapshot(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentEvidenceSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/evidence-originals",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def register_evidence_original(
        matter_id: UUID,
        body: PersistentEvidenceOriginalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.register_original_file(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                original_label=body.original_label,
                original_file_sha256=body.original_file_sha256,
                byte_size=body.byte_size,
                media_type=body.media_type,
                page_count=body.page_count,
                source_scan_fingerprint=body.source_scan_fingerprint,
                supersedes_file_id=(str(body.supersedes_file_id) if body.supersedes_file_id else None),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/decisions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_page_decision(
        matter_id: UUID,
        evidence_page_id: UUID,
        body: PersistentEvidencePageDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_page_decision_candidate(
                matter_id=str(matter_id),
                evidence_page_id=str(evidence_page_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                disposition=PageDisposition(body.disposition),
                reason=body.reason,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-page-decisions/{decision_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def approve_evidence_page_decision(
        matter_id: UUID,
        decision_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.approve_page_decision(
                matter_id=str(matter_id),
                decision_id=str(decision_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/annotations",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_annotation(
        matter_id: UUID,
        evidence_page_id: UUID,
        body: PersistentEvidenceAnnotationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_annotation_candidate(
                matter_id=str(matter_id),
                evidence_page_id=str(evidence_page_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                x0=float(body.x0),
                y0=float(body.y0),
                x1=float(body.x1),
                y1=float(body.y1),
                label=body.label,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-annotations/{annotation_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def approve_evidence_annotation(
        matter_id: UUID,
        annotation_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.approve_annotation(
                matter_id=str(matter_id),
                annotation_id=str(annotation_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-duplicate-groups",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_duplicate_group(
        matter_id: UUID,
        body: PersistentEvidenceDuplicateGroupRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_duplicate_group_candidate(
                matter_id=str(matter_id),
                evidence_page_ids=tuple(str(value) for value in body.evidence_page_ids),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-duplicate-groups/{duplicate_group_id}/resolve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def resolve_evidence_duplicate_group(
        matter_id: UUID,
        duplicate_group_id: UUID,
        body: PersistentEvidenceDuplicateResolutionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.resolve_duplicate_group(
                matter_id=str(matter_id),
                duplicate_group_id=str(duplicate_group_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                same_source_page=body.same_source_page,
                canonical_page_id=(str(body.canonical_page_id) if body.canonical_page_id else None),
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-manifests/lock",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def lock_evidence_manifest(
        matter_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.lock_manifest(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-manifests/{manifest_id}/derivatives",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence-worker"],
    )
    async def register_evidence_derivative(
        matter_id: UUID,
        manifest_id: UUID,
        body: PersistentEvidenceDerivativeCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.register_derivative_candidate(
                matter_id=str(matter_id),
                manifest_id=str(manifest_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                manifest_content_hash=body.manifest_content_hash,
                artifact_type=body.artifact_type,
                storage_object_key=body.storage_object_key,
                artifact_sha256=body.artifact_sha256,
                page_count=body.page_count,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/verify",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def verify_evidence_derivative(
        matter_id: UUID,
        derivative_id: UUID,
        body: PersistentEvidenceDerivativeVerificationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.verify_derivative(
                matter_id=str(matter_id),
                derivative_id=str(derivative_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                verification_hash=body.verification_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/access",
        response_model=PersistentArtifactAccessResponse,
        tags=["evidence-access"],
    )
    async def issue_evidence_derivative_access(
        matter_id: UUID,
        derivative_id: UUID,
        body: PersistentArtifactAccessRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentArtifactAccessResponse:
        broker, _ = require_artifact_services()
        locator = evidence_store.get_verified_derivative_locator(
            matter_id=str(matter_id),
            derivative_id=str(derivative_id),
            actor=identity.actor,
        )
        issued = broker.issue(
            locator=locator,
            purpose=ArtifactAccessPurpose(body.purpose),
            actor=identity.actor,
            session=_local_session(identity),
        )
        return PersistentArtifactAccessResponse(
            grant_id=issued.grant_id,
            derivative_id=issued.derivative_id,
            purpose=issued.purpose.value,
            access_token=issued.access_token,
            expires_at=issued.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/content",
        response_class=Response,
        tags=["evidence-access"],
    )
    async def deliver_evidence_derivative(
        matter_id: UUID,
        derivative_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        broker, artifact_store = require_artifact_services()
        access_token = _bearer_token(authorization)
        if request.client is None:
            raise PersistentRequestBlocked("local client address is unavailable")
        delivery = broker.deliver(
            access_token=access_token,
            actor=identity.actor,
            matter_id=str(matter_id),
            derivative_id=str(derivative_id),
            session=_local_session(identity),
            client_ip=request.client.host,
            artifact_store=artifact_store,
        )
        disposition = "inline" if delivery.purpose is ArtifactAccessPurpose.INLINE_PREVIEW else "attachment"
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": f'{disposition}; filename="{delivery.file_name}"',
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.artifact_sha256,
            },
        )

    return app


def _receipt(receipt: CaseLedgerCommandReceipt) -> CaseLedgerReceiptResponse:
    return CaseLedgerReceiptResponse(**receipt.__dict__)


def _local_session(identity: ServerIdentityContext) -> LocalSessionProof:
    return LocalSessionProof(
        session_id=identity.session_id,
        authentication_method=identity.authentication_method.value,
        authenticated_at=identity.authenticated_at,
        expires_at=identity.expires_at,
    )


def _bearer_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise PersistentRequestBlocked("artifact delivery requires a bearer token")
    token = value.removeprefix("Bearer ").strip()
    if not token:
        raise PersistentRequestBlocked("artifact delivery requires a bearer token")
    return token


def _evidence_links(items) -> tuple[EvidenceLink, ...]:
    return tuple(
        EvidenceLink(
            evidence_id=item.evidence_id,
            original_file_sha256=item.original_file_sha256,
            page_number=item.page_number,
            region_id=item.region_id,
            original_label=item.original_label,
        )
        for item in items
    )


def _error(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": current_request_id(),
        },
    )
