from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
import unittest
from zipfile import ZIP_STORED, ZipFile

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from case_api.persistent_app import PersistentApiDependencies, create_persistent_app
from case_api.persistent_identity import (
    AuthenticationMethod,
    DesktopSessionAuthority,
    ServerIdentityContext,
)
from case_kernel.artifact_access import EphemeralArtifactAccessBroker, VerifiedDerivativeLocator
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt, PersistentCaseSnapshot
from case_kernel.evidence_manifest_postgres import PersistentEvidenceSnapshot
from case_kernel.formal_calculation_postgres import PersistentFormalCalculationSnapshot
from case_kernel.fact_claim_ledger import AssertionOrigin, FactAssertion, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.local_access_grants import LocalFolderGrantRegistry
from case_kernel.original_page_access import OriginalPageAccessBroker, OriginalPageLocator
from case_kernel.official_source_capture_postgres import PersistentOfficialSourceCaptureSnapshot
from case_kernel.runtime import RuntimeMode, RuntimeSettings
from case_kernel.submission_postgres import PersistentSubmissionSnapshot
from case_kernel.submission_access import (
    SubmissionExportAccessBroker,
    VerifiedSubmissionExportLocator,
)


class StaticIdentityResolver:
    def __init__(self, identity: ServerIdentityContext) -> None:
        self.identity = identity

    async def resolve(self, request):
        del request
        return self.identity


