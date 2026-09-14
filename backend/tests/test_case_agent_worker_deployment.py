from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from case_api import case_agent_worker_deployment


class CaseAgentWorkerDeploymentTests(unittest.TestCase):
    def test_entrypoint_builds_atomic_repository_from_execution_worker_dsn(self) -> None:
        settings = SimpleNamespace(
            runtime=SimpleNamespace(
                postgres_dsn="postgresql://execution/lawcase",
                verifier_postgres_dsn="postgresql://verifier/lawcase",
                actor=SimpleNamespace(
                    firm_id="11111111-1111-4111-8111-111111111111",
                    actor_id="22222222-2222-4222-8222-222222222222",
                ),
                verifier_actor=SimpleNamespace(
                    actor_id="33333333-3333-4333-8333-333333333333"
                ),
            )
        )
        repository = object()
        memory = object()
        incidents = object()
        runtime = object()
        with (
            patch.object(
                case_agent_worker_deployment.CaseAgentWorkerProcessSettings,
                "from_environment",
                return_value=settings,
            ) as parse,
            patch.object(
                case_agent_worker_deployment,
                "PostgresCasePlanningProjectionRepository",
                return_value=repository,
            ) as repository_factory,
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_runtime_contract",
            ) as preflight,
            patch.object(
                case_agent_worker_deployment,
                "PostgresCaseAgentMemoryStore",
                return_value=memory,
            ) as memory_factory,
            patch.object(
                case_agent_worker_deployment,
                "PostgresCaseAgentRunnerIncidentSink",
                return_value=incidents,
            ) as incident_factory,
            patch.object(
                case_agent_worker_deployment,
                "compose_production_case_agent_worker",
                return_value=runtime,
            ) as compose,
            patch.object(
                case_agent_worker_deployment, "run_composed_worker"
            ) as run,
        ):
            case_agent_worker_deployment.main()
        parse.assert_called_once()
        preflight.assert_called_once_with(
            execution_dsn="postgresql://execution/lawcase",
            verifier_dsn="postgresql://verifier/lawcase",
            firm_id="11111111-1111-4111-8111-111111111111",
            execution_actor_id="22222222-2222-4222-8222-222222222222",
            verifier_actor_id="33333333-3333-4333-8333-333333333333",
        )
        repository_factory.assert_called_once_with(
            "postgresql://execution/lawcase"
        )
        memory_factory.assert_called_once_with("postgresql://execution/lawcase")
        incident_factory.assert_called_once_with(
            dsn="postgresql://execution/lawcase",
            actor=settings.runtime.actor,
        )
        compose.assert_called_once_with(
            settings=settings,
            planning_repository=repository,
            memory_checkpoint=memory,
            incident_sink=incidents,
            document_official_source_text=None,
        )
        run.assert_called_once_with(runtime)

    def test_preflight_failure_stops_before_repository_composition_or_heartbeat(self) -> None:
        settings = SimpleNamespace(
            runtime=SimpleNamespace(
                postgres_dsn="postgresql://execution/lawcase",
                verifier_postgres_dsn="postgresql://verifier/lawcase",
                actor=SimpleNamespace(
                    firm_id="11111111-1111-4111-8111-111111111111",
                    actor_id="22222222-2222-4222-8222-222222222222",
                ),
                verifier_actor=SimpleNamespace(
                    actor_id="33333333-3333-4333-8333-333333333333"
                ),
            )
        )
        with (
            patch.object(
                case_agent_worker_deployment.CaseAgentWorkerProcessSettings,
                "from_environment",
                return_value=settings,
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_runtime_contract",
                side_effect=RuntimeError("0035 missing"),
            ),
            patch.object(
                case_agent_worker_deployment,
                "PostgresCasePlanningProjectionRepository",
            ) as repository_factory,
            patch.object(
                case_agent_worker_deployment,
                "compose_production_case_agent_worker",
            ) as compose,
            patch.object(
                case_agent_worker_deployment, "run_composed_worker"
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "0035"):
                case_agent_worker_deployment.main()
        repository_factory.assert_not_called()
        compose.assert_not_called()
        run.assert_not_called()

    def test_document_delivery_receives_matter_bound_official_source_reader(self) -> None:
        official_text = object()
        settings = SimpleNamespace(
            document_delivery_enabled=True,
            object_store=object(),
            brave_credentials=None,
            qwen_credentials=None,
            runtime=SimpleNamespace(
                postgres_dsn="postgresql://execution/lawcase",
                verifier_postgres_dsn="postgresql://verifier/lawcase",
                actor=SimpleNamespace(
                    firm_id="11111111-1111-4111-8111-111111111111",
                    actor_id="22222222-2222-4222-8222-222222222222",
                ),
                verifier_actor=SimpleNamespace(
                    actor_id="33333333-3333-4333-8333-333333333333"
                ),
            ),
        )
        with (
            patch.object(
                case_agent_worker_deployment.CaseAgentWorkerProcessSettings,
                "from_environment",
                return_value=settings,
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_runtime_contract",
            ),
            patch.object(
                case_agent_worker_deployment,
                "PostgresCasePlanningProjectionRepository",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_deployment,
                "PostgresCaseAgentMemoryStore",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_deployment,
                "PostgresCaseAgentRunnerIncidentSink",
                return_value=object(),
            ),
            patch.object(
                case_agent_worker_deployment,
                "compose_official_source_s3_adapters",
                return_value=SimpleNamespace(verified_text=official_text),
            ) as official_factory,
            patch.object(
                case_agent_worker_deployment,
                "compose_production_case_agent_worker",
                return_value=object(),
            ) as compose,
            patch.object(case_agent_worker_deployment, "run_composed_worker"),
        ):
            case_agent_worker_deployment.main()
        official_factory.assert_called_once_with(settings.object_store)
        self.assertIs(
            compose.call_args.kwargs["document_official_source_text"], official_text
        )

    def test_enabled_public_search_requires_0037_preflight_before_composition(self) -> None:
        settings = SimpleNamespace(
            brave_credentials=object(),
            runtime=SimpleNamespace(
                postgres_dsn="postgresql://execution/lawcase",
                verifier_postgres_dsn="postgresql://verifier/lawcase",
                actor=SimpleNamespace(
                    firm_id="11111111-1111-4111-8111-111111111111",
                    actor_id="22222222-2222-4222-8222-222222222222",
                ),
                verifier_actor=SimpleNamespace(
                    actor_id="33333333-3333-4333-8333-333333333333"
                ),
            ),
        )
        with (
            patch.object(
                case_agent_worker_deployment.CaseAgentWorkerProcessSettings,
                "from_environment", return_value=settings,
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_runtime_contract",
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_research_runtime_contract",
                side_effect=RuntimeError("0037 missing"),
            ) as research_preflight,
            patch.object(
                case_agent_worker_deployment,
                "PostgresCasePlanningProjectionRepository",
            ) as repository_factory,
            patch.object(
                case_agent_worker_deployment,
                "compose_production_case_agent_worker",
            ) as compose,
            patch.object(
                case_agent_worker_deployment, "run_composed_worker"
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "0037"):
                case_agent_worker_deployment.main()
        research_preflight.assert_called_once_with(
            dsn="postgresql://execution/lawcase",
            worker_actor=settings.runtime.actor,
        )
        repository_factory.assert_not_called()
        compose.assert_not_called()
        run.assert_not_called()

    def test_enabled_visual_ocr_requires_0038_preflight_before_composition(self) -> None:
        settings = SimpleNamespace(
            qwen_credentials=object(),
            runtime=SimpleNamespace(
                postgres_dsn="postgresql://execution/lawcase",
                verifier_postgres_dsn="postgresql://verifier/lawcase",
                actor=SimpleNamespace(
                    firm_id="11111111-1111-4111-8111-111111111111",
                    actor_id="22222222-2222-4222-8222-222222222222",
                ),
                verifier_actor=SimpleNamespace(
                    actor_id="33333333-3333-4333-8333-333333333333"
                ),
            ),
        )
        with (
            patch.object(
                case_agent_worker_deployment.CaseAgentWorkerProcessSettings,
                "from_environment", return_value=settings,
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_runtime_contract",
            ),
            patch.object(
                case_agent_worker_deployment,
                "preflight_case_agent_visual_ocr_runtime_contract",
                side_effect=RuntimeError("0038 missing"),
            ) as visual_preflight,
            patch.object(
                case_agent_worker_deployment,
                "PostgresCasePlanningProjectionRepository",
            ) as repository_factory,
            patch.object(
                case_agent_worker_deployment,
                "compose_production_case_agent_worker",
            ) as compose,
            patch.object(
                case_agent_worker_deployment, "run_composed_worker"
            ) as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "0038"):
                case_agent_worker_deployment.main()
        visual_preflight.assert_called_once_with(
            dsn="postgresql://execution/lawcase",
            worker_actor=settings.runtime.actor,
        )
        repository_factory.assert_not_called()
        compose.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
