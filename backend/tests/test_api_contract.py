import hashlib
import unittest


try:
    from starlette.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - dependency is installed by the backend dev environment
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependency is not installed")
class SyntheticAlphaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from case_api.app import create_app

        self.client = TestClient(create_app())
        self.lead_headers = {"X-Alpha-Actor": "alpha_lead_lawyer"}

    def _headers(self, key: str) -> dict[str, str]:
        return {**self.lead_headers, "Idempotency-Key": key}

    def _create(self, matter_id: str = "alpha_matter_001") -> None:
        response = self.client.post(
            "/v1/matters",
            headers=self._headers("create-1"),
            json={"matter_id": matter_id, "title": "[合成] 民间借贷被告材料核验"},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_health_declares_alpha_only_and_create_requires_synthetic_guard(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.json()["mode"], "synthetic-alpha-only")

        rejected = self.client.post(
            "/v1/matters",
            headers=self._headers("bad-create"),
            json={"matter_id": "case_123", "title": "真实案件"},
        )
        self.assertEqual(rejected.status_code, 422)

    def test_create_is_idempotent_and_read_is_firm_scoped(self) -> None:
        self._create()
        duplicate = self.client.post(
            "/v1/matters",
            headers=self._headers("create-1"),
            json={"matter_id": "alpha_matter_001", "title": "[合成] 民间借贷被告材料核验"},
        )
        self.assertEqual(duplicate.status_code, 201)
        self.assertEqual(duplicate.json()["matter_version"], 1)

        found = self.client.get("/v1/matters/alpha_matter_001", headers=self.lead_headers)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["stage"], "CREATED")

    def test_assistant_is_denied_and_version_conflict_is_exposed(self) -> None:
        self._create()
        denied = self.client.post(
            "/v1/matters/alpha_matter_001/advance",
            headers={"X-Alpha-Actor": "alpha_assistant", "Idempotency-Key": "assistant-advance"},
            json={"expected_version": 1},
        )
        self.assertEqual(denied.status_code, 403)

        stale = self.client.post(
            "/v1/matters/alpha_matter_001/advance",
            headers=self._headers("stale-advance"),
            json={"expected_version": 2},
        )
        self.assertEqual(stale.status_code, 409)

    def test_lock_keeps_final_hash_bound_to_current_approval(self) -> None:
        self._create()
        version = 1
        # CREATED -> ... -> READY_TO_EXPORT requires nine guarded transitions.
        for index in range(9):
            response = self.client.post(
                "/v1/matters/alpha_matter_001/advance",
                headers=self._headers(f"advance-{index}"),
                json={"expected_version": version},
            )
            self.assertEqual(response.status_code, 200, response.text)
            version += 1

        final_text = "[SYNTHETIC] 仅用于锁定审批哈希的合成最终文本。"
        final_hash = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        approval = self.client.post(
            "/v1/matters/alpha_matter_001/approvals",
            headers=self._headers("approval-final"),
            json={"expected_version": version, "approval_type": "FINAL_TEXT", "approved_object_hash": final_hash},
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        version += 1

        rejected = self.client.post(
            "/v1/matters/alpha_matter_001/lock-submission",
            headers=self._headers("lock-wrong"),
            json={"expected_version": version, "final_text": "[SYNTHETIC] 不同的文本，哈希不应通过。"},
        )
        self.assertEqual(rejected.status_code, 422)

        locked = self.client.post(
            "/v1/matters/alpha_matter_001/lock-submission",
            headers=self._headers("lock-ok"),
            json={"expected_version": version, "final_text": final_text},
        )
        self.assertEqual(locked.status_code, 200, locked.text)

    def test_calculation_preview_is_lead_only_and_self_checks(self) -> None:
        payload = {
            "scenario_id": "alpha_interest_001",
            "legal_bundle_id": "alpha_legal_bundle_interest_001",
            "version": 1,
            "start_date": "2020-08-20",
            "end_date": "2020-09-20",
            "allocation_policy": "INTEREST_THEN_PRINCIPAL",
            "approval_hash": "scenario-approval",
            "events": [
                {
                    "event_id": "alpha_disbursement_001",
                    "effective_date": "2020-08-20",
                    "sequence": 1,
                    "kind": "DISBURSEMENT",
                    "amount": "10000.00",
                    "currency": "CNY",
                    "evidence_ids": ["alpha_evidence_disbursement"],
                    "approval_hash": "event-approval-1",
                },
                {
                    "event_id": "alpha_payment_001",
                    "effective_date": "2020-09-04",
                    "sequence": 1,
                    "kind": "PAYMENT",
                    "amount": "1000.00",
                    "currency": "CNY",
                    "evidence_ids": ["alpha_evidence_payment"],
                    "approval_hash": "event-approval-2",
                },
            ],
            "rule_segments": [
                {
                    "segment_id": "alpha_segment_001",
                    "start_date": "2020-08-20",
                    "end_date": "2020-09-05",
                    "annual_rate": "0.10",
                    "source_rule_version": "SYNTHETIC-RULE-1",
                    "applicability_anchor": "CONTRACT_FORMED_AT",
                    "approval_hash": "rule-approval-1",
                },
                {
                    "segment_id": "alpha_segment_002",
                    "start_date": "2020-09-05",
                    "end_date": "2020-09-20",
                    "annual_rate": "0.05",
                    "source_rule_version": "SYNTHETIC-RULE-2",
                    "applicability_anchor": "FILED_AT",
                    "approval_hash": "rule-approval-2",
                },
            ],
        }
        denied = self.client.post("/v1/calculation-previews", headers={"X-Alpha-Actor": "alpha_assistant"}, json=payload)
        self.assertEqual(denied.status_code, 403)

        unknown_bundle = self.client.post(
            "/v1/calculation-previews",
            headers=self.lead_headers,
            json={**payload, "legal_bundle_id": "alpha_legal_bundle_missing_001"},
        )
        self.assertEqual(unknown_bundle.status_code, 422)
        self.assertIn("unknown approved case legal bundle", unknown_bundle.json()["detail"])

        preview = self.client.post("/v1/calculation-previews", headers=self.lead_headers, json=payload)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["independent_check_match"])
        self.assertEqual(preview.json()["legal_bundle_id"], "alpha_legal_bundle_interest_001")
        self.assertEqual(preview.json()["remaining_principal"], "9041.10")
        self.assertEqual(preview.json()["payment_allocations"][0]["allocated_principal"], "958.90")

    def test_alpha_local_preview_origin_is_explicitly_allowed(self) -> None:
        response = self.client.options(
            "/v1/calculation-previews",
            headers={
                "Origin": "http://[::1]:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://[::1]:3000")


if __name__ == "__main__":
    unittest.main()
