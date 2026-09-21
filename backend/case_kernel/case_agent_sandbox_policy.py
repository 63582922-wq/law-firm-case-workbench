"""Fail-closed command and network grants for case-isolated Agent tasks.

This is the control-plane contract, not a process launcher.  A production
worker must translate a validated grant into a rootless one-shot sandbox and
return a receipt that exactly matches the grant.  No API accepts a shell
string, host path, credential or arbitrary URL from a model or browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from ipaddress import ip_address
import json
import math
import re
from urllib.parse import urlsplit
from uuid import UUID


class SandboxPolicyBlocked(PermissionError):
    """A command or network grant exceeded the task's least privilege."""


class SandboxNetworkMode(StrEnum):
    DISABLED = "DISABLED"
    BROKERED_HTTPS = "BROKERED_HTTPS"


class SandboxCommandMaturity(StrEnum):
    ENABLED = "ENABLED"
    GATED = "GATED"
    DISABLED = "DISABLED"


class SandboxInputMode(StrEnum):
    READ_ONLY_OBJECT = "READ_ONLY_OBJECT"


class SandboxOutputKind(StrEnum):
    MANAGED_DERIVATIVE = "MANAGED_DERIVATIVE"
    STRUCTURED_RESULT = "STRUCTURED_RESULT"
    VALIDATION_REPORT = "VALIDATION_REPORT"


@dataclass(frozen=True)
class SandboxResourceBudget:
    cpu_millis: int
    memory_bytes: int
    disk_bytes: int
    max_processes: int
    timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int

    def __post_init__(self) -> None:
        limits = (
            ("cpu_millis", self.cpu_millis, 100, 64_000),
            ("memory_bytes", self.memory_bytes, 64 * 1024 * 1024, 32 * 1024**3),
            ("disk_bytes", self.disk_bytes, 1024 * 1024, 100 * 1024**3),
            ("max_processes", self.max_processes, 1, 256),
            ("timeout_seconds", self.timeout_seconds, 1, 3_600),
            ("max_stdout_bytes", self.max_stdout_bytes, 0, 16 * 1024 * 1024),
            ("max_stderr_bytes", self.max_stderr_bytes, 0, 16 * 1024 * 1024),
        )
        for label, value, minimum, maximum in limits:
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise SandboxPolicyBlocked(f"sandbox {label} is outside the policy limit")


@dataclass(frozen=True)
class SandboxArgumentDefinition:
    name: str
    pattern: str
    required: bool
    max_length: int
    allowed_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _code(self.name, "argument name", lowercase=True)
        if not isinstance(self.max_length, int) or not 1 <= self.max_length <= 4_096:
            raise SandboxPolicyBlocked("sandbox argument length is invalid")
        try:
            re.compile(self.pattern)
        except re.error as error:
            raise SandboxPolicyBlocked("sandbox argument pattern is invalid") from error
        if len(self.allowed_values) != len(set(self.allowed_values)) or len(self.allowed_values) > 200:
            raise SandboxPolicyBlocked("sandbox argument allowlist is invalid")
        for item in self.allowed_values:
            _argument_value(item, self)


@dataclass(frozen=True)
class SandboxCommandTemplate:
    template_id: str
    version: str
    image_digest: str
    executable_path: str
    fixed_arguments: tuple[str, ...]
    arguments: tuple[SandboxArgumentDefinition, ...]
    maturity: SandboxCommandMaturity
    network_mode: SandboxNetworkMode
    output_kinds: tuple[SandboxOutputKind, ...]
    default_budget: SandboxResourceBudget

    def __post_init__(self) -> None:
        _code(self.template_id, "template_id")
        _version(self.version)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest) is None:
            raise SandboxPolicyBlocked("sandbox image must be pinned by SHA-256 digest")
        if (
            not self.executable_path.startswith("/")
            or ".." in self.executable_path.split("/")
            or any(char in self.executable_path for char in "\x00\n\r")
        ):
            raise SandboxPolicyBlocked("sandbox executable path is invalid")
        if len(self.fixed_arguments) > 100 or any(
            not isinstance(value, str) or not value or len(value) > 1_000
            or any(char in value for char in "\x00\n\r")
            for value in self.fixed_arguments
        ):
            raise SandboxPolicyBlocked("sandbox fixed arguments are invalid")
        names = tuple(item.name for item in self.arguments)
        if len(names) != len(set(names)) or len(names) > 100:
            raise SandboxPolicyBlocked("sandbox argument definitions must be unique and bounded")
        if not self.output_kinds or len(self.output_kinds) != len(set(self.output_kinds)):
            raise SandboxPolicyBlocked("sandbox output kinds must be non-empty and unique")


