use keyring::{Entry, Error as KeyringError};
use serde::Serialize;
use std::sync::Mutex;
use zeroize::Zeroizing;

const SERVICE: &str = "cn.lawcase.workbench.model-provider";
const DEEPSEEK_ACCOUNT: &str = "deepseek-api-key-v1";
const QWEN_ACCOUNT: &str = "qwen-api-key-v1";

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

    fn status_locked(&self, provider: ModelProvider) -> Result<ModelProviderStatus, String> {
        let configured = match entry(provider)?.get_password() {
            Ok(_) => true,
            Err(KeyringError::NoEntry) => false,
            Err(_) => return Err("无法读取 macOS Keychain 的模型密钥状态。".to_string()),
        };
        Ok(ModelProviderStatus {
            provider_id: provider.id().to_string(),
            display_name: provider.display_name().to_string(),
            model_id: provider.model_id().to_string(),
            configured,
        })
    }
}

fn entry(provider: ModelProvider) -> Result<Entry, String> {
    Entry::new(SERVICE, provider.account())
        .map_err(|_| "无法访问 macOS Keychain 的模型密钥项目。".to_string())
}

pub(crate) fn valid_api_key(value: &str) -> bool {
    let bytes = value.as_bytes();
    (16..=1024).contains(&bytes.len()) && bytes.iter().all(u8::is_ascii_graphic)
}

#[cfg(test)]
mod tests {
    use super::{ModelProvider, valid_api_key};

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
}
