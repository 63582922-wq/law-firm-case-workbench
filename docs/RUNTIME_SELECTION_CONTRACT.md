# 运行模式选择契约

状态：`桌面端已交付无需律所登记或 PostgreSQL 的本机基础案卷；受管 PostgreSQL 与 commercial-production 仍须完成各自的身份、运维和人工放行证据`

## 默认行为

后端 API / 普通开发导入默认使用 `synthetic-alpha`：内存仓库、固定合成角色、`alpha_*` 标识符，不读取数据库地址、不建立 PostgreSQL 连接，也不接收真实案卷。导入 `case_api.app` 不会读取环境变量；只有启动器显式调用 `load_runtime_settings()` 才会解析运行模式。

打包的 Tauri 桌面 sidecar 则明确以 `local-standalone` 为默认运行模式；这项默认仅属于已安装桌面应用，绝不会因为导入 Python 模块、打开浏览器开发页或运行普通 API 测试而创建本机案件账本。若桌面启动时显式设置了其他运行模式，仍须满足其完整的独立门槛。

## 本机基础案卷门

`local-standalone` 面向单台电脑上的律师首次使用。它可以真实地新建、列出和打开本机案件，选择或重新关联一份本机资料文件夹，并在律师明确点击后完成一次**只读**材料盘点。它不是 PostgreSQL 的缩小替身，也不把尚未具备的正式办案链伪装成可用。

1. 不需要律所登记、律所管理员、外部 PostgreSQL、受管对象库或外部网络；sidecar 就绪协议固定报告 `identity=LOCAL`、`persistence=LOCAL_CONFIGURED`、`workspace_mode=LOCAL_STANDALONE`、`local_workspace=READY`。`LOCAL` 仅代表本机进程会话，绝不代表律师登记、律所成员资格或商用受管模式授权；
2. 元数据账本仅位于 `~/Library/Application Support/cn.lawcase.workbench/local-workspace/local-workspace.sqlite3`（受控测试可用绝对路径 `CASE_WORKBENCH_LOCAL_WORKSPACE_ROOT` 覆盖）。目录要求 `0700`，SQLite 文件要求 `0600`；账本仅保存随机本机 actor/workspace UUID、案件题名/版本/阶段、资料文件夹显示名与 SHA-256 路径指纹（不保存路径原文）、审计/幂等记录和明确盘点产生的相对文件清单/哈希/类型；
3. 原始绝对路径、文件夹授权、原件内容、浏览器可用文件系统权限、律师姓名和律所身份均不写入 SQLite 或 HTTP 回执。原生文件选择产生的绝对路径只保存在 sidecar 内存的短时选择记录中，15 分钟到期且应用重启即失效；根目录、整个用户主目录和符号链接目录被拒绝；
4. WebView 只能通过短时本机会话读取脱敏案件与盘点结果。绝对路径注册、建案、重新关联与启动盘点只接受 Rust 桌面父进程的私有令牌，不能由网页、浏览器 Bearer 或手工 HTTP 请求冒充；盘点不修改、移动、上传或删除任何来源文件；
5. 本机基础案卷明确不开放正式事实确认、法律依据结论、利息/本息冲抵计算、文书或法院提交包、原件页处理、外部研究、OCR、DeepSeek、Qwen 或任何材料外发。模型 API Key 即使已经配置也不会被读取或调用；这些能力只能在独立的 `FIRM_MANAGED` 工作区经逐案预授权后使用。

本模式不可携带 `CASE_WORKBENCH_POSTGRES_DSN`、预览/商用确认变量或 Office 转换变量；任一残留即阻断，以免用户误把受管配置和本机基础案卷混在一起。它保存的是可复核的本机案件壳和真实目录盘点，不是演示数据，也不构成正式法律意见、利息结论或可向法院提交的文件。

## 内部持久化预览门

选择 PostgreSQL 必须同时满足：

