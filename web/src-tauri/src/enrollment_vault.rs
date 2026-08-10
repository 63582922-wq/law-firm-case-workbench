use base64::{Engine as _, engine::general_purpose::STANDARD};
use keyring::{Entry, Error as KeyringError};
use serde::Serialize;
use std::sync::{Arc, Mutex};
use zeroize::Zeroizing;

const SERVICE: &str = "cn.lawcase.workbench.desktop-enrollment";
const ENROLLMENT_ACCOUNT: &str = "signed-enrollment-v1";
const INSTALLATION_ACCOUNT: &str = "installation-binding-v1";
const INITIALIZE_CONFIRMATION: &str = "INIT_LOCAL_KEYCHAIN";
const LOCAL_DISABLE_CONFIRMATION: &str = "DISABLE_LOCAL_ENROLLMENT";
const INSTALLATION_SECRET_BYTES: usize = 32;
const MAX_ENROLLMENT_BYTES: usize = 16_384;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct EnrollmentVaultStatus {
    pub(crate) phase: String,
    pub(crate) message: String,
    pub(crate) installation_initialized: bool,
    pub(crate) enrollment_envelope_present: bool,
}

#[derive(Debug)]
struct StoreFailure;

trait CredentialStore: Send + Sync {
    fn get(&self, account: &str) -> Result<Option<String>, StoreFailure>;
    fn set(&self, account: &str, value: &str) -> Result<(), StoreFailure>;
    fn delete(&self, account: &str) -> Result<(), StoreFailure>;
}

struct NativeKeyringStore;

impl NativeKeyringStore {
    fn entry(account: &str) -> Result<Entry, StoreFailure> {
        Entry::new(SERVICE, account).map_err(|_| StoreFailure)
    }
}

impl CredentialStore for NativeKeyringStore {
    fn get(&self, account: &str) -> Result<Option<String>, StoreFailure> {
        match Self::entry(account)?.get_password() {
            Ok(value) => Ok(Some(value)),
            Err(KeyringError::NoEntry) => Ok(None),
            Err(_) => Err(StoreFailure),
        }
    }

    fn set(&self, account: &str, value: &str) -> Result<(), StoreFailure> {
        Self::entry(account)?
            .set_password(value)
            .map_err(|_| StoreFailure)
    }

    fn delete(&self, account: &str) -> Result<(), StoreFailure> {
        match Self::entry(account)?.delete_credential() {
            Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
            Err(_) => Err(StoreFailure),
        }
    }
}

pub(crate) struct EnrollmentVault {
    store: Arc<dyn CredentialStore>,
    operation_lock: Mutex<()>,
}

impl Default for EnrollmentVault {
    fn default() -> Self {
        Self {
            store: Arc::new(NativeKeyringStore),
            operation_lock: Mutex::new(()),
        }
    }
}

impl EnrollmentVault {
    #[cfg(test)]
    fn with_store(store: Arc<dyn CredentialStore>) -> Self {
        Self {
            store,
            operation_lock: Mutex::new(()),
        }
    }

    pub(crate) fn status(&self) -> EnrollmentVaultStatus {
        let _guard = match self.operation_lock.lock() {
            Ok(guard) => guard,
            Err(_) => return blocked_status("本机安全存储状态锁定失败。"),
        };
        self.status_locked()
    }

    pub(crate) fn initialize_installation(
        &self,
        confirmation: &str,
    ) -> Result<EnrollmentVaultStatus, String> {
        self.initialize_installation_with_secret(confirmation, None)
    }

