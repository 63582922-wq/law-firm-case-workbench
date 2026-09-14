from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import inspect
import unittest
from uuid import uuid4

from case_api.case_agent_worker_runtime import (
    CaseAgentWorkerRunner,
    CaseAgentWorkerRuntimeSettings,
    _document_delivery_policy,
    _lawyer_analysis_policy,
    _visual_ocr_policy,
    compose_case_agent_worker,
)
from case_kernel.case_agent_runtime_postgres import AgentRunInboxClaim
from case_kernel.case_agent_runtime_identity import case_agent_worker_id
from case_kernel.case_agent_worker import AgentWorkerStep, WorkerStepResult
from case_kernel.case_agent_lawyer_analysis_transport import (
    LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Inbox:
    def __init__(self, claim):
        self.claim = claim
        self.settlements = []

    def claim_next_run(self, **_):
        result, self.claim = self.claim, None
        return result

    def settle_run(self, claim, **kwargs):
        self.settlements.append((claim, kwargs))
        return True


class _StoppingInbox(_Inbox):
    def __init__(self):
        super().__init__(None)
        self.runner = None

    def claim_next_run(self, **_):
        self.runner.stop()
        return None


class _Worker:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def process_run_once(self, **_):
        if self.error is not None:
            raise self.error
        return self.result


class _Readiness:
    def __init__(self):
        self.calls = []

    def publish_heartbeat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace()


class _Memory:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def checkpoint_current_run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class _Incidents:
    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)


