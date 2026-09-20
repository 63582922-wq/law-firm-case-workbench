"""应诉材料包：交付清单、签字文件、证据目录的渲染测试。"""

from __future__ import annotations

import unittest

from case_kernel.matter_deliverables import (
    CATALOGUE,
    CATALOGUE_BY_ID,
    DELIVERABLE_STATES,
    PLACEHOLDER,
    MatterParties,
    catalogue_payload,
    render_authorisation,
    render_checklist,
    render_evidence_list,
    render_evidence_source,
    render_package,
    render_template,
    sanitize_filename,
)

PARTIES = MatterParties(
    respondent="测试乙", respondent_id="", respondent_address="合成市测试路1号",
    respondent_phone="13800000000", claimant="测试合成木业有限公司",
    court="合成市测试区人民法院", case_number="（2026）合成民初1号",
    cause="买卖合同纠纷", lawyer="测试律师", law_firm="测试律师事务所",
)

MATERIALS = [
    {"display_name": "起诉状.pdf", "page_count": 3},
    {"display_name": "微信聊天记录.pdf", "page_count": 43},
]


class CatalogueTests(unittest.TestCase):
    def test_every_deliverable_has_destination_signer_and_signature_flag(self) -> None:
        for item in CATALOGUE:
            self.assertIn(item.destination, ("法院", "当事人", "内部"), item.item_id)
            self.assertTrue(item.signer, item.item_id)
            self.assertIsInstance(item.needs_client_signature, bool)

    def test_signature_required_items_are_the_expected_set(self) -> None:
        required = {item.item_id for item in CATALOGUE if item.needs_client_signature}
        self.assertEqual(required, {
            "authorisation", "service_address", "statement", "evidence_source",
            "mediation", "answer", "appeal",
        })

    def test_payload_is_json_friendly(self) -> None:
        payload = catalogue_payload()
        self.assertEqual(len(payload), len(CATALOGUE))
        self.assertTrue(all(isinstance(item["needs_client_signature"], bool)
                            for item in payload))


