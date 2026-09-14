"""Rootless Podman execution adapter for compiled case-Agent commands.

This module is the first concrete command runtime behind the sandbox policy.
It deliberately has no shell-string API.  A trusted server compiler binds an
exact policy grant to a registered command template, and the adapter derives
the complete container argv itself.  Managed inputs and registered outputs
are the only file boundary.

The adapter is usable only when all production dependencies are injected:

* a rootless Podman process runner;
* a deployment image-signature verifier;
* a managed-object materializer and output registrar; and
* a RuntimeAdapterManifest bound to the exact sandbox policy digest.

The container always has ``--network=none``.  Public web research is performed
by the separately authorised broker/connector plane; a command container never
receives direct network access, even when a policy grant describes brokered
HTTPS.  The latter is therefore rejected by this adapter until an audited
Unix-socket broker transport is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Callable, Mapping, Protocol
from uuid import UUID

from .case_agent_sandbox_policy import (
    SandboxCommandTemplate,
    SandboxExecutionReceipt,
    SandboxNetworkMode,
    SandboxObjectInput,
    SandboxOutputKind,
    SandboxTaskGrant,
    SandboxTemplateRegistry,
    verify_sandbox_receipt,
)
from .case_agent_supervisor import AdapterExecutionMode, RuntimeAdapterManifest


class SandboxRuntimeBlocked(RuntimeError):
    """The command could not be admitted or safely accounted for."""


class SandboxRunState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    POLICY_KILLED = "POLICY_KILLED"
    FAILED_UNKNOWN = "FAILED_UNKNOWN"


@dataclass(frozen=True)
class ProcessRunResult:
    """Bounded result returned by a no-shell process runner."""

    exit_code: int | None
    timed_out: bool
    policy_killed: bool
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    started_epoch_seconds: int
    finished_epoch_seconds: int

    def __post_init__(self) -> None:
        if self.exit_code is not None and not -255 <= self.exit_code <= 255:
            raise SandboxRuntimeBlocked("process exit code is outside the supported boundary")
        if self.started_epoch_seconds < 1 or self.finished_epoch_seconds < self.started_epoch_seconds:
            raise SandboxRuntimeBlocked("process timing is invalid")


class NoShellProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        watch_directory: Path | None = None,
        max_watch_bytes: int | None = None,
    ) -> ProcessRunResult:
        """Execute an argv vector directly.  Implementations must never use a shell."""


class ManagedInputMaterializer(Protocol):
    def materialize(self, source: SandboxObjectInput, destination: Path) -> None:
        """Write one authorised object version to the runtime-selected destination."""


@dataclass(frozen=True)
class RegisteredSandboxOutput:
    object_id: str
    object_version: str
    content_sha256: str
    byte_size: int
    output_kind: SandboxOutputKind
    relative_path: str

    def __post_init__(self) -> None:
        try:
            UUID(self.object_id)
        except (TypeError, ValueError) as error:
            raise SandboxRuntimeBlocked("registered output object_id must be a UUID") from error
        if (
            not isinstance(self.object_version, str)
            or self.object_version != self.object_version.strip()
            or not 1 <= len(self.object_version) <= 200
        ):
            raise SandboxRuntimeBlocked("registered output object version is invalid")
        _sha256(self.content_sha256, "registered output content hash")
        if not isinstance(self.byte_size, int) or not 0 <= self.byte_size <= 100 * 1024**3:
            raise SandboxRuntimeBlocked("registered output byte size is invalid")
        _relative_output_path(self.relative_path)


class ManagedOutputRegistrar(Protocol):
    def register_batch(
        self,
        *,
        grant: SandboxTaskGrant,
        files: tuple["VerifiedSandboxOutput", ...],
        allowed_kinds: tuple[SandboxOutputKind, ...],
    ) -> tuple[RegisteredSandboxOutput, ...]:
        """Atomically commit verified files and return immutable object receipts."""


@dataclass(frozen=True)
class SandboxOutputScanReceipt:
    content_sha256: str
    engine: str
    engine_version: str
    verdict: str
    scan_receipt_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.content_sha256, "output scan content hash")
        _sha256(self.scan_receipt_sha256, "output scan receipt")
        if self.verdict != "CLEAN":
            raise SandboxRuntimeBlocked("sandbox output scanner did not return CLEAN")
        for label, value in (("engine", self.engine), ("engine_version", self.engine_version)):
            if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 200:
                raise SandboxRuntimeBlocked(f"output scan {label} is invalid")


@dataclass(frozen=True)
class VerifiedSandboxOutput:
    source: Path
    relative_path: str
    content_sha256: str
    byte_size: int
    scan_receipt: SandboxOutputScanReceipt


class SandboxOutputScanner(Protocol):
    def scan(
        self,
        *,
        source: Path,
        content_sha256: str,
        byte_size: int,
    ) -> SandboxOutputScanReceipt:
        """Scan exactly the verified output bytes before managed registration."""


@dataclass(frozen=True)
class SignedImageReference:
    """Deployment-controlled image name pinned to the policy digest."""

    reference: str
    image_digest: str
    signature_policy_hash: str

    def __post_init__(self) -> None:
        _image_digest(self.image_digest)
        _sha256(self.signature_policy_hash, "image signature policy hash")
        if (
            not isinstance(self.reference, str)
            or self.reference != self.reference.strip()
            or len(self.reference) > 1_000
            or any(value in self.reference for value in ("\x00", "\n", "\r", "://", "@http"))
            or not self.reference.endswith(f"@{self.image_digest}")
            or re.fullmatch(
                r"[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9][a-z0-9._-]*)+@sha256:[0-9a-f]{64}",
                self.reference,
            ) is None
        ):
            raise SandboxRuntimeBlocked("image reference must be an exact digest-pinned registry reference")


@dataclass(frozen=True)
class ImageTrustReceipt:
    image_digest: str
    signature_policy_hash: str
    signer_identity: str
    verification_receipt_sha256: str

    def __post_init__(self) -> None:
        _image_digest(self.image_digest)
        _sha256(self.signature_policy_hash, "image signature policy hash")
        _sha256(self.verification_receipt_sha256, "image verification receipt")
        if (
            not isinstance(self.signer_identity, str)
            or self.signer_identity != self.signer_identity.strip()
            or not 1 <= len(self.signer_identity) <= 500
        ):
            raise SandboxRuntimeBlocked("image signer identity is invalid")


class ImageSignatureVerifier(Protocol):
    def verify(
        self,
        *,
        image: SignedImageReference,
        inspection: Mapping[str, object],
    ) -> ImageTrustReceipt:
        """Verify the deployment signature/attestation for the exact inspected digest."""


@dataclass(frozen=True)
class StructuredSandboxCommand:
    """Canonical argv compiled only from a registered template and exact grant."""

    grant_id: str
    grant_hash: str
    template_id: str
    template_version: str
    image_digest: str
    executable_path: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class SandboxStreamSummary:
    content_sha256: str
    captured_bytes: int
    truncated: bool


@dataclass(frozen=True)
class SandboxRunResult:
    state: SandboxRunState
    receipt: SandboxExecutionReceipt
    image_trust: ImageTrustReceipt
    stdout: SandboxStreamSummary
    stderr: SandboxStreamSummary
    outputs: tuple[RegisteredSandboxOutput, ...]


@dataclass(frozen=True)
class SandboxRuntimePreflight:
    runtime: str
    rootless: bool
    image_digest: str
    image_trust: ImageTrustReceipt


class SubprocessNoShellRunner:
    """Concrete bounded subprocess runner used by a worker process.

    Output is redirected to private files instead of accumulated through
    ``communicate``.  The process is killed when its deadline, stream limits,
    or watched output-directory limit is exceeded.  The argv is passed directly
    to ``Popen`` with ``shell=False``.
    """

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        watch_directory: Path | None = None,
        max_watch_bytes: int | None = None,
    ) -> ProcessRunResult:
        if not argv or any(not isinstance(value, str) or "\x00" in value for value in argv):
            raise SandboxRuntimeBlocked("process runner requires a valid argv vector")
        started = int(time.time())
        scratch = Path(tempfile.mkdtemp(prefix="case-agent-process-"))
        os.chmod(scratch, 0o700)
        stdout_path = scratch / "stdout.bin"
        stderr_path = scratch / "stderr.bin"
        timed_out = False
        policy_killed = False
        exit_code: int | None = None
        try:
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                try:
                    process = subprocess.Popen(  # noqa: S603 - argv is structured; shell is explicitly disabled.
                        list(argv),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        shell=False,
                        close_fds=True,
                    )
                except OSError as error:
                    raise SandboxRuntimeBlocked("sandbox runtime process could not start") from error
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                    stdout_size = _file_size(stdout_path)
                    stderr_size = _file_size(stderr_path)
                    watched_size = _directory_size(watch_directory) if watch_directory is not None else 0
                    if stdout_size > max_stdout_bytes or stderr_size > max_stderr_bytes:
                        policy_killed = True
                    if max_watch_bytes is not None and watched_size > max_watch_bytes:
                        policy_killed = True
                    if timed_out or policy_killed:
                        process.kill()
                        break
                    time.sleep(0.02)
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        exit_code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        exit_code = None
            stdout_size = _file_size(stdout_path)
            stderr_size = _file_size(stderr_path)
            final_watched_size = _directory_size(watch_directory) if watch_directory is not None else 0
            if (
                stdout_size > max_stdout_bytes
                or stderr_size > max_stderr_bytes
                or (max_watch_bytes is not None and final_watched_size > max_watch_bytes)
            ):
                policy_killed = True
            return ProcessRunResult(
                exit_code=exit_code,
                timed_out=timed_out,
                policy_killed=policy_killed,
                stdout=_read_prefix(stdout_path, max_stdout_bytes),
                stderr=_read_prefix(stderr_path, max_stderr_bytes),
                stdout_truncated=stdout_size > max_stdout_bytes,
                stderr_truncated=stderr_size > max_stderr_bytes,
                started_epoch_seconds=started,
                finished_epoch_seconds=max(started, int(time.time())),
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class RootlessPodmanSandboxRuntime:
    """Translate a policy-bound structured command into one rootless container."""

    ADAPTER_ID = "rootless_podman_sandbox"
    ADAPTER_VERSION = "1.0.0"
    _CONTAINER_INPUT_ROOT = "/case/inputs"
    _CONTAINER_OUTPUT_ROOT = "/case/output"

    def __init__(
        self,
        *,
        manifest: RuntimeAdapterManifest,
        templates: tuple[SandboxCommandTemplate, ...],
        signed_images: Mapping[str, SignedImageReference],
        process_runner: NoShellProcessRunner,
        image_verifier: ImageSignatureVerifier,
        input_materializer: ManagedInputMaterializer,
        output_scanner: SandboxOutputScanner,
        output_registrar: ManagedOutputRegistrar,
        workspace_root: Path,
        podman_binary: Path,
        host_uid: int | None = None,
        host_gid: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        manifest.validate()
        self._registry = SandboxTemplateRegistry(templates)
        self._templates = {item.template_id: item for item in templates}
        if (
            manifest.adapter_id != self.ADAPTER_ID
            or manifest.adapter_version != self.ADAPTER_VERSION
            or manifest.execution_mode is not AdapterExecutionMode.ISOLATED_CONTAINER
            or manifest.network_capable
            or manifest.sandbox_policy_version != self._registry.policy_version
            or manifest.sandbox_policy_hash != self._registry.policy_hash
        ):
            raise SandboxRuntimeBlocked("runtime manifest is not bound to this Podman sandbox policy")
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            raise SandboxRuntimeBlocked("sandbox workspace root must be an absolute server path")
        workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if workspace_root.is_symlink() or not workspace_root.is_dir():
            raise SandboxRuntimeBlocked("sandbox workspace root must be a private real directory")
        os.chmod(workspace_root, 0o700)
        if (
            not isinstance(podman_binary, Path)
            or not podman_binary.is_absolute()
            or podman_binary.name != "podman"
        ):
            raise SandboxRuntimeBlocked("Podman binary must be an absolute deployment-controlled path")
        self._manifest = manifest
        self._signed_images = dict(signed_images)
        self._runner = process_runner
        self._image_verifier = image_verifier
        self._input_materializer = input_materializer
        self._output_scanner = output_scanner
        self._output_registrar = output_registrar
        self._workspace_root = workspace_root
        self._podman_binary = str(podman_binary)
        self._uid = os.getuid() if host_uid is None else host_uid
        self._gid = os.getgid() if host_gid is None else host_gid
        if not isinstance(self._uid, int) or not isinstance(self._gid, int) or self._uid <= 0 or self._gid <= 0:
            raise SandboxRuntimeBlocked("rootless sandbox worker must run as a non-root host identity")
        self._clock = clock

    def compile(self, grant: SandboxTaskGrant) -> StructuredSandboxCommand:
        """Compile the sole accepted command shape from the registered template."""

        template = self._template_for(grant)
        _verify_grant_hash(grant)
        validated = self._registry.issue_grant(
            grant_id=grant.grant_id,
            firm_id=grant.firm_id,
            matter_id=grant.matter_id,
            agent_run_id=grant.agent_run_id,
            task_id=grant.task_id,
            template_id=grant.template_id,
            arguments=grant.arguments,
            inputs=grant.inputs,
            output_kinds=grant.output_kinds,
            expires_epoch_seconds=grant.expires_epoch_seconds,
            egress_grant=grant.egress_grant,
            budget=grant.budget,
        )
        if validated != grant:
            raise SandboxRuntimeBlocked("sandbox grant is not the canonical registry-issued grant")
        if sum(item.byte_size for item in grant.inputs) >= grant.budget.disk_bytes:
            raise SandboxRuntimeBlocked("sandbox inputs leave no bounded workspace for managed output")
        provided = dict(grant.arguments)
        argument_values = tuple(provided[item.name] for item in template.arguments if item.name in provided)
        input_paths = tuple(f"{self._CONTAINER_INPUT_ROOT}/{item.object_id}" for item in grant.inputs)
        argv = (
            template.executable_path,
            *template.fixed_arguments,
            *argument_values,
            *input_paths,
            self._CONTAINER_OUTPUT_ROOT,
        )
        return StructuredSandboxCommand(
            grant_id=grant.grant_id,
            grant_hash=grant.grant_hash,
            template_id=grant.template_id,
            template_version=grant.template_version,
            image_digest=grant.image_digest,
            executable_path=template.executable_path,
            argv=argv,
        )

    def preflight(self, command: StructuredSandboxCommand) -> SandboxRuntimePreflight:
        """Verify rootless runtime, local image identity and deployment signature."""

        image = self._image_for(command.image_digest)
        info_result = self._runner.run(
            (self._podman_binary, "info", "--format", "json"),
            timeout_seconds=20,
            max_stdout_bytes=1024 * 1024,
            max_stderr_bytes=64 * 1024,
        )
        info = _required_json_result(info_result, "Podman rootless preflight")
        if not _podman_rootless(info):
            raise SandboxRuntimeBlocked("Podman runtime is not rootless")
        inspection_result = self._runner.run(
            (self._podman_binary, "image", "inspect", image.reference, "--format", "json"),
            timeout_seconds=30,
            max_stdout_bytes=4 * 1024 * 1024,
            max_stderr_bytes=64 * 1024,
        )
        inspection_value = _required_json_result(inspection_result, "Podman image preflight")
        inspection = _first_inspection(inspection_value)
        if not _inspection_contains_digest(inspection, command.image_digest):
            raise SandboxRuntimeBlocked("local image inspection does not match the signed digest")
        trust = self._image_verifier.verify(image=image, inspection=inspection)
        if (
            trust.image_digest != image.image_digest
            or trust.signature_policy_hash != image.signature_policy_hash
        ):
            raise SandboxRuntimeBlocked("image trust receipt does not match the deployment image policy")
        return SandboxRuntimePreflight(
            runtime="podman",
            rootless=True,
            image_digest=command.image_digest,
            image_trust=trust,
        )

    def execute(
        self,
        *,
        grant: SandboxTaskGrant,
        command: StructuredSandboxCommand,
    ) -> SandboxRunResult:
        """Execute, account for, register and clean one isolated command attempt."""

        expected = self.compile(grant)
        if command != expected:
            raise SandboxRuntimeBlocked("runtime accepts only the exact compiled StructuredSandboxCommand")
        if grant.network_mode is not SandboxNetworkMode.DISABLED or grant.egress_grant is not None:
            raise SandboxRuntimeBlocked("command containers cannot access the network; use the authorised broker connector")
        if int(self._clock()) >= grant.expires_epoch_seconds:
            raise SandboxRuntimeBlocked("sandbox grant has expired")
        preflight = self.preflight(command)
        remaining_seconds = grant.expires_epoch_seconds - int(self._clock())
        if remaining_seconds < 1:
            raise SandboxRuntimeBlocked("sandbox grant expired during runtime preflight")
        workspace = Path(tempfile.mkdtemp(prefix=f"run-{grant.grant_id}-", dir=self._workspace_root))
        os.chmod(workspace, 0o700)
        input_dir = workspace / "inputs"
        output_dir = workspace / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        container_name = f"case-agent-{grant.grant_id}"
        process_result: ProcessRunResult | None = None
        run_error: Exception | None = None
        cleanup_confirmed = False
        cleanup_error: Exception | None = None
        try:
            self._materialize_inputs(grant, input_dir)
            run_argv = self._container_argv(
                grant=grant,
                command=command,
                image=self._image_for(command.image_digest),
                container_name=container_name,
                input_dir=input_dir,
                output_dir=output_dir,
            )
            process_result = self._runner.run(
                run_argv,
                timeout_seconds=min(grant.budget.timeout_seconds, remaining_seconds),
                max_stdout_bytes=grant.budget.max_stdout_bytes,
                max_stderr_bytes=grant.budget.max_stderr_bytes,
                watch_directory=output_dir,
                max_watch_bytes=_remaining_output_bytes(grant),
            )
        except Exception as error:
            run_error = error
        finally:
            try:
                cleanup = self._runner.run(
                    (self._podman_binary, "rm", "--force", "--ignore", container_name),
                    timeout_seconds=20,
                    max_stdout_bytes=64 * 1024,
                    max_stderr_bytes=64 * 1024,
                )
                cleanup_confirmed = (
                    cleanup.exit_code == 0
                    and not cleanup.timed_out
                    and not cleanup.policy_killed
                    and not cleanup.stdout_truncated
                    and not cleanup.stderr_truncated
                )
            except Exception as error:  # The attempt is now conservatively unknown.
                cleanup_error = error
            if process_result is None or not cleanup_confirmed:
                shutil.rmtree(workspace, ignore_errors=True)
        if not cleanup_confirmed:
            raise SandboxRuntimeBlocked("ephemeral container cleanup could not be confirmed") from cleanup_error
        if run_error is not None:
            raise SandboxRuntimeBlocked("sandbox attempt did not return a trusted process result") from run_error
        assert process_result is not None
        try:
            state = _run_state(process_result)
            if process_result.finished_epoch_seconds > grant.expires_epoch_seconds:
                raise SandboxRuntimeBlocked("sandbox attempt finished after its grant expired")
            outputs: tuple[RegisteredSandboxOutput, ...] = ()
            if state is SandboxRunState.SUCCEEDED:
                outputs = self._register_outputs(grant, output_dir)
            stdout_hash = sha256(process_result.stdout).hexdigest()
            stderr_hash = sha256(process_result.stderr).hexdigest()
            receipt = SandboxExecutionReceipt(
                grant_id=grant.grant_id,
                grant_hash=grant.grant_hash,
                template_id=grant.template_id,
                template_version=grant.template_version,
                image_digest=grant.image_digest,
                exit_code=(
                    process_result.exit_code
                    if process_result.exit_code is not None and process_result.exit_code >= 0
                    else -1
                ),
                timed_out=process_result.timed_out,
                killed_for_policy=process_result.policy_killed,
                stdout_sha256=stdout_hash,
                stderr_sha256=stderr_hash,
                output_hashes=tuple(sorted({item.content_sha256 for item in outputs})),
                output_kinds=tuple(sorted({item.output_kind for item in outputs}, key=lambda value: value.value)),
                network_request_count=0,
                network_response_bytes=0,
                started_epoch_seconds=process_result.started_epoch_seconds,
                finished_epoch_seconds=process_result.finished_epoch_seconds,
            )
            verify_sandbox_receipt(grant, receipt)
            return SandboxRunResult(
                state=state,
                receipt=receipt,
                image_trust=preflight.image_trust,
                stdout=SandboxStreamSummary(stdout_hash, len(process_result.stdout), process_result.stdout_truncated),
                stderr=SandboxStreamSummary(stderr_hash, len(process_result.stderr), process_result.stderr_truncated),
                outputs=outputs,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _template_for(self, grant: SandboxTaskGrant) -> SandboxCommandTemplate:
        try:
            template = self._templates[grant.template_id]
        except KeyError as error:
            raise SandboxRuntimeBlocked("sandbox grant template is not registered") from error
        if template.version != grant.template_version or template.image_digest != grant.image_digest:
            raise SandboxRuntimeBlocked("sandbox grant is not bound to the exact template version and image")
        return template

    def _image_for(self, digest: str) -> SignedImageReference:
        try:
            image = self._signed_images[digest]
        except KeyError as error:
            raise SandboxRuntimeBlocked("sandbox image has no deployment signature policy") from error
        if image.image_digest != digest:
            raise SandboxRuntimeBlocked("sandbox image registry key is inconsistent")
        return image

    def _materialize_inputs(self, grant: SandboxTaskGrant, input_dir: Path) -> None:
        for source in grant.inputs:
            destination = input_dir / source.object_id
            self._input_materializer.materialize(source, destination)
            try:
                metadata = destination.lstat()
            except OSError as error:
                raise SandboxRuntimeBlocked("managed input was not materialized") from error
            if not stat.S_ISREG(metadata.st_mode) or destination.is_symlink():
                raise SandboxRuntimeBlocked("managed input must be a regular non-symlink file")
            if metadata.st_size != source.byte_size or _hash_file(destination) != source.content_sha256:
                raise SandboxRuntimeBlocked("managed input bytes do not match the authorised object version")
            os.chmod(destination, 0o400)

    def _container_argv(
        self,
        *,
        grant: SandboxTaskGrant,
        command: StructuredSandboxCommand,
        image: SignedImageReference,
        container_name: str,
        input_dir: Path,
        output_dir: Path,
    ) -> tuple[str, ...]:
        cpu = f"{grant.budget.cpu_millis / 1000:.3f}"
        tmp_size = min(grant.budget.disk_bytes, 512 * 1024 * 1024)
        return (
            self._podman_binary,
            "run",
            "--rm",
            "--pull=never",
            f"--name={container_name}",
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
            f"--user={self._uid}:{self._gid}",
            f"--pids-limit={grant.budget.max_processes}",
            f"--memory={grant.budget.memory_bytes}",
            f"--cpus={cpu}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={tmp_size}",
            f"--mount=type=bind,src={input_dir},dst={self._CONTAINER_INPUT_ROOT},ro=true",
            f"--mount=type=bind,src={output_dir},dst={self._CONTAINER_OUTPUT_ROOT},rw=true",
            f"--workdir={self._CONTAINER_OUTPUT_ROOT}",
            "--",
            image.reference,
            *command.argv,
        )

    def _register_outputs(
        self,
        grant: SandboxTaskGrant,
        output_dir: Path,
    ) -> tuple[RegisteredSandboxOutput, ...]:
        candidates: list[VerifiedSandboxOutput] = []
        total = 0
        for path in sorted(output_dir.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1:
                raise SandboxRuntimeBlocked("sandbox output contains a non-regular file")
            relative = path.relative_to(output_dir).as_posix()
            _relative_output_path(relative)
            total += metadata.st_size
            if total > _remaining_output_bytes(grant) or len(candidates) >= 10_000:
                raise SandboxRuntimeBlocked("sandbox output exceeds the task budget")
            content_hash = _hash_file(path)
            scan_receipt = self._output_scanner.scan(
                source=path,
                content_sha256=content_hash,
                byte_size=metadata.st_size,
            )
            if (
                scan_receipt.content_sha256 != content_hash
                or scan_receipt.verdict != "CLEAN"
            ):
                raise SandboxRuntimeBlocked("output scan receipt does not match the verified file")
            candidates.append(
                VerifiedSandboxOutput(
                    source=path,
                    relative_path=relative,
                    content_sha256=content_hash,
                    byte_size=metadata.st_size,
                    scan_receipt=scan_receipt,
                )
            )
        if not candidates:
            raise SandboxRuntimeBlocked("successful sandbox produced no managed output")
        registered = self._output_registrar.register_batch(
            grant=grant,
            files=tuple(candidates),
            allowed_kinds=grant.output_kinds,
        )
        if len(registered) != len(candidates):
            raise SandboxRuntimeBlocked("managed output registration is not complete")
        by_path = {item.relative_path: item for item in registered}
        if len(by_path) != len(registered):
            raise SandboxRuntimeBlocked("managed output receipts contain duplicate paths")
        for candidate in candidates:
            item = by_path.get(candidate.relative_path)
            if (
                item is None
                or item.content_sha256 != candidate.content_sha256
                or item.byte_size != candidate.byte_size
                or item.output_kind not in grant.output_kinds
            ):
                raise SandboxRuntimeBlocked("managed output receipt does not match the verified file")
        return tuple(sorted(registered, key=lambda value: value.relative_path))


def _verify_grant_hash(grant: SandboxTaskGrant) -> None:
    payload = {
        "grant_id": grant.grant_id,
        "firm_id": grant.firm_id,
        "matter_id": grant.matter_id,
        "agent_run_id": grant.agent_run_id,
        "task_id": grant.task_id,
        "template_id": grant.template_id,
        "template_version": grant.template_version,
        "image_digest": grant.image_digest,
        "arguments": sorted(grant.arguments),
        "inputs": [
            {
                "object_id": item.object_id,
                "object_version": item.object_version,
                "content_sha256": item.content_sha256,
                "byte_size": item.byte_size,
                "mode": item.mode.value,
            }
            for item in sorted(grant.inputs, key=lambda value: value.object_id)
        ],
        "output_kinds": sorted(item.value for item in grant.output_kinds),
        "budget": grant.budget.__dict__,
        "network_mode": grant.network_mode.value,
        "egress_grant": None if grant.egress_grant is None else {
            "grant_id": grant.egress_grant.grant_id,
            "allowed_hosts": grant.egress_grant.allowed_hosts,
            "allowed_methods": grant.egress_grant.allowed_methods,
            "max_requests": grant.egress_grant.max_requests,
            "max_response_bytes": grant.egress_grant.max_response_bytes,
            "expires_epoch_seconds": grant.egress_grant.expires_epoch_seconds,
            "query_data_minimized": grant.egress_grant.query_data_minimized,
        },
        "expires_epoch_seconds": grant.expires_epoch_seconds,
    }
    expected = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if grant.grant_hash != expected:
        raise SandboxRuntimeBlocked("sandbox grant hash does not match its exact policy payload")


def _required_json_result(result: ProcessRunResult, label: str) -> object:
    if (
        result.exit_code != 0
        or result.timed_out
        or result.policy_killed
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise SandboxRuntimeBlocked(f"{label} failed")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SandboxRuntimeBlocked(f"{label} did not return valid JSON") from error


def _podman_rootless(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    host = value.get("host", value.get("Host"))
    if not isinstance(host, dict):
        return False
    security = host.get("security", host.get("Security"))
    if not isinstance(security, dict):
        return False
    return security.get("rootless", security.get("Rootless")) is True


def _first_inspection(value: object) -> Mapping[str, object]:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict):
        return value
    raise SandboxRuntimeBlocked("Podman image inspection has an unexpected shape")


def _inspection_contains_digest(inspection: Mapping[str, object], digest: str) -> bool:
    image_id = inspection.get("Id", inspection.get("ID"))
    if image_id == digest:
        return True
    repo_digests = inspection.get("RepoDigests", inspection.get("RepoDigests", ()))
    return isinstance(repo_digests, list) and any(
        isinstance(value, str) and value.endswith(f"@{digest}") for value in repo_digests
    )


def _run_state(result: ProcessRunResult) -> SandboxRunState:
    if result.policy_killed:
        return SandboxRunState.POLICY_KILLED
    if result.timed_out:
        return SandboxRunState.TIMED_OUT
    if result.exit_code is None or result.exit_code < 0:
        return SandboxRunState.FAILED_UNKNOWN
    if result.exit_code == 0 and not result.stdout_truncated and not result.stderr_truncated:
        return SandboxRunState.SUCCEEDED
    return SandboxRunState.FAILED


def _image_digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise SandboxRuntimeBlocked("image digest must be a pinned SHA-256 digest")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SandboxRuntimeBlocked(f"{label} must be a SHA-256 digest")


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _directory_size(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            metadata = child.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
    return total


def _read_prefix(path: Path, maximum: int) -> bytes:
    if maximum <= 0:
        return b""
    with path.open("rb") as handle:
        return handle.read(maximum)


def _remaining_output_bytes(grant: SandboxTaskGrant) -> int:
    return grant.budget.disk_bytes - sum(item.byte_size for item in grant.inputs)


def _relative_output_path(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or value.startswith(("/", "../"))
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in ("\x00", "\n", "\r"))
    ):
        raise SandboxRuntimeBlocked("sandbox output path is invalid")


__all__ = [
    "ImageSignatureVerifier",
    "ImageTrustReceipt",
    "ManagedInputMaterializer",
    "ManagedOutputRegistrar",
    "NoShellProcessRunner",
    "ProcessRunResult",
    "RegisteredSandboxOutput",
    "RootlessPodmanSandboxRuntime",
    "SandboxRunResult",
    "SandboxRunState",
    "SandboxRuntimeBlocked",
    "SandboxRuntimePreflight",
    "SandboxOutputScanner",
    "SandboxOutputScanReceipt",
    "SandboxStreamSummary",
    "SignedImageReference",
    "StructuredSandboxCommand",
    "SubprocessNoShellRunner",
    "VerifiedSandboxOutput",
]
