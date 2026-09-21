"""答辩状草稿的确定性外壳、门禁与服务层测试。

覆盖：
- 律师选择是唯一立场来源：模型不能新增未选择的主张；
- 数字纪律：模型自算数字一律改为「见计算表」，计算表数字保留；
- 法条纪律：未登记法源改为占位；
- 请求事项由律师态度 + 引擎数字拼接；
- 服务层降级路径（无模型）仍产出可用的文书骨架。
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.defence_brief import (
    _AUTHORITY_PLACEHOLDER,
    BriefSelections,
    authority_cause_warnings,
    build_brief_prompt,
    build_request_paragraphs,
    cause_family,
    claim_catalogue,
    ground_catalogue,
    normalize_brief_output,
    render_brief_markdown,
    scrub_citations,
    scrub_figures,
)
from case_kernel.matter_documents import (
    SHORT_BLANK,
    _strip_gate_markers,
    build_answer_document,
    build_internal_checklist_document,
    scrub_stored_file_names,
    stored_file_name_count,
)
from case_kernel.defence_brief_service import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_MODEL_NOT_CONFIGURED,
    BriefRequest,
    _preflight_review,
    run_brief,
)

ENGINE = {
    "L1 未偿本金": "100000.00",
    "合计本金": "150000.00",
    "合计未付利息挂账": "122350.00",
    "利息暂计截止日": "2026-04-15",
}


def _selections(**overrides) -> BriefSelections:
    selections = BriefSelections(
        respondent="测试乙",
        claimant="测试甲",
        court="合成测试人民法院",
        case_number="（2026）合成民初1号",
        cause="民间借贷纠纷",
        grounds={"cap": True, "lawyer_fee": True, "offset": False, "limitation": False,
                 "delivery": False, "amount": False},
        stances={"principal": "部分认可", "interest": "不认可",
                 "lawyer_fee": "不认可", "costs": "不认可"},
        authorities=["《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"],
    )
    for key, value in overrides.items():
        setattr(selections, key, value)
    return selections


class GateTests(unittest.TestCase):
    def test_model_cannot_add_grounds_the_lawyer_did_not_select(self) -> None:
        raw = {
            "schema": "lawyer-defence-brief-v1",
            "sections": [
                {"ground_id": "cap", "title": "利息上限", "paragraphs": ["应按上限核减。"]},
                {"ground_id": "limitation", "title": "时效", "paragraphs": ["已过时效。"]},
            ],
        }
        payload, gate = normalize_brief_output(raw, selections=_selections(), engine_amounts=ENGINE)
        self.assertEqual([item["ground_id"] for item in payload["sections"]], ["cap"])
        self.assertTrue(any("未选择的主张" in repair for repair in gate.repairs))

    def test_model_numbers_are_replaced_but_engine_numbers_survive(self) -> None:
        raw = {"sections": [{"ground_id": "cap", "paragraphs": [
            "原告主张利息 99999.99 元，我方认为应按 122350.00 元计算，年利率 24%。"]}]}
        payload, gate = normalize_brief_output(raw, selections=_selections(), engine_amounts=ENGINE)
        text = payload["sections"][0]["paragraphs"][0]
        self.assertIn("[见计算表]", text)
        self.assertIn("122350.00", text)   # 引擎数字保留
        self.assertNotIn("99999.99", text)
        self.assertNotIn("24%", text)
        self.assertTrue(gate.repairs)

    def test_unregistered_citation_becomes_placeholder(self) -> None:
        text, removed = scrub_citations("依据《中华人民共和国合同法》第二百条。",
                                        ["《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"])
        self.assertIn("依据待律师登记", text)
        self.assertEqual(removed, ["《中华人民共和国合同法》第二百条"])

    def test_registered_citation_survives(self) -> None:
        authority = "《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"
        text, removed = scrub_citations(f"依据{authority}。", [authority])
        self.assertEqual(removed, [])
        self.assertIn(authority, text)

    def test_model_titles_and_notes_are_scrubbed_too(self) -> None:
        raw = {
            "sections": [{"ground_id": "cap", "title": "应按 99999.99 元核减",
                          "paragraphs": ["论证段落。"]}],
            "review_notes": ["模型认为应付 88888.88 元（依据《中华人民共和国合同法》第二百条）"],
        }
        payload, gate = normalize_brief_output(raw, selections=_selections(), engine_amounts=ENGINE)
        title = payload["sections"][0]["title"]
        self.assertEqual(title, "利息按司法保护上限核减")   # 标题取确定性文案
        self.assertNotIn("99999.99", title)
        note = payload["review_notes"][0]
        self.assertNotIn("88888.88", note)
        self.assertNotIn("《中华人民共和国合同法》", note)
        self.assertTrue(any("待核清单" in repair for repair in gate.repairs))

    def test_hard_redline_blocks_whole_draft(self) -> None:
        payload, gate = normalize_brief_output(
            {"sections": [{"ground_id": "cap", "paragraphs": ["我已批准提交。"]}]},
            selections=_selections(), engine_amounts=ENGINE)
        self.assertEqual(gate.level, "HARD_BLOCKED")
        self.assertEqual(payload, {})

    def test_scrub_figures_keeps_source_amounts(self) -> None:
        text, removed = scrub_figures("原告于 2024-01-01 转账 30000.00 元。", {"30000", "2024-01-01"})
        self.assertIn("30000.00", text)
        self.assertEqual(removed, [])


class RenderTests(unittest.TestCase):
    def test_requests_use_engine_numbers_only(self) -> None:
        requests = build_request_paragraphs(selections=_selections(), engine_amounts=ENGINE)
        joined = " ".join(requests)
        self.assertIn("122350.00", joined)
        self.assertIn("2026-04-15", joined)
        self.assertIn("150000.00", joined)
        self.assertIn("律师费", joined)

    def test_goods_payment_case_uses_sales_numbers(self) -> None:
        """买卖合同口径：答辩请求引用货款本金与逾期损失净额，不套用借贷数字。"""
        selections = _selections()
        numbers = {
            "原告主张货款": "10000.00",
            "律师确认应付货款": "10000.00",
            "已确认还本合计": "4000.00",
            "未付货款本金": "6000.00",
            "逾期付款损失（毛额）": "120.00",
            "已确认付息合计": "20.00",
            "逾期付款损失（净额）": "100.00",
            "本金与损失合计": "6100.00",
            "逾期起算日": "2025-10-25",
            "暂计截止日": "2026-06-22",
            "损失口径": "按一年期 LPR",
        }
        requests = build_request_paragraphs(selections=selections, engine_amounts=numbers)
        joined = " ".join(requests)
        self.assertIn("货款本金为 6000.00 元", joined)
        self.assertIn("逾期付款损失核减至 100.00 元", joined)
        self.assertIn("按一年期 LPR", joined)
        self.assertNotIn("合计本金", joined)

    def test_offset_request_quotes_net_numbers_when_confirmed(self) -> None:
        selections = _selections(grounds={"cap": True, "offset": True})
        numbers = {
            **ENGINE,
            "已确认付款合计": "12250.00",
            "冲抵后合计本金": "90000.00",
            "冲抵后合计未付利息挂账": "110000.00",
        }
        requests = build_request_paragraphs(selections=selections, engine_amounts=numbers)
        joined = " ".join(requests)
        self.assertIn("已支付的 12250.00 元", joined)
        self.assertIn("本金 90000.00 元", joined)
        self.assertIn("利息 110000.00 元", joined)

    def test_offset_request_without_confirmed_payments_stays_principle_level(self) -> None:
        selections = _selections(grounds={"cap": True, "offset": True})
        requests = build_request_paragraphs(selections=selections, engine_amounts=ENGINE)
        joined = " ".join(requests)
        # 提交件不写内部词汇；未确认付款时只作原则性主张
        self.assertIn("由律师确认后核定净额", joined)
        self.assertNotIn("计算表", joined)

    def test_requests_pending_when_nothing_selected(self) -> None:
        empty = BriefSelections()
        requests = build_request_paragraphs(selections=empty, engine_amounts=ENGINE)
        self.assertIn("待律师填写", requests[0])

    def test_markdown_carries_warning_and_authority_placeholder(self) -> None:
        markdown = render_brief_markdown(
            selections=_selections(authorities=[]),
            engine_amounts=ENGINE,
            sections=[{"ground_id": "cap", "title": "利息上限",
                       "paragraphs": ["论证段落（见计算表）。"]}],
            review_items=["引用了未登记法源"],
            gate_level="MARK_FOR_REVIEW",
            materials=[{"display_name": "合成起诉状.pdf", "page_count": 1}],
        )
        self.assertIn("不得提交法院", markdown)
        self.assertIn("依据待律师登记", markdown)
        # 工作稿不写存储层文件名；材料名称去掉扩展名后仍要认得出是哪份
        self.assertIn("合成起诉状", markdown)
        self.assertNotIn("合成起诉状.pdf", markdown)
        self.assertIn("122350.00", markdown)
        self.assertIn("答辩人（签名）", markdown)


class _FakeTransport:
    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[str] = []
        self._payload = payload or {
            "schema": "lawyer-defence-brief-v1",
            "sections": [{"ground_id": "cap", "title": "利息应按司法保护上限核减",
                          "paragraphs": ["原告主张的利息超过司法保护上限部分不应支持（见计算表）。"]}],
            "review_notes": ["请核对付款性质"],
        }

    def call_analysis(self, *, instruction, ledger, purpose="lawyer-analysis",
                      max_output_tokens=24576):
        self.calls.append(purpose)
        ledger.append(purpose=purpose, provider="fake", model="fake", region="cn",
                      retention="不保存", payload_sha256="0" * 64, status="ok",
                      cost_cny="0.010000")
        return self._payload


def _write_inputs(root: Path) -> tuple[Path, Path, Path, Path]:
    config = root / "case_config.json"
    config.write_text(json.dumps({
        "schema": "shadow-case-config-v1",
        "lpr_4x_monthly_rate": "0.01",
        "interest_cutoff": "2026-04-15",
        "debts": [{"debt_id": "L1", "principal": "100000.00", "disbursed_on": "2019-10-19",
                   "agreed_monthly_rate": "0.015", "due_on": "2019-12-19"}],
    }, ensure_ascii=False), encoding="utf-8")
    report = root / "决策包.md"
    report.write_text("# 决策包\n\n## 一、案情与立场\n原告主张本金与利息。\n\n## 二、争点\n- 利率上限\n",
                      encoding="utf-8")
    preflight = root / "preflight.json"
    preflight.write_text(json.dumps({
        "purpose": "case_analysis", "sent_fields": {"page_files": []}, "provider": "aliyun",
        "model": "qwen3-vl-plus", "region": "cn-beijing", "retention": "不保存",
        "budget_cap_cny": "2", "confirmed": "true",
    }, ensure_ascii=False), encoding="utf-8")
    env_file = root / "model.env"
    env_file.write_text("LAWCASE_AGENT_WORKER_QWEN_API_KEY=x\n"
                        "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID=ws-x\n", encoding="utf-8")
    return config, report, preflight, env_file


class ServiceTests(unittest.TestCase):
    def test_degraded_without_preflight_still_renders_skeleton(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, report, _preflight, _env = _write_inputs(root)
            result = run_brief(BriefRequest(
                case_id="c1", output_root=root / "out", selections=_selections(),
                case_config_path=config, analysis_report_path=report,
            ))
            self.assertEqual(result.status, STATUS_MODEL_NOT_CONFIGURED)
            self.assertEqual(result.engine_amounts["合计本金"], "100000.00")
            self.assertIn("待律师补写", result.markdown)
            self.assertIn("民事答辩状", result.markdown)
            self.assertEqual(result.calls, 0)

    def test_model_drafting_pipeline_writes_gated_markdown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, report, preflight, env_file = _write_inputs(root)
            transport = _FakeTransport()
            result = run_brief(BriefRequest(
                case_id="c1", output_root=root / "out", selections=_selections(),
                case_config_path=config, analysis_report_path=report,
                preflight_path=preflight, env_file=env_file, transport=transport,
            ))
            self.assertEqual(result.status, STATUS_COMPLETED)
            self.assertEqual(result.calls, 1)
            self.assertEqual(transport.calls, ["defence-brief"])
            self.assertEqual(result.gate_level, "MARK_FOR_REVIEW")
            self.assertIn("司法保护上限", result.markdown)
            self.assertTrue((root / "out" / "答辩状草稿.md").is_file())
            self.assertTrue((root / "out" / "brief_ledger.json").is_file())

    def test_model_numbers_and_unregistered_citations_are_scrubbed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, report, preflight, env_file = _write_inputs(root)
            transport = _FakeTransport({
                "sections": [{"ground_id": "cap", "title": "利息",
                              "paragraphs": ["应付 12345.67 元，依据《中华人民共和国合同法》第二百条。"]}],
            })
            result = run_brief(BriefRequest(
                case_id="c1", output_root=root / "out", selections=_selections(),
                case_config_path=config, analysis_report_path=report,
                preflight_path=preflight, env_file=env_file, transport=transport,
            ))
            self.assertEqual(result.status, STATUS_COMPLETED)
            self.assertNotIn("12345.67", result.markdown)
            body = result.markdown.split("## 事实与理由", 1)[1].split("## 依据与数字来源", 1)[0]
            self.assertIn("[见计算表]", body)
            self.assertIn("依据待律师登记", body)
            # 正文不得留下未登记法条（含紧随其后的条文号）
            self.assertNotIn("《中华人民共和国合同法》", body)
            self.assertNotIn("第二百条", body)
            # 待核清单里保留完整引用，便于律师核对模型到底引了什么
            self.assertIn("《中华人民共和国合同法》第二百条", result.markdown)

    def test_hard_redline_blocks_draft(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, report, preflight, env_file = _write_inputs(root)
            transport = _FakeTransport({"sections": [{"ground_id": "cap",
                                                      "paragraphs": ["我已提交法院。"]}]})
            result = run_brief(BriefRequest(
                case_id="c1", output_root=root / "out", selections=_selections(),
                case_config_path=config, analysis_report_path=report,
                preflight_path=preflight, env_file=env_file, transport=transport,
            ))
            self.assertEqual(result.status, STATUS_BLOCKED)
            self.assertEqual(result.gate_level, "HARD_BLOCKED")
            self.assertIn("红线", result.error or "")

    def test_no_analysis_report_keeps_model_out(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, _report, preflight, env_file = _write_inputs(root)
            transport = _FakeTransport()
            result = run_brief(BriefRequest(
                case_id="c1", output_root=root / "out", selections=_selections(),
                case_config_path=config, preflight_path=preflight, env_file=env_file,
                transport=transport,
            ))
            self.assertEqual(result.status, STATUS_MODEL_NOT_CONFIGURED)
            self.assertEqual(transport.calls, [])
            self.assertIn("尚无决策包报告", result.error or "")

class SelectionTests(unittest.TestCase):
    def test_from_dict_rejects_unknown_grounds_and_stances(self) -> None:
        selections = BriefSelections.from_dict({
            "grounds": {"cap": True, "no_such_ground": True},
            "stances": {"principal": "认可", "interest": "随便写", "unknown": "认可"},
            "authorities": [" 《中华人民共和国民法典》 ", "", 123],
        })
        self.assertEqual(sorted(selections.grounds), sorted(
            ["cap", "offset", "lawyer_fee", "limitation", "delivery", "amount"]))
        self.assertTrue(selections.grounds["cap"])
        self.assertFalse(selections.grounds["offset"])
        self.assertEqual(selections.stances["principal"], "认可")
        self.assertEqual(selections.stances["interest"], "不发表意见")
        self.assertNotIn("unknown", selections.stances)
        self.assertEqual(selections.authorities, ["《中华人民共和国民法典》", "123"])

    def test_round_trip_dict_is_stable(self) -> None:
        original = _selections()
        restored = BriefSelections.from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())


class EvidenceCitationTests(unittest.TestCase):
    """证据编号引用不能被法条清洗器吃掉（真实踩过：原告证据1［依据待律师登记］）。"""

    def test_evidence_reference_survives_citation_scrub(self) -> None:
        text, removed = scrub_citations(
            "原告主张的货款（见原告证据1《起诉状》第1、2页），依据《中华人民共和国合同法》第二百条。",
            [],
        )
        self.assertIn("原告证据1《起诉状》第1、2页", text)
        self.assertNotIn("原告证据1［依据待律师登记］", text)
        self.assertEqual(removed, ["《中华人民共和国合同法》第二百条"])

    def test_unregistered_law_citation_still_scrubbed(self) -> None:
        text, removed = scrub_citations("依据《中华人民共和国民法典》第五百七十七条。", [])
        self.assertIn(_AUTHORITY_PLACEHOLDER, text)
        self.assertEqual(removed, ["《中华人民共和国民法典》第五百七十七条"])

    def test_registered_law_citation_kept(self) -> None:
        authority = "《中华人民共和国民法典》第五百七十七条"
        text, removed = scrub_citations(f"依据{authority}。", [authority])
        self.assertEqual(removed, [])
        self.assertIn(authority, text)


class CauseFamilyTests(unittest.TestCase):
    """案由决定术语：买卖合同不得出现借贷概念（真实踩过：买卖合同引民间借贷规定）。"""

    SALES = "买卖合同纠纷"
    LOAN = "民间借贷纠纷"

    def test_catalogue_switches_wording(self) -> None:
        sales = {title for _id, title, _d in ground_catalogue(self.SALES)}
        loan = {title for _id, title, _d in ground_catalogue(self.LOAN)}
        self.assertIn("货款数额与证据不符", sales)
        self.assertIn("供货与交付事实证据不足", sales)
        self.assertIn("逾期付款损失的计算依据有误", sales)
        self.assertTrue(all("借贷" not in title and "出借" not in title for title in sales))
        self.assertIn("本金数额与证据不符", loan)
        self.assertIn("出借事实与款项交付证据不足", loan)

    def test_claim_labels_switch_wording(self) -> None:
        self.assertEqual(dict(claim_catalogue(self.SALES))["interest"], "逾期付款损失")
        self.assertEqual(dict(claim_catalogue(self.SALES))["principal"], "货款本金")
        self.assertEqual(dict(claim_catalogue(self.LOAN))["interest"], "利息")

    def test_prompt_states_cause_and_forbids_foreign_terms(self) -> None:
        prompt = build_brief_prompt(
            selections=BriefSelections(cause=self.SALES, grounds={"cap": True}),
            engine_amounts={}, claim_summary="", analysis_context="",
            evidence_index="原告证据1：《购销合同》第1-3页",
        )
        self.assertIn("案由：买卖合同纠纷", prompt)
        self.assertIn("禁止出现「借贷」「出借」「借款」「利息」", prompt)
        self.assertIn("原告证据1：《购销合同》第1-3页", prompt)
        self.assertIn("严禁写文件名、扩展名", prompt)

    def test_prompt_without_cause_demands_neutral_wording(self) -> None:
        prompt = build_brief_prompt(
            selections=BriefSelections(), engine_amounts={}, claim_summary="",
            analysis_context="", evidence_index="",
        )
        self.assertIn("案由未填写", prompt)
        self.assertIn("不要引用具体材料", prompt)

    def test_foreign_authority_is_flagged_not_deleted(self) -> None:
        authority = "《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"
        warnings = authority_cause_warnings([authority], self.SALES)
        self.assertEqual(len(warnings), 1)
        self.assertIn("法源与案由可能不匹配", warnings[0])
        # 提示而已：法源仍按律师登记原文保留
        self.assertIn(authority, warnings[0])
        # 同族法源不提示
        self.assertEqual(authority_cause_warnings([authority], self.LOAN), [])
        self.assertEqual(authority_cause_warnings([authority], ""), [])

    def test_preflight_reports_missing_cause(self) -> None:
        request = BriefRequest(case_id="c", output_root=Path("/tmp"),
                               selections=BriefSelections(grounds={"cap": True}))
        items = _preflight_review(request, {"合计本金": "1.00"})
        self.assertTrue(any("尚未填写案由" in item for item in items))

    def test_sales_request_has_no_loan_wording_and_no_doubled_an(self) -> None:
        requests = build_request_paragraphs(
            selections=BriefSelections(cause=self.SALES,
                                       stances={"principal": "不认可", "interest": "不认可"}),
            engine_amounts={
                "未付货款本金": "10000.00",
                "逾期付款损失（净额）": "197.26",
                "损失口径": "按一年期 LPR",
                "暂计截止日": "2026-04-15",
            },
        )
        text = "".join(requests)
        self.assertIn("应付货款本金为 10000.00 元", text)
        self.assertNotIn("按按", text)          # 口径标签自带「按」时不再重复
        self.assertIn("按一年期 LPR", text)
        for term in ("借款本金", "出借", "借贷"):
            self.assertNotIn(term, text)

    def test_loan_request_untouched_when_cause_is_loan(self) -> None:
        requests = build_request_paragraphs(
            selections=BriefSelections(cause=self.LOAN, stances={"principal": "部分认可"}),
            engine_amounts={"合计本金": "150000.00"},
        )
        self.assertIn("借款本金为 150000.00 元", "".join(requests))

    def test_sales_requests_keep_lawyer_fee_and_costs(self) -> None:
        """买卖分支曾提前 return，律师费/诉讼费请求被吃掉。"""
        requests = build_request_paragraphs(
            selections=BriefSelections(
                cause="买卖合同纠纷",
                stances={"principal": "部分认可", "interest": "不认可",
                         "lawyer_fee": "不认可", "costs": "不认可"},
            ),
            engine_amounts={"未付货款本金": "10000.00", "逾期付款损失（净额）": "197.26"},
        )
        joined = " ".join(requests)
        self.assertIn("诉讼费用由原告负担", joined)
        self.assertIn("承担律师费", joined)
        self.assertIn("货款本金", joined)
        self.assertEqual(len(requests), 4)

    def test_sales_section_titles_come_from_sales_catalogue(self) -> None:
        raw = {"sections": [{"ground_id": "amount", "title": "模型自己起的标题",
                             "paragraphs": ["论证段落。"]}],
               "review_notes": []}
        payload, _gate = normalize_brief_output(
            raw,
            selections=BriefSelections(cause=self.SALES, grounds={"amount": True}),
            engine_amounts={},
        )
        self.assertEqual(payload["sections"][0]["title"], "货款数额与证据不符")


class InternalJargonTests(unittest.TestCase):
    """内部词汇不得进提交件（真实踩过：答辩请求写成「以计算表逐笔核定为准」）。"""

    def test_count_sheet_wording_is_replaced(self) -> None:
        self.assertEqual(_strip_gate_markers("（以计算表逐笔核定为准）"), "（以本案证据逐笔核定为准）")
        self.assertEqual(_strip_gate_markers("具体数额以计算表为准。"), "具体数额以本案证据核定为准。")
        self.assertNotIn("计算表", _strip_gate_markers("其余以计算表为据。"))

    def test_requests_never_mention_count_sheet(self) -> None:
        requests = build_request_paragraphs(
            selections=BriefSelections(cause="买卖合同纠纷",
                                       stances={"principal": "不认可", "interest": "不认可"}),
            engine_amounts={"未付货款本金": "10000.00", "逾期付款损失（净额）": "197.26",
                            "损失口径": "按一年期 LPR", "暂计截止日": "2026-06-22"},
        )
        for item in requests:
            self.assertNotIn("计算表", _strip_gate_markers(item))
            self.assertNotIn("计算表", item)

    def test_offset_request_has_no_count_sheet(self) -> None:
        requests = build_request_paragraphs(
            selections=BriefSelections(cause="民间借贷纠纷", grounds={"offset": True},
                                       stances={}),
            engine_amounts={"已确认付款合计": "100.00", "冲抵后合计本金": "9900.00"},
        )
        self.assertTrue(requests)
        for item in requests:
            self.assertNotIn("计算表", item)

    def test_answer_document_has_facts_section(self) -> None:
        from case_kernel.matter_deliverables import MatterParties

        spec = build_answer_document(
            MatterParties(respondent="测试乙", claimant="某公司", cause="买卖合同纠纷",
                          case_number="（2026）合成民初1号"),
            answer_markdown="", engine_numbers={"未付货款本金": "10000.00"})
        titles = [block.text for block in spec.blocks if hasattr(block, "text")]
        self.assertTrue(any(text.startswith("（一）基本事实") for text in titles))
        self.assertTrue(any("交易经过" in text for text in titles))


class StoredFileNameTests(unittest.TestCase):
    """提交件里不得出现存储层文件名（真实踩过：哈希文件名进了答辩状正文）。"""

    def test_hash_file_names_are_blanked(self) -> None:
        text = scrub_stored_file_names(
            "原告提交的催收记录见 12207299bbcf203a4d481ebea25c5198.jpg 第22至43页。")
        self.assertNotIn("12207299bbcf", text)
        self.assertIn(SHORT_BLANK, text)

    def test_extension_names_are_blanked(self) -> None:
        self.assertNotIn(".pdf", scrub_stored_file_names("见《微信聊天记录.pdf》。"))
        self.assertNotIn(".docx", scrub_stored_file_names("附件：购销合同.docx"))

    def test_material_citation_drops_book_marks(self) -> None:
        # 「《＿＿＿＿》第3页」是把材料引用误排成法条引用
        self.assertEqual(_strip_gate_markers("原告在《＿＿＿＿》第1、2页中主张"),
                         "原告在＿＿＿＿第1、2页中主张")

    def test_count_matches_scrub(self) -> None:
        text = "见 a1b2c3d4e5f60718293a4b5c6d7e8f90.jpg 与 b1c2d3e4f5a60718293a4b5c6d7e8f90.pdf"
        self.assertEqual(stored_file_name_count(text), 2)

    def test_internal_file_reports_blanked_names(self) -> None:
        from case_kernel.matter_deliverables import MatterParties

        spec = build_internal_checklist_document(
            parties=MatterParties(case_number="（2026）合成民初1号", cause="买卖合同纠纷"),
            states={}, materials=[], engine_numbers={"未付货款本金": "10000.00"},
            review_items=["法源与案由可能不匹配：…"],
            blanked_amount_count=2, blanked_name_count=3,
            evidence_index="原告证据1：《购销合同》第1-3页",
        )
        text = "".join(block.text for block in spec.blocks if hasattr(block, "text"))
        self.assertIn("3 处原为上传文件的存储文件名", text)
        self.assertIn("原告证据1：《购销合同》第1-3页", text)
        self.assertIn("四、", text)


if __name__ == "__main__":
    unittest.main()
