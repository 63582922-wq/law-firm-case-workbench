"""Independent, fail-closed persistent preview API.

The synthetic Alpha application never imports or mounts these routes. Without
explicit dependencies this factory exposes only a disabled health response.
"""

from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from case_kernel.case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    PostgresCaseLedgerStore,
)
from case_kernel.evidence_refs import EvidenceLink, EvidenceReferenceBlocked
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.fact_claim_ledger import AssertionOrigin, FactStatus
from case_kernel.models import Actor
from case_kernel.runtime import RuntimeMode, RuntimeSettings

from .persistent_identity import (
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
    ServerIdentityResolver,
)
from .schemas import (
    CaseLedgerReceiptResponse,
    PersistentFactCandidateRequest,
    PersistentFactDecisionRequest,
    PersistentFactResponse,
)


class PersistentFactLedgerPort(Protocol):
    def create_fact_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def decide_fact(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def list_facts(self, *, matter_id: str, actor: Actor): ...


@dataclass(frozen=True)
class PersistentApiDependencies:
    settings: RuntimeSettings
    case_ledger_store: PersistentFactLedgerPort
    identity_resolver: ServerIdentityResolver

    def validate(self) -> None:
        if self.settings.mode is not RuntimeMode.POSTGRES_INTERNAL_PREVIEW:
            raise ValueError("persistent API requires postgres-internal-preview runtime settings")
        if not isinstance(self.case_ledger_store, PostgresCaseLedgerStore):
            # Test doubles must explicitly opt in via the marker; arbitrary
            # objects cannot accidentally become a production persistence port.
            if not getattr(self.case_ledger_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL case ledger store")


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

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        if not enabled:
            return {"service": "persistent-case-api", "mode": "disabled", "persistence": "not-configured"}
        return {
            "service": "persistent-case-api",
            "mode": "postgres-internal-preview",
            "persistence": "configured-not-probed",
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
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
        return idempotency_key.strip()

    @app.exception_handler(PersistentAuthenticationBlocked)
    async def authentication_handler(_: Request, exc: PersistentAuthenticationBlocked):
        return _error(status.HTTP_401_UNAUTHORIZED, str(exc))

    @app.exception_handler(PermissionError)
    async def permission_handler(_: Request, exc: PermissionError):
        return _error(status.HTTP_403_FORBIDDEN, str(exc))

    @app.exception_handler(VersionConflict)
    @app.exception_handler(IdempotencyConflict)
    async def conflict_handler(_: Request, exc: VersionConflict | IdempotencyConflict):
        return _error(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(CaseLedgerPersistenceBlocked)
    @app.exception_handler(EvidenceReferenceBlocked)
    async def ledger_handler(_: Request, exc: CaseLedgerPersistenceBlocked | EvidenceReferenceBlocked):
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @app.get(
        "/v1/matters/{matter_id}/facts",
        response_model=tuple[PersistentFactResponse, ...],
        tags=["facts"],
    )
    async def list_facts(
        matter_id: str,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> tuple[PersistentFactResponse, ...]:
        facts = dependencies.case_ledger_store.list_facts(matter_id=matter_id, actor=identity.actor)
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
        matter_id: str,
        body: PersistentFactCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.create_fact_candidate(
            matter_id=matter_id,
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            original_text=body.original_text,
            origin=AssertionOrigin(body.origin),
            evidence_links=tuple(
                EvidenceLink(
                    evidence_id=item.evidence_id,
                    original_file_sha256=item.original_file_sha256,
                    page_number=item.page_number,
                    region_id=item.region_id,
                    original_label=item.original_label,
                )
                for item in body.evidence_links
            ),
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/facts/{fact_id}/decision",
        response_model=CaseLedgerReceiptResponse,
        tags=["facts"],
    )
    async def decide_fact(
        matter_id: str,
        fact_id: str,
        body: PersistentFactDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.decide_fact(
            matter_id=matter_id,
            fact_id=fact_id,
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            status=FactStatus(body.status),
            decision_hash=body.decision_hash,
        )
        return _receipt(receipt)

    return app


def _receipt(receipt: CaseLedgerCommandReceipt) -> CaseLedgerReceiptResponse:
    return CaseLedgerReceiptResponse(**receipt.__dict__)


def _error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})
