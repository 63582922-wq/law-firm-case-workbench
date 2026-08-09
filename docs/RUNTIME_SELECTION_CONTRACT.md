# 运行模式选择契约

状态：`已实现显式配置、依赖注入与 Tauri 文件夹选择桥；生产持久化运行仍未开放`

## 默认行为

应用默认且无条件使用 `synthetic-alpha`：内存仓库、固定合成角色、`alpha_*` 标识符，不读取数据库地址、不建立 PostgreSQL 连接，也不接收真实案卷。导入 `case_api.app` 不会读取环境变量；只有桌面启动器显式调用 `load_runtime_settings()` 才会解析运行模式。

## 内部持久化预览门

选择 PostgreSQL 必须同时满足：

1. `CASE_WORKBENCH_RUNTIME_MODE=postgres-internal-preview`；
2. `CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES`；
3. `CASE_WORKBENCH_POSTGRES_DSN` 指向库名以 `_preview` 或 `_test` 结尾的专用数据库；
4. 后续独立 API 提供 UUID 身份，并由数据库内未撤销的案件成员角色再次授权。

任一条件不满足即阻断。合成模式出现任何持久化配置也会阻断，避免配置残留让测试页面误连数据库。配置对象的文本表示不包含 DSN，日志和错误信息不得输出密码或完整连接串。

## 尚未开放

- 现有 `case_api.app` 仍是合成 Alpha，不能因存在 `RuntimeServices` 就切换为 PostgreSQL；
- 尚无生产身份、MFA、桌面本地会话、密钥库或数据库迁移部署器；
- 原件单页预览代码要求本机 `pdftoppm`，并以独立临时目录生成 PNG；正式安装器必须固定并校验该渲染器版本；Tauri 桌面桥已只返回用户通过原生对话框选择的目录，不接受网页手填路径、文件系统根目录或整个用户主目录；
- 桌面壳现阶段只完成选择桥和静态资产封装；它尚未启动/监护 Python API、Worker、PostgreSQL 或 `pdftoppm`，也未提供真实律师登录身份；
- 尚未在获授权的专用 PostgreSQL 上执行集成测试；
- 当前门只允许继续开发内部持久化预览，不构成真实案件接入或生产放行。
