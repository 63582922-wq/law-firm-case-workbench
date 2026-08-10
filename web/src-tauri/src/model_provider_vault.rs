use keyring::{Entry, Error as KeyringError};
use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use zeroize::Zeroizing;

const SERVICE: &str = "cn.lawcase.workbench.model-provider";
const DEEPSEEK_ACCOUNT: &str = "deepseek-api-key-v1";
const QWEN_ACCOUNT: &str = "qwen-api-key-v1";
const QWEN_CONNECTION_ACCOUNT: &str = "qwen-connection-v1";

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

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ModelProviderStatus {
    provider_id: String,
    display_name: String,
    model_id: String,
    configured: bool,
    connection_ready: bool,
    connection_label: String,
}

#[derive(Deserialize, Serialize)]
struct QwenConnection {
    region_id: String,
    workspace_id: String,
}

/// Short-lived credentials passed only to the native model transport.
///
/// The API key is deliberately kept out of the webview, desktop bridge and
/// status objects.  It is read from Keychain immediately before a bounded
/// request and is zeroized when the transport drops it.
pub(crate) struct QwenOcrCredentials {
    pub(crate) api_key: Zeroizing<String>,
    pub(crate) region_id: String,
    pub(crate) workspace_id: String,
}

pub(crate) struct ModelProviderVault {
    operation_lock: Mutex<()>,
}

impl Default for ModelProviderVault {
    fn default() -> Self {
        Self {
            operation_lock: Mutex::new(()),
        }
    }
}

impl ModelProviderVault {
    pub(crate) fn statuses(&self) -> Result<Vec<ModelProviderStatus>, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型密钥安全存储状态锁定失败。".to_string())?;
        [ModelProvider::DeepSeek, ModelProvider::Qwen]
            .into_iter()
            .map(|provider| self.status_locked(provider))
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
        let entry = entry(provider)?;
        entry
            .set_password(value.as_str())
            .map_err(|_| "无法写入 macOS Keychain；模型密钥没有保存。".to_string())?;
        let stored = entry
            .get_password()
            .map_err(|_| "Keychain 写入后无法复核；模型密钥没有保存。".to_string())?;
        if stored != value.as_str() {
            let _ = entry.delete_credential();
            return Err("Keychain 写入复核不一致；模型密钥没有保存。".to_string());
        }
        self.status_locked(provider)
    }

    pub(crate) fn remove_key(
        &self,
        provider: ModelProvider,
    ) -> Result<ModelProviderStatus, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型密钥安全存储状态锁定失败。".to_string())?;
        match entry(provider)?.delete_credential() {
            Ok(()) | Err(KeyringError::NoEntry) => self.status_locked(provider),
            Err(_) => Err("无法删除 macOS Keychain 中的模型密钥。".to_string()),
        }
    }

    /// Store the non-secret DashScope routing identifier separately from the
    /// API key.  The application never accepts a caller-provided model URL:
    /// future model execution may only use one of these fixed provider regions.
    pub(crate) fn save_qwen_connection(
        &self,
        region_id: String,
        workspace_id: String,
    ) -> Result<ModelProviderStatus, String> {
        let connection = valid_qwen_connection(region_id, workspace_id)?;
        let encoded = serde_json::to_string(&connection)
            .map_err(|_| "无法编码百炼连接配置；未写入任何设置。".to_string())?;
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型连接安全存储状态锁定失败。".to_string())?;
        connection_entry(ModelProvider::Qwen)?
            .set_password(&encoded)
            .map_err(|_| "无法写入 macOS Keychain 的百炼连接配置。".to_string())?;
        self.status_locked(ModelProvider::Qwen)
    }

    pub(crate) fn load_qwen_ocr_credentials(&self) -> Result<QwenOcrCredentials, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "模型连接安全存储状态锁定失败。".to_string())?;
        let api_key = entry(ModelProvider::Qwen)?
            .get_password()
            .map_err(|_| "Qwen3.5-OCR 尚未在 macOS Keychain 中配置 API Key。".to_string())?;
        if !valid_api_key(&api_key) {
            return Err("Qwen3.5-OCR 的 Keychain 密钥格式无效，已拒绝调用。".to_string());
        }
        let raw_connection = connection_entry(ModelProvider::Qwen)?
            .get_password()
            .map_err(|_| "尚未固定 Qwen3.5-OCR 的百炼地域和业务空间。".to_string())?;
        let connection = serde_json::from_str::<QwenConnection>(&raw_connection)
            .map_err(|_| "Qwen3.5-OCR 的百炼连接配置无效，已拒绝调用。".to_string())?;
        let connection = valid_qwen_connection(connection.region_id, connection.workspace_id)
            .map_err(|_| "Qwen3.5-OCR 的百炼连接配置无效，已拒绝调用。".to_string())?;
        Ok(QwenOcrCredentials {
            api_key: Zeroizing::new(api_key),
            region_id: connection.region_id,
            workspace_id: connection.workspace_id,
        })
    }

    fn status_locked(&self, provider: ModelProvider) -> Result<ModelProviderStatus, String> {
        let configured = match entry(provider)?.get_password() {
            Ok(_) => true,
            Err(KeyringError::NoEntry) => false,
            Err(_) => return Err("无法读取 macOS Keychain 的模型密钥状态。".to_string()),
        };
        let (connection_ready, connection_label) = match provider {
            ModelProvider::DeepSeek => (true, "官方固定服务地址".to_string()),
            ModelProvider::Qwen => qwen_connection_status()?,
        };
        Ok(ModelProviderStatus {
            provider_id: provider.id().to_string(),
            display_name: provider.display_name().to_string(),
            model_id: provider.model_id().to_string(),
            configured,
            connection_ready,
            connection_label,
        })
    }
}