@dataclass(frozen=True)
class SandboxObjectInput:
    object_id: str
    object_version: str
    content_sha256: str
    byte_size: int
    mode: SandboxInputMode = SandboxInputMode.READ_ONLY_OBJECT

    def __post_init__(self) -> None:
        _uuid(self.object_id, "object_id")
        _text(self.object_version, "object_version", 200)
        _sha256(self.content_sha256, "content_sha256")
        if not isinstance(self.byte_size, int) or not 0 <= self.byte_size <= 20 * 1024**3:
            raise SandboxPolicyBlocked("sandbox input byte size is invalid")


@dataclass(frozen=True)
class EgressGrant:
    grant_id: str
    allowed_hosts: tuple[str, ...]
    allowed_methods: tuple[str, ...]
    max_requests: int
    max_response_bytes: int
    expires_epoch_seconds: int
    query_data_minimized: bool

    def __post_init__(self) -> None:
        _uuid(self.grant_id, "grant_id")
        if not self.allowed_hosts or len(self.allowed_hosts) > 50:
            raise SandboxPolicyBlocked("egress grant requires a bounded host allowlist")
        if tuple(sorted(set(self.allowed_hosts))) != self.allowed_hosts:
            raise SandboxPolicyBlocked("egress hosts must be unique and sorted")
        for host in self.allowed_hosts:
            _public_dns_host(host)
        if not self.allowed_methods or set(self.allowed_methods) - {"GET", "HEAD"}:
            raise SandboxPolicyBlocked("egress grant only supports GET and HEAD")
        if not 1 <= self.max_requests <= 1_000:
            raise SandboxPolicyBlocked("egress request count is invalid")
        if not 1 <= self.max_response_bytes <= 500 * 1024 * 1024:
            raise SandboxPolicyBlocked("egress byte limit is invalid")
        if self.expires_epoch_seconds < 1:
            raise SandboxPolicyBlocked("egress grant expiry is invalid")
        if not self.query_data_minimized:
            raise SandboxPolicyBlocked("egress query must be minimized before a grant is issued")


@dataclass(frozen=True)
class SandboxTaskGrant:
    grant_id: str
    firm_id: str
    matter_id: str
    agent_run_id: str
    task_id: str
    template_id: str
    template_version: str
    image_digest: str
    arguments: tuple[tuple[str, str], ...]
    inputs: tuple[SandboxObjectInput, ...]
    output_kinds: tuple[SandboxOutputKind, ...]
    budget: SandboxResourceBudget
    network_mode: SandboxNetworkMode
    egress_grant: EgressGrant | None
    expires_epoch_seconds: int
    grant_hash: str


@dataclass(frozen=True)
class SandboxExecutionReceipt:
    grant_id: str
    grant_hash: str
    template_id: str
    template_version: str
    image_digest: str
    exit_code: int
    timed_out: bool
    killed_for_policy: bool
    stdout_sha256: str
    stderr_sha256: str
    output_hashes: tuple[str, ...]
    output_kinds: tuple[SandboxOutputKind, ...]
    network_request_count: int
    network_response_bytes: int
    started_epoch_seconds: int
    finished_epoch_seconds: int

    def __post_init__(self) -> None:
        _uuid(self.grant_id, "grant_id")
        _sha256(self.grant_hash, "grant_hash")
        _code(self.template_id, "template_id")
        _version(self.template_version)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest) is None:
            raise SandboxPolicyBlocked("receipt image digest is invalid")
        if not isinstance(self.exit_code, int) or not -1 <= self.exit_code <= 255:
            raise SandboxPolicyBlocked("sandbox receipt exit code is invalid")
        _sha256(self.stdout_sha256, "stdout_sha256")
        _sha256(self.stderr_sha256, "stderr_sha256")
        for value in self.output_hashes:
            _sha256(value, "output_hash")
        if len(self.output_hashes) != len(set(self.output_hashes)):
            raise SandboxPolicyBlocked("sandbox receipt output hashes are duplicated")
        if not 0 <= self.network_request_count <= 1_000:
            raise SandboxPolicyBlocked("sandbox receipt network count is invalid")
        if not 0 <= self.network_response_bytes <= 500 * 1024 * 1024:
            raise SandboxPolicyBlocked("sandbox receipt network bytes are invalid")
        if self.started_epoch_seconds < 1 or self.finished_epoch_seconds < self.started_epoch_seconds:
            raise SandboxPolicyBlocked("sandbox receipt timing is invalid")


