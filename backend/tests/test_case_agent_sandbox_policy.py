from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from uuid import uuid4
import unittest

from case_kernel.case_agent_sandbox_policy import (
    EgressGrant,
    SandboxArgumentDefinition,
    SandboxCommandMaturity,
    SandboxCommandTemplate,
    SandboxExecutionReceipt,
    SandboxInputMode,
    SandboxNetworkMode,
    SandboxObjectInput,
    SandboxOutputKind,
    SandboxPolicyBlocked,
    SandboxResourceBudget,
    SandboxTemplateRegistry,
    validate_broker_target,
    verify_sandbox_receipt,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = SandboxResourceBudget(
            cpu_millis=2_000,
            memory_bytes=512 * 1024 * 1024,
            disk_bytes=2 * 1024**3,
            max_processes=32,
            timeout_seconds=300,
            max_stdout_bytes=1024 * 1024,
            max_stderr_bytes=1024 * 1024,
        )
        self.template = SandboxCommandTemplate(
            template_id="PDF_RENDER_PAGE",
            version="1.0.0",
            image_digest=f"sha256:{digest('image')}",
            executable_path="/usr/bin/pdftoppm",
            fixed_arguments=("-png", "-singlefile"),
            arguments=(
                SandboxArgumentDefinition("page", r"[1-9][0-9]{0,4}", True, 5),
            ),
            maturity=SandboxCommandMaturity.ENABLED,
            network_mode=SandboxNetworkMode.DISABLED,
            output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,),
            default_budget=self.budget,
        )
        self.registry = SandboxTemplateRegistry((self.template,))
        self.ids = {name: str(uuid4()) for name in ("grant", "firm", "matter", "run", "task", "object")}
        self.source = SandboxObjectInput(
            object_id=self.ids["object"],
            object_version="7",
            content_sha256=digest("pdf"),
            byte_size=1024,
            mode=SandboxInputMode.READ_ONLY_OBJECT,
        )

    def grant(self):
        return self.registry.issue_grant(
            grant_id=self.ids["grant"],
            firm_id=self.ids["firm"],
            matter_id=self.ids["matter"],
            agent_run_id=self.ids["run"],
            task_id=self.ids["task"],
            template_id="PDF_RENDER_PAGE",
            arguments=(("page", "2"),),
            inputs=(self.source,),
            output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,),
            expires_epoch_seconds=2_000,
        )

    def receipt(self, grant):
        return SandboxExecutionReceipt(
            grant_id=grant.grant_id,
            grant_hash=grant.grant_hash,
            template_id=grant.template_id,
            template_version=grant.template_version,
            image_digest=grant.image_digest,
            exit_code=0,
            timed_out=False,
            killed_for_policy=False,
            stdout_sha256=digest("stdout"),
            stderr_sha256=digest("stderr"),
            output_hashes=(digest("png"),),
            output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,),
            network_request_count=0,
            network_response_bytes=0,
            started_epoch_seconds=1_000,
            finished_epoch_seconds=1_010,
        )

    def test_exact_template_grant_and_receipt(self) -> None:
        grant = self.grant()
        self.assertEqual(grant.arguments, (("page", "2"),))
        self.assertEqual(self.registry.policy_version, "1.0.0")
        self.assertEqual(len(self.registry.policy_hash), 64)
        self.assertEqual(
            self.registry.policy_hash,
            SandboxTemplateRegistry((self.template,)).policy_hash,
        )
        verify_sandbox_receipt(grant, self.receipt(grant))

    def test_arbitrary_shell_and_unknown_arguments_have_no_surface(self) -> None:
        with self.assertRaisesRegex(SandboxPolicyBlocked, "unknown argument"):
            self.registry.issue_grant(
                grant_id=self.ids["grant"], firm_id=self.ids["firm"], matter_id=self.ids["matter"],
                agent_run_id=self.ids["run"], task_id=self.ids["task"], template_id="PDF_RENDER_PAGE",
                arguments=(("page", "2"), ("shell", "rm -rf /")), inputs=(self.source,),
                output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,), expires_epoch_seconds=2_000,
            )
        with self.assertRaisesRegex(SandboxPolicyBlocked, "argument page"):
            self.registry.issue_grant(
                grant_id=self.ids["grant"], firm_id=self.ids["firm"], matter_id=self.ids["matter"],
                agent_run_id=self.ids["run"], task_id=self.ids["task"], template_id="PDF_RENDER_PAGE",
                arguments=(("page", "2;curl bad"),), inputs=(self.source,),
                output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,), expires_epoch_seconds=2_000,
            )

    def test_disabled_network_rejects_egress_and_network_receipt(self) -> None:
        egress = EgressGrant(
            grant_id=str(uuid4()), allowed_hosts=("www.court.gov.cn",), allowed_methods=("GET",),
            max_requests=2, max_response_bytes=1024, expires_epoch_seconds=2_000,
            query_data_minimized=True,
        )
        with self.assertRaisesRegex(SandboxPolicyBlocked, "network-disabled"):
            self.registry.issue_grant(
                grant_id=self.ids["grant"], firm_id=self.ids["firm"], matter_id=self.ids["matter"],
                agent_run_id=self.ids["run"], task_id=self.ids["task"], template_id="PDF_RENDER_PAGE",
                arguments=(("page", "2"),), inputs=(self.source,),
                output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,), expires_epoch_seconds=2_000,
                egress_grant=egress,
            )
        grant = self.grant()
        with self.assertRaisesRegex(SandboxPolicyBlocked, "network activity"):
            verify_sandbox_receipt(
                grant, replace(self.receipt(grant), network_request_count=1, network_response_bytes=10)
            )

    def test_network_template_requires_exact_public_https_host(self) -> None:
        networked = replace(
            self.template,
            template_id="OFFICIAL_SOURCE_FETCH",
            network_mode=SandboxNetworkMode.BROKERED_HTTPS,
            output_kinds=(SandboxOutputKind.STRUCTURED_RESULT,),
        )
        registry = SandboxTemplateRegistry((networked,))
        egress = EgressGrant(
            grant_id=str(uuid4()), allowed_hosts=("flk.npc.gov.cn", "www.court.gov.cn"),
            allowed_methods=("GET", "HEAD"), max_requests=5, max_response_bytes=10_000,
            expires_epoch_seconds=2_000, query_data_minimized=True,
        )
        grant = registry.issue_grant(
            grant_id=self.ids["grant"], firm_id=self.ids["firm"], matter_id=self.ids["matter"],
            agent_run_id=self.ids["run"], task_id=self.ids["task"], template_id="OFFICIAL_SOURCE_FETCH",
            arguments=(("page", "2"),), inputs=(self.source,),
            output_kinds=(SandboxOutputKind.STRUCTURED_RESULT,), expires_epoch_seconds=2_000,
            egress_grant=egress,
        )
        self.assertEqual(validate_broker_target("https://flk.npc.gov.cn/detail", grant=egress), "flk.npc.gov.cn")
        with self.assertRaisesRegex(SandboxPolicyBlocked, "not authorized"):
            validate_broker_target("https://example.com/redirect", grant=egress)
        with self.assertRaisesRegex(SandboxPolicyBlocked, "credential-free"):
            validate_broker_target("https://user:pass@flk.npc.gov.cn/", grant=egress)
        verify_sandbox_receipt(grant, replace(self.receipt(grant), output_kinds=(SandboxOutputKind.STRUCTURED_RESULT,)))

    def test_resources_cannot_exceed_template_budget(self) -> None:
        with self.assertRaisesRegex(SandboxPolicyBlocked, "exceed"):
            self.registry.issue_grant(
                grant_id=self.ids["grant"], firm_id=self.ids["firm"], matter_id=self.ids["matter"],
                agent_run_id=self.ids["run"], task_id=self.ids["task"], template_id="PDF_RENDER_PAGE",
                arguments=(("page", "2"),), inputs=(self.source,),
                output_kinds=(SandboxOutputKind.MANAGED_DERIVATIVE,), expires_epoch_seconds=2_000,
                budget=replace(self.budget, timeout_seconds=301),
            )


if __name__ == "__main__":
    unittest.main()
