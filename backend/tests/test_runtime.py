from unittest.mock import patch
import unittest

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.formal_calculation_postgres import PostgresFormalCalculationStore
from case_kernel.postgres_store import PostgresMatterStore
from case_kernel.runtime import (
    RuntimeConfigurationBlocked,
    RuntimeMode,
    RuntimeSettings,
    build_runtime_services,
)
from case_kernel.store import InMemoryMatterStore


class RuntimeSettingsTests(unittest.TestCase):
    def test_default_runtime_is_synthetic_and_never_connects(self) -> None:
        settings = RuntimeSettings.from_environment({})
        with patch("case_kernel.postgres_store.psycopg.connect") as matter_connect, patch(
            "case_kernel.case_ledger_postgres.psycopg.connect"
        ) as ledger_connect:
            services = build_runtime_services(settings)
        self.assertEqual(settings.mode, RuntimeMode.SYNTHETIC_ALPHA)
        self.assertIsInstance(services.matter_store, InMemoryMatterStore)
        self.assertIsNone(services.case_ledger_store)
        self.assertIsNone(services.formal_calculation_store)
        matter_connect.assert_not_called()
        ledger_connect.assert_not_called()

    def test_database_settings_cannot_leak_into_synthetic_mode(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "cannot be present"):
            RuntimeSettings.from_environment(
                {"CASE_WORKBENCH_POSTGRES_DSN": "postgresql://localhost/lawcase_preview"}
            )

    def test_persistent_preview_requires_explicit_acknowledgement_and_dedicated_database(self) -> None:
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "requires CASE_WORKBENCH_ENABLE"):
            RuntimeSettings.from_environment(
                {
                    "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
                    "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://localhost/lawcase_preview",
                }
            )
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "ending in _preview or _test"):
            RuntimeSettings.from_environment(
                {
                    "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
                    "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
                    "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://localhost/production",
                }
            )

    def test_persistent_preview_builds_adapters_without_opening_a_connection_or_exposing_dsn(self) -> None:
        settings = RuntimeSettings.from_environment(
            {
                "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
                "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
                "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://synthetic-user:synthetic-password@localhost/lawcase_preview",
            }
        )
        with patch("case_kernel.postgres_store.psycopg.connect") as matter_connect, patch(
            "case_kernel.case_ledger_postgres.psycopg.connect"
        ) as ledger_connect:
            services = build_runtime_services(settings)
        self.assertIsInstance(services.matter_store, PostgresMatterStore)
        self.assertIsInstance(services.case_ledger_store, PostgresCaseLedgerStore)
        self.assertIsInstance(services.formal_calculation_store, PostgresFormalCalculationStore)
        self.assertNotIn("synthetic-password", repr(settings))
        matter_connect.assert_not_called()
        ledger_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
