# 运行模式选择契约

状态：`已实现显式配置、依赖注入、Tauri 文件夹选择桥与受监护本机 API sidecar；生产持久化运行仍未开放`

## 默认行为

应用默认且无条件使用 `synthetic-alpha`：内存仓库、固定合成角色、`alpha_*` 标识符，不读取数据库地址、不建立 PostgreSQL 连接，也不接收真实案卷。导入 `case_api.app` 不会读取环境变量；只有桌面启动器显式调用 `load_runtime_settings()` 才会解析运行模式。

## 内部持久化预览门

选择 PostgreSQL 必须同时满足：

1. `CASE_WORKBENCH_RUNTIME_MODE=postgres-internal-preview`；
2. `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES`；
3. `CASE_WORKBENCH_POSTGRES_DSN` 指向库名以 `_preview` 或 `_test` 结尾的专用数据库；
4. 后续独立 API 提供 UUID 身份，并由数据库内未撤销的案件成员角色再次授权。

任一条件不满足即阻断。合成模式出现任何持久化配置也会阻断，避免配置残留让测试页面误连数据库。配置对象的文本表示不包含 DSN，日志和错误信息不得输出密码或完整连接串。

## 尚未开放

- 现有 `case_api.app` 仍是合成 Alpha，不能因存在 `RuntimeServices` 就切换为 PostgreSQL；
- 尚无真实律所激活/续期/撤销服务、生产信任根运营、MFA 或数据库迁移部署器；后端已实现签名登记、设备绑定、门限签名信任目录、无角色激活请求、同身份续期、远程撤销与本机停用接口；Tauri 已可通过原生 Keychain API 初始化/回读安装秘密、读取脱敏状态、只停用本机登记，只从系统文件选择器导入经当前目录验签的 `.lawenroll`，或用无参数命令打开 macOS 原生密码框交换一次性激活码；但没有真实签发身份时不得装配为登录；
- 原件单页预览代码要求本机 `pdftoppm`，并以独立临时目录生成 PNG；正式安装器必须固定并校验该渲染器版本；Tauri 桌面桥已只返回用户通过原生对话框选择的目录，不接受网页手填路径、文件系统根目录或整个用户主目录；
- 桌面壳已把 Python 3.12/FastAPI/uvicorn 编译为自包含 sidecar，并通过私有 stdin 随机挑战、回执摘要、动态 `127.0.0.1` 端口和父进程退出联动完成启动/监护；该服务当前只开放无文档的禁用态 `/healthz`，不开放案件、身份、Worker、PostgreSQL 或 `pdftoppm` 路由，也不提供真实律师登录身份；
- 桌面 `身份与安全` 页分开显示进程、生产信任目录、签名登记、数据库和案件访问；用户可显式初始化安装秘密或只停用本机登记，但不能自选角色或创建凭证。只有信任目录 `READY` 后才开放原生登记包选择；健康进程不会冒充已登录。
- 已保存登记不会仅凭 Keychain 项存在恢复登录：sidecar 每次启动按当前目录重新验签，成功后才由 Rust 使用一次性父进程令牌交换短时会话。令牌不进入运行状态或持久存储；数据库未 `CONFIGURED` 时 WebView 不取得会话 grant，案件访问仍禁用。
- WebView 持久 API 客户端已使用原生 grant 解析动态数字 loopback 并注入短时 Bearer；固定预览地址若存在必须完全匹配，派生 PDF/ZIP/PNG 继续使用各自一次性许可。服务端仅允许 `tauri://localhost` 所需 CORS；会话过期在 Rust 和客户端双重阻断且不自动重试写命令。
- 续期/远程撤销只能使用签名目录中的 HTTPS Origin 与 TLS SPKI pin；sidecar 不跟随重定向、不自动重试，重新验签后由 Rust 按当前 Keychain 凭证哈希 CAS 提交。远程撤销先停止会话再删除本机凭证；默认 `NOT_CONFIGURED` 运行不挂载这些端点。
- 在线激活使用同一签名目录固定的 HTTPS/SPKI：激活码只进入 `NSSecureTextField` 和可清零的父进程缓冲区；sidecar 从 Keychain 读取安装绑定、拒绝现有登记并验签返回 envelope，Rust 只以“当前无登记”为 CAS 条件保存。默认 `NOT_CONFIGURED` 不显示输入框、不挂载端点。
- 尚未在获授权的专用 PostgreSQL 上执行集成测试；
- 当前门只允许继续开发内部持久化预览，不构成真实案件接入或生产放行。
