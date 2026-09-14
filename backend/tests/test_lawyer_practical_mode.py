"""实用模式门禁与报告的验收测试。

覆盖三档门禁（硬红线 / 自动修复 / 标记放行）、正式数字剔除、提示词契约与报告渲染。
服务层集成测试见 test_case_analysis_service.py。
"""

from __future__ import annotations

import json
import unittest

from case_kernel.lawyer_practical_mode import (
    build_practical_prompt,
    normalize_and_gate,
    render_practical_report,
    GateDecision,
    PracticalResult,
)


class GateTests(unittest.TestCase):
    def test_number_scrubbing_replaces_official_numbers(self) -> None:
        raw = {"case_posture": {"summary": "约定月利率1.5%，已还103,000元。"},
               "issues": [{"issue": "利率上限", "our_position": "按12%上限主张"}]}
        analysis, gate = normalize_and_gate(raw, engine_amounts={"合计本金": "300000.00"})
        self.assertIn("[见计算表]", analysis["case_posture"]["summary"])
        self.assertNotIn("1.5%", json.dumps(analysis, ensure_ascii=False))
        self.assertNotIn("103,000", json.dumps(analysis, ensure_ascii=False))
        self.assertTrue(gate.repairs)

    def test_hard_redline_blocks(self) -> None:
        _, gate = normalize_and_gate({"case_posture": {"summary": "我已批准并已提交法院"}},
                                    engine_amounts={})
        self.assertEqual(gate.level, "HARD_BLOCKED")

    def test_non_json_is_repaired(self) -> None:
        text = '前言。{"schema":"x","issues":[{"issue":"A"}]} 后记。'
        analysis, gate = normalize_and_gate(text, engine_amounts={})
        self.assertEqual(gate.level, "AUTO_REPAIRED")
        self.assertEqual(len(analysis["issues"]), 1)

    def test_unparsable_marks_for_review(self) -> None:
        analysis, gate = normalize_and_gate("没有任何结构化内容", engine_amounts={})
        self.assertEqual(gate.level, "MARK_FOR_REVIEW")
        self.assertEqual(analysis, {})
        self.assertTrue(gate.review_items)

    def test_missing_fields_are_filled(self) -> None:
        analysis, gate = normalize_and_gate({"case_posture": {"summary": "x"}},
                                            engine_amounts={})
        for key in ("facts", "issues", "adversarial_analysis", "strategy_options",
                    "decision_requests", "review_notes"):
            self.assertIn(key, analysis)
        self.assertEqual(gate.level, "PASS")

    def test_uncertain_language_enters_review_queue(self) -> None:
        raw = {"issues": [{"issue": "该笔性质无法确定，需核实"}]}
        _, gate = normalize_and_gate(raw, engine_amounts={})
        self.assertEqual(gate.level, "MARK_FOR_REVIEW")
        self.assertTrue(any("需律师确认" in item for item in gate.review_items))


class PromptAndReportTests(unittest.TestCase):
    def test_prompt_forbids_model_authored_numbers(self) -> None:
        prompt = build_practical_prompt(
            case_number="（2026）测试号", role="被告", stage="一审应诉",
            surface="### FILE=起诉状.pdf PAGE=1\n内容",
            engine_amounts={"合计本金": "300000.00"},
            trusted_authorities=["LAW-CIVIL-679"],
        )
        self.assertIn("不要写具体金额、利率百分比或计算结果", prompt)
        self.assertIn("300000.00", prompt)  # 数字只作为背景告知
        self.assertIn("LAW-CIVIL-679", prompt)

    def test_report_renders_all_sections(self) -> None:
        analysis = {
            "case_posture": {"summary": "立场概述"},
            "facts": [{"fact": "事实一", "source": "起诉状.pdf 第1页"}],
            "issues": [{"issue": "争点一", "why_it_matters": "重要",
                        "our_position": "立场", "evidence": ["起诉状.pdf 第1页"]}],
            "adversarial_analysis": [{"opponent_argument": "对方主张",
                                      "rebuttal_route": "反驳", "authority": "LAW-X",
                                      "residual_risk": "风险"}],
            "strategy_options": [{"option": "策略一", "pros": "优", "cons": "劣"}],
            "decision_requests": [{"question": "问题一", "options": ["A", "B"]}],
            "review_notes": ["注意一"],
        }
        gate = GateDecision(level="MARK_FOR_REVIEW", review_items=["待确认项"])
        result = PracticalResult(gate=gate, analysis=analysis,
                                 engine_amounts={"合计本金": "300000.00"}, review_queue=[])
        report = render_practical_report(case_number="（2026）测试号", role="被告",
                                         result=result, proposal_source="qwen（真实调用）")
        for heading in ("一、案情与立场", "二、已确认事实", "三、争点矩阵", "四、对抗分析",
                        "五、策略选项", "六、需要律师决定", "七、正式数字", "八、待律师确认清单"):
            self.assertIn(heading, report)
        self.assertIn("律师复核候选", report)


class FakeTransport:
    """确定性替身：OCR 返回固定文本，分析返回可解析的合法结构。"""

    def __init__(self, *, analysis: dict | None = None) -> None:
        self.calls: list[str] = []
        self._analysis = analysis or {
            "schema": "lawyer-practical-analysis-v1",
            "case_posture": {"summary": "被告主张已超额支付利息，约定月利率1.5%超过上限。"},
            "facts": [{"fact": "2019-10-19 归还本金", "source": "起诉状.pdf 第1页"}],
            "issues": [{"issue": "利率上限", "why_it_matters": "影响利息",
                        "our_position": "按起诉时上限", "evidence": ["起诉状.pdf 第1页"]}],
            "review_notes": ["微信凭证仅供参考，建议调取后台数据"],
        }

    def ocr_pages(self, pages, authorized, ledger):
        self.calls.append("ocr")
        return []

    def call_analysis(self, *, instruction, ledger, purpose="lawyer-analysis",
                      max_output_tokens=24576):
        self.calls.append("analysis")
        ledger.append(purpose=purpose, provider="fake", model="fake", region="cn",
                      retention="不保存", payload_sha256="0" * 64, status="ok",
                      cost_cny="0.010000")
        return self._analysis


if __name__ == "__main__":
    unittest.main()
