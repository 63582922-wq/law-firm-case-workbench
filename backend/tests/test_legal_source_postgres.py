from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.legal_rules import LegalEventKind
from case_kernel.legal_source_postgres import (
    LegalAuthorityLevel,
    LegalBundleSegmentSelection,
    LegalRateFormulaKind,
    PostgresLegalSourceStore,
)
from case_kernel.models import Actor, Role


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeLegalConnection:
    def __init__(
        self,
        *,
        include_required_binding: bool = True,
        rate_authority: str = "OFFICIAL_RATE_DATA",
        impacted_matter_id: str | None = None,
    ) -> None:
        self.source_snapshot_id = str(uuid4())
        self.rate_source_snapshot_id = str(uuid4())
        self.rule_version_id = str(uuid4())
        self.legal_event_id = str(uuid4())
        self.evidence_page_id = str(uuid4())
        self.fact_id = str(uuid4())
        self.include_required_binding = include_required_binding
        self.rate_authority = rate_authority
        self.impacted_matter_id = impacted_matter_id
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SET TRANSACTION") or "SELECT set_config" in normalized:
            return FakeResult()
        if "SELECT 1 FROM matters m" in normalized:
            return FakeResult(row={"authorized": 1})
        if normalized.startswith("SELECT version FROM matters"):
            return FakeResult(row={"version": 9})
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": True})
        if "SELECT source_id, verification_status, license_status" in normalized:
            return FakeResult(
                row={
                    "source_id": "PRIVATE-LENDING-CURRENT",
                    "verification_status": "VERIFIED",
                    "license_status": "ACTIVE",
                }
            )
        if "FROM case_legal_bundle_segments segment" in normalized:
            return FakeResult(
                rows=(
                    [{"matter_id": self.impacted_matter_id}]
                    if self.impacted_matter_id is not None
                    else []
                )
            )
        if "FROM official_legal_source_snapshots" in normalized and "snapshot_id = ANY" in normalized:
            return FakeResult(
                rows=[
                    {
                        "snapshot_id": self.rate_source_snapshot_id,
                        "content_sha256": "f" * 64,
                        "verification_status": "VERIFIED",
                        "license_status": "ACTIVE",
                        "authority_level": self.rate_authority,
                    }
                ]
            )
        if "FROM official_legal_source_snapshots" in normalized and "FOR SHARE" in normalized:
            authority_level = (
                "OFFICIAL_RATE_DATA"
                if params and str(params[0]) == self.rate_source_snapshot_id
                else "JUDICIAL_INTERPRETATION"
            )
            if params and str(params[0]) == self.rate_source_snapshot_id:
                authority_level = self.rate_authority
            return FakeResult(
                row={
                    "content_sha256": "a" * 64,
                    "verification_status": "VERIFIED",
                    "license_status": "ACTIVE",
                    "authority_level": authority_level,
                }
            )
        if "FROM official_legal_source_snapshots" in normalized:
            return FakeResult(
                rows=[
                    {
                        "snapshot_id": self.source_snapshot_id,
                        "source_id": "PRIVATE-LENDING-CURRENT",
                        "publisher": "最高人民法院",
                        "authority_level": "JUDICIAL_INTERPRETATION",
                        "official_url": "https://www.court.gov.cn/zixun/xiangqing/282621.html",
                        "provision_locator": "第二十五条、第三十一条",
                        "retrieved_at": datetime(2026, 8, 10, tzinfo=timezone.utc),
                        "content_sha256": "a" * 64,
                        "content_media_type": "text/html",
                        "verification_status": "VERIFIED",
                        "license_status": "ACTIVE",
                        "verified_by": self.fact_id,
                        "verification_hash": "b" * 64,
                        "supersedes_snapshot_id": None,
                    }
                ]
            )
        if "FROM legal_rule_versions rule" in normalized:
            return FakeResult(
                rows=[
                    {
                        "rule_version_id": self.rule_version_id,
                        "rule_version": "PRIVATE-LENDING-LPR-2020-08",
                        "issue_key": "interest_cap_after_2020_08_20",
                        "effective_from": date(2020, 8, 20),
                        "effective_to": None,
                        "trigger_event_kind": "CLAIM_FILED",
                        "derived_annual_rate": Decimal("0.154"),
                        "rule_status": "APPROVED",
                        "source_snapshot_id": self.source_snapshot_id,
                        "rule_approval_hash": "b" * 64,
                        "required_fact_keys": ["contract_before_2020_08_20"],
                        "formula_kind": "LPR_MULTIPLE",
                        "parameter_source_snapshot_id": self.rate_source_snapshot_id,
                        "parameter_evidence_locator": "2021-01-15 前最近一期一年期 LPR 3.85%",
                        "parameter_content_sha256": "f" * 64,
                        "parameter_verification_status": "VERIFIED",
                        "parameter_license_status": "ACTIVE",
                        "parameter_authority_level": "OFFICIAL_RATE_DATA",
                        "content_sha256": "a" * 64,
                        "verification_status": "VERIFIED",
                        "license_status": "ACTIVE",
                    }
                ]
            )
        if "FROM case_legal_fact_bindings binding" in normalized:
            if not self.include_required_binding:
                return FakeResult(rows=[])
            return FakeResult(
                rows=[
                    {
                        "fact_key": "contract_before_2020_08_20",
                        "fact_id": self.fact_id,
                        "binding_status": "APPROVED",
                        "fact_status": "CONFIRMED",
                        "decision_hash": "d" * 64,
                    }
                ]
            )
        if "FROM case_facts" in normalized and "FOR SHARE" in normalized:
            return FakeResult(row={"status": "CONFIRMED", "decision_hash": "d" * 64})
        if "FROM evidence_pages" in normalized and "FOR SHARE" in normalized:
            requested = {str(value) for value in (params[0] if params else [])}
            rows = (
                [{"evidence_page_id": self.evidence_page_id}]
                if self.evidence_page_id in requested
                else []
            )
            return FakeResult(rows=rows)
        if "FROM case_legal_events" in normalized and "FOR SHARE" in normalized:
            return FakeResult(
                rows=[
                    {
                        "legal_event_id": self.legal_event_id,
                        "event_kind": "CLAIM_FILED",
                        "local_date": date(2021, 1, 15),
                        "status": "APPROVED",
                    }
                ]
            )
        if "FROM legal_rule_versions" in normalized:
            return FakeResult(rows=[])
        if "FROM case_legal_events" in normalized:
            return FakeResult(rows=[])
        if "FROM case_legal_fact_bindings" in normalized and "binding" not in normalized:
            return FakeResult(rows=[])
        if "FROM case_legal_bundles" in normalized and "MAX(version)" not in normalized:
            return FakeResult(row=None)
        if "SELECT COALESCE(MAX(version), 0) + 1 AS next_version" in normalized:
            return FakeResult(row={"next_version": 1})
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeLegalConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class LegalSourceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresLegalSourceStore("postgresql://not-used.invalid/lawcase_test")

    def run_with(self, connection: FakeLegalConnection, callback):
        with patch(
            "case_kernel.legal_source_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            return callback()

    def test_source_snapshot_rejects_nonofficial_url_and_hash_mismatched_object_before_database(self) -> None:
        common = dict(
            matter_id=self.matter_id,
            actor=self.actor,
            expected_version=1,
            idempotency_key="legal-source-001",
            source_id="PRIVATE-LENDING-CURRENT",
            publisher="最高人民法院",
            authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION,
            provision_locator="第二十五条、第三十一条",
            retrieved_at=datetime.now(timezone.utc),
            content_sha256="a" * 64,
            content_media_type="text/html",
            storage_object_key=f"aa/aa/{'a' * 64}.lca",
            verification_hash="b" * 64,
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "registered official"):
            self.store.register_official_source_snapshot(
                **common, official_url="https://example.com/not-official"
            )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "content-addressed"):
            self.store.register_official_source_snapshot(
                **{**common, "storage_object_key": f"bb/bb/{'b' * 64}.lca"},
                official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
            )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "encrypted-object verifier"):
            self.store.register_official_source_snapshot(
                **common,
                official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
            )

    def test_source_snapshot_authenticates_encrypted_plaintext_before_database_registration(self) -> None:
        plaintext = b"synthetic official source bytes"
        content_hash = sha256(plaintext).hexdigest()
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda object_key, expected_hash: (
                plaintext
                if object_key == f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca"
                and expected_hash == content_hash
                else b""
            ),
        )
        connection = FakeLegalConnection()
        receipt = self.run_with(
            connection,
            lambda: store.register_official_source_snapshot(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="official-source-authenticated-001",
                source_id="PRIVATE-LENDING-CURRENT",
                publisher="最高人民法院",
                authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION,
                official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
                provision_locator="第二十五条、第三十一条",
                retrieved_at=datetime.now(timezone.utc),
                content_sha256=content_hash,
                content_media_type="text/html",
                storage_object_key=f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca",
                verification_hash="b" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        self.assertTrue(
            any("INSERT INTO official_legal_source_snapshots" in sql for sql, _ in connection.executed)
        )

    def test_source_replacement_blocks_cross_matter_downstream_invalidation(self) -> None:
        plaintext = b"synthetic replacement source bytes"
        content_hash = sha256(plaintext).hexdigest()
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda _object_key, _expected_hash: plaintext,
        )
        connection = FakeLegalConnection(impacted_matter_id=str(uuid4()))
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "bulk impact review"):
            self.run_with(
                connection,
                lambda: store.register_official_source_snapshot(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="official-source-cross-matter-001",
                    source_id="PRIVATE-LENDING-CURRENT",
                    publisher="最高人民法院",
                    authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION,
                    official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
                    provision_locator="第二十五条、第三十一条",
                    retrieved_at=datetime.now(timezone.utc),
                    content_sha256=content_hash,
                    content_media_type="text/html",
                    storage_object_key=f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca",
                    verification_hash="b" * 64,
                    supersedes_snapshot_id=connection.source_snapshot_id,
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("SET verification_status = 'SUPERSEDED'", sql)

    def test_approved_lpr_rule_derives_rate_from_verified_source_and_stales_dependents(self) -> None:
        connection = FakeLegalConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.approve_rule_version(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-rule-001",
                rule_id="private-lending-cap",
                rule_version="PRIVATE-LENDING-LPR-2020-08",
                issue_key="interest_cap_after_2020_08_20",
                source_snapshot_id=connection.source_snapshot_id,
                parameter_source_snapshot_id=connection.rate_source_snapshot_id,
                parameter_evidence_locator="2021-01-15 前最近一期一年期 LPR 3.85%",
                effective_from=date(2020, 8, 20),
                effective_to=None,
                trigger_event_kind=LegalEventKind.CLAIM_FILED,
                formula_kind=LegalRateFormulaKind.LPR_MULTIPLE,
                base_annual_rate=Decimal("0.0385"),
                rate_multiplier=Decimal("4"),
                required_fact_keys=("contract_before_2020_08_20",),
                transition_rule_versions=(),
                conflict_set=None,
                priority=100,
                approval_hash="b" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        inserts = [params for sql, params in connection.executed if "INSERT INTO legal_rule_versions" in sql]
        self.assertEqual(inserts[0][14], Decimal("0.1540"))
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("UPDATE calculation_runs", sql)
        self.assertIn("UPDATE case_legal_bundles", sql)

    def test_lpr_rule_rejects_non_rate_parameter_snapshot(self) -> None:
        connection = FakeLegalConnection(rate_authority="JUDICIAL_INTERPRETATION")
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "official rate-data"):
            self.run_with(
                connection,
                lambda: self.store.approve_rule_version(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="legal-rule-wrong-rate-source-001",
                    rule_id="private-lending-cap",
                    rule_version="PRIVATE-LENDING-LPR-2020-08",
                    issue_key="interest_cap_after_2020_08_20",
                    source_snapshot_id=connection.source_snapshot_id,
                    parameter_source_snapshot_id=connection.rate_source_snapshot_id,
                    parameter_evidence_locator="2021-01-15 前最近一期一年期 LPR 3.85%",
                    effective_from=date(2020, 8, 20),
                    effective_to=None,
                    trigger_event_kind=LegalEventKind.CLAIM_FILED,
                    formula_kind=LegalRateFormulaKind.LPR_MULTIPLE,
                    base_annual_rate=Decimal("0.0385"),
                    rate_multiplier=Decimal("4"),
                    required_fact_keys=("contract_before_2020_08_20",),
                    transition_rule_versions=(),
                    conflict_set=None,
                    priority=100,
                    approval_hash="b" * 64,
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO legal_rule_versions", sql)

    def test_bundle_uses_server_derived_rate_and_approved_event_not_client_rate(self) -> None:
        connection = FakeLegalConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.approve_case_legal_bundle(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-bundle-001",
                segments=(
                    LegalBundleSegmentSelection(
                        segment_id=str(uuid4()),
                        issue_key="interest_cap_after_2020_08_20",
                        rule_version_id=connection.rule_version_id,
                        trigger_event_id=connection.legal_event_id,
                        start_date=date(2020, 8, 20),
                        end_date=date(2022, 1, 1),
                        applicability_anchor="起诉时司法保护标准",
                    ),
                ),
                approval_hash="c" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        segment_insert = next(
            params for sql, params in connection.executed if "INSERT INTO case_legal_bundle_segments" in sql
        )
        self.assertEqual(segment_insert[15], Decimal("0.154"))
        self.assertIn("起诉时", segment_insert[16])
        self.assertEqual(segment_insert[9], connection.rate_source_snapshot_id)
        self.assertEqual(segment_insert[10], "f" * 64)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_legal_bundles", sql)
        self.assertIn("INSERT INTO case_legal_bundle_rule_versions", sql)
        self.assertIn("INSERT INTO audit_events", sql)

    def test_bundle_blocks_when_required_fact_key_is_not_human_bound(self) -> None:
        connection = FakeLegalConnection(include_required_binding=False)
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "contract_before_2020_08_20"):
            self.run_with(
                connection,
                lambda: self.store.approve_case_legal_bundle(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="legal-bundle-missing-fact-001",
                    segments=(
                        LegalBundleSegmentSelection(
                            segment_id=str(uuid4()),
                            issue_key="interest_cap_after_2020_08_20",
                            rule_version_id=connection.rule_version_id,
                            trigger_event_id=connection.legal_event_id,
                            start_date=date(2020, 8, 20),
                            end_date=date(2022, 1, 1),
                            applicability_anchor="起诉时司法保护标准",
                        ),
                    ),
                    approval_hash="c" * 64,
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO case_legal_bundles", sql)

    def test_legal_fact_key_can_bind_only_to_a_confirmed_decided_fact(self) -> None:
        connection = FakeLegalConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.approve_legal_fact_binding(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-fact-binding-001",
                fact_key="contract_before_2020_08_20",
                fact_id=connection.fact_id,
                approval_hash="e" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_legal_fact_bindings", sql)
        self.assertIn("UPDATE case_legal_bundles", sql)

    def test_legal_event_requires_and_normalizes_same_matter_evidence_pages(self) -> None:
        connection = FakeLegalConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.approve_legal_event(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-event-evidence-001",
                event_kind=LegalEventKind.PAYMENT,
                local_date=date(2020, 9, 1),
                evidence_ids=(connection.evidence_page_id,),
                approval_hash="e" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("FROM evidence_pages", sql)
        self.assertIn("INSERT INTO case_legal_event_evidence_pages", sql)
        event_insert = next(
            params
            for statement, params in connection.executed
            if "INSERT INTO case_legal_events" in statement
        )
        self.assertNotIn(connection.evidence_page_id, event_insert)

    def test_legal_event_blocks_foreign_or_missing_evidence_page(self) -> None:
        connection = FakeLegalConnection()
        missing_page_id = str(uuid4())
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "same matter"):
            self.run_with(
                connection,
                lambda: self.store.approve_legal_event(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="legal-event-foreign-evidence-001",
                    event_kind=LegalEventKind.PAYMENT,
                    local_date=date(2020, 9, 1),
                    evidence_ids=(missing_page_id,),
                    approval_hash="e" * 64,
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO case_legal_events", sql)

    def test_legal_review_read_is_repeatable_and_never_exposes_storage_object_keys(self) -> None:
        connection = FakeLegalConnection()
        snapshot = self.run_with(
            connection,
            lambda: self.store.get_legal_review_snapshot(
                matter_id=self.matter_id,
                actor=self.actor,
            ),
        )
        self.assertEqual(snapshot.matter_version, 9)
        self.assertEqual(len(snapshot.sources), 1)
        self.assertNotIn("storage_object_key", snapshot.sources[0])
        self.assertEqual(len(snapshot.snapshot_hash), 64)
        statements = [statement for statement, _ in connection.executed]
        self.assertEqual(statements[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertIn("SELECT set_config", statements[1])


if __name__ == "__main__":
    unittest.main()