1. `CASE_WORKBENCH_RUNTIME_MODE=postgres-internal-preview`；
2. `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES`；
3. `CASE_WORKBENCH_POSTGRES_DSN` 指向库名以 `_preview` 或 `_test` 结尾的专用数据库；
4. sidecar 已按当前受信目录重新验签本机登记，并取得 OS 绑定短时会话；
5. `CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT` 指向预先创建、非符号链接、非根目录且仅当前用户可读写的绝对目录；
6. macOS Keychain 的固定只读项可提供 32 字节 AES-256-GCM 密钥；
7. 数据库内未撤销的案件成员角色再次授权。

任一条件不满足即阻断。前六项全部满足时，sidecar 以 `PersistentApiDependencies` 直接启动持久 API：所有 PostgreSQL Store、加密对象库、一次性文件访问许可、案卷文件夹授权、原件单页预览、Agent 执行审计和外部调用预授权账本必须一次装配，不能只开其中一部分；向 Rust 桌面壳回报的状态固定为 `CONFIGURED`，不把内部运行模式名当作协议状态。合成模式出现任何持久化配置也会阻断，避免配置残留让测试页面误连数据库。配置对象的文本表示不包含 DSN，日志和错误信息不得输出密码或完整连接串。

需要执行抓取、转换或导出的独立 Worker 时，`CASE_WORKBENCH_SYSTEM_WORKER_ID` 只能指定 Worker 用户 UUID；律所范围必须由当前已重新验签的桌面登记派生，忽略任何环境或网页给出的 `firm_id`。Worker 仍需由 PostgreSQL 在每个任务领取时复核本案 `SYSTEM_WORKER` 角色，桌面登记不能替代该检查。

官方法源抓取 Worker 默认不启动。只有持久化预览门已全部通过，并同时设置 `CASE_WORKBENCH_ENABLE_OFFICIAL_CAPTURE_WORKER=YES`、有效的 `CASE_WORKBENCH_SYSTEM_WORKER_ID`、以及预创建的 `CASE_WORKBENCH_OFFICIAL_CAPTURE_WORK_ROOT` 后，sidecar 才会装配它。工作目录必须是绝对、非符号链接、非根目录、0700 私有目录，并且必须与加密对象目录完全分离；它只作为不落入案卷原件目录的 Worker 安全锚点，不读取律师所选案件文件夹。`CASE_WORKBENCH_OFFICIAL_CAPTURE_INTERVAL_SECONDS` 可设为 1—300 秒，缺省 5 秒。每轮至多查找、领取和执行一项同律所、未过期、未尝试的授权任务；租约与逐案 `SYSTEM_WORKER` 角色在数据库内再次核验。sidecar 退出时向该循环发出停止信号。任一显式配置错误都会在案件路由监听前阻断启动；未设置启用项则维持默认关闭。

## 商用运行时门

`commercial-production` 不是 `postgres-internal-preview` 的别名，也不因存在 PostgreSQL 配置而自动启用。它必须同时满足：

1. `CASE_WORKBENCH_RUNTIME_MODE=commercial-production`；
2. `CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION=YES`；
3. `CASE_WORKBENCH_POSTGRES_DSN` 可被 PostgreSQL 连接串解析器解析且明确包含数据库名；数据库名（不区分大小写）必须以 `_production` 结尾，因而 `_preview`、`_test`、通用 `postgres` 和开发库名均被拒绝；
4. 不得同时存在 `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW`；
5. 与所有持久化模式相同，sidecar 仍须重新验签桌面登记、取得 OS 绑定会话、从固定 macOS Keychain 项只读取得 32 字节 AES-256-GCM 工件密钥，并使用预创建、绝对、非符号链接、非根目录、0700 私有的 `CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT`；数据库中仍在每次案件操作复核活跃用户和本案角色。

运行时选择和部署前置检查都不会连接数据库、读取 Keychain、伪造登记、执行迁移或调用外部服务。静态通过只说明所选环境没有被预览配置替代；它不等于签名公证、生产迁移、备份恢复、撤销演练、真实律师会话或真实案件流程已经验收。

