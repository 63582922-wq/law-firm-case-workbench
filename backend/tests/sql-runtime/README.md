# 可选内存 SQL 检查

## 合成原件生产读取器检查

`PYTHONPATH=backend backend/.venv/bin/python backend/tests/sql-runtime/native_materials.py artifacts/unassisted-court-g763df86`
只接受项目私有 artifacts 下的无辅助标记合成草稿；核对冻结文件哈希后调用生产 `read_authorized_pdf_document`，
打印文件/页级摘要，图片保持未读取。它不调用 golden 元数据读取器，不创建 Web 授权、数据库登记、Agent 运行或模型请求。
已实跑 7 PDF/84 页；4 JPEG 等待视觉链。不要把此只读检查作为混合材料 Agent 完成记录。

## 原生对象存储探针

最新实测：磁盘恢复约 4.3GiB 后，在原目录版本化 SSE-AES256 写入/按版本读回通过，观测峰值 RSS
196,752KiB，服务已停止，证据 `artifacts/native-minio.EU6APN/probe-run.log`。目录现已有数据与私有密钥，
不可重新当空目录执行探针或删除密钥；后续执行已有 Agent 任务时使用同一存储。尚未执行 Agent 任务和独立成果校验。

后续同一 Run 已实跑至 READY_FOR_REVIEW：`minio-probe.py --resume-run UUID --pg-socket PATH --pg-data PATH`
调用 `native_task.py`，真实 Worker/上下文适配器/S3 保存/独立 DB 角色回读及验证通过，并晋升一项 REVIEW 工作计划候选。
证据 `artifacts/native-minio.EU6APN/task-execution.log`；一次任务认领，一个候选，原一次模型外发未增加。
该模式禁止新规划，最多三个步骤，已有运行日志拒绝覆盖；出现不确定状态须先读取现场，不能删日志重跑。
仅核对已完成成果使用互斥的 `--inspect-run UUID`（同样指定 socket/data），数据库默认只读，
复用生产独立对象读取端，校验字节与运行回执，解析律师显示区块并确认运行状态前后不变；不执行 Worker。
每次保留独立存储日志，不覆盖原执行记录。该模式已对上述原 Run 实跑通过，实际只有代理档案一个区块，
不能把空待确认列表解释为整案无缺口。它不代替律师会话授权或实际浏览器验收。
仅隔离合成测试，S3 root 凭据共用且 loopback HTTP，本地加密密钥不代表生产 KES/IAM/TLS 验收。

`minio-probe.py` 仅接受项目 artifacts 内的私有新目录，以及官方 release 元数据可核对的同版 macOS MinIO。
启动前至少保留 1.5GiB 磁盘余量；本次余量降到约 1.0GiB，因此在创建密钥/数据前拒绝。下载已校验，程序未执行。
探针预设 20 秒启动界限、96MiB Go 内存目标和 256MiB 观测 RSS 上限，检查 loopback 上的版本化 AES256 读写，
完成/失败停止子进程。RSS 是采样而非操作系统硬隔离；Go 内存目标也不是进程总内存上限。只处理合成探针字节。
后续即使通过，也不代表生产 KES/TLS/独立存储账号隔离或 Agent 成果链已验收，不可绕过正式配置上线。

## 当前实际结果（2026-09-05，优先于下方历史记录）

单次真实规划：`test_one_synthetic_provider_plan_enters_durable_graph` 在 `data.DZSVON` 通过，
日志 `native-single-provider.log`；仅合成代理档案，已配置 deepseek-v4-pro，一次请求，最多 2048 输出 token。
默认跳过，须显式 `CASE_WORKBENCH_RUN_ONE_PLANNER_TRIAL=YES`，仍需私有原生空库安全检查及环境中已有密钥。
不要把这个收费试跑加入普通回归或失败自动重试。真实外发由产品 guard 留账，无本地规划事件；只证明任务图进入
数据库，不证明任务执行、成果质量、浏览器或文书。该库现在包含待执行任务，应保留继续，不重建/重规划。