fn entry(provider: ModelProvider) -> Result<Entry, String> {
    Entry::new(SERVICE, provider.account())
        .map_err(|_| "无法访问 macOS Keychain 的模型密钥项目。".to_string())
}

fn connection_entry(provider: ModelProvider) -> Result<Entry, String> {
    let account = provider
        .connection_account()
        .ok_or_else(|| "该模型服务不需要额外连接配置。".to_string())?;
    Entry::new(SERVICE, account).map_err(|_| "无法访问 macOS Keychain 的模型连接配置。".to_string())
}

fn qwen_connection_status() -> Result<(bool, String), String> {
    match connection_entry(ModelProvider::Qwen)?.get_password() {
        Err(KeyringError::NoEntry) => Ok((false, "尚未固定百炼业务空间和地域".to_string())),
        Err(_) => Err("无法读取 macOS Keychain 的百炼连接配置状态。".to_string()),
        Ok(value) => match serde_json::from_str::<QwenConnection>(&value)
            .ok()
            .and_then(|connection| {
                valid_qwen_connection(connection.region_id, connection.workspace_id).ok()
            }) {
            Some(connection) => Ok((true, qwen_region_label(&connection.region_id).to_string())),
            None => Ok((false, "百炼连接配置无效，尚未启用调用".to_string())),
        },
    }
}

fn valid_qwen_connection(
    region_id: String,
    workspace_id: String,
) -> Result<QwenConnection, String> {
    let region_id = region_id.trim().to_string();
    let workspace_id = workspace_id.trim().to_string();
    if !matches!(region_id.as_str(), "cn-beijing" | "ap-southeast-1") {
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
        "cn-beijing" => "已固定：华北2（北京）",
        "ap-southeast-1" => "已固定：新加坡",
        _ => "连接配置无效",
    }
}

pub(crate) fn valid_api_key(value: &str) -> bool {
    let bytes = value.as_bytes();
    (16..=1024).contains(&bytes.len()) && bytes.iter().all(u8::is_ascii_graphic)
}

#[cfg(test)]
mod tests {
    use super::{ModelProvider, valid_api_key, valid_qwen_connection};

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
}
