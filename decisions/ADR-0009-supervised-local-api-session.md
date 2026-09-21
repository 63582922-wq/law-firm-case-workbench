# ADR-0009：自包含本机 API、父进程监护与 OS 绑定短时会话

状态：已接受（合成 Alpha 桌面第二段）

## 背景

律师最终应直接运行桌面应用，不能被要求另装 Python、手工启动 API 或把案卷路径交给浏览器。另一方面，“当前 macOS 用户启动了应用”只能证明本机进程上下文，不能证明某位执业律师、律所归属或案件权限。若在身份和数据库尚未配置时直接挂载持久案件路由，会把本地便利性错误扩大为授权。

## 决定

1. 使用 PyInstaller 把 Python 3.12、FastAPI 和 uvicorn 打成当前平台的单文件 `lawcase-local-api`，通过 Tauri `externalBin` 随 macOS `.app` 分发。最终安装用户不依赖系统 Python。
2. 只有 Rust 主进程可以启动 sidecar。Tauri Shell 插件未向 WebView capability 开放 execute/spawn/stdin/kill；发布构建的未使用命令裁剪也确认这些前端命令已被移除。
3. Rust 生成 64 个十六进制字符的随机挑战，仅经子进程 stdin 发送。sidecar 必须返回固定协议、`READY`、有效 PID、动态端口和挑战 SHA-256；任一不符立即终止进程并把案件访问置为阻断。
4. sidecar 只绑定 `127.0.0.1:0`，由操作系统选择端口；当前只开放无 OpenAPI/Docs 的 `/healthz`，明确返回 `persistence=not-configured` 与 `identity=not-enrolled`。
5. 桌面退出时 Rust 终止 child；sidecar 同时监测父 PID 和 stdin。macOS 生产包已实测启动后出现父进程、PyInstaller bootloader 和服务进程，退出后全部消失。
6. 后端另实现 `DesktopSessionAuthority`：服务端预先注入 Actor/firm/role，一次性 bootstrap 交换短时 Bearer，只保存令牌摘要，强制数值 loopback 与 `tauri://localhost`，支持过期和撤销。浏览器请求不能选择 actor、firm 或 role。
7. 该身份契约现阶段不装配到打包 sidecar：缺少 Keychain 中的受保护律师登记档案、撤销机制和数据库案件成员关系。健康 sidecar 不等于真实登录或真实案件可用。

## 实现与验证

- 严格握手测试拒绝多余客户端身份字段、错误协议、短挑战、布尔 PID 和超长输入；
- 桌面会话测试覆盖一次交换、重复使用、错误 Origin、非数值 loopback、伪造令牌、过期、撤销和秘密脱敏；
- Rust 测试覆盖回执协议、挑战摘要、PID 与端口，并经 `clippy -D warnings`；
- 自包含二进制已独立启动，实际访问动态 `127.0.0.1` 健康端点成功；
- 新 macOS `.app` 已真实启动，首页显示“本机服务已就绪（案件仍禁用）”，证据页导航正常；打包 sidecar 实际只监听数字 loopback，关闭应用后无残留进程；
- 生产构建开启 `removeUnusedCommands`，输出确认 Shell execute/spawn/stdin/kill 与通用文件、Dialog 命令均从 WebView 命令面移除。
- Python 依赖审计曾发现 `cryptography 46.0.7` 的 4 条已知漏洞；依赖升级并锁定为 `50.0.0` 后，加密对象专项回归和 `pip-audit` 均通过且无已知漏洞。

## 权威技术依据

- Tauri Shell 插件默认阻断危险命令，能力文件必须显式授权前端使用：<https://v2.tauri.app/plugin/shell/>
- Tauri 官方 sidecar 指南说明 `externalBin` 打包、平台命名和桌面自包含进程模式，并提示 localhost、stdin/stdout 与本地 socket 各有安全取舍：<https://v2.tauri.app/learn/sidecar-nodejs/>
- Tauri permission/capability 将命令权限绑定到具体窗口或 WebView：<https://v2.tauri.app/security/permissions/>
- PyInstaller 官方文档说明 macOS 单文件产物是可从命令行启动的 Unix 可执行文件，且跨平台必须分别构建：<https://pyinstaller.org/en/stable/usage.html>

## 后果与后续门

- 桌面安装用户不需要 API key，也不需要手工启动 Python 服务；API key 属于未来外部模型/研究供应商配置，与本机运行不是同一门。
- sidecar 单文件启动存在 PyInstaller 解包延迟，桌面状态会显示 `STARTING`，验证完成后才显示就绪。
- 下一段必须先实现 Keychain 律师登记档案、明确注册/撤销界面和数据库成员关系复核，再把 `DesktopSessionAuthority` 装配到持久 API；不能用 macOS 用户名、主机名或浏览器字段推导执业身份。
- 安装器签名、公证、sidecar 启动前签名/哈希证明、休眠唤醒、崩溃恢复、自动更新和平台分别构建仍是发布门。
