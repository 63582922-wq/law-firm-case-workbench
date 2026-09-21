use keyring::{Entry, Error as KeyringError};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use uuid::Uuid;
use zeroize::Zeroizing;

const SERVICE: &str = "cn.lawcase.workbench.model-provider";
const DEEPSEEK_ACCOUNT: &str = "deepseek-api-key-v1";
const QWEN_ACCOUNT: &str = "qwen-api-key-v1";
const QWEN_CONNECTION_ACCOUNT: &str = "qwen-connection-v1";
const STATUS_MARKER_VERSION: u8 = 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ModelProvider {
    DeepSeek,
    Qwen,
}

impl ModelProvider {
    pub(crate) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "deepseek" => Ok(Self::DeepSeek),
            "qwen" => Ok(Self::Qwen),
            _ => Err("不支持的模型服务商；未读取或写入任何密钥。".to_string()),
        }
    }

    fn account(self) -> &'static str {
        match self {
            Self::DeepSeek => DEEPSEEK_ACCOUNT,
            Self::Qwen => QWEN_ACCOUNT,
        }
    }

    fn connection_account(self) -> Option<&'static str> {
        match self {
            Self::DeepSeek => None,
            Self::Qwen => Some(QWEN_CONNECTION_ACCOUNT),
        }
    }

    pub(crate) fn display_name(self) -> &'static str {
        match self {
            Self::DeepSeek => "DeepSeek（文本与推理）",
            Self::Qwen => "通义千问百炼 Qwen3.5-OCR（视觉、OCR 与版面解析）",
        }
    }

    fn model_id(self) -> &'static str {
        match self {
            Self::DeepSeek => "deepseek-v4-pro",
            Self::Qwen => "qwen3.5-ocr",
        }
    }

    fn id(self) -> &'static str {
        match self {
            Self::DeepSeek => "deepseek",
            Self::Qwen => "qwen",
        }
    }
}

/// This is deliberately a non-secret state indicator, not a Keychain probe.
/// `configured` means only that this application has a local configuration
/// record; it never means the Keychain credential was read during this status
/// request.  The credential is validated only inside an approved native call.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ModelProviderStatus {
    provider_id: String,
    display_name: String,
    model_id: String,
    configured: bool,
    configuration_state: String,
    connection_ready: bool,
    connection_label: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct QwenConnection {
    region_id: String,
    workspace_id: String,
}

/// Short-lived credentials passed only to the native model transport.
///
/// The API key is deliberately kept out of the webview, desktop bridge and
/// status objects. It is read from Keychain only immediately before a bounded,
/// lawyer-authorised request and is zeroized when the transport drops it.
pub(crate) struct QwenOcrCredentials {
    pub(crate) api_key: Zeroizing<String>,
    pub(crate) region_id: String,
    pub(crate) workspace_id: String,
}

/// Short-lived DeepSeek credentials for the native text/planning transport.
/// The fixed provider endpoint and model selection live in that transport;
/// neither is caller-controlled by the WebView.
pub(crate) struct DeepSeekPlannerCredentials {
    pub(crate) api_key: Zeroizing<String>,
}

trait CredentialStore: Send + Sync {
    fn get(&self, account: &str) -> Result<Option<String>, ()>;
    fn set(&self, account: &str, value: &str) -> Result<(), ()>;
    fn delete(&self, account: &str) -> Result<(), ()>;
}

struct NativeKeyringStore;

impl NativeKeyringStore {
    fn entry(account: &str) -> Result<Entry, ()> {
        Entry::new(SERVICE, account).map_err(|_| ())
    }
}

