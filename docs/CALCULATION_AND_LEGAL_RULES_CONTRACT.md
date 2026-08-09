# v1 计算与法律规则契约

状态：`待执业律师和法律编辑批准`

本文件冻结的是系统如何保存、执行和复算已批准规则，不预先决定任何个案的利率、期间、冲抵顺序或法律结论。正式规则只能来自已核验的官方法源及其适用/过渡依据。

实现状态：`0005_formal_calculations.sql` 与 `0006_legal_source_rules.sql` 已形成持久化控制链；正式计算只接收规则包标识/哈希，规则段及年利率由服务端读取。官方来源自动抓取、来源内容解析、LPR 参数机器核验、专用 PostgreSQL 实迁移和执业律师规则验收仍未完成。

## 1. 硬边界

- v1 只允许人民币 `CNY` 进入正式计算；外币原币种、金额和证据位置可保存，但必须阻断正式计算与法院提交输出。
- 金额使用十进制或最小货币单位，禁止浮点数；所有输入、舍入、显示和合计均保留规则版本。
- 法律规则、付款性质、同日先后和期间首尾均须在计算前由有权限律师批准；模型只能提出候选，不得生成正式金额。
- 任何未确认日期、币种、金额、付款性质、规则版本、过渡规则或冲突锚点都使场景状态为 `BLOCKED`。

## 2. 时间与事件模型

计算使用中国标准时间 `Asia/Shanghai`，但借款与付款的法律计算默认保存为 `local_date`，不从文件时间戳推定法律事件日期。

```text
ApprovedEvent
  event_id / event_type / local_date / time_precision
  amount / currency / source_evidence_ids
  legal_effect / approved_by / approval_hash
  same_day_sequence / decision_basis
```

`event_type` 至少包括：`CONTRACT_SIGNED`、`DISBURSEMENT`、`PAYMENT`、`INTEREST_PAYMENT`、`PRINCIPAL_REPAYMENT`、`DEFAULT`、`CLAIM_FILED`、`CASE_ACCEPTED`、`JUDGMENT`、`RULE_EFFECTIVE`。

期间内部统一采用半开区间 `[start_date, end_date)`；面向律师的“起算/截止是否包含”由 `date_inclusion_rule` 显式显示，并由该规则转换为内部区间。任何同日多事件若会改变本金或利息，必须有 `same_day_sequence` 和证据/律师决定；缺失时阻断，不使用默认猜测。

## 3. 场景输入契约

```text
CalculationScenario
  scenario_id / matter_id / version / status
  principal_obligations[] / approved_events[]
  legal_bundle_id / allocation_policy_id
  date_inclusion_rule / same_day_order_policy
  rounding_rule / currency_policy
  approved_by / approval_hash / input_hash
```

`principal_obligations[]` 必须区分借据记载本金、实际出借、对方主张、本方承认和待决差额；不得把其中任一字段自动作为正式本金。

`allocation_policy_id` 不得由算法自行选择。它必须连接至律师批准的合同约定、法律规则或个案决定，并记录适用条件、优先级和来源。

每笔付款事件还须声明经律师批准的 `payment_application`：`BY_POLICY`（适用情景冲抵顺序）、`INTEREST_ONLY`（仅用于未付利息）或 `PRINCIPAL_ONLY`（仅用于本金）。后两种模式的余额保留为未适用付款，禁止引擎为了“用完金额”自动冲抵另一类别；其来源和多债务分配由 [交易台账与付款性质契约](TRANSACTION_LEDGER_CONTRACT.md) 约束。

## 4. 法律适用锚点与规则包

每个正式 `RuleVersion` 关联一个可验证 `ApplicabilityRule`：

```text
rule_id / rule_version / official_source_url / source_sha256
authority_level / effective_from / effective_to
trigger_event_type / trigger_date_source / conditions
transition_rule_ids / conflict_resolution / reviewer / approval_hash
```

计算前构建不可变 `CaseLegalBundle`，将合同成立、出借、付款、违约、起诉、受理和裁判等已批准日期映射到候选规则。若不同触发日期、过渡条款或证据支持多个合理规则，必须保留并列候选并阻断正式场景；只有律师确认具体适用路径后方可执行。

规则声明的每个 `required_fact_key` 必须由主办律师绑定到同案、已有证据定位且状态为 `CONFIRMED` 的事实。浏览器、模型或规则编辑器仅提供事实键文本不能满足前置条件；绑定事实一旦失效，规则包、计算与提交材料随之失效。

利率变化、法定上限、LPR 或合同约定的任何变化都以 `RuleSegment` 表示，不允许把“当前利率”回填历史期间：

```text
RuleSegment
  start_date / end_date / rate_formula / cap_formula
  source_rule_version / applicability_anchor / approved_by
```

本产品不得把“规则名称”或“模型引用”当作公式依据；每个公式参数均须能回跳至批准的规则版本和精确条款。

`LPR_MULTIPLE` 必须同时绑定两类不同快照：一是证明“按 LPR 若干倍”法律公式的法律/司法解释快照，二是证明具体基准数值及日期的官方利率数据快照，并保存参数定位说明。两者不能复用同一快照；任一快照被替代、撤销许可或失去核验状态时，依赖规则与计算失效。

## 5. 确定性执行与输出

Worker 按已批准的事件、规则段、期间边界和分配政策生成不可变 `CalculationRun`：

```text
CalculationRun
  run_id / scenario_version / engine_version / inputs_hash
  line_items[] / invariant_results[] / output_hash
  started_at / ended_at / status / stale_reason

CalculationLineItem
  period_start / period_end / opening_principal
  rate_expression / rate_value / day_count / interest
  payment_id / payment_amount / allocated_interest / allocated_principal
  closing_principal / evidence_ids / rule_segment_id
```

每一行显示期间、本金余额、公式、日数、利率来源、付款分配、期末余额和证据链接。修改任一上游事实、规则、日期、付款性质、分配政策或舍入规则时，旧 `CalculationRun` 必须标为 `STALE`，不得覆盖或重新贴标签为当前结果。

## 6. 舍入与不变量

`rounding_rule` 必须版本化，至少声明：内部精度、每期间舍入或最终舍入、半舍规则、最小单位和展示精度。没有批准的舍入规则不能输出正式合计。

每个正式运行至少验证：

1. 相同已批准输入与引擎版本生成相同输出哈希；
2. 每一笔付款的利息与本金分配之和不超过该笔付款；
3. 本金只因经批准的出借、冲抵或偿还事件变化；
4. 相邻期间连续且无重叠、无遗漏；
5. 每期末余额等于期初余额加减已批准本金事件；
6. 各行利息、付款分配与汇总可独立相加复算；
7. 非 CNY、阻断事件、未批准规则或已失效依赖均不能进入正式输出。

正式计算需要两个独立实现或等价的独立复算路径；差异为零前不得供文书或提交包使用。

## 7. 程序期限边界

送达、起算、届满、节假日、顺延、管辖地域和法院特别通知进入独立 `DeadlineRule`/`DeadlineCalculation`，不能与利息计算共用未经区分的“日期加减”函数。法院通知不完整、送达方式不明或规则冲突时，只显示“待律师确认”，不得显示可依赖倒计时。
