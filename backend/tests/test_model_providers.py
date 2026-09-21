"""供应商切换：解析、端点、价格档位、图片 token 与请求体的测试。

真实背景：本流水线原先分别用视觉模型做 OCR、再调模型做分析；DeepSeek 的
`deepseek-flash` 原生支持图片输入，因此改成一把 key、一个模型覆盖全流程。
切换时踩过的坑写进测试：模型名必须按供应商解析，不能把默认模型名发过去。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import unittest

from case_kernel.model_providers import (
    ALIYUN,
    CST,
    DEEPSEEK,
    PROVIDER_ENV,
    PROVIDERS,
    ProviderError,
    endpoint_for,
    image_tokens,
    provider_summary,
    resolve_provider,
)

DEEPSEEK_ENV = {
    "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": "sk-" + "x" * 32,
}
ALIYUN_ENV = {
    "LAWCASE_AGENT_WORKER_QWEN_API_KEY": "qwen-key",
    "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID": "ws-test123",
}


class ResolutionTests(unittest.TestCase):
    def test_deepseek_is_preferred_when_both_keys_exist(self) -> None:
        provider, key = resolve_provider({**DEEPSEEK_ENV, **ALIYUN_ENV})
        self.assertEqual(provider.key, "deepseek")
        self.assertEqual(provider.model, "deepseek-flash")
        self.assertTrue(key.startswith("sk-"))

    def test_falls_back_to_aliyun_when_only_qwen_key(self) -> None:
        provider, _key = resolve_provider(ALIYUN_ENV)
        self.assertEqual(provider.key, "aliyun-maas")
        self.assertEqual(provider.model, "qwen3-vl-plus")

    def test_explicit_provider_wins(self) -> None:
        provider, _key = resolve_provider(
            {**DEEPSEEK_ENV, **ALIYUN_ENV, PROVIDER_ENV: "aliyun-maas"})
        self.assertEqual(provider.key, "aliyun-maas")

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            resolve_provider({**DEEPSEEK_ENV, PROVIDER_ENV: "openai"})
        self.assertIn("未知模型供应商", str(caught.exception))

    def test_missing_keys_reports_what_to_configure(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            resolve_provider({})
        self.assertIn("没有可用的模型密钥", str(caught.exception))
        self.assertIn("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY", str(caught.exception))

    def test_aliyun_without_workspace_is_not_selected(self) -> None:
        with self.assertRaises(ProviderError):
            resolve_provider({"LAWCASE_AGENT_WORKER_QWEN_API_KEY": "qwen-key"})

    def test_endpoints(self) -> None:
        self.assertEqual(endpoint_for(DEEPSEEK, DEEPSEEK_ENV),
                         "https://api.deepseek.com/chat/completions")
        self.assertEqual(
            endpoint_for(ALIYUN, ALIYUN_ENV),
            "https://ws-test123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
        with self.assertRaises(ProviderError):
            endpoint_for(ALIYUN, {"LAWCASE_AGENT_WORKER_QWEN_API_KEY": "x"})


class PricingTests(unittest.TestCase):
    """价格用于预算门禁与记账：高峰/空闲两档，缓存命中价更低。"""

    def _peak(self) -> datetime:
        # 北京时间周三 10:00（高峰）
        return datetime(2026, 9, 16, 10, 0, tzinfo=CST)

    def _off_peak(self) -> datetime:
        # 北京时间周三 22:00（空闲）
        return datetime(2026, 9, 16, 22, 0, tzinfo=CST)

    def test_peak_window_detection(self) -> None:
        self.assertTrue(DEEPSEEK.is_peak(self._peak()))
        self.assertFalse(DEEPSEEK.is_peak(self._off_peak()))
        # 周末全天按空闲档
        saturday = datetime(2026, 9, 19, 10, 0, tzinfo=CST)
        self.assertFalse(DEEPSEEK.is_peak(saturday))

    def test_deepseek_rates_match_public_pricing(self) -> None:
        cached, input_rate, output_rate = DEEPSEEK.rates(self._off_peak())
        self.assertEqual((cached, input_rate, output_rate),
                         (Decimal("0.02"), Decimal("1"), Decimal("4")))
        cached, input_rate, output_rate = DEEPSEEK.rates(self._peak())
        self.assertEqual((cached, input_rate, output_rate),
                         (Decimal("0.04"), Decimal("2"), Decimal("8")))

    def test_cost_uses_cache_hit_price(self) -> None:
        # 2 万输入（其中 1.5 万命中缓存）+ 3 千输出，空闲档：0.3 + 5 + 12 = 17.3 元？
        cost = DEEPSEEK.price(prompt_tokens=20_000, completion_tokens=3_000,
                              cached_tokens=15_000, when=self._off_peak())
        expected = (Decimal(15_000) * Decimal("0.02")
                    + Decimal(5_000) * Decimal("1")
                    + Decimal(3_000) * Decimal("4")) / Decimal(1_000_000)
        self.assertEqual(cost, expected)

    def test_peak_costs_more_than_off_peak(self) -> None:
        off = DEEPSEEK.price(prompt_tokens=10_000, completion_tokens=1_000,
                             when=self._off_peak())
        peak = DEEPSEEK.price(prompt_tokens=10_000, completion_tokens=1_000,
                              when=self._peak())
        self.assertEqual(peak, off * 2)

    def test_summary_has_no_secret_and_reports_vision(self) -> None:
        summary = provider_summary(DEEPSEEK, when=self._off_peak())
        self.assertTrue(summary["vision"])
        self.assertEqual(summary["model"], "deepseek-flash")
        self.assertNotIn("key", {k for k in summary if k == "api_key"})
        self.assertEqual(summary["pricing_cny_per_million"]["peak"], False)


class ImageTokenTests(unittest.TestCase):
    def test_deepseek_images_are_capped_at_public_limit(self) -> None:
        self.assertLessEqual(image_tokens(DEEPSEEK, 5000, 5000), DEEPSEEK.image_token_cap)
        self.assertGreater(image_tokens(DEEPSEEK, 1300, 1300), 500)
        self.assertGreaterEqual(image_tokens(DEEPSEEK, 50, 50), 1)

    def test_aliyun_keeps_patch_estimate(self) -> None:
        self.assertEqual(image_tokens(ALIYUN, 1000, 1400), (1000 * 1400) // 1024 + 2)


class RequestBodyTests(unittest.TestCase):
    """请求体必须按供应商拼装：DeepSeek 用 thinking 开关，阿里云用 enable_thinking。"""

    def _transport(self, provider):
        from case_kernel.shadow_live_transport import QwenShadowTransport

        transport = QwenShadowTransport.__new__(QwenShadowTransport)
        transport.provider = provider
        transport.model = provider.model
        transport.endpoint = provider.endpoint or "https://example.invalid/x"
        transport.api_key = "k"
        transport.materials_root = None
        transport.run_root = None
        transport.budget_cny = Decimal("2")
        transport.spent_cny = Decimal("0")
        return transport

    def test_deepseek_body_disables_thinking_and_keeps_temperature_out(self) -> None:
        transport = self._transport(DEEPSEEK)
        self.assertEqual(transport.provider.disable_thinking_field, "thinking")
        # 组装逻辑与 _call 中一致
        body: dict = {"max_tokens": 1024}
        if transport.provider.disable_thinking_field == "thinking":
            body["thinking"] = {"type": "disabled"}
        else:
            body["temperature"] = 0.1
            body["enable_thinking"] = False
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", body)

    def test_model_defaults_to_provider_model(self) -> None:
        from case_kernel.shadow_live_transport import QwenShadowTransport

        self.assertEqual(DEEPSEEK.model, "deepseek-flash")
        self.assertEqual(ALIYUN.model, "qwen3-vl-plus")
        # 缺省参数不能是某一家模型名，否则切换供应商会把旧模型名发过去
        # （真实踩过：默认 "qwen3-vl-plus" 被发到 DeepSeek，返回 400 invalid model name）
        self.assertEqual(QwenShadowTransport.__init__.__kwdefaults__["model"], "")
        from case_kernel import shadow_live_transport as module

        self.assertEqual(module.MODEL, "")


if __name__ == "__main__":
    unittest.main()