class FakePersistentFactStore:
    persistent_test_double = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_fact_candidate(self, **kwargs):
        self.calls.append(("create", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="CREATE_FACT_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="FACT",
            object_id=str(uuid4()),
        )
    def decide_fact(self, **kwargs):
        self.calls.append(("decide", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="DECIDE_FACT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="FACT",
            object_id=kwargs["fact_id"],
        )

    def list_facts(self, *, matter_id: str, actor: Actor):
        self.calls.append(("list", {"matter_id": matter_id, "actor": actor}))
        return ()

    def create_transaction_candidate(self, **kwargs):
        self.calls.append(("create_transaction", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="CREATE_TRANSACTION_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="TRANSACTION",
            object_id=str(uuid4()),
        )

    def create_payment_classification_candidate(self, **kwargs):
        self.calls.append(("create_classification", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="CREATE_PAYMENT_CLASSIFICATION_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="PAYMENT_CLASSIFICATION",
            object_id=str(uuid4()),
        )

    def get_case_snapshot(self, *, matter_id: str, actor: Actor):
        self.calls.append(("snapshot", {"matter_id": matter_id, "actor": actor}))
        return PersistentCaseSnapshot(
            matter_id=matter_id,
            title="[合成] 持久化案件快照",
            stage="FACT_REVIEW",
            version=4,
            snapshot_hash="c" * 64,
            facts=(),
            claims=(),
            issues=(),
            transactions=(),
            payment_classifications=(),
            duplicate_groups=(),
        )


class FakePersistentEvidenceStore:
    persistent_test_double = True

    def __init__(
        self,
        *,
        locator: VerifiedDerivativeLocator | None = None,
        original_page_locator: OriginalPageLocator | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.locator = locator
        self.original_page_locator = original_page_locator

    def get_evidence_snapshot(self, *, matter_id: str, actor: Actor):
        self.calls.append(("snapshot", {"matter_id": matter_id, "actor": actor}))
        return PersistentEvidenceSnapshot(
            matter_id=matter_id,
            version=5,
            snapshot_hash="d" * 64,
            original_files=(),
            pages=(),
            duplicate_groups=(),
            locked_manifest=None,
            derivatives=(),
            derivative_runs=(),
        )

    def create_page_decision_candidate(self, **kwargs):
        self.calls.append(("create_page_decision", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="CREATE_EVIDENCE_PAGE_DECISION_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="EVIDENCE_PAGE_DECISION",
            object_id=str(uuid4()),
        )

    def get_verified_derivative_locator(self, *, matter_id: str, derivative_id: str, actor: Actor):
        self.calls.append(
            (
                "verified_locator",
                {"matter_id": matter_id, "derivative_id": derivative_id, "actor": actor},
            )
        )
        if self.locator is None or self.locator.derivative_id != derivative_id:
            raise KeyError(derivative_id)
        return self.locator

    def get_original_page_locator(self, *, matter_id: str, evidence_page_id: str, actor: Actor):
        self.calls.append(
            (
                "original_page_locator",
                {"matter_id": matter_id, "evidence_page_id": evidence_page_id, "actor": actor},
            )
        )
        if self.original_page_locator is None or self.original_page_locator.evidence_page_id != evidence_page_id:
            raise KeyError(evidence_page_id)
        return self.original_page_locator

    def enqueue_derivative_run(self, **kwargs):
        self.calls.append(("enqueue_derivative_run", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="ENQUEUE_EVIDENCE_DERIVATIVE_RUN",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="EVIDENCE_DERIVATIVE_RUN",
            object_id=str(uuid4()),
        )


class FakePersistentFormalCalculationStore:
    persistent_test_double = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.scenario_id = str(uuid4())
        self.run_id = str(uuid4())
        self.bundle_id = str(uuid4())
        self.segment_id = str(uuid4())

    def create_formal_calculation(self, **kwargs):
        self.calls.append(("create_formal_calculation", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="CREATE_FORMAL_CALCULATION",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="CALCULATION_RUN",
            object_id=self.run_id,
        )

    def get_current_calculation(self, *, matter_id: str, obligation_id: str, actor: Actor):
        self.calls.append(
            (
                "get_current_calculation",
                {"matter_id": matter_id, "obligation_id": obligation_id, "actor": actor},
            )
        )
        return PersistentFormalCalculationSnapshot(
            matter_id=matter_id,
            matter_version=6,
            scenario={
                "scenario_id": self.scenario_id,
                "obligation_id": obligation_id,
                "version": 1,
                "start_date": "2020-01-01",
                "end_date": "2021-01-01",
                "currency": "CNY",
                "allocation_policy": "INTEREST_THEN_PRINCIPAL",
                "legal_bundle_id": self.bundle_id,
                "legal_bundle_hash": "a" * 64,
                "transaction_snapshot_hash": "b" * 64,
                "input_hash": "c" * 64,
                "approved_by": actor.actor_id,
                "approval_hash": "d" * 64,
            },
            run={
                "run_id": self.run_id,
                "scenario_id": self.scenario_id,
                "scenario_version": 1,
                "engine_version": "lawcase-calc-1",
                "legal_bundle_id": self.bundle_id,
                "legal_bundle_hash": "a" * 64,
                "input_hash": "c" * 64,
                "output_hash": "e" * 64,
                "independent_check_hash": "f" * 64,
                "total_interest_accrued": "1200.00",
                "total_interest_paid": "1000.00",
                "remaining_principal": "10000.00",
                "remaining_unpaid_interest": "200.00",
                "unapplied_payments": "0.00",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "line_items": (
                    {
                        "line_sequence": 1,
                        "period_start": "2020-01-01",
                        "period_end": "2021-01-01",
                        "opening_principal": "10000.00",
                        "annual_rate": "0.12",
                        "day_count": 366,
                        "accrued_interest": "1200.00",
                        "closing_principal": "10000.00",
                        "accrued_unpaid_interest": "200.00",
                        "rule_segment_id": self.segment_id,
                        "source_rule_version": "SYNTHETIC-RULE-2020",
                        "evidence_ids": ("synthetic-evidence",),
                    },
                ),
                "payment_allocations": (),
            },
            snapshot_hash="9" * 64,
        )


class FakePersistentLegalSourceStore:
    persistent_test_double = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def approve_case_legal_bundle(self, **kwargs):
        self.calls.append(("approve_case_legal_bundle", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="APPROVE_CASE_LEGAL_BUNDLE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="CASE_LEGAL_BUNDLE",
            object_id=str(uuid4()),
        )

    def register_reviewed_capture_snapshot(self, **kwargs):
        self.calls.append(("register_reviewed_capture_snapshot", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="REGISTER_REVIEWED_OFFICIAL_SOURCE_CAPTURE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="OFFICIAL_LEGAL_SOURCE_SNAPSHOT",
            object_id=str(uuid4()),
        )

    def approve_legal_fact_binding(self, **kwargs):
        self.calls.append(("approve_legal_fact_binding", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="APPROVE_CASE_LEGAL_FACT_BINDING",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="CASE_LEGAL_FACT_BINDING",
            object_id=str(uuid4()),
        )


class FakePersistentSubmissionStore:
    persistent_test_double = True

    def __init__(self, locator: VerifiedSubmissionExportLocator | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.bundle_id = str(uuid4())
        self.locator = locator

    def _receipt(self, name: str, kwargs: dict, object_type: str):
        self.calls.append((name, kwargs))
        return CaseLedgerCommandReceipt(
            command_name=name.upper(),
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type=object_type,
            object_id=self.bundle_id if object_type == "SUBMISSION_BUNDLE" else str(uuid4()),
        )

    def register_work_product_candidate(self, **kwargs):
        return self._receipt("register_work_product_candidate", kwargs, "SUBMISSION_WORK_PRODUCT")

    def approve_work_product(self, **kwargs):
        return self._receipt("approve_work_product", kwargs, "SUBMISSION_WORK_PRODUCT")

    def create_qa_ready_bundle(self, **kwargs):
        return self._receipt("create_qa_ready_bundle", kwargs, "SUBMISSION_BUNDLE")

    def lock_submission_bundle(self, **kwargs):
        return self._receipt("lock_submission_bundle", kwargs, "SUBMISSION_BUNDLE")

    def get_submission_snapshot(self, *, matter_id: str, actor: Actor):
        self.calls.append(("get_submission_snapshot", {"matter_id": matter_id, "actor": actor}))
        return PersistentSubmissionSnapshot(
            matter_id=matter_id,
            matter_version=12,
            stage="READY_TO_EXPORT",
            work_products=(),
            bundles=(),
            current_bundle=None,
            current_components=(),
            current_export=None,
            snapshot_hash="a" * 64,
        )

    def get_verified_export_locator(self, **kwargs):
        self.calls.append(("get_verified_export_locator", kwargs))
        if self.locator is None:
            raise KeyError(kwargs["export_id"])
        return self.locator


class FakePersistentOfficialSourceCaptureStore:
    persistent_test_double = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.run_id = str(uuid4())

    def _receipt(self, name: str, kwargs: dict, object_type: str):
        self.calls.append((name, kwargs))
        return CaseLedgerCommandReceipt(
            command_name=name.upper(),
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type=object_type,
            object_id=self.run_id if object_type == "OFFICIAL_SOURCE_CAPTURE_RUN" else str(uuid4()),
        )

    def queue_capture(self, **kwargs):
        return self._receipt("queue_capture", kwargs, "OFFICIAL_SOURCE_CAPTURE_RUN")

    def review_capture(self, **kwargs):
        return self._receipt("review_capture", kwargs, "OFFICIAL_SOURCE_CAPTURE_REVIEW")

    def get_snapshot(self, *, matter_id: str, actor: Actor):
        self.calls.append(("get_snapshot", {"matter_id": matter_id, "actor": actor}))
        return PersistentOfficialSourceCaptureSnapshot(
            matter_id=matter_id,
            matter_version=8,
            runs=(
                {
                    "run_id": self.run_id,
                    "source_id": "CFETS-LPR-HISTORY",
                    "status": "REVIEW_REQUIRED",
                    "content_sha256": "a" * 64,
                    "parsed_output_hash": "b" * 64,
                },
            ),
            reviews=(),
            snapshot_hash="c" * 64,
        )

class PersistentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.identity = ServerIdentityContext(
            actor=Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER})),
            session_id=str(uuid4()),
            issuer="synthetic-test-identity",
            authentication_method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            authenticated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        self.settings = RuntimeSettings(
            mode=RuntimeMode.POSTGRES_INTERNAL_PREVIEW,
            _postgres_dsn="postgresql://localhost/lawcase_preview",
        )

    def test_default_app_exposes_only_disabled_health_and_no_case_routes(self) -> None:
        client = TestClient(create_persistent_app())
        self.assertEqual(client.get("/healthz").json()["mode"], "disabled")
        self.assertEqual(client.get(f"/v1/matters/{self.matter_id}/facts").status_code, 404)

    def test_server_identity_drives_fact_candidate_without_actor_headers(self) -> None:
        store = FakePersistentFactStore()
        app = create_persistent_app(
            PersistentApiDependencies(
                settings=self.settings,
                case_ledger_store=store,
                identity_resolver=StaticIdentityResolver(self.identity),
            )
        )
        client = TestClient(app)
        response = client.post(
            f"/v1/matters/{self.matter_id}/facts",
            headers={"Idempotency-Key": "persistent-fact-001", "X-Actor": "forged-actor-is-ignored"},
            json={
                "expected_version": 1,
                "original_text": "[合成] 持久化预览事实候选。",
                "origin": "ASSISTANT_ENTRY",
                "evidence_links": [
                    {
                        "evidence_id": "synthetic-evidence",
                        "original_file_sha256": "a" * 64,
                        "page_number": 1,
                        "region_id": "synthetic-region",
                        "original_label": "[合成] 原始材料第1页",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        UUID(response.headers["X-Request-ID"])
        self.assertEqual(store.calls[0][1]["actor"].actor_id, self.actor_id)

    def test_expired_server_identity_is_rejected_before_store_call(self) -> None:
        expired = ServerIdentityContext(
            actor=self.identity.actor,
            session_id=self.identity.session_id,
            issuer=self.identity.issuer,
            authentication_method=self.identity.authentication_method,
            authenticated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        store = FakePersistentFactStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=store,
                    identity_resolver=StaticIdentityResolver(expired),
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/facts")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(store.calls, [])

    def test_desktop_bootstrap_is_one_time_and_drives_persistent_identity(self) -> None:
        now = datetime.now(timezone.utc)
        authority = DesktopSessionAuthority(
            actor=self.identity.actor,
            bootstrap_token="b" * 64,
            bootstrap_expires_at=now + timedelta(seconds=30),
            session_expires_at=now + timedelta(minutes=30),
            token_factory=lambda: "s" * 64,
        )
        store = FakePersistentFactStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=store,
                    identity_resolver=authority,
                    desktop_session_authority=authority,
                )
            ),
            client=("127.0.0.1", 50001),
        )
        exchange = client.post(
            "/v1/desktop-sessions/exchange",
            headers={"Origin": "tauri://localhost", "X-Desktop-Bootstrap": "b" * 64},
        )
        self.assertEqual(exchange.status_code, 200, exchange.text)
        self.assertEqual(exchange.headers["Cache-Control"], "no-store")
        self.assertEqual(exchange.json()["token_type"], "Bearer")
        self.assertEqual(exchange.json()["access_token"], "s" * 64)
        facts = client.get(
            f"/v1/matters/{self.matter_id}/facts",
            headers={
                "Origin": "tauri://localhost",
                "Authorization": f"Bearer {exchange.json()['access_token']}",
            },
        )
        self.assertEqual(facts.status_code, 200, facts.text)
        self.assertEqual(store.calls[0][1]["actor"].actor_id, self.actor_id)
        reused = client.post(
            "/v1/desktop-sessions/exchange",
            headers={"Origin": "tauri://localhost", "X-Desktop-Bootstrap": "b" * 64},
        )
        self.assertEqual(reused.status_code, 401)
        self.assertEqual(reused.json()["code"], "AUTHENTICATION_REQUIRED")

    def test_desktop_bootstrap_cannot_use_a_different_identity_resolver(self) -> None:
        now = datetime.now(timezone.utc)
        authority = DesktopSessionAuthority(
            actor=self.identity.actor,
            bootstrap_token="b" * 64,
            bootstrap_expires_at=now + timedelta(seconds=30),
            session_expires_at=now + timedelta(minutes=30),
        )
        with self.assertRaisesRegex(ValueError, "same authority"):
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    desktop_session_authority=authority,
                )
            )

    def test_transaction_and_payment_classification_routes_keep_currency_and_application_explicit(self) -> None:
        store = FakePersistentFactStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=store,
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        transaction = client.post(
            f"/v1/matters/{self.matter_id}/transactions",
            headers={"Idempotency-Key": "persistent-transaction-001"},
            json={
                "expected_version": 1,
                "local_date": "2020-08-20",
                "date_precision": "EXACT_DATE",
                "amount": "1000.00",
                "currency": "CNY",
                "direction": "OUTGOING",
                "payer_label": "[合成] 被告",
                "payee_label": "[合成] 原告",
                "channel": "WECHAT",
                "transaction_reference": "synthetic-reference",
                "evidence_links": [
                    {
                        "evidence_id": "synthetic-transaction-evidence",
                        "original_file_sha256": "b" * 64,
                        "page_number": 2,
                        "region_id": "synthetic-transaction-region",
                        "original_label": "[合成] 微信流水第2页",
                    }
                ],
            },
        )
        self.assertEqual(transaction.status_code, 201, transaction.text)
        transaction_id = transaction.json()["object_id"]
        classification = client.post(
            f"/v1/matters/{self.matter_id}/transactions/{transaction_id}/payment-classifications",
            headers={"Idempotency-Key": "persistent-classification-001"},
            json={
                "expected_version": 2,
                "origin": "DEFENDANT_STATEMENT",
                "nature": "INTEREST_PAYMENT",
                "allocations": [
                    {"obligation_id": "synthetic-obligation", "amount": "1000.00", "currency": "CNY"}
                ],
                "same_day_sequence": 1,
                "evidence_links": [
                    {
                        "evidence_id": "synthetic-transaction-evidence",
                        "original_file_sha256": "b" * 64,
                        "page_number": 2,
                        "region_id": "synthetic-transaction-region",
                        "original_label": "[合成] 微信流水第2页",
                    }
                ],
            },
        )
        self.assertEqual(classification.status_code, 201, classification.text)
        transaction_call = next(call for call in store.calls if call[0] == "create_transaction")[1]
        classification_call = next(call for call in store.calls if call[0] == "create_classification")[1]
        self.assertEqual(transaction_call["currency"], "CNY")
        self.assertEqual(classification_call["nature"].value, "INTEREST_PAYMENT")
        self.assertEqual(classification_call["allocations"][0].currency, "CNY")

    def test_case_snapshot_is_a_single_versioned_read_model(self) -> None:
        store = FakePersistentFactStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=store,
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/snapshot")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["snapshot_hash"], "c" * 64)
        self.assertEqual(payload["payment_classifications"], [])

    def test_evidence_routes_fail_closed_when_evidence_store_is_not_configured(self) -> None:
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/evidence-snapshot")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "EVIDENCE_SERVICE_UNAVAILABLE")

    def test_evidence_snapshot_and_page_decision_use_server_identity_and_uuid_scope(self) -> None:
        evidence_store = FakePersistentEvidenceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    evidence_manifest_store=evidence_store,
                )
            )
        )
        snapshot = client.get(f"/v1/matters/{self.matter_id}/evidence-snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(snapshot.json()["snapshot_hash"], "d" * 64)

        page_id = str(uuid4())
        decision = client.post(
            f"/v1/matters/{self.matter_id}/evidence-pages/{page_id}/decisions",
            headers={"Idempotency-Key": "evidence-page-decision-001", "X-Actor": "forged-and-ignored"},
            json={
                "expected_version": 5,
                "disposition": "INCLUDE",
                "reason": "[合成] 与目标微信昵称相关，纳入派生件。",
            },
        )
        self.assertEqual(decision.status_code, 201, decision.text)
        call = next(item for item in evidence_store.calls if item[0] == "create_page_decision")[1]
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["evidence_page_id"], page_id)
        self.assertEqual(call["disposition"].value, "INCLUDE")

    def test_evidence_snapshot_exposes_pending_page_decision_for_interrupted_approval_recovery(self) -> None:
        evidence_store = FakePersistentEvidenceStore()
        page_id = str(uuid4())
        file_id = str(uuid4())
        decision_id = str(uuid4())
        evidence_store.get_evidence_snapshot = lambda *, matter_id, actor: PersistentEvidenceSnapshot(
            matter_id=matter_id,
            version=8,
            snapshot_hash="e" * 64,
            original_files=(),
            pages=(
                {
                    "evidence_page_id": page_id,
                    "evidence_file_id": file_id,
                    "page_number": 3,
                    "rendered_page_sha256": None,
                    "decision": None,
                    "pending_decision": {
                        "decision_id": decision_id,
                        "disposition": "EXCLUDE",
                        "reason": "[合成] 与本案目标主体无关。",
                        "status": "CANDIDATE",
                        "approval_hash": None,
                        "approved_by": None,
                    },
                    "annotations": (),
                },
            ),
            duplicate_groups=(),
            locked_manifest=None,
            derivatives=(),
            derivative_runs=(),
        )
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    evidence_manifest_store=evidence_store,
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/evidence-snapshot")
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()["pages"][0]["pending_decision"]
        self.assertEqual(pending["decision_id"], decision_id)
        self.assertEqual(pending["disposition"], "EXCLUDE")
        self.assertEqual(pending["status"], "CANDIDATE")

    def test_original_page_preview_uses_confirmed_folder_one_time_token_and_png_only(self) -> None:
        with TemporaryDirectory(prefix="persistent-original-page-api-test-") as temporary:
            root = Path(temporary) / "selected-case"
            root.mkdir()
            source = root / "synthetic-source.pdf"
            document = canvas.Canvas(str(source), pagesize=(300, 400))
            document.drawString(30, 350, "SYNTHETIC PAGE ONE")
            document.showPage()
            document.drawString(30, 350, "SYNTHETIC PAGE TWO")
            document.showPage()
            document.save()
            page_id = str(uuid4())
            locator = OriginalPageLocator(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                evidence_page_id=page_id,
                evidence_file_id=str(uuid4()),
                original_label=source.name,
                original_file_sha256=sha256(source.read_bytes()).hexdigest(),
                byte_size=source.stat().st_size,
                media_type="application/pdf",
                page_count=2,
                page_number=2,
            )
            evidence_store = FakePersistentEvidenceStore(original_page_locator=locator)
            folder_grants = LocalFolderGrantRegistry()
            preview_broker = OriginalPageAccessBroker(folder_grants=folder_grants)
            client = TestClient(
                create_persistent_app(
                    PersistentApiDependencies(
                        settings=self.settings,
                        case_ledger_store=FakePersistentFactStore(),
                        identity_resolver=StaticIdentityResolver(self.identity),
                        evidence_manifest_store=evidence_store,
                        local_folder_grants=folder_grants,
                        original_page_access_broker=preview_broker,
                    )
                ),
                client=("127.0.0.1", 51001),
            )
            inspected = client.post(
                f"/v1/matters/{self.matter_id}/local-folder-selections/inspect",
                json={"selected_root": str(root)},
            )
            self.assertEqual(inspected.status_code, 200, inspected.text)
            self.assertEqual(inspected.json()["display_name"], "selected-case")
            self.assertNotIn(str(root), inspected.text)
            granted = client.post(
                f"/v1/matters/{self.matter_id}/local-folder-grants",
                json={
                    "selected_root": str(root),
                    "confirmed_root_fingerprint": inspected.json()["root_fingerprint"],
                },
            )
            self.assertEqual(granted.status_code, 200, granted.text)
            self.assertNotIn(str(root), granted.text)
            issued = client.post(
                f"/v1/matters/{self.matter_id}/evidence-pages/{page_id}/original-preview/access",
                json={"folder_grant_id": granted.json()["grant_id"]},
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            token = issued.json()["access_token"]
            delivered = client.get(
                f"/v1/matters/{self.matter_id}/evidence-pages/{page_id}/original-preview/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(delivered.status_code, 200, delivered.text)
            self.assertEqual(delivered.headers["content-type"], "image/png")
            self.assertTrue(delivered.content.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(delivered.headers["cache-control"], "no-store, private")
            self.assertEqual(sha256(delivered.content).hexdigest(), delivered.headers["x-artifact-sha256"])
            self.assertGreater(int(delivered.headers["x-image-width"]), 0)
            replay = client.get(
                f"/v1/matters/{self.matter_id}/evidence-pages/{page_id}/original-preview/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(replay.status_code, 403)
            self.assertEqual(replay.json()["code"], "ORIGINAL_PAGE_ACCESS_DENIED")

    def test_verified_derivative_uses_short_lived_bearer_and_one_time_loopback_delivery(self) -> None:
        with TemporaryDirectory(prefix="persistent-artifact-api-test-") as temporary:
            root = Path(temporary)
            case_root = root / "case"
            case_root.mkdir()
            source = root / "verified.pdf"
            content = b"%PDF-1.4\nSYNTHETIC API ARTIFACT\n%%EOF\n"
            source.write_bytes(content)
            artifact_hash = sha256(content).hexdigest()
            artifact_store = LocalEncryptedArtifactStore(
                root / "managed",
                key_id="synthetic-api-key-v1",
                encryption_key=b"k" * 32,
            )
            stored = artifact_store.put_file(
                source,
                expected_sha256=artifact_hash,
                case_root=case_root,
            )
            derivative_id = str(uuid4())
            locator = VerifiedDerivativeLocator(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                derivative_id=derivative_id,
                manifest_id=str(uuid4()),
                artifact_type="ANNOTATED_RELATED_PAGES_PDF",
                object_key=stored.object_key,
                artifact_sha256=artifact_hash,
                page_count=1,
                status="VERIFIED",
            )
            evidence_store = FakePersistentEvidenceStore(locator=locator)
            client = TestClient(
                create_persistent_app(
                    PersistentApiDependencies(
                        settings=self.settings,
                        case_ledger_store=FakePersistentFactStore(),
                        identity_resolver=StaticIdentityResolver(self.identity),
                        evidence_manifest_store=evidence_store,
                        artifact_access_broker=EphemeralArtifactAccessBroker(),
                        artifact_store=artifact_store,
                    )
                ),
                client=("127.0.0.1", 51000),
            )
            issued = client.post(
                f"/v1/matters/{self.matter_id}/evidence-derivatives/{derivative_id}/access",
                json={"purpose": "INLINE_PREVIEW"},
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            token = issued.json()["access_token"]
            delivered = client.get(
                f"/v1/matters/{self.matter_id}/evidence-derivatives/{derivative_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(delivered.status_code, 200, delivered.text)
            self.assertEqual(delivered.content, content)
            self.assertEqual(delivered.headers["cache-control"], "no-store, private")
            self.assertEqual(delivered.headers["x-artifact-sha256"], artifact_hash)
            self.assertIn("inline", delivered.headers["content-disposition"])
            replay = client.get(
                f"/v1/matters/{self.matter_id}/evidence-derivatives/{derivative_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(replay.status_code, 403)
            self.assertEqual(replay.json()["code"], "PERMISSION_DENIED")

    def test_lead_enqueues_a_version_and_manifest_hash_bound_derivative_run(self) -> None:
        evidence_store = FakePersistentEvidenceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    evidence_manifest_store=evidence_store,
                )
            )
        )
        manifest_id = str(uuid4())
        response = client.post(
            f"/v1/matters/{self.matter_id}/evidence-manifests/{manifest_id}/derivative-runs",
            headers={"Idempotency-Key": "evidence-run-ui-001"},
            json={
                "expected_version": 5,
                "manifest_content_hash": "8" * 64,
                "approval_hash": "7" * 64,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        call = next(item for item in evidence_store.calls if item[0] == "enqueue_derivative_run")[1]
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["manifest_id"], manifest_id)
        self.assertEqual(call["expected_version"], 5)
        self.assertEqual(call["manifest_content_hash"], "8" * 64)

    def test_formal_calculation_routes_fail_closed_without_formal_store(self) -> None:
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.get(
            f"/v1/matters/{self.matter_id}/calculations/synthetic-obligation/current"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "FORMAL_CALCULATION_SERVICE_UNAVAILABLE")

    def test_formal_calculation_read_and_command_are_identity_version_and_rule_bound(self) -> None:
        calculation_store = FakePersistentFormalCalculationStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    formal_calculation_store=calculation_store,
                )
            )
        )
        current = client.get(
            f"/v1/matters/{self.matter_id}/calculations/synthetic-obligation/current"
        )
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["run"]["engine_version"], "lawcase-calc-1")
        self.assertEqual(current.json()["scenario"]["currency"], "CNY")

        created = client.post(
            f"/v1/matters/{self.matter_id}/formal-calculations",
            headers={"Idempotency-Key": "formal-calculation-api-001"},
            json={
                "expected_version": 6,
                "obligation_id": "synthetic-obligation",
                "start_date": "2020-01-01",
                "end_date": "2021-01-01",
                "legal_bundle_id": calculation_store.bundle_id,
                "legal_bundle_hash": "a" * 64,
                "allocation_policy": "INTEREST_THEN_PRINCIPAL",
                "approval_hash": "c" * 64,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        call = next(item for item in calculation_store.calls if item[0] == "create_formal_calculation")[1]
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["expected_version"], 6)
        self.assertEqual(call["allocation_policy"].value, "INTEREST_THEN_PRINCIPAL")

    def test_formal_calculation_rejects_empty_interval_before_store_call(self) -> None:
        calculation_store = FakePersistentFormalCalculationStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    formal_calculation_store=calculation_store,
                )
            )
        )
        response = client.post(
            f"/v1/matters/{self.matter_id}/formal-calculations",
            headers={"Idempotency-Key": "formal-calculation-api-gap"},
            json={
                "expected_version": 6,
                "obligation_id": "synthetic-obligation",
                "start_date": "2021-01-01",
                "end_date": "2020-01-01",
                "legal_bundle_id": calculation_store.bundle_id,
                "legal_bundle_hash": "a" * 64,
                "allocation_policy": "INTEREST_THEN_PRINCIPAL",
                "approval_hash": "d" * 64,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(any(name == "create_formal_calculation" for name, _ in calculation_store.calls))

    def test_legal_bundle_route_fails_closed_without_official_source_store(self) -> None:
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.post(
            f"/v1/matters/{self.matter_id}/legal-bundles",
            headers={"Idempotency-Key": "legal-bundle-disabled"},
            json={
                "expected_version": 1,
                "segments": [
                    {
                        "segment_id": str(uuid4()),
                        "issue_key": "interest_cap",
                        "rule_version_id": str(uuid4()),
                        "trigger_event_id": str(uuid4()),
                        "start_date": "2020-08-20",
                        "end_date": "2021-01-01",
                        "applicability_anchor": "起诉时司法保护标准",
                    }
                ],
                "approval_hash": "a" * 64,
            },
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "LEGAL_SOURCE_SERVICE_UNAVAILABLE")

    def test_legal_bundle_api_accepts_only_rule_and_event_ids_not_client_rate(self) -> None:
        legal_store = FakePersistentLegalSourceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    legal_source_store=legal_store,
                )
            )
        )
        response = client.post(
            f"/v1/matters/{self.matter_id}/legal-bundles",
            headers={"Idempotency-Key": "legal-bundle-api-001"},
            json={
                "expected_version": 7,
                "segments": [
                    {
                        "segment_id": str(uuid4()),
                        "issue_key": "interest_cap",
                        "rule_version_id": str(uuid4()),
                        "trigger_event_id": str(uuid4()),
                        "start_date": "2020-08-20",
                        "end_date": "2021-01-01",
                        "applicability_anchor": "起诉时司法保护标准",
                    }
                ],
                "approval_hash": "a" * 64,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        call = legal_store.calls[0][1]
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["expected_version"], 7)
        self.assertFalse(hasattr(call["segments"][0], "annual_rate"))

    def test_legal_fact_binding_api_uses_server_identity_and_confirmed_fact_id(self) -> None:
        legal_store = FakePersistentLegalSourceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    legal_source_store=legal_store,
                )
            )
        )
        fact_id = str(uuid4())
        response = client.post(
            f"/v1/matters/{self.matter_id}/legal-fact-bindings",
            headers={"Idempotency-Key": "legal-fact-binding-api-001"},
            json={
                "expected_version": 7,
                "fact_key": "contract_before_2020_08_20",
                "fact_id": fact_id,
                "approval_hash": "a" * 64,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        name, call = legal_store.calls[0]
        self.assertEqual(name, "approve_legal_fact_binding")
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["fact_id"], fact_id)

    def test_lpr_rule_api_requires_a_distinct_official_rate_snapshot_locator(self) -> None:
        legal_store = FakePersistentLegalSourceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    legal_source_store=legal_store,
                )
            )
        )
        response = client.post(
            f"/v1/matters/{self.matter_id}/legal-rule-versions",
            headers={"Idempotency-Key": "lpr-rule-missing-rate-source"},
            json={
                "expected_version": 7,
                "rule_id": "private-lending-cap",
                "rule_version": "PRIVATE-LENDING-LPR-2020-08",
                "issue_key": "interest_cap_after_2020_08_20",
                "source_snapshot_id": str(uuid4()),
                "effective_from": "2020-08-20",
                "trigger_event_kind": "CLAIM_FILED",
                "formula_kind": "LPR_MULTIPLE",
                "base_annual_rate": "0.0385",
                "rate_multiplier": "4",
                "required_fact_keys": ["contract_before_2020_08_20"],
                "priority": 100,
                "approval_hash": "a" * 64,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(legal_store.calls, [])

    def test_official_source_capture_routes_fail_closed_when_capture_store_is_not_configured(self) -> None:
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/official-source-captures")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "LEGAL_SOURCE_SERVICE_UNAVAILABLE")

    def test_official_source_capture_snapshot_queue_and_review_routes_map_server_identity(self) -> None:
        capture_store = FakePersistentOfficialSourceCaptureStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    official_source_capture_store=capture_store,
                )
            )
        )
        snapshot = client.get(f"/v1/matters/{self.matter_id}/official-source-captures")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(snapshot.json()["runs"][0]["status"], "REVIEW_REQUIRED")

        queued = client.post(
            f"/v1/matters/{self.matter_id}/official-source-captures",
            headers={"Idempotency-Key": "source-capture-api-001"},
            json={
                "expected_version": 8,
                "source_id": "CFETS-LPR-HISTORY",
                "target_url": "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN",
                "query_sha256": "a" * 64,
                "authorization_hash": "b" * 64,
            },
        )
        self.assertEqual(queued.status_code, 201, queued.text)
        queue_call = next(call for name, call in capture_store.calls if name == "queue_capture")
        self.assertEqual(queue_call["actor"], self.identity.actor)
        self.assertEqual(queue_call["expected_version"], 8)

        reviewed = client.post(
            f"/v1/matters/{self.matter_id}/official-source-captures/{capture_store.run_id}/review",
            headers={"Idempotency-Key": "source-review-api-001"},
            json={
                "expected_version": 9,
                "decision": "APPROVE_FOR_REGISTRATION",
                "provision_locator": "一年期LPR records[0]",
                "review_hash": "c" * 64,
            },
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        review_call = next(call for name, call in capture_store.calls if name == "review_capture")
        self.assertEqual(review_call["run_id"], capture_store.run_id)
        self.assertEqual(review_call["actor"], self.identity.actor)

    def test_reviewed_capture_registration_requires_legal_store_and_maps_license_review(self) -> None:
        run_id = str(uuid4())
        without_legal_store = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        missing = without_legal_store.post(
            f"/v1/matters/{self.matter_id}/official-source-captures/{run_id}/register",
            headers={"Idempotency-Key": "reviewed-capture-register-missing"},
            json={
                "expected_version": 10,
                "license_basis": "官方公开页面，仅限本案内部法律研究与引用核验。",
                "license_review_hash": "a" * 64,
                "registration_hash": "b" * 64,
            },
        )
        self.assertEqual(missing.status_code, 503)

        legal_store = FakePersistentLegalSourceStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    legal_source_store=legal_store,
                )
            )
        )
        response = client.post(
            f"/v1/matters/{self.matter_id}/official-source-captures/{run_id}/register",
            headers={"Idempotency-Key": "reviewed-capture-register-001"},
            json={
                "expected_version": 10,
                "license_basis": "官方公开页面，仅限本案内部法律研究与引用核验。",
                "license_review_hash": "a" * 64,
                "registration_hash": "b" * 64,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        name, call = legal_store.calls[0]
        self.assertEqual(name, "register_reviewed_capture_snapshot")
        self.assertEqual(call["run_id"], run_id)
        self.assertEqual(call["actor"], self.identity.actor)
        self.assertEqual(call["license_review_hash"], "a" * 64)

    def test_submission_routes_fail_closed_when_submission_store_is_not_configured(self) -> None:
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                )
            )
        )
        response = client.get(f"/v1/matters/{self.matter_id}/submission-snapshot")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "SUBMISSION_SERVICE_UNAVAILABLE")

    def test_submission_snapshot_qa_and_lock_use_server_identity_and_hash_bound_inputs(self) -> None:
        submission_store = FakePersistentSubmissionStore()
        client = TestClient(
            create_persistent_app(
                PersistentApiDependencies(
                    settings=self.settings,
                    case_ledger_store=FakePersistentFactStore(),
                    identity_resolver=StaticIdentityResolver(self.identity),
                    submission_store=submission_store,
                )
            )
        )
        snapshot = client.get(f"/v1/matters/{self.matter_id}/submission-snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(snapshot.json()["snapshot_hash"], "a" * 64)

        work_product_ids = [str(uuid4()) for _ in range(2)]
        manifest_id = str(uuid4())
        legal_bundle_id = str(uuid4())
        calculation_run_id = str(uuid4())
        final_approval_id = str(uuid4())
        qa = client.post(
            f"/v1/matters/{self.matter_id}/submission-bundles/qa-ready",
            headers={"Idempotency-Key": "submission-qa-api-001"},
            json={
                "expected_version": 12,
                "selections": [
                    {
                        "work_product_id": work_product_ids[0],
                        "sequence": 1,
                        "court_filename": "01_民事答辩状.pdf",
                    },
                    {
                        "work_product_id": work_product_ids[1],
                        "sequence": 2,
                        "court_filename": "02_证据材料.pdf",
                    },
                ],
                "required_document_kinds": ["DEFENCE_STATEMENT", "EVIDENCE_MATERIAL"],
                "evidence_manifest_id": manifest_id,
                "legal_bundle_id": legal_bundle_id,
                "calculation_run_id": calculation_run_id,
                "final_text_approval_id": final_approval_id,
                "expected_qa_hash": "b" * 64,
            },
        )
        self.assertEqual(qa.status_code, 201, qa.text)
        qa_call = next(call for name, call in submission_store.calls if name == "create_qa_ready_bundle")
        self.assertEqual(qa_call["actor"], self.identity.actor)
        self.assertEqual(qa_call["evidence_manifest_id"], manifest_id)
        self.assertEqual(qa_call["selections"][0].court_filename, "01_民事答辩状.pdf")

        locked = client.post(
            f"/v1/matters/{self.matter_id}/submission-bundles/{submission_store.bundle_id}/lock",
            headers={"Idempotency-Key": "submission-lock-api-001"},
            json={
                "expected_version": 13,
                "expected_input_hash": "b" * 64,
                "lock_approval_hash": "c" * 64,
            },
        )
        self.assertEqual(locked.status_code, 200, locked.text)
        lock_call = next(call for name, call in submission_store.calls if name == "lock_submission_bundle")
        self.assertEqual(lock_call["actor"], self.identity.actor)
        self.assertEqual(lock_call["expected_input_hash"], "b" * 64)
        self.assertNotIn("court_zip_object_key", lock_call)

    def test_verified_submission_zip_uses_one_time_loopback_download(self) -> None:
        with TemporaryDirectory(prefix="persistent-submission-api-test-") as temporary:
            root = Path(temporary)
            case_root = root / "case"
            case_root.mkdir()
            source = root / "court.zip"
            with ZipFile(source, "w", ZIP_STORED) as archive:
                archive.writestr("01_民事答辩状.pdf", b"%PDF-1.4\n%%EOF\n")
            content = source.read_bytes()
            artifact_hash = sha256(content).hexdigest()
            artifact_store = LocalEncryptedArtifactStore(
                root / "managed",
                key_id="synthetic-submission-api-key-v1",
                encryption_key=b"k" * 32,
            )
            stored = artifact_store.put_file(
                source, expected_sha256=artifact_hash, case_root=case_root
            )
            export_id = str(uuid4())
            locator = VerifiedSubmissionExportLocator(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                export_id=export_id,
                bundle_id=str(uuid4()),
                object_key=stored.object_key,
                court_zip_sha256=artifact_hash,
                court_zip_bytes=len(content),
                lifecycle="EXPORTED",
                validity="VALID",
            )
            submission_store = FakePersistentSubmissionStore(locator=locator)
            client = TestClient(
                create_persistent_app(
                    PersistentApiDependencies(
                        settings=self.settings,
                        case_ledger_store=FakePersistentFactStore(),
                        identity_resolver=StaticIdentityResolver(self.identity),
                        submission_store=submission_store,
                        submission_access_broker=SubmissionExportAccessBroker(),
                        artifact_store=artifact_store,
                    )
                ),
                client=("127.0.0.1", 51001),
            )
            issued = client.post(
                f"/v1/matters/{self.matter_id}/submission-exports/{export_id}/access"
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            token = issued.json()["access_token"]
            delivered = client.get(
                f"/v1/matters/{self.matter_id}/submission-exports/{export_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(delivered.status_code, 200, delivered.text)
            self.assertEqual(delivered.content, content)
            self.assertEqual(delivered.headers["content-type"], "application/zip")
            self.assertEqual(delivered.headers["x-artifact-sha256"], artifact_hash)
            self.assertIn("court-submission.zip", delivered.headers["content-disposition"])
            replay = client.get(
                f"/v1/matters/{self.matter_id}/submission-exports/{export_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(replay.status_code, 403)
            self.assertEqual(replay.json()["code"], "SUBMISSION_ACCESS_DENIED")


if __name__ == "__main__":
    unittest.main()
