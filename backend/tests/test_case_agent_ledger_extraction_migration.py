from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0042_case_agent_ledger_extraction_staging.sql"
)


class CaseAgentLedgerExtractionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_staging_keeps_exception_lane_and_source_text_binding(self) -> None:
        self.assertIn("case_agent_ledger_extraction_batches", self.sql)
        self.assertIn("case_agent_ledger_extraction_candidates", self.sql)
        self.assertIn("case_agent_ledger_extraction_staging_events", self.sql)
        self.assertIn("case_agent_ledger_extraction_candidate_pages", self.sql)
        self.assertIn("case_agent_ledger_extraction_batch_confirmations", self.sql)
        self.assertIn("review_lane", self.sql)
        self.assertIn("eligible_for_bulk_promotion", self.sql)
        self.assertIn("review_reason_codes", self.sql)
        self.assertIn("source_text_sha256", self.sql)
        self.assertIn("lawyer_batch_decision_hash", self.sql)
        self.assertIn("EXCEPTION_REVIEW", self.sql)
        self.assertIn(
            "extraction_candidate_id, extraction_batch_id, firm_id, matter_id",
            self.sql,
        )
        self.assertIn("ALTER TABLE %I FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("case_agent_ledger_extraction_promotions", self.sql)
        self.assertIn("case_agent_ledger_extraction_batch_confirmations", self.sql)

    def test_private_staging_preserves_source_version_and_private_audit(self) -> None:
        self.assertIn(
            "CHECK (staged_matter_version = source_matter_version)", self.sql
        )
        self.assertNotIn(
            "CHECK (staged_matter_version = source_matter_version + 1)", self.sql
        )
        self.assertIn("VERIFIED_EXTRACTION_STAGED_PRIVATE", self.sql)
        self.assertIn(
            "case_agent_ledger_extraction_staging_events_append_only", self.sql
        )

    def test_exception_reason_codes_are_fixed_and_bulk_lane_has_none(self) -> None:
        for code in (
            "POSSIBLE_DUPLICATE",
            "OCR_DERIVED",
            "BELOW_BULK_CONFIDENCE_THRESHOLD",
            "NON_NATIVE_SOURCE",
            "SOURCE_TEXT_NOT_REVERIFIED",
            "CURRENT_LEDGER_CONFLICT_OR_DUPLICATE",
        ):
            self.assertIn(code, self.sql)
        self.assertIn("cardinality(review_reason_codes) = 0", self.sql)
        self.assertIn("cardinality(review_reason_codes) > 0", self.sql)

    def test_polymorphic_promotion_uses_trigger_not_subquery_check(self) -> None:
        self.assertIn(
            "CREATE FUNCTION case_agent_ledger_extraction_target_matches_candidate",
            self.sql,
        )
        self.assertIn(
            "CREATE FUNCTION enforce_case_agent_ledger_extraction_promotion_target", self.sql
        )
        self.assertIn(
            "case_agent_ledger_extraction_promotions_target_integrity", self.sql
        )
        self.assertIn("target.firm_id = input_firm_id", self.sql)
        self.assertIn("target.matter_id = input_matter_id", self.sql)
        self.assertIn("target.original_text = candidate_payload->>'fact_text'", self.sql)
        self.assertIn("target.evidence_links = expected_evidence_links", self.sql)
        self.assertIn(
            "target.amount = (candidate_payload->>'amount')::numeric", self.sql
        )
        self.assertIn("jsonb_array_elements_text", self.sql)
        self.assertGreaterEqual(
            self.sql.count(
                "case_agent_ledger_extraction_target_matches_candidate("
            ),
            4,
        )
        self.assertIn(
            "extraction promotion target changed before commit", self.sql
        )
        self.assertNotIn("CHECK (EXISTS", self.sql.upper())

    def test_batch_confirmation_requires_the_exact_eligible_set(self) -> None:
        self.assertIn(
            "enforce_case_agent_ledger_extraction_batch_confirmation_completeness",
            self.sql,
        )
        self.assertIn(
            "case_agent_ledger_extraction_batch_confirmation_complete",
            self.sql,
        )
        self.assertIn("candidate.eligible_for_bulk_promotion", self.sql)
        self.assertIn("NOT candidate.eligible_for_bulk_promotion", self.sql)
        self.assertIn(
            "promotion_count IS DISTINCT FROM NEW.confirmed_candidate_count",
            self.sql,
        )
        self.assertIn(
            "promotion.promoted_matter_version IS DISTINCT FROM",
            self.sql,
        )
        self.assertIn("fact.status IS DISTINCT FROM 'CONFIRMED'", self.sql)
        self.assertIn(
            "transaction_row.status IS DISTINCT FROM 'CONFIRMED'", self.sql
        )

    def test_new_foreign_key_paths_are_indexed(self) -> None:
        self.assertIn("case_agent_ledger_extraction_batches_run_task_idx", self.sql)
        self.assertIn("case_agent_ledger_extraction_batches_verification_idx", self.sql)
        self.assertIn("case_agent_ledger_extraction_staging_events_run_idx", self.sql)
        self.assertIn(
            "case_agent_ledger_extraction_candidate_pages_evidence_idx", self.sql
        )
        self.assertIn("case_agent_ledger_extraction_promotions_batch_idx", self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
