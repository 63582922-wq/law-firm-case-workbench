# 律所案件 AI 工作台

面向诉讼律师的本机优先办案工作台。首个垂直场景是**民间借贷案件应诉**：将法院材料、借据、微信记录、银行流水和律师意见，整理为可追溯、可复算、可审核、可提交的案件工作底稿与法院提交包。

本项目不做“通用法律聊天机器人”，也不允许 AI 自动决定诉讼立场、心算利息或直接向法院提交材料。

## 律师如何使用

桌面端有两条边界清晰的使用路径：

1. **本机基础案卷（安装后直接可用）**：新建或打开本机案件，选择本案资料文件夹，并明确发起只读材料盘点。它不需要律所管理员、外部 PostgreSQL 或模型 Key，也不读取、修改、上传来源文件；
2. **律所受管案件工作区（另行配置）**：在律师登记、案件角色、受控 PostgreSQL、加密对象库和逐案授权都就绪后，才能处理逐页证据、事实与争点、法律依据、利息口径、可编辑文书和提交包。

本机基础案卷不输出正式事实、法律或利息结论，也不生成可向法院提交的材料；它保存真实的本机案件壳和材料盘点，供后续进入受管工作区时复核。首页不再把系统安全或研究能力当作主工作区：AI 助手负责给出受控的下一步清单，律师决定事实、法律口径、对外发送和最终文书。模型、权限、审计和运维信息都收在“工作台设置”的高级层。

## 当前交付状态

当前已交付可启动的 macOS 中文桌面工作台；打包桌面默认进入可真实建案、重开案件和只读盘点资料的“本机基础案卷”，无需律所登记、外部 PostgreSQL 或模型配置。律所受管案件模式仍需要登记、专用 PostgreSQL、受管加密对象库和运维配置后才能启用；两种模式绝不互相降级或伪造权限。

