use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::{ShellExt, process::CommandChild, process::CommandEvent};
use uuid::Uuid;

const LOCAL_API_PROTOCOL: &str = "lawcase-local-api-v1";

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
    persistence_phase: String,
}

struct LocalApiState {
    phase: String,
    message: String,
    api_base: Option<String>,
    process_id: Option<u32>,
    identity_phase: String,
    persistence_phase: String,
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
            persistence_phase: "UNKNOWN".to_string(),
            child: None,
        }
    }
}

#[derive(Clone, Default)]
struct LocalApiRuntime {
    inner: Arc<Mutex<LocalApiState>>,
}

#[derive(Deserialize)]
struct LocalApiReady {
    protocol: String,
    status: String,
    port: u16,
    pid: u32,
    challenge_sha256: String,
    identity: String,
    persistence: String,
}

fn snapshot_runtime(runtime: &LocalApiRuntime) -> DesktopRuntimeStatus {
    let state = runtime.inner.lock().expect("local API state lock poisoned");
    DesktopRuntimeStatus {
        phase: state.phase.clone(),
        message: state.message.clone(),
        api_base: state.api_base.clone(),
        process_id: state.process_id,
        identity_phase: state.identity_phase.clone(),
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
        state.persistence_phase = "UNAVAILABLE".to_string();
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
        || ready.identity != "NOT_ENROLLED"
        || ready.persistence != "NOT_CONFIGURED"
    {
        return Err("本机服务未通过父进程绑定核验。".to_string());
    }
    Ok(ready)
}

fn start_local_api(app: &AppHandle, runtime: LocalApiRuntime) -> Result<(), String> {
    let challenge = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
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
    });
    if child.write(format!("{}\n", handshake).as_bytes()).is_err() {
        let _ = child.kill();
        return Err("无法建立桌面父进程与本机服务的私有握手。".to_string());
    }
    {
        let mut state = runtime.inner.lock().expect("local API state lock poisoned");
        state.process_id = Some(process_id);
        state.child = Some(child);
    }

    tauri::async_runtime::spawn(async move {
        let mut ready_received = false;
        while let Some(event) = receiver.recv().await {
            match event {
                CommandEvent::Stdout(line) if !ready_received => {
                    match verify_ready_payload(&line, &challenge) {
                        Ok(ready) => {
                            let mut state =
                                runtime.inner.lock().expect("local API state lock poisoned");
                            state.phase = "READY".to_string();
                            state.message =
                                "本机受控服务已就绪；真实案件数据仍保持禁用。".to_string();
                            state.api_base = Some(format!("http://127.0.0.1:{}", ready.port));
                            state.process_id = Some(process_id);
                            state.identity_phase = ready.identity;
                            state.persistence_phase = ready.persistence;
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
                    state.persistence_phase = "UNAVAILABLE".to_string();
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
        state.persistence_phase = "UNAVAILABLE".to_string();
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
            if let Err(message) = start_local_api(app.handle(), runtime.clone()) {
                mark_runtime_blocked(&runtime, &message);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_runtime_status,
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
        LOCAL_API_PROTOCOL, validate_matter_id, validate_selected_root, verify_ready_payload,
    };
    use sha2::{Digest, Sha256};
    use std::path::Path;

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
            "{{\"protocol\":\"{}\",\"status\":\"READY\",\"port\":43127,\"pid\":77,\"challenge_sha256\":\"{}\",\"identity\":\"NOT_ENROLLED\",\"persistence\":\"NOT_CONFIGURED\"}}",
            LOCAL_API_PROTOCOL, digest
        );
        assert!(verify_ready_payload(payload.as_bytes(), &challenge).is_ok());
        assert!(verify_ready_payload(payload.as_bytes(), "b").is_err());
    }
}
