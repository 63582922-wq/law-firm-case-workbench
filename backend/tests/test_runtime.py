from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.formal_calculation_postgres import PostgresFormalCalculationStore
from case_kernel.legal_source_postgres import PostgresLegalSourceStore
from case_kernel.postgres_store import PostgresMatterStore
from case_kernel.official_source_capture_postgres import PostgresOfficialSourceCaptureStore
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.submission_postgres import PostgresSubmissionStore
from case_kernel.reviewable_draft_postgres import PostgresReviewableDraftStore
from case_kernel.agent_execution_postgres import PostgresAgentExecutionStore
from case_kernel.external_request_postgres import PostgresExternalRequestStore
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
        self.assertIsNone(services.legal_source_store)
        self.assertIsNone(services.official_source_capture_store)
        self.assertIsNone(services.submission_store)
        self.assertIsNone(services.reviewable_draft_store)
        self.assertIsNone(services.agent_execution_store)
        self.assertIsNone(services.external_request_store)
        self.assertIsNone(services.artifact_store)
        self.assertIsNone(services.office_pdf_converter)
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
        self.assertIsInstance(services.legal_source_store, PostgresLegalSourceStore)
        self.assertIsInstance(services.official_source_capture_store, PostgresOfficialSourceCaptureStore)
        self.assertIsInstance(services.submission_store, PostgresSubmissionStore)
        self.assertIsInstance(services.reviewable_draft_store, PostgresReviewableDraftStore)
        self.assertIsInstance(services.agent_execution_store, PostgresAgentExecutionStore)
        self.assertIsInstance(services.external_request_store, PostgresExternalRequestStore)
        self.assertIsNone(services.artifact_store)
        self.assertNotIn("synthetic-password", repr(settings))
        matter_connect.assert_not_called()
        ledger_connect.assert_not_called()

    def test_persistent_runtime_can_bind_one_keychain_backed_store_to_all_object_verifiers(self) -> None:
        settings = RuntimeSettings(
            mode=RuntimeMode.POSTGRES_INTERNAL_PREVIEW,
            _postgres_dsn="postgresql://localhost/lawcase_preview",
        )
        with TemporaryDirectory(prefix="runtime-artifact-store-test-") as temporary:
            store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed",
                key_id="synthetic-runtime-key-v1",
                encryption_key=b"r" * 32,
            )
            services = build_runtime_services(settings, artifact_store=store)
        self.assertIs(services.artifact_store, store)
        self.assertIsNotNone(services.legal_source_store._official_source_reader)
        self.assertIsNotNone(services.official_source_capture_store._artifact_reader)
        self.assertIsNotNone(services.submission_store._artifact_reader)
        self.assertIsNotNone(services.reviewable_draft_store._artifact_reader)

    def test_synthetic_runtime_rejects_a_persistent_artifact_store(self) -> None:
        with TemporaryDirectory(prefix="runtime-synthetic-store-test-") as temporary:
            store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed",
                key_id="synthetic-runtime-key-v1",
                encryption_key=b"r" * 32,
            )
            with self.assertRaisesRegex(RuntimeConfigurationBlocked, "cannot receive"):
                build_runtime_services(
                    RuntimeSettings(mode=RuntimeMode.SYNTHETIC_ALPHA), artifact_store=store
                )

    def test_office_conversion_requires_explicit_preview_acknowledgement_and_both_bundled_paths(self) -> None:
        base = {
            "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
            "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
            "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://localhost/lawcase_preview",
            "CASE_WORKBENCH_OFFICE_SOFFICE": "/opt/lawcase/soffice",
        }
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "ENABLE_OFFICE_CONVERSION"):
            RuntimeSettings.from_environment(base)
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "OFFICE_SOFFICE and CASE_WORKBENCH_OFFICE_PDF_RENDERER"):
            RuntimeSettings.from_environment({**base, "CASE_WORKBENCH_ENABLE_OFFICE_CONVERSION": "YES"})

    def test_office_conversion_never_becomes_available_without_managed_encrypted_storage(self) -> None:
        settings = RuntimeSettings(
            mode=RuntimeMode.POSTGRES_INTERNAL_PREVIEW,
            _postgres_dsn="postgresql://localhost/lawcase_preview",
            _office_soffice_executable="/opt/lawcase/soffice",
            _office_pdf_renderer_executable="/opt/lawcase/pdftoppm",
        )
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "encrypted artifact store"):
            build_runtime_services(settings)


if __name__ == "__main__":
    unittest.main()
