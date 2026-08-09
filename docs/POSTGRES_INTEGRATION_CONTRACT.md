# PostgreSQL 集成契约（内部 Alpha 后续阶段）

状态：`已实现案件核心及事实/交易命令适配器；未配置或执行集成测试数据库`

## 目的与边界

`case_kernel.postgres_store.PostgresMatterStore` 是案件状态机的生产标识适配器；`case_kernel.case_ledger_postgres.PostgresCaseLedgerStore` 负责事实、诉请、回应、争点、交易、付款分类/分配与重复组命令。它们要求 PostgreSQL 16+、顺序执行 `backend/migrations/0001_core.sql` 与 `backend/migrations/0002_case_ledgers.sql`，并由身份/成员关系服务预先创建 UUID 律所、用户及案件角色数据。

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
