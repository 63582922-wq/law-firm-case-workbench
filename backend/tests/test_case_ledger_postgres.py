from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore, _evidence_links_from_json
from case_kernel.errors import VersionConflict
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.request_context import reset_request_id, set_request_id
from case_kernel.stable_pagination import StablePaginationBlocked, encode_page_cursor
from case_kernel.transaction_ledger import DatePrecision, TransactionChannel, TransactionDirection
from case_kernel.transaction_ledger import ClassificationOrigin, ObligationAllocation, PaymentNature


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None
    rowcount: int = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeConnection:
    def __init__(
        self,
        *,
        permitted: bool = True,
        fact_status: str = "CANDIDATE",
        duplicate_members: tuple[str, ...] = (),
        projection_version: int = 7,
        fact_page_rows: tuple[dict, ...] = (),
        transaction_page_rows: tuple[dict, ...] = (),
        summary_claim_rows: tuple[dict, ...] = (),
        summary_issue_rows: tuple[dict, ...] = (),
        fact_total_count: int = 0,
        fact_candidate_count: int = 0,
        transaction_total_count: int = 0,
    ) -> None:
        self.permitted = permitted
        self.fact_status = fact_status
        self.duplicate_members = duplicate_members
        self.projection_version = projection_version
        self.fact_page_rows = fact_page_rows
        self.transaction_page_rows = transaction_page_rows
        self.summary_claim_rows = summary_claim_rows
        self.summary_issue_rows = summary_issue_rows
        self.fact_total_count = fact_total_count
        self.fact_candidate_count = fact_candidate_count
        self.transaction_total_count = transaction_total_count
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": self.permitted})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"authorized": 1} if self.permitted else None)
        if normalized.startswith("SELECT matter_id, title, stage, version FROM matters"):
            return FakeResult(
                row={"matter_id": params[0], "title": "[合成] 持久化快照案件", "stage": "FACT_REVIEW", "version": 7}
            )
        if normalized.startswith("SELECT version FROM matters"):
            return FakeResult(row={"version": self.projection_version})
        if "COUNT(*) FILTER (WHERE status = 'CANDIDATE')" in normalized:
            return FakeResult(
                row={
                    "total_count": self.fact_total_count,
                    "candidate_count": self.fact_candidate_count,
                }
            )
        if normalized.startswith("SELECT COUNT(*) AS total_count FROM case_transactions"):
            return FakeResult(row={"total_count": self.transaction_total_count})
        if normalized.startswith("SELECT fact_id, original_text, origin, status, jsonb_array_length(evidence_links)"):
            return FakeResult(rows=list(self.fact_page_rows))
        if normalized.startswith("SELECT transaction.transaction_id, transaction.local_date"):
            return FakeResult(rows=list(self.transaction_page_rows))
        if normalized.startswith("SELECT (SELECT COUNT(*) FROM case_facts"):
            return FakeResult(
                row={
                    "fact_count": self.fact_total_count,
                    "candidate_fact_count": self.fact_candidate_count,
                    "transaction_count": self.transaction_total_count,
                }
            )
        if normalized.startswith("SELECT claim.claim_id, claim.original_claim_text"):
            return FakeResult(rows=list(self.summary_claim_rows))
        if normalized.startswith("SELECT issue.issue_id, issue.question, issue.status"):
            return FakeResult(rows=list(self.summary_issue_rows))
        if "SELECT status FROM case_facts" in normalized:
            return FakeResult(row={"status": self.fact_status})
        if "SELECT status, claimed_amount, currency FROM case_claims" in normalized:
            return FakeResult(row={"status": "CONFIRMED_SCOPE", "claimed_amount": Decimal("1000.00"), "currency": "CNY"})
        if "SELECT fact_id, status FROM case_facts WHERE fact_id = ANY" in normalized:
            return FakeResult(rows=[{"fact_id": value, "status": "CONFIRMED"} for value in (params or ([],))[0]])
        if "SELECT claim_id, status FROM case_claims WHERE claim_id = ANY" in normalized:
            return FakeResult(rows=[{"claim_id": value, "status": "CONFIRMED_SCOPE"} for value in (params or ([],))[0]])
        if "SELECT amount, currency, status, evidence_links FROM case_transactions" in normalized:
            return FakeResult(
                row={
                    "amount": Decimal("1000.00"),
                    "currency": "CNY",
                    "status": "CONFIRMED",
                    "evidence_links": [
                        {
                            "evidence_id": "synthetic-transaction-evidence",
                            "original_file_sha256": "a" * 64,
                            "page_number": 1,
                            "region_id": "synthetic-region",
                            "original_label": "[合成] 原始交易页",
                        }
                    ],
                }
            )
        if "SELECT pc.status, pc.nature, pc.transaction_id" in normalized:
            return FakeResult(
                row={
                    "status": "CANDIDATE",
                    "nature": "INTEREST_PAYMENT",
                    "transaction_id": str(uuid4()),
                    "amount": Decimal("1000.00"),
                    "currency": "CNY",
                    "transaction_status": "CONFIRMED",
                }
            )
        if "SELECT obligation_id, amount, currency FROM case_payment_allocations" in normalized:
            return FakeResult(rows=[{"obligation_id": "synthetic-obligation", "amount": Decimal("1000.00"), "currency": "CNY"}])
        if "SELECT transaction_id, status FROM case_transactions WHERE transaction_id = ANY" in normalized:
            return FakeResult(rows=[{"transaction_id": value, "status": "CONFIRMED"} for value in (params or ([],))[0]])
        if "SELECT member.transaction_id FROM case_transaction_duplicate_members" in normalized:
            return FakeResult(row=None)
        if "SELECT status FROM case_transaction_duplicate_groups" in normalized:
            return FakeResult(row={"status": "CANDIDATE"})
        if "SELECT transaction_id FROM case_transaction_duplicate_members" in normalized:
            return FakeResult(rows=[{"transaction_id": value} for value in self.duplicate_members])
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> FakeConnection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def evidence() -> tuple[EvidenceLink, ...]:
    return (
        EvidenceLink(
            evidence_id="synthetic-evidence-id",
            original_file_sha256="a" * 64,
            page_number=1,
            region_id="synthetic-region-id",
            original_label="[合成] 微信账单第1页",
        ),
    )


class PostgresCaseLedgerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresCaseLedgerStore("postgresql://not-used.invalid/lawcase_workbench_test")

    def test_fact_candidate_commits_object_version_audit_outbox_and_idempotency(self) -> None:
        connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.create_fact_candidate(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="fact-create-001",
                original_text="[合成] 被告主张已支付一笔款项。",
                origin=AssertionOrigin.DEFENDANT_STATEMENT,
                evidence_links=evidence(),
            )

        UUID(receipt.object_id)
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_facts", sql)
        self.assertIn("UPDATE matters SET version = version + 1", sql)
        self.assertIn("INSERT INTO audit_events", sql)
        self.assertIn("INSERT INTO outbox_events", sql)
        self.assertIn("INSERT INTO command_idempotency", sql)
        self.assertLess(sql.index("SELECT set_config"), sql.index("INSERT INTO case_facts"))

    def test_api_request_id_is_bound_into_the_same_audit_transaction(self) -> None:
        connection = FakeConnection()
        request_id = str(uuid4())
        token = set_request_id(request_id)
        try:
            with patch(
                "case_kernel.case_ledger_postgres.psycopg.connect",
                return_value=FakeConnectionContext(connection),
            ):
                self.store.create_fact_candidate(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="fact-request-context-001",
                    original_text="[合成] 审计请求上下文。",
                    origin=AssertionOrigin.ASSISTANT_ENTRY,
                    evidence_links=evidence(),
                )
        finally:
            reset_request_id(token)
        audit_params = next(params for sql, params in connection.executed if "INSERT INTO audit_events" in sql)
        self.assertEqual(audit_params[7], request_id)

    def test_fact_decision_revokes_dependent_objects_and_stales_submission(self) -> None:
        connection = FakeConnection()
        fact_id = str(uuid4())
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.decide_fact(
                matter_id=self.matter_id,
                fact_id=fact_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="fact-decide-001",
                status=FactStatus.CONFIRMED,
                decision_hash="b" * 64,
            )

        self.assertEqual(receipt.object_id, fact_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("DELETE FROM case_claim_responses", sql)
        self.assertIn("UPDATE case_dispute_issues", sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", sql)
        self.assertIn("current_submission_bundle_id = CASE WHEN", sql)

    def test_database_membership_is_required_even_when_actor_claims_a_role(self) -> None:
        connection = FakeConnection(permitted=False)
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            with self.assertRaisesRegex(PermissionError, "database role"):
                self.store.create_fact_candidate(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="fact-create-denied",
                    original_text="[合成] 不应写入。",
                    origin=AssertionOrigin.AGENT_CANDIDATE,
                    evidence_links=evidence(),
                )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO case_facts", sql)

    def test_transaction_candidate_and_confirmation_use_the_same_command_boundary(self) -> None:
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            candidate = self.store.create_transaction_candidate(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="transaction-create-001",
                local_date=date(2020, 8, 20),
                date_precision=DatePrecision.EXACT_DATE,
                amount=Decimal("1000.00"),
                currency="cny",
                direction=TransactionDirection.OUTGOING,
                payer_label="[合成] 被告",
                payee_label="[合成] 原告",
                channel=TransactionChannel.WECHAT,
                transaction_reference="synthetic-reference",
                evidence_links=evidence(),
            )
        UUID(candidate.object_id)
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_transactions", candidate_sql)

        confirmation_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(confirmation_connection),
        ):
            confirmed = self.store.confirm_transaction(
                matter_id=self.matter_id,
                transaction_id=candidate.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="transaction-confirm-001",
                confirmation_hash="c" * 64,
            )
        self.assertEqual(confirmed.object_id, candidate.object_id)
        confirmation_sql = "\n".join(statement for statement, _ in confirmation_connection.executed)
        self.assertIn("UPDATE case_transactions SET status = 'CONFIRMED'", confirmation_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", confirmation_sql)

    def test_claim_response_uses_normalized_confirmed_fact_links(self) -> None:
        claim_id = str(uuid4())
        fact_id = str(uuid4())
        connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.set_claim_response(
                matter_id=self.matter_id,
                claim_id=claim_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="claim-response-001",
                position=ClaimResponsePosition.PARTIALLY_ADMIT,
                confirmed_fact_ids=(fact_id,),
                partial_amount=Decimal("800.00"),
                currency="CNY",
                approval_hash="d" * 64,
            )
        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_claim_responses", sql)
        self.assertIn("INSERT INTO case_claim_response_facts", sql)
        self.assertIn("UPDATE case_dispute_issues", sql)
        self.assertNotIn("confirmed_fact_ids", sql)

    def test_payment_classification_persists_normalized_allocations_and_approval(self) -> None:
        transaction_id = str(uuid4())
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            candidate = self.store.create_payment_classification_candidate(
                matter_id=self.matter_id,
                transaction_id=transaction_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="classification-create-001",
                origin=ClassificationOrigin.DEFENDANT_STATEMENT,
                nature=PaymentNature.INTEREST_PAYMENT,
                allocations=(ObligationAllocation("synthetic-obligation", Decimal("1000.00"), "CNY"),),
                same_day_sequence=1,
                evidence_links=evidence(),
            )
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_payment_classifications", candidate_sql)
        self.assertIn("INSERT INTO case_payment_allocations", candidate_sql)

        approval_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(approval_connection),
        ):
            approved = self.store.approve_payment_classification(
                matter_id=self.matter_id,
                classification_id=candidate.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="classification-approve-001",
                approval_hash="e" * 64,
            )
        self.assertEqual(approved.object_id, candidate.object_id)
        approval_sql = "\n".join(statement for statement, _ in approval_connection.executed)
        self.assertIn("SET status = 'INVALIDATED'", approval_sql)
        self.assertIn("SET status = 'APPROVED'", approval_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", approval_sql)

    def test_transaction_evidence_can_be_copied_without_exposing_private_transaction_detail(self) -> None:
        links = _evidence_links_from_json([
            {
                "evidence_id": "transaction-page-08",
                "original_file_sha256": "a" * 64,
                "page_number": 8,
                "region_id": "transaction-row-02",
                "original_label": "微信交易记录第8页",
            }
        ])
        self.assertEqual(links[0].evidence_id, "transaction-page-08")
        self.assertEqual(links[0].original_file_sha256, "a" * 64)

    def test_duplicate_group_preserves_all_sources_and_requires_member_canonical(self) -> None:
        first = str(uuid4())
        second = str(uuid4())
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            group = self.store.create_duplicate_group_candidate(
                matter_id=self.matter_id,
                transaction_ids=(first, second),
                actor=self.actor,
                expected_version=1,
                idempotency_key="duplicate-create-001",
            )
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_transaction_duplicate_groups", candidate_sql)
        self.assertEqual(candidate_sql.count("INSERT INTO case_transaction_duplicate_members"), 2)
        self.assertNotIn("DELETE FROM case_transactions", candidate_sql)

        resolution_connection = FakeConnection(duplicate_members=(first, second))
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(resolution_connection),
        ):
            resolved = self.store.resolve_duplicate_group(
                matter_id=self.matter_id,
                duplicate_group_id=group.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="duplicate-resolve-001",
                same_economic_event=True,
                canonical_transaction_id=first,
                approval_hash="f" * 64,
            )
        self.assertEqual(resolved.object_id, group.object_id)
        resolution_sql = "\n".join(statement for statement, _ in resolution_connection.executed)
        self.assertIn("UPDATE case_transaction_duplicate_groups", resolution_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", resolution_sql)

    def test_inexact_transaction_date_is_rejected_before_connection(self) -> None:
        with patch("case_kernel.case_ledger_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "non-exact"):
                self.store.create_transaction_candidate(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="transaction-invalid-date",
                    local_date=date(2020, 8, 20),
                    date_precision=DatePrecision.MONTH_ONLY,
                    amount=Decimal("1000.00"),
                    currency="CNY",
                    direction=TransactionDirection.OUTGOING,
                    payer_label=None,
                    payee_label=None,
                    channel=TransactionChannel.WECHAT,
                    transaction_reference=None,
                    evidence_links=evidence(),
                )
        connect.assert_not_called()

    def test_case_snapshot_uses_repeatable_read_before_tenant_queries(self) -> None:
        connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            snapshot = self.store.get_case_snapshot(matter_id=self.matter_id, actor=self.actor)
        self.assertEqual(snapshot.version, 7)
        self.assertEqual(len(snapshot.snapshot_hash), 64)
        self.assertEqual(snapshot.facts, ())
        self.assertTrue(connection.executed[0][0].startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        self.assertTrue(connection.executed[1][0].startswith("SELECT set_config"))

    def test_fact_page_is_version_bound_keyset_paged_and_minimal(self) -> None:
        first_id = str(uuid4())
        second_id = str(uuid4())
        third_id = str(uuid4())
        created_at = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)
        connection = FakeConnection(
            fact_total_count=3,
            fact_candidate_count=1,
            fact_page_rows=(
                {
                    "fact_id": first_id,
                    "original_text": "[合成] 已确认事实一",
                    "origin": "DEFENDANT_STATEMENT",
                    "status": "CONFIRMED",
                    "evidence_count": 2,
                    "created_at": created_at,
                },
                {
                    "fact_id": second_id,
                    "original_text": "[合成] 待确认事实二",
                    "origin": "AGENT_CANDIDATE",
                    "status": "CANDIDATE",
                    "evidence_count": 1,
                    "created_at": created_at,
                },
                {
                    "fact_id": third_id,
                    "original_text": "[合成] 已确认事实三",
                    "origin": "LAWYER_ENTRY",
                    "status": "CONFIRMED",
                    "evidence_count": 1,
                    "created_at": created_at,
                },
            ),
        )
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            page = self.store.list_fact_page(
                matter_id=self.matter_id,
                actor=self.actor,
                limit=2,
                cursor=None,
            )

        self.assertEqual(page.total_count, 3)
        self.assertEqual(page.candidate_count, 1)
        self.assertEqual(len(page.items), 2)
        self.assertTrue(page.has_more)
        self.assertIsNotNone(page.next_cursor)
        self.assertEqual(
            frozenset(page.items[0]),
            frozenset({"fact_id", "original_text", "origin", "status", "evidence_count"}),
        )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("ORDER BY created_at ASC, fact_id ASC", sql)
        self.assertNotIn("decision_hash", sql)
        self.assertNotIn("decided_by", sql)

        continuation = FakeConnection(fact_total_count=3, fact_candidate_count=1)
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(continuation),
        ):
            self.store.list_fact_page(
                matter_id=self.matter_id,
                actor=self.actor,
                limit=2,
                cursor=page.next_cursor,
            )
        continuation_sql = "\n".join(statement for statement, _ in continuation.executed)
        self.assertIn("AND (created_at, fact_id) >", continuation_sql)

    def test_fact_page_rejects_changed_version_before_reading_rows(self) -> None:
        cursor = encode_page_cursor(
            kind="FACTS",
            matter_id=self.matter_id,
            matter_version=7,
            sort_values=("2026-08-10T03:00:00Z", str(uuid4())),
        )
        connection = FakeConnection(projection_version=8)
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            with self.assertRaisesRegex(VersionConflict, "restart from the first page"):
                self.store.list_fact_page(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    limit=50,
                    cursor=cursor,
                )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("FROM case_facts", sql)

    def test_first_page_can_be_bound_to_the_summary_version(self) -> None:
        connection = FakeConnection(projection_version=8)
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            with self.assertRaisesRegex(VersionConflict, "expected paged projection version 7"):
                self.store.list_fact_page(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    limit=50,
                    cursor=None,
                    expected_version=7,
                )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("FROM case_facts", sql)

    def test_case_review_summary_does_not_load_fact_or_transaction_rows(self) -> None:
        claim_id = str(uuid4())
        issue_id = str(uuid4())
        connection = FakeConnection(
            fact_total_count=41,
            fact_candidate_count=3,
            transaction_total_count=112,
            summary_claim_rows=(
                {
                    "claim_id": claim_id,
                    "original_claim_text": "[合成] 请求偿还本金",
                    "claimed_amount": Decimal("1000.00"),
                    "currency": "CNY",
                    "status": "CONFIRMED_SCOPE",
                    "position": "ADMIT",
                    "partial_amount": None,
                    "response_currency": None,
                },
            ),
            summary_issue_rows=(
                {
                    "issue_id": issue_id,
                    "question": "[合成] 利息标准如何确定？",
                    "status": "CONFIRMED",
                    "claim_count": 1,
                    "fact_count": 2,
                },
            ),
        )
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            summary = self.store.get_case_review_summary(matter_id=self.matter_id, actor=self.actor)

        self.assertEqual(summary.version, 7)
        self.assertEqual(summary.fact_count, 41)
        self.assertEqual(summary.candidate_fact_count, 3)
        self.assertEqual(summary.transaction_count, 112)
        self.assertEqual(summary.claims[0]["response"]["position"], "ADMIT")
        self.assertEqual(summary.issues[0]["fact_count"], 2)
        self.assertEqual(len(summary.summary_hash), 64)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("SELECT fact_id, original_text", sql)
        self.assertNotIn("SELECT transaction_id, local_date", sql)

    def test_transaction_page_omits_private_detail_and_uses_stable_order(self) -> None:
        transaction_ids = (str(uuid4()), str(uuid4()))
        created_at = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
        connection = FakeConnection(
            transaction_total_count=2,
            transaction_page_rows=(
                {
                    "transaction_id": transaction_ids[0],
                    "local_date": date(2020, 8, 20),
                    "amount": Decimal("1000.00"),
                    "currency": "CNY",
                    "status": "CONFIRMED",
                    "evidence_count": 1,
                    "created_at": created_at,
                    "classification_nature": "INTEREST_PAYMENT",
                    "classification_status": "APPROVED",
                },
                {
                    "transaction_id": transaction_ids[1],
                    "local_date": None,
                    "amount": Decimal("200.00"),
                    "currency": "CNY",
                    "status": "CANDIDATE",
                    "evidence_count": 1,
                    "created_at": created_at,
                    "classification_nature": None,
                    "classification_status": None,
                },
            ),
        )
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            page = self.store.list_transaction_page(
                matter_id=self.matter_id,
                actor=self.actor,
                limit=50,
                cursor=None,
            )

        self.assertEqual(page.total_count, 2)
        self.assertFalse(page.has_more)
        self.assertEqual(page.items[0]["classification_nature"], "INTEREST_PAYMENT")
        self.assertTrue(
            frozenset({"payer_label", "payee_label", "transaction_reference", "evidence_links"}).isdisjoint(
                page.items[0]
            )
        )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("LEFT JOIN LATERAL", sql)
        self.assertIn("ORDER BY (transaction.local_date IS NULL) ASC", sql)
        self.assertNotIn("payer_label", sql)
        self.assertNotIn("payee_label", sql)
        self.assertNotIn("transaction_reference", sql)

    def test_cross_matter_page_cursor_is_rejected_before_connection(self) -> None:
        cursor = encode_page_cursor(
            kind="FACTS",
            matter_id=str(uuid4()),
            matter_version=7,
            sort_values=("2026-08-10T03:00:00Z", str(uuid4())),
        )
        with patch("case_kernel.case_ledger_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(StablePaginationBlocked, "scope"):
                self.store.list_fact_page(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    limit=50,
                    cursor=cursor,
                )
        connect.assert_not_called()

    def test_alpha_identifiers_are_rejected_before_connection(self) -> None:
        with patch("case_kernel.case_ledger_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "requires UUID"):
                self.store.create_fact_candidate(
                    matter_id="alpha_matter_001",
                    actor=Actor("alpha_lead", "alpha_firm", frozenset({Role.LEAD_LAWYER})),
                    expected_version=1,
                    idempotency_key="alpha-rejected",
                    original_text="[合成] 不可持久化。",
                    origin=AssertionOrigin.AGENT_CANDIDATE,
                    evidence_links=evidence(),
                )
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
