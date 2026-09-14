# ADR-0036：Qwen OCR 回执 ID 使用显式长度约束

- 状态：已接受
- 日期：2026-08-24

## 背景

真实受管 Agent Run 的首个扫描件调用在约 2.8 秒后进入通用 `UNKNOWN_SUBMISSION`。数据库服务器日志证明，Qwen 已返回并进入成功回执写入，但 PostgreSQL 在计算 `provider_request_id ~ '^[A-Za-z0-9._:-]{1,500}$'` 时以 `invalid repetition count(s)` 拒绝语句。PostgreSQL ARE 的区间上界不能表达 500；因此这是本地持久化约束缺陷，不是供应商网络超时或 OCR 拒绝。

失败事务没有保存供应商响应正文或请求 ID。既有未知请求继续保持未知并禁止重发，不能根据日志猜造 OCR 输出。

## 决策

0057 以前向迁移替换结果约束：字符集合使用 `^[A-Za-z0-9._:-]+$`，长度单独使用 `char_length(provider_request_id) BETWEEN 1 AND 500`。成功、已知失败和六类未知结果的其他追加式约束保持不变。

不改写已有 `UNKNOWN_SUBMISSION`，不从数据库日志恢复案卷内容，也不以相同外部请求再次调用 Qwen。本轮只能使用原任务图中尚未执行的其他材料继续受控验证，或在无法安全推进时保留真实阻断结论。

对于管理员已经以数据库错误日志哈希证明“供应商返回后，本地成功回执写入失败”的单个通用未知请求，0058 允许追加一条独立的本地失败解决回证。原未知 outcome 保持不变；Worker 对该回证只有读取权，并只会把任务终结为 `QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED`，不会产生 OCR 成果或再次发送请求。

## 后果

- 后续合法 Qwen 成功回执可以在 PostgreSQL 中持久化并接受精确查验。
- 原未知调用仍无可验证正文，不能被宣称为 OCR 成功。
- 本地失败解决回证必须绑定原 exchange、ACTIVE 同案 Worker、受控错误类型和外部日志哈希，且追加后不可更新或删除。
- Worker 只有解决回证表的 `SELECT` 权限；启动预检通过只读 `pg_catalog.pg_trigger` 核验保护触发器，不能为了 `information_schema` 可见性向 Worker 授予 `TRIGGER` 或写权限。
- 迁移测试必须同时证明 500 字符上限和原有未知/失败围栏未被放宽。
