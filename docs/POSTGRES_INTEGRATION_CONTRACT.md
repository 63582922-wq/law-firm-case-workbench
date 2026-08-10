# PostgreSQL 集成契约（内部 Alpha 后续阶段）

状态：`已实现案件核心、事实/交易、证据 Manifest、正式计算、法律来源/规则包、官方抓取队列及提交编译适配器；未配置或执行集成测试数据库`

## 目的与边界

`case_kernel.postgres_store.PostgresMatterStore` 是案件状态机的生产标识适配器；`case_kernel.case_ledger_postgres.PostgresCaseLedgerStore` 负责事实、诉请、回应、争点、交易、付款分类/分配与重复组命令。证据使用 `PostgresEvidenceIntakeStore`（扩展 Manifest Store）保存版本化案卷盘点、逐文件材料接收租约、不可变原件/页、页级处置、重复结论、红框坐标、锁定 Manifest 和派生件谱系。`case_kernel.legal_source_postgres.PostgresLegalSourceStore` 保存经人工核验的官方来源快照、服务端派生的规则版本、案件法律事件、关键事实绑定和不可变规则包；`PostgresOfficialSourceCaptureStore` 独立保存律师授权、一次抓取租约、加密内容回执、解析摘要和 append-only 复核结论。`case_kernel.formal_calculation_postgres.PostgresFormalCalculationStore` 保存逐期本金、利息、逐笔冲抵和独立复算谱系。`case_kernel.submission_postgres.PostgresSubmissionStore` 保存已批准法院 PDF 成品、QA 编译规格、文件顺序和名称、四项正式依赖以及已核验导出。`PostgresAgentExecutionStore` 仅保存 Agent 计划、准许使用的 Skill/Tool、范围/审批门、输入输出哈希和执行结果；它不保存提示词或案卷正文，且计划本身不授予执行权限。`PostgresExternalRequestStore` 保存外部模型、OCR 或 MCP 的律师预授权与系统 Worker 回执，固定字段标识、供应商/地域/保留/训练政策、次数/成本上限及输入/授权哈希；`UNKNOWN_SUBMISSION` 阻断自动重试。它们要求 PostgreSQL 16+，并按文件名顺序执行 `backend/migrations/0001_core.sql` 至 `backend/migrations/0017_external_request_ledger.sql`；身份/成员关系服务须预先创建 UUID 律所、用户及案件角色数据。

它们不属于当前 Web/API 的合成 Alpha 运行路径。`alpha_*` 标识符会在建立数据库连接之前被拒绝；这条限制防止测试页面意外写入持久化环境。台账适配器还会在数据库内复核调用人具有本案未撤销且用户状态有效的角色，不能只信任请求携带的角色声明。

## 每条命令的事务规则

1. 检查 UUID 与幂等键；
2. 连接后在同一事务中执行 `set_config('app.firm_id', ..., true)`；
3. 按调用人、案件、命令、幂等键取得事务级 advisory lock；
4. 复用同键同载荷的回执，拒绝同键异载荷；
5. 用 `FOR UPDATE` 读取案件并检查版本；
6. 执行领域状态机回调或受控台账命令；
7. 写入案件与台账对象、批准撤销/新增、提交包、append-only 审计和 Outbox；事实决定和交易确认等上游正式变化同时使有效提交包失效并清空当前指针；
8. 写入幂等回执并一次提交。

任何错误导致整笔事务回滚。全部 SQL 使用参数绑定；不得用字符串拼接案件内容、身份或输入金额。

案卷材料接收只允许主办律师基于当前已批准扫描建立；建立前通过 OS 绑定 grant 重算 Manifest。逐文件项目由 SYSTEM_WORKER 领取短租约，登记结果必须匹配该项目的相对路径、预期大小和 SHA-256，并保存本机扫描器/定义版本及结构检查哈希。新范围获批时旧接收任务统一 `STALE`，不删除已登记原件。

正式计算命令还要求：币种为 `CNY` 且精确到分、交易日期为精确日期、同日顺序已批准、重复组已解决、法律规则期间连续覆盖整个计算区间、规则版本属于当前案件规则包。旧正式情景和计算运行保留为 `STALE`；新运行只有在独立复算完全一致后才成为 `VERIFIED`。`0006` 已加入官方快照登记、规则版本、法律事件、确认事实键绑定和规则包审批命令。正式计算 API 不接收规则段或客户端利率，只能按 `legal_bundle_id`/哈希读取数据库中已批准分段。

正式来源快照只接受允许清单中的官方 HTTPS 域名，并要求配置本机加密对象读取器；登记前必须成功认证、解密对象并重新核对明文 SHA-256，不能只提交一个看似正确的对象键。LPR 倍数规则的法律公式来源与官方利率参数来源必须是两个不同快照，参数来源还必须标记为 `OFFICIAL_RATE_DATA` 并保存精确定位。自动抓取与确定性解析已经装入独立一次执行队列，成功状态固定为 `REVIEW_REQUIRED`；律师批准复核记录仍不能自动登记正式快照。发现清单、开发冒烟和解析摘要本身均不能进入正式规则包。

正式登记必须另外保存许可核验依据及其哈希。抓取转登记端点不接受对象键、发布者、效力层级、URL、内容哈希或条文定位的浏览器副本，而是锁定并读取同案抓取/复核记录、重新认证加密对象，再由服务端生成正式验证哈希。旧快照可以继续显示，但许可依据/哈希缺失时不得支撑新的规则版本或规则包。

提交编译命令只接受 `CNY`、同案 `APPROVED` 且面向 `COURT_SUBMISSION` 的 PDF。QA 哈希绑定文件标识、顺序、法院文件名、锁定证据 Manifest、已批准法律规则包、同规则包的 `VERIFIED` 计算及当前最终文本审批。SYSTEM_WORKER 对法院 ZIP 和独立内部清单执行第二次结构/哈希核验后才能登记导出；读取对象键只允许系统 Worker，律师快照不包含对象键。导出内容只可由主办/复核律师通过 OS 绑定会话和 loopback 一次性许可读取。

事实、诉请、交易等候选不会直接成为正式结论；律师确认、回应、争点确认、付款分类批准和重复组结论才触发正式上游失效。诉请回应到事实、争点到事实/诉请、付款分类到债务分配、重复组到原交易均使用同律所同案件的正规化关联表；重复候选的处理只选择规范交易，不删除任何来源交易。

## 受控集成测试

集成测试没有默认数据库地址，也不会自动启动容器或删除任何库。只有在以下两个条件同时满足时才可运行：

```sh
export CASE_WORKBENCH_TEST_DATABASE_URL='postgresql://.../lawcase_workbench_test'
export CASE_WORKBENCH_ALLOW_DESTRUCTIVE_TEST_DB='YES'
cd backend
.venv/bin/python -m unittest tests.test_postgres_integration -v
```

测试只接受数据库名以 `_test` 结尾的地址，并会清空该**专用测试数据库**的 `public` schema 后按文件名顺序执行全部迁移。因此不得将任何真实、共享、开发或生产数据库写入上述变量。

通过以下准备才允许配置该测试：独立数据库、无真实案件数据、可创建 `pgcrypto` 扩展的测试账号、明确的本地或 CI 环境授权。