class SandboxTemplateRegistry:
    def __init__(self, templates: tuple[SandboxCommandTemplate, ...]) -> None:
        if not templates:
            raise SandboxPolicyBlocked("sandbox registry cannot be empty")
        self._templates = {item.template_id: item for item in templates}
        if len(self._templates) != len(templates):
            raise SandboxPolicyBlocked("sandbox template identifiers must be unique")

    @property
    def policy_version(self) -> str:
        return "1.0.0"

    @property
    def policy_hash(self) -> str:
        """Stable public digest for Supervisor runtime-manifest binding."""

        payload = [
            {
                "template_id": item.template_id,
                "version": item.version,
                "image_digest": item.image_digest,
                "executable_path": item.executable_path,
                "fixed_arguments": item.fixed_arguments,
                "arguments": [
                    {
                        "name": argument.name,
                        "pattern": argument.pattern,
                        "required": argument.required,
                        "max_length": argument.max_length,
                        "allowed_values": argument.allowed_values,
                    }
                    for argument in item.arguments
                ],
                "maturity": item.maturity.value,
                "network_mode": item.network_mode.value,
                "output_kinds": [value.value for value in item.output_kinds],
                "default_budget": item.default_budget.__dict__,
            }
            for item in sorted(self._templates.values(), key=lambda value: value.template_id)
        ]
        return sha256(
            json.dumps(
                {"schema_version": "case-agent-sandbox-policy-v1", "policy_version": self.policy_version, "templates": payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def issue_grant(
        self,
        *,
        grant_id: str,
        firm_id: str,
        matter_id: str,
        agent_run_id: str,
        task_id: str,
        template_id: str,
        arguments: tuple[tuple[str, str], ...],
        inputs: tuple[SandboxObjectInput, ...],
        output_kinds: tuple[SandboxOutputKind, ...],
        expires_epoch_seconds: int,
        egress_grant: EgressGrant | None = None,
        budget: SandboxResourceBudget | None = None,
    ) -> SandboxTaskGrant:
        for label, value in (
            ("grant_id", grant_id),
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("agent_run_id", agent_run_id),
            ("task_id", task_id),
        ):
            _uuid(value, label)
        try:
            template = self._templates[template_id]
        except KeyError as error:
            raise SandboxPolicyBlocked("sandbox template is not registered") from error
        if template.maturity is not SandboxCommandMaturity.ENABLED:
            raise SandboxPolicyBlocked("sandbox template is not enabled in this server release")
        provided = dict(arguments)
        if len(provided) != len(arguments):
            raise SandboxPolicyBlocked("sandbox arguments must be unique")
        definitions = {item.name: item for item in template.arguments}
        if set(provided) - set(definitions):
            raise SandboxPolicyBlocked("sandbox grant contains an unknown argument")
        for name, definition in definitions.items():
            if definition.required and name not in provided:
                raise SandboxPolicyBlocked("sandbox grant omits a required argument")
            if name in provided:
                _argument_value(provided[name], definition)
        if not inputs or len(inputs) > 10_000:
            raise SandboxPolicyBlocked("sandbox grant requires bounded managed-object inputs")
        if len({item.object_id for item in inputs}) != len(inputs):
            raise SandboxPolicyBlocked("sandbox grant contains duplicate input objects")
        if not output_kinds or set(output_kinds) - set(template.output_kinds):
            raise SandboxPolicyBlocked("sandbox output kind is not allowed by the template")
        if template.network_mode is SandboxNetworkMode.DISABLED and egress_grant is not None:
            raise SandboxPolicyBlocked("network-disabled template cannot carry an egress grant")
        if template.network_mode is SandboxNetworkMode.BROKERED_HTTPS and egress_grant is None:
            raise SandboxPolicyBlocked("networked template requires an exact egress grant")
        if expires_epoch_seconds < 1:
            raise SandboxPolicyBlocked("sandbox grant expiry is invalid")
        selected_budget = budget or template.default_budget
        _budget_within(selected_budget, template.default_budget)
        payload = {
            "grant_id": grant_id,
            "firm_id": firm_id,
            "matter_id": matter_id,
            "agent_run_id": agent_run_id,
            "task_id": task_id,
            "template_id": template.template_id,
            "template_version": template.version,
            "image_digest": template.image_digest,
            "arguments": sorted(arguments),
            "inputs": [
                {
                    "object_id": item.object_id,
                    "object_version": item.object_version,
                    "content_sha256": item.content_sha256,
                    "byte_size": item.byte_size,
                    "mode": item.mode.value,
                }
                for item in sorted(inputs, key=lambda value: value.object_id)
            ],
            "output_kinds": sorted(item.value for item in output_kinds),
            "budget": selected_budget.__dict__,
            "network_mode": template.network_mode.value,
            "egress_grant": None if egress_grant is None else {
                "grant_id": egress_grant.grant_id,
                "allowed_hosts": egress_grant.allowed_hosts,
                "allowed_methods": egress_grant.allowed_methods,
                "max_requests": egress_grant.max_requests,
                "max_response_bytes": egress_grant.max_response_bytes,
                "expires_epoch_seconds": egress_grant.expires_epoch_seconds,
                "query_data_minimized": egress_grant.query_data_minimized,
            },
            "expires_epoch_seconds": expires_epoch_seconds,
        }
        grant_hash = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return SandboxTaskGrant(
            grant_id=grant_id,
            firm_id=firm_id,
            matter_id=matter_id,
            agent_run_id=agent_run_id,
            task_id=task_id,
            template_id=template.template_id,
            template_version=template.version,
            image_digest=template.image_digest,
            arguments=tuple(sorted(arguments)),
            inputs=tuple(sorted(inputs, key=lambda value: value.object_id)),
            output_kinds=tuple(sorted(set(output_kinds), key=lambda value: value.value)),
            budget=selected_budget,
            network_mode=template.network_mode,
            egress_grant=egress_grant,
            expires_epoch_seconds=expires_epoch_seconds,
            grant_hash=grant_hash,
        )


def verify_sandbox_receipt(
    grant: SandboxTaskGrant, receipt: SandboxExecutionReceipt
) -> None:
    exact = (
        receipt.grant_id == grant.grant_id
        and receipt.grant_hash == grant.grant_hash
        and receipt.template_id == grant.template_id
        and receipt.template_version == grant.template_version
        and receipt.image_digest == grant.image_digest
        and set(receipt.output_kinds).issubset(grant.output_kinds)
        and receipt.finished_epoch_seconds <= grant.expires_epoch_seconds
    )
    if not exact:
        raise SandboxPolicyBlocked("sandbox receipt does not match the exact task grant")
    if grant.network_mode is SandboxNetworkMode.DISABLED:
        if receipt.network_request_count or receipt.network_response_bytes:
            raise SandboxPolicyBlocked("network-disabled sandbox reported network activity")
    else:
        egress = grant.egress_grant
        assert egress is not None
        if (
            receipt.network_request_count > egress.max_requests
            or receipt.network_response_bytes > egress.max_response_bytes
        ):
            raise SandboxPolicyBlocked("sandbox exceeded its egress grant")


def validate_broker_target(target_url: str, *, grant: EgressGrant) -> str:
    """Validate a single request target; the broker must also re-check DNS IPs."""

    if not isinstance(target_url, str) or len(target_url) > 4_096:
        raise SandboxPolicyBlocked("egress target URL is invalid")
    parsed = urlsplit(target_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise SandboxPolicyBlocked("egress target must be a credential-free HTTPS URL")
    if parsed.port not in (None, 443):
        raise SandboxPolicyBlocked("egress target port is not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    _public_dns_host(host)
    if host not in grant.allowed_hosts:
        raise SandboxPolicyBlocked("egress target host is not authorized")
    return host


def _argument_value(value: str, definition: SandboxArgumentDefinition) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > definition.max_length
        or "\x00" in value
        or re.fullmatch(definition.pattern, value) is None
        or (definition.allowed_values and value not in definition.allowed_values)
    ):
        raise SandboxPolicyBlocked(f"sandbox argument {definition.name} is invalid")


def _budget_within(requested: SandboxResourceBudget, maximum: SandboxResourceBudget) -> None:
    for field in maximum.__dict__:
        if getattr(requested, field) > getattr(maximum, field):
            raise SandboxPolicyBlocked("sandbox requested resources exceed its template budget")


def _public_dns_host(value: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.lower()
        or value.endswith(".")
        or len(value) > 253
        or re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value) is None
    ):
        raise SandboxPolicyBlocked("egress host must be a normalized public DNS name")
    if value in {"localhost", "localhost.localdomain"} or value.endswith((".local", ".internal", ".localhost")):
        raise SandboxPolicyBlocked("egress host is private or local")
    try:
        resolved = ip_address(value)
    except ValueError:
        return
    if not resolved.is_global:
        raise SandboxPolicyBlocked("egress IP address is not globally routable")


def _code(value: str, label: str, *, lowercase: bool = False) -> None:
    pattern = r"[a-z][a-z0-9_]{1,119}" if lowercase else r"[A-Z][A-Z0-9_]{1,119}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise SandboxPolicyBlocked(f"sandbox {label} is invalid")


def _version(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None:
        raise SandboxPolicyBlocked("sandbox template version must be semantic")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise SandboxPolicyBlocked(f"sandbox {label} must be a UUID") from error


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SandboxPolicyBlocked(f"sandbox {label} must be a SHA-256 digest")


def _text(value: str, label: str, maximum: int) -> None:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum:
        raise SandboxPolicyBlocked(f"sandbox {label} is invalid")