原生 PostgreSQL 16.15 新空库：78 项迁移、部署后置加固、37 项 ACL 检查通过。初次运行在 0064
因受限函数所有者缺少 schema CREATE 失败；按 ADR-0087 修正部署入口的临时权限窗口后，在另一空库通过，
旧失败现场未重置。最终额外查询确认 ledger confirmation / recovery lock 两个所有者均 NOLOGIN、非超级用户、
无 schema CREATE，schema owner 已降权。两个临时实例均已停止，没有启动 Docker。

日志：`artifacts/native-pg16/build.AV40mw/native-owner-window.log`；1.70 秒，迁移客户端最大 RSS
55,951,360 bytes（不是服务器及系统总内存）。尚未证明带业务行的 RLS、触发器写入/回滚、并发恢复、
律师端操作和整案 Agent 交付；下方“尚未执行”的历史文字不再表示当前空库迁移状态。

## 原生 PostgreSQL 16 低内存路径

已迁移且未加入数据的隔离库可复用 `tests.test_postgres_integration`：显式设置
`CASE_WORKBENCH_NATIVE_PREPARED_SOCKET` 和 Unix socket 的 `CASE_WORKBENCH_TEST_DATABASE_URL`。
入口在连接前验证 socket 目标，在写入前核验服务器目录/版本/禁用 TCP/完整迁移标记/空 firms；该模式不清库。
本轮四项既有用例（建案幂等审计、跨租户读取、运行认领、事实确认）通过，日志 `native-business-configured.log`。
后续原生模式已分离 fixture 管理连接与业务调用：建案/台账使用 Web 角色，运行认领使用 Worker 角色，
同四项用例在 `data.3VV1Gv` 通过，日志 `native-runtime-roles-business.log`。入口核实 current_user、非超级用户、
无 BYPASSRLS；角色通过连接选项切换，不代表独立登录/TLS 认证验收。两库现在含合成数据，入口会拒绝再次
当空库运行，不得重置掩盖结果。正文提案、授权及恢复尚未具备真实数据库业务用例。
临时资源限制须写入实例自身配置：pg_ctl 的一次 start 参数不会在下一次 start 自动重用。

同一迁移/ACL 脚本现可通过 `--native-socket=绝对路径` 使用原生 PG16，不导入 PGlite/WASM，也不增加新的 SQL 测试副本。
`native-driver.py` 使用项目已有 psycopg，保持同一个数据库会话及 SET ROLE/事务语义。只接受项目 artifacts 内的 Unix
socket，服务器数据目录也必须位于 artifacts/native-pg16；拒绝非 PG16、已存在应用角色或 public 表的实例。
只运行空库迁移及权限检查，不加载案件，不连接远程服务器；失败现场应保留诊断，不能在原失败库反复重置。

启动临时实例时须禁用 TCP 监听，socket 目录权限 0700，shared_buffers=16MB、max_connections=10、work_mem=1MB，
保持 fsync/full_page_writes 开启，串行执行。实例生命周期由调用者管理；脚本关闭自己的连接，不停止或删除数据库。
此路径尚不证明整案 Agent、生产 TLS、浏览器或文书渲染可用。

```sh
node --max-old-space-size=128 backend/tests/sql-runtime/migration-smoke.mjs --native-socket=/项目绝对路径/artifacts/临时socket目录
```

本机临时构建来源为 PostgreSQL 16.15 官方发布包及 OpenSSL 3.5.8 官方仓库发布包，校验值分别为
`c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed` 和
`a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2`。
OpenSSL 独立校验文件下载超时，后者与官方 GitHub release asset 的 SHA-256 digest 核对一致。
仅在项目忽略目录编译/安装，不改系统安装；PostgreSQL 需启用 OpenSSL 才能安装真实 pgcrypto，不能用替身函数跳过。

## PGlite 路径（本机内存不足时不运行）

