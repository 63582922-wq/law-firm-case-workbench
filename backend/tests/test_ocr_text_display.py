import unittest
from uuid import uuid4

from case_api.ocr_text_display import ocr_display_text
from case_api.web_case_agent_artifacts import _visual_sections


class OcrTextDisplayTests(unittest.TestCase):
    def test_fenced_html_becomes_text_without_inventing_missing_warning(self):
        raw = '```html\n<html><body><h2>借条</h2><p>借款300,000.00元。</p><p>月息2分。</p></body></html>\n```'
        display, converted = ocr_display_text(raw)
        self.assertTrue(converted)
        self.assertEqual(display, "借条\n借款300,000.00元。\n月息2分。")
        self.assertNotIn("禁止提交", display)

    def test_plain_text_is_unchanged(self):
        for raw in ("金额 < 300，余额 > 200", "  原样文字\n", "<p>证据原文</p>"):
            self.assertEqual(ocr_display_text(raw), (raw, False))

    def test_active_or_unsupported_markup_is_preserved_literally(self):
        for inner in (
            '<script>fetch("https://example.invalid")</script>',
            '<img src="https://example.invalid/a.png">',
            '<p onclick="run()">甲</p>', '<!--可能重要的内容--><p>甲</p>',
            '<unknown>乙</unknown>', '<p>未闭合', '<p>错闭合</div>',
            '<?hidden value?><p>甲</p>',
        ):
            raw = f"```html\n<html><body>{inner}</body></html>\n```"
            self.assertEqual(ocr_display_text(raw), (raw, False))

    def test_table_cells_do_not_merge_amounts(self):
        raw = "<html><body><table><tr><td>本金</td><td>300000</td></tr><tr><td>利息</td><td>6000</td></tr></table></body></html>"
        self.assertEqual(ocr_display_text(raw), ("本金\t300000\n利息\t6000", True))

    def test_entities_are_decoded_once_not_reinterpreted_as_markup(self):
        raw = "<html><body><p>&lt;script&gt;原文&lt;/script&gt; &amp;amp;</p></body></html>"
        self.assertEqual(ocr_display_text(raw), ("<script>原文</script> &amp;", True))

    def test_web_projection_retains_long_tail_source_and_completeness_warning(self):
        page_id = str(uuid4())
        raw = "甲" * 16000 + "最后一笔还款不可遗漏"
        payload = {
            "schema_version": "agent-visual-page-candidate-bundle-v1",
            "pages": [{"evidence_page_id": page_id,
                       "text_blocks": [{"block_id": "ocr-full-page", "text": raw,
                                        "kind": "TEXT", "confidence": 0.5}],
                       "fields": [], "quality_risks": []}],
        }
        section = _visual_sections(payload)[0]
        text_items = section.items[:-1]
        self.assertEqual("".join(item.detail for item in text_items), raw)
        self.assertEqual(len({item.item_id for item in section.items}), 4)
        self.assertTrue(all(item.confidence is None for item in text_items))
        self.assertTrue(all(item.sources[0].source_id == page_id for item in section.items))
        self.assertEqual(section.items[-1].title, "全文完整性待核对")
        self.assertEqual(payload["pages"][0]["text_blocks"][0]["text"], raw)

    def test_empty_page_still_reports_no_recognized_content(self):
        payload = {"schema_version": "agent-visual-page-candidate-bundle-v1",
                   "pages": [{"evidence_page_id": str(uuid4()), "text_blocks": [],
                              "fields": [], "quality_risks": []}]}
        self.assertEqual(_visual_sections(payload)[0].items[0].title, "未识别到可靠内容")
