# 后端核心（内部合成 Alpha）

本目录开始实施 `PLAN.md` 的 Phase 2：它是模块化单体的领域核心，而不是可对外部署的服务。

当前范围：

- 用代码强制案件生命周期、角色权限、乐观并发版本和幂等命令；
- 记录最小化审计事件；
- 在上游变更时使当前提交包失效；
- 只使用严格合成数据和内存仓库，便于先验证领域控制逻辑；
- 提供仅供合成环境使用的 FastAPI 契约边界：固定合成角色选择、幂等命令、版本冲突、审批和锁定；
- 提供本地案卷文件夹的只读清单组件：文件哈希、容量上限、不写原件、不跟随符号链接；
- 提供公开研究网关的合成领域边界：敏感检索词阻断、来源域名白名单、律师显式授权、请求/响应哈希账本；
- 提供合成利息与冲抵预览：只接受已批准的 CNY 事件、连续规则期间和明确冲抵顺序，并以独立日步进校验器复算；
- 提供 PostgreSQL 16+ 的 UUID 持久化仓储适配器：RLS 租户上下文、命令幂等、锁行版本检查、append-only 审计和 Outbox 在同一事务中写入；默认 API 不使用它；
- 提供可由 Tauri 监护的自包含禁用态 sidecar，以及一次性桌面启动凭证、令牌摘要、短时 Bearer、数值 loopback/Origin、过期与撤销的本机会话契约；没有服务端登记律师身份和持久依赖时不开放案件路由；
- 提供律所 Ed25519 签名、严格规范化字段、30 天上限、安装秘密摘要绑定、Keychain 双项目只读和短时会话装配的律师登记契约；测试只使用生成密钥和假 Keychain runner，不写用户真实 Keychain；
- 提供登记、续期、远程撤销与本机停用的生命周期接口：激活请求不含角色，签名结果先验证再 CAS 保存，续期不切换身份，撤销回执拒绝时不删除凭证，本机停用不冒充远程撤销；当前没有真实 issuer 或可写 Keychain 适配器；
- 提供官方公开案例元数据目录及严格加载器；未许可来源不能声明批量抓取、全文仓库、商业训练或正式案件使用；
- 不包含生产 HTTP 身份认证、已配置或实际执行的数据库、真实文件接入、外部模型、OCR、真实文书、真实案件或导出功能。

`POST /v1/calculation-previews` 仅返回**非持久化的合成预览**，不是正式 `CalculationRun`，不支持真实身份或真实案卷。其调用者仍必须在生产实现中先完成法律规则、适用期间、付款性质与冲抵顺序的权限化审批；本 Alpha API 的 `X-Alpha-Actor` 只是一份固定测试选择器，绝非认证机制。

独立的持久化预览 API 已提供正式计算读写边界：正式运行只由数据库中已确认交易、已批准付款分类和当前已批准案件法律规则包生成，保存逐期本金/利息、逐笔冲抵及独立复算哈希；服务或正式运行缺失时返回明确阻断，不会调用上述合成预览作为替代。官方法源快照登记与法律规则包审批命令仍未完成，因此该边界尚不能用于真实办案。

`PostgresMatterStore` 的使用契约和显式破坏性测试门见 [`docs/POSTGRES_INTEGRATION_CONTRACT.md`](../docs/POSTGRES_INTEGRATION_CONTRACT.md)。它只接受 UUID 标识，故 Alpha 的 `alpha_*` 调用不会触发数据库连接。

后续替换内存仓库为 PostgreSQL 时，必须保持同样的 Command、版本、幂等和审计语义，并补充数据库事务、RLS 和持久化 Outbox。不得将此目录的内存实现误用于生产。

执行固定自动化检查：

```sh
PYTHON=/Users/cft/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
$PYTHON -m unittest discover -s backend/tests -v
```

合成 API 的开发环境由 `backend/pyproject.toml` 和 `backend/uv.lock` 固定：

```sh
cd backend
uv sync --group dev
.venv/bin/python -m unittest discover -s tests -v
```

构建当前平台的桌面 sidecar：

```sh
.venv/bin/python scripts/build_desktop_sidecar.py
```

生成物写入 Tauri 的忽略目录并由桌面构建重新生成，不进入 Git。
