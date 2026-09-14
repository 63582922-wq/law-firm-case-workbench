# ADR-0028：隔离的本机受管 Alpha 验收环境

状态：已接受（装配层实现中，待真实凭据与浏览器整案验收）

## 背景

现有 `start_local_web.py` 是 SQLite/关闭外网和模型的开发辅助，不能承载“律师以普通用户身份测试完整功能”的验收。正式 `deployment/web` 又故意要求外部 OIDC、TLS PostgreSQL、TLS 私有对象库和真实模型，这些缺失时必须 fail closed。若为了本机方便而放宽生产验证、伪造 JWT/MFA、改用 SQLite 或伪造模型回包，测到的就不是用户真实路径。

## 决策

新增 `deployment/local-managed-test/` 作为独立装配层，不修改 `deployment/web` 的生产语义：

1. 复用现有前端、API、Agent Worker 和隔离文书渲染镜像，API 仍使用 `PRODUCTION_WEB`，Worker 仍使用 `PRODUCTION_AGENT_WORKER`。
2. 用 `workbench.127.0.0.1.nip.io` 与 `identity.127.0.0.1.nip.io` 作为非保留 HTTPS 主机，只绑定 loopback 443。Docker gateway 在 edge 网络内持有两个完整主机别名，使容器与浏览器看到同一 issuer/origin。
3. 用 Keycloak 正式启动模式提供 OIDC authorization-code + PKCE，固定合成 LEAD subject，首次登录强制 `CONFIGURE_TOTP`；应用仍验证 issuer、audience、RS256、`auth_time` 和 MFA AMR。
4. PostgreSQL 仅允许 TLS，由独立 migrator 动态执行连续 `0001—当前最高版本`，再建立 Web application、identity directory、session gateway、execution Worker 和 verifier 五套分离登录身份。每次启动都重跑 migrator/assert，历史 Exited(0) 不能作为当前源文件的证据。
5. MinIO 仅允许 TLS，Web/Worker 使用独立桶策略与凭据。由只在本机测试使用的 KES mTLS + filesystem keystore 提供 SSE-S3 密钥；启动门必须用两套身份分别完成真实 `AES256` PUT/HEAD/GET/DELETE。
6. ClamAV 必须更新真实签名库，以无网络常驻 `clamd` 避免每次上传重新加载数百万签名；API 仅通过私有 Unix socket 以 `clamdscan --stream` 传递已打开文件。扫描流/单文件上限与 256 MiB 上传一致，解包总量上限 512 MiB，任何超限都以风险结果拒绝。启动门同时要求干净 PDF 返回 clean，EICAR 返回 infected。
7. DeepSeek 规划、DeepSeek 台账提取、Qwen 视觉 OCR 和文书交付是本次核心验收必选项。真实凭据由管理员不回显录入；不得使用假模型。Brave 搜索不在已锁定的核心路径中，默认不登记。
8. 短期 CA、随机凭据和合成账号只存在 Git 忽略的 0700/0600 `runtime/`。脚本不修改 `/etc/hosts` 或 macOS Keychain；浏览器信任 CA 必须由用户亲自授权。
9. `preflight` 只表示可启动条件，`verify` 只表示 `INFRA_ONLY`；两者都固定输出 `full_user_e2e_verified=false`。完整验收必须在真实登录会话内另行断言 Agent 权限、Worker 心跳/目录、材料处理、计划执行和文书交付。`verify` 还要求持久服务 restart=0 且没有 OOMKilled。
10. 本机 Docker VM 只有约 4 GiB 内存；Keycloak 因此使用 128–512 MiB 显式 JVM 堆边界，而不按整台 VM 百分比预留。这不改变 OIDC/TOTP 的验证规则。

## 后果

- 好处：能在不削弱生产代码门禁的前提下，验证真实登录、权限、存储、模型、Worker、文书和恢复路径；开发辅助与完整验收不再混淆。
- 代价：首次构建需要多个镜像、病毒库与真实供应商凭据；用户还必须明确信任短期 CA 并绑定 TOTP。
- 限制：KES filesystem keystore、nip.io loopback 主机和自签 CA 只是内部 Alpha 验收装配，不是生产 KMS、公有 DNS/PKI、备份恢复或商用放行证据。

## 被拒绝的方案

- 扩展 `start_local_web.py` 并伪造完整：拒绝，因为 SQLite、关闭外网/模型和本地身份无法验证真实用户路径。
- 给生产 API 增加测试 JWT/MFA 后门：拒绝，因为会永久扩大身份边界。
- 内部明文 PostgreSQL/MinIO：拒绝，因为无法通过现有 `verify-full`/HTTPS 门禁，且与验收目标不同构。
- 无 KES 的 MinIO `AES256`：拒绝，因为真实应用 PUT 会被对象库拒绝，“服务可打开”不等于材料可存储。
