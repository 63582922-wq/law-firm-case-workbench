from __future__ import annotations

from base64 import urlsafe_b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from case_api import case_agent_worker_entrypoint
from case_api.case_agent_worker_entrypoint import (
    CaseAgentWorkerConfigurationBlocked,
    CaseAgentWorkerProcessSettings,
    compose_production_case_agent_worker,
    run_production_case_agent_worker,
)
from case_kernel.case_agent_runtime_identity import case_agent_worker_id


class CaseAgentWorkerEntrypointTests(unittest.TestCase):
    def test_single_recovery_does_not_compose_or_run_the_agent_loop(self) -> None:
        from types import SimpleNamespace
        settings = SimpleNamespace(runtime=SimpleNamespace(verifier_postgres_dsn="verification-test",
            verifier_actor=object(), actor=SimpleNamespace(actor_id=str(uuid4()))), object_store=object())
        args = dict(settings=settings, matter_id=str(uuid4()), run_id=str(uuid4()), request_id=str(uuid4()))
        objects = Mock()
        with patch.object(case_agent_worker_entrypoint, "preflight_content_recovery_runtime") as preflight, \
             patch.object(case_agent_worker_entrypoint, "PostgresReviewableDocumentPackageAccessPort") as access, \
             patch.object(case_agent_worker_entrypoint, "PostgresContentRevisionRecoveryStore") as recovery, \
             patch.object(case_agent_worker_entrypoint, "compose_production_case_agent_worker") as compose:
            with self.assertRaises(CaseAgentWorkerConfigurationBlocked):
                case_agent_worker_entrypoint.recover_one_production_document_content(**args, object_store_factory=objects)
            with self.assertRaises(CaseAgentWorkerConfigurationBlocked):
                case_agent_worker_entrypoint.recover_one_production_document_content(**{**args, "request_id": "invalid"}, enabled=True, object_store_factory=objects)
            preflight.assert_not_called()
            objects.assert_not_called()
            recovery.return_value.recover.return_value = str(uuid4())
            result = case_agent_worker_entrypoint.recover_one_production_document_content(**args, enabled=True, object_store_factory=objects)
            self.assertEqual(result, recovery.return_value.recover.return_value)
            recovery.return_value.recover.assert_called_once_with(matter_id=args["matter_id"], run_id=args["run_id"], request_id=args["request_id"])
            self.assertTrue(recovery.call_args.kwargs["enabled"])
            access.assert_called_once()
            compose.assert_not_called()
            objects.reset_mock()
            preflight.side_effect = RuntimeError("preflight unavailable")
            with self.assertRaises(RuntimeError):
                case_agent_worker_entrypoint.recover_one_production_document_content(**args, enabled=True, object_store_factory=objects)
            objects.assert_not_called()

    def setUp(self) -> None:
        preflight_patcher = patch.object(
            case_agent_worker_entrypoint,
            "preflight_case_agent_ledger_exception_followup_schema",
        )
        self.exception_followup_preflight = preflight_patcher.start()
        self.addCleanup(preflight_patcher.stop)

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
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT": "https://objects.lawfirm.internal",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_REGION": "cn-south-1",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_BUCKET": "lawcase-private",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ACCESS_KEY_ID": "lawcase-agent-worker",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_SECRET_ACCESS_KEY": "x" * 32,
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENCRYPTION": "AES256",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_KMS_KEY_ID": "",
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": "d" * 32,
            "LAWCASE_AGENT_WORKER_DEEPSEEK_MODEL": "deepseek-chat",
            "LAWCASE_AGENT_WORKER_DEEPSEEK_ALLOWED_MODELS": "deepseek-chat",
        }

    @staticmethod
    def _enable_document_delivery(environment: dict[str, str]) -> str:
        secret = urlsafe_b64encode(b"r" * 32).decode("ascii").rstrip("=")
        environment.update(
            {
                "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED": "true",
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT": (
                    "http://document-renderer:8090"
                ),
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET": secret,
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
            }
        )
        return secret

    def test_settings_are_server_only_redacted_and_firm_scoped(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        self.assertEqual(
            settings.runtime.worker_id,
            case_agent_worker_id(environment["LAWCASE_AGENT_WORKER_FIRM_ID"]),
        )
        self.assertNotIn(environment["LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY"], repr(settings))
        self.assertNotIn(environment["LAWCASE_AGENT_WORKER_POSTGRES_DSN"], repr(settings))
        self.assertIsNone(settings.brave_credentials)
        self.assertIsNone(settings.qwen_credentials)
        self.assertIsNone(settings.qwen_workspace_id)
        self.assertIsNone(settings.qwen_pdftoppm_executable)
        self.assertIsNone(settings.lawyer_analysis_credentials)
        self.assertFalse(settings.document_delivery_enabled)
        self.assertFalse(settings.document_content_revisions_enabled)
        self.assertIsNone(settings.document_renderer_settings)

    def test_production_worker_refuses_missing_0049_lifecycle_before_adapters(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = CaseAgentWorkerProcessSettings.from_environment(
                self._environment(Path(temporary))
            )
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        object_store_factory = Mock()
        self.exception_followup_preflight.side_effect = RuntimeError(
            "synthetic missing 0049 lifecycle"
        )

        with self.assertRaisesRegex(
            CaseAgentWorkerConfigurationBlocked,
            "exception follow-up lifecycle database boundary is unavailable",
        ):
            compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                object_store_factory=object_store_factory,
            )

        self.exception_followup_preflight.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            firm_id=settings.runtime.actor.firm_id,
        )
        object_store_factory.assert_not_called()

    def test_optional_brave_key_is_server_only_and_redacted(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            secret = "brave-" + "s" * 32
            environment["LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY"] = secret
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        self.assertIsNotNone(settings.brave_credentials)
        self.assertNotIn(secret, repr(settings))

    def test_invalid_explicit_brave_key_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY"] = "short"
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "provider"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_optional_qwen_group_is_all_or_none_server_only_and_redacted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            renderer = root / "pdftoppm"
            renderer.touch()
            renderer.chmod(0o700)
            environment = self._environment(root)
            secret = "q" * 40
            environment.update(
                {
                    "LAWCASE_AGENT_WORKER_QWEN_API_KEY": secret,
                    "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID": "ws-legal-prod",
                    "LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE": str(renderer),
                }
            )
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        self.assertIsNotNone(settings.qwen_credentials)
        self.assertEqual(settings.qwen_workspace_id, "ws-legal-prod")
        self.assertEqual(settings.qwen_pdftoppm_executable, renderer.resolve())
        self.assertNotIn(secret, repr(settings))

    def test_partial_or_unsafe_qwen_group_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            environment["LAWCASE_AGENT_WORKER_QWEN_API_KEY"] = "q" * 40
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "requires"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

            renderer = root / "pdftoppm"
            renderer.touch()
            renderer.chmod(0o700)
            environment.update(
                {
                    "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID": "INVALID_WORKSPACE",
                    "LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE": str(renderer),
                }
            )
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "workspace"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_optional_lawyer_analysis_group_is_all_or_none_and_redacted(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            secret = "qa-" + "x" * 40
            environment.update(
                {
                    "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": secret,
                    "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID": (
                        "ws-lawyer-prod"
                    ),
                }
            )
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        self.assertIsNotNone(settings.lawyer_analysis_credentials)
        assert settings.lawyer_analysis_credentials is not None
        self.assertEqual(
            settings.lawyer_analysis_credentials.endpoint_host,
            "ws-lawyer-prod.cn-beijing.maas.aliyuncs.com",
        )
        self.assertNotIn(secret, repr(settings))

        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment[
                "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY"
            ] = secret
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "requires"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_dynamic_document_group_is_explicit_and_all_or_none(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            secret = self._enable_document_delivery(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        self.assertTrue(settings.document_delivery_enabled)
        self.assertIsNotNone(settings.document_renderer_settings)
        assert settings.document_renderer_settings is not None
        self.assertEqual(
            settings.document_renderer_settings.health_endpoint,
            "http://document-renderer:8090/healthz",
        )
        self.assertNotIn(secret, repr(settings))

        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_WORKER_DOCUMENT_CONTENT_REVISIONS_ENABLED"] = "true"
            with self.assertRaisesRegex(CaseAgentWorkerConfigurationBlocked, "require document delivery"):
                CaseAgentWorkerProcessSettings.from_environment(environment)

        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment[
                "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED"
            ] = "true"
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "renderer configuration"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment[
                "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED"
            ] = "yes"
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "must be true"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            self._enable_document_delivery(environment)
            environment["LAWCASE_AGENT_WORKER_DOCUMENT_SOFFICE_EXECUTABLE"] = "/usr/bin/soffice"
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "isolated renderer"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_enabled_document_delivery_requires_verified_official_source_access(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            environment = self._environment(root)
            self._enable_document_delivery(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
            repository = type(
                "Repository",
                (),
                {"read_atomic_projection": lambda self, **_: None},
            )()
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked,
                "verified official-source access",
            ):
                compose_production_case_agent_worker(
                    settings=settings,
                    planning_repository=repository,
                    object_store_factory=lambda _config: object(),
                )

    def test_enabled_document_delivery_wires_recoverable_document_chain(self) -> None:
        self._assert_document_chain(content_enabled=False)

    def test_content_revisions_join_existing_process_only_when_enabled(self) -> None:
        self._assert_document_chain(content_enabled=True)

    def _assert_document_chain(self, *, content_enabled: bool) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            environment = self._environment(root)
            self._enable_document_delivery(environment)
            environment["LAWCASE_AGENT_WORKER_DOCUMENT_CONTENT_REVISIONS_ENABLED"] = str(content_enabled).lower()
            settings = CaseAgentWorkerProcessSettings.from_environment(
                environment
            )
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        official_text = type(
            "OfficialText",
            (),
            {"read_verified_source_text": lambda self, **_: "法源原文"},
        )()
        values = {
            name: object()
            for name in (
                "object_store",
                "provider",
                "raw_transport",
                "response_store",
                "exchange",
                "binding",
                "converter",
                "staging",
                "access",
                "revision_worker",
                "runtime",
            )
        }
        values["converter"] = Mock()
        values["verified_package_access"] = object()
        values["verified_package"] = object()
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
                "DeepSeekDocumentDraftProvider",
                return_value=values["provider"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DeepSeekDocumentRawHttpsTransport",
                return_value=values["raw_transport"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "S3DocumentDraftRawResponseStore",
                return_value=values["response_store"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_document_exchange_runtime_contract",
            ) as preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "preflight_case_agent_document_delivery_runtime_contract",
            ) as delivery_preflight,
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresRecoverableDocumentDraftExchange",
                return_value=values["exchange"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresDynamicDocumentBindingPort",
                return_value=values["binding"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresManagedArtifactAccessPort",
                return_value=values["verified_package_access"],
            ) as managed_access_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresVerifiedLawyerDecisionPackagePort",
                return_value=values["verified_package"],
            ) as verified_package_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "IsolatedDocumentRendererClient",
                return_value=values["converter"],
            ) as renderer_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresReviewableDocumentPackageStore",
                return_value=values["staging"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresReviewableDocumentPackageAccessPort",
                return_value=values["access"],
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresDocumentRevisionWorker",
                return_value=values["revision_worker"],
            ) as revision_worker_factory,
            patch.object(case_agent_worker_entrypoint, "preflight_content_revision_runtime") as content_preflight,
            patch.object(case_agent_worker_entrypoint, "PostgresContentRevisionWorker") as content_factory,
            patch.object(case_agent_worker_entrypoint, "DocumentRevisionWorkerGroup") as group_factory,
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
                return_value=values["runtime"],
            ) as compose,
        ):
            result = compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                document_official_source_text=official_text,
                object_store_factory=lambda _config: values["object_store"],
            )
        self.assertIs(result, values["runtime"])
        managed_access_factory.assert_called_once_with(
            dsn=settings.runtime.verifier_postgres_dsn,
            verifier_actor=settings.runtime.verifier_actor,
            execution_actor_id=settings.runtime.actor.actor_id,
            object_store=values["object_store"],
        )
        verified_package_factory.assert_called_once_with(
            dsn=settings.runtime.verifier_postgres_dsn,
            verifier_actor=settings.runtime.verifier_actor,
            execution_actor_id=settings.runtime.actor.actor_id,
            artifact_access=values["verified_package_access"],
        )
        renderer_factory.assert_called_once_with(
            settings=settings.document_renderer_settings
        )
        values["converter"].preflight.assert_called_once_with()
        delivery_preflight.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            verifier_actor=settings.runtime.verifier_actor,
            object_store=values["object_store"],
        )
        preflight.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=values["raw_transport"],
            response_store=values["response_store"],
        )
        kwargs = compose.call_args.kwargs
        self.assertIs(kwargs["document_binding"], values["binding"])
        self.assertIs(kwargs["document_provider"], values["provider"])
        self.assertIs(kwargs["document_exchange"], values["exchange"])
        self.assertIs(kwargs["document_converter"], values["converter"])
        self.assertIs(kwargs["document_staging"], values["staging"])
        self.assertIs(kwargs["document_artifact_access"], values["access"])
        if content_enabled:
            content_preflight.assert_called_once_with(
                execution_dsn=settings.runtime.postgres_dsn,
                verifier_dsn=settings.runtime.verifier_postgres_dsn,
                worker_actor=settings.runtime.actor, verifier_actor=settings.runtime.verifier_actor,
            )
            self.assertIs(content_factory.call_args.kwargs["package_store"], values["staging"])
            self.assertIs(content_factory.call_args.kwargs["converter"], values["converter"])
            group_factory.assert_called_once_with(values["revision_worker"], content_factory.return_value)
            self.assertIs(kwargs["document_revision_worker"], group_factory.return_value)
        else:
            content_preflight.assert_not_called()
            content_factory.assert_not_called()
            group_factory.assert_not_called()
            self.assertIs(kwargs["document_revision_worker"], values["revision_worker"])
        revision_worker_factory.assert_called_once_with(
            execution_dsn=settings.runtime.postgres_dsn,
            verifier_dsn=settings.runtime.verifier_postgres_dsn,
            worker_actor=settings.runtime.actor,
            verifier_actor=settings.runtime.verifier_actor,
            binding=values["binding"],
            converter=values["converter"],
            package_store=values["staging"],
            package_access=values["access"],
        )

    def test_renderer_preflight_failure_blocks_before_worker_readiness_is_composed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            self._enable_document_delivery(environment)
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        repository = type(
            "Repository",
            (),
            {"read_atomic_projection": lambda self, **_: None},
        )()
        official_text = type(
            "OfficialText",
            (),
            {"read_verified_source_text": lambda self, **_: "法源原文"},
        )()
        converter = Mock()
        converter.preflight.side_effect = TimeoutError("renderer unavailable")
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
                "IsolatedDocumentRendererClient",
                return_value=converter,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
            ) as compose,
        ):
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked,
                "renderer is unavailable",
            ):
                compose_production_case_agent_worker(
                    settings=settings,
                    planning_repository=repository,
                    document_official_source_text=official_text,
                    object_store_factory=lambda _config: object(),
                )
        converter.preflight.assert_called_once_with()
        compose.assert_not_called()

    def test_production_composition_always_wires_dynamic_memory_but_omits_search_without_key(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = CaseAgentWorkerProcessSettings.from_environment(
                self._environment(Path(temporary))
            )
        repository, object_store, memory_store, request_factory, runtime = (
            type("Repository", (), {"read_atomic_projection": lambda self, **_: None})(),
            object(), object(), object(), object()
        )
        with (
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresPlanningMemoryEnrichmentStore",
                return_value=memory_store,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "DynamicPlanningMemorySearchRequestFactory",
                return_value=request_factory,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
                return_value=runtime,
            ) as compose,
        ):
            result = compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                object_store_factory=lambda _config: object_store,
            )
        self.assertIs(result, runtime)
        kwargs = compose.call_args.kwargs
        self.assertIs(kwargs["planning_memory_enrichment"], memory_store)
        self.assertIs(kwargs["planning_memory_request_factory"], request_factory)
        self.assertIsNone(kwargs["public_research_binding"])
        self.assertIsNone(kwargs["public_research_provider"])
        self.assertIsNone(kwargs["public_research_exchange"])
        self.assertIsNone(kwargs["visual_ocr_binding"])
        self.assertIsNone(kwargs["visual_ocr_exchange"])
        self.assertIsNone(kwargs["visual_ocr_workspace_id"])
        self.assertIsNone(kwargs["lawyer_analysis_exchange"])

    def test_production_composition_wires_lawyer_analysis_only_with_pair(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment.update(
                {
                    "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": "q" * 40,
                    "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID": (
                        "ws-lawyer-prod"
                    ),
                }
            )
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        repository = type(
            "Repository", (), {"read_atomic_projection": lambda self, **_: None}
        )()
        object_store, exchange, runtime = object(), object(), object()
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
                "PostgresBoundRecoverableLawyerAnalysisExchange",
                return_value=exchange,
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
                object_store_factory=lambda _config: object_store,
            )
        self.assertIs(result, runtime)
        exchange_factory.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            credentials=settings.lawyer_analysis_credentials,
            object_store=object_store,
        )
        self.assertIs(
            compose.call_args.kwargs["lawyer_analysis_exchange"], exchange
        )

    def test_production_composition_wires_all_search_ports_only_with_server_key(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY"] = "b" * 40
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
        repository, object_store, binding, provider, exchange, runtime = (
            type("Repository", (), {"read_atomic_projection": lambda self, **_: None})(),
            object(), object(), object(), object(), object()
        )
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
                "PostgresPublicResearchBindingPort",
                return_value=binding,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "BravePublicSearchProvider",
                return_value=provider,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PostgresDurablePublicSearchExchange",
                return_value=exchange,
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "PinnedBraveHttpsTransport",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_entrypoint,
                "compose_case_agent_worker_from_repository",
                return_value=runtime,
            ) as compose,
        ):
            result = compose_production_case_agent_worker(
                settings=settings,
                planning_repository=repository,
                object_store_factory=lambda _config: object_store,
            )
        self.assertIs(result, runtime)
        kwargs = compose.call_args.kwargs
        self.assertIs(kwargs["public_research_binding"], binding)
        self.assertIs(kwargs["public_research_provider"], provider)
        self.assertIs(kwargs["public_research_exchange"], exchange)

    def test_production_composition_wires_qwen_only_with_server_configuration(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            renderer = root / "pdftoppm"
            renderer.touch()
            renderer.chmod(0o700)
            environment = self._environment(root)
            environment.update(
                {
                    "LAWCASE_AGENT_WORKER_QWEN_API_KEY": "q" * 40,
                    "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID": "ws-legal-prod",
                    "LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE": str(renderer),
                }
            )
            settings = CaseAgentWorkerProcessSettings.from_environment(environment)
            repository = type(
                "Repository", (), {"read_atomic_projection": lambda self, **_: None}
            )()
            object_store, binding, durable_broker, transport, exchange, runtime = (
                object(), object(), object(), object(), object(), object()
            )
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
                    "PostgresVisualOcrBindingPort",
                    return_value=binding,
                ) as binding_factory,
                patch.object(
                    case_agent_worker_entrypoint,
                    "PinnedQwenVisualOcrHttpsBroker",
                    return_value=transport,
                ),
                patch.object(
                    case_agent_worker_entrypoint,
                    "PostgresDurableQwenVisualOcrBroker",
                    return_value=durable_broker,
                ) as durable_factory,
                patch.object(
                    case_agent_worker_entrypoint,
                    "QwenVisualOcrRecoverableExchange",
                    return_value=exchange,
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
                    object_store_factory=lambda _config: object_store,
                )
        self.assertIs(result, runtime)
        binding_factory.assert_called_once()
        self.assertEqual(
            binding_factory.call_args.kwargs["workspace_id"], "ws-legal-prod"
        )
        durable_factory.assert_called_once_with(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=transport,
        )
        exchange_factory.assert_called_once_with(
            credentials=settings.qwen_credentials,
            egress=durable_broker,
            recovery=durable_broker,
        )
        kwargs = compose.call_args.kwargs
        self.assertIs(kwargs["visual_ocr_binding"], binding)
        self.assertIs(kwargs["visual_ocr_exchange"], exchange)
        self.assertEqual(kwargs["visual_ocr_workspace_id"], "ws-legal-prod")

    def test_missing_key_or_non_tls_infrastructure_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment.pop("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY")
            with self.assertRaises(CaseAgentWorkerConfigurationBlocked):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_verifier_must_have_a_distinct_actor_and_database_principal(self) -> None:
        with TemporaryDirectory() as temporary:
            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_VERIFIER_ACTOR_ID"] = environment[
                "LAWCASE_AGENT_WORKER_ACTOR_ID"
            ]
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "differ"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_VERIFIER_DATABASE_ROLE"] = environment[
                "LAWCASE_AGENT_WORKER_DATABASE_ROLE"
            ]
            with self.assertRaisesRegex(
                CaseAgentWorkerConfigurationBlocked, "role"
            ):
                CaseAgentWorkerProcessSettings.from_environment(environment)

            environment = self._environment(Path(temporary))
            environment["LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT"] = "http://objects.internal"
            with self.assertRaisesRegex(CaseAgentWorkerConfigurationBlocked, "HTTPS"):
                CaseAgentWorkerProcessSettings.from_environment(environment)

    def test_process_refuses_to_start_without_atomic_repository(self) -> None:
        with self.assertRaisesRegex(
            CaseAgentWorkerConfigurationBlocked, "repository is not installed"
        ):
            run_production_case_agent_worker(planning_repository=None, environ={})


if __name__ == "__main__":
    unittest.main()