- 已建立案件状态机、事实/诉请/交易台账、证据 Manifest/红框 PDF 派生、官方法源与规则包、确定性利息计算和中文工作台代码。
- 已实现办案首页、收集材料、核对案情、法律依据、还款与利息、应诉材料及工作台设置；界面按律师办案顺序而不是开发/研究术语组织。
- 证据页持久化写链已接入逐页候选/批准、既有红框批准、重复页裁决和 Manifest 锁定；大案首屏使用全案就绪摘要与前 50 页版本分页，断线保留已载入页面并允许续载，锁定由服务端复核全部页及就绪哈希。
- 原件页已具备本机短时文件夹授权、哈希绑定的单页 PNG 渲染、一次性预览许可、红框拖选候选和批准 UI；Tauri 2 桌面壳已装配专用原生文件夹选择命令，不向网页开放通用文件系统权限。
- 案卷根目录现可在短时授权内执行只读盘点；数据库只保存相对路径、文件哈希、类型和新增/修改/移动/缺失差异。新盘点先进入候选，必须由主办律师批准后才替代正式范围，并使依赖旧范围的证据/提交链失效。
- 已批准范围可建立逐文件材料接收任务；排队前重新核对目录 Manifest，Worker 以短租约重验哈希、本机恶意文件扫描结果和 PDF 活动内容，只有安全静态 PDF 使用真实页数登记原件。图片、Office、文本、邮件和压缩包再经过格式签名、宏/外链、危险路径、异常压缩和结构上限检查，逐项原因在中文界面可见；通过者仍等待隔离转换，不伪造页数。
- 桌面包已携带自包含 Python/FastAPI 本机服务，由 Rust 父进程完成随机挑战、动态 loopback 端口、健康状态和退出联动监护。只有已登记身份、受控 PostgreSQL 运行时、Keychain 只读 AES 密钥及预创建私有加密目录同时满足时，sidecar 才装配受控持久化 API，不会因服务已启动而伪造律师身份或数据库连接。
- 已交付独立 `local-standalone` 运行时：本机 SQLite 账本目录 `0700`、账本文件 `0600`，仅保存随机本机身份、案件元数据、文件夹显示名/指纹与显式盘点的相对文件清单；绝对路径只在 sidecar 内存的 15 分钟原生选择授权中存在，重启即失效。它不读取模型 Key、不发网络请求，也不开放正式事实、利息、文书或法院提交能力。
- macOS 本地 Alpha 构建现在会校验固定产品名、Bundle ID、主程序和 sidecar，再进行由内到外的临时签名及严格包验证；不完整 `.app` 会直接构建失败。该临时签名不等于 Developer ID、公证或公开分发许可。
- 已实现律所 Ed25519 签发、30 天上限、设备秘密绑定和会话装配的律师登记契约；另实现门限签名生产信任目录、到期/连续版本/前序哈希回滚防护及活动/退休/撤销签发公钥策略。仓库发布配置故意为 `NOT_CONFIGURED`，不带测试根或生产私钥。
- 已实现登记、续期、远程撤销和“仅停用本机”的生命周期编排；原生 `.lawenroll` 导入只由系统文件选择器触发，以父进程私有令牌调用 sidecar 验签，精确字节和当前凭证哈希匹配后才 CAS 保存到 Keychain，WebView 不能提交任意凭证。
- Tauri 桌面端已实现安装秘密初始化/回读、脱敏状态、本机停用和受信验签后凭证保存；秘密不进入命令参数或 WebView。真实律所生产信任根和签发服务未部署，中文页面因此继续禁用在线激活、离线导入与真实案件。
- 已实现“每次启动重新验签”状态机：信任目录未就绪时不读 Keychain，缺少登记与无效登记分开显示；有效登记才以一次性父进程引导交换最长 30 分钟本机会话。令牌只在 Rust 内存中，运行状态不含令牌/session ID，专用数据库未配置时 WebView 不能取得授权，案件路由继续关闭。
- WebView 正式案件读写已统一使用原生短时 grant 和 sidecar 动态数字 loopback；浏览器不能覆盖身份 Bearer，PDF/ZIP/PNG 仍需各自一次性读取许可。Rust 在向 WebView 返回前再次阻断并清除过期会话，服务端 CORS 只允许 `tauri://localhost` 的必要方法和头。
- 律所登记续期与远程撤销已接入由签名目录固定的 HTTPS/SPKI 通道：sidecar 重新验签并在内存 staging，Rust 再按当前凭证哈希 CAS 写入或删除 Keychain；远程撤销需页面二次点击、macOS 原生确认和已接受回执，并立即停止本机会话。“只停用本机”仍明确不代表远程撤销。
- 一次性激活码已接入 macOS 原生安全输入：WebView 只能调用无参数命令，激活码不会进入页面状态或命令参数；sidecar 从 Keychain 取得安装绑定、拒绝覆盖现有登记，再经固定 HTTPS/SPKI 服务换取并验签 envelope，Rust 只以“当前无登记”为 CAS 条件保存。
- “工作台设置 → 按需配置模型服务”可配置 DeepSeek/通义千问百炼 Qwen3.5-OCR 的 API Key：密钥只通过 macOS 原生安全输入框进入系统钥匙串，页面仅显示是否已配置，不能填写任意供应商地址。模型 Key 不授予律师身份、案件访问或材料外发权限；本机基础案卷不会读取或调用它们，只有律所受管案件中的 Qwen 单页 OCR 和 DeepSeek 受控办案步骤规划才可能在逐案预授权后使用。
- 激活、续期和撤销已具备断线未知结果消解：远程请求前先把 UUID 操作号、类型、原凭据哈希和设备绑定写入第三个 Keychain 项；结果不确定时禁止重复提交，中文页面可通过固定状态端点查询，成功结果重新验签并与凭据 CAS、待决标记清除共同提交。
- 开发环境可展示清晰标记的演示案件；在已部署桌面中，无登记用户可进入本机基础案卷并创建真实的本机案件壳、选择资料目录和执行只读盘点。只有原生层确认律师登记、会话、信任目录和案件资料库均已就绪，界面才开放律所受管案件功能；未就绪时不把演示案情混入本机案件，也不会读取、创建或修改来源材料。
- 已新增独立 `commercial-production` 运行时：须由受控运维明确确认，并只接受名称以 `_production` 结尾的专用 PostgreSQL 数据库；它不等于商用放行，仍须完成迁移/回滚、登记、备份恢复、签名公证和律师验收。
- 提交材料已具备 PDF-only 编译、依赖锁定、ZIP/内部清单分离、加密对象登记和本机一次性下载的合成实现；不会自动向法院提交。
- 官方法源已具备律师授权队列、一次执行本机 Worker、原字节加密、确定性解析、律师复核、许可核验和正式快照登记 UI；尚未在专用 PostgreSQL 实库和真实桌面身份/Keychain 环境装配运行。
- 官方案例数据已建立严格元数据目录，登记 6 个已核验的民间借贷真实案例候选；中文法律规则页直接读取同一目录，展示争点、评测用途和官方链接，并始终标为“仅研究线索”。许可未建立前禁止批量抓取、全文仓库、商业训练和正式案件使用。
- PostgreSQL 迁移尚未在专用测试库执行；真实身份、真实案卷、完整文书生成、持久下载审计和律师验收仍未完成。
- 所有项目文件、设计、代码、测试和交付物均在本文件夹内维护。

## 关键文档

