from pathlib import Path
import unittest


SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0062_case_agent_ledger_exception_staging_trigger_authority.sql"
).read_text(encoding="utf-8")


class CaseAgentLedgerExceptionStagingTriggerAuthorityMigrationTests(unittest.TestCase):
    def test_staging_trigger_is_an_internal_isolated_definer(self) -> None:
        self.assertIn(
            "ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()\n"
            "    OWNER TO lawcase_schema_owner",
            SQL,
        )
        self.assertIn(
            "ALTER FUNCTION public.group_case_agent_ledger_exceptions_after_staging()\n"
            "    SECURITY DEFINER",
            SQL,
        )
        self.assertIn("SET search_path = pg_catalog, public, pg_temp", SQL)
        self.assertIn("lawcase_agent_worker", SQL)
        self.assertNotIn("GRANT EXECUTE", SQL)


if __name__ == "__main__":
    unittest.main()
