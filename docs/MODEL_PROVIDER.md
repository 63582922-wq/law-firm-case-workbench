# 模型供应商：一把 key、一个模型跑全流程

状态：`v1 · 已切换到 DeepSeek，真实案卷跑通`

## 1. 为什么改了

原来 OCR（读扫描件）与案件分析分别依赖视觉模型，配置里写死了 `qwen3-vl-plus`。
`deepseek-flash` 现在**原生支持图片输入**，因此整条流水线（OCR → 分析 → 答辩状）
用同一个模型、同一把 key 即可，不再分两个模型。

## 2. 现在怎么工作

```
材料图片/扫描页 ─┐
起诉状文本     ─┼─▶ deepseek-flash（同一个模型）
律师参数/付款   ─┘        │
                          ├─ OCR 转写（JSON）
                          ├─ 案件分析决策包（JSON → 门禁）
                          └─ 答辩状论证文字（JSON → 门禁）
```

配置在模型环境文件里（本机为 `deployment/local-managed-test/runtime/local-managed.env`，
该文件被 `.gitignore` 忽略，不进 Git）：

```text
LAWCASE_AGENT_WORKER_PROVIDER=deepseek
LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY=sk-…
```

指定 `LAWCASE_AGENT_WORKER_PROVIDER` 时严格按它；不指定则按
**DeepSeek → 阿里云 MaaS** 的顺序取第一个密钥齐全的供应商。阿里云配置
（`LAWCASE_AGENT_WORKER_QWEN_API_KEY` + `..._WORKSPACE_ID`）保持兼容，可随时切回。

## 3. 供应商能力与限制（写进代码，不是靠记忆）

| 项 | deepseek-flash | qwen3-vl-plus（阿里云 MaaS） |
|---|---|---|
| 端点 | `https://api.deepseek.com/chat/completions` | `https://<workspace>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions` |
| 图片输入 | 支持（`image_url` + base64 data URL，仅 user 消息） | 支持 |
| 单图上限 | 32 MiB（内联）、单边 ≤8192 像素 | — |
| 请求体上限 | 48 MiB | — |
| 单图 token | 封顶 1024 | 按像素估算 |
| 思考模式 | **默认开启**，本流水线显式 `{"thinking":{"type":"disabled"}}` | `enable_thinking: false` |
| JSON 输出 | `response_format: {"type":"json_object"}` | 同 |
| 上下文/输出 | 1M / 最大 384K | — |

## 4. 计费（元 / 百万 tokens，按供应商公开价目）

| 时段 | 缓存命中输入 | 缓存未命中输入 | 输出 |
|---|---|---|---|
| 高峰（周一至周五 9:00–12:00、14:00–18:00 北京时间） | 0.04 | 2 | 8 |
| 空闲（其余时段与周末） | 0.02 | 1 | 4 |

- 价格只用于**预算门禁与记账**，集中在 `case_kernel/model_providers.py` 一处；
- 账本按实际用量与供应商价目计算，并**区分缓存命中**（`prompt_cache_hit_tokens`）；
- 节假日无法在本机判断，一律按空闲档计价——宁可少算，不虚增律师的账。

## 5. 切换供应商时的两个真实坑（已修，并有测试）

1. **模型名不能写死默认值**：旧默认 `"qwen3-vl-plus"` 被发到 DeepSeek，
   返回 `HTTP 400 invalid model name`；现在模型名由供应商解析，缺省为空。
2. **OCR 缓存必须按供应商+模型隔离**：否则换了模型仍复用上一家的识别结果；
   缓存文件现在记录 `provider`/`model`，不一致即重新识别。

另外，供应商拒绝请求的原因（HTTP 4xx 的响应体）现在会写进账本与错误信息，
不再只显示"被拒"。

## 6. 实测（合成木业真实案卷，23 份材料 / 73 页）

| 环节 | 结果 |
|---|---|
| 完整分析（7 次 OCR + 1 次分析） | `COMPLETED`，**¥0.061408**，门禁 `MARK_FOR_REVIEW` |
| OCR 调用 | 7 次，单次 ¥0.0023–0.0073（按图片 token 计） |
| 分析调用 | 1 次，25,943 tokens，¥0.034382 |
| 答辩状草稿 | 1 次调用 **¥0.012465**，`MARK_FOR_REVIEW`，草稿可导出 |
| 报告抬头 | 「分析来源：deepseek-flash（真实调用）」 |
| 预检记录 | `provider: deepseek-official`、`model: deepseek-flash`、`region: cn` |

> 对照：同一案件此前用 Qwen 完成一次完整分析约 ¥0.1267；DeepSeek 约 ¥0.0614。

## 7. 当前边界

- 只装配了 `deepseek-flash`；`deepseek-v4-pro` 不支持图片输入，因此不能用于本流水线；
- 计费为估算上限（图片 token 按上限估），以供应商账单为准；
- 换供应商后第一次运行会重新 OCR（缓存按供应商隔离），费用按新材料量计算。
