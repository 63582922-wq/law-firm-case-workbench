# 桌面部署前置检查

管理员在启用受控桌面环境前，可运行只读检查：

```bash
cd backend
.venv/bin/python -m scripts.deployment_readiness \
  --target internal-preview \
  --app-bundle "/受控发布目录/律所案件 AI 工作台.app"
```

对拟商用部署，目标必须显式写为 `commercial-production`：

```bash
cd backend
.venv/bin/python -m scripts.deployment_readiness \
  --target commercial-production \
  --app-bundle "/受控发布目录/律所案件 AI 工作台.app"
```

内部预览环境需要机器可见的受控环境变量：

- `CASE_WORKBENCH_RUNTIME_MODE=postgres-internal-preview`；
- `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES`；
- `CASE_WORKBENCH_POSTGRES_DSN`（工具不会输出其内容，也不会连接数据库）；
- `CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT`（预先创建、绝对、非符号链接且仅当前用户可访问）。

`--format json` 可供安装或运维流程读取。检查会核对固定迁移清单、登记信任 bootstrap、macOS 应用内 sidecar 布局和受管对象目录；不会启动 sidecar、读取 Keychain、执行迁移或修改文件。

商用目标需由受控运行环境提供：

- `CASE_WORKBENCH_RUNTIME_MODE=commercial-production`；
- `CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION=YES`；
- `CASE_WORKBENCH_POSTGRES_DSN`（有效 PostgreSQL 连接串，必须明确数据库名，且数据库名必须以 `_production` 结尾；工具不会输出其内容，也不会连接数据库）；
- `CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT`（预先创建、绝对、非符号链接且仅当前用户可访问）。

`commercial-production` 不接受 `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW`。内部预览仍只接受 `postgres-internal-preview` 与其独立的 `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES`，二者不能互相替代。

通过只表示**静态前置条件完整**，不表示可以接入真实案件。数据库实际迁移/回滚、目标 Mac 的律所登记与逐案权限、备份恢复、签名公证和律师验收仍须由相应受控流程留存证据。

当且仅当商用目标拥有上述独立运行时、明确确认、带 `_production` 标记的数据库名、受管对象目录、迁移清单、登记信任目录和受控 sidecar 的静态形状时，检查可以通过。但报告中的 `release_authorized` 始终为 `false`：生产数据库迁移/回滚、目标 Mac 的真实登记和逐案权限、Keychain、签名公证、漏洞处置、备份恢复、撤销演练及律师验收仍必须由受控人工流程完成并留存证据。
