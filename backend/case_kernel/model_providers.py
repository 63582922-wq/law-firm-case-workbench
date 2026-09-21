"""模型供应商抽象：同一套数据路径纪律，可切换供应商与模型。

为什么要这一层：流水线（OCR → 分析 → 答辩状）只需要"一个能读图、能出 JSON 的模型"，
不该把某一家供应商的端点、字段名与计费方式焊死在代码里。切换供应商应当只改配置。

已装配的供应商：

- ``deepseek``（默认优先）：`deepseek-flash`，OpenAI 兼容 `https://api.deepseek.com`，
  **原生支持图片输入**，因此 OCR 与分析可用同一个模型、同一把 key（见 docs/MODEL_PROVIDER.md）；
- ``aliyun-maas``：`qwen3-vl-plus`，经业务空间端点调用（既有部署仍在用，保留兼容）。

价格只用于**费用记账与预算门禁**，取自供应商公开价目（元/百万 tokens），
按高峰/空闲两档计算；价格变动时改这里一处即可，不散落在调用代码里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal

CST = timezone(timedelta(hours=8))

# 北京时间周一至周五的高峰时段（不含法定节假日；节假日无法在本机判断，
# 因此按"非高峰"档计价——宁可少算也不虚增律师的账）
PEAK_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(12, 0)),
    (time(14, 0), time(18, 0)),
)


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    model: str
    key_env_names: tuple[str, ...]
    region: str
    retention: str
    supports_vision: bool
    supports_json_mode: bool
    # 每张图片计入的 token 上限（供应商公开口径）
    image_token_cap: int
    # 单张内联图片上限（字节）与请求体上限（字节）
    image_bytes_limit: int
    body_bytes_limit: int
    # 价格：元 / 百万 tokens
    input_cached_peak: Decimal
    input_cached_off_peak: Decimal
    input_peak: Decimal
    input_off_peak: Decimal
    output_peak: Decimal
    output_off_peak: Decimal
    # 关闭思考模式的请求字段（OpenAI 兼容格式），None 表示该供应商无此开关
    disable_thinking_field: str | None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def is_peak(self, when: datetime | None = None) -> bool:
        moment = (when or datetime.now(timezone.utc)).astimezone(CST)
        if moment.weekday() >= 5:      # 周末全天按空闲档
            return False
        return any(start <= moment.time() < end for start, end in PEAK_WINDOWS)

    def rates(self, when: datetime | None = None) -> tuple[Decimal, Decimal, Decimal]:
        """返回 (缓存命中输入价, 缓存未命中输入价, 输出价)，单位元/百万 tokens。"""
        if self.is_peak(when):
            return self.input_cached_peak, self.input_peak, self.output_peak
        return self.input_cached_off_peak, self.input_off_peak, self.output_off_peak

    def price(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
        when: datetime | None = None,
    ) -> Decimal:
        cached_rate, input_rate, output_rate = self.rates(when)
        cached = max(0, min(cached_tokens, prompt_tokens))
        uncached = max(0, prompt_tokens - cached)
        total = (
            Decimal(cached) * cached_rate
            + Decimal(uncached) * input_rate
            + Decimal(completion_tokens) * output_rate
        )
        return total / Decimal(1_000_000)

    def max_output_tokens(self) -> int:
        """单次调用的输出上限（按供应商公开上限的一半留出余量）。"""
        return 24_576


DEEPSEEK = Provider(
    key="deepseek",
    label="deepseek-official",
    base_url="https://api.deepseek.com",
    model="deepseek-flash",
    key_env_names=("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    region="cn",
    retention="供应商不用于训练；调用即弃（见供应商数据政策）",
    supports_vision=True,
    supports_json_mode=True,
    image_token_cap=1024,
    image_bytes_limit=32 * 1024 * 1024,
    body_bytes_limit=48 * 1024 * 1024,
    input_cached_peak=Decimal("0.04"),
    input_cached_off_peak=Decimal("0.02"),
    input_peak=Decimal("2"),
    input_off_peak=Decimal("1"),
    output_peak=Decimal("8"),
    output_off_peak=Decimal("4"),
    disable_thinking_field="thinking",
)

ALIYUN = Provider(
    key="aliyun-maas",
    label="aliyun-model-studio",
    base_url="",                      # 由业务空间拼接，见 endpoint_for()
    model="qwen3-vl-plus",
    key_env_names=("LAWCASE_AGENT_WORKER_QWEN_API_KEY",),
    region="cn-beijing",
    retention="不保存（调用即弃，不用于训练）",
    supports_vision=True,
    supports_json_mode=True,
    image_token_cap=0,                # 按像素估算，不用固定上限
    image_bytes_limit=32 * 1024 * 1024,
    body_bytes_limit=64 * 1024 * 1024,
    input_cached_peak=Decimal("1"),
    input_cached_off_peak=Decimal("1"),
    input_peak=Decimal("1"),
    input_off_peak=Decimal("1"),
    output_peak=Decimal("10"),
    output_off_peak=Decimal("10"),
    disable_thinking_field="enable_thinking",
)

PROVIDERS: dict[str, Provider] = {item.key: item for item in (DEEPSEEK, ALIYUN)}

# 默认优先级：能用 DeepSeek 就用 DeepSeek（一把 key、一个模型覆盖 OCR 与分析）
DEFAULT_ORDER: tuple[str, ...] = ("deepseek", "aliyun-maas")

PROVIDER_ENV = "LAWCASE_AGENT_WORKER_PROVIDER"


class ProviderError(RuntimeError):
    """供应商配置不完整：由调用方转成数据路径门阻断。"""


def _first_key(env: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_provider(env: dict[str, str], *, requested: str = "") -> tuple[Provider, str]:
    """按配置或密钥可用性选定供应商；返回 (供应商, API 密钥)。

    显式配置 ``LAWCASE_AGENT_WORKER_PROVIDER`` 时严格按它；否则按
    DeepSeek → 阿里云 的顺序，取第一个密钥齐全的供应商。
    """
    explicit = (requested or env.get(PROVIDER_ENV, "")).strip()
    if explicit:
        provider = PROVIDERS.get(explicit)
        if provider is None:
            raise ProviderError(
                f"未知模型供应商「{explicit}」；可选：{'、'.join(sorted(PROVIDERS))}")
        key = _first_key(env, provider.key_env_names)
        if not key and provider.key == "aliyun-maas":
            # 阿里云还需要业务空间
            raise ProviderError("环境文件缺少 Qwen API 密钥")
        if not key:
            raise ProviderError(f"环境文件缺少 {provider.key} 的 API 密钥")
        return provider, key

    for name in DEFAULT_ORDER:
        provider = PROVIDERS[name]
        key = _first_key(env, provider.key_env_names)
        if not key:
            continue
        if provider.key == "aliyun-maas" and not str(
                env.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID", "")).startswith("ws-"):
            continue
        return provider, key

    raise ProviderError(
        "环境文件里没有可用的模型密钥：请配置 "
        + " 或 ".join(PROVIDERS[name].key_env_names[0] for name in DEFAULT_ORDER))


def endpoint_for(provider: Provider, env: dict[str, str]) -> str:
    """阿里云按业务空间拼端点；DeepSeek 用官方 base_url。"""
    if provider.key == "aliyun-maas":
        workspace = str(env.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID", "")).strip()
        if not workspace.startswith("ws-"):
            raise ProviderError("环境文件缺少有效的 Qwen 业务空间")
        return (f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
                "/compatible-mode/v1/chat/completions")
    return provider.endpoint


def image_tokens(provider: Provider, width: int, height: int) -> int:
    """估算一张图片计入的 token（用于批次控制与预算预留）。

    DeepSeek：每张图片有公开上限（1024），且会先把图缩放到约 1300×1300；
    这里按像素比例估算并封顶，宁可略高估（预算是护栏，不是账单）。
    """
    if provider.key == "deepseek":
        scaled = min(width * height, 1300 * 1300)
        estimate = int(scaled / (1300 * 1300) * provider.image_token_cap)
        return max(1, min(provider.image_token_cap, estimate or 1))
    return (width * height) // (32 * 32) + 2


def provider_summary(provider: Provider, *, when: datetime | None = None) -> dict:
    """给报告与页面用的供应商说明（不含密钥）。"""
    cached_rate, input_rate, output_rate = provider.rates(when)
    return {
        "key": provider.key,
        "label": provider.label,
        "model": provider.model,
        "region": provider.region,
        "vision": provider.supports_vision,
        "pricing_cny_per_million": {
            "input_cached": str(cached_rate),
            "input": str(input_rate),
            "output": str(output_rate),
            "peak": provider.is_peak(when),
        },
    }
