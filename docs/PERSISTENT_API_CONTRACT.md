# 独立持久化 API 契约

状态：`内部预览路由已实现；默认禁用；未接真实身份或专用 PostgreSQL`

## 隔离原则

- `case_api.app` 继续只服务 `alpha_*` 合成对象，不挂载任何持久化路由；
- `case_api.persistent_app.create_persistent_app()` 未注入依赖时只返回“已禁用”健康状态，案件路由不存在；
- 启用持久化路由必须使用 `postgres-internal-preview` 配置、受控 PostgreSQL 台账 Store 和服务端身份解析器；
- 本地原件页预览要求成对注入 `LocalFolderGrantRegistry` 与使用同一 Registry 的 `OriginalPageAccessBroker`；缺任一依赖时接口按 503 阻断，不回退到整份 PDF 或浏览器直读；
- 文件夹检查、短时授权和单页预览仅接受 loopback 请求及 `OS_BOUND_LOCAL_SESSION`。响应不返回绝对路径；预览许可绑定律所、案件、操作者、会话、文件夹授权、原件哈希和来源页，一次使用后立即失效；
- 加密派生件访问必须与证据 Store、OS 绑定会话、Keychain 取钥和本机 loopback 同时配置；缺任一项返回 `503`，不回退公开对象路径、合成文件或环境密钥；
- 客户端提交的姓名、角色或 `X-Actor` 等头部不形成身份，最终权限还必须由数据库内本案成员关系复核。

## 当前路由范围

当前 UUID 路由覆盖事实候选/决定、诉请候选/范围确认/回应、争点候选/确认、交易候选/确认、付款分类候选/批准、重复流水组候选/结论；证据路由另覆盖案卷文件夹候选盘点/批准、原件登记、逐页处置候选/批准、红框坐标候选/批准、重复页候选/结论、Manifest 锁定、派生任务入队、租约领取、心跳、完成和失败。请求明确携带案件版本、币种、日期精度、付款性质、债务分配、文件夹授权、证据原件哈希与页码；业务写命令必须携带幂等键。Worker 心跳只延长当前未过期租约，不改变案件业务版本。

`GET /v1/matters/{matter_id}/snapshot` 在 PostgreSQL `REPEATABLE READ READ ONLY` 事务中读取案件版本、事实、诉请回应、争点、交易、付款分类/分配和重复组，并返回稳定快照哈希；这样同一响应不会把两个不同案件版本的行拼在一起。事实列表仍有独立只读路由，供局部刷新使用。

事实与争点工作台不再使用上述全量快照作为首屏。`GET /review-summary` 只读取案件头、事实/候选/交易计数、诉请回应和争点；`GET /fact-pages` 与 `GET /transaction-pages` 使用默认 50、最大 100 的稳定键集游标。首批可用 `expected_version` 绑定摘要版本，后续页同时绑定游标内案件版本；版本变化返回 409 并要求从摘要重新开始。游标只用于定位，不形成权限，每页仍重新校验服务端身份、数据库本案角色、律所/案件范围和 RLS。事实页与交易页按列表用途返回字段白名单，交易不返回付款人、收款人、流水号、渠道和完整证据链接。

`GET /v1/matters/{matter_id}/evidence-review-summary` 使用独立证据 Store 的只读重复读摘要，返回原件登记、全案页数与阻断计数、重复页组成员定位、当前锁定 Manifest 摘要、派生件、可恢复 Worker 任务状态和 `manifest_readiness_hash`。`GET /v1/matters/{matter_id}/evidence-pages` 返回最多 100 页的案件版本绑定键集分页及页级最小投影。Manifest 锁定必须提交就绪哈希，服务端在同一事务内重算全部页、候选决定、批准红框和重复组后才可物化。原全量 `evidence-snapshot` 暂时保留给内部兼容调用，但中文工作台不再依赖。租约 ID、加密对象键和绝对路径不进入摘要或分页；证据 Store 未注入时路由返回 `503 EVIDENCE_SERVICE_UNAVAILABLE`，不得回退合成证据。

`GET /v1/matters/{matter_id}/local-folder-intake` 返回当前候选与已批准案卷范围的脱敏摘要；`POST /local-folder-scans` 只接受当前 OS 绑定会话持有的短时 `folder_grant_id`，由服务端扫描已授权目录并保存候选，浏览器不能提交自组装文件清单。`GET /local-folder-scans/{scan_id}/files` 以案件版本和扫描 UUID 绑定的游标返回最多 100 条相对路径差异。`POST /local-folder-scans/{scan_id}/approve` 仅主办律师可调用，绑定案件版本、Manifest 哈希和批准输入哈希；成功后旧案卷范围及其证据/提交依赖失效。所有响应均不返回绝对路径、授权令牌、设备号或 inode。

`GET /evidence-intake-runs/current` 返回当前已批准范围的接收汇总；`GET /evidence-intake-runs/{run_id}/items` 以案件版本、运行 UUID 和稳定游标返回最多 100 项材料状态，只含相对路径、类型、终态原因和必要展示字段。`POST /evidence-intake-runs` 必须携带短时 folder grant，服务端重新扫描并确认 Manifest 未变化后才排队。Worker 的 claim/heartbeat/complete/finalize 路由只接受数据库 SYSTEM_WORKER 权限；单项租约绑定运行、扫描、相对路径、大小与哈希，完成时再核对登记原件。普通页面不取得租约 ID、源哈希、扫描器输出或绝对路径。

派生件先通过 `/access` 取得 45—90 秒的一次性 Bearer，再通过 `Authorization` 请求头读取 `/content`；令牌、对象键和绝对路径不得进入 URL、证据快照或前端日志。读取只接受当前 `VERIFIED` 且仍属于 `LOCKED` Manifest 的记录、当前案件服务端身份和数值型 loopback 客户端；成功读取后令牌立即失效。