class TemplateTests(unittest.TestCase):
    def test_authorisation_keeps_permissions_unselected(self) -> None:
        text = render_authorisation(PARTIES)
        self.assertIn("测试乙", text)
        self.assertIn("- [ ] 一般代理", text)          # 系统不替律师选权限
        self.assertIn("- [ ] 特别授权", text)
        self.assertIn("委托人（签名）", text)

    def test_each_template_has_a_signature_block(self) -> None:
        for item_id in ("authorisation", "service_address", "statement", "mediation"):
            text = render_template(item_id, PARTIES, MATERIALS) or ""
            self.assertIn("签名", text, item_id)

    def test_templates_never_invent_party_facts(self) -> None:
        """主体信息为空时一律留【待填】，系统不替律师或当事人编事实。"""
        empty = MatterParties()
        for item_id in ("authorisation", "service_address", "statement", "mediation"):
            text = render_template(item_id, empty, []) or ""
            self.assertIn(PLACEHOLDER, text, item_id)
            self.assertNotIn("测试", text, item_id)
        statement = render_template("statement", PARTIES, MATERIALS) or ""
        self.assertIn("不清楚的写「不清楚」", statement)
        self.assertIn("虚假陈述可能承担法律责任", statement)

    def test_evidence_source_lists_case_materials(self) -> None:
        text = render_evidence_source(PARTIES, MATERIALS)
        self.assertIn("起诉状.pdf", text)
        self.assertIn("微信聊天记录.pdf", text)
        self.assertIn("原始载体", text)

    def test_evidence_list_lists_materials_with_blank_proof_column(self) -> None:
        text = render_evidence_list(PARTIES, MATERIALS)
        self.assertIn("| 2 | 微信聊天记录.pdf | 43 |", text)
        self.assertIn(PLACEHOLDER, text)

    def test_cross_examination_lists_each_material_with_blank_opinions(self) -> None:
        from case_kernel.matter_deliverables import render_cross_examination

        text = render_cross_examination(PARTIES, MATERIALS)
        self.assertIn("真实性", text)
        self.assertIn("合法性", text)
        self.assertIn("关联性", text)
        self.assertIn("起诉状.pdf", text)
        self.assertIn("微信聊天记录.pdf", text)
        # 三性意见与理由必须留空：系统不代律师作出认可判断
        row = [line for line in text.splitlines() if "起诉状.pdf" in line][0]
        self.assertEqual(row.count(PLACEHOLDER), 4)
        self.assertIn("不构成质证结论", text)

    def test_argument_can_carry_issues_from_analysis(self) -> None:
        from case_kernel.matter_deliverables import render_argument

        text = render_argument(PARTIES, ["交易主体是否为答辩人", "货款金额是否确定"])
        self.assertIn("交易主体是否为答辩人", text)
        self.assertIn("来自决策包，请律师确认", text)
        self.assertIn("系统不代为选择法条", text)
        empty = render_argument(MatterParties())
        self.assertIn(PLACEHOLDER, empty)          # 主体为空时同样留待填

    def test_applications_cover_four_types_with_placeholders(self) -> None:
        from case_kernel.matter_deliverables import render_applications

        text = render_applications(PARTIES)
        for marker in ("申请追加当事人", "申请调查取证", "申请鉴定", "申请延期举证"):
            self.assertIn(marker, text)
        self.assertGreaterEqual(text.count(PLACEHOLDER), 10)

    def test_templates_are_marked_as_template_source(self) -> None:
        for item_id in ("cross_examination", "argument", "applications"):
            self.assertEqual(CATALOGUE_BY_ID[item_id].source, "template", item_id)

    def test_unknown_template_returns_none(self) -> None:
        self.assertIsNone(render_template("no-such-item", PARTIES, MATERIALS))


class ChecklistAndPackageTests(unittest.TestCase):
    def test_checklist_splits_signature_pending_from_lawyer_items(self) -> None:
        states = {"answer": "待当事人签字", "evidence_list": "起草中"}
        text = render_checklist(PARTIES, states)
        self.assertIn("需要当事人签字的文件", text)
        self.assertIn("由律师/律所署名的文件", text)
        self.assertIn("内部工作件", text)
        self.assertIn("待当事人签字", text)
        self.assertIn("起草中", text)

    def test_checklist_defaults_to_not_started(self) -> None:
        text = render_checklist(PARTIES, {})
        self.assertIn("未开始", text)
        self.assertIn(DELIVERABLE_STATES[0], text)

    def test_package_includes_everything_and_flags_missing_answer(self) -> None:
        text = render_package(parties=PARTIES, states={"answer": "未开始"},
                              materials=MATERIALS, answer_draft="")
        for marker in ("交付清单", "授权委托书", "送达地址确认书", "当事人陈述",
                       "调解意见确认", "证据目录", "证据来源说明", "质证意见",
                       "代理词", "申请书", "提交前检查清单"):
            self.assertIn(marker, text)
        self.assertIn("尚未生成答辩状草稿", text)
        self.assertIn("不构成法律意见", text)

    def test_package_includes_existing_answer_draft(self) -> None:
        text = render_package(parties=PARTIES, states={}, materials=MATERIALS,
                              answer_draft="# 民事答辩状（草稿）\n\n答辩请求：……")
        self.assertIn("附：民事答辩状（草稿）", text)
        self.assertIn("答辩请求", text)
        self.assertNotIn("尚未生成答辩状草稿", text)

    def test_filename_sanitised(self) -> None:
        self.assertEqual(sanitize_filename("（2026）合成民初1号/应诉材料包"),
                         "（2026）合成民初1号_应诉材料包")
        self.assertEqual(sanitize_filename("   "), "应诉材料包")


if __name__ == "__main__":
    unittest.main()
