from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_case_context import (
    BoundCaseContextProjection,
    BoundCaseContextSource,
    CaseContextSourceType,
)
from case_kernel.case_agent_lawyer_analysis import (
    LAWYER_ANALYSIS_CORE_SCHEMA,
    LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
    LAWYER_ANALYSIS_MODEL_ID,
    LAWYER_DECISION_PACKAGE_SCHEMA,
    LawyerAnalysisBlocked,
    build_lawyer_analysis_contract,
    compile_lawyer_decision_package_candidate,
    known_lawyer_analysis_response_cost_minor_units,
    lawyer_decision_package_source_refs,
    parse_lawyer_analysis_provider_response,
    parse_lawyer_decision_package_candidate,
    prepare_lawyer_analysis_request,
    price_qwen37_minor_units,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (
    LAWYER_ANALYSIS_CONNECT_FAILED,
    LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED,
    LAWYER_ANALYSIS_SKILL_ID,
    LAWYER_ANALYSIS_TOOL_ID,
    LawyerAnalysisKnownFailure,
    LawyerAnalysisRecoveryBinding,
    QWEN_LAWYER_ANALYSIS_MANIFEST,
    QwenLawyerAnalysisTaskAdapter,
    RecoveredLawyerAnalysis,
)
from case_kernel.case_agent_lawyer_analysis_transport import (
    LawyerAnalysisUnknownSubmission,
)
from case_kernel.case_agent_skill_adapters import StagedReviewCandidate
from case_kernel.case_agent_worker import CaseAgentReconciliationUnavailable
from case_kernel.case_agent_supervisor import (
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
)
from case_kernel.case_agent_planner import PlanningInputStatus


RUN_ID = "00000000-0000-4000-8000-000000000101"
TASK_ID = "00000000-0000-4000-8000-000000000102"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000103"
FIRM_ID = "00000000-0000-4000-8000-000000000104"
MATTER_ID = "00000000-0000-4000-8000-000000000105"
HOST = "ws-commercial-lawyer.cn-beijing.maas.aliyuncs.com"


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _source(
    number: int,
    source_type: CaseContextSourceType,
    title: str,
    *,
    status: PlanningInputStatus = PlanningInputStatus.CONFIRMED,
) -> BoundCaseContextSource:
    prefix = {
        CaseContextSourceType.POSTURE_PROFILE: "posture-profile",
        CaseContextSourceType.CASE_CLAIM: "claim",
        CaseContextSourceType.DISPUTE_ISSUE: "issue",
        CaseContextSourceType.CASE_FACT: "fact",
        CaseContextSourceType.CASE_TRANSACTION: "transaction",
        CaseContextSourceType.VERIFIED_LEGAL_SOURCE: "legal-source",
        CaseContextSourceType.APPROVED_LEGAL_RULE: "legal-rule",
        CaseContextSourceType.REVIEW_OBLIGATION: "review-obligation",
    }[source_type]
    object_id = f"00000000-0000-4000-8000-{number:012d}"
    return BoundCaseContextSource(
        input_ref=f"{prefix}:{object_id}",
        source_type=source_type,
        object_id=object_id,
        object_version="v1",
        content_hash=_digest(f"source-{number}"),
        status=status,
        primary_text=title,
        secondary_text="服务器核验的案件对象摘要，仅作为内部复核输入。",
        signals=(status.value,),
    )


def _projection() -> BoundCaseContextProjection:
    sources = (
        _source(201, CaseContextSourceType.POSTURE_PROFILE, "代理被告处理一审民间借贷争议"),
        _source(202, CaseContextSourceType.CASE_CLAIM, "原告主张借款本息尚未清偿"),
        _source(
            203,
            CaseContextSourceType.DISPUTE_ISSUE,
            "实际交付范围与诉请范围是否一致",
            status=PlanningInputStatus.DISPUTED,
        ),
        _source(
            204,
            CaseContextSourceType.DISPUTE_ISSUE,
            "已付款项性质和冲抵顺序如何认定",
            status=PlanningInputStatus.REVIEW_REQUIRED,
        ),
        _source(205, CaseContextSourceType.CASE_FACT, "双方存在多次资金往来"),
        _source(206, CaseContextSourceType.CASE_TRANSACTION, "已确认的一笔银行付款"),
        _source(207, CaseContextSourceType.VERIFIED_LEGAL_SOURCE, "已核验的民法典条文快照"),
        _source(208, CaseContextSourceType.APPROVED_LEGAL_RULE, "已批准的利息规则版本"),
    )
    return BoundCaseContextProjection.build(
        run_id=RUN_ID,
        task_id=TASK_ID,
        task_input_hash=_digest("task-input"),
        firm_id=FIRM_ID,
        matter_id=MATTER_ID,
        matter_version=7,
        case_snapshot_hash=_digest("case-snapshot"),
        input_refs=tuple(item.input_ref for item in sources),
        sources=sources,
    )


def _numeric_projection() -> BoundCaseContextProjection:
    base = _projection()
    titles = {
        CaseContextSourceType.CASE_CLAIM: "原告主张被告返还205,000.00元借款",
        CaseContextSourceType.DISPUTE_ISSUE: None,
        CaseContextSourceType.CASE_FACT: "被告称存在8,000.00 HKD还款记录",
        CaseContextSourceType.CASE_TRANSACTION: "2019年6月3日银行流水记载300,000.00元转账",
    }
    issue_titles = iter(
        (
            "银行流水记载200,000.00元交付，与诉请范围是否一致",
            "2022年9月10日发生50,000.00元付款，性质和冲抵顺序待核验",
        )
    )
    sources = tuple(
        replace(
            source,
            primary_text=(
                next(issue_titles)
                if source.source_type is CaseContextSourceType.DISPUTE_ISSUE
                else titles.get(source.source_type) or source.primary_text
            ),
        )
        for source in base.sources
    )
    return BoundCaseContextProjection.build(
        run_id=base.run_id,
        task_id=base.task_id,
        task_input_hash=base.task_input_hash,
        firm_id=base.firm_id,
        matter_id=base.matter_id,
        matter_version=base.matter_version,
        case_snapshot_hash=base.case_snapshot_hash,
        input_refs=base.input_refs,
        sources=sources,
    )


def _valid_core(projection: BoundCaseContextProjection) -> dict[str, object]:
    contract = build_lawyer_analysis_contract(projection)
    claim_position = next(
        item["position_id"]
        for item in contract.position_register
        if item["status"] == "ASSERTED_SOURCE_POSITION"
    )
    issue_rows = []
    for index, issue_ref in enumerate(contract.issue_ids):
        issue_rows.append(
            {
                "issue_ref": issue_ref,
                "priority": "CRITICAL" if index == 0 else "HIGH",
                "evidence_status": "CONTRADICTED" if index == 0 else "PARTIALLY_SUPPORTED",
                "strengths": ["已确认来源能够支持部分抗辩路径"],
                "weaknesses": ["相反材料仍可能削弱本方解释"],
                "supporting_source_refs": [issue_ref, contract.source_ids[-2]],
                "adverse_source_refs": [contract.source_ids[1]],
                "missing_evidence": ["仍需补充原始资金往来沟通"],
                "authority_refs": list(contract.authority_ids),
            }
        )
    return {
        "schema_version": LAWYER_ANALYSIS_CORE_SCHEMA,
        "run_id": projection.run_id,
        "case_posture": "现有材料支持形成分层抗辩，但关键付款性质仍待复核",
        "working_direction": "先封闭交付与还款证据缺口，再确定主备位表达",
        "issues": issue_rows,
        "adversarial_analysis": [
            {
                "issue_ref": issue_ref,
                "position_id": claim_position,
                "why_it_may_work": "对方能够利用书面主张与部分来源形成表面一致性",
                "rebuttal_route": "按原始来源逐项拆分交付、付款和对应关系",
                "residual_risk": "来源链不完整时反驳强度仍会受到限制",
                "source_refs": [issue_ref, contract.source_ids[1]],
                "authority_refs": list(contract.authority_ids),
            }
            for issue_ref in contract.adversarial_issue_ids
        ],
        "strategy_options": [
            {
                "strategy_id": "EVIDENCE_FIRST",
                "conditions": ["原始来源能够形成连续且可核对的证据链"],
                "execution_risks": ["补证不足会限制主位抗辩的证明强度"],
                "tradeoff_note": "以较低事实争议换取更高来源可信度",
                "issue_refs": list(contract.issue_ids),
            },
            {
                "strategy_id": "LAYERED_ALTERNATIVES",
                "conditions": ["争议事项能够按不同认定条件分别表达"],
                "execution_risks": ["层次过多可能增加庭审表达复杂度"],
                "tradeoff_note": "保留抗辩空间但不预先锁定正式立场",
                "issue_refs": list(contract.issue_ids),
            },
        ],
        "decision_analysis": [
            {
                "issue_ref": issue_ref,
                "agent_lean": "FOLLOW_UP_EVIDENCE",
                "reason": "当前来源存在缺口，先补充原始材料更利于律师取舍",
                "source_refs": [issue_ref],
                "authority_refs": list(contract.authority_ids),
            }
            for issue_ref in contract.decision_ids
        ],
        "security": {
            "material_instruction_detected": False,
            "ignored": True,
            "claimed_approval_or_submission": False,
            "notes": "未发现需要执行的材料内指令，仍按不可信数据处理",
        },
    }


def _provider_response(core: dict[str, object]) -> bytes:
    content = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(
        {
            "id": "chatcmpl-controlled",
            "model": LAWYER_ANALYSIS_MODEL_ID,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 12_000,
                "completion_tokens": 3_000,
                "total_tokens": 15_000,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class _ProjectionPort:
    def __init__(self, projection: BoundCaseContextProjection) -> None:
        self.projection = projection
        self.calls: list[dict[str, object]] = []

    def project_case_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.projection


class _Staging:
    def __init__(self) -> None:
        self.request = None

    def stage_review_candidate(self, request):
        self.request = request
        return StagedReviewCandidate.build(
            request,
            artifact_id=str(uuid5(UUID(request.task_id), request.idempotency_key)),
        )


class _Exchange:
    endpoint_host = HOST

    def __init__(self, response: bytes | Exception) -> None:
        self.response = response
        self.requests = []
        self.recovered = None
        self.recovery_bindings = []

    def send(self, *, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def recover(self, **_kwargs):
        if self.recovered is None:
            raise AssertionError("unexpected recovery")
        return self.recovered

    def recover_bound(self, *, binding):
        self.recovery_bindings.append(binding)
        if self.recovered is None:
            raise AssertionError("unexpected bound recovery")
        return self.recovered


class _Context:
    def __init__(self, projection: BoundCaseContextProjection) -> None:
        self.claim = SimpleNamespace(
            run_id=projection.run_id,
            task_id=projection.task_id,
            attempt_id=ATTEMPT_ID,
            reconciliation=False,
        )
        self.task = SimpleNamespace(
            task_id=projection.task_id,
            input_hash=projection.task_input_hash,
            input_refs=projection.input_refs,
            skill=SimpleNamespace(
                skill_id=LAWYER_ANALYSIS_SKILL_ID,
                tool_id=LAWYER_ANALYSIS_TOOL_ID,
                adapter_id=QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_id,
                adapter_version=QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_version,
            ),
            capability=SimpleNamespace(
                network_policy=NetworkPolicy.EXACT_ALLOWLIST,
                allowed_domains=(HOST,),
            ),
            budget=SimpleNamespace(
                max_attempts=1,
                max_external_calls=1,
                max_cost_minor_units=LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
                max_output_bytes=4 * 1024 * 1024,
            ),
        )
        self.input_refs = projection.input_refs
        self.external_request_id = None
        self.external_request = None

    def begin_external_submission(self, **kwargs):
        self.external_request_id = kwargs["external_request_id"]
        self.external_request = kwargs


class LawyerAnalysisContractTests(unittest.TestCase):
    def test_pending_review_is_an_anchor_without_losing_fact_anchors(self) -> None:
        base = _projection()
        pending = _source(299, CaseContextSourceType.REVIEW_OBLIGATION,
            "付款主体尚待核对，不确认还本。", status=PlanningInputStatus.BLOCKED)
        sources = (*tuple(item for item in base.sources
            if item.source_type is not CaseContextSourceType.DISPUTE_ISSUE), pending)
        projection = BoundCaseContextProjection.build(run_id=base.run_id, task_id=base.task_id,
            task_input_hash=base.task_input_hash, firm_id=base.firm_id, matter_id=base.matter_id,
            matter_version=base.matter_version, case_snapshot_hash=base.case_snapshot_hash,
            input_refs=tuple(item.input_ref for item in sources), sources=sources)
        contract = build_lawyer_analysis_contract(projection=projection)
        self.assertIn(pending.input_ref, contract.issue_ids)
        self.assertTrue(any(ref.startswith("fact:") for ref in contract.issue_ids))
        from case_kernel.case_agent_lawyer_analysis import _candidate_analysis_anchor_refs, _analysis_anchor_title
        refs = _candidate_analysis_anchor_refs(tuple(item.input_ref for item in sources),
            {item.input_ref: {"source_type": item.source_type.value} for item in sources})
        self.assertEqual(refs, contract.issue_ids)
        self.assertNotIn("已确认", _analysis_anchor_title(pending.source_type, pending.primary_text))

    def test_early_stage_case_without_registered_issue_uses_confirmed_fact_anchor(self) -> None:
        base = _projection()
        sources = tuple(
            item
            for item in base.sources
            if item.source_type is not CaseContextSourceType.DISPUTE_ISSUE
        )
        projection = BoundCaseContextProjection.build(
            run_id=base.run_id,
            task_id=base.task_id,
            task_input_hash=base.task_input_hash,
            firm_id=base.firm_id,
            matter_id=base.matter_id,
            matter_version=base.matter_version,
            case_snapshot_hash=base.case_snapshot_hash,
            input_refs=tuple(item.input_ref for item in sources),
            sources=sources,
        )

        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        self.assertEqual(contract.issue_ids, (sources[2].input_ref,))
        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(_valid_core(projection)), contract=contract
        )
        candidate = parse_lawyer_decision_package_candidate(
            compile_lawyer_decision_package_candidate(
                projection=projection,
                contract=contract,
                parsed=parsed,
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
            )
        )
        self.assertTrue(
            str(candidate["issues"][0]["title"]).startswith(
                "围绕已确认事项的风险分析："
            )
        )
        self.assertEqual(
            candidate["decision_requests"][0]["disposition"],
            "REQUIRES_LAWYER",
        )

    def test_duplicate_model_source_refs_are_deduplicated_before_strict_validation(self) -> None:
        projection = _projection()
        contract, _request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        core = _valid_core(projection)
        source_ref = core["issues"][0]["authority_refs"][0]
        core["issues"][0]["authority_refs"] = [source_ref, source_ref]
        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )
        self.assertEqual(parsed.core["issues"][0]["authority_refs"], [source_ref])

    def test_managed_adapter_discovers_reviewable_issues_when_no_formal_issue_exists(self) -> None:
        base = _projection()
        sources = tuple(
            item
            for item in base.sources
            if item.source_type is not CaseContextSourceType.DISPUTE_ISSUE
        )
        projection = BoundCaseContextProjection.build(
            run_id=base.run_id,
            task_id=base.task_id,
            task_input_hash=base.task_input_hash,
            firm_id=base.firm_id,
            matter_id=base.matter_id,
            matter_version=base.matter_version,
            case_snapshot_hash=base.case_snapshot_hash,
            input_refs=tuple(item.input_ref for item in sources),
            sources=sources,
        )
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(AssertionError("request preparation only")),
            staging_port=_Staging(),
        )

        prepared_projection, contract, _request = adapter._prepare(_Context(projection))

        self.assertEqual(prepared_projection, projection)
        self.assertEqual(contract.schema_name, "case_agent_discovered_analysis_v1")

    def test_builds_strict_qwen_request_without_truncation_parameter(self) -> None:
        projection = _projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        body = json.loads(request.body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["model"], LAWYER_ANALYSIS_MODEL_ID)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertFalse(contract.schema["additionalProperties"])
        self.assertLessEqual(
            request.worst_case_cost_minor_units,
            LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
        )
        self.assertEqual(request.input_refs, projection.input_refs)

    def test_prompt_quarantines_unverified_law_and_external_action_choice(self) -> None:
        base = _projection()
        sources = tuple(
            item
            for item in base.sources
            if item.source_type is not CaseContextSourceType.VERIFIED_LEGAL_SOURCE
        )
        projection = BoundCaseContextProjection.build(
            run_id=base.run_id,
            task_id=base.task_id,
            task_input_hash=base.task_input_hash,
            firm_id=base.firm_id,
            matter_id=base.matter_id,
            matter_version=base.matter_version,
            case_snapshot_hash=base.case_snapshot_hash,
            input_refs=tuple(item.input_ref for item in sources),
            sources=sources,
        )

        contract = build_lawyer_analysis_contract(projection)

        self.assertIn(
            '"authority_mode":"NO_VERIFIED_AUTHORITIES_RESEARCH_HYPOTHESES_ONLY"',
            contract.user_prompt,
        )
        self.assertIn("待检索、待核验的研究假设", contract.system_prompt)
        self.assertIn("不得替律师决定是否发函、起诉、保全、和解", contract.system_prompt)
        self.assertIn("服务器仅会补齐这些确定锚点", contract.system_prompt)
        self.assertIn("working_direction只能写证据补强", contract.user_prompt)
        self.assertIn("required_anchor_citation_rules", contract.user_prompt)

    def test_prompt_binds_readable_quantities_to_each_source_without_calculation(self) -> None:
        projection = _numeric_projection()

        contract = build_lawyer_analysis_contract(projection)

        for literal in (
            "205,000.00元",
            "200,000.00元",
            "50,000.00元",
            "8,000.00 HKD",
            "300,000.00元",
            "2022年9月10日",
            "2019年6月3日",
        ):
            self.assertNotIn(literal, contract.user_prompt)
        self.assertIn("金额事实", contract.user_prompt)
        self.assertIn("日期事实", contract.user_prompt)
        self.assertIn(
            "QUALITATIVE_ONLY_NO_NUMERICS",
            contract.user_prompt,
        )
        self.assertIn("中文数词", contract.system_prompt)
        self.assertIn(
            "amount:CNY:205000",
            contract.numeric_fact_sources,
        )
        self.assertIn("SOURCE_BOUND_QUANTITIES_V1", contract.user_prompt)
        catalog = json.loads(contract.user_prompt.rsplit("\n", 1)[-1])
        facts_seen = set()
        for item in catalog:
            source = next(source for source in projection.sources if source.input_ref == item["source_ref"])
            self.assertEqual(item["status"], source.status.value)
            for fact in item["quantitative_facts"]:
                canonical = (
                    f"amount:{fact['currency']}:{fact['value']}"
                    if fact["kind"] == "amount" else f"date:{fact['value']}"
                )
                self.assertIn(item["source_ref"], contract.numeric_fact_sources[canonical])
                self.assertIn(fact["label"], item["title"] + item["detail"])
                facts_seen.add(canonical)
        self.assertIn("amount:CNY:205000", facts_seen)
        self.assertIn("amount:HKD:8000", facts_seen)
        self.assertIn("date:2022-09-10", facts_seen)
        self.assertIn("date:2019-06-03", facts_seen)
        self.assertNotIn("amount:CNY:5000", facts_seen)

    def test_quantity_projection_does_not_expand_account_numbers_or_guess_values(self) -> None:
        from case_kernel.case_agent_lawyer_analysis import (
            _numeric_fact_source_index, _prompt_numeric_fact_labels, _prompt_source,
        )
        source = replace(
            _projection().sources[2],
            primary_text="对方声称账户622288889999收款100 USD，另有100未注明币种",
            secondary_text="日期2026年2月30日无效，合同金额壹万元尚待提取",
        )
        labels = _prompt_numeric_fact_labels(_numeric_fact_source_index({
            source.input_ref: (source.primary_text, source.secondary_text),
        }))
        projected = _prompt_source(source, numeric_fact_labels=labels)
        self.assertEqual(projected["status"], "DISPUTED")
        facts = projected["quantitative_facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["currency"], "USD")
        self.assertEqual(facts[0]["value"], "100")
        self.assertNotIn("622288889999", json.dumps(projected))

    def test_rejects_chinese_quantitative_model_text(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["working_direction"] = "建议核验五千元差额来源。"

        with self.assertRaisesRegex(
            LawyerAnalysisBlocked, "Chinese quantitative expression"
        ):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_allows_qualitative_chinese_amount_reference_with_a_determiner(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        # This says that a lawyer must choose which existing amount to rely on;
        # it does not state, infer, or calculate any amount.
        core["working_direction"] = "需确定以哪一金额作为认定依据。"

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )

        self.assertEqual(parsed.core["working_direction"], core["working_direction"])

        core["working_direction"] = "建议核验百分之五的规则口径。"
        with self.assertRaisesRegex(
            LawyerAnalysisBlocked, "Chinese quantitative expression"
        ):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_approved_rule_is_source_bound_but_never_promoted_to_legal_authority(self) -> None:
        projection = _projection()

        contract = build_lawyer_analysis_contract(projection)
        rule_ref = next(
            item.input_ref
            for item in projection.sources
            if item.source_type is CaseContextSourceType.APPROVED_LEGAL_RULE
        )

        self.assertIn(rule_ref, contract.source_ids)
        self.assertNotIn(rule_ref, contract.authority_ids)
        self.assertIn('"source_type":"APPROVED_LEGAL_RULE"', contract.user_prompt)
        self.assertIn("不得复述、推导或适用其中的参数", contract.system_prompt)
        self.assertIn("只能写“受控规则来源”", contract.system_prompt)

    def test_allows_only_the_narrow_approved_rule_source_reference(self) -> None:
        projection = _projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["adversarial_analysis"][0]["rebuttal_route"] = (
            "结合已批准规则审查差额性质，并要求对方补充原始结算来源"
        )

        parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )

        core["adversarial_analysis"][0]["rebuttal_route"] = (
            "本案处理已批准，后续无需核验"
        )
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_rejects_model_authored_number_or_controlled_conclusion(self) -> None:
        projection = _projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["case_posture"] = "建议减免百分之五十并认定已经超过3年"
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_accepts_only_source_bound_factual_numbers_and_records_bindings(self) -> None:
        projection = _numeric_projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        core = _valid_core(projection)
        core["working_direction"] = (
            "建议核验：1. 205,000元诉请与200,000元流水；"
            "2. 2022年9月10日50,000元付款；3. 8,000 HKD还款；"
            "4. 2019年6月3日300,000元转账。"
        )
        core["issues"][0]["strengths"] = [
            "银行流水记载200,000元，需与诉请逐项核验。"
        ]

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )
        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        candidate = parse_lawyer_decision_package_candidate(payload)
        bindings = candidate["numeric_fact_bindings"]
        self.assertEqual(
            [item["canonical"] for item in bindings],
            [
                "amount:CNY:200000",
                "amount:CNY:205000",
                "amount:CNY:200000",
                "date:2022-09-10",
                "amount:CNY:50000",
                "amount:HKD:8000",
                "date:2019-06-03",
                "amount:CNY:300000",
            ],
        )
        self.assertEqual(
            bindings[0]["path"], "/issues/0/strengths/0"
        )
        self.assertEqual(
            bindings[1]["path"], "/executive_assessment/working_direction"
        )
        self.assertTrue(all(item["source_refs"] for item in bindings))

        tampered = json.loads(payload)
        tampered["numeric_fact_bindings"][0]["source_refs"] = []
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_decision_package_candidate(
                json.dumps(
                    tampered,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )

    def test_rejects_unbound_or_calculated_numeric_text(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["working_direction"] = "建议核验205,001元诉请构成。"
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

        core["working_direction"] = "建议核验205,000元+200,000元=405,000元。"
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_redacts_only_source_derived_difference_for_lawyer_review(self) -> None:
        projection = _numeric_projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        core = _valid_core(projection)
        issue = core["issues"][0]
        issue["supporting_source_refs"] = [contract.issue_ids[0]]
        issue["adverse_source_refs"] = [contract.source_ids[1]]
        issue["missing_evidence"] = ["解释5,000元差额的书面说明或原始凭证"]
        adversarial = core["adversarial_analysis"][0]
        adversarial["source_refs"] = [contract.issue_ids[0], contract.source_ids[1]]
        adversarial["rebuttal_route"] = (
            "诉请与银行流水存在5,000元差异，应逐项核验差额来源。"
        )

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )
        self.assertEqual(
            [item["path"] for item in parsed.model_output_normalizations],
            [
                "/issues/0/missing_evidence/0",
                "/adversarial_analysis/0/rebuttal_route",
            ],
        )
        self.assertNotIn("5,000", parsed.core["issues"][0]["missing_evidence"][0])
        self.assertNotIn(
            "5,000", parsed.core["adversarial_analysis"][0]["rebuttal_route"]
        )

        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        candidate = parse_lawyer_decision_package_candidate(payload)
        normalization = candidate["model_output_normalization"]
        self.assertEqual(
            normalization["status"], "REDACTED_SOURCE_DERIVED_DIFFERENCE"
        )
        self.assertEqual(len(normalization["items"]), 2)
        self.assertTrue(
            all(len(item["source_refs"]) == 2 for item in normalization["items"])
        )

        tampered = json.loads(payload)
        tampered["model_output_normalization"]["items"][0]["source_refs"] = []
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_decision_package_candidate(
                json.dumps(
                    tampered,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )

    def test_difference_redaction_rejects_unproven_or_non_difference_number(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        issue = core["issues"][0]
        issue["supporting_source_refs"] = [contract.issue_ids[0]]
        issue["adverse_source_refs"] = [contract.source_ids[1]]
        issue["missing_evidence"] = ["解释6,000元差额的书面说明"]
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_redacts_proven_difference_when_label_precedes_amount(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        issue = core["issues"][0]
        issue["supporting_source_refs"] = [contract.issue_ids[0]]
        issue["adverse_source_refs"] = [contract.source_ids[1]]
        issue["missing_evidence"] = ["解释差额5,000元的书面说明或原始凭证"]

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )

        self.assertEqual(
            [item["path"] for item in parsed.model_output_normalizations],
            ["/issues/0/missing_evidence/0"],
        )
        self.assertNotIn("5,000", parsed.core["issues"][0]["missing_evidence"][0])

    def test_reverse_difference_label_does_not_allow_unproven_amount(self) -> None:
        projection = _numeric_projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        issue = core["issues"][0]
        issue["supporting_source_refs"] = [contract.issue_ids[0]]
        issue["adverse_source_refs"] = [contract.source_ids[1]]
        issue["missing_evidence"] = ["解释差额6,000元的书面说明"]

        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_redacts_proven_difference_from_two_facts_in_one_source(self) -> None:
        base = _numeric_projection()
        sources = list(base.sources)
        sources[1] = replace(
            sources[1],
            primary_text="同一核验来源同时载明205,000.00元诉请与200,000.00元流水交付",
        )
        sources[2] = replace(sources[2], primary_text="交付范围与诉请范围是否一致")
        projection = BoundCaseContextProjection.build(
            run_id=base.run_id,
            task_id=base.task_id,
            task_input_hash=base.task_input_hash,
            firm_id=base.firm_id,
            matter_id=base.matter_id,
            matter_version=base.matter_version,
            case_snapshot_hash=base.case_snapshot_hash,
            input_refs=base.input_refs,
            sources=tuple(sources),
        )
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        core = _valid_core(projection)
        issue = core["issues"][0]
        issue["supporting_source_refs"] = [contract.issue_ids[0]]
        issue["adverse_source_refs"] = [contract.source_ids[1]]
        issue["missing_evidence"] = ["说明差额5,000元的原始凭证"]

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )
        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        candidate = parse_lawyer_decision_package_candidate(payload)

        self.assertEqual(
            candidate["model_output_normalization"]["items"][0]["source_refs"],
            [contract.source_ids[1]],
        )

        core["issues"][0]["missing_evidence"] = ["要求支付5,000元的书面说明"]
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_compiler_injects_only_required_server_source_anchors(self) -> None:
        projection = _projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        core = _valid_core(projection)
        selected = contract.source_ids[-2]
        for row in core["issues"]:
            row["supporting_source_refs"] = [selected]
        for row in core["adversarial_analysis"]:
            row["source_refs"] = [selected]
        for row in core["decision_analysis"]:
            row["source_refs"] = [selected]

        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(core), contract=contract
        )
        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        candidate = parse_lawyer_decision_package_candidate(payload)
        claim_position = next(
            item
            for item in contract.position_register
            if item["status"] == "ASSERTED_SOURCE_POSITION"
        )

        self.assertNotIn(
            contract.issue_ids[0], parsed.core["issues"][0]["supporting_source_refs"]
        )
        for row in candidate["issues"]:
            self.assertIn(row["issue_ref"], row["supporting_source_refs"])
        for row in candidate["adversarial_analysis"]:
            self.assertIn(row["issue_ref"], row["source_refs"])
            self.assertTrue(
                set(claim_position["source_refs"]).issubset(row["source_refs"])
            )
        for row in candidate["decision_requests"]:
            self.assertIn(row["issue_ref"], row["source_refs"])

    def test_known_usage_cost_survives_rejected_model_semantics(self) -> None:
        projection = _projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["case_posture"] = "存在3项待核验问题"
        response = _provider_response(core)

        self.assertEqual(known_lawyer_analysis_response_cost_minor_units(response), 5)
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(response, contract=contract)

    def test_compiles_review_only_source_bound_decision_package(self) -> None:
        projection = _projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(_valid_core(projection)), contract=contract
        )
        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        value = parse_lawyer_decision_package_candidate(payload)
        self.assertEqual(value["schema_version"], LAWYER_DECISION_PACKAGE_SCHEMA)
        self.assertFalse(value["court_ready"])
        self.assertFalse(value["legal_conclusion"])
        self.assertFalse(value["official_numeric_result_authored_by_model"])
        self.assertEqual(
            lawyer_decision_package_source_refs(payload),
            frozenset(projection.input_refs),
        )
        self.assertTrue(value["decision_requests"])
        self.assertTrue(
            all(
                item["disposition"] == "REQUIRES_LAWYER"
                for item in value["decision_requests"]
            )
        )

    def test_candidate_parser_rejects_inner_field_or_compiler_control_drift(self) -> None:
        projection = _projection()
        contract, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        parsed = parse_lawyer_analysis_provider_response(
            _provider_response(_valid_core(projection)), contract=contract
        )
        payload = compile_lawyer_decision_package_candidate(
            projection=projection,
            contract=contract,
            parsed=parsed,
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        base = json.loads(payload)
        mutations: list[dict[str, object]] = []

        changed = json.loads(payload)
        changed["issues"][0]["model_score"] = "high"
        mutations.append(changed)

        changed = json.loads(payload)
        changed["issues"][0]["formal_legal_conclusion"] = True
        mutations.append(changed)

        changed = json.loads(payload)
        changed["action_plan"][0]["owner"] = "Agent"
        mutations.append(changed)

        changed = json.loads(payload)
        changed["source_catalog"][0]["signals"] = ["Z", "A"]
        mutations.append(changed)

        changed = json.loads(payload)
        changed["provider_receipt"]["cost_minor_units"] += 1
        mutations.append(changed)

        changed = json.loads(payload)
        changed["executive_assessment"]["top_risk_issue_refs"] = list(
            reversed(changed["executive_assessment"]["top_risk_issue_refs"])
        )
        if (
            changed["executive_assessment"]["top_risk_issue_refs"]
            == base["executive_assessment"]["top_risk_issue_refs"]
        ):
            changed["executive_assessment"]["top_risk_issue_refs"] = []
        mutations.append(changed)

        for changed in mutations:
            encoded = json.dumps(
                changed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            with self.subTest(keys=tuple(changed)):
                with self.assertRaises(LawyerAnalysisBlocked):
                    parse_lawyer_decision_package_candidate(encoded)

    def test_rejects_unknown_or_mismatched_source_reference(self) -> None:
        projection = _projection()
        contract = build_lawyer_analysis_contract(projection)
        core = _valid_core(projection)
        core["issues"][0]["supporting_source_refs"] = [
            "issue:00000000-0000-4000-8000-999999999999"
        ]
        with self.assertRaises(LawyerAnalysisBlocked):
            parse_lawyer_analysis_provider_response(
                _provider_response(core), contract=contract
            )

    def test_official_price_ceiling_is_rounded_up_to_fen(self) -> None:
        self.assertEqual(price_qwen37_minor_units(22_224, 131_072), 110)
        self.assertEqual(price_qwen37_minor_units(12_000, 3_000), 5)

    def test_managed_adapter_crosses_one_boundary_and_stages_candidate(self) -> None:
        projection = _projection()
        context = _Context(projection)
        staging = _Staging()
        exchange = _Exchange(_provider_response(_valid_core(projection)))
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=exchange,
            staging_port=staging,
        )
        outcome = adapter.execute(context=context)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.SUBMITTED
        )
        self.assertEqual(outcome.external_calls, 1)
        self.assertEqual(outcome.cost_minor_units, 5)
        self.assertEqual(len(outcome.artifacts), 1)
        self.assertEqual(
            outcome.artifacts[0].artifact_kind,
            "LAWYER_DECISION_PACKAGE_CANDIDATE",
        )
        self.assertEqual(context.external_request["destination"], HOST)
        value = parse_lawyer_decision_package_candidate(staging.request.payload)
        self.assertEqual(
            value["external_request_id"], context.external_request_id
        )

    def test_managed_adapter_records_known_pre_dispatch_failure(self) -> None:
        projection = _projection()
        context = _Context(projection)
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(
                LawyerAnalysisKnownFailure(
                    LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED
                )
            ),
            staging_port=_Staging(),
        )
        outcome = adapter.execute(context=context)
        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state,
            ExternalSubmissionState.NOT_SUBMITTED,
        )
        self.assertEqual(outcome.external_calls, 0)

    def test_managed_adapter_preserves_controlled_unknown_phase(self) -> None:
        projection = _projection()
        context = _Context(projection)
        _, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=projection.task_id,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(
                LawyerAnalysisUnknownSubmission(
                    external_request_id=request.external_request_id,
                    error_code="LAWYER_ANALYSIS_UNKNOWN_RESPONSE_HEAD",
                )
            ),
            staging_port=_Staging(),
        )

        outcome = adapter.execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.UNKNOWN)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.UNKNOWN
        )
        self.assertEqual(
            outcome.error_code, "LAWYER_ANALYSIS_UNKNOWN_RESPONSE_HEAD"
        )
        self.assertEqual(outcome.external_calls, 1)

    def test_managed_adapter_treats_connect_failure_as_zero_call(self) -> None:
        projection = _projection()
        context = _Context(projection)
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(
                LawyerAnalysisKnownFailure(LAWYER_ANALYSIS_CONNECT_FAILED)
            ),
            staging_port=_Staging(),
        )

        outcome = adapter.execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state,
            ExternalSubmissionState.NOT_SUBMITTED,
        )
        self.assertEqual(outcome.error_code, LAWYER_ANALYSIS_CONNECT_FAILED)
        self.assertEqual(outcome.external_calls, 0)

    def test_managed_adapter_rejects_bad_provider_output_as_known_failure(self) -> None:
        projection = _projection()
        context = _Context(projection)
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(b'{"unexpected":true}'),
            staging_port=_Staging(),
        )

        outcome = adapter.execute(context=context)

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.SUBMITTED
        )
        self.assertEqual(outcome.error_code, "LAWYER_ANALYSIS_OUTPUT_REJECTED")
        self.assertEqual(outcome.external_calls, 1)
        self.assertEqual(outcome.cost_minor_units, 0)

    def test_managed_adapter_records_known_cost_for_rejected_semantics(self) -> None:
        projection = _projection()
        core = _valid_core(projection)
        core["case_posture"] = "存在3项待核验问题"
        adapter = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=_Exchange(_provider_response(core)),
            staging_port=_Staging(),
        )

        outcome = adapter.execute(context=_Context(projection))

        self.assertEqual(outcome.status, ResultStatus.FAILED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.SUBMITTED
        )
        self.assertEqual(outcome.error_code, "LAWYER_ANALYSIS_OUTPUT_REJECTED")
        self.assertEqual(outcome.external_calls, 1)
        self.assertEqual(outcome.cost_minor_units, 5)

    def test_reconciliation_is_lookup_only(self) -> None:
        projection = _projection()
        context = _Context(projection)
        exchange = _Exchange(AssertionError("send must not run"))
        _, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=projection.task_id,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        context.claim.reconciliation = True
        context.external_request_id = request.external_request_id
        exchange.recovered = RecoveredLawyerAnalysis(
            status="SUCCEEDED",
            response_body=_provider_response(_valid_core(projection)),
            request_hash=request.request_hash,
        )
        outcome = QwenLawyerAnalysisTaskAdapter(
            projection_port=_ProjectionPort(projection),
            exchange=exchange,
            staging_port=_Staging(),
        ).reconcile(context=context)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(exchange.requests, [])
        self.assertEqual(len(exchange.recovery_bindings), 1)

    def test_unresolved_reconciliation_defers_without_resubmission(self) -> None:
        projection = _projection()
        context = _Context(projection)
        exchange = _Exchange(AssertionError("send must not run"))
        _, request = prepare_lawyer_analysis_request(
            projection=projection,
            task_id=projection.task_id,
            attempt_id=ATTEMPT_ID,
            endpoint_host=HOST,
        )
        context.claim.reconciliation = True
        context.external_request_id = request.external_request_id
        exchange.recovered = RecoveredLawyerAnalysis(status="UNRESOLVED")

        with self.assertRaisesRegex(
            CaseAgentReconciliationUnavailable,
            "external result remains unavailable",
        ) as raised:
            QwenLawyerAnalysisTaskAdapter(
                projection_port=_ProjectionPort(projection),
                exchange=exchange,
                staging_port=_Staging(),
            ).reconcile(context=context)

        self.assertEqual(
            raised.exception.reason_code,
            "LAWYER_ANALYSIS_RECOVERY_UNRESOLVED",
        )
        self.assertEqual(exchange.requests, [])
        self.assertEqual(len(exchange.recovery_bindings), 1)
        self.assertIsInstance(
            exchange.recovery_bindings[0], LawyerAnalysisRecoveryBinding
        )

    def test_unresolved_reconciliation_does_not_rebuild_current_prompt(self) -> None:
        projection = _projection()
        context = _Context(projection)
        exchange = _Exchange(AssertionError("send must not run"))
        context.claim.reconciliation = True
        context.external_request_id = "00000000-0000-4000-8000-000000000999"
        exchange.recovered = RecoveredLawyerAnalysis(status="UNRESOLVED")
        projection_port = _ProjectionPort(projection)

        with self.assertRaises(CaseAgentReconciliationUnavailable):
            QwenLawyerAnalysisTaskAdapter(
                projection_port=projection_port,
                exchange=exchange,
                staging_port=_Staging(),
            ).reconcile(context=context)

        self.assertEqual(projection_port.calls, [])
        self.assertEqual(exchange.requests, [])
        self.assertEqual(
            exchange.recovery_bindings[0].external_request_id,
            context.external_request_id,
        )


if __name__ == "__main__":
    unittest.main()