impl CredentialStore for NativeKeyringStore {
    fn get(&self, account: &str) -> Result<Option<String>, ()> {
        match Self::entry(account)?.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(KeyringError::NoEntry) => Ok(None),
            Err(_) => Err(()),
        }
    }

    fn set(&self, account: &str, value: &str) -> Result<(), ()> {
        Self::entry(account)?.set_password(value).map_err(|_| ())
    }

    fn delete(&self, account: &str) -> Result<(), ()> {
        match Self::entry(account)?.delete_credential() {
            Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
            Err(_) => Err(()),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CredentialState {
    /// No Keychain read has been made in this process and there is no local
    /// non-secret record from a prior explicit configuration action.
    NotChecked,
    /// A user explicitly saved a key through this application. This is not a
    /// claim that the Keychain entry still exists.
    ConfigurationRecorded,
    /// An approved model invocation read and shape-validated the key in this
    /// process. The state is intentionally downgraded to ConfigurationRecorded
    /// after restart.
    ValidatedForCurrentSession,
    /// The user explicitly removed the key, or a confirmed invocation found no
    /// valid entry. This state is a non-secret local record.
    NotConfigured,
}

impl CredentialState {
    fn configuration_state(self) -> &'static str {
        match self {
            Self::NotChecked => "NOT_CHECKED",
            Self::ConfigurationRecorded => "CONFIGURATION_RECORDED",
            Self::ValidatedForCurrentSession => "VALIDATED_FOR_CURRENT_SESSION",
            Self::NotConfigured => "NOT_CONFIGURED",
        }
    }

    fn has_configuration_record(self) -> bool {
        matches!(
            self,
            Self::ConfigurationRecorded | Self::ValidatedForCurrentSession
        )
    }

    fn marker_value(self) -> Option<bool> {
        match self {
            Self::NotChecked => None,
            Self::ConfigurationRecorded | Self::ValidatedForCurrentSession => Some(true),
            Self::NotConfigured => Some(false),
        }
    }

    fn from_marker(value: Option<bool>) -> Self {
        match value {
            Some(true) => Self::ConfigurationRecorded,
            Some(false) => Self::NotConfigured,
            None => Self::NotChecked,
        }
    }
}

#[derive(Clone)]
struct ProviderStatusCache {
    deepseek_key: CredentialState,
    qwen_key: CredentialState,
    qwen_connection_region: Option<String>,
}

impl Default for ProviderStatusCache {
    fn default() -> Self {
        Self {
            deepseek_key: CredentialState::NotChecked,
            qwen_key: CredentialState::NotChecked,
            qwen_connection_region: None,
        }
    }
}

impl ProviderStatusCache {
    fn key_state(&self, provider: ModelProvider) -> CredentialState {
        match provider {
            ModelProvider::DeepSeek => self.deepseek_key,
            ModelProvider::Qwen => self.qwen_key,
        }
    }

    fn set_key_state(&mut self, provider: ModelProvider, state: CredentialState) {
        match provider {
            ModelProvider::DeepSeek => self.deepseek_key = state,
            ModelProvider::Qwen => self.qwen_key = state,
        }
    }

    fn to_marker(&self) -> ModelProviderStatusMarker {
        ModelProviderStatusMarker {
            version: STATUS_MARKER_VERSION,
            deepseek_key_recorded: self.deepseek_key.marker_value(),
            qwen_key_recorded: self.qwen_key.marker_value(),
            qwen_connection_region: self.qwen_connection_region.clone(),
        }
    }
}

/// This file contains no secret, workspace identifier, API key, prompt, case
/// material or Keychain reference. It exists solely to keep opening Settings
/// from querying macOS Keychain.
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ModelProviderStatusMarker {
    version: u8,
    deepseek_key_recorded: Option<bool>,
    qwen_key_recorded: Option<bool>,
    qwen_connection_region: Option<String>,
}

pub(crate) struct ModelProviderVault {
    store: Arc<dyn CredentialStore>,
    operation_lock: Mutex<()>,
    status_cache: Mutex<ProviderStatusCache>,
    status_marker_path: Option<PathBuf>,
}

impl Default for ModelProviderVault {
    fn default() -> Self {
        Self::with_parts(Arc::new(NativeKeyringStore), None)
    }
}

impl ModelProviderVault {
    /// The marker is non-secret and is never used to authorise a model call.
    /// It merely avoids touching Keychain during app launch and Settings reads.
    pub(crate) fn with_status_marker_path(path: PathBuf) -> Self {
        Self::with_parts(Arc::new(NativeKeyringStore), Some(path))
    }

    fn with_parts(store: Arc<dyn CredentialStore>, status_marker_path: Option<PathBuf>) -> Self {
        let status_cache = load_status_cache(status_marker_path.as_deref());
        Self {
            store,
            operation_lock: Mutex::new(()),
            status_cache: Mutex::new(status_cache),
            status_marker_path,
        }
    }

    /// This method intentionally does not create a Keychain entry or call any
    /// Keychain API. It is safe to call on app launch and when Settings opens.
    pub(crate) fn statuses(&self) -> Result<Vec<ModelProviderStatus>, String> {
        [ModelProvider::DeepSeek, ModelProvider::Qwen]
            .into_iter()
            .map(|provider| self.status_from_cache(provider))
            .collect()
    }

    pub(crate) fn save_key(
        &self,
        provider: ModelProvider,
        value: Zeroizing<String>,
    ) -> Result<ModelProviderStatus, String> {
        if !valid_api_key(value.as_str()) {
            return Err("模型 API Key 格式无效；未写入 macOS Keychain。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型密钥安全存储状态锁定失败。".to_string())?;
        self.store
            .set(provider.account(), value.as_str())
            .map_err(|_| "无法写入 macOS Keychain；模型密钥没有保存。".to_string())?;
        // The user explicitly initiated key configuration, so a readback is
        // allowed here. It is never done by the status command.
        let stored = Zeroizing::new(
            self.store
                .get(provider.account())
                .map_err(|_| "Keychain 写入后无法复核；模型密钥没有保存。".to_string())?
                .ok_or_else(|| "Keychain 写入后无法复核；模型密钥没有保存。".to_string())?,
        );
        if stored.as_str() != value.as_str() {
            let _ = self.store.delete(provider.account());
            self.record_key_state(provider, CredentialState::NotConfigured);
            return Err("Keychain 写入复核不一致；模型密钥没有保存。".to_string());
        }
        self.record_key_state(provider, CredentialState::ConfigurationRecorded);
        self.status_from_cache(provider)
    }

    pub(crate) fn remove_key(
        &self,
        provider: ModelProvider,
    ) -> Result<ModelProviderStatus, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型密钥安全存储状态锁定失败。".to_string())?;
        // This is reachable only after the native explicit removal confirmation.
        self.store
            .delete(provider.account())
            .map_err(|_| "无法删除 macOS Keychain 中的模型密钥。".to_string())?;
        self.record_key_state(provider, CredentialState::NotConfigured);
        self.status_from_cache(provider)
    }

    /// Store the non-secret DashScope routing identifier separately from the
    /// API key. The application never accepts a caller-provided model URL:
    /// future model execution may only use one of these fixed provider regions.
    pub(crate) fn save_qwen_connection(
        &self,
        region_id: String,
        workspace_id: String,
    ) -> Result<ModelProviderStatus, String> {
        let connection = valid_qwen_connection(region_id, workspace_id)?;
        let encoded = Zeroizing::new(
            serde_json::to_string(&connection)
                .map_err(|_| "无法编码百炼连接配置；未写入任何设置。".to_string())?,
        );
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型连接安全存储状态锁定失败。".to_string())?;
        self.store
            .set(
                ModelProvider::Qwen
                    .connection_account()
                    .expect("Qwen connection account is fixed"),
                encoded.as_str(),
            )
            .map_err(|_| "无法写入 macOS Keychain 的百炼连接配置。".to_string())?;
        // Do not look up the API key after changing this non-secret setting.
        self.record_qwen_connection(Some(connection.region_id));
        self.status_from_cache(ModelProvider::Qwen)
    }

    pub(crate) fn load_qwen_ocr_credentials(&self) -> Result<QwenOcrCredentials, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型连接安全存储状态锁定失败。".to_string())?;
        let api_key = self.load_valid_api_key(ModelProvider::Qwen)?;
        let raw_connection = Zeroizing::new(
            self.store
                .get(
                    ModelProvider::Qwen
                        .connection_account()
                        .expect("Qwen connection account is fixed"),
                )
                .map_err(|_| {
                    "尚未能从 macOS Keychain 读取 Qwen3.5-OCR 的百炼连接配置。".to_string()
                })?
                .ok_or_else(|| "尚未固定 Qwen3.5-OCR 的百炼地域和业务空间。".to_string())?,
        );
        let connection = serde_json::from_str::<QwenConnection>(raw_connection.as_str())
            .map_err(|_| "Qwen3.5-OCR 的百炼连接配置无效，已拒绝调用。".to_string())?;
        let connection = valid_qwen_connection(connection.region_id, connection.workspace_id)
            .map_err(|_| "Qwen3.5-OCR 的百炼连接配置无效，已拒绝调用。".to_string())?;
        self.record_qwen_connection(Some(connection.region_id.clone()));
        Ok(QwenOcrCredentials {
            api_key,
            region_id: connection.region_id,
            workspace_id: connection.workspace_id,
        })
    }

    pub(crate) fn load_deepseek_planner_credentials(
        &self,
    ) -> Result<DeepSeekPlannerCredentials, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型连接安全存储状态锁定失败。".to_string())?;
        Ok(DeepSeekPlannerCredentials {
            api_key: self.load_valid_api_key(ModelProvider::DeepSeek)?,
        })
    }

    /// Call only from a user-confirmed native model invocation while the
    /// operation lock is held.
    fn load_valid_api_key(&self, provider: ModelProvider) -> Result<Zeroizing<String>, String> {
        let api_key = match self.store.get(provider.account()) {
            Ok(Some(value)) => Zeroizing::new(value),
            Ok(None) => {
                self.record_key_state(provider, CredentialState::NotConfigured);
                return Err(format!(
                    "{} 尚未在 macOS Keychain 中配置 API Key。",
                    provider.display_name()
                ));
            }
            Err(()) => {
                return Err(format!(
                    "{} 的 macOS Keychain 密钥无法在本次已确认调用中读取。",
                    provider.display_name()
                ));
            }
        };
        if !valid_api_key(api_key.as_str()) {
            self.record_key_state(provider, CredentialState::NotConfigured);
            return Err(format!(
                "{} 的 Keychain 密钥格式无效，已拒绝调用。",
                provider.display_name()
            ));
        }
        self.record_key_state(provider, CredentialState::ValidatedForCurrentSession);
        Ok(api_key)
    }

    fn status_from_cache(&self, provider: ModelProvider) -> Result<ModelProviderStatus, String> {
        let cache = self
            .status_cache
            .lock()
            .map_err(|_| "模型配置状态缓存锁定失败。".to_string())?;
        let credential_state = cache.key_state(provider);
        let (connection_ready, connection_label) = match provider {
            ModelProvider::DeepSeek => (true, "官方固定服务地址".to_string()),
            ModelProvider::Qwen => match cache.qwen_connection_region.as_deref() {
                Some(region_id) => (true, qwen_region_label(region_id).to_string()),
                None => (false, "连接设置将在已确认识别时核验".to_string()),
            },
        };
        Ok(ModelProviderStatus {
            provider_id: provider.id().to_string(),
            display_name: provider.display_name().to_string(),
            model_id: provider.model_id().to_string(),
            configured: credential_state.has_configuration_record(),
            configuration_state: credential_state.configuration_state().to_string(),
            connection_ready,
            connection_label,
        })
    }

    fn record_key_state(&self, provider: ModelProvider, state: CredentialState) {
        self.update_status_cache(|cache| cache.set_key_state(provider, state));
    }

    fn record_qwen_connection(&self, region_id: Option<String>) {
        self.update_status_cache(|cache| {
            cache.qwen_connection_region = region_id.filter(|value| is_allowed_qwen_region(value));
        });
    }

    fn update_status_cache(&self, update: impl FnOnce(&mut ProviderStatusCache)) {
        let marker = {
            let Ok(mut cache) = self.status_cache.lock() else {
                return;
            };
            update(&mut cache);
            cache.to_marker()
        };
        persist_status_marker(self.status_marker_path.as_deref(), &marker);
    }

    #[cfg(test)]
    fn with_store_for_test(
        store: Arc<dyn CredentialStore>,
        status_marker_path: Option<PathBuf>,
    ) -> Self {
        Self::with_parts(store, status_marker_path)
    }
}

