use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::{ShellExt, process::CommandChild, process::CommandEvent};
use uuid::Uuid;
use zeroize::Zeroizing;

const LOCAL_API_PROTOCOL: &str = "lawcase-local-api-v1";
const MAX_ENROLLMENT_PACKAGE_BYTES: u64 = 16_384;
const MAX_LOCAL_API_RESPONSE_BYTES: u64 = 65_536;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct SelectedCaseFolder {
    selected_root: String,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopRuntimeStatus {
    phase: String,
    message: String,
    api_base: Option<String>,
    process_id: Option<u32>,
    identity_phase: String,
    enrollment_trust_phase: String,
    session_phase: String,
    session_expires_at: Option<String>,
    persistence_phase: String,
}

struct LocalApiState {
    phase: String,
    message: String,
    api_base: Option<String>,
    process_id: Option<u32>,
    identity_phase: String,
    enrollment_trust_phase: String,
    session_phase: String,
    session_id: Option<String>,
    session_expires_at: Option<String>,
    desktop_access_token: Option<Zeroizing<String>>,
    persistence_phase: String,
    api_port: Option<u16>,
    parent_api_token: Option<Zeroizing<String>>,
    child: Option<CommandChild>,
}

impl Default for LocalApiState {
    fn default() -> Self {
        Self {
            phase: "STARTING".to_string(),
            message: "正在核验本机受控服务…".to_string(),
            api_base: None,
            process_id: None,
            identity_phase: "UNKNOWN".to_string(),
            enrollment_trust_phase: "UNKNOWN".to_string(),
            session_phase: "UNKNOWN".to_string(),
            session_id: None,
            session_expires_at: None,
            desktop_access_token: None,
            persistence_phase: "UNKNOWN".to_string(),
            api_port: None,
            parent_api_token: None,
            child: None,
        }
    }
}

#[derive(Clone, Default)]
struct LocalApiRuntime {
    inner: Arc<Mutex<LocalApiState>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LocalApiReady {
    protocol: String,
    status: String,
    port: u16,
    pid: u32,
    challenge_sha256: String,
    identity: String,
    enrollment_trust: String,
    persistence: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentVerificationResponse {
    status: String,
    envelope_sha256: String,
    enrollment_id: String,
    expires_at: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentActivationResponse {
    status: String,
    operation_id: String,
    enrollment_id: String,
    envelope_text: String,
    envelope_sha256: String,
    installation_binding_sha256: String,
    expires_at: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentRenewalResponse {
    status: String,
    operation_id: String,
    enrollment_id: String,
    envelope_text: String,
    envelope_sha256: String,
    expected_current_sha256: String,
    installation_binding_sha256: String,
    expires_at: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentRevocationResponse {
    status: String,
    operation_id: String,
    enrollment_id: String,
    expected_current_sha256: String,
    remote_revocation_confirmed: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentOperationStatusResponse {
    status: String,
    operation_id: String,
    operation_kind: String,
    enrollment_id: String,
    envelope_text: String,
    envelope_sha256: String,
    expected_current_sha256: Option<String>,
    installation_binding_sha256: String,
    expires_at: String,
    remote_revocation_confirmed: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DesktopSessionExchangeResponse {
    status: String,
    access_token: String,
    session_id: String,
    expires_at: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopSessionGrant {
    api_base: String,
    access_token: String,
    session_id: String,
    expires_at: String,
}

fn snapshot_runtime(runtime: &LocalApiRuntime) -> DesktopRuntimeStatus {
    let state = runtime.inner.lock().expect("local API state lock poisoned");
    DesktopRuntimeStatus {
        phase: state.phase.clone(),
        message: state.message.clone(),
        api_base: state.api_base.clone(),
        process_id: state.process_id,
        identity_phase: state.identity_phase.clone(),
        enrollment_trust_phase: state.enrollment_trust_phase.clone(),
        session_phase: state.session_phase.clone(),
        session_expires_at: state.session_expires_at.clone(),
        persistence_phase: state.persistence_phase.clone(),
    }
}

fn mark_runtime_blocked(runtime: &LocalApiRuntime, message: &str) {
    let child = {
        let mut state = runtime.inner.lock().expect("local API state lock poisoned");
        state.phase = "BLOCKED".to_string();
        state.message = message.to_string();
        state.api_base = None;
        state.process_id = None;
        state.identity_phase = "UNAVAILABLE".to_string();
        state.enrollment_trust_phase = "UNAVAILABLE".to_string();
        state.session_phase = "UNAVAILABLE".to_string();
        state.session_id = None;
        state.session_expires_at = None;
        state.desktop_access_token = None;
        state.persistence_phase = "UNAVAILABLE".to_string();
        state.api_port = None;
        state.parent_api_token = None;
        state.child.take()
    };
    if let Some(child) = child {
        let _ = child.kill();
    }
}

fn verify_ready_payload(payload: &[u8], challenge: &str) -> Result<LocalApiReady, String> {
    if payload.len() > 1024 {
        return Err("本机服务就绪回执过长。".to_string());
    }
    let ready: LocalApiReady =
        serde_json::from_slice(payload).map_err(|_| "本机服务就绪回执格式无效。".to_string())?;
    let expected_digest = format!("{:x}", Sha256::digest(challenge.as_bytes()));
    if ready.protocol != LOCAL_API_PROTOCOL
        || ready.status != "READY"
        || ready.pid <= 1
        || ready.port == 0
        || ready.challenge_sha256 != expected_digest
        || !matches!(
            ready.identity.as_str(),
            "NOT_ENROLLED" | "BLOCKED" | "ENROLLED"
        )
        || !matches!(
            ready.enrollment_trust.as_str(),
            "NOT_CONFIGURED" | "BLOCKED" | "READY"
        )
        || !matches!(ready.persistence.as_str(), "NOT_CONFIGURED" | "CONFIGURED")
    {
        return Err("本机服务未通过父进程绑定核验。".to_string());
    }
    Ok(ready)
}

fn start_local_api(app: &AppHandle, runtime: LocalApiRuntime) -> Result<(), String> {
    let challenge = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let parent_api_token = Zeroizing::new(format!(
        "{}{}",
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple()
    ));
    let (mut receiver, mut child) = app
        .shell()
        .sidecar("lawcase-local-api")
        .map_err(|_| "无法定位随应用分发的本机服务。".to_string())?
        .spawn()
        .map_err(|_| "无法启动随应用分发的本机服务。".to_string())?;
    let process_id = child.pid();
    let handshake = serde_json::json!({
        "protocol": LOCAL_API_PROTOCOL,
        "challenge": challenge,
        "parent_pid": std::process::id(),
        "parent_api_token": parent_api_token.as_str(),
    });
    if child.write(format!("{}\n", handshake).as_bytes()).is_err() {
        let _ = child.kill();
        return Err("无法建立桌面父进程与本机服务的私有握手。".to_string());
    }
    {
        let mut state = runtime.inner.lock().expect("local API state lock poisoned");
        state.process_id = Some(process_id);
        state.parent_api_token = Some(parent_api_token);
        state.child = Some(child);
    }

    tauri::async_runtime::spawn(async move {
        let mut ready_received = false;
        while let Some(event) = receiver.recv().await {
            match event {
                CommandEvent::Stdout(line) if !ready_received => {
                    match verify_ready_payload(&line, &challenge) {
                        Ok(ready) => {
                            let should_exchange_session = ready.identity == "ENROLLED";
                            let mut state =
                                runtime.inner.lock().expect("local API state lock poisoned");
                            state.phase = "READY".to_string();
                            state.message =
                                "本机受控服务已就绪；真实案件数据仍保持禁用。".to_string();
                            state.api_base = Some(format!("http://127.0.0.1:{}", ready.port));
                            state.process_id = Some(process_id);
                            state.identity_phase = ready.identity;
                            state.enrollment_trust_phase = ready.enrollment_trust;
                            state.session_phase = if should_exchange_session {
                                "STARTING".to_string()
                            } else {
                                "NOT_AVAILABLE".to_string()
                            };
                            state.persistence_phase = ready.persistence;
                            state.api_port = Some(ready.port);
                            drop(state);
                            if should_exchange_session {
                                let token = match parent_api_channel(&runtime) {
                                    Ok((_, token)) => token,
                                    Err(message) => {
                                        mark_runtime_blocked(&runtime, &message);
                                        break;
                                    }
                                };
                                match exchange_desktop_session_with_sidecar(
                                    ready.port,
                                    token.as_str(),
                                ) {
                                    Ok(grant) => {
                                        let mut state = runtime
                                            .inner
                                            .lock()
                                            .expect("local API state lock poisoned");
                                        state.session_phase = "READY".to_string();
                                        state.session_id = Some(grant.session_id);
                                        state.session_expires_at = Some(grant.expires_at);
                                        state.desktop_access_token =
                                            Some(Zeroizing::new(grant.access_token));
                                    }
                                    Err(message) => {
                                        mark_runtime_blocked(&runtime, &message);
                                        break;
                                    }
                                }
                            }
                            ready_received = true;
                        }
                        Err(message) => {
                            mark_runtime_blocked(&runtime, &message);
                            break;
                        }
                    }
                }
                CommandEvent::Error(_) => {
                    mark_runtime_blocked(&runtime, "本机服务进程通信失败，案件访问保持禁用。");
                    break;
                }
                CommandEvent::Terminated(_) => {
                    let mut state = runtime.inner.lock().expect("local API state lock poisoned");
                    if state.phase != "BLOCKED" {
                        state.phase = "STOPPED".to_string();
                        state.message = "本机受控服务已停止，案件访问保持禁用。".to_string();
                    }
                    state.api_base = None;
                    state.process_id = None;
                    state.identity_phase = "UNAVAILABLE".to_string();
                    state.enrollment_trust_phase = "UNAVAILABLE".to_string();
                    state.session_phase = "UNAVAILABLE".to_string();
                    state.session_id = None;
                    state.session_expires_at = None;
                    state.desktop_access_token = None;
                    state.persistence_phase = "UNAVAILABLE".to_string();
                    state.api_port = None;
                    state.parent_api_token = None;
                    state.child = None;
                    break;
                }
                _ => {}
            }
        }
    });
    Ok(())
}

fn stop_local_api(runtime: &LocalApiRuntime) {
    let child = {
        let mut state = runtime.inner.lock().expect("local API state lock poisoned");
        state.phase = "STOPPED".to_string();
        state.message = "桌面应用退出，本机受控服务已停止。".to_string();
        state.api_base = None;
        state.process_id = None;
        state.identity_phase = "UNAVAILABLE".to_string();
        state.enrollment_trust_phase = "UNAVAILABLE".to_string();
        state.session_phase = "UNAVAILABLE".to_string();
        state.session_id = None;
        state.session_expires_at = None;
        state.desktop_access_token = None;
        state.persistence_phase = "UNAVAILABLE".to_string();
        state.api_port = None;
        state.parent_api_token = None;
        state.child.take()
    };
    if let Some(child) = child {
        let _ = child.kill();
    }
}

#[tauri::command]
fn desktop_runtime_status(runtime: State<'_, LocalApiRuntime>) -> DesktopRuntimeStatus {
    snapshot_runtime(&runtime)
}

#[tauri::command]
fn desktop_session_grant(
    runtime: State<'_, LocalApiRuntime>,
) -> Result<DesktopSessionGrant, String> {
    snapshot_desktop_session_grant(&runtime)
}

fn snapshot_desktop_session_grant(
    runtime: &LocalApiRuntime,
) -> Result<DesktopSessionGrant, String> {
    let mut state = runtime
        .inner
        .lock()
        .map_err(|_| "本机会话状态锁定失败。".to_string())?;
    if state.phase != "READY"
        || state.session_phase != "READY"
        || state.persistence_phase != "CONFIGURED"
    {
        return Err("专用案件数据库和本机会话尚未同时就绪。".to_string());
    }
    let expires_at = state
        .session_expires_at
        .clone()
        .ok_or_else(|| "本机会话到期时间尚未就绪。".to_string())?;
    let parsed_expiry = DateTime::parse_from_rfc3339(&expires_at)
        .map_err(|_| "本机会话到期时间无效。".to_string())?
        .with_timezone(&Utc);
    let now = Utc::now();
    if parsed_expiry <= now || parsed_expiry > now + ChronoDuration::minutes(31) {
        state.session_phase = "EXPIRED".to_string();
        state.session_id = None;
        state.session_expires_at = None;
        state.desktop_access_token = None;
        return Err("本机会话已到期或有效期异常；请重新启动工作台。".to_string());
    }
    Ok(DesktopSessionGrant {
        api_base: state
            .api_base
            .clone()
            .ok_or_else(|| "本机案件 API 尚未就绪。".to_string())?,
        access_token: state
            .desktop_access_token
            .as_ref()
            .map(|value| value.to_string())
            .ok_or_else(|| "本机会话凭证尚未就绪。".to_string())?,
        session_id: state
            .session_id
            .clone()
            .ok_or_else(|| "本机会话标识尚未就绪。".to_string())?,
        expires_at,
    })
}

#[tauri::command]
fn desktop_enrollment_vault_status(vault: State<'_, EnrollmentVault>) -> EnrollmentVaultStatus {
    vault.status()
}

#[tauri::command]
fn desktop_model_provider_statuses(
    vault: State<'_, ModelProviderVault>,
) -> Result<Vec<ModelProviderStatus>, String> {
    vault.statuses()
}

#[tauri::command]
async fn configure_desktop_model_provider_key(
    app: AppHandle,
    provider_id: String,
    vault: State<'_, ModelProviderVault>,
) -> Result<ModelProviderStatus, String> {
    let provider = ModelProvider::parse(&provider_id)?;
    let receiver = native_model_api_key_prompt::schedule_model_api_key_prompt(&app, provider)?;
    let prompt_result = tauri::async_runtime::spawn_blocking(move || {
        receiver.recv_timeout(Duration::from_secs(300))
    })
    .await
    .map_err(|_| "原生 API Key 输入任务异常结束；未写入任何密钥。".to_string())?
    .map_err(|_| "原生 API Key 输入已超时；未写入任何密钥。".to_string())?;
    let key = prompt_result?.ok_or_else(|| "已取消 API Key 配置；未写入任何密钥。".to_string())?;
    vault.save_key(provider, key)
}

#[tauri::command]
async fn remove_desktop_model_provider_key(
    app: AppHandle,
    provider_id: String,
    vault: State<'_, ModelProviderVault>,
) -> Result<ModelProviderStatus, String> {
    let provider = ModelProvider::parse(&provider_id)?;
    let title = format!("移除 {} API Key", provider.display_name());
    let confirmation_app = app.clone();
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
        confirmation_app
            .dialog()
            .message("这会从本机 macOS Keychain 移除该服务商的 API Key。不会影响已生成的案卷、审计或外部请求记录。是否继续？")
            .title(title)
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancelCustom(
                "移除密钥".to_string(),
                "取消".to_string(),
            ))
            .blocking_show()
    })
    .await
    .map_err(|_| "无法显示原生移除确认框；未删除任何密钥。".to_string())?;
    if !confirmed {
        return Err("已取消移除 API Key；未删除任何密钥。".to_string());
    }
    vault.remove_key(provider)
}

#[tauri::command]
fn initialize_desktop_installation(
    vault: State<'_, EnrollmentVault>,
    confirmation: String,
) -> Result<EnrollmentVaultStatus, String> {
    vault.initialize_installation(&confirmation)
}

#[tauri::command]
fn disable_local_enrollment(
    vault: State<'_, EnrollmentVault>,
    confirmation: String,
) -> Result<EnrollmentVaultStatus, String> {
    vault.disable_local_enrollment(&confirmation)
}

#[tauri::command]
fn import_signed_enrollment_package(
    app: AppHandle,
    runtime: State<'_, LocalApiRuntime>,
    vault: State<'_, EnrollmentVault>,
) -> Result<EnrollmentVaultStatus, String> {
    let (port, token) = enrollment_verification_channel(&runtime)?;
    let selected = app
        .dialog()
        .file()
        .set_title("选择律所签名登记包")
        .add_filter("律所签名登记包", &["lawenroll"])
        .blocking_pick_file();
    let Some(selected) = selected else {
        return Err("已取消选择；未读取或写入任何登记凭证。".to_string());
    };
    let selected_path = selected
        .into_path()
        .map_err(|_| "所选登记包不是可读取的本机文件。".to_string())?;
    let metadata =
        fs::symlink_metadata(&selected_path).map_err(|_| "无法核验所选登记包。".to_string())?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.len() == 0
        || metadata.len() > MAX_ENROLLMENT_PACKAGE_BYTES
        || selected_path.extension().and_then(|value| value.to_str()) != Some("lawenroll")
    {
        return Err("登记包必须是 16 KB 以内的 .lawenroll 普通文件。".to_string());
    }
    let envelope_bytes =
        fs::read(&selected_path).map_err(|_| "无法读取所选登记包。".to_string())?;
    let envelope_text = String::from_utf8(envelope_bytes)
        .map_err(|_| "登记包不是有效 UTF-8 文本；未写入 Keychain。".to_string())?;
    let context = vault.verification_context()?;
    let verified = verify_enrollment_with_sidecar(
        port,
        token.as_str(),
        &envelope_text,
        &context.installation_binding_sha256,
    )?;
    let _ = Uuid::parse_str(&verified.enrollment_id)
        .map_err(|_| "本机服务返回的登记标识无效；未写入 Keychain。".to_string())?;
    if verified.status != "VERIFIED" || verified.expires_at.len() < 20 {
        return Err("本机服务未确认登记包有效；未写入 Keychain。".to_string());
    }
    let mut status = vault.commit_enrollment_after_verification(
        &envelope_text,
        &verified.envelope_sha256,
        &context.installation_binding_sha256,
        context.expected_current_sha256.as_deref(),
    )?;
    status.phase = "CREDENTIAL_SAVED_VERIFIED".to_string();
    status.message = "律所签名登记包已验签并保存；仍需连接案件数据库核验逐案权限。".to_string();
    Ok(status)
}

#[tauri::command]
async fn activate_desktop_enrollment(
    app: AppHandle,
    runtime: State<'_, LocalApiRuntime>,
    vault: State<'_, EnrollmentVault>,
) -> Result<EnrollmentVaultStatus, String> {
    let (port, token) = enrollment_verification_channel(&runtime)?;
    let context = vault.verification_context()?;
    if context.expected_current_sha256.is_some() {
        return Err("本机已有律所登记；不能用激活码覆盖，请使用续期或先由管理员撤销。".to_string());
    }
    let receiver = native_activation_prompt::schedule_activation_prompt(&app)?;
    let prompt_result = tauri::async_runtime::spawn_blocking(move || {
        receiver.recv_timeout(Duration::from_secs(300))
    })
    .await
    .map_err(|_| "原生激活码输入任务异常结束；未联系律所服务。".to_string())?
    .map_err(|_| "原生激活码输入已超时；未联系律所服务。".to_string())?;
    let activation_secret =
        prompt_result?.ok_or_else(|| "已取消激活；未联系律所服务或写入 Keychain。".to_string())?;
    let operation_id = Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
    vault.begin_remote_operation(&operation_id, "ACTIVATE", None, &created_at)?;
    let body = Zeroizing::new(
        serde_json::to_vec(&serde_json::json!({
            "activation_secret": activation_secret.as_str(),
            "operation_id": operation_id.as_str(),
        }))
        .map_err(|_| "无法构造本机激活请求；未联系律所服务。".to_string())?,
    );
    drop(activation_secret);
    let remote_result = tauri::async_runtime::spawn_blocking(move || {
        parent_lifecycle_request(
            port,
            token.as_str(),
            "/v1/desktop-enrollment/activate",
            body.as_slice(),
        )
    })
    .await
    .map_err(|_| "律所登记激活任务异常结束；远程结果待查询，不能重复提交。".to_string())?;
    let response = remote_result.map_err(|_| {
        "未取得确定的激活结果；操作号已安全保留，请使用“查询待决操作”。".to_string()
    })?;
    let activated: EnrollmentActivationResponse = serde_json::from_slice(&response)
        .map_err(|_| "律所登记激活回执内容无效；未写入 Keychain。".to_string())?;
    if activated.status != "REGISTERED"
        || activated.operation_id != operation_id
        || Uuid::parse_str(&activated.enrollment_id).is_err()
        || activated.installation_binding_sha256 != context.installation_binding_sha256
        || activated.envelope_text.is_empty()
        || activated.envelope_text.len() as u64 > MAX_ENROLLMENT_PACKAGE_BYTES
        || !valid_enrollment_expiry(&activated.expires_at)
    {
        return Err("律所登记激活回执与当前本机状态不一致；未写入 Keychain。".to_string());
    }
    let mut status = vault.commit_remote_enrollment_operation(
        &operation_id,
        "ACTIVATE",
        &activated.envelope_text,
        &activated.envelope_sha256,
        &activated.installation_binding_sha256,
        None,
    )?;
    status.phase = "CREDENTIAL_SAVED_VERIFIED".to_string();
    status.message = "律所签名登记已安全激活并保存；请重启桌面应用重新验签。".to_string();
    Ok(status)
}

#[tauri::command]
async fn renew_desktop_enrollment(
    runtime: State<'_, LocalApiRuntime>,
    vault: State<'_, EnrollmentVault>,
) -> Result<EnrollmentVaultStatus, String> {
    let (port, token) = enrollment_verification_channel(&runtime)?;
    let context = vault.verification_context()?;
    let expected_current = context
        .expected_current_sha256
        .as_deref()
        .ok_or_else(|| "当前没有可续期的律所登记。".to_string())?;
    let operation_id = Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
    vault.begin_remote_operation(&operation_id, "RENEW", Some(expected_current), &created_at)?;
    let body = serde_json::to_vec(&serde_json::json!({
        "operation_id": operation_id.as_str(),
    }))
    .map_err(|_| "无法构造续期请求；远程操作尚未提交。".to_string())?;
    let remote_result = tauri::async_runtime::spawn_blocking(move || {
        parent_lifecycle_request(port, token.as_str(), "/v1/desktop-enrollment/renew", &body)
    })
    .await
    .map_err(|_| "律所登记续期任务异常结束；远程结果待查询，不能重复提交。".to_string())?;
    let response = remote_result.map_err(|_| {
        "未取得确定的续期结果；操作号已安全保留，请使用“查询待决操作”。".to_string()
    })?;
    let renewed: EnrollmentRenewalResponse = serde_json::from_slice(&response)
        .map_err(|_| "律所登记续期回执内容无效；未写入 Keychain。".to_string())?;
    if renewed.status != "RENEWED"
        || renewed.operation_id != operation_id
        || Uuid::parse_str(&renewed.enrollment_id).is_err()
        || renewed.expected_current_sha256 != expected_current
        || renewed.installation_binding_sha256 != context.installation_binding_sha256
        || renewed.envelope_text.is_empty()
        || renewed.envelope_text.len() as u64 > MAX_ENROLLMENT_PACKAGE_BYTES
        || !valid_enrollment_expiry(&renewed.expires_at)
    {
        return Err("律所登记续期回执与当前本机状态不一致；未写入 Keychain。".to_string());
    }
    let mut status = vault.commit_remote_enrollment_operation(
        &operation_id,
        "RENEW",
        &renewed.envelope_text,
        &renewed.envelope_sha256,
        &renewed.installation_binding_sha256,
        Some(expected_current),
    )?;
    status.phase = "CREDENTIAL_SAVED_VERIFIED".to_string();
    status.message = "律所签名登记已续期并保存；请重启桌面应用重新验签。".to_string();
    Ok(status)
}

#[tauri::command]
async fn revoke_desktop_enrollment(
    app: AppHandle,
    runtime: State<'_, LocalApiRuntime>,
    vault: State<'_, EnrollmentVault>,
) -> Result<EnrollmentVaultStatus, String> {
    let confirmation_app = app.clone();
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
        confirmation_app
            .dialog()
            .message("这会联系律所服务端撤销当前律师登记，并立即停止本机案件会话。只有服务端确认后才删除本机凭证。是否继续？")
            .title("确认远程撤销律师登记")
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancelCustom(
                "确认远程撤销".to_string(),
                "取消".to_string(),
            ))
            .blocking_show()
    })
    .await
    .map_err(|_| "无法显示原生远程撤销确认框；未执行任何操作。".to_string())?;
    if !confirmed {
        return Err("未确认远程撤销；未联系律所服务或删除本机登记。".to_string());
    }
    let (port, token) = enrollment_verification_channel(&runtime)?;
    let context = vault.verification_context()?;
    let expected_current = context
        .expected_current_sha256
        .as_deref()
        .ok_or_else(|| "当前没有可撤销的律所登记。".to_string())?;
    let operation_id = Uuid::new_v4().to_string();
    let created_at = Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
    vault.begin_remote_operation(&operation_id, "REVOKE", Some(expected_current), &created_at)?;
    let body = serde_json::to_vec(&serde_json::json!({
        "confirmation": "CONFIRM_REMOTE_REVOCATION",
        "operation_id": operation_id.as_str(),
    }))
    .map_err(|_| "无法构造远程撤销请求。".to_string())?;
    let remote_result = tauri::async_runtime::spawn_blocking(move || {
        parent_lifecycle_request(port, token.as_str(), "/v1/desktop-enrollment/revoke", &body)
    })
    .await
    .map_err(|_| "律所远程撤销任务异常结束；远程结果待查询，不能重复提交。".to_string())?;
    let response = remote_result.map_err(|_| {
        "未取得确定的撤销结果；操作号已安全保留，请使用“查询待决操作”。".to_string()
    })?;
    let revoked: EnrollmentRevocationResponse = serde_json::from_slice(&response)
        .map_err(|_| "律所远程撤销回执内容无效；未删除本机登记。".to_string())?;
    if revoked.status != "REVOKED"
        || revoked.operation_id != operation_id
        || Uuid::parse_str(&revoked.enrollment_id).is_err()
        || revoked.expected_current_sha256 != expected_current
        || !revoked.remote_revocation_confirmed
    {
        return Err("律所远程撤销回执与当前本机登记不一致；未删除本机登记。".to_string());
    }
    let status = vault.commit_remote_revocation_operation(&operation_id, expected_current)?;
    mark_runtime_blocked(&runtime, "律所登记已远程撤销；本机会话已停止。");
    Ok(status)
}

#[tauri::command]
async fn resolve_pending_desktop_enrollment(
    runtime: State<'_, LocalApiRuntime>,
    vault: State<'_, EnrollmentVault>,
) -> Result<EnrollmentVaultStatus, String> {
    let pending = vault.pending_remote_operation()?;
    let (port, token) = enrollment_verification_channel(&runtime)?;
    let body = serde_json::to_vec(&serde_json::json!({
        "operation_id": pending.operation_id.as_str(),
        "operation_kind": pending.operation_kind.as_str(),
    }))
    .map_err(|_| "无法构造待决操作查询。".to_string())?;
    let response = tauri::async_runtime::spawn_blocking(move || {
        parent_lifecycle_request(port, token.as_str(), "/v1/desktop-enrollment/status", &body)
    })
    .await
    .map_err(|_| "待决操作查询任务异常结束；操作号仍安全保留。".to_string())??;
    let resolved: EnrollmentOperationStatusResponse = serde_json::from_slice(&response)
        .map_err(|_| "待决操作查询回执内容无效；本机状态未改变。".to_string())?;
    if resolved.operation_id != pending.operation_id
        || resolved.operation_kind != pending.operation_kind
        || resolved.expected_current_sha256 != pending.expected_current_sha256
        || resolved.installation_binding_sha256 != pending.installation_binding_sha256
        || !matches!(
            resolved.status.as_str(),
            "PENDING" | "REJECTED" | "SUCCEEDED"
        )
    {
        return Err("待决操作查询回执与 Keychain 标记不一致；本机状态未改变。".to_string());
    }
    if resolved.status == "PENDING" {
        if !resolved.enrollment_id.is_empty()
            || !resolved.envelope_text.is_empty()
            || !resolved.envelope_sha256.is_empty()
            || !resolved.expires_at.is_empty()
            || resolved.remote_revocation_confirmed
        {
            return Err("未完成的远程操作返回了结果材料；本机状态未改变。".to_string());
        }
        return Ok(vault.status());
    }
    if resolved.status == "REJECTED" {
        if !resolved.enrollment_id.is_empty()
            || !resolved.envelope_text.is_empty()
            || !resolved.envelope_sha256.is_empty()
            || !resolved.expires_at.is_empty()
            || resolved.remote_revocation_confirmed
        {
            return Err("已拒绝的远程操作返回了结果材料；本机状态未改变。".to_string());
        }
        return vault.clear_rejected_remote_operation(&pending.operation_id);
    }

    match pending.operation_kind.as_str() {
        "ACTIVATE" | "RENEW" => {
            if Uuid::parse_str(&resolved.enrollment_id).is_err()
                || resolved.envelope_text.is_empty()
                || resolved.envelope_text.len() as u64 > MAX_ENROLLMENT_PACKAGE_BYTES
                || resolved.remote_revocation_confirmed
                || !valid_enrollment_expiry(&resolved.expires_at)
            {
                return Err("远程登记恢复回执字段无效；本机状态未改变。".to_string());
            }
            let mut status = vault.commit_remote_enrollment_operation(
                &pending.operation_id,
                &pending.operation_kind,
                &resolved.envelope_text,
                &resolved.envelope_sha256,
                &resolved.installation_binding_sha256,
                pending.expected_current_sha256.as_deref(),
            )?;
            status.phase = "CREDENTIAL_SAVED_VERIFIED".to_string();
            status.message = if pending.operation_kind == "ACTIVATE" {
                "已确认远程激活成功并安全保存凭证；请重启桌面应用重新验签。".to_string()
            } else {
                "已确认远程续期成功并安全保存凭证；请重启桌面应用重新验签。".to_string()
            };
            Ok(status)
        }
        "REVOKE" => {
            let expected = pending
                .expected_current_sha256
                .as_deref()
                .ok_or_else(|| "待决撤销标记缺少原凭证哈希；本机状态未改变。".to_string())?;
            if Uuid::parse_str(&resolved.enrollment_id).is_err()
                || !resolved.envelope_text.is_empty()
                || !resolved.envelope_sha256.is_empty()
                || !resolved.expires_at.is_empty()
                || !resolved.remote_revocation_confirmed
            {
                return Err("远程撤销恢复回执字段无效；本机状态未改变。".to_string());
            }
            let status =
                vault.commit_remote_revocation_operation(&pending.operation_id, expected)?;
            mark_runtime_blocked(&runtime, "律所登记已远程撤销；本机会话已停止。");
            Ok(status)
        }
        _ => Err("待决操作类型无效；本机状态未改变。".to_string()),
    }
}

fn parent_lifecycle_request(
    port: u16,
    parent_api_token: &str,
    path: &str,
    body: &[u8],
) -> Result<Vec<u8>, String> {
    if parent_api_token.len() != 64
        || !parent_api_token
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        || !matches!(
            path,
            "/v1/desktop-enrollment/activate"
                | "/v1/desktop-enrollment/renew"
                | "/v1/desktop-enrollment/revoke"
                | "/v1/desktop-enrollment/status"
        )
        || body.len() > 1024
    {
        return Err("本机登记生命周期请求格式无效。".to_string());
    }
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let mut stream = TcpStream::connect_timeout(&address.into(), Duration::from_secs(3))
        .map_err(|_| "无法连接本机登记生命周期服务。".to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .map_err(|_| "无法限制登记生命周期读取时长。".to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "无法限制登记生命周期写入时长。".to_string())?;
    let content_type = if body.is_empty() {
        ""
    } else {
        "Content-Type: application/json\r\n"
    };
    let head = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {parent_api_token}\r\n{content_type}Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream
        .write_all(head.as_bytes())
        .and_then(|_| stream.write_all(body))
        .map_err(|_| "无法发送本机登记生命周期请求。".to_string())?;
    let mut response = Vec::new();
    stream
        .take(MAX_LOCAL_API_RESPONSE_BYTES + 1)
        .read_to_end(&mut response)
        .map_err(|_| "无法读取本机登记生命周期回执。".to_string())?;
    if response.len() as u64 > MAX_LOCAL_API_RESPONSE_BYTES {
        return Err("本机登记生命周期回执过长。".to_string());
    }
    let body = successful_http_body(&response, "律所登记生命周期操作未完成。")?;
    Ok(body.to_vec())
}

fn valid_enrollment_expiry(value: &str) -> bool {
    if !value.ends_with('Z') {
        return false;
    }
    let Ok(parsed) = DateTime::parse_from_rfc3339(value) else {
        return false;
    };
    let parsed = parsed.with_timezone(&Utc);
    let now = Utc::now();
    parsed > now && parsed <= now + ChronoDuration::days(31)
}

fn enrollment_verification_channel(
    runtime: &LocalApiRuntime,
) -> Result<(u16, Zeroizing<String>), String> {
    let state = runtime
        .inner
        .lock()
        .map_err(|_| "本机受控服务状态锁定失败；未开始登记。".to_string())?;
    if state.phase != "READY" || state.enrollment_trust_phase != "READY" {
        return Err("生产信任目录尚未通过核验；不能进行律所登记操作。".to_string());
    }
    let port = state
        .api_port
        .ok_or_else(|| "本机验签通道不可用；未开始登记。".to_string())?;
    let token = state
        .parent_api_token
        .as_ref()
        .ok_or_else(|| "本机验签通道未绑定桌面父进程。".to_string())?;
    Ok((port, Zeroizing::new(token.to_string())))
}

fn parent_api_channel(runtime: &LocalApiRuntime) -> Result<(u16, Zeroizing<String>), String> {
    let state = runtime
        .inner
        .lock()
        .map_err(|_| "本机受控服务状态锁定失败。".to_string())?;
    let port = state
        .api_port
        .ok_or_else(|| "本机父进程通道不可用。".to_string())?;
    let token = state
        .parent_api_token
        .as_ref()
        .ok_or_else(|| "本机父进程通道未绑定。".to_string())?;
    Ok((port, Zeroizing::new(token.to_string())))
}

fn exchange_desktop_session_with_sidecar(
    port: u16,
    parent_api_token: &str,
) -> Result<DesktopSessionExchangeResponse, String> {
    if parent_api_token.len() != 64
        || !parent_api_token
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return Err("本机会话引导凭证无效。".to_string());
    }
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let mut stream = TcpStream::connect_timeout(&address.into(), Duration::from_secs(3))
        .map_err(|_| "无法连接本机会话服务。".to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "无法限制本机会话读取时长。".to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "无法限制本机会话写入时长。".to_string())?;
    let request = format!(
        "POST /v1/desktop-sessions/exchange HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: tauri://localhost\r\nX-Desktop-Bootstrap: {parent_api_token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|_| "无法发送本机会话引导请求。".to_string())?;
    let mut response = Vec::new();
    stream
        .take(MAX_LOCAL_API_RESPONSE_BYTES + 1)
        .read_to_end(&mut response)
        .map_err(|_| "无法读取本机会话引导回执。".to_string())?;
    if response.len() as u64 > MAX_LOCAL_API_RESPONSE_BYTES {
        return Err("本机会话引导回执过长。".to_string());
    }
    let body = successful_http_body(&response, "本机会话引导未通过。")?;
    let grant: DesktopSessionExchangeResponse =
        serde_json::from_slice(body).map_err(|_| "本机会话引导回执内容无效。".to_string())?;
    if grant.status != "SESSION_READY"
        || Uuid::parse_str(&grant.session_id).is_err()
        || grant.expires_at.len() < 20
        || !grant.expires_at.ends_with('Z')
        || !(32..=160).contains(&grant.access_token.len())
        || !grant
            .access_token
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err("本机会话引导回执字段无效。".to_string());
    }
    Ok(grant)
}

fn verify_enrollment_with_sidecar(
    port: u16,
    parent_api_token: &str,
    envelope_text: &str,
    installation_binding_sha256: &str,
) -> Result<EnrollmentVerificationResponse, String> {
    if parent_api_token.len() != 64
        || installation_binding_sha256.len() != 64
        || envelope_text.is_empty()
        || envelope_text.len() as u64 > MAX_ENROLLMENT_PACKAGE_BYTES
    {
        return Err("本机验签请求格式无效；未写入 Keychain。".to_string());
    }
    let body = serde_json::to_vec(&serde_json::json!({
        "envelope_text": envelope_text,
        "installation_binding_sha256": installation_binding_sha256,
    }))
    .map_err(|_| "无法构造本机验签请求。".to_string())?;
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let mut stream = TcpStream::connect_timeout(&address.into(), Duration::from_secs(3))
        .map_err(|_| "无法连接本机验签服务；未写入 Keychain。".to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "无法限制本机验签读取时长。".to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "无法限制本机验签写入时长。".to_string())?;
    let head = format!(
        "POST /v1/desktop-enrollment/verify HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {parent_api_token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream
        .write_all(head.as_bytes())
        .and_then(|_| stream.write_all(&body))
        .map_err(|_| "无法发送本机验签请求；未写入 Keychain。".to_string())?;
    let mut response = Vec::new();
    stream
        .take(MAX_LOCAL_API_RESPONSE_BYTES + 1)
        .read_to_end(&mut response)
        .map_err(|_| "无法读取本机验签回执；未写入 Keychain。".to_string())?;
    if response.len() as u64 > MAX_LOCAL_API_RESPONSE_BYTES {
        return Err("本机验签回执过长；未写入 Keychain。".to_string());
    }
    parse_enrollment_verification_response(&response)
}

fn parse_enrollment_verification_response(
    response: &[u8],
) -> Result<EnrollmentVerificationResponse, String> {
    let body = successful_http_body(response, "律所登记包未通过本机受信验签；未写入 Keychain。")?;
    serde_json::from_slice(body).map_err(|_| "本机验签回执内容无效；未写入 Keychain。".to_string())
}

fn successful_http_body<'a>(response: &'a [u8], failure: &str) -> Result<&'a [u8], String> {
    let boundary = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "本机验签回执格式无效；未写入 Keychain。".to_string())?;
    let header = std::str::from_utf8(&response[..boundary])
        .map_err(|_| "本机验签回执头无效。".to_string())?;
    if !header.starts_with("HTTP/1.1 200 ") || header.lines().any(|line| line.contains('\0')) {
        return Err(failure.to_string());
    }
    Ok(&response[boundary + 4..])
}

fn validate_matter_id(matter_id: &str) -> Result<(), String> {
    Uuid::parse_str(matter_id)
        .map(|_| ())
        .map_err(|_| "案件标识无效，未打开本机文件夹选择器。".to_string())
}

fn validate_selected_root(selected_root: &Path, home_root: &Path) -> Result<(), String> {
    if selected_root.parent().is_none() {
        return Err("不能将文件系统根目录授权为案卷范围。".to_string());
    }
    if selected_root == home_root {
        return Err("不能将整个用户主目录授权为案卷范围。".to_string());
    }
    Ok(())
}

#[tauri::command]
async fn select_case_folder(
    app: AppHandle,
    matter_id: String,
) -> Result<Option<SelectedCaseFolder>, String> {
    validate_matter_id(&matter_id)?;

    let selected = app
        .dialog()
        .file()
        .set_title("选择本案案卷文件夹")
        .set_can_create_directories(false)
        .blocking_pick_folder();
    let Some(selected) = selected else {
        return Ok(None);
    };

    let selected_path = selected
        .into_path()
        .map_err(|_| "所选位置不是可读取的本机文件夹。".to_string())?;
    let selected_root = selected_path
        .canonicalize()
        .map_err(|_| "无法核验所选文件夹的真实位置。".to_string())?;
    if !selected_root.is_dir() {
        return Err("所选位置不是文件夹。".to_string());
    }

    let home_root = app
        .path()
        .home_dir()
        .map_err(|_| "无法确认本机用户目录边界。".to_string())?
        .canonicalize()
        .map_err(|_| "无法核验本机用户目录边界。".to_string())?;
    validate_selected_root(&selected_root, &home_root)?;

    let selected_root = selected_root
        .into_os_string()
        .into_string()
        .map_err(|_| "所选文件夹名称包含当前版本无法安全处理的字符。".to_string())?;

    Ok(Some(SelectedCaseFolder { selected_root }))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let runtime = LocalApiRuntime::default();
            app.manage(runtime.clone());
            app.manage(EnrollmentVault::default());
            if let Err(message) = start_local_api(app.handle(), runtime.clone()) {
                mark_runtime_blocked(&runtime, &message);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_runtime_status,
            desktop_session_grant,
            desktop_enrollment_vault_status,
            desktop_model_provider_statuses,
            configure_desktop_model_provider_key,
            remove_desktop_model_provider_key,
            initialize_desktop_installation,
            import_signed_enrollment_package,
            activate_desktop_enrollment,
            renew_desktop_enrollment,
            revoke_desktop_enrollment,
            resolve_pending_desktop_enrollment,
            disable_local_enrollment,
            select_case_folder
        ])
        .build(tauri::generate_context!())
        .expect("桌面应用启动失败");
    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
            stop_local_api(&app_handle.state::<LocalApiRuntime>());
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        EnrollmentActivationResponse, EnrollmentOperationStatusResponse, EnrollmentRenewalResponse,
        EnrollmentRevocationResponse, LOCAL_API_PROTOCOL, LocalApiRuntime,
        enrollment_verification_channel, exchange_desktop_session_with_sidecar,
        parent_lifecycle_request, parse_enrollment_verification_response,
        snapshot_desktop_session_grant, validate_matter_id, validate_selected_root,
        verify_ready_payload,
    };
    use chrono::{Duration as ChronoDuration, Utc};
    use sha2::{Digest, Sha256};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::Path;
    use std::thread;

    #[test]
    fn accepts_uuid_matter_id() {
        assert!(validate_matter_id("6b37b52e-7749-4ef1-a817-f4b37c74ab59").is_ok());
    }

    #[test]
    fn rejects_non_uuid_matter_id() {
        assert!(validate_matter_id("alpha_matter_001").is_err());
    }

    #[test]
    fn rejects_filesystem_root() {
        assert!(validate_selected_root(Path::new("/"), Path::new("/Users/example")).is_err());
    }

    #[test]
    fn rejects_home_root() {
        assert!(
            validate_selected_root(Path::new("/Users/example"), Path::new("/Users/example"))
                .is_err()
        );
    }

    #[test]
    fn accepts_nested_case_folder() {
        assert!(
            validate_selected_root(
                Path::new("/Users/example/Cases/Matter-A"),
                Path::new("/Users/example")
            )
            .is_ok()
        );
    }

    #[test]
    fn verifies_sidecar_pid_protocol_port_and_parent_challenge() {
        let challenge = "a".repeat(64);
        let digest = format!("{:x}", Sha256::digest(challenge.as_bytes()));
        let payload = format!(
            "{{\"protocol\":\"{}\",\"status\":\"READY\",\"port\":43127,\"pid\":77,\"challenge_sha256\":\"{}\",\"identity\":\"NOT_ENROLLED\",\"enrollment_trust\":\"NOT_CONFIGURED\",\"persistence\":\"NOT_CONFIGURED\"}}",
            LOCAL_API_PROTOCOL, digest
        );
        assert!(verify_ready_payload(payload.as_bytes(), &challenge).is_ok());
        let enrolled =
            payload.replace("\"identity\":\"NOT_ENROLLED\"", "\"identity\":\"ENROLLED\"");
        assert!(verify_ready_payload(enrolled.as_bytes(), &challenge).is_ok());
        assert!(verify_ready_payload(payload.as_bytes(), "b").is_err());
    }

    #[test]
    fn ready_payload_rejects_unknown_fields_and_invalid_trust_phase() {
        let challenge = "a".repeat(64);
        let digest = format!("{:x}", Sha256::digest(challenge.as_bytes()));
        let extra = format!(
            "{{\"protocol\":\"{}\",\"status\":\"READY\",\"port\":43127,\"pid\":77,\"challenge_sha256\":\"{}\",\"identity\":\"NOT_ENROLLED\",\"enrollment_trust\":\"READY\",\"persistence\":\"NOT_CONFIGURED\",\"role\":\"ADMIN\"}}",
            LOCAL_API_PROTOCOL, digest
        );
        let invalid = extra.replace(",\"role\":\"ADMIN\"", "").replace(
            "\"enrollment_trust\":\"READY\"",
            "\"enrollment_trust\":\"BYPASS\"",
        );
        assert!(verify_ready_payload(extra.as_bytes(), &challenge).is_err());
        assert!(verify_ready_payload(invalid.as_bytes(), &challenge).is_err());
    }

    #[test]
    fn native_verification_channel_requires_ready_trust_and_keeps_token_private() {
        let runtime = LocalApiRuntime::default();
        assert!(enrollment_verification_channel(&runtime).is_err());
        {
            let mut state = runtime.inner.lock().unwrap();
            state.phase = "READY".to_string();
            state.enrollment_trust_phase = "READY".to_string();
            state.api_port = Some(43127);
            state.parent_api_token = Some(zeroize::Zeroizing::new("c".repeat(64)));
        }
        let (port, token) = enrollment_verification_channel(&runtime).unwrap();
        assert_eq!(port, 43127);
        assert_eq!(token.as_str(), "c".repeat(64));
        let snapshot = super::snapshot_runtime(&runtime);
        let serialized = serde_json::to_string(&snapshot).unwrap();
        assert!(!serialized.contains(&"c".repeat(64)));
    }

    #[test]
    fn desktop_session_grant_requires_database_and_never_enters_runtime_status() {
        let runtime = LocalApiRuntime::default();
        {
            let mut state = runtime.inner.lock().unwrap();
            state.phase = "READY".to_string();
            state.session_phase = "READY".to_string();
            state.persistence_phase = "NOT_CONFIGURED".to_string();
            state.api_base = Some("http://127.0.0.1:43127".to_string());
            state.session_id = Some("11111111-1111-4111-8111-111111111111".to_string());
            state.session_expires_at = Some(
                (Utc::now() + ChronoDuration::minutes(20))
                    .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            );
            state.desktop_access_token = Some(zeroize::Zeroizing::new("s".repeat(64)));
        }
        assert!(snapshot_desktop_session_grant(&runtime).is_err());
        {
            runtime.inner.lock().unwrap().persistence_phase = "CONFIGURED".to_string();
        }
        let grant = snapshot_desktop_session_grant(&runtime).unwrap();
        assert_eq!(grant.access_token, "s".repeat(64));
        let status = serde_json::to_string(&super::snapshot_runtime(&runtime)).unwrap();
        assert!(!status.contains(&"s".repeat(64)));
        assert!(!status.contains("sessionId"));
    }

    #[test]
    fn expired_session_is_zeroized_before_webview_grant() {
        let runtime = LocalApiRuntime::default();
        {
            let mut state = runtime.inner.lock().unwrap();
            state.phase = "READY".to_string();
            state.session_phase = "READY".to_string();
            state.persistence_phase = "CONFIGURED".to_string();
            state.api_base = Some("http://127.0.0.1:43127".to_string());
            state.session_id = Some("11111111-1111-4111-8111-111111111111".to_string());
            state.session_expires_at = Some(
                (Utc::now() - ChronoDuration::seconds(1))
                    .to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            );
            state.desktop_access_token = Some(zeroize::Zeroizing::new("s".repeat(64)));
        }
        assert!(snapshot_desktop_session_grant(&runtime).is_err());
        let state = runtime.inner.lock().unwrap();
        assert_eq!(state.session_phase, "EXPIRED");
        assert!(state.session_id.is_none());
        assert!(state.session_expires_at.is_none());
        assert!(state.desktop_access_token.is_none());
    }

    #[test]
    fn native_parent_exchanges_session_over_numeric_loopback() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request_bytes = Vec::new();
            let mut chunk = [0_u8; 512];
            loop {
                let size = stream.read(&mut chunk).unwrap();
                assert!(size > 0);
                request_bytes.extend_from_slice(&chunk[..size]);
                let Some(header_end) = request_bytes
                    .windows(4)
                    .position(|window| window == b"\r\n\r\n")
                    .map(|position| position + 4)
                else {
                    continue;
                };
                let head = std::str::from_utf8(&request_bytes[..header_end]).unwrap();
                let content_length = head
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("Content-Length: ")
                            .and_then(|value| value.parse::<usize>().ok())
                    })
                    .unwrap();
                if request_bytes.len() >= header_end + content_length {
                    break;
                }
            }
            let request = std::str::from_utf8(&request_bytes).unwrap();
            assert!(request.starts_with("POST /v1/desktop-sessions/exchange HTTP/1.1\r\n"));
            assert!(request.contains("Origin: tauri://localhost\r\n"));
            assert!(request.contains(&format!("X-Desktop-Bootstrap: {}\r\n", "d".repeat(64))));
            let body = format!(
                "{{\"status\":\"SESSION_READY\",\"access_token\":\"{}\",\"session_id\":\"11111111-1111-4111-8111-111111111111\",\"expires_at\":\"2026-08-10T12:30:00Z\"}}",
                "s".repeat(64)
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        let grant = exchange_desktop_session_with_sidecar(port, &"d".repeat(64)).unwrap();
        server.join().unwrap();
        assert_eq!(grant.status, "SESSION_READY");
        assert_eq!(grant.access_token, "s".repeat(64));
    }

    #[test]
    fn native_parent_lifecycle_request_is_token_bound_and_strictly_parsed() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request_bytes = Vec::new();
            let mut chunk = [0_u8; 512];
            loop {
                let size = stream.read(&mut chunk).unwrap();
                assert!(size > 0);
                request_bytes.extend_from_slice(&chunk[..size]);
                let Some(header_end) = request_bytes
                    .windows(4)
                    .position(|window| window == b"\r\n\r\n")
                    .map(|position| position + 4)
                else {
                    continue;
                };
                let head = std::str::from_utf8(&request_bytes[..header_end]).unwrap();
                let content_length = head
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("Content-Length: ")
                            .and_then(|value| value.parse::<usize>().ok())
                    })
                    .unwrap();
                if request_bytes.len() >= header_end + content_length {
                    break;
                }
            }
            let request = std::str::from_utf8(&request_bytes).unwrap();
            assert!(request.starts_with("POST /v1/desktop-enrollment/renew HTTP/1.1\r\n"));
            assert!(request.contains(&format!("Authorization: Bearer {}\r\n", "d".repeat(64))));
            assert!(
                request.ends_with("{\"operation_id\":\"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\"}")
            );
            let body = serde_json::json!({
                "status": "RENEWED",
                "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "enrollment_id": "11111111-1111-4111-8111-111111111111",
                "envelope_text": "signed-envelope",
                "envelope_sha256": "a".repeat(64),
                "expected_current_sha256": "b".repeat(64),
                "installation_binding_sha256": "c".repeat(64),
                "expires_at": "2026-08-20T12:00:00Z"
            })
            .to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        let renewal_request = serde_json::to_vec(&serde_json::json!({
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        }))
        .unwrap();
        let body = parent_lifecycle_request(
            port,
            &"d".repeat(64),
            "/v1/desktop-enrollment/renew",
            &renewal_request,
        )
        .unwrap();
        let response: EnrollmentRenewalResponse = serde_json::from_slice(&body).unwrap();
        assert_eq!(response.status, "RENEWED");
        server.join().unwrap();

        let extra = br#"{"status":"REVOKED","operation_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","enrollment_id":"11111111-1111-4111-8111-111111111111","expected_current_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","remote_revocation_confirmed":true,"role":"ADMIN"}"#;
        assert!(serde_json::from_slice::<EnrollmentRevocationResponse>(extra).is_err());

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request_bytes = Vec::new();
            let mut chunk = [0_u8; 512];
            loop {
                let size = stream.read(&mut chunk).unwrap();
                assert!(size > 0);
                request_bytes.extend_from_slice(&chunk[..size]);
                let Some(header_end) = request_bytes
                    .windows(4)
                    .position(|window| window == b"\r\n\r\n")
                    .map(|position| position + 4)
                else {
                    continue;
                };
                let head = std::str::from_utf8(&request_bytes[..header_end]).unwrap();
                let content_length = head
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("Content-Length: ")
                            .and_then(|value| value.parse::<usize>().ok())
                    })
                    .unwrap();
                if request_bytes.len() >= header_end + content_length {
                    break;
                }
            }
            let request = std::str::from_utf8(&request_bytes).unwrap();
            assert!(request.starts_with("POST /v1/desktop-enrollment/activate HTTP/1.1\r\n"));
            assert!(request.contains(&format!("Authorization: Bearer {}\r\n", "d".repeat(64))));
            assert!(
                request.ends_with(&format!(
                    "{{\"activation_secret\":\"{}\",\"operation_id\":\"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\"}}",
                    "A".repeat(32)
                ))
            );
            let body = serde_json::json!({
                "status": "REGISTERED",
                "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "enrollment_id": "11111111-1111-4111-8111-111111111111",
                "envelope_text": "signed-envelope",
                "envelope_sha256": "a".repeat(64),
                "installation_binding_sha256": "c".repeat(64),
                "expires_at": "2026-08-20T12:00:00Z"
            })
            .to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        let request_body = serde_json::to_vec(&serde_json::json!({
            "activation_secret": "A".repeat(32),
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        }))
        .unwrap();
        let body = parent_lifecycle_request(
            port,
            &"d".repeat(64),
            "/v1/desktop-enrollment/activate",
            &request_body,
        )
        .unwrap();
        let response: EnrollmentActivationResponse = serde_json::from_slice(&body).unwrap();
        assert_eq!(response.status, "REGISTERED");
        server.join().unwrap();
    }

    #[test]
    fn native_verification_response_parser_rejects_status_and_extra_fields() {
        let valid = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"status\":\"VERIFIED\",\"envelope_sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"enrollment_id\":\"11111111-1111-4111-8111-111111111111\",\"expires_at\":\"2026-09-01T00:00:00Z\"}";
        assert!(parse_enrollment_verification_response(valid.as_bytes()).is_ok());
        let denied = valid.replacen("200", "422", 1);
        assert!(parse_enrollment_verification_response(denied.as_bytes()).is_err());
        let extra = valid.replacen("}", ",\"role\":\"ADMIN\"}", 1);
        assert!(parse_enrollment_verification_response(extra.as_bytes()).is_err());
    }

    #[test]
    fn operation_status_response_rejects_unknown_or_missing_fields() {
        let valid = serde_json::json!({
            "status": "PENDING",
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "operation_kind": "RENEW",
            "enrollment_id": "",
            "envelope_text": "",
            "envelope_sha256": "",
            "expected_current_sha256": "b".repeat(64),
            "installation_binding_sha256": "c".repeat(64),
            "expires_at": "",
            "remote_revocation_confirmed": false,
        });
        assert!(serde_json::from_value::<EnrollmentOperationStatusResponse>(valid.clone()).is_ok());
        let mut extra = valid.clone();
        extra["role"] = serde_json::Value::String("ADMIN".to_string());
        assert!(serde_json::from_value::<EnrollmentOperationStatusResponse>(extra).is_err());
        let mut missing = valid;
        missing.as_object_mut().unwrap().remove("operation_kind");
        assert!(serde_json::from_value::<EnrollmentOperationStatusResponse>(missing).is_err());
    }
}
mod enrollment_vault;
mod model_provider_vault;
mod native_activation_prompt;
mod native_model_api_key_prompt;

use enrollment_vault::{EnrollmentVault, EnrollmentVaultStatus};
use model_provider_vault::{ModelProvider, ModelProviderStatus, ModelProviderVault};
