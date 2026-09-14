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
from case_kernel.lpr_source_parser import parse_lpr_source_bytes
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
        license_reviewed: bool = True,
        reused_source_snapshot: dict | None = None,
        reused_rule_version: dict | None = None,
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
        self.license_reviewed = license_reviewed
        self.reused_source_snapshot = reused_source_snapshot
        self.reused_rule_version = reused_rule_version
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
                    "license_basis": (
                        "official public access for internal legal review"
                        if self.license_reviewed
                        else None
                    ),
                    "license_review_hash": "e" * 64 if self.license_reviewed else None,
                }
            )
        if (
            "FROM official_legal_source_snapshots" in normalized
            and "WHERE firm_id = %s AND source_id = %s AND content_sha256 = %s" in normalized
        ):
            return FakeResult(row=self.reused_source_snapshot)
        if "FROM official_lpr_observations" in normalized:
            return FakeResult(row={"one_year_rate": Decimal("0.0385")})
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
                        "license_basis": "official public access for internal legal review",
                        "license_review_hash": "e" * 64,
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
                    "source_id": "CFETS-LPR-HISTORY",
                    "content_sha256": "a" * 64,
                    "verification_status": "VERIFIED",
                    "license_status": "ACTIVE",
                    "license_basis": (
                        "official public access for internal legal review"
                        if self.license_reviewed
                        else None
                    ),
                    "license_review_hash": "e" * 64 if self.license_reviewed else None,
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
                        "license_basis": "official public access for internal legal review",
                        "license_review_hash": "e" * 64,
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
                        "parameter_license_basis": "official public access for internal legal review",
                        "parameter_license_review_hash": "e" * 64,
                        "parameter_authority_level": "OFFICIAL_RATE_DATA",
                        "content_sha256": "a" * 64,
                        "verification_status": "VERIFIED",
                        "license_status": "ACTIVE",
                        "license_basis": "official public access for internal legal review",
                        "license_review_hash": "e" * 64,
                    }
                ]
            )
        if (
            "FROM legal_rule_versions" in normalized
            and "WHERE firm_id = %s AND rule_id = %s AND rule_version = %s" in normalized
        ):
            return FakeResult(row=self.reused_rule_version)
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


