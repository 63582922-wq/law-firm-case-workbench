from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
import unittest

from case_kernel.case_agent_sandbox_policy import (
    EgressGrant,
    SandboxArgumentDefinition,
    SandboxCommandMaturity,
    SandboxCommandTemplate,
    SandboxNetworkMode,
    SandboxObjectInput,
    SandboxOutputKind,
    SandboxResourceBudget,
    SandboxTemplateRegistry,
)
from case_kernel.case_agent_sandbox_runtime import (
    ImageTrustReceipt,
    ProcessRunResult,
    RegisteredSandboxOutput,
    RootlessPodmanSandboxRuntime,
    SandboxOutputScanReceipt,
    SandboxRunState,
    SandboxRuntimeBlocked,
    SignedImageReference,
)
from case_kernel.case_agent_supervisor import AdapterExecutionMode, RuntimeAdapterManifest


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return sha256(value).hexdigest()


class FakeRunner:
    def __init__(self, image_digest: str) -> None:
        self.image_digest = image_digest
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.rootless = True
        self.run_exit_code: int | None = 0
        self.run_timed_out = False
        self.run_policy_killed = False
        self.cleanup_exit_code = 0
        self.output_bytes = b"derived evidence"

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        now = 1_000
        if argv[1:3] == ("info", "--format"):
            stdout = json.dumps({"host": {"security": {"rootless": self.rootless}}}).encode()
            return self._result(stdout=stdout)
        if argv[1:3] == ("image", "inspect"):
            reference = argv[3]
            stdout = json.dumps([{"Id": self.image_digest, "RepoDigests": [reference]}]).encode()
            return self._result(stdout=stdout)
        if argv[1] == "run":
            watch_directory = kwargs.get("watch_directory")
            if watch_directory is not None and self.run_exit_code == 0:
                (watch_directory / "result.pdf").write_bytes(self.output_bytes)
            return ProcessRunResult(
                exit_code=self.run_exit_code,
                timed_out=self.run_timed_out,
                policy_killed=self.run_policy_killed,
                stdout=b"bounded stdout",
                stderr=b"bounded stderr",
                stdout_truncated=False,
                stderr_truncated=False,
                started_epoch_seconds=now,
                finished_epoch_seconds=now + 1,
            )
        if argv[1:3] == ("rm", "--force"):
            return ProcessRunResult(
                exit_code=self.cleanup_exit_code,
                timed_out=False,
                policy_killed=False,
                stdout=b"",
                stderr=b"",
                stdout_truncated=False,
                stderr_truncated=False,
                started_epoch_seconds=now,
                finished_epoch_seconds=now + 1,
            )
        raise AssertionError(f"unexpected fake command: {argv!r}")

    def _result(self, *, stdout: bytes):
        return ProcessRunResult(
            exit_code=0,
            timed_out=False,
            policy_killed=False,
            stdout=stdout,
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            started_epoch_seconds=1_000,
            finished_epoch_seconds=1_001,
        )


class FakeMaterializer:
    def __init__(self, source_bytes: bytes) -> None:
        self.source_bytes = source_bytes
        self.destinations: list[Path] = []

    def materialize(self, source, destination):
        self.destinations.append(destination)
        destination.write_bytes(self.source_bytes)


class FakeVerifier:
    def __init__(self, receipt: ImageTrustReceipt) -> None:
        self.receipt = receipt
        self.inspections: list[object] = []

    def verify(self, *, image, inspection):
        self.inspections.append(inspection)
        return self.receipt


class FakeScanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.verdict = "CLEAN"

    def scan(self, *, source, content_sha256, byte_size):
        self.calls.append((content_sha256, byte_size))
        return SandboxOutputScanReceipt(
            content_sha256=content_sha256,
            engine="clamav",
            engine_version="1.4.0",
            verdict=self.verdict,
            scan_receipt_sha256=digest(f"scan:{content_sha256}"),
        )


