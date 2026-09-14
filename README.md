# 律所案件 AI 工作台

## 律师正式入口与开发入口

律师正式使用的是律所管理员部署的完整受管 Web 服务；管理员统一入口为：

```text
python3 scripts/manage_managed_web.py init
python3 scripts/manage_managed_web.py preflight
python3 scripts/manage_managed_web.py start
```

开发人员只需验证基础材料链时，可以运行离线开发模式：

```text
python3 scripts/start_local_web.py
```

离线启动器会等待页面真正就绪，并打印、打开唯一可用地址（默认从 `http://127.0.0.1:33000` 开始选择空闲端口）。它不要求 OIDC、PostgreSQL、对象存储、ClamAV、Poppler 或 LibreOffice，但只提供基础 PDF/人工审阅能力，界面明确标注为开发模式，不得交付律师处理真实案件。两种运行方式复用同一套 Web UI；完整能力缺失时原案件页显示服务待恢复，不再让律师寻找另一个产品。开发 SQLite 数据不会静默转入正式受管案卷，迁移须另行提供带哈希和审计回执的管理员导入。见 [离线开发模式说明](docs/LOCAL_WEB.md)、[当前商用 Web 执行计划](docs/COMMERCIAL_WEB_EXECUTION_PLAN.md) 和 [生产部署说明](docs/WEB_DEPLOYMENT.md)。

面向诉讼律师的**浏览器优先、自托管**办案工作台。首个垂直场景是**民间借贷案件应诉**：将法院材料、借据、微信记录、银行流水和律师意见，整理为可追溯、可复算、可审核、可提交的案件工作底稿与法院提交包。

本项目不做“通用法律聊天机器人”，也不允许 AI 自动决定诉讼立场、心算利息或直接向法院提交材料。

> 产品方向已于 2026-08-11 校正：主交付形态是律所服务器或私有云上的 Web 工作台，律师可在 Windows、macOS 或 Linux 的浏览器中使用。此前的 macOS/Tauri `.app` 仅保留为可复用实现和未来可选离线助手，不再是产品交付路线，也不能被宣传为 Windows 可用方案。

## 律师如何使用

正式使用路径为：

1. 律所部署受管 Web 服务，律师通过浏览器登录并打开有权限的案件；
2. 律师明确选择材料文件夹（Chrome/Edge 增强）、多份 PDF 或 ZIP 材料包；浏览器不提交或保存本机绝对路径。ZIP 先由服务端安全接收并形成待处理回执，逐文件入卷与可恢复分片仍需后续 Worker 切片；
3. 资料进入该案私有受管库后，系统完成哈希、隔离、页级预览和重复候选；
4. Agent 先按相关性、风险、冲突和 OCR 异常整理候选；律师默认复核异常并批量确认低风险候选，再确认红框、锁定，生成相关页 PDF，核对事实、法律依据、利息和应诉材料；
5. 身份、案件角色、受控 PostgreSQL、对象存储、审批与审计均由服务端核验，模型和网络调用仍按案、按次取得律师确认。

首页不把系统安全或研究能力当作主工作区：AI 助手负责给出受控的下一步清单，律师决定事实、法律口径、对外发送和最终文书。模型、权限、审计和运维信息都收在“工作台设置”的高级层。

## 当前交付状态

跨平台 Web 主路径已经落地为真实服务端边界：浏览器登录、案件列表/建案、PDF 接收位、流式接收、结果待核验保护、私有对象绑定、单页 PNG 预览、逐页纳入/排除候选与人工确认、红框候选与人工确认、证据清单锁定、服务器 Worker 派生两份 PDF、受验证下载，以及同案事实/诉请/争点/交易台账只读核对均已有 Web API 和中文 UI。浏览器版默认不显示合成案件；在律所登录、案件权限、受管资料库和生产配置尚未部署完成前，它明确停在配置门，而不是伪造可办案件。**真实 Web Agent 总控、模型 Worker 和候选批次写链仍在实施，当前材料接收或本机文本预处理不得称为 Agent 已完成。** 当前 macOS 桌面包不是本项目的产品交付物。

- 已建立案件状态机、事实/诉请/交易台账、证据 Manifest/红框 PDF 派生、官方法源与规则包、确定性利息计算和中文工作台代码。
- Web 端材料链已从浏览器明确选 PDF/材料文件夹/ZIP 开始：PDF 经过服务端扫描、哈希、私有对象库、页级预览、双步骤审阅、红框和清单锁定；ZIP 经过服务器私有暂存、解压炸弹与路径安全检查、哈希和不可重复接收状态机后进入待逐文件处理队列。服务端异常结果会显示“待核验”，不会诱导重复上传。ZIP 子文件 Worker 和可恢复分片仍未完成，不在页面宣称已入卷。
- Web 案件要点页读取同案事实、诉请、争点和收付款台账；主办律师可以对事实候选作确认、争议或否认，并确认诉请范围和收付款记录，决定哈希由服务端按案件版本派生，不能由浏览器伪造。
- Web 法律与利息页可读取同案已登记的官方法源、规则版本和法律事件；确定性测算入口只接受律师明确的义务编号、期间和还款抵扣顺序，利率、交易和规则包由服务端绑定并独立复算，不自动选择适用规则或推定起算日。
- Web 案件首页会显示材料、事实、法源、法律事件和规则包分别缺少什么，并给出下一步；该状态来自服务端案件版本，不是页面自行推断。
- Web 商用部署增加了只读预检 `backend/scripts/web_deployment_readiness.py`：它检查生产配置、Web 迁移、ClamAV、Poppler 和 SYSTEM_WORKER，但明确不替代真实登录、数据库连通性、备份恢复或律师浏览器验收。
- Web 端已提供 `SETUP_GATED` 与 `PRODUCTION_WEB` 两种启动门：配置不完整时只开放健康检查，配置完整后才装配 OIDC、三套 PostgreSQL、私有对象存储、ClamAV、Poppler 和同源案件 API。
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
- 提交材料内核已具备 PDF-only 编译、依赖锁定、ZIP/内部清单分离和加密对象登记；Web 页面已接入候选文书批准与材料包锁定，但最终 Worker 导出和浏览器下载仍须完成真实部署验收后开放，不会自动向法院提交。
- 官方法源已具备律师授权队列、一次执行本机 Worker、原字节加密、确定性解析、律师复核、许可核验和正式快照登记 UI；尚未在专用 PostgreSQL 实库和真实桌面身份/Keychain 环境装配运行。
- 官方案例数据已建立严格元数据目录，登记 6 个已核验的民间借贷真实案例候选；中文法律规则页直接读取同一目录，展示争点、评测用途和官方链接，并始终标为“仅研究线索”。许可未建立前禁止批量抓取、全文仓库、商业训练和正式案件使用。
- PostgreSQL 迁移尚未在专用测试库执行；真实身份、真实案卷、完整文书生成、持久下载审计和律师验收仍未完成。
- 所有项目文件、设计、代码、测试和交付物均在本文件夹内维护。

## 关键文档

- [深刻需求理解](./CONTEXT.md)
- [完整开发计划](./PLAN.md)
- [浏览器优先、自托管工作台决策](./decisions/ADR-0026-web-first-self-hosted-workbench.md)
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
- [国内法律交付物版式与字体合同](./docs/DOMESTIC_LEGAL_DELIVERABLE_FORMAT.md)
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
