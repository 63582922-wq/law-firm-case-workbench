from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import stat
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from case_api.web_runtime import (
    WebRuntimeAssemblyAdapters,
    WebRuntimeConfigurationBlocked,
    WebRuntimeSettings,
    build_web_runtime_composition,
    create_web_runtime_app,
)
from case_kernel.models import Role
from case_kernel.official_source_private_store import (
    OfficialSourceS3ProductionAdapters,
    S3OfficialSourcePrivateObjectStore,
    S3VerifiedOfficialSourceTextPort,
)


class _FakeScanner:
    def scan(self, *args, **kwargs):  # pragma: no cover - composition never scans a browser body.
        del args, kwargs
        raise AssertionError("scanner must not run during runtime composition")


class _FakeOfficialSourceClient:
    def put_object(self, **kwargs):  # pragma: no cover - composition only.
        del kwargs
        raise AssertionError("official source store must not write during composition")

    def head_object(self, **kwargs):  # pragma: no cover - composition only.
        del kwargs
        raise AssertionError("official source store must not read during composition")

    def get_object(self, **kwargs):  # pragma: no cover - composition only.
        del kwargs
        raise AssertionError("official source store must not read during composition")


class _FakeObjectStore:
    def put_verified_pdf(self, *args, **kwargs):  # pragma: no cover - composition never stores a browser body.
        del args, kwargs
        raise AssertionError("object store must not run during runtime composition")

    def delete_unbound_upload_object(self, *args, **kwargs):  # pragma: no cover - as above.
        del args, kwargs
        raise AssertionError("object store must not run during runtime composition")

    def put_verified_zip(self, *args, **kwargs):  # pragma: no cover - archive route is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not run during runtime composition")

    def materialize_verified_pdf(self, *args, **kwargs):  # pragma: no cover - worker path is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not materialize during runtime composition")

    def put_verified_derivative(self, *args, **kwargs):  # pragma: no cover - worker path is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not store during runtime composition")

    def materialize_verified_derivative(self, *args, **kwargs):  # pragma: no cover - delivery path is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not materialize a derivative during runtime composition")

    def read_case_agent_review_candidate(self, *args, **kwargs):  # pragma: no cover - review route is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not read an Agent candidate during runtime composition")

    def verify_case_agent_review_candidate(self, *args, **kwargs):
        raise AssertionError("object store must not verify candidate bytes during runtime composition")

    def read_reviewable_document_object(self, *args, **kwargs):  # pragma: no cover - document route is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not read an Agent document during runtime composition")

    def put_reviewable_document_object(self, *args, **kwargs):  # pragma: no cover - document route is not invoked here.
        del args, kwargs
        raise AssertionError("object store must not store an Agent document during runtime composition")


class _FakeCommonMaterialObjectStore:
    def put_immutable_common_material(self, *args, **kwargs):  # pragma: no cover - composition only.
        del args, kwargs
        raise AssertionError("common material store must not write during composition")

    def recover_immutable_common_material(self, *args, **kwargs):  # pragma: no cover - composition only.
        del args, kwargs
        raise AssertionError("common material store must not recover during composition")

    def materialize_common_material(self, *args, **kwargs):  # pragma: no cover - composition only.
        del args, kwargs
        raise AssertionError("common material store must not materialize during composition")


