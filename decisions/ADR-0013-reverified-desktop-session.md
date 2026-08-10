# ADR-0013：已保存登记必须在每次启动重新验签后才能建立短时会话

状态：已接受（合成信任、Keychain runner、sidecar 与 Rust 父进程链完成；生产信任和专用数据库未配置）

## 背景

把一个曾经通过验签的登记包写进 Keychain，不代表它在下次启动时仍然有效。目录可能已到期或升级，签发公钥可能退休/撤销，登记可能过期，安装秘密也可能变化。如果桌面端只看“Keychain 有一项”就恢复登录，会绕过当前信任和设备绑定。另一方面，即使律师身份有效，也不代表其拥有任一案件权限。

## 决定

1. sidecar 只在生产信任目录为 `READY` 时读取 Keychain；目录未配置时不触发系统凭证访问，目录核验失败时身份直接 `BLOCKED`。
2. Keychain 返回码 44 只表示“尚无登记”，状态为 `NOT_ENROLLED`；其他读取失败、无效 JSON/签名、未知或撤销公钥、过期、设备绑定不符及剩余有效期不足均为 `BLOCKED`，不得降级成匿名或合成身份。
3. 有效登记在每次 sidecar 启动时按当前信任目录重新验签，再从服务端字段恢复 actor、firm 和专业角色。浏览器、文件夹、macOS 用户名和当前页面均不能补写或改变身份。
4. Rust 父进程在启动握手中生成 256 位随机私有令牌。登记有效时，sidecar 用该令牌建立一次性、最长 30 秒的 session bootstrap；Rust 立即通过数字 loopback 和 `tauri://localhost` Origin 交换最长 30 分钟 Bearer。
5. bootstrap 只能使用一次；会话有效期不得超过登记凭证到期。交换失败会使整个本机服务状态 `BLOCKED`，而不是继续显示已登记。
6. Bearer、父进程令牌和 session ID 不进入普通运行状态 JSON、DOM、日志或本地存储。Rust 使用零化字符串保存在进程内；应用退出时清除并终止 sidecar。
7. WebView 只能在“本机会话 `READY` 且专用数据库 `CONFIGURED`”同时成立时，通过专用 Tauri 命令取得易失会话授权；当前数据库未配置，因此该命令按设计拒绝返回令牌。
8. 身份会话仍只回答“是谁”。每个案件读取和写入必须由 PostgreSQL 再检查 active user、律所隔离和未撤销的逐案角色。当前禁用 sidecar 不挂载案件路由。
9. 导入新 `.lawenroll` 后提示重启桌面应用；当前版本不在同一进程热替换 identity authority，避免旧会话与新登记并存。

## 已验证

- 信任未配置或失败时绝不读取 Keychain；缺少登记与损坏登记产生不同状态；
- 有效登记可创建 process-local authority，过短有效期不能创建会话；
- 一次性 bootstrap 只交换一次，错误 Origin、非数字 loopback 和错误令牌均失败；
- Rust 实际通过数字 loopback 发送 Origin/bootstrap 并严格解析响应，未知字段和非法令牌格式被拒绝；
- 运行状态不包含 access token、父进程令牌或 session ID；数据库未配置时 WebView grant 命令拒绝；
- 默认安装包仍显示 `NOT_CONFIGURED / NOT_ENROLLED`，没有任何真实案件路由。

## 仍未放行

- 生产根、真实签发/续期/撤销后台和管理员 MFA；
- 专用 PostgreSQL 迁移、备份恢复、连接健康与逐案权限真实整链；
- WebView 持久 API 客户端的会话内存注入、到期刷新和 401 恢复；
- 登记热更新、远程撤销推送、休眠唤醒重验和设备遗失演练。
