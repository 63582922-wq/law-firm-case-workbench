# Keycloak OIDC 验收合同

这一层只服务于本机受管完整测试环境，不是开发模式配置，也不是生产身份库。它固定以下安全合同：

- Keycloak `26.7.0` 通过 `start --import-realm` 以 production profile 启动，服务名为 `keycloak`，容器内部只监听 HTTP `8080`；
- Caddy 在 `https://identity.127.0.0.1.nip.io` 终止 TLS，并用受控的 `X-Forwarded-*` 头转发给 Keycloak；
- realm 固定为 `lawcase-test`，confidential client 固定为 `lawcase-web-workbench`；
- 只允许 Authorization Code，且 PKCE 必须为 `S256`；禁用 implicit、direct grant、device grant、CIBA 和 service account；
- 回调只允许 `https://workbench.127.0.0.1.nip.io/api/v1/auth/oidc/callback`（非 443 端口时由渲染器精确加入端口）；
- 浏览器登录流必须先验证密码，再执行真实 TOTP，不允许仅凭 SSO cookie 绕过 MFA；
- ID token 固定使用 RS256，必须含 `iss/sub/aud/exp/nbf/iat/auth_time/amr`，且 `amr` 中的 `mfa` 来自 TOTP authenticator execution reference，不是写死的 token claim；
- 合成主办律师的 OIDC subject 与数据库 actor 固定为 `22222222-2222-4222-8222-222222222222`。firm、角色和授权不从浏览器 token 发放，由应用在服务端按 `(issuer, subject)` 查询受管身份目录。

Keycloak 26.7.0 不会仅因 realm/client `notBefore` 就在 ID token 中序列化 `nbf`，而应用合同要求该数字字段必须存在。因此模板显式写入数字 `nbf=0`；这只是 token schema 兼容层，不伪造 MFA。`amr=mfa` 仍只能由实际执行 TOTP 后的 built-in AMR mapper 产生。

## 密钥边界

`lawcase-test-realm.template.json` 不含可用密码或 client secret。`render_realm.py` 只从外部 `0600` env 文件读取：

- `LAWCASE_OIDC_CLIENT_SECRET`
- `LAWCASE_TEST_LEAD_PASSWORD`
- `LAWCASE_TEST_LEAD_SUBJECT`
- `LAWCASE_TEST_LEAD_ACTOR_ID`
- `LAWCASE_TEST_LEAD_USERNAME`
- `LAWCASE_WORKBENCH_HOST`
- `LAWCASE_IDENTITY_HOST`
- `LAWCASE_HTTPS_PORT`
- `LAWCASE_KEYCLOAK_REALM`
- `LAWCASE_KEYCLOAK_CLIENT_ID`

渲染后的 realm 内含测试用户密码和 client secret，因此它也必须是 `0600`、位于 Git 忽略的 `runtime/` 目录中。管理脚本不回显这些值。

## 渲染和静态验证

正常情况下从项目根目录运行总管理器：

```text
python3 scripts/manage_local_managed_test.py init
python3 scripts/manage_local_managed_test.py preflight
```

`init` 会在 `deployment/local-managed-test/runtime/` 生成私有 env、realm 和 OIDC contract。如需单独复核渲染器，使用：

```text
python3 deployment/local-managed-test/keycloak/render_realm.py \
  --env-file deployment/local-managed-test/runtime/local-managed.env \
  --output deployment/local-managed-test/runtime/keycloak/realm.json \
  --contract-output deployment/local-managed-test/runtime/keycloak/oidc-contract.json

python3 deployment/local-managed-test/keycloak/verify_realm.py \
  deployment/local-managed-test/runtime/keycloak/realm.json \
  --expected-workbench-origin https://workbench.127.0.0.1.nip.io
```

任何未解析模板标记、非 `0600` 输入/输出、回调漂移、PKCE/MFA 降级、丢失 audience/auth_time/nbf/AMR mapper，或在 token 中加入 firm/应用角色权威，都会使验证失败。

## 可验证的导入与 production 启动

下列离线导入会让固定版本 Keycloak 解析真实 realm schema，完成后退出：

```text
docker run --rm \
  --mount type=bind,src="$PWD/deployment/local-managed-test/runtime/keycloak/realm.json",dst=/tmp/lawcase-test.json,readonly \
  quay.io/keycloak/keycloak:26.7.0@sha256:0f198be292568439d700cdbfb893e69a6009bb43a94a06a945b1d3d506c76b13 \
  import --file /tmp/lawcase-test.json
```

完整栈始终使用正式启动命令，不使用 `start-dev`：

```text
python3 scripts/manage_local_managed_test.py start
python3 scripts/manage_local_managed_test.py verify
```

Compose 中的 Keycloak 命令是 `start --import-realm`，且精确锁定上述 26.7.0 manifest digest。服务日志应显示 `Profile prod activated`，管理健康端口应返回 ready；总管理器的 `verify` 还会通过本机 CA 验证 OIDC discovery 和对外 issuer。

## 真实协议验收脚本

`acceptance_probe.py` 不调用密码直授。它真实提交浏览器授权表单，完成 TOTP 绑定，等待下一个 30 秒 TOTP 时间片，再用“密码 + TOTP”发起第二次 Authorization Code + PKCE 登录。它最终使用实时 JWKS 验证 RS256 签名、回调参数白名单和全部 ID-token claims。

该脚本会消耗首次 TOTP 绑定，所以**只能对新建的、执行后立即销毁的独立 realm/数据库副本运行**：

```text
python3 deployment/local-managed-test/keycloak/acceptance_probe.py \
  --fresh-disposable-realm \
  --realm-file /path/to/disposable/realm.json \
  --contract-file /path/to/disposable/oidc-contract.json \
  --ca-file /path/to/disposable/ca.crt
```

不要对律师将要手工验收的持久 `lawcase-test` 运行它。成功输出必须同时包含：

```text
Keycloak production Authorization Code + PKCE + real TOTP PASS
RS256 and iss/sub/aud/exp/nbf/iat/auth_time/amr/nonce claims PASS
Callback allowlist code/state/iss PASS
```

## 律师首次 TOTP 登录

1. 管理员先完成 `start` 和 `verify`，并按顶层 README 说明让浏览器信任该短期本机 CA。
2. 从项目根目录运行 `python3 scripts/manage_local_managed_test.py login-info`，只在本机终端读取合成账号和初始密码。
3. 在浏览器打开 `https://workbench.127.0.0.1.nip.io`，选择登录，输入用户名 `lead.lawyer` 和第 2 步密码。
4. Keycloak 会强制显示 TOTP 二维码。用自己的验证器应用扫码，输入当前 6 位验证码完成绑定；不要把二维码、种子或验证码发给 Codex。
5. 首次绑定这个 required action 不能代表应用已看到 `amr=mfa`。绑定完成后退出 Keycloak/工作台，再发起一次新登录，这次必须依次输入密码和新的 TOTP 验证码。
6. 第二次回调成功后才进入工作台。页面不应询问律所、firm ID 或角色；这些由数据库中预先绑定的合成身份决定。

如果遗失 TOTP 验证器，不要降级 MFA 或手改 token mapper。这是可销毁的合成测试环境；停止并重建其私有运行时身份数据，然后重新绑定 TOTP。
