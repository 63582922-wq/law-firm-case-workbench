# ADR-0082：受管完整重建的宿主磁盘余量门

状态：`Accepted`

日期：`2026-09-05`

## 背景

本机受管 Alpha 的 `start` 会先停止旧消费者，再顺序重建 API、Keycloak、隔离文书渲染和前端镜像；正式 Web
管理员入口同样会执行 `docker compose up --build`。此前 Docker Desktop 的宿主磁盘耗尽曾导致 BuildKit I/O
异常和 PostgreSQL 运行时锁残留。若预检只检查凭据、编排和 Docker 可用性，却不在停止服务前检查空间，启动命令
可能在重建中失败，既不能形成可发布镜像，也会扩大恢复范围。

## 决策

1. `scripts/manage_local_managed_test.py preflight/start` 与
   `scripts/manage_managed_web.py preflight/start` 都必须在任何 Compose 配置、停止消费者或镜像构建之前，使用
   `shutil.disk_usage(ROOT).free` 核验宿主 Data 分区可供当前管理员使用的空间。
2. 完整镜像重建的硬下限为 **5 GiB**。不足、读取失败或读数无效时，预检必须失败关闭，并明确说明不会停止旧容器、
   构建镜像、启动 Agent 或发起模型调用。
3. 此门只约束完整受管重建；它不主动停止已运行的服务、不删除案件数据、不删除命名卷，也不把现有服务健康或
   `verify` 的 `INFRA_ONLY` 结果伪装为发布结论。
4. 不能为了通过余量门而由脚本清理其他项目、用户资料、Docker 命名卷或案件对象。任何可重建缓存的清理都必须先
   明确归属，且与数据卷分离留证。

## 后果

- 重建在低磁盘下会更早拒绝，避免“先停服务、后构建失败”的半恢复状态。
- 本机验收与正式管理员入口使用同一最低运维边界；这不替代镜像签名、漏洞扫描、备份恢复、真实 OIDC/MFA、
  律师验收或商用放行。
- 已在静态测试中覆盖阈值边界：`5 GiB - 1 byte` 拒绝，精确 `5 GiB` 允许继续其他前置门。
