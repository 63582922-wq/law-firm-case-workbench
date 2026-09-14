# 本机受管完整测试环境

这个目录是一个独立的内部 Alpha 装配层：它隔离保存本机凭据和数据，但复用并实际验证 `deployment/web` 的共享生产前端镜像合同，防止本机可起动而生产镜像失效。它的用途是让律师以真实用户身份走完整核心路径，不是将本地开发页包装成完整产品。

装配包含：

- `https://workbench.127.0.0.1.nip.io` 的真实 HTTPS 工作台，仅绑定宿主机 `127.0.0.1:443`；
- `https://identity.127.0.0.1.nip.io` 的 Keycloak OIDC authorization-code + PKCE 登录，合成主办律师首次登录必须绑定 TOTP；
- 只接受 TLS 的 PostgreSQL 16，动态连续执行当前 `0001—最高版本`，并预建 Web、身份目录、会话、执行 Worker 和独立校验者五套最小权限身份；启动探针还会以真实 Web 角色证明跨律所执行记录写入被数据库精确拒绝；
- 只使用 TLS 的私有 MinIO 桶，Web 与 Worker 使用两套独立凭据；启动探针还会证明两者只看到指定桶、无桶管理权且匿名读取被拒绝；
- KES mTLS 密钥服务和持久 filesystem keystore，确保应用要求的 `AES256` SSE-S3 真实可执行；
- 真实 ClamAV 病毒库更新和无网络常驻 `clamd`，API 通过私有 Unix socket 流式扫描；启动前同时证明干净 PDF 可通过、EICAR 必须被拒绝，256 MiB 上传与扫描边界一致，解包超限 fail closed；
- DeepSeek 规划与台账提取、Qwen 视觉 OCR、独立 LibreOffice/Poppler 文书渲染；启动门会用真实 DOCX 和 XLSX 分别转换并验证 PDF 签名、哈希和页数。凭据未填写时 Worker 拒绝启动，没有假模型、SQLite 或离线结果回退。

Keycloak 的 JVM 在本机测试栈中固定为 128 MiB 初始堆、512 MiB 最大堆，避免默认按整台 Docker VM 百分比预留内存；这不降低 OIDC/TOTP 验收边界。

公网搜索不在本次已锁定的核心验收路径内，Brave 默认为空并且不会登记搜索能力；这不会被显示为已开通。

## 管理员入口

从项目根目录执行：

```text
python3 scripts/manage_local_managed_test.py init
python3 scripts/manage_local_managed_test.py import-keychain-providers --qwen-workspace-id ws-your-workspace-id
python3 scripts/manage_local_managed_test.py preflight
python3 scripts/manage_local_managed_test.py start
python3 scripts/manage_local_managed_test.py verify
```

`init` 仅在已忽略的 `runtime/` 中生成 0600 随机凭据、30 天测试 CA、分服务证书、KES 身份和 Keycloak realm；它不启动服务。`import-keychain-providers` 只读取固定 service/account 下的 DeepSeek/Qwen Keychain 项，不在参数、日志或终端回显密钥，也不修改 Keychain；命令里的 `ws-your-workspace-id` 必须替换为真实 Qwen Workspace ID。如不使用 Keychain，可改用 `configure-providers` 不回显录入。

`preflight` 只检查可启动条件，且必须先确认宿主可用空间不少于 5 GiB；空间不足时不会停止旧消费者、构建镜像或启动 Agent。它还拒绝规范 Compose 把宿主 Python 源码挂载到 `/app/backend`：运行代码必须来自构建镜像。`compose.sealed-recovery-overlay.yaml` 仅供低磁盘时检查历史封存响应，管理员入口绝不自动加载它；使用该覆盖层的容器不是可发布镜像，`verify` 会明确报告。`start` 会先停止旧消费者，丢弃历史一次性容器结果，顺序构建本机镜像，然后按“PostgreSQL 与租户门 → clamd 完整加载与实扫 → KES/MinIO 加密 → Keycloak → 真实 DOCX/XLSX 渲染 → 前端 → 真实模型 → API/Worker”分阶段放行，避免小内存 Docker VM 的并发冷启动互相拖死。模型门分别核对 DeepSeek 文书、DeepSeek 台账与 Qwen 真实图片 OCR 的精确模型名、正常停止和结构化 JSON，日志不输出正文或密钥。任一门禁失败，API/Worker 不会进入可测状态。`verify` 仅是明确标记的 `INFRA_ONLY`：它核对持久服务的实际 health、restart/OOM 状态、运行时代码是否来自镜像、Web `/readyz`、OIDC discovery 和 issuer 同源绑定，不会冒充登录后的整案验收。若 Docker 状态读取超时，它只会报告“当前无法判断服务状态”，不会把未知状态误说成服务已停止。

每次 `start` 会先通过 DeepSeek 余额接口只核对账号当前是否可用，不读取或输出余额金额；账号不可用时会在产生推理费用前停止。账号可用后才会发起两次 DeepSeek 和一次 Qwen 小型真实请求，因而可能产生供应商计费；这是防止首个真实任务才暴露模型/账号错位所必需的有界探针，不会保存模型正文。

如果同一次分阶段启动只因供应商账户不可用而停在模型门，恢复余额后可运行 `python3 scripts/manage_local_managed_test.py resume-provider-gate`。该命令会先核验 PostgreSQL、ClamAV、KES/MinIO、Keycloak、文书服务、前端及其全部上游一次性门禁仍然健康且未失败；只有条件完全匹配时才重跑固定的三次供应商探针并启动 API、Agent Worker 和网关。供应商门一旦通过，三个消费者使用 `--no-deps` 启动，防止 Compose 再次执行付费探针；任一上游状态变化都会拒绝续跑并要求重新执行完整 `start`。

## 唯一的手工系统授权

浏览器不会自动信任自签 CA。先运行：

```text
python3 scripts/manage_local_managed_test.py trust-info
```

核对 SHA-256 指纹后，由用户亲自在 macOS 钥匙串中导入并信任该短期 CA。脚本不调用 `security add-trusted-cert`，不修改 Keychain，也不修改 `/etc/hosts`。`nip.io` 两个主机公开解析到 `127.0.0.1`，Docker 内部则由 gateway 精确网络别名接管，避免 API 把 issuer 解析到自己。

测试账号说明位于 0600 文件，只在明确运行下列命令时回显：

```text
python3 scripts/manage_local_managed_test.py login-info
```

首次测试请从一个全新的无痕/隐私窗口打开工作台。OIDC 登录会要求用户扫码绑定 TOTP；这一步是真实 MFA 仪式，不由脚本代替。绑定完成后，关闭所有同一浏览器的无痕/隐私窗口以清除 Keycloak 单点登录会话，再新开无痕/隐私窗口，用“密码 + 当期 TOTP”完成第二次登录；只完成绑定页或沿用首次单点登录会话都不算 MFA 验收。

## 不能越界的结论

本环境通过后只能说“生产门禁下的本机内部 Alpha 可以进入律师整案验收”。它不证明公有域名、KMS/HSM、备份恢复、Windows/macOS 浏览器矩阵、真实律所运维或商用放行。KES filesystem keystore 明确只用于本机测试，不得搬入生产。
