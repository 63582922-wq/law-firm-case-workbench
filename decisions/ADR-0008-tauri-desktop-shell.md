# ADR-0008：桌面壳采用 Tauri 2 与专用文件夹选择命令

状态：已接受（合成 Alpha 桌面第一段）

## 背景

证据页已将原件读取限定为本机短时授权，但浏览器不应允许律师手填绝对路径，也不应被授予通用文件系统 API。产品需要一个可在律师电脑上运行的原生壳，仅将用户明确选择的案卷目录交给已实现的 loopback API 进行二次检查和确认。

## 决定

1. 桌面壳使用 Tauri 2，前端继续使用现有 Next.js，生产版通过 `output: "export"` 生成静态资产供 Tauri 打包。
2. 不向前端开放 Tauri 通用文件系统插件、asset 协议或 Dialog 插件命令。Rust 核心只登记 `select_case_folder`，并由前端将它封装成既有 `window.lawCaseDesktop.selectCaseFolder` 契约。
3. 命令在打开对话框前校验 UUID 格式的案件标识；选择后解析真实路径，阻断文件系统根目录、整个用户主目录、非目录和无法安全序列化的路径。
4. 选择结果本身不等于授权。前端必须先请求服务端返回无绝对路径的文件夹概要，律师确认后才签发最长十分钟的进程内只读授权。
5. Tauri capability 只绑定内置 `main` 窗口，不定义远程 URL 访问；CSP 仅允许打包资产、Tauri IPC、Blob/内联图像与数字 loopback API，Tauri 保留对本地脚本和样式的哈希/nonce 注入。
6. 桌面壳现阶段不表示已完成安装器、签名、公证、自动更新、OS 身份、Keychain 密钥生命周期或 Python/PostgreSQL/Worker 常驻编排。这些仍是真实案件接入前的独立门禁。

## 为什么不选 Electron

Electron 也能通过 `contextBridge` 和受限 IPC 实现同样边界，但会内嵌一套 Chromium/Node 运行时，并要额外维护主进程、preload 和渲染进程的攻击面。本产品当前只需很小的原生桥，Tauri 的系统 WebView、能力边界和 Rust 专用命令更贴合最小权限目标。如果后续出现只能依赖 Chromium 的功能，再以 ADR 复审，不在本阶段预支复杂度。

## 实现与验证

- Tauri 及 Dialog 依赖在 Cargo/PNPM 锁文件中固定；
- Rust 单元测试覆盖合法/非法案件标识、文件系统根、用户主目录和正常案卷子目录；
- Next.js 完成全路由静态导出，Rust `check/test/clippy` 与前端 `lint/build` 均作为本次合成 Alpha 验证；
- macOS `.app` 生产包已实际启动，可访问 `tauri://localhost`，并通过内置导航进入 `tauri://localhost/evidence`；中文标题、证据页列表和合成 Alpha 阻断语义已从真实桌面 WebView 可访问性树核对；
- 首次生产运行曾因 CSP 缺少显式 `script-src` 而出现空白窗口；增加 `script-src 'self'` 并保留 Tauri 资产 CSP 修改后，重新构建和运行已通过；
- RustSec 首次扫描在锁文件中发现 `RUSTSEC-2026-0194`、`RUSTSEC-2026-0195` 和 `RUSTSEC-2026-0009`；将 `quick-xml` 升级至 0.41.0、`time` 升级至 0.3.55 并把项目最低 Rust 版本提高到 1.88 后，已知漏洞为零；扫描仍对 Linux GTK3 目标依赖给出未维护/不健全警告，因此当前验收只覆盖 macOS，Linux 发布须单独解决该依赖链并通过安全门；
- 原生对话框的真实人工选择、取消、权限提示和持久化 API 整链仍需在持久预览运行模式下另行验收。

## 权威技术依据

- Tauri 在安全优先时建议用专用命令代替通用对话框前端 API：<https://v2.tauri.app/reference/javascript/dialog/>
- Tauri 命令和驳峰命名参数契约：<https://v2.tauri.app/develop/calling-rust/>
- Tauri capability 与内置代码/远程来源边界：<https://v2.tauri.app/security/capabilities/>
- Tauri CSP 在编译时对本地资产注入哈希与 nonce：<https://v2.tauri.app/security/csp/>
- Tauri 对 Next.js 静态导出与 `frontendDist` 的官方指南：<https://v2.tauri.app/start/frontend/nextjs/>

## 后果

- 律师在桌面应用中获得原生文件夹选择器，网页模式继续安全阻断手填路径。
- 选择的绝对路径仅在桌面壳、当前前端请求和 API 进程内短时出现，不进入案件快照或前端持久存储。
- 开发环境新增 Rust/Xcode 依赖，但避免引入通用 Node 文件系统桥和 Chromium 打包体积。
