"""传输层解析测试：模型返回非 JSON 时必须 fail closed，而不是抛 UnboundLocalError。

真实回归：合成木业案 OCR 阶段，模型把 JSON 包在代码块里返回，旧代码在
``json.loads`` 失败后仍访问未赋值的 ``parsed``，抛出
``UnboundLocalError: cannot access local variable 'parsed'``，把一个本可
提取/本应留证的情况变成看不懂的崩溃。
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.shadow_live_transport import (
    QwenShadowTransport,
    _parse_model_json,
)
from case_kernel.shadow_mode import RequestLedger, ShadowBlocked


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class _FakeOpener:
    def __init__(self, content: str) -> None:
        self.content = content

    def open(self, request, timeout=None):  # noqa: ANN001 - 测试替身
        return _FakeResponse({
            "model": "qwen3-vl-plus",
            "choices": [{"finish_reason": "stop", "message": {"content": self.content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        })


def _transport(root: Path) -> QwenShadowTransport:
    transport = QwenShadowTransport.__new__(QwenShadowTransport)
    transport.materials_root = root
    transport.run_root = root
    transport.api_key = "x"
    transport.workspace_id = "ws-x"
    transport.model = "qwen3-vl-plus"
    transport.budget_cny = Decimal("2")
    transport.spent_cny = Decimal("0")
    transport.allow_image_identifiers = False
    transport.image_identifier_findings = []
    return transport


class ParseHelperTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(_parse_model_json('{"schema":"s"}'), ({"schema": "s"}, ""))

    def test_fenced_json_is_recovered(self) -> None:
        value, note = _parse_model_json('```json\n{"schema":"s"}\n```')
        self.assertEqual(value, {"schema": "s"})
        self.assertIn("围栏", note)

    def test_prose_wrapped_json_is_recovered(self) -> None:
        value, note = _parse_model_json('结果如下：\n{"schema":"s"}\n以上。')
        self.assertEqual(value, {"schema": "s"})
        self.assertIn("说明文字", note)

    def test_non_object_returns_none(self) -> None:
        self.assertEqual(_parse_model_json("[1,2,3]"), (None, ""))
        self.assertEqual(_parse_model_json("完全不是 JSON"), (None, ""))


class CallParsingTests(unittest.TestCase):
    def _call(self, root: Path, content: str, *, schema: str = "shadow-ocr-v1"):
        import case_kernel.shadow_live_transport as module

        original = module._NO_PROXY_OPENER
        module._NO_PROXY_OPENER = _FakeOpener(content)  # type: ignore[assignment]
        try:
            transport = _transport(root)
            ledger = RequestLedger(root / "ledger.json")
            return transport._call(
                instruction="转写图片", images=[], max_output_tokens=128,
                purpose="ocr", ledger=ledger, expected_schema=schema,
            ), ledger, transport
        finally:
            module._NO_PROXY_OPENER = original  # type: ignore[assignment]

    def test_fenced_json_is_accepted_with_note(self) -> None:
        with TemporaryDirectory() as tmp:
            payload, ledger, _transport_used = self._call(
                Path(tmp), '```json\n{"schema":"shadow-ocr-v1","pages":[]}\n```')
            self.assertEqual(payload["schema"], "shadow-ocr-v1")
            self.assertEqual(ledger.rows[-1]["status"], "ok")
            self.assertIn("围栏", ledger.rows[-1].get("note", ""))

    def test_unparseable_content_fails_closed_with_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ShadowBlocked) as caught:
                self._call(root, "这不是 JSON，只有一段说明文字")
            self.assertIn("无法解析", str(caught.exception))
            self.assertTrue((root / "derivatives" / "provider_reject_ocr.json").is_file())

    def test_wrong_schema_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ShadowBlocked):
                self._call(Path(tmp), '{"schema":"something-else"}')

    def test_partial_json_object_does_not_crash(self) -> None:
        """回归：内容里只有不完整对象时不得再出现 UnboundLocalError。"""
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ShadowBlocked) as caught:
                self._call(Path(tmp), '{"schema": "shadow-ocr-v1", "pages": [')
            self.assertNotIn("UnboundLocalError", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