仅开发测试，PGlite 0.5.8（PostgreSQL 18.3 WASM）。不启动服务、不读环境中的数据库地址、不加载案件数据，结束即关闭内存数据库。
不接入日常轻量测试：本机一次 0078 检查结束时 RSS 约 1305 MiB，明显高于普通单元测试。V8 的 384 MiB 参数不限制 WASM 总内存。
运行前必须确认内存余量；用户内存不足时不要自动运行。安装的测试依赖约 25 MiB，不进入应用发布依赖。

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm test -- --focused-0078
```

针对性模式执行真实 0078 SQL，使用最小外键/请求前置表及从 0073 提取的版本字段/形状约束、不可变、请求校验与入队触发器定义，
同时加载部署 `prepare-schema.sql` 的默认授权设置；当前脚本检查 15 项角色表/列权限，包含网页及后台不能直接入队。
前置表不具备完整业务字段，针对性模式不得用来执行请求行或证明旧触发器/正文登记正确；这些须使用全量模式。
这不是完整迁移、数据行隔离、并发租约、真实 PostgreSQL 16 或浏览器验收。

`npm test` 从空库执行全部迁移。此前在 0048 的权限失败来自测试引导未复现规范部署的受限管理窗口，
不能据此认定生产迁移存在同样故障。源码核对发现真实 `migrate-and-seed.sh` 会先撤销登录迁移角色的成员关系，
临时提升 NOLOGIN schema owner，逐文件指定该角色，迁移后收紧权限并降权，再恢复成员关系。
测试已改为读取该脚本中的窗口语句，检查临时角色不可由迁移登录角色到达、结束后已降权，并在失败时清理。
全量模式同时执行真实 post-migrate-hardening.sql；针对性模式因缺少全量依赖表而不执行该脚本。
这次入口修正仅做源码/语法检查，尚未高内存复跑；全量迁移仍未获得通过证据。

本轮证据：修复前，0078 的后台角色意外拥有生成任务 INSERT 权限；修复后，0078 的 6 项权限检查通过。
原因是部署默认权限授予具体角色，单独 `REVOKE ... FROM PUBLIC` 不会撤销这些角色已经取得的权限。
上述通过记录早于正文请求登记和后台按认领取稿的变更；当前 15 项检查及新增迁移段尚未执行。

## 低内存语法检查（不执行数据库）

单次恢复入口接线后：当前迁移为 93 条 SQL/10 个函数/1 个 DO，解析通过；两份改写后的旧模板守卫函数体也仅解析。
可选脚本检查恢复表/视图读取、仅核验者可追加、锁函数执行资格及专用锁角色隔离，共 37 项 ACL 检查；针对性夹具
补齐复合外键、旧守卫及锁函数所需运行/案件空表。此版本仅脚本语法检查，37 项实际查询未执行。

在项目根目录使用开发环境安装 `pglast==6.16`（对应 PG16 的解析器，非应用发布依赖）：

```sh
uv pip install --python backend/.venv/bin/python --only-binary :all: --no-deps --no-cache pglast==6.16
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -c 'import sys,re,pglast; s=sys.stdin.read(); pglast.parse_sql(s); [pglast.parse_plpgsql(f) for f in re.findall(r"CREATE (?:OR REPLACE )?FUNCTION[\s\S]*?\$\$;",s)]; print("SQL/PLpgSQL syntax parsed; not executed")' < backend/migrations/0078_lawyer_document_content_proposals.sql
```

此前解析 55 条 SQL 和 6 个 PLpgSQL 函数通过，峰值 RSS 42,844,160 bytes（约 41 MiB）。
入队及人类状态读取接线后重新解析当前 62 条 SQL 和 7 个函数通过；本次没有重新测量峰值，也未执行数据库。
它发现了回执条件 CASE 表达式缺少括号的问题，修复后通过。仅作变更后的语法检查，不会验证关系/列名、
RLS、授权、触发器执行或并发，不据此启用消费者。功能说明见 [pglast 官方 API](https://pglast.readthedocs.io/en/latest/api.html)。
