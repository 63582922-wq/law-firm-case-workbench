# ADR-0024：商用桌面运行必须使用独立运行时与明确确认

状态：已接受（静态门与受控装配完成；真实商用放行仍待运维与律师验收）

## 背景

`postgres-internal-preview` 的用途是合成和受控预览开发：它只接受名称以 `_preview` 或 `_test` 结尾的专用 PostgreSQL 数据库。若部署检查仅因“存在 PostgreSQL DSN”就把该模式视为可商用，管理员可能误把预览数据源、错误环境变量或未完成的登记/密钥装配当成真实案件环境。

商用目标需要一个能被代码、安装检查和审计记录明确区分的运行时，而不是给预览模式改标签。与此同时，运行时选择本身不能读取律师密钥、探测数据库、伪造律所登记、执行迁移或把静态检查包装成最终放行。

## 决定

1. 新增唯一的 `commercial-production` RuntimeMode；它与 `synthetic-alpha`、`postgres-internal-preview` 互斥。
2. 该模式必须同时提供精确的 `CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION=YES`。`postgres-internal-preview` 不得携带该确认，`commercial-production` 也不得携带 `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW`。缺失、拼写错误或混用均在案件路由监听前 fail closed。
3. `commercial-production` 必须提供可由 PostgreSQL 连接串解析器解析、且明确包含数据库名的 `CASE_WORKBENCH_POSTGRES_DSN`。数据库名按不区分大小写检查，必须以 `_production` 结尾；因此 `_preview`、`_test`、通用 `postgres` 和开发库名都被拒绝。解析不建立网络连接，错误和对象表示不得回显完整 DSN 或密码。该命名是静态身份标记，不替代实际迁移、权限或备份验收。
4. 商用模式沿用而非绕过现有受控桌面装配：重新验签的律所登记与 OS 绑定短会话、固定 macOS Keychain 中只读的 32 字节 AES-256-GCM 工件密钥、预创建的绝对/非符号链接/非根目录/0700 受管对象根，以及 PostgreSQL 对活跃用户和逐案角色的独立复核全部仍是必要条件。
5. `deployment_readiness --target commercial-production` 仅在上述模式、确认、数据库名、受管对象目录、迁移清单、登记信任目录与受控 sidecar 的**静态形状**同时成立时通过。报告的 `release_authorized` 固定为 `false`，并保留人工放行门。
6. 部署检查不得启动 sidecar、读取 Keychain、连接数据库、执行迁移、修改文件、伪造登记或调用外部服务。真实生产动作由独立、可审计的运维流程完成。

## 不选择的方案

- **复用 preview 模式再增加“商用”标签**：无法在代码与检查报告中区分 `_preview/_test` 数据库，容易造成错环境放行。
- **用前端开关或普通配置页确认**：WebView 不是运行时权限根，不能证明管理员已经接受生产数据边界。
- **让静态检查连接数据库或验证 Keychain**：会把只读预检变成凭证读取和状态变化风险；这些步骤必须在目标机的受控放行流程中单独留证。

## 后果与仍需完成的门

- 受监护 sidecar 可以在满足全部本机条件后装配商用持久路由；它不会因静态检查通过而自动打开真实案件、自动运行 Worker 或自动调用模型/外部服务。
- 商用管理员仍须在专用环境完成并留存：数据库迁移和回滚验证、真实律所登记/续期/撤销与逐案权限、Keychain/对象根安装、安装包签名和公证、漏洞处置、备份恢复、遗失设备处置和律师真实工作流验收。
- 当前仓库与测试只使用合成身份、合成密钥和不连接的 PostgreSQL 适配器验证门逻辑；不得把这些测试当作真实案件或生产服务互操作证据。
