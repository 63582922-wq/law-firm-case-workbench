# ADR-0030：受控 Web 会话不继承上游令牌的短到期

状态：已接受（本机受管 Alpha 实现）

## 背景

本机受管 Alpha 通过 OIDC authorization-code + PKCE 和 MFA 验证身份后，浏览器仅持有 `Secure`、`HttpOnly`、同源的 opaque session cookie。服务器保存会话与 CSRF 随机值的摘要，并在每次请求时复核会话撤销状态及当前用户目录中的律所、角色和活动状态。

此前 `WebSessionAuthority` 取 `min(上游 ID token 到期, 一小时会话上限)` 作为 opaque session 到期。受管 Keycloak 为例行令牌轮换配置十分钟令牌；这使每个已完成 MFA 的浏览器会话也在十分钟后失效。真实验收需要上传、处理混合材料、人工处理冲突和查看成果，十分钟不足以完成一条正常路径，并诱发重复登录。

## 决策

只要 OIDC ID token 在签发时仍有效、经签名/issuer/audience/nonce/MFA/`auth_time` 全部验证，并且用户目录中的人类账号和角色有效，服务器签发的 opaque Web session 一律采用 `WebSessionPolicy.max_session_lifetime`（当前固定十二小时，即一个工作日）。它不保存、下发或续用上游 JWT，也不接受浏览器携带 bearer token。

会话的强制边界保持不变：

- 最大寿命为十二小时，不能由浏览器选择或延长；
- 签发时仍拒绝已过期令牌、非 MFA 身份、过期 MFA 或非人类执行身份；
- 每次请求仍校验 cookie 摘要、过期、显式撤销、同源/CSRF（写操作）和当前身份目录；
- 登出或服务器端撤销立即失效；用户被停用、跨律所或角色映射失效时请求 fail closed；
- 不增加刷新令牌持久化，不把 OIDC token 暴露给浏览器，也不把本机 Alpha 的十二小时策略宣称为生产 SSO/注销联动方案。

## 后果

- 好处：短寿命 OIDC token 可以继续用于安全的登录交换，律师的已验证浏览器会话仍有明确、十二小时、可撤销的工作窗口；混合案卷验收不再因例行令牌到期反复要求 MFA。
- 代价：上游身份提供方在签发后发生的会话级注销不会单独缩短该十二小时窗口；当前环境以应用撤销和每请求目录复核降低该窗口，生产接入仍需补充 IdP 事件/会话注销联动的放行证据。

## 被拒绝的方案

- 将 Keycloak 的短令牌寿命直接拉长：拒绝作为应用修复，因为它改变的是身份提供方令牌轮换策略，且需要重新装载本机 realm 和用户 MFA 状态。
- 在浏览器保存或每次 API 请求附带 OIDC JWT：拒绝，因为扩大了令牌暴露面，并违背同源 opaque-session 边界。
- 无上限地续期 opaque cookie：拒绝，因为会削弱可审计的认证窗口和撤销控制。
