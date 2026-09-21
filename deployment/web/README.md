# 自托管 Web 部署入口

这里是跨平台浏览器工作台的完整单机服务器组合入口：反向代理、Next.js 前端、API、PostgreSQL、私有 S3 兼容对象存储和受控 Agent Worker。它部署在律所管理的 Linux 服务器（或管理员批准的私有云虚拟机），律师只使用 Windows、macOS 或 Linux 浏览器。

管理员从项目根目录使用一个入口，不需要律师接触环境变量：

```text
python3 scripts/manage_managed_web.py init
python3 scripts/manage_managed_web.py preflight
python3 scripts/manage_managed_web.py start
```

`init` 只建立权限为 0600 的配置草稿并为捆绑数据服务生成本地随机密码，不会启动容器；OIDC、数据库、对象库和模型服务的真实授权仍须管理员填写。`start` 只有在预检通过后才会启动完整组合，绝不回退到 SQLite 或演示案件。预检通过也不是商用放行，真实登录、恢复和律师全流程仍需验收。

默认 `SETUP_GATED` 时它不会处理真实案件；填写完整的生产 Web 配置并切换为
`PRODUCTION_WEB` 后，同一入口提供登录、案件、材料、逐页审阅和受验证派生 PDF。
它不会把桌面 loopback API 暴露给浏览器。完整的部署边界、Windows/macOS/Linux
操作方法、健康检查和放行门见 [WEB_DEPLOYMENT.md](../../docs/WEB_DEPLOYMENT.md)。

`api-gate.py` 返回 `SETUP_GATED` 是管理员尚未完成配置，不是律师产品入口；此时不能交付律师处理真实案件。

`case-agent-worker` 是独立的、每所一个的受控运行时 profile，不在默认
Compose 中启动。当前代码已固定其 PostgreSQL inbox、对象库、专用
`SYSTEM_WORKER`、不同的独立复核 `SYSTEM_WORKER`、两套数据库身份、DeepSeek
服务端密钥和私有工作根契约。缺少任一生产配置时 profile 会拒绝
启动，不会用演示快照、单一超级账号或浏览器密钥代替。
默认首批可执行文件只是已登记 PDF 页和扫描入库的 DOCX/XLSX，不代表任意文件能力已完成。

受控公网搜索是可选服务端能力：只有管理员设置
`LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY` 且数据库已安装 `0037` 时，Worker
才会登记 `controlled_web_search`。密钥为空时搜索不进入可执行
catalog，PDF/Office 与计划记忆仍可用；律师浏览器永不填写该密钥。

视觉 OCR 也是可选服务端能力。只有管理员同时设置
`LAWCASE_AGENT_WORKER_QWEN_API_KEY`、`LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID`
和 `LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE`，且数据库完整安装 `0038`
后，Worker 才会登记逐页 `image_visual_ocr`。三项均为空时该能力不会出现在
可执行 catalog；只填其中一项会直接阻止 Worker 启动。每次调用只处理同案一个
私有页，结果仅生成待律师复核候选；未知提交状态不会自动重试。
Worker 只通过不发布端口的独立出站网络访问已固定的模型端点；案件数据库和对象库
仍在内部数据网络中。

事实/交易候选提取使用独立的可选 DeepSeek 配置组：
`LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY` 与
`LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL=deepseek-v4-pro` 必须同时配置。
两项均为空时 Worker 不登记 `case_ledger_extraction`；只填一项、模型不匹配、
`0042` 私有律师复核落点或 `0045` 一次调用交换账本预检失败时，Worker 会在发布
readiness/heartbeat 前拒绝启动。原始响应先进入加密私有对象库并复核，结果不明只
查确定性对象键，不会再次调用模型。

动态 Word/Excel 预览使用另一个 `document-renderer` 服务。它不加入模型出站网络、
数据网络或浏览器入口，不发布宿主机端口，并以只读根文件系统、非 root 用户、全量
能力删除和临时内存工作目录运行。Agent Worker 只把系统生成且已做 SHA-256 绑定的
`DOCX`/`XLSX` 字节发送到这个内部网络；请求和响应均由短期 HMAC 绑定，重复 nonce
会被拒绝。请求超时属于结果未知，客户端只发送一次，不会自动重传。

管理员通过受管入口启动时，脚本会在文书交付开关为 `true` 时同时启用两个
profile；直接运行 Compose 时等价命令为：

```text
docker compose --profile agent-worker --profile document-delivery up -d
```

`init` 会为单机组合生成 `LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET`；它必须保持为至少
32 个随机字节生成的无填充 base64url 值，并同时只注入 Worker 与 renderer。renderer
的 `/healthz` 会实际执行
`soffice --version` 和 `pdftoppm -v`，仅检查文件存在不算就绪。当前仓库只完成了
隔离边界和静态/单元验证；在真实 Docker 引擎上完成镜像启动、无公网出口检查、
大文件与故障恢复验收之前，不能把它标记为商用放行。

Worker 的生产入口必须通过
`IsolatedDocumentRendererClientSettings.from_worker_environment(...)` 构造客户端，
并在发布 readiness 心跳前调用一次 `client.preflight()`。该预检固定访问同源
`/healthz`，最多一次、最长 10 秒，并核对 canonical 响应、协议版本和 HMAC；少启
`document-delivery` profile 时 Worker 应直接拒绝就绪，不能只因客户端对象构造成功
就宣称文书能力可用。
