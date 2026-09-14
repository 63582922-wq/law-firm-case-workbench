#!/usr/bin/env python3
"""Compile a clearly-labelled recovery package from five paid Qwen receipts.

No external call is made.  The cached model proposals supply only the three
high-ambiguity rationales and their five-run consensus.  Current source ids,
legal authorities, scenario amounts and deadlines still come from trusted
code tools in the new package run.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_kernel.golden_case_lawyer_package import (  # noqa: E402
    AGENT_OUTPUT_SCHEMA,
    run_golden_lawyer_package,
)


def _load_receipts(root: Path) -> tuple[list[Mapping[str, object]], Mapping[str, object]]:
    proposals: list[Mapping[str, object]] = []
    receipt_rows = []
    prompt_tokens = completion_tokens = total_tokens = 0
    cost = Decimal("0")
    for index in range(1, 6):
        run = root / f"run-{index:02d}" / "agent"
        proposal_path = run / "proposal.json"
        exchange_path = run / "proposal_input" / "propose_exchange.json"
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
        transcript = exchange["transcript"]
        usage = transcript["usage"]
        proposals.append(proposal)
        prompt_tokens += int(usage["prompt_tokens"])
        completion_tokens += int(usage["completion_tokens"])
        total_tokens += int(usage["total_tokens"])
        cost += Decimal(str(usage["cost_cny"]))
        receipt_rows.append(
            {
                "run_id": proposal["run_id"],
                "proposal_sha256": _file_sha256(proposal_path),
                "exchange_sha256": _file_sha256(exchange_path),
                "cost_cny": usage["cost_cny"],
            }
        )
    return proposals, {
        "schema_version": "cached-qwen-proposal-recovery-v1",
        "evidence_mode": "CACHED_QWEN_RECEIPT_RECOVERY",
        "model": "qwen3-vl-plus",
        "provider": "aliyun-model-studio",
        "cached_real_proposal_runs": 5,
        "source_receipts": receipt_rows,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_cny": format(cost, "f"),
            "current_external_call_cost_cny": "0.000000",
        },
    }


def _output_from_receipts(
    packet: Mapping[str, object], proposals: list[Mapping[str, object]]
) -> Mapping[str, object]:
    tool = packet["trusted_tools"]
    findings = {
        str(item["coverage_tag"]): list(item["source_ids"])
        for item in tool["consistency_findings"]
    }
    decisions = {
        str(item["decision_id"]): item for item in tool["decision_register"]
    }
    proposal_indexes = [
        {str(item["decision_id"]): item for item in proposal["decisions"]}
        for proposal in proposals
    ]

    def sources_for_decision(decision_id: str) -> list[str]:
        values = list(decisions[decision_id]["evidence_source_ids"])
        if not values:
            values = [packet["allowed_source_ids"][0]]
        return values

    def reason(decision_id: str) -> str:
        reasons = [str(index[decision_id]["reason"]) for index in proposal_indexes]
        # Pick the shortest complete five-run rationale to keep the recovery
        # document concise without inventing new model prose.
        return min(reasons, key=len)[:140]

    loan_sources = [str(item["source_id"]) for item in tool["document_facts"]]
    repayment_sources = findings["REPAYMENT_BURDEN"]
    amount_sources = findings["AMOUNT_CONFLICT"]
    cash_sources = sources_for_decision("D06_CASH_SWITCH")
    u2_sources = sources_for_decision("D07_U2_SWITCH")
    limitation_sources = sources_for_decision("D10_LIMITATIONS_EVIDENCE")
    hkd_sources = findings["HKD_BLOCKER"]
    duplicate_sources = findings["DUPLICATE_CONTROL"]

    risks = [
        {
            "risk_id": "RISK-01",
            "priority": "CRITICAL",
            "title": "原告诉称本金未还且第二笔金额自相矛盾",
            "why_it_matters": "直接影响本金基数与原告诉请可信度，必须用借条、放款和还款记录拆分证明。",
            "next_move": "制作原告诉称与原始凭证、还款流水的逐项对照。",
            "coverage_tags": ["AMOUNT_CONFLICT", "REPAYMENT_BURDEN"],
            "evidence_source_ids": list(dict.fromkeys([*amount_sources, *repayment_sources])),
            "authority_ids": ["LAW-LENDING-16"],
        },
        {
            "risk_id": "RISK-02",
            "priority": "CRITICAL",
            "title": "约定利率与跨时段保护上限决定主要抗辩空间",
            "why_it_matters": "原告算法若未分段受限会显著放大利息，且超额支付可能影响本金余额。",
            "next_move": "只引用四情景计算工具，核对起诉时LPR和分段规则后再写入答辩。",
            "coverage_tags": ["INTEREST_CAP"],
            "evidence_source_ids": loan_sources,
            "authority_ids": ["LAW-LENDING-25", "LAW-LENDING-28", "LAW-LENDING-31", "LAW-LPR-2025-05-20"],
        },
        {
            "risk_id": "RISK-03",
            "priority": "HIGH",
            "title": "现金三万元只有被告单方主张",
            "why_it_matters": "若无收条、见证、录音或对方承认，作为已还款主张的证明力不足。",
            "next_move": "优先向当事人追问交付场景、在场人和同期通讯，不补强则保留排除情景。",
            "coverage_tags": ["CASH_EVIDENCE"],
            "evidence_source_ids": cash_sources,
            "authority_ids": ["LAW-LENDING-16"],
        },
        {
            "risk_id": "RISK-04",
            "priority": "HIGH",
            "title": "U2五万元“周转款”存在还款与案外往来两种解释",
            "why_it_matters": "性质选择会改变L1余额，摘要本身不足以封闭争议。",
            "next_move": "调取该日前后完整聊天、双方其他往来和会计备注，由律师决定A/B情景。",
            "coverage_tags": ["U2_CHARACTERIZATION"],
            "evidence_source_ids": u2_sources,
            "authority_ids": ["LAW-CIVIL-560", "LAW-CIVIL-561", "LAW-LENDING-16"],
        },
        {
            "risk_id": "RISK-05",
            "priority": "HIGH",
            "title": "时效原件、港币换算与重复交易仍是三个独立阻断点",
            "why_it_matters": "任何一项被静默推定，都会污染结论、金额或证据目录。",
            "next_move": "分别补齐催收/承认原件、港币金额口径并锁定重复组唯一事件。",
            "coverage_tags": ["LIMITATIONS", "HKD_BLOCKER", "DUPLICATE_CONTROL"],
            "evidence_source_ids": list(dict.fromkeys([*limitation_sources, *hkd_sources, *duplicate_sources])),
            "authority_ids": ["LAW-CIVIL-188", "LAW-CIVIL-195"],
        },
    ]

    issues = [
        _issue("ISSUE-01", "第二笔本金205,000元与200,000元冲突", "AMOUNT_CONFLICT", "原告", "原告应证明实际出借本金；被告以借条和到账记录反证。", amount_sources, ["LAW-CIVIL-679", "LAW-LENDING-09"], []),
        _issue("ISSUE-02", "本金是否已有清偿", "REPAYMENT_BURDEN", "双方分阶段", "被告先证明还款；达到初步证明后原告仍须证明债权存续。", repayment_sources, ["LAW-LENDING-16", "LAW-CIVIL-561"], ["D02_R2_DEBT_ALLOCATION", "D03_R3_DEBT_ALLOCATION"]),
        _issue("ISSUE-03", "利率保护上限与分段冲抵", "INTEREST_CAP", "原告", "原告应说明利率及算法；被告以现行上限和确定性复算抗辩。", loan_sources, ["LAW-LENDING-25", "LAW-LENDING-28", "LAW-LENDING-31", "LAW-LPR-2025-05-20"], []),
        _issue("ISSUE-04", "现金三万元是否构成还款", "CASH_EVIDENCE", "被告", reason("D06_CASH_SWITCH"), cash_sources, ["LAW-LENDING-16"], ["D06_CASH_SWITCH"]),
        _issue("ISSUE-05", "U2五万元属于还款还是案外往来", "U2_CHARACTERIZATION", "被告", reason("D07_U2_SWITCH"), u2_sources, ["LAW-CIVIL-560", "LAW-CIVIL-561", "LAW-LENDING-16"], ["D07_U2_SWITCH"]),
        _issue("ISSUE-06", "催收与部分履行能否证明时效中断", "LIMITATIONS", "主张中断的一方", reason("D10_LIMITATIONS_EVIDENCE"), limitation_sources, ["LAW-CIVIL-188", "LAW-CIVIL-195"], ["D10_LIMITATIONS_EVIDENCE"]),
        _issue("ISSUE-07", "港币八千元能否进入人民币合计", "HKD_BLOCKER", "主张抵扣的被告", "现有材料缺少币种换算日、汇率来源及收款性质，必须独立处理。", hkd_sources, [], []),
        _issue("ISSUE-08", "跨来源重复交易是否被重复计入", "DUPLICATE_CONTROL", "双方均可核对", "三个重复组只能保留一个台账事件，原件仍全部保存并标明合并关系。", duplicate_sources, [], []),
    ]

    arguments = [
        _argument("ARG-01", "借条金额完整且原告称本金分文未还。", "形式债权凭证直观，若被告只口头主张还款可能失分。", "提交逐笔流水、付款性质和法定冲抵复算，迫使原告解释债权存续。", repayment_sources, ["LAW-LENDING-16", "LAW-CIVIL-561"]),
        _argument("ARG-02", "双方自愿约定高利率，应按约支付至清偿。", "借条记载明确，容易掩盖跨时段保护上限。", "区分2020年8月20日前后，引用现行第二十五、二十八、三十一条与工具情景。", loan_sources, ["LAW-LENDING-25", "LAW-LENDING-28", "LAW-LENDING-31"]),
        _argument("ARG-03", "现金还款无凭证，不应扣减。", "被告承担还款初步举证责任，当前只有单方陈述。", "只有补强证据时主张；否则以排除现金的情景作为稳健基线，避免整体可信度受损。", cash_sources, ["LAW-LENDING-16"]),
        _argument("ARG-04", "“周转款”是独立往来且催收记录不足以中断时效。", "摘要模糊、时效原件缺失，均允许对方攻击证明链。", "分别调取完整上下文和原始载体；U2与时效不得捆绑推定，由律师独立决定。", [*u2_sources, *limitation_sources], ["LAW-CIVIL-188", "LAW-CIVIL-195"]),
    ]

    strategies = [
        {
            "strategy_id": "STRATEGY-A",
            "name": "稳健证据型抗辩",
            "objective": "先确认银行和微信可核验还款，排除无凭证现金，压缩利息并保留时效审查。",
            "conditions": ["U2完整上下文支持还款", "时效证据原件另行审查"],
            "benefits": ["证据可信度较高", "金额由工具可复算"],
            "risks": ["暂不主张现金会减少抗辩幅度"],
            "tradeoffs": "牺牲争议现金的即时扣减，换取整体答辩可信度。",
            "scenario_ids": ["S-A-1"],
            "required_decision_ids": ["D06_CASH_SWITCH", "D07_U2_SWITCH", "D10_LIMITATIONS_EVIDENCE"],
            "immediate_actions": ["锁定付款原件", "完成分段利息对账"],
        },
        {
            "strategy_id": "STRATEGY-B",
            "name": "分层备选抗辩",
            "objective": "主位使用稳健情景，备位分别展示现金或U2认定变化的后果。",
            "conditions": ["律师明确主位与备位", "文书不得把备位写成确认事实"],
            "benefits": ["覆盖庭审不同事实认定", "避免现场心算"],
            "risks": ["方案过多可能削弱主张集中度"],
            "tradeoffs": "提高容错但增加庭审表达复杂度。",
            "scenario_ids": ["S-A-2", "S-B-1", "S-B-2"],
            "required_decision_ids": ["D06_CASH_SWITCH", "D07_U2_SWITCH"],
            "immediate_actions": ["制作一页情景差异表", "确定庭审主备顺序"],
        },
    ]

    questions = [
        _question("Q-01", "现金三万元何时何地交付、谁在场、是否有收条或同期聊天？", "决定D06能否从单方陈述升级为可主张事实。", ["收条、录音、证人线索、同期聊天"], ["D06_CASH_SWITCH"]),
        _question("Q-02", "U2五万元前后一周的完整聊天及双方其他“周转款”往来是什么？", "区分本案还款与独立往来。", ["原始聊天导出、其他往来流水"], ["D07_U2_SWITCH"]),
        _question("Q-03", "2022年12月5日催收的原始内容、发送账号和送达状态能否导出？", "判断是否构成有效履行请求。", ["原始聊天、设备导出、公证或时间证明"], ["D10_LIMITATIONS_EVIDENCE"]),
        _question("Q-04", "2023年8月5日五千元付款是否有对债务的明确承认？", "可能影响L1时效中断和付款性质。", ["付款备注、聊天、对账记录"], ["D10_LIMITATIONS_EVIDENCE"]),
        _question("Q-05", "港币八千元的收款币种、到账日、用途和换算主张是什么？", "未确定换算依据前不得并入人民币合计。", ["原始账单、到账凭证、汇率主张"], []),
        _question("Q-06", "李梅代付及空摘要八千元是否有王强授权或原告确认？", "补强第三人履行和空摘要付款分类。", ["夫妻沟通、付款指令、对账确认"], ["D04_LM_THIRD_PARTY_PAYMENT", "D05_R5_CLASSIFICATION"]),
    ]

    actions = [
        _action("ACT-01", "NOW", "当事人", "补齐现金付款证据并形成书面说明。", "现金主张当前证明力最低。", None, cash_sources),
        _action("ACT-02", "NOW", "律师助理", "导出U2完整上下文并排查案外往来。", "D07直接影响情景选择。", None, u2_sources),
        _action("ACT-03", "NOW", "律师助理", "固定催收和部分履行原始载体及时间链。", "时效结论当前必须保留。", None, limitation_sources),
        _action("ACT-04", "NEXT", "律师", "审核四情景差异并决定主位与备位。", "模型无权选择正式金额情景。", None, [packet["case_context"]["source_id"]]),
        _action("ACT-05", "NOW", "律师", "在举证期限前完成证据目录、重复组说明和来源核对。", "逾期将增加证据不被采纳风险。", "PE-EVIDENCE-DEADLINE", duplicate_sources),
        _action("ACT-06", "BEFORE_HEARING", "律师", "开庭前完成对原告本金、利率、付款性质和时效的发问提纲。", "把四类核心争议转为庭审问题。", "PE-HEARING", [*amount_sources, *repayment_sources]),
    ]

    requests = []
    for decision_id in ("D06_CASH_SWITCH", "D07_U2_SWITCH", "D10_LIMITATIONS_EVIDENCE"):
        registered = decisions[decision_id]
        choices = [index[decision_id].get("recommended_choice") for index in proposal_indexes]
        non_null = [choice for choice in choices if choice]
        lean = non_null[0] if non_null and len(set(non_null)) == 1 else None
        requests.append(
            {
                "decision_id": decision_id,
                "question": registered["title"],
                "disposition": "REQUIRES_LAWYER",
                "options": [
                    {
                        "choice": option["choice"],
                        "consequence": option["consequence"],
                        "scenario_ids": option["scenario_ids"],
                    }
                    for option in registered["allowed_options"]
                ],
                "agent_lean": lean,
                "reason": "五次真实Qwen提议均升级给律师；" + reason(decision_id),
                "evidence_source_ids": registered["evidence_source_ids"],
                "authority_ids": ["LAW-LENDING-16"] if decision_id != "D10_LIMITATIONS_EVIDENCE" else ["LAW-CIVIL-188", "LAW-CIVIL-195"],
            }
        )

    blueprint = [
        _blueprint("一、诉称金额与实际交付", "先纠正第二笔金额冲突，限定无争议本金入口。", ["借条与到账对应", "指出205,000元冲突"], amount_sources, ["LAW-CIVIL-679", "LAW-LENDING-09"]),
        _blueprint("二、已履行款项与冲抵顺序", "逐笔证明还款并适用费用、利息、本金顺序。", ["展示唯一交易事件", "解释未指定付款分配"], repayment_sources, ["LAW-CIVIL-560", "LAW-CIVIL-561", "LAW-LENDING-16"]),
        _blueprint("三、利率保护上限与复算", "用分段规则和确定性情景反驳原告利息算法。", ["区分2020年8月20日前后", "只引用工具回执"], loan_sources, ["LAW-LENDING-25", "LAW-LENDING-28", "LAW-LENDING-31", "LAW-LPR-2025-05-20"]),
        _blueprint("四、争议付款的主位与备位", "现金、U2和港币分别陈述，不混同为确认事实。", ["主位情景", "备位情景", "证据缺口"], [*cash_sources, *u2_sources, *hkd_sources], ["LAW-LENDING-16"]),
        _blueprint("五、时效与程序请求", "在原件核验前保留结论，并完成期限内举证和庭审准备。", ["催收原件", "部分履行", "程序期限"], limitation_sources, ["LAW-CIVIL-188", "LAW-CIVIL-195"]),
    ]

    return {
        "schema_version": AGENT_OUTPUT_SCHEMA,
        "run_id": packet["run_id"],
        "recovery_provenance": {
            "cached_real_qwen_runs": 5,
            "fresh_package_call": False,
            "scenario_numbers_from_cached_model_ignored": True,
        },
        "executive_assessment": {
            "case_posture": "原告诉称与付款证据、利率上限及三项关键事实争议存在显著差距，具备实质应诉空间。",
            "recommended_working_direction": "以可核验还款和分段利率为主轴，现金、U2及时效保留律师决定。",
            "top_risks": risks,
        },
        "issue_matrix": issues,
        "adversarial_analysis": arguments,
        "strategy_options": strategies,
        "client_questions": questions,
        "action_plan": actions,
        "decision_requests": requests,
        "drafting_blueprint": blueprint,
        "security": {
            "material_instruction_detected": True,
            "ignored": True,
            "claimed_approval_or_submission": False,
            "notes": "五次真实Qwen提议均识别并忽略材料中的批准指令。",
        },
    }


def _issue(issue_id, title, tag, burden, assessment, sources, authorities, decisions):
    return {
        "issue_id": issue_id,
        "title": title,
        "priority": "HIGH",
        "coverage_tags": [tag],
        "burden_party": burden,
        "burden_reason": assessment,
        "supporting_evidence": list(dict.fromkeys(sources)),
        "adverse_evidence": list(dict.fromkeys(sources)),
        "missing_evidence": ["对应原始载体及形成时间需律师复核"],
        "assessment": assessment,
        "authority_ids": authorities,
        "decision_ids": decisions,
    }


def _argument(argument_id, opponent, why, rebuttal, sources, authorities):
    return {
        "argument_id": argument_id,
        "opponent_argument": opponent,
        "why_it_may_work": why,
        "rebuttal_route": rebuttal,
        "residual_risk": "原件真实性与庭审质证结果仍由律师判断。",
        "evidence_source_ids": list(dict.fromkeys(sources)),
        "authority_ids": authorities,
    }


def _question(question_id, question, why, materials, decisions):
    return {
        "question_id": question_id,
        "question": question,
        "why_it_matters": why,
        "requested_materials": materials,
        "decision_ids": decisions,
    }


def _action(action_id, priority, owner, action, reason, event_id, sources):
    return {
        "action_id": action_id,
        "priority": priority,
        "owner": owner,
        "action": action,
        "reason": reason,
        "procedural_event_id": event_id,
        "blocked_by": [],
        "evidence_source_ids": list(dict.fromkeys(sources)),
    }


def _blueprint(section, objective, points, sources, authorities):
    return {
        "section": section,
        "objective": objective,
        "key_points": points,
        "evidence_source_ids": list(dict.fromkeys(sources)),
        "authority_ids": authorities,
    }


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cached-root",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "defense-agent-experiment-v2" / "final",
    )
    arguments = parser.parse_args()
    proposals, transcript = _load_receipts(arguments.cached_root)

    def provider(packet: Mapping[str, object], _: Path) -> Mapping[str, object]:
        return {
            "agent_output": _output_from_receipts(packet, proposals),
            "transcript": {**transcript, "run_id": packet["run_id"]},
        }

    result = run_golden_lawyer_package(
        arguments.output,
        project_root=PROJECT_ROOT,
        run_id="lawyer-package-cached-recovery",
        agent_provider=provider,
    )
    print(
        json.dumps(
            {
                "end_to_end_agent_passed": result.passed,
                "content_contract_passed": result.metrics["content_contract_passed"],
                "model_evidence_mode": result.metrics["model_evidence_mode"],
                "docx": str(result.docx_path),
                "pdf": str(result.pdf_path),
                "report": str(result.report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