## 身份与请求审计

服务端身份上下文包含 UUID 律所、用户、会话，签发方、认证方式、认证与过期时间；时间必须带时区且会话未过期。当前已实现 OS 绑定本机会话 authority：服务端注入登记 Actor，一次性 bootstrap 交换短时 Bearer，只保存令牌摘要，校验数字 loopback 与 `tauri://localhost`，并支持过期/撤销。

登记 Actor 的正式来源契约也已实现：律所受信 Ed25519 公钥核验严格规范化凭证，凭证绑定 32 字节安装秘密的 SHA-256、最长 30 天；macOS 适配器从两个固定 Keychain 项只读取得签名凭证与秘密，浏览器不能选择 actor、firm 或 role。验证后的会话到期不超过凭证到期。每次案件读写仍查询 `users.status=ACTIVE` 与未撤销的 `matter_actor_roles`，因此登记凭证不替代本案数据库授权。

登记生命周期编排也已实现为独立接口：一次性激活请求只含激活秘密、设备绑定摘要和 nonce；返回签名凭证先验证再 CAS 保存；续期保持同一登记号、用户和律所且必须延长有效期；远程撤销只有在匹配的接受回执后才删除本机凭证；“仅停用本机”可清除损坏或过期凭证，但明确标为远程撤销未确认。当前网络链使用合成信任和 issuer 验证，未配置真实律所服务。

续期和远程撤销现已装配原生网络链：HTTPS Origin 与最多四个 TLS SPKI pin 只从当前签名目录读取；系统 CA/主机名与 SPKI 双重核验，固定路径、严格 JSON、大小/超时限制且不自动重试。sidecar 从固定 Keychain 项读取当前材料并在内存 staging，续期 envelope 再验签后只把精确字节/哈希交给 Rust CAS；远程撤销还必须通过 macOS 原生警告框，只有匹配、接受且时效有效的回执才使 Rust 停止会话并按旧哈希删除本机凭证。WebView 只能触发命令和读取脱敏状态，不能绕过原生确认，也不能看到 envelope、安装秘密或远程回执细节。

在线激活也已装配到同一固定网络链：WebView 只调用无参数原生命令；Rust 在 macOS 主线程显示 `NSSecureTextField`，取消、超时或格式错误时不发请求。sidecar 仅接受父进程 Bearer 与严格激活字段，从 Keychain 取得安装绑定并拒绝覆盖已有登记；返回 envelope 重新验签后，Rust 以“当前无登记”为 CAS 条件写入并回读。激活码、安装秘密和 envelope 均不进入普通前端状态。

Tauri 原生层现可通过系统 Keychain API 显式生成、保存并回读 32 字节安装秘密，以及读取脱敏状态和只删除本机登记凭证；秘密不进入命令参数或 WebView。安装包固定离线目录根公钥，sidecar 验证门限签名、到期、连续版本、前序哈希、TLS pin 和签发公钥状态；原生层只从系统文件选择器读取 `.lawenroll`，以父进程私有令牌发送精确凭证字节和 Keychain 内秘密的摘要，验签成功后按回执哈希与当前凭证哈希 CAS 保存。仓库 bootstrap 为 `NOT_CONFIGURED`，不生成生产信任或假登录。

每次 sidecar 启动还必须重新验签 Keychain 登记：信任目录未就绪时不读 Keychain，返回码 44 仅表示 `NOT_ENROLLED`，其他凭证/绑定/时效错误为 `BLOCKED`。有效登记才建立一次性父进程 bootstrap，并由 Rust 立即交换最长 30 分钟 Bearer；普通运行状态不包含 bearer、bootstrap 或 session ID。WebView 会话 grant 还要求专用数据库同时 `CONFIGURED`，当前禁用 sidecar 不挂载案件路由。

WebView 的全部正式案件请求现统一通过受控客户端：桌面模式从原生命令取得动态 `127.0.0.1` 地址和短时 Bearer，不使用 Cookie、不持久化令牌、不允许调用方覆盖身份头；固定配置地址如存在必须与 grant 完全一致。派生 PDF、ZIP 和 PNG 的内容读取仍使用独立一次性 Bearer。持久 API 仅为 `tauri://localhost` 开放所需 CORS 方法/头和最小响应头；未登记 Origin 的预检失败。401 不自动重试写命令。

启动重验与短时 identity authority 已装配到打包 sidecar，但仓库固定的生产信任目录仍为 `NOT_CONFIGURED`，专用 PostgreSQL 也未配置。没有律所真实信任根、签发/续期/撤销服务和数据库逐案授权时，产品不会形成真实登录或开放案件路由。

API 为每个请求生成 UUID `X-Request-ID`，通过上下文写入同一数据库审计事务。已知错误返回稳定代码、中文消息和相同请求号，不向普通界面暴露数据库异常或连接信息。

## 未完成门槛

- 律所真实激活/续期/撤销/状态服务与操作号保留策略、生产根运营、专用数据库下的桌面 identity authority/Store/Worker 正式装配，或真实 OIDC/MFA；客户端断线状态消解已实现但未做生产互操作验收；
- 专用 `_test` PostgreSQL 的迁移与整链集成执行；
- 诉请/争点及其他长列表的大案分页；事实、交易和证据来源页最小投影已完成，仍待专用实库性能和并发验收；
- 速率限制、CSRF/本机 IPC 来源绑定、Keychain 初始化/轮换、迁移部署与备份恢复；
- 真实安全验收和律师工作流验收。

上述条件完成前，不得接入真实案件或把本 API 描述为生产可用。
