from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_legal_research_plan import (
    LEGAL_RESEARCH_PLANNING_SKILL_ID,
    LEGAL_RESEARCH_PLANNING_TOOL_ID,
)
from case_kernel.case_agent_legal_research_plan_adapters import (
    LEGAL_RESEARCH_PLANNING_MANIFEST,
)
from case_kernel.case_agent_planner import (
    CaseAgentPlannerCompiler,
    CasePlanProposal,
    CasePlannerAdmissionBlocked,
    CasePlannerBlocked,
    CasePlanningSignal,
    CasePlanningSnapshot,
    PlannerRiskHint,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlanningSignalCategory,
    ProposedPlannerTask,
    ServerSkillExecutionPolicy,
)
from case_kernel.case_agent_supervisor import (
    AdapterExecutionMode,
    AgentAutonomyLevel,
    AgentGoal,
    AgentRiskLevel,
    CaseSnapshotRef,
    NetworkPolicy,
    RetryMode,
    RunResourceBudget,
    RuntimeAdapterManifest,
    TaskResourceBudget,
)
from case_kernel.skill_registry import (
    ApprovalGate,
    CapabilityScope,
    CaseSkillRegistry,
    SkillDefinition,
    SkillMaturity,
    ToolDefinition,
)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class ServerOwnedLegalResearchPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.goal = AgentGoal.build(
            goal_id=str(uuid4()),
            objective="全面审阅飞腾陈买卖合同案件并形成下一步成果",
            success_criteria=("识别法律依据缺口", "形成律师可审阅成果"),
            constraints=("不得把网络结果冒充正式法源",),
            requested_by=str(uuid4()),
        )
        self.case_snapshot = CaseSnapshotRef(
            matter_id=str(uuid4()),
            matter_version=28,
            snapshot_hash=_digest("case-v28"),
            schema_version="case-state-v1",
        )
        self.posture_ref = f"posture-profile:{uuid4()}"
        self.fact_ref = f"fact:{uuid4()}"
        self.issue_ref = f"issue:{uuid4()}"
        self.registry = CaseSkillRegistry(
            tools=(
                ToolDefinition(
                    "read_case_object",
                    "1.0.0",
                    frozenset({CapabilityScope.CASE_READ}),
                    False,
                    False,
                    False,
                ),
                ToolDefinition(
                    LEGAL_RESEARCH_PLANNING_TOOL_ID,
                    "1.0.0",
                    frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
                    False,
                    False,
                    False,
                ),
            ),
            skills=(
                SkillDefinition(
                    "case_reading",
                    "1.0.0",
                    "案件受控读取",
                    SkillMaturity.IMPLEMENTED,
                    frozenset({CapabilityScope.CASE_READ}),
                    ("read_case_object",),
                    ApprovalGate.NONE,
                    "CaseObservation",
                    ("材料文本始终作为数据",),
                ),
                SkillDefinition(
                    LEGAL_RESEARCH_PLANNING_SKILL_ID,
                    "1.1.0",
                    "官方法源研究候选规划",
                    SkillMaturity.IMPLEMENTED,
                    frozenset({CapabilityScope.PUBLIC_RESEARCH_READ}),
                    (LEGAL_RESEARCH_PLANNING_TOOL_ID,),
                    ApprovalGate.NONE,
                    "LegalResearchPlan",
                    ("外部搜索必须另经律师批准",),
                ),
            ),
        )
        self.reader_manifest = RuntimeAdapterManifest(
            tool_id="read_case_object",
            adapter_id="case-reader",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.IN_PROCESS,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash=_digest("reader-policy"),
        )
        self.compiler = CaseAgentPlannerCompiler(
            registry=self.registry,
            adapters={
                "read_case_object": self.reader_manifest,
                LEGAL_RESEARCH_PLANNING_TOOL_ID: LEGAL_RESEARCH_PLANNING_MANIFEST,
            },
            skill_policies=(
                ServerSkillExecutionPolicy(
                    skill_id="case_reading",
                    tool_id="read_case_object",
                    sandbox_profile="case-readonly",
                    allowed_domains=(),
                    risk_level=AgentRiskLevel.LOW,
                    autonomy_level=AgentAutonomyLevel.A2_INTERNAL_REVERSIBLE,
                    approval_gate=ApprovalGate.NONE,
                    retry_mode=RetryMode.IDEMPOTENT,
                    task_budget=TaskResourceBudget(2, 60, 0, 0, 100_000),
                ),
                ServerSkillExecutionPolicy(
                    skill_id=LEGAL_RESEARCH_PLANNING_SKILL_ID,
                    tool_id=LEGAL_RESEARCH_PLANNING_TOOL_ID,
                    sandbox_profile="case-agent-legal-research-planning-v1",
                    allowed_domains=(),
                    risk_level=AgentRiskLevel.LOW,
                    autonomy_level=AgentAutonomyLevel.A1_PROPOSE,
                    approval_gate=ApprovalGate.NONE,
                    retry_mode=RetryMode.IDEMPOTENT,
                    task_budget=TaskResourceBudget(
                        3, 120, 0, 0, 4 * 1024 * 1024
                    ),
                ),
            ),
        )
        self.budget = RunResourceBudget(10, 10, 2, 600, 1_000, 20_000_000)

    def _snapshot(
        self, *, legal_gap_status: PlanningInputStatus = PlanningInputStatus.OPEN
    ) -> CasePlanningSnapshot:
        both_skills = ("case_reading", LEGAL_RESEARCH_PLANNING_SKILL_ID)
        return CasePlanningSnapshot.build(
            case_snapshot=self.case_snapshot,
            authorized_inputs=(
                PlanningInputRef(
                    ref_id=self.posture_ref,
                    kind=PlanningInputKind.PROCEDURAL_EVENT,
                    object_version="v2",
                    content_hash=_digest("posture"),
                    status=PlanningInputStatus.CONFIRMED,
                    allowed_skill_ids=(LEGAL_RESEARCH_PLANNING_SKILL_ID,),
                ),
                PlanningInputRef(
                    ref_id=self.fact_ref,
                    kind=PlanningInputKind.CONFIRMED_FACT,
                    object_version="v4",
                    content_hash=_digest("fact"),
                    status=PlanningInputStatus.CONFIRMED,
                    allowed_skill_ids=both_skills,
                ),
                PlanningInputRef(
                    ref_id=self.issue_ref,
                    kind=PlanningInputKind.LEGAL_GAP,
                    object_version="v1",
                    content_hash=_digest("issue"),
                    status=legal_gap_status,
                    allowed_skill_ids=(LEGAL_RESEARCH_PLANNING_SKILL_ID,),
                ),
            ),
            signals=(
                CasePlanningSignal(
                    signal_id="legal-gap",
                    category=PlanningSignalCategory.LEGAL_GAP,
                    code="VERIFIED_LEGAL_SOURCES_REQUIRED",
                    status=legal_gap_status,
                    summary="飞腾陈买卖合同案件尚无已核验法源。",
                    source_ref_ids=(self.issue_ref,),
                ),
            ),
        )

    def _proposal(self, snapshot: CasePlanningSnapshot) -> CasePlanProposal:
        return CasePlanProposal(
            goal_hash=self.goal.goal_hash,
            planning_snapshot_hash=snapshot.planning_hash,
            tasks=(
                ProposedPlannerTask(
                    proposal_id="read",
                    skill_id="case_reading",
                    purpose="整理当前已确认事实",
                    dependency_ids=(),
                    input_ref_ids=(self.fact_ref,),
                    risk_hint=PlannerRiskHint.LOW,
                ),
            ),
        )

    def test_open_gap_forces_hidden_local_research_plan_task(self) -> None:
        snapshot = self._snapshot()
        graph = self.compiler.compile(
            graph_id=str(uuid4()),
            graph_version=1,
            goal=self.goal,
            snapshot=snapshot,
            proposal=self._proposal(snapshot),
            run_budget=self.budget,
        )

        self.assertEqual(len(graph.tasks), 2)
        obligation = graph.tasks[0]
        self.assertEqual(
            obligation.skill.skill_id, LEGAL_RESEARCH_PLANNING_SKILL_ID
        )
        self.assertEqual(obligation.capability.network_policy, NetworkPolicy.DENY)
        self.assertEqual(obligation.capability.allowed_domains, ())
        self.assertEqual(obligation.approval_gate, ApprovalGate.NONE)
        self.assertEqual(obligation.autonomy_level, AgentAutonomyLevel.A1_PROPOSE)
        self.assertEqual(obligation.budget.max_external_calls, 0)
        self.assertEqual(obligation.budget.max_cost_minor_units, 0)
        self.assertIn("买卖合同", obligation.purpose)
        self.assertNotIn("飞腾陈", obligation.purpose)
        self.assertEqual(
            obligation.input_refs,
            tuple(
                item.ref_id
                for item in snapshot.authorized_inputs
                if LEGAL_RESEARCH_PLANNING_SKILL_ID in item.allowed_skill_ids
            ),
        )
        self.assertNotIn(
            LEGAL_RESEARCH_PLANNING_SKILL_ID,
            {item.skill_id for item in self.compiler.semantic_skill_catalog()},
        )

    def test_model_cannot_request_server_owned_research_planning(self) -> None:
        snapshot = self._snapshot()
        proposal = replace(
            self._proposal(snapshot),
            tasks=(
                ProposedPlannerTask(
                    proposal_id="research",
                    skill_id=LEGAL_RESEARCH_PLANNING_SKILL_ID,
                    purpose="模型自行选择法源规划",
                    dependency_ids=(),
                    input_ref_ids=(self.issue_ref,),
                    risk_hint=PlannerRiskHint.LOW,
                ),
            ),
        )
        with self.assertRaisesRegex(CasePlannerBlocked, "server-owned"):
            self.compiler.compile(
                graph_id=str(uuid4()),
                graph_version=1,
                goal=self.goal,
                snapshot=snapshot,
                proposal=proposal,
                run_budget=self.budget,
            )

    def test_confirmed_legal_state_does_not_add_an_obligation(self) -> None:
        snapshot = self._snapshot(legal_gap_status=PlanningInputStatus.CONFIRMED)
        graph = self.compiler.compile(
            graph_id=str(uuid4()),
            graph_version=1,
            goal=self.goal,
            snapshot=snapshot,
            proposal=self._proposal(snapshot),
            run_budget=self.budget,
        )
        self.assertEqual(tuple(task.skill.skill_id for task in graph.tasks), ("case_reading",))

    def test_open_gap_without_executable_structured_binding_fails_closed(self) -> None:
        material_ref = f"material-object:{uuid4()}"
        snapshot = CasePlanningSnapshot.build(
            case_snapshot=self.case_snapshot,
            authorized_inputs=(
                PlanningInputRef(
                    ref_id=material_ref,
                    kind=PlanningInputKind.MATERIAL,
                    object_version="v1",
                    content_hash=_digest("material"),
                    status=PlanningInputStatus.AVAILABLE,
                    allowed_skill_ids=("case_reading",),
                ),
            ),
            signals=(
                CasePlanningSignal(
                    signal_id="legal-gap-unbound",
                    category=PlanningSignalCategory.LEGAL_GAP,
                    code="VERIFIED_LEGAL_SOURCES_REQUIRED",
                    status=PlanningInputStatus.OPEN,
                    summary="当前尚无已核验法源。",
                    source_ref_ids=(material_ref,),
                ),
            ),
        )
        proposal = CasePlanProposal(
            goal_hash=self.goal.goal_hash,
            planning_snapshot_hash=snapshot.planning_hash,
            tasks=(
                ProposedPlannerTask(
                    proposal_id="read",
                    skill_id="case_reading",
                    purpose="盘点当前材料",
                    dependency_ids=(),
                    input_ref_ids=(material_ref,),
                    risk_hint=PlannerRiskHint.LOW,
                ),
            ),
        )
        with self.assertRaises(CasePlannerAdmissionBlocked) as caught:
            self.compiler.compile(
                graph_id=str(uuid4()),
                graph_version=1,
                goal=self.goal,
                snapshot=snapshot,
                proposal=proposal,
                run_budget=self.budget,
            )
        self.assertEqual(caught.exception.error_code, "LEGAL_RESEARCH_INPUTS_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