## 尚未开放

- 现有 `case_api.app` 仍是合成 Alpha，不能因存在 `RuntimeServices` 就切换为 PostgreSQL；持久案件路由只由受监护 sidecar 按受控模式装配；
- 尚无真实律所激活/续期/撤销服务、生产信任根运营、MFA 或数据库迁移部署器；后端已实现签名登记、设备绑定、门限签名信任目录、无角色激活请求、同身份续期、远程撤销与本机停用接口；Tauri 已可通过原生 Keychain API 初始化/回读安装秘密、读取脱敏状态、只停用本机登记，只从系统文件选择器导入经当前目录验签的 `.lawenroll`，或用无参数命令打开 macOS 原生密码框交换一次性激活码；但没有真实签发身份时不得装配为登录；
- 原件单页预览代码要求本机 `pdftoppm`，并以独立临时目录生成 PNG；正式安装器必须固定并校验该渲染器版本；Tauri 桌面桥已只返回用户通过原生对话框选择的目录，不接受网页手填路径、文件系统根目录或整个用户主目录；
- 桌面壳已把 Python 3.12/FastAPI/uvicorn 编译为自包含 sidecar，并通过私有 stdin 随机挑战、回执摘要、动态 `127.0.0.1` 端口和父进程退出联动完成启动/监护；打包桌面默认直接启动无文档的 `local-standalone` API，提供本机基础案卷的建案、重新打开和显式只读盘点，不提供正式案件路由或外部模型。已实现的持久化装配会在其全部门槛满足时改为直接启动受控持久 API；仓库未配置真实信任根、Keychain 工件密钥、专用 PostgreSQL 或私有对象目录，因此当前不会打开律所受管案件路由；
- 桌面 `身份与安全` 页分开显示进程、生产信任目录、签名登记、数据库和案件访问；用户可显式初始化安装秘密或只停用本机登记，但不能自选角色或创建凭证。只有信任目录 `READY` 后才开放原生登记包选择；健康进程不会冒充已登录。
- 已保存登记不会仅凭 Keychain 项存在恢复登录：**律所受管路径**的 sidecar 每次启动按当前目录重新验签，成功后才由 Rust 使用一次性父进程令牌交换短时会话。令牌不进入运行状态或持久存储；受管数据库未 `CONFIGURED` 时 WebView 不取得受管会话 grant，受管案件访问仍禁用。`LOCAL_CONFIGURED` 的本机基础案卷使用独立的本机设备会话，不能因此取得任何受管案件权限。
- WebView 持久 API 客户端已使用原生 grant 解析动态数字 loopback 并注入短时 Bearer；固定预览地址若存在必须完全匹配，派生 PDF/ZIP/PNG 继续使用各自一次性许可。服务端仅允许 `tauri://localhost` 所需 CORS；会话过期在 Rust 和客户端双重阻断且不自动重试写命令。
- 续期/远程撤销只能使用签名目录中的 HTTPS Origin 与 TLS SPKI pin；sidecar 不跟随重定向、不自动重试，重新验签后由 Rust 按当前 Keychain 凭证哈希 CAS 提交。远程撤销先停止会话再删除本机凭证；默认 `NOT_CONFIGURED` 运行不挂载这些端点。
- 在线激活使用同一签名目录固定的 HTTPS/SPKI：激活码只进入 `NSSecureTextField` 和可清零的父进程缓冲区；sidecar 从 Keychain 读取安装绑定、拒绝现有登记并验签返回 envelope，Rust 只以“当前无登记”为 CAS 条件保存。默认 `NOT_CONFIGURED` 不显示输入框、不挂载端点。
- 尚未在获授权的专用 PostgreSQL 上执行集成测试；
- `commercial-production` 的代码门和静态检查不构成真实案件接入或生产放行；必须分别留存生产 PostgreSQL 迁移/回滚、律所登记与逐案授权、Keychain 和对象目录、安装包签名/公证、备份恢复、撤销演练和律师工作流验收证据。