class FakeRegistrar:
    def __init__(self) -> None:
        self.calls = []

    def register_batch(self, *, grant, files, allowed_kinds):
        self.calls.append((grant, files, allowed_kinds))
        return tuple(
            RegisteredSandboxOutput(
                object_id=str(uuid4()),
                object_version="1",
                content_sha256=item.content_sha256,
                byte_size=item.byte_size,
                output_kind=allowed_kinds[0],
                relative_path=item.relative_path,
            )
            for item in files
        )


class SandboxRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source_bytes = b"source pdf"
        self.image_digest = f"sha256:{digest('sandbox-image')}"
        self.signature_policy_hash = digest("cosign-policy-v1")
        self.budget = SandboxResourceBudget(
            cpu_millis=1_500,
            memory_bytes=256 * 1024 * 1024,
            disk_bytes=64 * 1024 * 1024,
            max_processes=16,
            timeout_seconds=60,
            max_stdout_bytes=128 * 1024,
            max_stderr_bytes=128 * 1024,
        )
        self.template = SandboxCommandTemplate(
            template_id="PDF_RENDER_PAGE",
            version="1.0.0",
            image_digest=self.image_digest,
            executable_path="/usr/bin/render-page",
            fixed_arguments=("--page",),
            arguments=(SandboxArgumentDefinition("page", r"[1-9][0-9]{0,4}", True, 5),),
            maturity=SandboxCommandMaturity.ENABLED,
            network_mode=SandboxNetworkMode.DISABLED,
            output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,),
            default_budget=self.budget,
        )
        self.registry = SandboxTemplateRegistry((self.template,))
        self.manifest = RuntimeAdapterManifest(
            tool_id="render_pdf_page",
            adapter_id="rootless_podman_sandbox",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.ISOLATED_CONTAINER,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version=self.registry.policy_version,
            sandbox_policy_hash=self.registry.policy_hash,
        )
        self.image = SignedImageReference(
            reference=f"registry.lawfirm.invalid/case-tools/pdf@{self.image_digest}",
            image_digest=self.image_digest,
            signature_policy_hash=self.signature_policy_hash,
        )
        self.trust = ImageTrustReceipt(
            image_digest=self.image_digest,
            signature_policy_hash=self.signature_policy_hash,
            signer_identity="https://issuer.example/deployment/case-tools",
            verification_receipt_sha256=digest("verified image"),
        )
        self.runner = FakeRunner(self.image_digest)
        self.materializer = FakeMaterializer(self.source_bytes)
        self.scanner = FakeScanner()
        self.registrar = FakeRegistrar()
        self.workspace_root = Path(self.temp.name) / "runtime"
        self.runtime = RootlessPodmanSandboxRuntime(
            manifest=self.manifest,
            templates=(self.template,),
            signed_images={self.image_digest: self.image},
            process_runner=self.runner,
            image_verifier=FakeVerifier(self.trust),
            input_materializer=self.materializer,
            output_scanner=self.scanner,
            output_registrar=self.registrar,
            workspace_root=self.workspace_root,
            podman_binary=Path("/usr/bin/podman"),
            host_uid=10001,
            host_gid=10001,
            clock=lambda: 1_000,
        )
        self.ids = {name: str(uuid4()) for name in ("grant", "firm", "matter", "run", "task", "object")}
        self.source = SandboxObjectInput(
            object_id=self.ids["object"],
            object_version="4",
            content_sha256=digest(self.source_bytes),
            byte_size=len(self.source_bytes),
        )
        self.grant = self.registry.issue_grant(
            grant_id=self.ids["grant"],
            firm_id=self.ids["firm"],
            matter_id=self.ids["matter"],
            agent_run_id=self.ids["run"],
            task_id=self.ids["task"],
            template_id=self.template.template_id,
            arguments=(("page", "2"),),
            inputs=(self.source,),
            output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,),
            expires_epoch_seconds=2_000,
        )

    def test_exact_structured_command_runs_with_hard_container_boundaries(self) -> None:
        command = self.runtime.compile(self.grant)
        self.assertEqual(
            command.argv,
            (
                "/usr/bin/render-page",
                "--page",
                "2",
                f"/case/inputs/{self.ids['object']}",
                "/case/output",
            ),
        )
        result = self.runtime.execute(grant=self.grant, command=command)
        self.assertEqual(result.state, SandboxRunState.SUCCEEDED)
        self.assertEqual(result.receipt.output_hashes, (digest(self.runner.output_bytes),))
        self.assertEqual(len(result.outputs), 1)
        run_argv, run_kwargs = next(item for item in self.runner.calls if item[0][1] == "run")
        self.assertEqual(run_argv[0], "/usr/bin/podman")
        for boundary in (
            "--network=none",
            "--read-only",
            "--http-proxy=false",
            "--log-driver=none",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--ipc=private",
            "--pid=private",
            "--uts=private",
            "--userns=keep-id",
            "--user=10001:10001",
            "--pids-limit=16",
            f"--memory={self.budget.memory_bytes}",
            "--cpus=1.500",
            "--pull=never",
            "--rm",
        ):
            self.assertIn(boundary, run_argv)
        self.assertNotIn("/bin/sh", run_argv)
        self.assertNotIn("-c", run_argv)
        self.assertFalse(any("docker.sock" in value for value in run_argv))
        self.assertFalse(any(value.startswith("--env") for value in run_argv))
        self.assertLess(run_argv.index("--"), run_argv.index(self.image.reference))
        self.assertLess(run_argv.index(self.image.reference), run_argv.index("/usr/bin/render-page"))
        self.assertEqual(run_kwargs["watch_directory"].name, "output")
        self.assertEqual(run_kwargs["watch_directory"].parent.parent, self.workspace_root)
        self.assertEqual(list(self.workspace_root.iterdir()), [])
        self.assertEqual(len(self.scanner.calls), 1)
        self.assertEqual(len(self.registrar.calls), 1)

    def test_only_exact_compiled_command_is_accepted(self) -> None:
        command = self.runtime.compile(self.grant)
        tampered = replace(command, argv=("/bin/sh", "-c", "cat /etc/passwd"))
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "exact compiled"):
            self.runtime.execute(grant=self.grant, command=tampered)
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.runner.calls))

    def test_manifest_must_match_the_exact_policy_hash(self) -> None:
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "manifest"):
            RootlessPodmanSandboxRuntime(
                manifest=replace(self.manifest, sandbox_policy_hash=digest("different")),
                templates=(self.template,),
                signed_images={self.image_digest: self.image},
                process_runner=self.runner,
                image_verifier=FakeVerifier(self.trust),
                input_materializer=self.materializer,
                output_scanner=self.scanner,
                output_registrar=self.registrar,
                workspace_root=self.workspace_root,
                podman_binary=Path("/usr/bin/podman"),
                host_uid=10001,
                host_gid=10001,
            )

    def test_preflight_blocks_non_rootless_runtime_before_container_run(self) -> None:
        self.runner.rootless = False
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "not rootless"):
            self.runtime.execute(grant=self.grant, command=self.runtime.compile(self.grant))
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.runner.calls))

    def test_unknown_exit_is_a_conservative_failure_and_cleanup_is_confirmed(self) -> None:
        self.runner.run_exit_code = None
        result = self.runtime.execute(grant=self.grant, command=self.runtime.compile(self.grant))
        self.assertEqual(result.state, SandboxRunState.FAILED_UNKNOWN)
        self.assertEqual(result.receipt.exit_code, -1)
        self.assertEqual(result.outputs, ())
        self.assertTrue(any(argv[1:3] == ("rm", "--force") for argv, _ in self.runner.calls))
        self.assertEqual(list(self.workspace_root.iterdir()), [])

    def test_unconfirmed_container_cleanup_blocks_any_receipt(self) -> None:
        self.runner.cleanup_exit_code = 125
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "cleanup"):
            self.runtime.execute(grant=self.grant, command=self.runtime.compile(self.grant))
        self.assertEqual(self.registrar.calls, [])
        self.assertEqual(list(self.workspace_root.iterdir()), [])

    def test_runner_exception_is_not_mistaken_for_a_known_failure(self) -> None:
        original_run = self.runner.run

        def raising_run(argv, **kwargs):
            if argv[1] == "run":
                raise OSError("runtime connection lost")
            return original_run(argv, **kwargs)

        self.runner.run = raising_run  # type: ignore[method-assign]
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "trusted process result"):
            self.runtime.execute(grant=self.grant, command=self.runtime.compile(self.grant))
        self.assertTrue(any(argv[1:3] == ("rm", "--force") for argv, _ in self.runner.calls))
        self.assertEqual(self.registrar.calls, [])
        self.assertEqual(list(self.workspace_root.iterdir()), [])

    def test_output_scan_failure_prevents_registration(self) -> None:
        class RejectingScanner:
            def scan(self, **kwargs):
                raise SandboxRuntimeBlocked("malicious output")

        runtime = RootlessPodmanSandboxRuntime(
            manifest=self.manifest,
            templates=(self.template,),
            signed_images={self.image_digest: self.image},
            process_runner=self.runner,
            image_verifier=FakeVerifier(self.trust),
            input_materializer=self.materializer,
            output_scanner=RejectingScanner(),
            output_registrar=self.registrar,
            workspace_root=self.workspace_root,
            podman_binary=Path("/usr/bin/podman"),
            host_uid=10001,
            host_gid=10001,
            clock=lambda: 1_000,
        )
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "malicious"):
            runtime.execute(grant=self.grant, command=runtime.compile(self.grant))
        self.assertEqual(self.registrar.calls, [])
        self.assertEqual(list(self.workspace_root.iterdir()), [])

    def test_brokered_network_grant_is_not_mapped_to_direct_container_network(self) -> None:
        network_template = replace(
            self.template,
            template_id="OFFICIAL_SOURCE_FETCH",
            network_mode=SandboxNetworkMode.BROKERED_HTTPS,
            output_kinds=(SandboxOutputKind.STRUCTURED_RESULT,),
        )
        registry = SandboxTemplateRegistry((network_template,))
        manifest = replace(
            self.manifest,
            sandbox_policy_hash=registry.policy_hash,
            sandbox_policy_version=registry.policy_version,
        )
        runtime = RootlessPodmanSandboxRuntime(
            manifest=manifest,
            templates=(network_template,),
            signed_images={self.image_digest: self.image},
            process_runner=self.runner,
            image_verifier=FakeVerifier(self.trust),
            input_materializer=self.materializer,
            output_scanner=self.scanner,
            output_registrar=self.registrar,
            workspace_root=self.workspace_root,
            podman_binary=Path("/usr/bin/podman"),
            host_uid=10001,
            host_gid=10001,
            clock=lambda: 1_000,
        )
        egress = EgressGrant(
            grant_id=str(uuid4()),
            allowed_hosts=("flk.npc.gov.cn",),
            allowed_methods=("GET",),
            max_requests=2,
            max_response_bytes=10_000,
            expires_epoch_seconds=2_000,
            query_data_minimized=True,
        )
        grant = registry.issue_grant(
            grant_id=str(uuid4()),
            firm_id=self.ids["firm"],
            matter_id=self.ids["matter"],
            agent_run_id=self.ids["run"],
            task_id=self.ids["task"],
            template_id=network_template.template_id,
            arguments=(("page", "2"),),
            inputs=(self.source,),
            output_kinds=(SandboxOutputKind.STRUCTURED_RESULT,),
            expires_epoch_seconds=2_000,
            egress_grant=egress,
        )
        with self.assertRaisesRegex(SandboxRuntimeBlocked, "authorised broker"):
            runtime.execute(grant=grant, command=runtime.compile(grant))
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.runner.calls))


if __name__ == "__main__":
    unittest.main()
