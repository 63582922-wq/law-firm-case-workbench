# ADR-0083：受管规范编排禁止宿主 Python 源码覆盖

状态：`Accepted`

日期：`2026-09-05`

## 背景

磁盘余量不足时，为读取已认证的 M2 封存响应而在正在运行的本机 Alpha API 上临时挂载了少量经过复核的 Python 源文件。该做法避免了在低磁盘下再次构建大镜像，但它也意味着容器镜像 ID 不能单独说明实际执行的代码版本。若这些绑定留在规范 `compose.yaml`，后续管理员即使完成完整构建，仍可能运行宿主工作区而不是已构建镜像，从而无法把健康检查、审计回执或浏览器结果归因到可发布工件。

## 决策

1. `deployment/local-managed-test/compose.yaml` 不得将任何宿主 Python 文件挂载到 `/app/backend`。运行代码必须来自其 Dockerfile 构建的镜像；证书、只读探针、迁移和合成文本夹具等非应用代码挂载不受此规则影响。
2. 封存响应的临时本机检查只能使用独立的 `compose.sealed-recovery-overlay.yaml`。`scripts/manage_local_managed_test.py` 的 `preflight`、`start`、`resume-provider-gate` 和 `verify` 均不自动加载该文件；使用它的容器明确不是可发布镜像或商用基线。
3. 本机 `preflight/start` 读取规范 Compose 时必须拒绝任何 `/app/backend/*.py` 宿主绑定；运行时 `verify` 必须通过 `docker inspect` 检出仍在运行的此类绑定，并以不可发布状态报告。报告只显示容器内目标路径，不泄露宿主绝对路径。若 Docker 状态读取本身超时，`verify` 只能报告当前无法判断，不能把未知状态误写为服务已停止。
4. 该边界不改变 M1/M2/M3 状态、不触发 Worker、不发送模型请求、不写入案件、对象库或身份数据。当前已运行的历史 API 覆盖层只可保持为局部验收证据，直到有至少 5 GiB 宿主余量完成干净镜像重建。

## 实施证据（2026-09-05）

- 规范 Compose 已不含 `:/app/backend/*.py` 宿主绑定；五项 API 与三项 Worker 的历史恢复绑定均移至显式的
  `compose.sealed-recovery-overlay.yaml`。
- 管理员预检已实际返回“规范 Compose 不挂载宿主 Python 源码”；静态 YAML 解析、Python 编译及 15 项相关
  管理员/渲染部署测试通过。
- 当前宿主空间仍低于 5 GiB，故未启动重建。Docker 状态读取随后超时，`verify` 已实际返回“无法判断服务是否仍在运行”，
  没有把控制面未知误写为服务停机，也没有对 Docker 或案件数据执行写操作。

## 后果

- 空间恢复后的正式重建会以构建镜像而非工作区文件作为可验证发布对象；浏览器、文书和 Agent 验收可绑定到该工件。
- 当前 API 即使健康，也会在运行时完整性检查中被正确标为“非发布基线”，不会因源码覆盖而被误报为已部署版本。
- 该措施只改善工件可归因性；它不替代真实 OIDC/MFA 浏览器旅程、律师认可、备份恢复、跨租户测试、供应商治理或商用放行。
