# Phase 0 田野研究与金标准案件协议

状态：`待执行`

本协议落实 `RESEARCH_PLAN.md`，用于收集可开发的流程证据，而不是收集当事人隐私或要求律师提供真实案件策略。

## 1. 访谈前检查

每位参与者使用匿名编号，例如 `L-01`。研究记录不得保存客户姓名、案号、身份证号、电话、账户、聊天原文、未公开裁判文书或可识别截图。

开始前记录：

| 字段 | 必填 | 说明 |
|---|---|---|
| 参与者编号 | 是 | 与身份信息分离保存 |
| 角色与团队规模区间 | 是 | 仅记录承办/协办/助理/负责人和规模段 |
| 高频案由与年限区间 | 是 | 不记录律所或客户名称，除非另有书面同意 |
| 记录同意 | 是 | 仅限笔记、音频或屏幕演示中的已同意范围 |
| 禁止范围确认 | 是 | 不展示未脱敏客户材料，不录入第三方工具 |

最低同意文本：参与者理解本研究用于改进律所辅助工具；可跳过任何问题；研究记录不保存可识别客户资料；不同意不会影响任何合作或服务。

## 2. 访谈记录模板

每次访谈按同一字段记录，原始笔记保存在经授权的研究存储，不进入 Git：

| 字段 | 记录要求 |
|---|---|
| 最近一件同类案件的材料入口 | 格式、来源、接收方式和首个责任人 |
| 实际工作流 | 去重、筛页、OCR、身份核对、台账、法律、文书、提交的顺序 |
| 工具与人工切换 | 工具名称类别、重复输入、等待、纸面/表格对照 |
| 耗时 | 角色、步骤、估算范围及是否为返工 |
| 错误或近失误 | 发生条件、发现方式、后果级别和现有补救 |
| 审批与交接 | 谁能确认付款性质、法律规则、计算、提交版 |
| 信任与数据顾虑 | 上传、第三方处理、部署、审计、删除要求 |
| 购买信号 | 预算口径、决策者、不可接受错误和试用条件 |
| 研究者结论 | 事实、推断、待验证假设必须分栏 |

访谈有效的最低标准：完成“最近一次案件”与“购买行为”两组问题，至少给出一个可观察的工作流例子和一个停止使用条件。不能仅记录“觉得有用”。

## 3. 历史案件观察协议

观察只允许已办结、已脱敏且使用获得授权的材料。每个观察使用编号 `O-01`，记录：

1. 工作从收到材料到准备提交的实际顺序；
2. 每次文件、WPS/PDF、表格、法律库和人工沟通之间的切换；
3. 关键决定的责任人、依据与返工；
4. 关键证据、利息期间、付款性质和最终版本的定位耗时；
5. 不确定性是否被显式标出，还是被口头解决。

研究者不得要求演示者打开未脱敏客户资料；如演示中意外出现，立即停止记录并删除该段未授权笔记或截图。

## 4. 金标准案件 Manifest

每个金标准案件只保留脱敏版本，并由两名具备相应权限的人员完成“标注—复核”。Manifest 至少包含：

```text
gold_case_id / deidentification_review_id / permitted_use
source_file_id / source_file_sha256 / page_count
page_id / page_disposition / disposition_reason / reviewer
duplicate_group_id / duplicate_kind / canonical_page_id
entity_id / alias / identity_confidence / lawyer_decision
claim_id / claim_text_source / requested_amount / currency / period
transaction_id / date / amount / currency / source_page / classification
legal_anchor_id / event_type / approved_date / source_evidence
rule_id / authority_version / transition_rule / approval_id
calculation_scenario_id / inputs_hash / expected_line_items_hash
document_expectation_id / approved_fact_ids / approved_rule_ids
submission_manifest_hash / final_reviewer / locked_at
```

`page_disposition` 只能为 `IN_SCOPE`、`OUT_OF_SCOPE`、`DUPLICATE_EXACT`、`SIMILAR_REVIEW_REQUIRED` 或 `UNREADABLE`。除 `IN_SCOPE` 外的每页仍必须有人工理由；相似页不得自动剔除。

## 5. 复核与冻结

- 标注人不得独自批准自己制作的正式计算答案或最终提交包；
- 复核人发现证据、分类、法律版本或计算不确定时，标记 `BLOCKED`，不以多数意见或模型建议解决；
- 每个案例冻结时生成 Manifest 哈希、答案版本和批准记录；修改上游标注必须使下游计算、文书期望和提交结构失效；
- Git 只保存合成夹具或不可反推真实案件的结构性测试数据。脱敏原件与完整 Manifest 存放在获得授权的受控存储。

## 6. 研究结论门

完成 10 次有效访谈和 3 次观察后，输出一份不含案件隐私的结论表：

| 假设 | 证据数 | 反例 | 结论 | 对范围/设计的影响 | 批准人 |
|---|---:|---|---|---|---|

未达到 `RESEARCH_PLAN.md` 的成功门槛时，应调整信任、部署或收费假设；不得用更多功能替代缺失的市场证据。