- [深刻需求理解](./CONTEXT.md)
- [完整开发计划](./PLAN.md)
- [计划审查记录](./PLAN-REVIEW-LOG.md)
- [系统架构](./docs/ARCHITECTURE.md)
- [UI、交互与视觉设计计划](./docs/UI_UX_DESIGN.md)
- [知识库与 Skills](./docs/KNOWLEDGE_AND_SKILLS.md)
- [安全与合规](./docs/SECURITY_AND_COMPLIANCE.md)
- [质量、验收与发布](./docs/QUALITY_AND_RELEASE.md)
- [视觉设计简报](./design/DESIGN_BRIEF.md)
- [编排架构决策](./decisions/ADR-0001-orchestration.md)
- [Phase 2 执行台账](./docs/PHASE_2_EXECUTION_LEDGER.md)
- [民间借贷利息时态规则研究](./research/PRIVATE_LENDING_INTEREST_TEMPORAL_RULES.md)
- [官方法源抓取与解析契约](./docs/OFFICIAL_SOURCE_CAPTURE_CONTRACT.md)
- [法院提交包编译契约](./docs/SUBMISSION_COMPILATION_CONTRACT.md)
- [桌面壳与文件夹选择决策](./decisions/ADR-0008-tauri-desktop-shell.md)
- [本机 API 与桌面会话决策](./decisions/ADR-0009-supervised-local-api-session.md)
- [本机基础案卷决策](./decisions/ADR-0025-local-first-standalone-workspace.md)
- [商用运行时门决策](./decisions/ADR-0024-commercial-production-runtime-gate.md)
- [桌面部署前置检查](./docs/DESKTOP_DEPLOYMENT_READINESS.md)
- [签名律师登记与设备绑定决策](./decisions/ADR-0010-signed-desktop-lawyer-enrollment.md)
- [登记、续期与撤销生命周期决策](./decisions/ADR-0011-desktop-enrollment-lifecycle.md)
- [门限签名与回滚防护的生产登记信任决策](./decisions/ADR-0012-rollback-protected-enrollment-trust.md)
- [每次启动重新验签与短时会话决策](./decisions/ADR-0013-reverified-desktop-session.md)
- [WebView 持久 API 短时会话决策](./decisions/ADR-0014-webview-persistent-api-session.md)
- [固定 HTTPS 登记续期与远程撤销决策](./decisions/ADR-0015-pinned-enrollment-renewal-revocation.md)
- [macOS 原生安全激活决策](./decisions/ADR-0016-native-secure-enrollment-activation.md)
- [登记断线未知结果消解决策](./decisions/ADR-0017-enrollment-unknown-remote-state-resolution.md)
- [大案长台账稳定分页决策](./decisions/ADR-0018-version-bound-large-ledger-pagination.md)
- [证据就绪摘要、分页与锁定复核决策](./decisions/ADR-0019-evidence-readiness-pagination.md)
- [本机案卷只读盘点与范围批准决策](./decisions/ADR-0020-versioned-local-folder-intake.md)
- [已批准案卷到证据原件的安全接收决策](./decisions/ADR-0021-approved-folder-evidence-intake.md)
- [非 PDF 材料有界结构检查决策](./decisions/ADR-0022-bounded-non-pdf-structural-inspection.md)
- [案卷材料接收 Worker 契约](./docs/EVIDENCE_INTAKE_WORKER_CONTRACT.md)
- [官方案例与真实数据接入目录](./research/OFFICIAL_CASE_DATA_CATALOG.md)

## 不可突破的产品红线

1. 原始证据永不覆盖，删页、去重、红框均生成派生件。
2. 正式结论必须回指原文件、页码、坐标和哈希。
3. 法律依据必须包含官方来源、版本和有效期间。
4. 利息、本息冲抵、日期和合计由确定性程序计算。
5. 诉讼立场、法律路径、计算参数和提交版必须由律师审批。
6. 任何上游数据变化都使受影响的下游结果自动失效。
7. 法院提交包只存在一个当前锁定版本，草稿和历史版本不会混入提交目录。
8. 未经明确授权，不自动发送邮件、签章或提交法院。

## 预期交付节奏

- 三种高保真方向：约第 4—6 周完成；可点击原型和律师任务测试：约第 6—8 周完成。
- 技术预览版（技术基础、材料、台账）：约第 13—16 周完成，不称为端到端 Alpha。
- 端到端内部 Alpha：约第 21—24 周完成。
- 律师封闭试点及商用 Beta 安全门槛：约 29—34 周起（含 2 周招募、专项复核和阶段返工缓冲）；累计 30 案、复购和付费续用作为后续独立商业验证，不替代安全验收。

时间以阶段验收为准，而不是以“页面做出来”作为完成标准。
