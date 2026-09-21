from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from uuid import uuid4

from case_kernel.models import Actor, Role
from case_kernel.web_agent_material_postgres import PostgresAgentMaterialRunStore
from case_kernel.web_agent_material_review import (
    AgentMaterialReviewBlocked,
    AgentMaterialRunSnapshot,
    AgentPlanContext,
    AgentRunStatus,
)


class _Result:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, *, matter_id, firm_id, actor_id, run_id, external_request_id):
        self.executed = []
        self.matter_id = matter_id
        self.firm_id = firm_id
        self.actor_id = actor_id
        self.run_id = run_id
        self.external_request_id = external_request_id

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT run_id FROM web_agent_material_runs"):
            return _Result({"run_id": self.run_id})
        if "FROM web_agent_material_runs" in normalized and "SELECT run_id, firm_id" in normalized:
            return _Result({
                "run_id": self.run_id,
                "firm_id": self.firm_id,
                "matter_id": self.matter_id,
                "requested_by": self.actor_id,
                "matter_version": 8,
                "input_hash": "a" * 64,
                "plan_request_hash": "b" * 64,
                "agent_intent": "MATERIAL_NEUTRAL_REVIEW",
                "representation_profile_version": 1,
                "representation_profile_hash": "c" * 64,
                "external_request_id": self.external_request_id,
                "run_version": 2,
                "status": "QUEUED",
                "attempt_count": 0,
                "lease_id": None,
                "lease_expires_at": None,
                "output_hash": None,
                "failure_code": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            })
        if "FROM web_agent_material_tasks" in normalized and normalized.startswith("SELECT"):
            return _Result(rows=[])
        if "FROM web_agent_material_page_bindings" in normalized and normalized.startswith("SELECT"):
            return _Result(rows=[])
        if "FROM web_agent_material_candidates" in normalized and normalized.startswith("SELECT"):
            return _Result(rows=[])
        if normalized.startswith("SELECT m.version,"):
            return _Result({"version": 8, "permitted": True})
        if "FROM external_request_authorizations" in normalized:
            return _Result({
                "request_kind": "MODEL",
                "provider_id": "deepseek",
                "processor_region": "cn-beijing",
                "selected_field_ids": [f"agent-material-input:{'a' * 64}"],
                "service_id": "deepseek-v4-pro",
                "call_cap": 1,
                "input_hash": "a" * 64,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            })
        if "FROM external_request_attempts" in normalized:
            return _Result()
        return _Result(rowcount=1)


class PostgresAgentMaterialRunStoreTests(TestCase):
    def setUp(self):
        self.matter_id = str(uuid4())
        self.firm_id = str(uuid4())
        self.worker_id = str(uuid4())
        self.run_id = str(uuid4())
        self.external_request_id = str(uuid4())
        self.worker = Actor(self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER}))

    def test_claim_is_lease_bound_and_refuses_prior_external_attempt(self):
        store = PostgresAgentMaterialRunStore("postgresql://not-used.invalid/test")
        connection = _Connection(
            matter_id=self.matter_id,
            firm_id=self.firm_id,
            actor_id=self.worker_id,
            run_id=self.run_id,
            external_request_id=self.external_request_id,
        )

        @contextmanager
        def transaction(_):
            yield connection

        store._transaction = transaction
        claimed = store.claim_run(
            actor=self.worker,
            run_id=self.run_id,
            expected_run_version=2,
            idempotency_key="claim-agent-material-0001",
        )
        self.assertEqual(claimed.status, AgentRunStatus.QUEUED)  # fake reload is static
        statements = "\n".join(sql for sql, _ in connection.executed)
        self.assertIn("status = 'CLAIMED'", statements)
        self.assertIn("lease_expires_at = now()", statements)
        self.assertIn("FROM external_request_attempts", statements)

    def test_authorization_requires_exact_aggregate_scope(self):
        run = AgentMaterialRunSnapshot(
            run_id=self.run_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            requested_by=self.worker_id,
            matter_version=8,
            input_hash="a" * 64,
            request_hash="b" * 64,
            page_bindings=(),
            run_version=2,
            status=AgentRunStatus.QUEUED,
            tasks=(),
            candidates=(),
            output_hash=None,
            failure_code=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            external_request_id=self.external_request_id,
            plan_context=AgentPlanContext.neutral(
                representation_profile_version=1,
                representation_profile_hash="c" * 64,
            ),
        )
        valid = {
            "request_kind": "MODEL",
            "provider_id": "deepseek",
            "service_id": "deepseek-v4-pro",
            "call_cap": 1,
            "selected_field_ids": [f"agent-material-input:{run.input_hash}"],
            "input_hash": run.input_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        PostgresAgentMaterialRunStore._validate_external_authorization(valid, run=run)
        invalid = {**valid, "selected_field_ids": ["case-plan:minimal-projection"]}
        with self.assertRaisesRegex(AgentMaterialReviewBlocked, "exact Agent material input"):
            PostgresAgentMaterialRunStore._validate_external_authorization(invalid, run=run)


if __name__ == "__main__":
    import unittest
    unittest.main()
