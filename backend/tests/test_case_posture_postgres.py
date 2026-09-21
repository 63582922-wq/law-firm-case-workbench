from __future__ import annotations

from hashlib import sha256
import inspect
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_kernel.case_posture_postgres import (
    CasePartyConfirmation,
    CourtProceedingConfirmation,
    FirmEngagementConfirmation,
    PostgresCasePostureStore,
    _profile_hash,
)
from case_kernel.models import Actor, Role


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, matter_version: int = 7) -> None:
        self.matter_version = matter_version
        self.executed: list[tuple[str, tuple | None]] = []
        self.profile_id: str | None = None

    def execute(self, sql: str, params: tuple | None = None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return _Result()
        if normalized.startswith("SELECT m.version,"):
            return _Result({"version": self.matter_version, "permitted": True})
        if normalized.startswith("SELECT pv.party_version_id"):
            return _Result(
                {
                    "party_version_id": str(uuid4()),
                    "display_label": "合成被代理人",
                    "proceeding_version_id": str(uuid4()),
                    "case_type_code": "CIVIL.PRIVATE_LENDING",
                    "procedure_stage": "FIRST_INSTANCE",
                    "position_version_id": str(uuid4()),
                    "position_code": "DEFENDANT",
                    "engagement_version_id": str(uuid4()),
                    "authority_scope_code": "GENERAL_AUTHORITY",
                    "engagement_state": "ACTIVE",
                }
            )
        if "SELECT latest_profile_version, current_profile_id" in normalized:
            return _Result()
        if normalized.startswith("SELECT profile_id FROM case_posture_profiles"):
            return _Result()
        if normalized.startswith("INSERT INTO case_posture_profiles"):
            assert params is not None
            self.profile_id = str(params[0])
            return _Result()
        if normalized.startswith("UPDATE matters SET version = version + 1"):
            return _Result({"version": self.matter_version + 1})
        return _Result()


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class CasePosturePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.store = PostgresCasePostureStore("postgresql://not-used.invalid/lawcase_test")

    def test_only_lead_lawyer_can_enter_a_write_command(self) -> None:
        with self.assertRaises(PermissionError):
            self.store.confirm_party(
                matter_id=self.matter_id,
                actor=self.worker,
                expected_version=1,
                idempotency_key="worker-cannot-confirm-party",
                confirmation=CasePartyConfirmation("NATURAL_PERSON", "合成当事人", "a" * 64),
            )

    def test_codes_and_basis_hashes_fail_closed_before_database_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical uppercase"):
            self.store.confirm_proceeding(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="invalid-proceeding-code",
                confirmation=CourtProceedingConfirmation(
                    "court-local", "CIVIL.PRIVATE_LENDING", "FIRST_INSTANCE", "a" * 64
                ),
            )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.store.confirm_engagement(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="invalid-engagement-basis",
                confirmation=FirmEngagementConfirmation(
                    str(uuid4()), str(uuid4()), "GENERAL_AUTHORITY", "ACTIVE", "not-a-hash"
                ),
            )

    def test_profile_hash_is_deterministic_and_binds_every_upstream_version(self) -> None:
        values = {
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "profile_version": 3,
            "represented_party_id": str(uuid4()),
            "represented_party_version_id": str(uuid4()),
            "proceeding_id": str(uuid4()),
            "proceeding_version_id": str(uuid4()),
            "position_id": str(uuid4()),
            "position_version_id": str(uuid4()),
            "engagement_id": str(uuid4()),
            "engagement_version_id": str(uuid4()),
            "case_type_code": "CIVIL.PRIVATE_LENDING",
            "procedure_stage": "FIRST_INSTANCE",
            "represented_position": "DEFENDANT",
            "authority_scope_code": "GENERAL_AUTHORITY",
            "engagement_state": "ACTIVE",
        }
        first = _profile_hash(**values)
        self.assertEqual(first, _profile_hash(**values))
        self.assertEqual(len(first), 64)
        changed = {**values, "position_version_id": str(uuid4())}
        self.assertNotEqual(first, _profile_hash(**changed))
        expected = sha256(
            "|".join(
                (
                    "case-posture-profile-v1",
                    *(str(value) for value in values.values()),
                )
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first, expected)

    def test_store_has_no_position_to_fixed_deliverable_mapping(self) -> None:
        source = inspect.getsource(PostgresCasePostureStore).lower()
        self.assertNotIn("required_material", source)
        self.assertNotIn("deliverable_template", source)
        self.assertNotIn("complaint_template", source)
        self.assertNotIn("defence_template", source)

    def test_profile_confirmation_uses_lock_idempotency_audit_and_outbox_command_chain(self) -> None:
        connection = _Connection(matter_version=7)
        party_id, proceeding_id, position_id, engagement_id = (
            str(uuid4()) for _ in range(4)
        )
        with patch(
            "case_kernel.case_posture_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            receipt = self.store.confirm_current_profile(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=7,
                idempotency_key="confirm-posture-profile-001",
                represented_party_id=party_id,
                proceeding_id=proceeding_id,
                position_id=position_id,
                engagement_id=engagement_id,
            )
        self.assertEqual(receipt.matter_version, 8)
        self.assertEqual(receipt.object_id, connection.profile_id)
        statements = [statement for statement, _ in connection.executed]
        self.assertTrue(any("pg_advisory_xact_lock" in item for item in statements))
        self.assertTrue(any("INSERT INTO case_posture_profiles" in item for item in statements))
        self.assertTrue(any("INSERT INTO audit_events" in item for item in statements))
        self.assertTrue(any("INSERT INTO outbox_events" in item for item in statements))
        self.assertTrue(any("INSERT INTO command_idempotency" in item for item in statements))

    def test_upstream_confirmation_receipt_returns_stable_id_needed_by_next_command(self) -> None:
        connection = _Connection(matter_version=7)
        with patch(
            "case_kernel.case_posture_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            receipt = self.store.confirm_party(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=7,
                idempotency_key="confirm-new-stable-party-001",
                confirmation=CasePartyConfirmation(
                    "NATURAL_PERSON", "合成当事人", "a" * 64
                ),
            )
        self.assertEqual(receipt.object_type, "CASE_PARTY")
        self.assertEqual(len(receipt.object_id), 36)
        inserted_party_id = next(
            params[0]
            for statement, params in connection.executed
            if statement.startswith("INSERT INTO case_parties") and params is not None
        )
        self.assertEqual(receipt.object_id, inserted_party_id)

    def test_mixed_system_worker_role_cannot_write_posture(self) -> None:
        mixed = Actor(
            str(uuid4()),
            self.firm_id,
            frozenset({Role.SYSTEM_WORKER, Role.LEAD_LAWYER}),
        )
        with self.assertRaisesRegex(PermissionError, "read-only"):
            self.store.confirm_party(
                matter_id=self.matter_id,
                actor=mixed,
                expected_version=1,
                idempotency_key="mixed-worker-cannot-confirm-party",
                confirmation=CasePartyConfirmation("NATURAL_PERSON", "合成当事人", "a" * 64),
            )


if __name__ == "__main__":
    unittest.main()