class ReviewedCaptureConnection(FakeLegalConnection):
    def __init__(
        self,
        *,
        plaintext: bytes,
        decision: str = "APPROVE_FOR_REGISTRATION",
        source_id: str = "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
        source_tier: str = "JUDICIAL_INTERPRETATION",
        final_url: str = "https://www.court.gov.cn/zixun/xiangqing/282621.html",
        content_media_type: str = "text/html",
        parser_kind: str = "PRIVATE_LENDING_SECOND_REVISION",
        parsed_output_hash: str = "d" * 64,
        provision_locator: str = "第二十五条、第三十一条",
    ) -> None:
        super().__init__()
        self.plaintext = plaintext
        self.content_hash = sha256(plaintext).hexdigest()
        self.capture_run_id = str(uuid4())
        self.capture_review_id = str(uuid4())
        self.decision = decision
        self.capture_source_id = source_id
        self.capture_source_tier = source_tier
        self.capture_final_url = final_url
        self.capture_content_media_type = content_media_type
        self.capture_parser_kind = parser_kind
        self.capture_parsed_output_hash = parsed_output_hash
        self.capture_provision_locator = provision_locator

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        if "FROM official_source_capture_runs capture" in normalized:
            self.executed.append((normalized, params))
            return FakeResult(
                row={
                    "run_id": self.capture_run_id,
                    "source_id": self.capture_source_id,
                    "publisher": "全国银行间同业拆借中心" if self.capture_source_id == "CFETS-LPR-HISTORY" else "最高人民法院",
                    "source_tier": self.capture_source_tier,
                    "final_url": self.capture_final_url,
                    "retrieved_at": datetime.now(timezone.utc),
                    "content_media_type": self.capture_content_media_type,
                    "content_sha256": self.content_hash,
                    "storage_object_key": (
                        f"{self.content_hash[:2]}/{self.content_hash[2:4]}/{self.content_hash}.lca"
                    ),
                    "capture_verification_hash": "c" * 64,
                    "parser_kind": self.capture_parser_kind,
                    "parsed_output_hash": self.capture_parsed_output_hash,
                    "review_id": self.capture_review_id,
                    "decision": self.decision,
                    "provision_locator": self.capture_provision_locator,
                    "review_hash": "e" * 64,
                }
            )
        if "WHERE capture_run_id = %s" in normalized:
            self.executed.append((normalized, params))
            return FakeResult(row=None)
        return super().execute(sql, params)


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
            license_basis="official public access for internal legal review",
            license_review_hash="e" * 64,
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
        # ``www.spp.gov.cn`` is an explicitly registered primary-law mirror,
        # not a broad ``*.gov.cn`` exception.  Reaching the missing-reader
        # guard proves the URL passed the formal-source allowlist.
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "encrypted-object verifier"):
            self.store.register_official_source_snapshot(
                **common,
                official_url="https://www.spp.gov.cn/zdgz/202006/t20200602_463886.shtml",
            )
        # ADR-0073 admits this exact government host for the frozen M1 Civil
        # Code source.  A missing object reader proves URL validation passed;
        # the following assertion keeps the exception from becoming a broad
        # MIIT-domain wildcard.
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "encrypted-object verifier"):
            self.store.register_official_source_snapshot(
                **common,
                official_url=(
                    "https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/"
                    "art_20cf1a2e1b854924b5caa744c8045d1f.html"
                ),
            )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "registered official"):
            self.store.register_official_source_snapshot(
                **common,
                official_url="https://miit.gov.cn/zwgk/not-the-approved-source.html",
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
                license_basis="official public access for internal legal review",
                license_review_hash="e" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        self.assertTrue(
            any("INSERT INTO official_legal_source_snapshots" in sql for sql, _ in connection.executed)
        )

    def test_matching_firm_source_snapshot_is_reused_with_a_new_matter_receipt(self) -> None:
        plaintext = b"synthetic shared official source bytes"
        content_hash = sha256(plaintext).hexdigest()
        storage_object_key = f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca"
        connection = FakeLegalConnection()
        connection.reused_source_snapshot = {
            "snapshot_id": connection.source_snapshot_id,
            "publisher": "最高人民法院",
            "authority_level": "JUDICIAL_INTERPRETATION",
            "official_url": "https://www.court.gov.cn/zixun/xiangqing/282621.html",
            "provision_locator": "第二十五条、第三十一条",
            "content_media_type": "text/html",
            "storage_object_key": storage_object_key,
            "verification_status": "VERIFIED",
            "license_status": "ACTIVE",
            "license_basis": "official public access for internal legal review",
            "license_review_hash": "e" * 64,
        }
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda _key, _expected: plaintext,
        )

        receipt = self.run_with(
            connection,
            lambda: store.register_official_source_snapshot(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="official-source-reuse-001",
                source_id="PRIVATE-LENDING-CURRENT",
                publisher="最高人民法院",
                authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION,
                official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
                provision_locator="第二十五条、第三十一条",
                retrieved_at=datetime.now(timezone.utc),
                content_sha256=content_hash,
                content_media_type="text/html",
                storage_object_key=storage_object_key,
                verification_hash="b" * 64,
                license_basis="official public access for internal legal review",
                license_review_hash="f" * 64,
            ),
        )

        self.assertEqual(receipt.object_id, connection.source_snapshot_id)
        self.assertFalse(
            any("INSERT INTO official_legal_source_snapshots" in sql for sql, _ in connection.executed)
        )
        self.assertIn("OFFICIAL_LEGAL_SOURCE_SNAPSHOT_REUSED", str(connection.executed))

    def test_conflicting_firm_source_snapshot_cannot_be_silently_reused(self) -> None:
        plaintext = b"synthetic conflicting official source bytes"
        content_hash = sha256(plaintext).hexdigest()
        storage_object_key = f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca"
        connection = FakeLegalConnection()
        connection.reused_source_snapshot = {
            "snapshot_id": connection.source_snapshot_id,
            "publisher": "错误发布者",
            "authority_level": "JUDICIAL_INTERPRETATION",
            "official_url": "https://www.court.gov.cn/zixun/xiangqing/282621.html",
            "provision_locator": "第二十五条、第三十一条",
            "content_media_type": "text/html",
            "storage_object_key": storage_object_key,
            "verification_status": "VERIFIED",
            "license_status": "ACTIVE",
            "license_basis": "official public access for internal legal review",
            "license_review_hash": "e" * 64,
        }
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda _key, _expected: plaintext,
        )

        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "different governance fields"):
            self.run_with(
                connection,
                lambda: store.register_official_source_snapshot(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="official-source-reuse-conflict-001",
                    source_id="PRIVATE-LENDING-CURRENT",
                    publisher="最高人民法院",
                    authority_level=LegalAuthorityLevel.JUDICIAL_INTERPRETATION,
                    official_url="https://www.court.gov.cn/zixun/xiangqing/282621.html",
                    provision_locator="第二十五条、第三十一条",
                    retrieved_at=datetime.now(timezone.utc),
                    content_sha256=content_hash,
                    content_media_type="text/html",
                    storage_object_key=storage_object_key,
                    verification_hash="b" * 64,
                    license_basis="official public access for internal legal review",
                    license_review_hash="f" * 64,
                ),
            )
        self.assertFalse(
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
                    license_basis="official public access for internal legal review",
                    license_review_hash="e" * 64,
                    supersedes_snapshot_id=connection.source_snapshot_id,
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("SET verification_status = 'SUPERSEDED'", sql)

    def test_reviewed_capture_registration_derives_formal_fields_and_reauthenticates_object(self) -> None:
        plaintext = b"synthetic captured official source bytes"
        connection = ReviewedCaptureConnection(plaintext=plaintext)
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda key, expected: (
                plaintext
                if key == f"{connection.content_hash[:2]}/{connection.content_hash[2:4]}/{connection.content_hash}.lca"
                and expected == connection.content_hash
                else b""
            ),
        )
        receipt = self.run_with(
            connection,
            lambda: store.register_reviewed_capture_snapshot(
                matter_id=self.matter_id,
                run_id=connection.capture_run_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="reviewed-capture-register-001",
                license_basis="官方公开页面，仅限本案内部法律研究与引用核验。",
                license_review_hash="f" * 64,
                registration_hash="9" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        insert = next(
            (params for sql, params in connection.executed if "INSERT INTO official_legal_source_snapshots" in sql),
            None,
        )
        self.assertIsNotNone(insert)
        self.assertEqual(insert[-1], connection.capture_run_id)
        self.assertIn("官方公开页面", insert[-5])
        audit_text = str(connection.executed)
        self.assertIn("REVIEWED_OFFICIAL_SOURCE_CAPTURE_REGISTERED", audit_text)

    def test_rejected_capture_cannot_become_a_formal_source_snapshot(self) -> None:
        plaintext = b"synthetic rejected official source bytes"
        connection = ReviewedCaptureConnection(plaintext=plaintext, decision="REJECT")
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda _key, _expected: plaintext,
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "not approved"):
            self.run_with(
                connection,
                lambda: store.register_reviewed_capture_snapshot(
                    matter_id=self.matter_id,
                    run_id=connection.capture_run_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="reviewed-capture-register-rejected",
                    license_basis="官方公开页面，仅限本案内部法律研究与引用核验。",
                    license_review_hash="f" * 64,
                    registration_hash="9" * 64,
                ),
            )
        self.assertFalse(
            any("INSERT INTO official_legal_source_snapshots" in sql for sql, _ in connection.executed)
        )

    def test_reviewed_lpr_capture_rederives_and_persists_authenticated_observations(self) -> None:
        plaintext = (
            b'{"head":{"rep_code":"200"},"records":['
            b'{"showDateCN":"2021-01-20","1Y":"3.85","5Y":"4.65"}]}'
        )
        captured_at = datetime.now(timezone.utc)
        parsed = parse_lpr_source_bytes(
            source_id="CFETS-LPR-HISTORY",
            source_url="https://www.chinamoney.com.cn/",
            content_sha256=sha256(plaintext).hexdigest(),
            media_type="application/json",
            retrieved_on=captured_at.date(),
            body=plaintext,
        )
        connection = ReviewedCaptureConnection(
            plaintext=plaintext,
            source_id="CFETS-LPR-HISTORY",
            source_tier="OFFICIAL_RATE_DATA",
            final_url="https://www.chinamoney.com.cn/",
            content_media_type="application/json",
            parser_kind="CFETS_LPR_JSON",
            parsed_output_hash=parsed.parsed_output_hash,
            provision_locator="records[0]",
        )
        store = PostgresLegalSourceStore(
            "postgresql://not-used.invalid/lawcase_test",
            official_source_reader=lambda _key, _expected: plaintext,
        )
        self.run_with(
            connection,
            lambda: store.register_reviewed_capture_snapshot(
                matter_id=self.matter_id,
                run_id=connection.capture_run_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="reviewed-lpr-capture-register-001",
                license_basis="官方公开页面，仅限本案内部法律研究与引用核验。",
                license_review_hash="f" * 64,
                registration_hash="9" * 64,
            ),
        )
        observation_insert = next(
            (params for sql, params in connection.executed if "INSERT INTO official_lpr_observations" in sql),
            None,
        )
        self.assertIsNotNone(observation_insert)
        self.assertEqual(observation_insert[7], Decimal("0.038500"))
        self.assertEqual(observation_insert[9], "records[0]")

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
                base_annual_rate=None,
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
        self.assertEqual(inserts[0][12], Decimal("0.0385"))
        self.assertEqual(inserts[0][14], Decimal("0.1540"))
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("FROM official_lpr_observations", sql)
        self.assertIn("UPDATE calculation_runs", sql)
        self.assertIn("UPDATE case_legal_bundles", sql)

    def test_matching_firm_rule_version_is_reused_with_a_new_matter_receipt(self) -> None:
        connection = FakeLegalConnection()
        connection.reused_rule_version = {
            "rule_version_id": connection.rule_version_id,
            "issue_key": "private_lending_response_source_scope",
            "source_snapshot_id": connection.source_snapshot_id,
            "parameter_source_snapshot_id": None,
            "parameter_evidence_locator": None,
            "effective_from": date(2019, 1, 1),
            "effective_to": None,
            "trigger_event_kind": "CONTRACT_SIGNED",
            "formula_kind": "NO_INTEREST",
            "base_annual_rate": None,
            "rate_multiplier": None,
            "derived_annual_rate": Decimal("0"),
            "required_fact_keys": [],
            "transition_rule_versions": [],
            "conflict_set": None,
            "priority": 1,
            "status": "APPROVED",
        }

        receipt = self.run_with(
            connection,
            lambda: self.store.approve_rule_version(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-rule-reuse-001",
                rule_id="private-lending-response-source-scope",
                rule_version="1.0.0",
                issue_key="private_lending_response_source_scope",
                source_snapshot_id=connection.source_snapshot_id,
                parameter_source_snapshot_id=None,
                parameter_evidence_locator=None,
                effective_from=date(2019, 1, 1),
                effective_to=None,
                trigger_event_kind=LegalEventKind.CONTRACT_SIGNED,
                formula_kind=LegalRateFormulaKind.NO_INTEREST,
                base_annual_rate=None,
                rate_multiplier=None,
                required_fact_keys=(),
                transition_rule_versions=(),
                conflict_set=None,
                priority=1,
                approval_hash="b" * 64,
            ),
        )

        self.assertEqual(receipt.object_id, connection.rule_version_id)
        self.assertFalse(
            any("INSERT INTO legal_rule_versions" in sql for sql, _ in connection.executed)
        )
        self.assertIn("LEGAL_RULE_VERSION_REUSED", str(connection.executed))

    def test_conflicting_firm_rule_version_cannot_be_silently_reused(self) -> None:
        connection = FakeLegalConnection()
        connection.reused_rule_version = {
            "rule_version_id": connection.rule_version_id,
            "issue_key": "wrong_issue_key",
            "source_snapshot_id": connection.source_snapshot_id,
            "parameter_source_snapshot_id": None,
            "parameter_evidence_locator": None,
            "effective_from": date(2019, 1, 1),
            "effective_to": None,
            "trigger_event_kind": "CONTRACT_SIGNED",
            "formula_kind": "NO_INTEREST",
            "base_annual_rate": None,
            "rate_multiplier": None,
            "derived_annual_rate": Decimal("0"),
            "required_fact_keys": [],
            "transition_rule_versions": [],
            "conflict_set": None,
            "priority": 1,
            "status": "APPROVED",
        }

        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "different governed fields"):
            self.run_with(
                connection,
                lambda: self.store.approve_rule_version(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="legal-rule-reuse-conflict-001",
                    rule_id="private-lending-response-source-scope",
                    rule_version="1.0.0",
                    issue_key="private_lending_response_source_scope",
                    source_snapshot_id=connection.source_snapshot_id,
                    parameter_source_snapshot_id=None,
                    parameter_evidence_locator=None,
                    effective_from=date(2019, 1, 1),
                    effective_to=None,
                    trigger_event_kind=LegalEventKind.CONTRACT_SIGNED,
                    formula_kind=LegalRateFormulaKind.NO_INTEREST,
                    base_annual_rate=None,
                    rate_multiplier=None,
                    required_fact_keys=(),
                    transition_rule_versions=(),
                    conflict_set=None,
                    priority=1,
                    approval_hash="b" * 64,
                ),
            )
        self.assertFalse(
            any("INSERT INTO legal_rule_versions" in sql for sql, _ in connection.executed)
        )

    def test_legacy_source_without_license_review_cannot_support_a_new_rule(self) -> None:
        connection = FakeLegalConnection(license_reviewed=False)
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "explicit license review"):
            self.run_with(
                connection,
                lambda: self.store.approve_rule_version(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="legal-rule-unlicensed-source",
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
                    base_annual_rate=None,
                    rate_multiplier=Decimal("4"),
                    required_fact_keys=("contract_before_2020_08_20",),
                    transition_rule_versions=("PRIVATE-LENDING-2015",),
                    conflict_set="private-lending-interest-cap",
                    priority=100,
                    approval_hash="c" * 64,
                ),
            )
        self.assertFalse(
            any("INSERT INTO legal_rule_versions" in sql for sql, _ in connection.executed)
        )

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
                    base_annual_rate=None,
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

    def test_lpr_rule_rejects_rate_submitted_by_client_before_database(self) -> None:
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "not accepted from the client"):
            self.store.approve_rule_version(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="legal-rule-client-rate-001",
                rule_id="private-lending-cap",
                rule_version="PRIVATE-LENDING-LPR-2020-08",
                issue_key="interest_cap_after_2020_08_20",
                source_snapshot_id=str(uuid4()),
                parameter_source_snapshot_id=str(uuid4()),
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
            )

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