    fn initialize_installation_with_secret(
        &self,
        confirmation: &str,
        supplied_secret: Option<[u8; INSTALLATION_SECRET_BYTES]>,
    ) -> Result<EnrollmentVaultStatus, String> {
        if confirmation != INITIALIZE_CONFIRMATION {
            return Err("未确认本机 Keychain 初始化，未执行任何写入。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        if self.read_installation_secret()?.is_some() {
            return Ok(self.status_locked());
        }

        let mut secret = supplied_secret.unwrap_or([0_u8; INSTALLATION_SECRET_BYTES]);
        if supplied_secret.is_none() {
            getrandom::fill(&mut secret)
                .map_err(|_| "无法生成本机安装秘密，未执行写入。".to_string())?;
        }
        let encoded = Zeroizing::new(STANDARD.encode(secret));
        self.store
            .set(INSTALLATION_ACCOUNT, encoded.as_str())
            .map_err(|_| "无法写入 macOS Keychain；本机身份仍未启用。".to_string())?;
        let readback = self
            .read_installation_secret()?
            .ok_or_else(|| "Keychain 写入后无法复核，本机身份仍未启用。".to_string())?;
        if readback.as_slice() != secret {
            return Err("Keychain 写入复核不一致，本机身份仍未启用。".to_string());
        }
        Ok(self.status_locked())
    }

    pub(crate) fn disable_local_enrollment(
        &self,
        confirmation: &str,
    ) -> Result<EnrollmentVaultStatus, String> {
        if confirmation != LOCAL_DISABLE_CONFIRMATION {
            return Err("未确认仅停用本机登记，未执行任何删除。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        self.store
            .delete(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法删除本机登记凭证；远程撤销状态未改变。".to_string())?;
        let mut status = self.status_locked();
        status.phase = "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED".to_string();
        status.message = "本机登记已清除；这不代表律所服务端已撤销。".to_string();
        Ok(status)
    }

    fn status_locked(&self) -> EnrollmentVaultStatus {
        let secret = match self.read_installation_secret() {
            Ok(secret) => secret,
            Err(message) => return blocked_status(&message),
        };
        let envelope = match self.store.get(ENROLLMENT_ACCOUNT) {
            Ok(value) => value,
            Err(_) => return blocked_status("无法读取本机登记凭证状态。"),
        };
        let envelope_valid_shape = envelope
            .as_ref()
            .map(|value| {
                !value.is_empty()
                    && value.len() <= MAX_ENROLLMENT_BYTES
                    && serde_json::from_str::<serde_json::Value>(value)
                        .ok()
                        .and_then(|parsed| parsed.as_object().cloned())
                        .map(|object| {
                            object.len() == 2
                                && object.contains_key("credential")
                                && object.contains_key("signature")
                        })
                        .unwrap_or(false)
            })
            .unwrap_or(true);
        if !envelope_valid_shape {
            return EnrollmentVaultStatus {
                phase: "BROKEN_LOCAL_CREDENTIAL".to_string(),
                message: "Keychain 中的登记凭证格式异常；可仅停用本机后重新登记。".to_string(),
                installation_initialized: secret.is_some(),
                enrollment_envelope_present: true,
            };
        }
        match (secret.is_some(), envelope.is_some()) {
            (false, false) => EnrollmentVaultStatus {
                phase: "NOT_INITIALIZED".to_string(),
                message: "尚未初始化本机身份安全存储。".to_string(),
                installation_initialized: false,
                enrollment_envelope_present: false,
            },
            (true, false) => EnrollmentVaultStatus {
                phase: "INSTALLATION_READY".to_string(),
                message: "本机安装秘密已就绪；尚未取得律所签名登记。".to_string(),
                installation_initialized: true,
                enrollment_envelope_present: false,
            },
            (true, true) => EnrollmentVaultStatus {
                phase: "CREDENTIAL_PRESENT_UNVERIFIED".to_string(),
                message: "检测到登记凭证；仍需由受信公钥验签并连接案件数据库。".to_string(),
                installation_initialized: true,
                enrollment_envelope_present: true,
            },
            (false, true) => EnrollmentVaultStatus {
                phase: "BROKEN_LOCAL_CREDENTIAL".to_string(),
                message: "存在登记凭证但缺少本机安装秘密；案件访问保持禁用。".to_string(),
                installation_initialized: false,
                enrollment_envelope_present: true,
            },
        }
    }

    fn read_installation_secret(&self) -> Result<Option<Zeroizing<Vec<u8>>>, String> {
        let Some(encoded) = self
            .store
            .get(INSTALLATION_ACCOUNT)
            .map_err(|_| "无法读取本机安装秘密状态。".to_string())?
        else {
            return Ok(None);
        };
        let decoded = STANDARD
            .decode(encoded.as_bytes())
            .map_err(|_| "Keychain 中的本机安装秘密格式异常。".to_string())?;
        if decoded.len() != INSTALLATION_SECRET_BYTES {
            return Err("Keychain 中的本机安装秘密长度异常。".to_string());
        }
        Ok(Some(Zeroizing::new(decoded)))
    }
}

fn blocked_status(message: &str) -> EnrollmentVaultStatus {
    EnrollmentVaultStatus {
        phase: "UNAVAILABLE".to_string(),
        message: message.to_string(),
        installation_initialized: false,
        enrollment_envelope_present: false,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CredentialStore, ENROLLMENT_ACCOUNT, EnrollmentVault, INITIALIZE_CONFIRMATION,
        INSTALLATION_ACCOUNT, LOCAL_DISABLE_CONFIRMATION, StoreFailure,
    };
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct MemoryStore {
        values: Mutex<HashMap<String, String>>,
    }

    impl CredentialStore for MemoryStore {
        fn get(&self, account: &str) -> Result<Option<String>, StoreFailure> {
            Ok(self.values.lock().unwrap().get(account).cloned())
        }

        fn set(&self, account: &str, value: &str) -> Result<(), StoreFailure> {
            self.values
                .lock()
                .unwrap()
                .insert(account.to_string(), value.to_string());
            Ok(())
        }

        fn delete(&self, account: &str) -> Result<(), StoreFailure> {
            self.values.lock().unwrap().remove(account);
            Ok(())
        }
    }

    #[test]
    fn initialization_is_explicit_idempotent_and_stores_exactly_32_bytes() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        assert!(
            vault
                .initialize_installation_with_secret("wrong", Some([7_u8; 32]))
                .is_err()
        );
        assert!(store.values.lock().unwrap().is_empty());

        let status = vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
            .unwrap();
        assert_eq!(status.phase, "INSTALLATION_READY");
        let encoded = store
            .values
            .lock()
            .unwrap()
            .get(INSTALLATION_ACCOUNT)
            .cloned()
            .unwrap();
        assert_eq!(STANDARD.decode(encoded).unwrap(), vec![7_u8; 32]);

        vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([9_u8; 32]))
            .unwrap();
        let encoded_after = store
            .values
            .lock()
            .unwrap()
            .get(INSTALLATION_ACCOUNT)
            .cloned()
            .unwrap();
        assert_eq!(STANDARD.decode(encoded_after).unwrap(), vec![7_u8; 32]);
    }

    #[test]
    fn malformed_existing_installation_secret_is_never_overwritten() {
        let store = Arc::new(MemoryStore::default());
        store
            .values
            .lock()
            .unwrap()
            .insert(INSTALLATION_ACCOUNT.to_string(), "broken".to_string());
        let vault = EnrollmentVault::with_store(store.clone());
        assert!(
            vault
                .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
                .is_err()
        );
        assert_eq!(
            store.values.lock().unwrap().get(INSTALLATION_ACCOUNT),
            Some(&"broken".to_string())
        );
    }

    #[test]
    fn local_disable_deletes_only_enrollment_and_never_claims_remote_revocation() {
        let store = Arc::new(MemoryStore::default());
        {
            let mut values = store.values.lock().unwrap();
            values.insert(
                INSTALLATION_ACCOUNT.to_string(),
                STANDARD.encode([3_u8; 32]),
            );
            values.insert(
                ENROLLMENT_ACCOUNT.to_string(),
                r#"{"credential":{},"signature":"synthetic"}"#.to_string(),
            );
        }
        let vault = EnrollmentVault::with_store(store.clone());
        assert!(vault.disable_local_enrollment("wrong").is_err());
        let status = vault
            .disable_local_enrollment(LOCAL_DISABLE_CONFIRMATION)
            .unwrap();
        assert_eq!(status.phase, "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED");
        let values = store.values.lock().unwrap();
        assert!(values.contains_key(INSTALLATION_ACCOUNT));
        assert!(!values.contains_key(ENROLLMENT_ACCOUNT));
    }
}
