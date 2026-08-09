from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4
import unittest

from fastapi.testclient import TestClient

from case_api.persistent_app import PersistentApiDependencies, create_persistent_app
from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.artifact_access import EphemeralArtifactAccessBroker, VerifiedDerivativeLocator
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt, PersistentCaseSnapshot
from case_kernel.evidence_manifest_postgres import PersistentEvidenceSnapshot
from case_kernel.fact_claim_ledger import AssertionOrigin, FactAssertion, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.runtime import RuntimeMode, RuntimeSettings


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

    def __init__(self, *, locator: VerifiedDerivativeLocator | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.locator = locator

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


if __name__ == "__main__":
    unittest.main()