class _DocumentRevisions:
    def __init__(self, *, result=False, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def run_cycle(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class CaseAgentWorkerRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = Actor(_id(), _id(), frozenset({Role.SYSTEM_WORKER}))
        self.settings = CaseAgentWorkerRuntimeSettings(
            worker_id=case_agent_worker_id(self.actor.firm_id),
            actor=self.actor,
            postgres_dsn="postgresql://not-used.invalid/lawcase",
            verifier_actor=Actor(
                _id(), self.actor.firm_id, frozenset({Role.SYSTEM_WORKER})
            ),
            verifier_postgres_dsn="postgresql://verifier.invalid/lawcase",
            worker_root="/not-used-by-runner-test",
        )
        self.claim = AgentRunInboxClaim(
            run_id=_id(),
            firm_id=self.actor.firm_id,
            matter_id=_id(),
            observed_event_version=4,
            inbox_version=2,
            lease_owner=case_agent_worker_id(self.actor.firm_id),
            lease_token=_id(),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

    def _runner(
        self,
        *,
        inbox,
        worker,
        memory=None,
        incidents=None,
        document_revisions=None,
        official_source_captures=None,
    ):
        return CaseAgentWorkerRunner(
            settings=self.settings,
            inbox=inbox,
            worker=worker,
            readiness=_Readiness(),
            document_revision_worker=document_revisions,
            official_source_capture_worker=official_source_captures,
            memory_checkpoint=memory,
            incident_sink=incidents,
        )

    def test_document_revision_is_consumed_before_agent_run_inbox(self) -> None:
        inbox = _Inbox(self.claim)
        revisions = _DocumentRevisions(result=True)
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(),
            document_revisions=revisions,
        )

        self.assertTrue(runner.run_cycle())
        self.assertEqual(revisions.calls, 1)
        self.assertIs(inbox.claim, self.claim)
        self.assertEqual(inbox.settlements, [])

    def test_document_revision_outage_does_not_block_agent_run(self) -> None:
        inbox = _Inbox(self.claim)
        incidents = _Incidents()
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(
                WorkerStepResult(
                    self.claim.run_id,
                    AgentWorkerStep.WAITING_HUMAN,
                    self.claim.observed_event_version,
                )
            ),
            incidents=incidents,
            document_revisions=_DocumentRevisions(error=RuntimeError("private")),
        )

        self.assertTrue(runner.run_cycle())
        self.assertEqual(
            incidents.calls[0]["code"],
            "CASE_AGENT_DOCUMENT_REVISION_CYCLE_BLOCKED",
        )
        self.assertNotIn("private", str(incidents.calls))
        self.assertEqual(inbox.settlements[0][1], {"quiet": True})

    def test_official_source_capture_is_consumed_before_agent_run_inbox(self) -> None:
        inbox = _Inbox(self.claim)
        captures = _DocumentRevisions(result=True)
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(),
            official_source_captures=captures,
        )

        self.assertTrue(runner.run_cycle())
        self.assertEqual(captures.calls, 1)
        self.assertEqual(inbox.settlements, [])

    def test_official_source_capture_outage_does_not_block_agent_run(self) -> None:
        inbox = _Inbox(self.claim)
        incidents = _Incidents()
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(
                WorkerStepResult(
                    self.claim.run_id,
                    AgentWorkerStep.WAITING_HUMAN,
                    self.claim.observed_event_version,
                )
            ),
            incidents=incidents,
            official_source_captures=_DocumentRevisions(error=RuntimeError("private")),
        )

        self.assertTrue(runner.run_cycle())
        self.assertEqual(
            incidents.calls[0]["code"],
            "CASE_AGENT_OFFICIAL_SOURCE_CAPTURE_CYCLE_BLOCKED",
        )
        self.assertNotIn("private", str(incidents.calls))
        self.assertEqual(inbox.settlements[0][1], {"quiet": True})

    def test_waiting_human_quiets_until_next_authoritative_event(self) -> None:
        inbox = _Inbox(self.claim)
        worker = _Worker(
            WorkerStepResult(
                self.claim.run_id, AgentWorkerStep.WAITING_HUMAN, 4
            )
        )
        runner = self._runner(inbox=inbox, worker=worker)
        self.assertTrue(runner.run_cycle())
        self.assertEqual(inbox.settlements[0][1], {"quiet": True})

    def test_successful_step_gets_memory_checkpoint_before_requeue(self) -> None:
        inbox = _Inbox(self.claim)
        memory = _Memory()
        worker = _Worker(
            WorkerStepResult(
                self.claim.run_id, AgentWorkerStep.TASK_SUCCEEDED, 5
            )
        )
        runner = self._runner(
            inbox=inbox, worker=worker, memory=memory, incidents=_Incidents()
        )
        self.assertTrue(runner.run_cycle())
        self.assertEqual(memory.calls[0]["run_id"], self.claim.run_id)
        self.assertEqual(inbox.settlements[0][1], {"quiet": False, "retry_after_seconds": 0})

    def test_terminal_verification_step_checkpoints_before_next_human_gate(self) -> None:
        for step in (
            AgentWorkerStep.VERIFICATION_PASSED,
            AgentWorkerStep.VERIFICATION_FAILED,
        ):
            with self.subTest(step=step):
                inbox = _Inbox(self.claim)
                memory = _Memory()
                runner = self._runner(
                    inbox=inbox,
                    worker=_Worker(
                        WorkerStepResult(self.claim.run_id, step, 6)
                    ),
                    memory=memory,
                    incidents=_Incidents(),
                )

                self.assertTrue(runner.run_cycle())
                self.assertEqual(memory.calls[0]["run_id"], self.claim.run_id)
                self.assertEqual(
                    inbox.settlements[0][1],
                    {"quiet": False, "retry_after_seconds": 0},
                )

    def test_memory_failure_is_not_reclassified_as_task_failure(self) -> None:
        inbox = _Inbox(self.claim)
        memory = _Memory(error=RuntimeError("private detail must not escape"))
        incidents = _Incidents()
        worker = _Worker(
            WorkerStepResult(
                self.claim.run_id, AgentWorkerStep.TASK_SUCCEEDED, 5
            )
        )
        runner = self._runner(
            inbox=inbox, worker=worker, memory=memory, incidents=incidents
        )
        self.assertTrue(runner.run_cycle())
        self.assertEqual(
            incidents.calls[0]["code"], "CASE_AGENT_MEMORY_CHECKPOINT_BLOCKED"
        )
        self.assertNotIn("private detail", str(incidents.calls))
        self.assertEqual(
            inbox.settlements[0][1],
            {"quiet": False, "retry_after_seconds": 30},
        )

    def test_indeterminate_verification_stays_recoverably_scheduled(self) -> None:
        inbox = _Inbox(self.claim)
        worker = _Worker(
            WorkerStepResult(
                self.claim.run_id,
                AgentWorkerStep.VERIFICATION_REQUIRED,
                self.claim.observed_event_version,
                reason_code="VERIFICATION_RESULT_INDETERMINATE",
            )
        )
        runner = self._runner(inbox=inbox, worker=worker)
        self.assertTrue(runner.run_cycle())
        self.assertEqual(
            inbox.settlements[0][1],
            {"quiet": False, "retry_after_seconds": 30},
        )

    def test_unconfigured_verifier_remains_a_quiet_operator_gate(self) -> None:
        inbox = _Inbox(self.claim)
        worker = _Worker(
            WorkerStepResult(
                self.claim.run_id,
                AgentWorkerStep.VERIFICATION_REQUIRED,
                self.claim.observed_event_version,
                reason_code="INDEPENDENT_VERIFIER_NOT_REGISTERED",
            )
        )
        runner = self._runner(inbox=inbox, worker=worker)
        self.assertTrue(runner.run_cycle())
        self.assertEqual(inbox.settlements[0][1], {"quiet": True})

    def test_unresolved_reconciliation_is_audited_then_quieted(self) -> None:
        inbox = _Inbox(self.claim)
        incidents = _Incidents()
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(
                WorkerStepResult(
                    self.claim.run_id,
                    AgentWorkerStep.RECONCILIATION_DEFERRED,
                    self.claim.observed_event_version,
                    reason_code="LAWYER_ANALYSIS_RECOVERY_UNRESOLVED",
                )
            ),
            incidents=incidents,
        )

        self.assertTrue(runner.run_cycle())
        self.assertEqual(
            incidents.calls[0]["code"], "CASE_AGENT_RECONCILIATION_UNRESOLVED"
        )
        self.assertEqual(inbox.settlements[0][1], {"quiet": True})

    def test_readiness_cannot_publish_before_consumer_loop_started(self) -> None:
        runner = self._runner(inbox=_Inbox(None), worker=_Worker())
        with self.assertRaisesRegex(RuntimeError, "before consumer"):
            runner._publish_readiness_if_due()

    def test_serve_loop_publishes_only_after_real_consumption_loop_starts(self) -> None:
        readiness = _Readiness()
        inbox = _StoppingInbox()
        runner = CaseAgentWorkerRunner(
            settings=self.settings,
            inbox=inbox,
            worker=_Worker(),
            readiness=readiness,
        )
        inbox.runner = runner
        runner.serve_forever()
        self.assertEqual(
            readiness.calls,
            [{"ttl_seconds": self.settings.readiness_ttl_seconds}],
        )

    def test_worker_exception_requeues_without_persisting_exception_text(self) -> None:
        inbox = _Inbox(self.claim)
        incidents = _Incidents()
        runner = self._runner(
            inbox=inbox,
            worker=_Worker(error=RuntimeError("case text")),
            incidents=incidents,
        )
        self.assertTrue(runner.run_cycle())
        self.assertEqual(incidents.calls[0]["code"], "CASE_AGENT_RUN_STEP_BLOCKED")
        self.assertNotIn("case text", str(incidents.calls))
        self.assertEqual(
            inbox.settlements[0][1],
            {"quiet": False, "retry_after_seconds": 30},
        )

    def test_composition_fails_closed_without_exactly_one_snapshot_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            compose_case_agent_worker(
                settings=self.settings,
                object_store=object(),
                planner_factory=lambda _: object(),
            )

    def test_composition_always_injects_exception_followup_automation(self) -> None:
        source = inspect.getsource(compose_case_agent_worker)
        self.assertIn(
            "PostgresCaseLedgerExceptionFollowupAutomation(", source
        )
        self.assertIn(
            "ledger_exception_followups=ledger_exception_followups", source
        )
        provider = SimpleNamespace(build_for_run=lambda **_: None)
        repository = SimpleNamespace(read_atomic_projection=lambda **_: None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            compose_case_agent_worker(
                settings=self.settings,
                object_store=object(),
                snapshot_provider=provider,
                planning_repository=repository,
                planner_factory=lambda _: object(),
            )

    def test_memory_requires_a_durable_incident_sink(self) -> None:
        with self.assertRaisesRegex(ValueError, "incident sink"):
            self._runner(
                inbox=_Inbox(None), worker=_Worker(), memory=_Memory(), incidents=None
            )

    def test_visual_ocr_policy_is_exact_review_only_and_never_retried(self) -> None:
        policy = _visual_ocr_policy("ws-legal-prod")
        self.assertEqual(policy.skill_id, "image_visual_ocr")
        self.assertEqual(policy.task_budget.max_cost_minor_units, 6)
        self.assertEqual(policy.tool_id, "understand_visual_page")
        self.assertEqual(
            policy.allowed_domains,
            ("ws-legal-prod.cn-beijing.maas.aliyuncs.com",),
        )
        self.assertEqual(policy.risk_level.value, "HIGH")
        self.assertEqual(policy.autonomy_level.value, "A3_LAWYER_APPROVAL")
        self.assertEqual(policy.approval_gate.value, "LAWYER_REVIEW")
        self.assertEqual(policy.retry_mode.value, "NEVER_AUTOMATIC")
        self.assertEqual(policy.task_budget.max_attempts, 1)
        self.assertEqual(policy.task_budget.max_external_calls, 1)

    def test_lawyer_analysis_policy_is_exact_capped_and_never_retried(self) -> None:
        host = "ws-commercial-lawyer.cn-beijing.maas.aliyuncs.com"
        policy = _lawyer_analysis_policy(host)
        self.assertEqual(policy.skill_id, "lawyer_decision_package")
        self.assertEqual(policy.tool_id, "analyze_lawyer_decision_package")
        self.assertEqual(policy.allowed_domains, (host,))
        self.assertEqual(policy.sandbox_profile, "case-agent-qwen-lawyer-analysis-v1")
        self.assertEqual(policy.risk_level.value, "HIGH")
        self.assertEqual(policy.autonomy_level.value, "A3_LAWYER_APPROVAL")
        self.assertEqual(policy.approval_gate.value, "LAWYER_REVIEW")
        self.assertEqual(policy.retry_mode.value, "NEVER_AUTOMATIC")
        self.assertEqual(policy.task_budget.max_attempts, 1)
        self.assertEqual(
            policy.task_budget.timeout_seconds,
            LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
        )
        self.assertEqual(policy.task_budget.max_external_calls, 1)
        self.assertEqual(policy.task_budget.max_cost_minor_units, 120)
        self.assertEqual(policy.task_budget.max_output_bytes, 4 * 1024 * 1024)
        self.assertEqual(policy.max_input_refs, 500)
        with self.assertRaisesRegex(ValueError, "endpoint host"):
            _lawyer_analysis_policy("example.com")

    def test_dynamic_document_policies_create_review_candidates_without_preapproval(self) -> None:
        for skill_id, tool_id, domains, calls, timeout in (
            (
                "dynamic_document_delivery",
                "draft_reviewable_docx_package",
                (),
                0,
                210,
            ),
            (
                "dynamic_spreadsheet_delivery",
                "draft_reviewable_xlsx_package",
                (),
                0,
                210,
            ),
        ):
            with self.subTest(skill_id=skill_id):
                policy = _document_delivery_policy(
                    skill_id=skill_id,
                    tool_id=tool_id,
                )
                self.assertEqual(policy.allowed_domains, domains)
                self.assertEqual(policy.risk_level.value, "MEDIUM")
                self.assertEqual(
                    policy.autonomy_level.value, "A2_INTERNAL_REVERSIBLE"
                )
                self.assertEqual(policy.approval_gate.value, "NONE")
                self.assertEqual(policy.retry_mode.value, "NEVER_AUTOMATIC")
                self.assertEqual(policy.task_budget.max_attempts, 1)
                self.assertEqual(policy.task_budget.max_external_calls, calls)
                self.assertEqual(policy.task_budget.timeout_seconds, timeout)



if __name__ == "__main__":
    unittest.main()
