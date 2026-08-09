# 后端核心（内部合成 Alpha）

本目录开始实施 `PLAN.md` 的 Phase 2：它是模块化单体的领域核心，而不是可对外部署的服务。

当前范围：

- 用代码强制案件生命周期、角色权限、乐观并发版本和幂等命令；
- 记录最小化审计事件；
- 在上游变更时使当前提交包失效；
- 只使用严格合成数据和内存仓库，便于先验证领域控制逻辑；
- 提供仅供合成环境使用的 FastAPI 契约边界：固定合成角色选择、幂等命令、版本冲突、审批和锁定；
- 提供本地案卷文件夹的只读清单组件：文件哈希、容量上限、不写原件、不跟随符号链接；
- 不包含生产 HTTP 身份认证、已执行数据库、真实文件接入、外部模型、OCR、真实文书、真实案件或导出功能。

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
