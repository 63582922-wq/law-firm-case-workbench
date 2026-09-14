# ADR-0039：模型调用前故障必须确定失败，终态 Run 必须静默

- 状态：已采纳
- 日期：2026-08-25

## 背景

受管运行 `e11bef01-3e2d-567c-a219-fe9a3e3cb283` 已成功完成案件情境、Office、PDF、JPEG OCR 和 PNG OCR 五项任务；台账提取任务在通用外发 `STARTED` 记录之后、0045 台账 exchange 建立之前失败。0045 exchange 和 outcome 均不存在，因此按照代码中的固定顺序，`send_raw` 不可能已经执行。

旧实现把建立 exchange 的本地异常继续抛给 Worker。Worker 看到通用 `STARTED` 边界后只能把结果保守记为 `UNKNOWN`，而恢复路径在缺少 exchange 时又返回 `UNRESOLVED`，造成无法自行收敛的对账状态。同时，取消 Run 后 Worker 仍先执行快照刷新和 lease 回收；运行记忆检查点使用裸 `FOR UPDATE`，还会尝试锁定联接的 `users` 行，超出 Worker 最小权限。

## 决策

1. 0045 exchange 未成功建立时，统一返回 `LEDGER_EXCHANGE_NOT_CREATED`，记为 `FAILED / NOT_SUBMITTED / 0 external calls`。
2. 恢复时若找不到 0045 exchange，也返回同一确定失败；该分支只查询，不发送，不进入无限 `UNRESOLVED`。
3. 只有 exchange 已提交后发生的传输、响应持久化或结果提交不确定，才能进入 `UNKNOWN`，并继续遵守只查询、绝不重发。
4. Worker 在任何刷新、回收、规划或适配器调用之前读取投影；`COMPLETED`、`CANCELLED`、`FAILED` Run 直接返回 `IDLE`。
5. 运行记忆检查点只 `FOR UPDATE OF run`，不锁定用于授权校验的 `users` 和 `matter_actor_roles` 联接行。

## 后果

- 通用外发记录不再被误当作“供应商已经收到请求”的证据。
- 模型前本地故障会确定终止且保留明确错误码，不消耗外部调用额度，也不触发自动重发。
- 已取消和其他终态 Run 会被 inbox 收敛为 `QUIET`，不会制造周期性事故。
- Worker 继续使用最小权限；授权表只读校验，锁只落在要保护的案件运行行。
- 本 ADR 不改写既有事件，也不把本次 5/6 运行升级为端到端通过。