class WebRuntimeTests(unittest.TestCase):
    def _environment(self, root: Path) -> tuple[dict[str, str], str, str]:
        firm_id = str(uuid4())
        worker_id = str(uuid4())
        verifier_id = str(uuid4())
        renderer = root / "pdftoppm"
        renderer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        renderer.chmod(0o700)
        return (
            {
                "LAWCASE_WEB_RUNTIME_MODE": "PRODUCTION_WEB",
                "LAWCASE_WEB_DEPLOYMENT_TOPOLOGY": "SINGLE_API_PROCESS",
                "LAWCASE_WEB_OIDC_STATE_STORE": "IN_PROCESS_EPHEMERAL",
                "LAWCASE_WEB_PUBLIC_ORIGIN": "https://workbench.lawfirm.cn",
                "LAWCASE_WEB_OIDC_ISSUER": "https://id.lawfirm.cn/oidc",
                "LAWCASE_WEB_OIDC_AUTHORIZATION_ENDPOINT": "https://id.lawfirm.cn/oidc/authorize",
                "LAWCASE_WEB_OIDC_TOKEN_ENDPOINT": "https://id.lawfirm.cn/oidc/token",
                "LAWCASE_WEB_OIDC_JWKS_URL": "https://id.lawfirm.cn/oidc/keys",
                "LAWCASE_WEB_OIDC_CLIENT_ID": "lawcase-web-workbench",
                "LAWCASE_WEB_OIDC_CLIENT_SECRET": "oidc-client-secret-value-1234567890",
                "LAWCASE_WEB_OIDC_AUDIENCE": "lawcase-web-production",
                "LAWCASE_WEB_OIDC_SCOPES": "openid,profile",
                "LAWCASE_WEB_OIDC_REQUIRED_AMR": "mfa,webauthn",
                "LAWCASE_WEB_OIDC_ACCEPTED_ACR": "",
                "LAWCASE_WEB_APP_DATABASE_ROLE": "lawcase_web_application",
                "LAWCASE_WEB_APP_POSTGRES_DSN": (
                    "postgresql://lawcase_web_application:application-password-123456789@"
                    "postgres.lawfirm.internal:5432/lawcase_production?sslmode=verify-full"
                ),
                "LAWCASE_WEB_IDENTITY_DIRECTORY_DATABASE_ROLE": "lawcase_identity_directory",
                "LAWCASE_WEB_IDENTITY_DIRECTORY_POSTGRES_DSN": (
                    "postgresql://lawcase_identity_directory:identity-password-123456789@"
                    "postgres.lawfirm.internal:5432/lawcase_production?sslmode=verify-full"
                ),
                "LAWCASE_WEB_SESSION_GATEWAY_DATABASE_ROLE": "lawcase_web_session_gateway",
                "LAWCASE_WEB_SESSION_GATEWAY_POSTGRES_DSN": (
                    "postgresql://lawcase_web_session_gateway:session-password-123456789@"
                    "postgres.lawfirm.internal:5432/lawcase_production?sslmode=verify-full"
                ),
                "LAWCASE_WEB_OBJECT_STORE_ENDPOINT": "https://objects.lawfirm.cn",
                "LAWCASE_WEB_OBJECT_STORE_REGION": "cn-south-1",
                "LAWCASE_WEB_OBJECT_STORE_BUCKET": "lawcase-private-evidence",
                "LAWCASE_WEB_OBJECT_STORE_ACCESS_KEY_ID": "LAWCASEOBJECTACCESS001",
                "LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY": "object-store-secret-value-1234567890",
                "LAWCASE_WEB_OBJECT_STORE_ENCRYPTION": "aws:kms",
                "LAWCASE_WEB_OBJECT_STORE_KMS_KEY_ID": "alias/lawcase-web-production",
                "LAWCASE_WEB_CLAMAV_EXECUTABLE": "/opt/lawcase/bin/clamdscan",
                "LAWCASE_WEB_CLAMAV_TIMEOUT_SECONDS": "120",
                "LAWCASE_WEB_PDFTOPPM_EXECUTABLE": str(renderer.resolve()),
                "LAWCASE_WEB_PDFTOPPM_TIMEOUT_SECONDS": "30",
                "LAWCASE_WEB_UPLOAD_STAGING_ROOT": str(root / "staging"),
                "LAWCASE_WEB_WORKER_MATERIALIZATION_ROOT": str(root / "worker-materialization"),
                "LAWCASE_WEB_SYSTEM_WORKERS_JSON": json.dumps({firm_id: worker_id}),
                "LAWCASE_WEB_SYSTEM_VERIFIERS_JSON": json.dumps(
                    {firm_id: verifier_id}
                ),
            },
            firm_id,
            worker_id,
        )

    @staticmethod
    def _adapters(*, calls: list[tuple[str, object]] | None = None) -> WebRuntimeAssemblyAdapters:
        events = calls if calls is not None else []

        def object_store_factory(config):
            events.append(("object_store", config))
            return _FakeObjectStore()

        def official_source_factory(config):
            events.append(("official_sources", config))
            objects = S3OfficialSourcePrivateObjectStore(
                config, client=_FakeOfficialSourceClient()
            )
            return OfficialSourceS3ProductionAdapters(
                objects=objects,
                verified_text=S3VerifiedOfficialSourceTextPort(objects=objects),
            )

        def common_material_object_store_factory(config):
            events.append(("common_material_object_store", config))
            return _FakeCommonMaterialObjectStore()

        def preflight(executable: Path, timeout_seconds: int) -> None:
            events.append(("clamav_preflight", (executable, timeout_seconds)))

        def scanner_factory(executable: Path, timeout_seconds: int):
            events.append(("scanner", (executable, timeout_seconds)))
            return _FakeScanner()

        def renderer_preflight(executable: Path, timeout_seconds: int) -> None:
            events.append(("pdftoppm_preflight", (executable, timeout_seconds)))

        def ledger_confirmation_preflight(dsn: str) -> None:
            events.append(("ledger_confirmation_preflight", dsn))

        def ledger_exception_followup_preflight(
            dsn: str, firm_id: str
        ) -> None:
            events.append(
                ("ledger_exception_followup_preflight", (dsn, firm_id))
            )

        def matter_provisioning_preflight(dsn: str, workers, verifiers) -> None:
            events.append(
                ("matter_provisioning_preflight", (dsn, dict(workers), dict(verifiers)))
            )

        return WebRuntimeAssemblyAdapters(
            object_store_factory=object_store_factory,
            common_material_object_store_factory=common_material_object_store_factory,
            official_source_factory=official_source_factory,
            clamav_preflight=preflight,
            scanner_factory=scanner_factory,
            pdftoppm_preflight=renderer_preflight,
            ledger_confirmation_preflight=ledger_confirmation_preflight,
            ledger_exception_followup_preflight=(
                ledger_exception_followup_preflight
            ),
            matter_provisioning_preflight=matter_provisioning_preflight,
        )

    def test_parser_requires_explicit_single_process_production_settings_without_secret_repr(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, firm_id, worker_id = self._environment(Path(temporary))
            settings = WebRuntimeSettings.from_environment(environment)

        self.assertEqual(settings.public_origin, "https://workbench.lawfirm.cn")
        self.assertEqual(settings.oidc_required_amr, frozenset({"mfa", "webauthn"}))
        self.assertEqual(settings.system_worker_ids_by_firm[firm_id], worker_id)
        self.assertNotEqual(
            settings.system_verifier_ids_by_firm[firm_id], worker_id
        )
        self.assertNotIn(environment["LAWCASE_WEB_OIDC_CLIENT_SECRET"], repr(settings))
        self.assertNotIn(environment["LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY"], repr(settings))
        self.assertNotIn(environment["LAWCASE_WEB_APP_POSTGRES_DSN"], repr(settings))

    def test_parser_rejects_missing_placeholder_multinode_and_unsafe_transport(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _, _ = self._environment(root)
            secret = environment["LAWCASE_WEB_OIDC_CLIENT_SECRET"]
            environment.pop("LAWCASE_WEB_OIDC_CLIENT_SECRET")
            with self.assertRaises(WebRuntimeConfigurationBlocked) as missing:
                WebRuntimeSettings.from_environment(environment)
            self.assertNotIn(secret, str(missing.exception))

            environment, _, _ = self._environment(root)
            environment["LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY"] = "REPLACE_WITH_A_SECRET"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "OBJECT_STORE_SECRET_ACCESS_KEY"):
                WebRuntimeSettings.from_environment(environment)

            environment, _, _ = self._environment(root)
            environment["LAWCASE_WEB_DEPLOYMENT_TOPOLOGY"] = "MULTI_API_PROCESS"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "one API process"):
                WebRuntimeSettings.from_environment(environment)

            environment, _, _ = self._environment(root)
            environment["UVICORN_WORKERS"] = "2"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "exactly one API process"):
                WebRuntimeSettings.from_environment(environment)

            environment, _, _ = self._environment(root)
            environment["LAWCASE_WEB_PUBLIC_ORIGIN"] = "http://workbench.lawfirm.cn"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "HTTPS"):
                WebRuntimeSettings.from_environment(environment)

            environment, _, _ = self._environment(root)
            environment["LAWCASE_WEB_OBJECT_STORE_ENDPOINT"] = "http://objects.lawfirm.cn"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "object-store"):
                WebRuntimeSettings.from_environment(environment)

    def test_parser_rejects_shared_roles_and_invalid_system_worker_mapping(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, _, _ = self._environment(Path(temporary))
            environment["LAWCASE_WEB_SESSION_GATEWAY_DATABASE_ROLE"] = "lawcase_identity_directory"
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "roles must be distinct"):
                WebRuntimeSettings.from_environment(environment)

            environment, firm_id, worker_id = self._environment(Path(temporary))
            environment["LAWCASE_WEB_SYSTEM_WORKERS_JSON"] = (
                "{"
                f"\"{firm_id}\":\"{worker_id}\","
                f"\"{firm_id}\":\"{worker_id}\""
                "}"
            )
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "system-worker mapping"):
                WebRuntimeSettings.from_environment(environment)

            environment, firm_id, worker_id = self._environment(Path(temporary))
            environment["LAWCASE_WEB_SYSTEM_VERIFIERS_JSON"] = json.dumps(
                {firm_id: worker_id}
            )
            with self.assertRaisesRegex(
                WebRuntimeConfigurationBlocked, "execution/verifier"
            ):
                WebRuntimeSettings.from_environment(environment)

    def test_document_draft_generation_requires_authenticated_isolated_renderer(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, _, _ = self._environment(Path(temporary))
            secret = urlsafe_b64encode(
                b"web-document-renderer-test-secret-at-least-32-bytes"
            ).decode("ascii").rstrip("=")
            environment.update(
                {
                    "LAWCASE_WEB_DOCUMENT_WORKER_ENABLED": "true",
                    "LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT": "http://document-renderer:8090",
                    "LAWCASE_WEB_DOCUMENT_RENDERER_SHARED_SECRET": secret,
                    "LAWCASE_WEB_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                }
            )
            settings = WebRuntimeSettings.from_environment(environment)

            self.assertTrue(settings.document_worker_enabled)
            self.assertEqual(
                settings.document_renderer_settings.endpoint,
                "http://document-renderer:8090/internal/v1/render-office",
            )
            self.assertNotIn(secret, repr(settings))

            environment["LAWCASE_WEB_SOFFICE_EXECUTABLE"] = "/usr/bin/soffice"
            with self.assertRaisesRegex(
                WebRuntimeConfigurationBlocked, "isolated document renderer"
            ):
                WebRuntimeSettings.from_environment(environment)

    def test_composition_builds_real_oidc_session_upload_graph_before_routes_and_factory_mounts_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, firm_id, worker_id = self._environment(root)
            settings = WebRuntimeSettings.from_environment(environment)
            calls: list[tuple[str, object]] = []
            adapters = self._adapters(calls=calls)

            composition = build_web_runtime_composition(settings, adapters=adapters)
            dependencies = composition.api_dependencies
            self.assertIsNotNone(dependencies.fact_correction_store)
            self.assertTrue(callable(dependencies.fact_correction_store.is_available))
            self.assertEqual(dependencies.matter_store._dsn, environment["LAWCASE_WEB_APP_POSTGRES_DSN"])
            self.assertEqual(
                dependencies.matter_store._system_workers[firm_id], worker_id
            )
            self.assertEqual(
                dependencies.matter_store._system_verifiers[firm_id],
                settings.system_verifier_ids_by_firm[firm_id],
            )
            self.assertEqual(composition.evidence_manifest_store._dsn, environment["LAWCASE_WEB_APP_POSTGRES_DSN"])
            self.assertIsNotNone(dependencies.case_agent_control_service)
            self.assertIsNotNone(
                dependencies.case_agent_document_review_service
            )
            self.assertIsNotNone(
                dependencies.agent_ledger_extraction_review_service
            )
            self.assertIsNotNone(dependencies.case_agent_runtime_ready)
            self.assertIsNotNone(
                dependencies.case_agent_ledger_runtime_ready
            )
            self.assertIsNotNone(
                dependencies.case_agent_document_runtime_ready
            )
            self.assertIs(
                dependencies.case_agent_ledger_runtime_ready.__self__,
                dependencies.case_agent_runtime_ready,
            )
            self.assertEqual(
                dependencies.case_agent_ledger_runtime_ready.__name__,
                "ledger_ready",
            )
            self.assertIs(
                dependencies.case_agent_document_runtime_ready.__self__,
                dependencies.case_agent_runtime_ready,
            )
            self.assertEqual(
                dependencies.case_agent_document_runtime_ready.__name__,
                "document_ready",
            )
            self.assertEqual(
                dependencies.agent_ledger_extraction_review_service._store._configured_firm_ids,
                frozenset({firm_id}),
            )
            self.assertEqual(
                dependencies.case_agent_control_service._store._dsn,
                environment["LAWCASE_WEB_APP_POSTGRES_DSN"],
            )
            self.assertEqual(
                dependencies.session_authority._store._dsn,
                environment["LAWCASE_WEB_SESSION_GATEWAY_POSTGRES_DSN"],
            )
            self.assertEqual(
                dependencies.session_authority._actor_directory._dsn,
                environment["LAWCASE_WEB_IDENTITY_DIRECTORY_POSTGRES_DSN"],
            )
            self.assertEqual(
                {event[0] for event in calls},
                {
                    "clamav_preflight",
                    "scanner",
                    "pdftoppm_preflight",
                    "ledger_confirmation_preflight",
                    "ledger_exception_followup_preflight",
                    "matter_provisioning_preflight",
                    "object_store",
                    "common_material_object_store",
                    "official_sources",
                },
            )
            self.assertIn(
                (
                    "ledger_confirmation_preflight",
                    environment["LAWCASE_WEB_APP_POSTGRES_DSN"],
                ),
                calls,
            )
            self.assertIn(
                (
                    "matter_provisioning_preflight",
                    (
                        environment["LAWCASE_WEB_APP_POSTGRES_DSN"],
                        {firm_id: worker_id},
                        {firm_id: settings.system_verifier_ids_by_firm[firm_id]},
                    ),
                ),
                calls,
            )
            self.assertIn(
                (
                    "ledger_exception_followup_preflight",
                    (
                        environment["LAWCASE_WEB_APP_POSTGRES_DSN"],
                        firm_id,
                    ),
                ),
                calls,
            )
            self.assertEqual(stat.S_IMODE(composition.private_roots.staging_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(composition.private_roots.worker_materialization_root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(
                    (composition.private_roots.worker_materialization_root / "ledger-confirmation").stat().st_mode
                ),
                0o700,
            )
            worker = composition.system_worker_for_firm(firm_id)
            self.assertEqual(worker.actor_id, worker_id)
            self.assertEqual(worker.roles, frozenset({Role.SYSTEM_WORKER}))
            with self.assertRaises(PermissionError):
                composition.system_worker_for_firm(str(uuid4()))

            app = create_web_runtime_app(environment, adapters=adapters)
            client = TestClient(app)
            health = client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["mode"], "self-hosted-web")
            self.assertEqual(client.get("/api/v1/session").status_code, 401)

    def test_actual_startup_preflight_refuses_missing_clamav_before_mounting_routes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, _, _ = self._environment(root)
            environment["LAWCASE_WEB_CLAMAV_EXECUTABLE"] = str(root / "missing-clamav")
            settings = WebRuntimeSettings.from_environment(environment)
            adapters = WebRuntimeAssemblyAdapters(
                object_store_factory=lambda config: _FakeObjectStore(),
                common_material_object_store_factory=lambda config: _FakeCommonMaterialObjectStore(),
                scanner_factory=lambda executable, timeout_seconds: _FakeScanner(),
            )
            with self.assertRaisesRegex(WebRuntimeConfigurationBlocked, "ClamAV is unavailable"):
                build_web_runtime_composition(settings, adapters=adapters)

    def test_managed_web_startup_refuses_missing_0048_authority_boundary(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, _, _ = self._environment(Path(temporary))
            settings = WebRuntimeSettings.from_environment(environment)

            def reject_missing_authority(_dsn: str) -> None:
                raise RuntimeError("synthetic missing 0048 authority")

            adapters = replace(
                self._adapters(),
                ledger_confirmation_preflight=reject_missing_authority,
            )
            with self.assertRaisesRegex(
                WebRuntimeConfigurationBlocked,
                "components could not be assembled safely",
            ):
                build_web_runtime_composition(settings, adapters=adapters)

    def test_managed_web_startup_refuses_missing_0049_lifecycle_boundary(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, firm_id, _ = self._environment(Path(temporary))
            settings = WebRuntimeSettings.from_environment(environment)

            def reject_missing_lifecycle(dsn: str, candidate_firm_id: str) -> None:
                self.assertEqual(dsn, environment["LAWCASE_WEB_APP_POSTGRES_DSN"])
                self.assertEqual(candidate_firm_id, firm_id)
                raise RuntimeError("synthetic missing 0049 lifecycle")

            adapters = replace(
                self._adapters(),
                ledger_exception_followup_preflight=reject_missing_lifecycle,
            )
            with self.assertRaisesRegex(
                WebRuntimeConfigurationBlocked,
                "components could not be assembled safely",
            ):
                build_web_runtime_composition(settings, adapters=adapters)

    def test_managed_web_startup_refuses_missing_matter_provisioning_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            environment, firm_id, worker_id = self._environment(Path(temporary))
            settings = WebRuntimeSettings.from_environment(environment)

            def reject_missing_lock(dsn: str, workers, verifiers) -> None:
                self.assertEqual(dsn, environment["LAWCASE_WEB_APP_POSTGRES_DSN"])
                self.assertEqual(dict(workers), {firm_id: worker_id})
                self.assertEqual(
                    dict(verifiers),
                    {firm_id: settings.system_verifier_ids_by_firm[firm_id]},
                )
                raise RuntimeError("synthetic missing matter principal lock")

            adapters = replace(
                self._adapters(),
                matter_provisioning_preflight=reject_missing_lock,
            )
            with self.assertRaisesRegex(
                WebRuntimeConfigurationBlocked,
                "components could not be assembled safely",
            ):
                build_web_runtime_composition(settings, adapters=adapters)


if __name__ == "__main__":
    unittest.main()
