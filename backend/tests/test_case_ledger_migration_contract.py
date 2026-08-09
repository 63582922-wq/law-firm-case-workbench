from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0002_case_ledgers.sql"


class CaseLedgerMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_case_ledger_tables_are_matter_scoped_and_evidence_bound(self) -> None:
        for table in (
            "case_facts",
            "case_claims",
            "case_claim_responses",
            "case_claim_response_facts",
            "case_dispute_issues",
            "case_dispute_issue_claims",
            "case_dispute_issue_facts",
            "case_transactions",
            "case_payment_classifications",
            "case_payment_allocations",
            "case_transaction_duplicate_groups",
            "case_transaction_duplicate_members",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)
        self.assertGreaterEqual(self.sql.count("evidence_links jsonb NOT NULL"), 4)
        self.assertGreaterEqual(self.sql.count("FOREIGN KEY (matter_id, firm_id) REFERENCES matters(matter_id, firm_id)"), 12)

    def test_cross_object_links_are_normalized_and_same_matter_scoped(self) -> None:
        self.assertNotIn("confirmed_fact_ids uuid[]", self.sql)
        self.assertNotIn("obligation_allocations jsonb", self.sql)
        self.assertIn("CREATE TABLE case_claim_response_facts", self.sql)
        self.assertIn("CREATE TABLE case_payment_allocations", self.sql)
        self.assertIn("REFERENCES case_facts(fact_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES case_claims(claim_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES case_transactions(transaction_id, firm_id, matter_id)", self.sql)

    def test_candidate_statuses_and_monetary_date_guards_are_encoded(self) -> None:
        self.assertIn("'CANDIDATE', 'CONFIRMED', 'DISPUTED', 'DENIED', 'INVALIDATED'", self.sql)
        self.assertIn("'CANDIDATE', 'CONFIRMED_SCOPE', 'INVALIDATED'", self.sql)
        self.assertIn("position = 'PARTIALLY_ADMIT'", self.sql)
        self.assertIn("date_precision = 'EXACT_DATE' AND local_date IS NOT NULL", self.sql)
        self.assertIn("currency ~ '^[A-Z]{3}$'", self.sql)
        self.assertIn("case_payment_classifications_one_approved_per_transaction", self.sql)
        self.assertIn("'INTEREST_PAYMENT', 'PRINCIPAL_REPAYMENT'", self.sql)
        self.assertIn("'SAME_ECONOMIC_EVENT', 'DISTINCT_EVENTS'", self.sql)
        self.assertIn("status = 'APPROVED' AND approval_hash IS NOT NULL", self.sql)
        self.assertIn("status <> 'CANDIDATE' AND confirmation_hash IS NOT NULL", self.sql)


if __name__ == "__main__":
    unittest.main()