fn load_status_cache(path: Option<&Path>) -> ProviderStatusCache {
    let Some(path) = path else {
        return ProviderStatusCache::default();
    };
    let Some(raw) = read_private_marker(path) else {
        return ProviderStatusCache::default();
    };
    let Ok(marker) = serde_json::from_slice::<ModelProviderStatusMarker>(&raw) else {
        return ProviderStatusCache::default();
    };
    if marker.version != STATUS_MARKER_VERSION {
        return ProviderStatusCache::default();
    }
    ProviderStatusCache {
        deepseek_key: CredentialState::from_marker(marker.deepseek_key_recorded),
        qwen_key: CredentialState::from_marker(marker.qwen_key_recorded),
        qwen_connection_region: marker
            .qwen_connection_region
            .filter(|value| is_allowed_qwen_region(value)),
    }
}

fn persist_status_marker(path: Option<&Path>, marker: &ModelProviderStatusMarker) {
    let Some(path) = path else {
        return;
    };
    let Some(parent) = path.parent() else {
        return;
    };
    let Ok(encoded) = serde_json::to_vec(marker) else {
        return;
    };
    if !prepare_private_marker_parent(parent) {
        return;
    }
    if matches!(fs::symlink_metadata(path), Ok(metadata) if !metadata.file_type().is_file() || metadata.file_type().is_symlink())
    {
        return;
    }
    let Some(file_name) = path.file_name().and_then(|value| value.to_str()) else {
        return;
    };
    let temporary = parent.join(format!(".{file_name}.{}.tmp", Uuid::new_v4()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let Ok(mut file) = options.open(&temporary) else {
        return;
    };
    if file.write_all(&encoded).is_err()
        || file.sync_all().is_err()
        || !matches!(fs::symlink_metadata(&temporary), Ok(metadata) if metadata.file_type().is_file() && !metadata.file_type().is_symlink())
        || fs::rename(&temporary, path).is_err()
    {
        let _ = fs::remove_file(&temporary);
        return;
    }
    #[cfg(unix)]
    {
        let _ = fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

fn read_private_marker(path: &Path) -> Option<Vec<u8>> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return None;
    }
    #[cfg(unix)]
    if metadata.permissions().mode() & 0o077 != 0 {
        return None;
    }
    fs::read(path).ok()
}

fn prepare_private_marker_parent(path: &Path) -> bool {
    if fs::create_dir_all(path).is_err() {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return false;
    }
    #[cfg(unix)]
    {
        if fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).is_err() {
            return false;
        }
        return fs::symlink_metadata(path)
            .map(|current| current.permissions().mode() & 0o077 == 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    true
}

fn is_allowed_qwen_region(value: &str) -> bool {
    matches!(value, "cn-beijing" | "ap-southeast-1")
}

fn valid_qwen_connection(
    region_id: String,
    workspace_id: String,
) -> Result<QwenConnection, String> {
    let region_id = region_id.trim().to_string();
    let workspace_id = workspace_id.trim().to_string();
    if !is_allowed_qwen_region(&region_id) {
        return Err("百炼地域只能选择华北2（北京）或新加坡；未保存设置。".to_string());
    }
    let bytes = workspace_id.as_bytes();
    if !(3..=120).contains(&bytes.len())
        || !bytes[0].is_ascii_alphanumeric()
        || !bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
    {
        return Err("百炼业务空间 ID 格式无效；未保存设置。".to_string());
    }
    Ok(QwenConnection {
        region_id,
        workspace_id,
    })
}

fn qwen_region_label(region_id: &str) -> &'static str {
    match region_id {
        "cn-beijing" => "已记录：华北2（北京）",
        "ap-southeast-1" => "已记录：新加坡",
        _ => "连接配置无效",
    }
}

pub(crate) fn valid_api_key(value: &str) -> bool {
    let bytes = value.as_bytes();
    (16..=1024).contains(&bytes.len()) && bytes.iter().all(u8::is_ascii_graphic)
}

#[cfg(test)]
mod tests {
    use super::{
        CredentialState, CredentialStore, DEEPSEEK_ACCOUNT, ModelProvider, ModelProviderVault,
        QWEN_CONNECTION_ACCOUNT, valid_api_key, valid_qwen_connection,
    };
    use std::collections::HashMap;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use uuid::Uuid;
    use zeroize::Zeroizing;

    #[derive(Default)]
    struct MemoryCredentialStore {
        values: Mutex<HashMap<String, String>>,
        reads: AtomicUsize,
        writes: AtomicUsize,
        deletes: AtomicUsize,
    }

    impl MemoryCredentialStore {
        fn insert(&self, account: &str, value: &str) {
            self.values
                .lock()
                .expect("test store lock")
                .insert(account.to_string(), value.to_string());
        }

        fn reads(&self) -> usize {
            self.reads.load(Ordering::SeqCst)
        }

        fn writes(&self) -> usize {
            self.writes.load(Ordering::SeqCst)
        }
    }

    impl CredentialStore for MemoryCredentialStore {
        fn get(&self, account: &str) -> Result<Option<String>, ()> {
            self.reads.fetch_add(1, Ordering::SeqCst);
            Ok(self
                .values
                .lock()
                .expect("test store lock")
                .get(account)
                .cloned())
        }

        fn set(&self, account: &str, value: &str) -> Result<(), ()> {
            self.writes.fetch_add(1, Ordering::SeqCst);
            self.values
                .lock()
                .expect("test store lock")
                .insert(account.to_string(), value.to_string());
            Ok(())
        }

        fn delete(&self, account: &str) -> Result<(), ()> {
            self.deletes.fetch_add(1, Ordering::SeqCst);
            self.values.lock().expect("test store lock").remove(account);
            Ok(())
        }
    }

    fn test_vault(store: Arc<MemoryCredentialStore>) -> ModelProviderVault {
        ModelProviderVault::with_store_for_test(store, None)
    }

    fn test_marker_path() -> PathBuf {
        let root = std::env::temp_dir().join(format!("lawcase-model-status-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).expect("test marker directory");
        root.join("model-provider-status-v1.json")
    }

    #[test]
    fn provider_id_is_allowlisted() {
        assert_eq!(
            ModelProvider::parse("deepseek"),
            Ok(ModelProvider::DeepSeek)
        );
        assert_eq!(ModelProvider::parse("qwen"), Ok(ModelProvider::Qwen));
        assert!(ModelProvider::parse("https://example.invalid").is_err());
    }

    #[test]
    fn api_key_shape_rejects_whitespace_and_unbounded_values() {
        assert!(valid_api_key(&"a".repeat(16)));
        assert!(!valid_api_key(&"a".repeat(15)));
        assert!(!valid_api_key("a key with space"));
        assert!(!valid_api_key(&"a".repeat(1025)));
    }

    #[test]
    fn qwen_connection_accepts_only_allowlisted_regions_and_workspace_shape() {
        assert!(valid_qwen_connection("cn-beijing".to_string(), "ws-123".to_string()).is_ok());
        assert!(
            valid_qwen_connection("ap-southeast-1".to_string(), "workspace-abc".to_string())
                .is_ok()
        );
        assert!(
            valid_qwen_connection("https://example.invalid".to_string(), "ws-123".to_string())
                .is_err()
        );
        assert!(valid_qwen_connection("cn-beijing".to_string(), "../other".to_string()).is_err());
    }

    #[test]
    fn statuses_never_read_the_keychain_store() {
        let store = Arc::new(MemoryCredentialStore::default());
        store.insert(DEEPSEEK_ACCOUNT, &"d".repeat(32));
        let vault = test_vault(store.clone());

        let statuses = vault.statuses().expect("status cache");

        assert_eq!(store.reads(), 0, "status lookup must not probe Keychain");
        let deepseek = statuses
            .iter()
            .find(|item| item.provider_id == "deepseek")
            .expect("DeepSeek status");
        assert!(!deepseek.configured);
        assert_eq!(deepseek.configuration_state, "NOT_CHECKED");
    }

    #[test]
    fn explicit_key_save_marks_a_nonsecret_record_without_future_status_reads() {
        let store = Arc::new(MemoryCredentialStore::default());
        let vault = test_vault(store.clone());

        let saved = vault
            .save_key(ModelProvider::DeepSeek, Zeroizing::new("d".repeat(32)))
            .expect("explicit save");
        assert_eq!(store.writes(), 1);
        assert_eq!(store.reads(), 1, "explicit save may verify its own write");
        assert!(saved.configured);
        assert_eq!(saved.configuration_state, "CONFIGURATION_RECORDED");

        let _ = vault.statuses().expect("cache status");
        assert_eq!(
            store.reads(),
            1,
            "status must use only the local marker/cache"
        );
    }

    #[test]
    fn confirmed_model_invocation_validates_key_and_updates_only_cache_state() {
        let store = Arc::new(MemoryCredentialStore::default());
        store.insert(DEEPSEEK_ACCOUNT, &"d".repeat(32));
        let vault = test_vault(store.clone());

        let credentials = vault
            .load_deepseek_planner_credentials()
            .expect("confirmed invocation can read key");
        assert_eq!(credentials.api_key.as_str(), "d".repeat(32));
        assert_eq!(store.reads(), 1);

        let status = vault
            .statuses()
            .expect("cached status")
            .into_iter()
            .find(|item| item.provider_id == "deepseek")
            .expect("DeepSeek status");
        assert_eq!(status.configuration_state, "VALIDATED_FOR_CURRENT_SESSION");
        assert_eq!(store.reads(), 1, "status must not re-read a validated key");
    }

    #[test]
    fn saving_qwen_connection_does_not_read_the_api_key() {
        let store = Arc::new(MemoryCredentialStore::default());
        let vault = test_vault(store.clone());

        let status = vault
            .save_qwen_connection("cn-beijing".to_string(), "workspace-abc".to_string())
            .expect("explicit connection save");

        assert_eq!(store.reads(), 0);
        assert_eq!(store.writes(), 1);
        assert!(status.connection_ready);
        assert_eq!(status.connection_label, "已记录：华北2（北京）");
        assert!(
            store
                .values
                .lock()
                .expect("test store lock")
                .contains_key(QWEN_CONNECTION_ACCOUNT)
        );
    }

    #[test]
    fn persistent_marker_contains_no_api_key_and_is_not_a_credential_claim() {
        let store = Arc::new(MemoryCredentialStore::default());
        let marker_path = test_marker_path();
        let api_key = "s".repeat(32);
        let vault =
            ModelProviderVault::with_store_for_test(store.clone(), Some(marker_path.clone()));

        vault
            .save_key(ModelProvider::DeepSeek, Zeroizing::new(api_key.clone()))
            .expect("explicit save");
        let marker = fs::read_to_string(&marker_path).expect("non-secret marker");
        assert!(!marker.contains(&api_key));
        assert!(!marker.contains("workspace-abc"));

        let reopened = ModelProviderVault::with_store_for_test(store, Some(marker_path.clone()));
        let status = reopened
            .statuses()
            .expect("cache status after restart")
            .into_iter()
            .find(|item| item.provider_id == "deepseek")
            .expect("DeepSeek status");
        assert_eq!(status.configuration_state, "CONFIGURATION_RECORDED");
        assert_eq!(status.configured, true);
        assert_eq!(
            reopened
                .status_cache
                .lock()
                .expect("cache lock")
                .deepseek_key,
            CredentialState::ConfigurationRecorded
        );
        let _ = fs::remove_dir_all(marker_path.parent().expect("marker parent"));
    }
}
