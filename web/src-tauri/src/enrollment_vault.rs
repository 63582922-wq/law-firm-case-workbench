use base64::{Engine as _, engine::general_purpose::STANDARD};
use keyring::{Entry, Error as KeyringError};
use serde::Serialize;
use sha2::{Digest, Sha256};
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

pub(crate) struct EnrollmentVerificationContext {
    pub(crate) installation_binding_sha256: String,
    pub(crate) expected_current_sha256: Option<String>,
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

    pub(crate) fn verification_context(&self) -> Result<EnrollmentVerificationContext, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let secret = self
            .read_installation_secret()?
            .ok_or_else(|| "请先初始化本机安全存储，再导入律所登记包。".to_string())?;
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未开始导入。".to_string())?;
        Ok(EnrollmentVerificationContext {
            installation_binding_sha256: format!("{:x}", Sha256::digest(secret.as_slice())),
            expected_current_sha256: current.as_deref().map(envelope_sha256),
        })
    }

    /// Persist an envelope only after a trusted verifier has authenticated the
    /// exact bytes and returned their SHA-256.  This method is deliberately not
    /// exposed as a Tauri command: the WebView can never submit arbitrary
    /// identity material for storage.
    pub(crate) fn commit_enrollment_after_verification(
        &self,
        envelope_text: &str,
        verified_envelope_sha256: &str,
        verified_installation_binding_sha256: &str,
        expected_current_sha256: Option<&str>,
    ) -> Result<EnrollmentVaultStatus, String> {
        if !valid_enrollment_shape(envelope_text)
            || !valid_sha256(verified_envelope_sha256)
            || !valid_sha256(verified_installation_binding_sha256)
            || expected_current_sha256.is_some_and(|value| !valid_sha256(value))
        {
            return Err("已验签登记回执格式无效；未写入本机凭证。".to_string());
        }
        let actual_hash = envelope_sha256(envelope_text);
        if actual_hash != verified_envelope_sha256 {
            return Err("登记凭证与已验签回执不一致；未写入本机凭证。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let current_secret = self
            .read_installation_secret()?
            .ok_or_else(|| "本机安装秘密尚未就绪；未写入登记凭证。".to_string())?;
        let current_binding = format!("{:x}", Sha256::digest(current_secret.as_slice()));
        if current_binding != verified_installation_binding_sha256 {
            return Err("本机安装秘密已变化；未保存绑定旧设备状态的凭证。".to_string());
        }
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未执行替换。".to_string())?;
        let current_hash = current.as_deref().map(envelope_sha256);
        let expected = expected_current_sha256.map(str::to_string);
        if current_hash != expected {
            return Err("当前登记凭证已变化；未覆盖较新的本机状态。".to_string());
        }
        self.store
            .set(ENROLLMENT_ACCOUNT, envelope_text)
            .map_err(|_| "无法写入已验签登记凭证。".to_string())?;
        let readback = self.store.get(ENROLLMENT_ACCOUNT);
        if !matches!(readback, Ok(Some(ref value)) if value == envelope_text) {
            match current {
                Some(ref prior) => {
                    let _ = self.store.set(ENROLLMENT_ACCOUNT, prior);
                }
                None => {
                    let _ = self.store.delete(ENROLLMENT_ACCOUNT);
                }
            }
            return Err("登记凭证写入后复核失败；已尝试恢复原状态。".to_string());
        }
        Ok(self.status_locked())
    }

    /// Delete the current envelope only after the parent-protected sidecar has
    /// returned an accepted remote revocation receipt bound to its exact hash.
    pub(crate) fn commit_remote_revocation(
        &self,
        expected_current_sha256: &str,
    ) -> Result<EnrollmentVaultStatus, String> {
        if !valid_sha256(expected_current_sha256) {
            return Err("远程撤销回执格式无效；未删除本机登记。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未完成远程撤销。".to_string())?
            .ok_or_else(|| "当前没有可撤销的本机登记凭证。".to_string())?;
        if envelope_sha256(&current) != expected_current_sha256 {
            return Err("当前登记凭证已变化；未删除较新的本机状态。".to_string());
        }
        self.store
            .delete(ENROLLMENT_ACCOUNT)
            .map_err(|_| "律所已接受撤销，但本机凭证删除失败；请立即联系管理员。".to_string())?;
        match self.store.get(ENROLLMENT_ACCOUNT) {
            Ok(None) => {}
            Ok(Some(_)) | Err(_) => {
                let _ = self.store.set(ENROLLMENT_ACCOUNT, &current);
                return Err("本机凭证删除后复核失败；已尝试恢复并请联系管理员。".to_string());
            }
        }
        let mut status = self.status_locked();
        status.phase = "REMOTE_REVOKED_CONFIRMED".to_string();
        status.message = "律所服务端已接受撤销，本机登记凭证也已清除。".to_string();
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
            .map(|value| valid_enrollment_shape(value))
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

fn valid_enrollment_shape(value: &str) -> bool {
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
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
}

fn envelope_sha256(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
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
        INSTALLATION_ACCOUNT, LOCAL_DISABLE_CONFIRMATION, StoreFailure, envelope_sha256,
    };
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    use sha2::{Digest, Sha256};
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

    #[test]
    fn verified_enrollment_commit_is_hash_bound_and_compare_and_set() {
        let store = Arc::new(MemoryStore::default());
        store.values.lock().unwrap().insert(
            INSTALLATION_ACCOUNT.to_string(),
            STANDARD.encode([3_u8; 32]),
        );
        let vault = EnrollmentVault::with_store(store.clone());
        let first = r#"{"credential":{},"signature":"first"}"#;
        assert!(
            vault
                .commit_enrollment_after_verification(
                    first,
                    &"0".repeat(64),
                    &format!("{:x}", Sha256::digest([3_u8; 32])),
                    None,
                )
                .is_err()
        );
        let first_hash = envelope_sha256(first);
        assert!(
            vault
                .commit_enrollment_after_verification(first, &first_hash, &"0".repeat(64), None)
                .is_err()
        );
        assert!(
            !store
                .values
                .lock()
                .unwrap()
                .contains_key(ENROLLMENT_ACCOUNT)
        );
        let status = vault
            .commit_enrollment_after_verification(
                first,
                &first_hash,
                &format!("{:x}", Sha256::digest([3_u8; 32])),
                None,
            )
            .unwrap();
        assert_eq!(status.phase, "CREDENTIAL_PRESENT_UNVERIFIED");

        let second = r#"{"credential":{},"signature":"second"}"#;
        let second_hash = envelope_sha256(second);
        assert!(
            vault
                .commit_enrollment_after_verification(
                    second,
                    &second_hash,
                    &format!("{:x}", Sha256::digest([3_u8; 32])),
                    None,
                )
                .is_err()
        );
        vault
            .commit_enrollment_after_verification(
                second,
                &second_hash,
                &format!("{:x}", Sha256::digest([3_u8; 32])),
                Some(&first_hash),
            )
            .unwrap();
        assert_eq!(
            store.values.lock().unwrap().get(ENROLLMENT_ACCOUNT),
            Some(&second.to_string())
        );
    }

    #[test]
    fn verified_enrollment_commit_requires_initialized_installation() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        let envelope = r#"{"credential":{},"signature":"first"}"#;
        assert!(
            vault
                .commit_enrollment_after_verification(
                    envelope,
                    &envelope_sha256(envelope),
                    &format!("{:x}", Sha256::digest([3_u8; 32])),
                    None,
                )
                .is_err()
        );
        assert!(store.values.lock().unwrap().is_empty());
    }

    #[test]
    fn verification_context_exposes_only_binding_and_current_envelope_hashes() {
        let store = Arc::new(MemoryStore::default());
        let envelope = r#"{"credential":{},"signature":"first"}"#;
        {
            let mut values = store.values.lock().unwrap();
            values.insert(
                INSTALLATION_ACCOUNT.to_string(),
                STANDARD.encode([3_u8; 32]),
            );
            values.insert(ENROLLMENT_ACCOUNT.to_string(), envelope.to_string());
        }
        let vault = EnrollmentVault::with_store(store);
        let context = vault.verification_context().unwrap();
        assert_eq!(
            context.installation_binding_sha256,
            format!("{:x}", Sha256::digest([3_u8; 32]))
        );
        assert_eq!(
            context.expected_current_sha256,
            Some(envelope_sha256(envelope))
        );
    }

    #[test]
    fn remote_revocation_delete_is_exact_hash_bound() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
            .unwrap();
        let envelope = r#"{"credential":{},"signature":"x"}"#;
        store
            .values
            .lock()
            .unwrap()
            .insert(ENROLLMENT_ACCOUNT.to_string(), envelope.to_string());
        assert!(vault.commit_remote_revocation(&"0".repeat(64)).is_err());
        assert!(
            store
                .values
                .lock()
                .unwrap()
                .contains_key(ENROLLMENT_ACCOUNT)
        );
        let status = vault
            .commit_remote_revocation(&envelope_sha256(envelope))
            .unwrap();
        assert_eq!(status.phase, "REMOTE_REVOKED_CONFIRMED");
        assert!(
            !store
                .values
                .lock()
                .unwrap()
                .contains_key(ENROLLMENT_ACCOUNT)
        );
    }
}
