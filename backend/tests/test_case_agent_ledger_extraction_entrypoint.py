from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_api import case_agent_worker_entrypoint
from case_api.case_agent_worker_entrypoint import (
    CaseAgentWorkerConfigurationBlocked,
    CaseAgentWorkerProcessSettings,
    compose_production_case_agent_worker,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
)


class LedgerExtractionEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        preflight_patcher = patch.object(
            case_agent_worker_entrypoint,
            "preflight_case_agent_ledger_exception_followup_schema",
        )
        self.addCleanup(preflight_patcher.stop)
        preflight_patcher.start()

    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "LAWCASE_AGENT_WORKER_RUNTIME_MODE": "PRODUCTION_AGENT_WORKER",
            "LAWCASE_AGENT_WORKER_FIRM_ID": str(uuid4()),
            "LAWCASE_AGENT_WORKER_ACTOR_ID": str(uuid4()),
            "LAWCASE_AGENT_VERIFIER_ACTOR_ID": str(uuid4()),
            "LAWCASE_AGENT_WORKER_DATABASE_ROLE": "lawcase_agent_worker",
            "LAWCASE_AGENT_WORKER_POSTGRES_DSN": (
                "postgresql://lawcase_agent_worker:secret@postgres.lawfirm.internal/"
                "lawcase?sslmode=verify-full"
            ),
            "LAWCASE_AGENT_VERIFIER_DATABASE_ROLE": "lawcase_agent_verifier",
            "LAWCASE_AGENT_VERIFIER_POSTGRES_DSN": (
                "postgresql://lawcase_agent_verifier:secret@postgres.lawfirm.internal/"
                "lawcase?sslmode=verify-full"
            ),
            "LAWCASE_AGENT_WORKER_PRIVATE_ROOT": str(root / "worker"),
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT": (
                "https://objects.lawfirm.internal"
            ),
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_REGION": "cn-south-1",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_BUCKET": "lawcase-private",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ACCESS_KEY_ID": "worker",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_SECRET_ACCESS_KEY": "s" * 32,
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENCRYPTION": "AES256",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_KMS_KEY_ID": "",
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": "planner-" + "p" * 32,
            "LAWCASE_AGENT_WORKER_DEEPSEEK_MODEL": "deepseek-chat",
            "LAWCASE_AGENT_WORKER_DEEPSEEK_ALLOWED_MODELS": "deepseek-chat",
        }

    def _enable(self, environment: dict[str, str]) -> str:
        secret = "ledger-" + "x" * 40
        environment.update(
            {
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY": secret,
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL": (
                    DEEPSEEK_LEDGER_EXTRACTION_MODEL
                ),
            }
        )
        return secret

    def test_all_empty_omits_capability_and_full_group_is_redacted(self):
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
            self.assertIsNone(settings.ledger_extraction_credentials)

            secret = self._enable(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
        self.assertIsNotNone(settings.ledger_extraction_credentials)
        self.assertNotIn(secret, repr(settings))

    def test_partial_or_nonfixed_group_blocks_startup(self):
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment[
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY"
            ] = "ledger-" + "x" * 40
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked,
                "server key and fixed model together",
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

            environment[
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL"
            ] = "deepseek-chat"
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked,
                "fixed contract",
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_all_empty_does_not_preflight_or_register_extraction_adapter(self):
        with TemporaryDirectory() as temporary:
            settings = CaseAgentWorkerProcessSettings.from_environment(
                self._environment(Path(temporary))
            )
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        runtime = object()
        with (
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresPlanningMemoryEnrichmentStore",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DynamicPlanningMemorySearchRequestFactory",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DeepSeekLedgerExtractionRawHttpsTransport",
            ) as transport_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "S3LedgerExtractionRawResponseStore",
            ) as store_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_staging_runtime_contract",
            ) as staging_preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_runtime_contract",
            ) as exchange_preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresRecoverableLedgerExtractionExchange",
            ) as exchange_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
                return_value=runtime,
            ) as compose,
        ):
            result = compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                object_store_factory=lambda _config: object(),
            )
        self.assertIs(result, runtime)
        transport_factory.assert_not_called()
        store_factory.assert_not_called()
        staging_preflight.assert_not_called()
        exchange_preflight.assert_not_called()
        exchange_factory.assert_not_called()
        self.assertIsNone(
            compose.call_args.kwargs["ledger_extraction_exchange"]
        )

    def test_both_0042_and_0045_preflight_before_readiness_composition(self):
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            self._enable(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        values = {
            "object_store": object(),
            "transport": object(),
            "response_store": object(),
            "exchange": object(),
            "runtime": object(),
        }
        order: list[str] = []
        with (
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresPlanningMemoryEnrichmentStore",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DynamicPlanningMemorySearchRequestFactory",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DeepSeekLedgerExtractionRawHttpsTransport",
                return_value=values["transport"],
            ) as transport_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "S3LedgerExtractionRawResponseStore",
                return_value=values["response_store"],
            ) as store_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_staging_runtime_contract",
                side_effect=lambda **_: order.append("0042-preflight"),
            ) as staging_preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_runtime_contract",
                side_effect=lambda **_: order.append("0045-preflight"),
            ) as exchange_preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresRecoverableLedgerExtractionExchange",
                side_effect=lambda **_: (
                    order.append("exchange-created") or values["exchange"]
                ),
            ) as exchange_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
                side_effect=lambda **_: (
                    order.append("readiness-composed") or values["runtime"]
                ),
            ) as compose,
        ):
            result = compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                object_store_factory=lambda _config: values["object_store"],
            )
        self.assertIs(result, values["runtime"])
        self.assertEqual(
            order,
            [
                "0042-preflight",
                "0045-preflight",
                "exchange-created",
                "readiness-composed",
            ],
        )
        transport_factory.assert_called_once_with(
            credentials=settings.ledger_extraction_credentials
        )
        store_factory.assert_called_once_with(settings.object_store)
        staging_preflight.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
        )
        exchange_preflight.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=values["transport"],
            response_store=values["response_store"],
        )
        exchange_factory.assert_called_once()
        self.assertIs(
            compose.call_args.kwargs["ledger_extraction_exchange"],
            values["exchange"],
        )

    def test_failed_0045_preflight_cannot_publish_adapter_heartbeat(self):
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            self._enable(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        with (
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresPlanningMemoryEnrichmentStore",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DynamicPlanningMemorySearchRequestFactory",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DeepSeekLedgerExtractionRawHttpsTransport",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "S3LedgerExtractionRawResponseStore",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_staging_runtime_contract",
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_ledger_extraction_runtime_contract",
                side_effect=RuntimeError("0045 absent"),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
            ) as compose,
        ):
            with self.assertRaisesRegex(RuntimeError, "0045 absent"):
                compose_production_case_agent_worker(
                    settings=settings,
                    planning_repository=repository,
                    object_store_factory=lambda _config: object(),
                )
        compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
