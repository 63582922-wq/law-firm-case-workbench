# ADR-0014：WebView 正式案件请求必须使用原生短时会话和动态 loopback

状态：已接受（客户端、原生 grant 到期门与服务端 CORS 完成；专用 PostgreSQL 和生产登记仍未配置）

## 背景

旧的持久预览前端依赖构建时固定 API 地址和 Cookie。桌面 sidecar 每次启动使用随机 `127.0.0.1` 端口，身份 authority 又签发最长 30 分钟的进程内 Bearer；如果继续使用固定端口或 Cookie，WebView 无法进入真实受控链。反过来，把 Bearer 写入环境变量、URL、本地存储或普通运行状态，会扩大泄露和重放范围。

## 决定

1. 全部正式案件读取和写入统一经过 `persistent-api-client.ts`。合成 Alpha 请求保持独立，不能在持久请求失败时回退。
2. Tauri WebView 每次请求前通过专用原生命令取得当前 grant；JavaScript 不把 grant 写入 localStorage、sessionStorage、Cookie、URL、日志或全局配置。
3. 原生命令只有在受控进程、已重验登记、短时会话和专用数据库同时就绪时返回；Rust 在返回前再次核对 RFC 3339 到期时间，过期或异常时清除 access token、session ID 和到期时间。
4. 桌面 API 地址必须精确为 `http://127.0.0.1:<有效端口>`，不能包含用户名、密码、路径、查询或片段。若构建配置另给地址，必须与原生 grant 完全一致。
5. 普通案件请求由客户端注入桌面 Bearer，调用方不能覆盖 `Authorization`；浏览器不发送 Cookie。服务端只允许 `tauri://localhost` Origin 的 `GET/POST` CORS，并仅开放所需请求头和可核验响应头。
6. PDF、ZIP 和 PNG 内容端点继续使用各自一次性 Bearer。客户端只用原生 grant 解析受控动态端口，不能用桌面身份 Bearer 替代派生件读取许可。
7. 客户端强制 `no-store`、`no-referrer`；401 不自动重试写命令，也不静默换身份，而是提示重启并重新核验。
8. 非桌面内部预览仍可在显式安全 API 地址下使用服务端 Cookie，作为开发边界；这不构成桌面生产身份或真实案件放行。

## 已验证

- 正式案件代码中不再直接拼接固定 API 地址，全部进入统一客户端；
- 桌面 grant 校验数字 loopback、令牌格式、UUID session 和最多 31 分钟的到期窗口；
- 调用方不能覆盖桌面 `Authorization`，派生件一次性令牌必须显式存在；
- 服务端允许 `tauri://localhost` 所需预检，拒绝未登记 Origin；
- Rust 过期测试证明令牌、session ID 和到期时间会在向 WebView 返回前清除；
- 默认 `NOT_CONFIGURED` 桌面仍无法取得 grant，也没有任何案件路由。

## 仍未放行

- 专用 PostgreSQL 的真实迁移、备份恢复和逐案 RLS 整链；
- 生产信任根与真实签发、续期、撤销服务；
- sidecar 将 persistent app、Store、Worker 和生命周期统一装配；
- 休眠唤醒后的即时重验、无重启续期和远程撤销推送；
- 真实律师、真实案卷与安全负责人验收。
