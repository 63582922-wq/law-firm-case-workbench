# ADR-0089：会话撤销的精确行可见性

状态：已部署至本机受管 Alpha；不是商用发布。2026-09-06。

真实合成入卷结束后，会话网关 revoke 返回 false，但管理员核对该会话未撤销。
0024 的 UPDATE 策略允许 app.web_session_id，SELECT 策略仅允许律所或 token 摘要；
UPDATE 的 WHERE/RETURNING 同样受 SELECT 行可见性约束，因此精确编号撤销看不到目标。
0048 的精确编号 SELECT 只授予 ledger confirmation owner，不能帮助会话网关。

新增 0079：仅 lawcase_web_session_gateway 获得精确 app.web_session_id 的 SELECT 策略。
不修改旧迁移，不扩大 PUBLIC/应用/目录角色权限，不授予枚举、BYPASSRLS 或额外写列；
现有 append-then-revoke 触发器、UUID 校验、仅 revoked_at 更新和服务端会话解析继续有效。
该角色原本受信任可创建会话并撤销指定会话；编号仍由服务端取得，不新增浏览器编号入口。

验收需真实受限角色证明：无选择器为零、精确目标可撤销、其他会话不可见、回滚无残留。
不能将管理员清理临时会话视为注销功能通过，也不能通过吞掉异常掩盖未撤销状态。

本机升级前核对全部 77 条已应用迁移与源码 SHA-256 一致、无 APPLYING；停应用消费者并
保存私有数据库备份后，经现有 postgres-migrate 一次性入口依序应用 0078/0079。
postgres-assert 的 TLS/角色/RLS/迁移/身份隔离检查通过，两个专用所有者保持 NOLOGIN、
非超级用户。恢复原应用容器后，真实 WebSessionAuthority.issue → revoke 成功退出，
数据库确认新会话已撤销；没有管理员代撤销、模型请求或重新上传。
此结果证明本机受管会话服务撤销链，不证明浏览器完整登录/注销旅程或商用验收。
