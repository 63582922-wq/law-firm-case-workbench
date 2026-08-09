from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from fastapi.testclient import TestClient

from case_api.persistent_app import PersistentApiDependencies, create_persistent_app
from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.fact_claim_ledger import AssertionOrigin, FactAssertion, FactStatus
from case_kernel.models import Actor, Role
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
        self.assertEqual(store.calls, [])


if __name__ == "__main__":
    unittest.main()
