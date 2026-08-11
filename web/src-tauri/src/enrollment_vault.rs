use base64::{Engine as _, engine::general_purpose::STANDARD};
use chrono::{DateTime, Utc};
use keyring::{Entry, Error as KeyringError};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use uuid::Uuid;
use zeroize::Zeroizing;

const SERVICE: &str = "cn.lawcase.workbench.desktop-enrollment";
const ENROLLMENT_ACCOUNT: &str = "signed-enrollment-v1";
const INSTALLATION_ACCOUNT: &str = "installation-binding-v1";
const PENDING_OPERATION_ACCOUNT: &str = "pending-enrollment-operation-v1";
const INITIALIZE_CONFIRMATION: &str = "INIT_LOCAL_KEYCHAIN";
const LOCAL_DISABLE_CONFIRMATION: &str = "DISABLE_LOCAL_ENROLLMENT";
const INSTALLATION_SECRET_BYTES: usize = 32;
const MAX_ENROLLMENT_BYTES: usize = 16_384;
const PENDING_OPERATION_VERSION: u8 = 1;
const STATUS_MARKER_VERSION: u8 = 1;

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct EnrollmentVaultStatus {
    pub(crate) phase: String,
    pub(crate) message: String,
    pub(crate) installation_initialized: bool,
    pub(crate) enrollment_envelope_present: bool,
}

/// A non-secret, local reminder of the last explicitly observed enrollment
/// state. It is deliberately not used to authorize a session or decide that a
/// Keychain credential exists. Its sole purpose is to let app launch and the
/// Settings page explain the next safe action without asking macOS Keychain.
#[derive(Clone)]
struct EnrollmentStatusCache {
    installation_initialized: Option<bool>,
    enrollment_envelope_present: Option<bool>,
    pending_operation_recorded: bool,
}

impl Default for EnrollmentStatusCache {
    fn default() -> Self {
        Self {
            installation_initialized: None,
            enrollment_envelope_present: None,
            pending_operation_recorded: false,
        }
    }
}

impl EnrollmentStatusCache {
    fn to_marker(&self) -> EnrollmentStatusMarker {
        EnrollmentStatusMarker {
            version: STATUS_MARKER_VERSION,
            installation_initialized: self.installation_initialized,
            enrollment_envelope_present: self.enrollment_envelope_present,
            pending_operation_recorded: self.pending_operation_recorded,
        }
    }
}

/// This marker never contains an installation secret, enrollment envelope,
/// pending-operation identifier, signature, keychain account, or case data.
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentStatusMarker {
    version: u8,
    installation_initialized: Option<bool>,
    enrollment_envelope_present: Option<bool>,
    pending_operation_recorded: bool,
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
    status_cache: Mutex<EnrollmentStatusCache>,
    status_marker_path: Option<PathBuf>,
}

pub(crate) struct EnrollmentVerificationContext {
    pub(crate) installation_binding_sha256: String,
    pub(crate) expected_current_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PendingEnrollmentOperation {
    version: u8,
    pub(crate) operation_id: String,
    pub(crate) operation_kind: String,
    pub(crate) expected_current_sha256: Option<String>,
    pub(crate) installation_binding_sha256: String,
    pub(crate) created_at: String,
}

impl Default for EnrollmentVault {
    fn default() -> Self {
        Self::with_parts(Arc::new(NativeKeyringStore), None)
    }
}

impl EnrollmentVault {
    /// Opening Settings reads only this non-secret marker, never Keychain.
    pub(crate) fn with_status_marker_path(path: PathBuf) -> Self {
        Self::with_parts(Arc::new(NativeKeyringStore), Some(path))
    }

    fn with_parts(store: Arc<dyn CredentialStore>, status_marker_path: Option<PathBuf>) -> Self {
        Self {
            store,
            operation_lock: Mutex::new(()),
            status_cache: Mutex::new(load_status_cache(status_marker_path.as_deref())),
            status_marker_path,
        }
    }

    #[cfg(test)]
    fn with_store(store: Arc<dyn CredentialStore>) -> Self {
        Self::with_parts(store, None)
    }

    /// Safe for app launch and Settings: this intentionally never opens or
    /// reads a Keychain entry. The returned values are configuration records,
    /// not proof that a credential currently exists.
    pub(crate) fn status(&self) -> EnrollmentVaultStatus {
        let cache = match self.status_cache.lock() {
            Ok(cache) => cache,
            Err(_) => return blocked_status("本机安全存储状态缓存锁定失败。"),
        };
        cached_status(&cache)
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
            return Ok(self.status_after_authorized_operation_locked());
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
        Ok(self.status_after_authorized_operation_locked())
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
        if self.read_pending_operation()?.is_some() {
            return Err("存在尚未消解的远程登记操作；不能先清除本机凭证。".to_string());
        }
        self.store
            .delete(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法删除本机登记凭证；远程撤销状态未改变。".to_string())?;
        let mut status = self.status_after_authorized_operation_locked();
        status.phase = "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED".to_string();
        status.message = "本机登记已清除；这不代表律所服务端已撤销。".to_string();
        Ok(status)
    }

    pub(crate) fn verification_context(&self) -> Result<EnrollmentVerificationContext, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        if self.read_pending_operation()?.is_some() {
            return Err("存在尚未消解的远程登记操作；请先查询远程状态。".to_string());
        }
        let secret = self
            .read_installation_secret()?
            .ok_or_else(|| "请先初始化本机安全存储，再导入律所登记包。".to_string())?;
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未开始导入。".to_string())?;
        self.update_status_cache(|cache| {
            cache.installation_initialized = Some(true);
            cache.enrollment_envelope_present = Some(current.is_some());
            cache.pending_operation_recorded = false;
        });
        Ok(EnrollmentVerificationContext {
            installation_binding_sha256: format!("{:x}", Sha256::digest(secret.as_slice())),
            expected_current_sha256: current.as_deref().map(envelope_sha256),
        })
    }

    pub(crate) fn begin_remote_operation(
        &self,
        operation_id: &str,
        operation_kind: &str,
        expected_current_sha256: Option<&str>,
        created_at: &str,
    ) -> Result<PendingEnrollmentOperation, String> {
        validate_pending_fields(
            operation_id,
            operation_kind,
            expected_current_sha256,
            None,
            created_at,
        )?;
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        if self.read_pending_operation()?.is_some() {
            return Err("已有远程登记操作结果待确认；请先查询状态，不能重复提交。".to_string());
        }
        let installation_secret = self
            .read_installation_secret()?
            .ok_or_else(|| "本机安装秘密尚未就绪；未开始远程操作。".to_string())?;
        let installation_binding_sha256 =
            format!("{:x}", Sha256::digest(installation_secret.as_slice()));
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未开始远程操作。".to_string())?;
        let current_hash = current.as_deref().map(envelope_sha256);
        let expected = expected_current_sha256.map(str::to_string);
        if current_hash != expected
            || (operation_kind == "ACTIVATE" && expected.is_some())
            || (operation_kind != "ACTIVATE" && expected.is_none())
        {
            return Err("当前登记凭证与远程操作前置状态不一致；未提交请求。".to_string());
        }
        let pending = PendingEnrollmentOperation {
            version: PENDING_OPERATION_VERSION,
            operation_id: operation_id.to_string(),
            operation_kind: operation_kind.to_string(),
            expected_current_sha256: expected,
            installation_binding_sha256,
            created_at: created_at.to_string(),
        };
        let serialized = serde_json::to_string(&pending)
            .map_err(|_| "无法记录远程操作标识；未提交请求。".to_string())?;
        self.store
            .set(PENDING_OPERATION_ACCOUNT, &serialized)
            .map_err(|_| "无法在 Keychain 记录待决操作；未提交请求。".to_string())?;
        if self.read_pending_operation()? != Some(pending.clone()) {
            let _ = self.store.delete(PENDING_OPERATION_ACCOUNT);
            return Err("待决操作写入 Keychain 后复核失败；未提交请求。".to_string());
        }
        self.update_status_cache(|cache| {
            cache.installation_initialized = Some(true);
            cache.enrollment_envelope_present = Some(current.is_some());
            cache.pending_operation_recorded = true;
        });
        Ok(pending)
    }

    pub(crate) fn pending_remote_operation(&self) -> Result<PendingEnrollmentOperation, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        match self.read_pending_operation()? {
            Some(pending) => {
                self.update_status_cache(|cache| cache.pending_operation_recorded = true);
                Ok(pending)
            }
            None => {
                self.update_status_cache(|cache| cache.pending_operation_recorded = false);
                Err("当前没有待确认的远程登记操作。".to_string())
            }
        }
    }

    pub(crate) fn clear_rejected_remote_operation(
        &self,
        operation_id: &str,
    ) -> Result<EnrollmentVaultStatus, String> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let pending = self.require_pending_operation(operation_id, None, None)?;
        self.store
            .delete(PENDING_OPERATION_ACCOUNT)
            .map_err(|_| "无法清除已拒绝的待决操作；本机凭证未改变。".to_string())?;
        if self.read_pending_operation()?.is_some() {
            let _ = self.restore_pending_operation(&pending);
            return Err("待决操作清除后复核失败；本机凭证未改变。".to_string());
        }
        let mut status = self.base_status_locked();
        self.record_authorized_status(&status);
        status.phase = "REMOTE_OPERATION_REJECTED".to_string();
        status.message = "律所服务端已明确拒绝该操作；本机登记凭证未改变。".to_string();
        Ok(status)
    }

    pub(crate) fn commit_remote_enrollment_operation(
        &self,
        operation_id: &str,
        operation_kind: &str,
        envelope_text: &str,
        verified_envelope_sha256: &str,
        verified_installation_binding_sha256: &str,
        expected_current_sha256: Option<&str>,
    ) -> Result<EnrollmentVaultStatus, String> {
        if !matches!(operation_kind, "ACTIVATE" | "RENEW")
            || !valid_enrollment_shape(envelope_text)
            || !valid_sha256(verified_envelope_sha256)
            || !valid_sha256(verified_installation_binding_sha256)
            || expected_current_sha256.is_some_and(|value| !valid_sha256(value))
            || envelope_sha256(envelope_text) != verified_envelope_sha256
        {
            return Err("远程登记成功回执格式无效；未写入本机凭证。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let pending = self.require_pending_operation(
            operation_id,
            Some(operation_kind),
            Some(expected_current_sha256),
        )?;
        let current_secret = self
            .read_installation_secret()?
            .ok_or_else(|| "本机安装秘密尚未就绪；未写入登记凭证。".to_string())?;
        if format!("{:x}", Sha256::digest(current_secret.as_slice()))
            != verified_installation_binding_sha256
        {
            return Err("本机安装秘密已变化；未保存绑定旧设备状态的凭证。".to_string());
        }
        let current = self
            .store
            .get(ENROLLMENT_ACCOUNT)
            .map_err(|_| "无法读取当前登记凭证；未执行替换。".to_string())?;
        if current.as_deref().map(envelope_sha256) != expected_current_sha256.map(str::to_string) {
            return Err("当前登记凭证已变化；未覆盖较新的本机状态。".to_string());
        }
        self.store
            .set(ENROLLMENT_ACCOUNT, envelope_text)
            .map_err(|_| "无法写入已验签登记凭证。".to_string())?;
        if !matches!(self.store.get(ENROLLMENT_ACCOUNT), Ok(Some(ref value)) if value == envelope_text)
        {
            self.restore_enrollment(current.as_deref());
            return Err("登记凭证写入后复核失败；已尝试恢复原状态。".to_string());
        }
        if self.store.delete(PENDING_OPERATION_ACCOUNT).is_err()
            || !matches!(self.read_pending_operation(), Ok(None))
        {
            self.restore_enrollment(current.as_deref());
            let _ = self.restore_pending_operation(&pending);
            return Err("登记凭证与待决标记无法共同提交；已尝试恢复原状态。".to_string());
        }
        let status = self.base_status_locked();
        self.record_authorized_status(&status);
        Ok(status)
    }

    pub(crate) fn commit_remote_revocation_operation(
        &self,
        operation_id: &str,
        expected_current_sha256: &str,
    ) -> Result<EnrollmentVaultStatus, String> {
        if !valid_sha256(expected_current_sha256) {
            return Err("远程撤销回执格式无效；未删除本机登记。".to_string());
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| "本机安全存储状态锁定失败。".to_string())?;
        let pending = self.require_pending_operation(
            operation_id,
            Some("REVOKE"),
            Some(Some(expected_current_sha256)),
        )?;
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
        if !matches!(self.store.get(ENROLLMENT_ACCOUNT), Ok(None)) {
            let _ = self.store.set(ENROLLMENT_ACCOUNT, &current);
            return Err("本机凭证删除后复核失败；已尝试恢复并请联系管理员。".to_string());
        }
        if self.store.delete(PENDING_OPERATION_ACCOUNT).is_err()
            || !matches!(self.read_pending_operation(), Ok(None))
        {
            let _ = self.store.set(ENROLLMENT_ACCOUNT, &current);
            let _ = self.restore_pending_operation(&pending);
            return Err("远程撤销与待决标记无法共同提交；已尝试恢复本机凭证。".to_string());
        }
        let mut status = self.base_status_locked();
        self.record_authorized_status(&status);
        status.phase = "REMOTE_REVOKED_CONFIRMED".to_string();
        status.message = "律所服务端已接受撤销，本机登记凭证也已清除。".to_string();
        Ok(status)
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
        Ok(self.status_after_authorized_operation_locked())
    }

    /// Only call after an explicit enrollment operation has already opened
    /// Keychain. This refreshes the non-secret marker for later status reads.
    fn status_after_authorized_operation_locked(&self) -> EnrollmentVaultStatus {
        let status = self.status_locked();
        self.record_authorized_status(&status);
        status
    }

    fn record_authorized_status(&self, status: &EnrollmentVaultStatus) {
        self.update_status_cache(|cache| {
            cache.installation_initialized = Some(status.installation_initialized);
            cache.enrollment_envelope_present = Some(status.enrollment_envelope_present);
            cache.pending_operation_recorded = status.phase == "REMOTE_OPERATION_PENDING";
        });
    }

    fn update_status_cache(&self, update: impl FnOnce(&mut EnrollmentStatusCache)) {
        let marker = {
            let Ok(mut cache) = self.status_cache.lock() else {
                return;
            };
            update(&mut cache);
            cache.to_marker()
        };
        persist_status_marker(self.status_marker_path.as_deref(), &marker);
    }

    fn status_locked(&self) -> EnrollmentVaultStatus {
        let mut status = self.base_status_locked();
        match self.read_pending_operation() {
            Ok(Some(pending)) => {
                status.phase = "REMOTE_OPERATION_PENDING".to_string();
                status.message = format!(
                    "{}操作的远程结果尚未确认（始于 {}）；请查询状态，不要重复提交。",
                    operation_kind_label(&pending.operation_kind),
                    pending.created_at
                );
            }
            Ok(None) => {}
            Err(message) => return blocked_status(&message),
        }
        status
    }

    fn base_status_locked(&self) -> EnrollmentVaultStatus {
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

    fn require_pending_operation(
        &self,
        operation_id: &str,
        operation_kind: Option<&str>,
        expected_current_sha256: Option<Option<&str>>,
    ) -> Result<PendingEnrollmentOperation, String> {
        let pending = self
            .read_pending_operation()?
            .ok_or_else(|| "待决远程操作标记不存在；拒绝提交回执。".to_string())?;
        if pending.operation_id != operation_id
            || operation_kind.is_some_and(|kind| pending.operation_kind != kind)
            || expected_current_sha256
                .is_some_and(|expected| pending.expected_current_sha256.as_deref() != expected)
        {
            return Err("远程操作回执与 Keychain 待决标记不一致；本机状态未改变。".to_string());
        }
        Ok(pending)
    }

    fn read_pending_operation(&self) -> Result<Option<PendingEnrollmentOperation>, String> {
        let Some(serialized) = self
            .store
            .get(PENDING_OPERATION_ACCOUNT)
            .map_err(|_| "无法读取 Keychain 待决操作状态。".to_string())?
        else {
            return Ok(None);
        };
        if serialized.is_empty() || serialized.len() > 1024 {
            return Err("Keychain 中的待决操作标记格式异常；案件访问保持禁用。".to_string());
        }
        let pending: PendingEnrollmentOperation = serde_json::from_str(&serialized)
            .map_err(|_| "Keychain 中的待决操作标记格式异常；案件访问保持禁用。".to_string())?;
        if pending.version != PENDING_OPERATION_VERSION {
            return Err("Keychain 中的待决操作版本不受支持；案件访问保持禁用。".to_string());
        }
        validate_pending_fields(
            &pending.operation_id,
            &pending.operation_kind,
            pending.expected_current_sha256.as_deref(),
            Some(&pending.installation_binding_sha256),
            &pending.created_at,
        )?;
        Ok(Some(pending))
    }

    fn restore_pending_operation(
        &self,
        pending: &PendingEnrollmentOperation,
    ) -> Result<(), String> {
        let serialized = serde_json::to_string(pending)
            .map_err(|_| "无法恢复 Keychain 待决操作标记。".to_string())?;
        self.store
            .set(PENDING_OPERATION_ACCOUNT, &serialized)
            .map_err(|_| "无法恢复 Keychain 待决操作标记。".to_string())
    }

    fn restore_enrollment(&self, prior: Option<&str>) {
        match prior {
            Some(value) => {
                let _ = self.store.set(ENROLLMENT_ACCOUNT, value);
            }
            None => {
                let _ = self.store.delete(ENROLLMENT_ACCOUNT);
            }
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

fn cached_status(cache: &EnrollmentStatusCache) -> EnrollmentVaultStatus {
    if cache.pending_operation_recorded {
        return EnrollmentVaultStatus {
            phase: "REMOTE_OPERATION_PENDING".to_string(),
            message: "已记录待确认的律所操作；本页未读取系统钥匙串。请点击“查询待确认的律所操作”重新核验，且不要重复提交。".to_string(),
            installation_initialized: cache.installation_initialized.unwrap_or(false),
            enrollment_envelope_present: cache.enrollment_envelope_present.unwrap_or(false),
        };
    }

    match (
        cache.installation_initialized,
        cache.enrollment_envelope_present,
    ) {
        (None, _) => EnrollmentVaultStatus {
            phase: "NOT_INITIALIZED".to_string(),
            message: "本页未读取系统钥匙串，尚未取得本机初始化记录。若此前已配置，请通过下方明确操作重新核验。".to_string(),
            installation_initialized: false,
            enrollment_envelope_present: false,
        },
        (Some(false), Some(true)) => EnrollmentVaultStatus {
            phase: "BROKEN_LOCAL_CREDENTIAL".to_string(),
            message: "上次明确核验时发现登记记录与本机初始化记录不一致；本页未再次读取系统钥匙串。请在受管操作中重新核验。".to_string(),
            installation_initialized: false,
            enrollment_envelope_present: true,
        },
        (Some(true), Some(true)) => EnrollmentVaultStatus {
            phase: "CREDENTIAL_PRESENT_UNVERIFIED".to_string(),
            message: "已记录律所登记。本页未读取系统钥匙串；这不是本次已验证的登记凭证。进行登记、更新、撤销或进入受管工作区时才会重新核验。".to_string(),
            installation_initialized: true,
            enrollment_envelope_present: true,
        },
        (Some(true), Some(false) | None) => EnrollmentVaultStatus {
            phase: "INSTALLATION_READY".to_string(),
            message: "已记录本机初始化。本页未读取系统钥匙串；这只是初始化记录，不代表本次已验证的安全存储。继续登记时会重新核验。".to_string(),
            installation_initialized: true,
            enrollment_envelope_present: false,
        },
        (Some(false), Some(false) | None) => EnrollmentVaultStatus {
            phase: "NOT_INITIALIZED".to_string(),
            message: "已记录本机登记已停用；本页未读取系统钥匙串。需要重新启用时，请通过下方明确操作重新核验。".to_string(),
            installation_initialized: false,
            enrollment_envelope_present: false,
        },
    }
}

fn load_status_cache(path: Option<&Path>) -> EnrollmentStatusCache {
    let Some(path) = path else {
        return EnrollmentStatusCache::default();
    };
    let Some(raw) = read_private_marker(path) else {
        return EnrollmentStatusCache::default();
    };
    let Ok(marker) = serde_json::from_slice::<EnrollmentStatusMarker>(&raw) else {
        return EnrollmentStatusCache::default();
    };
    if marker.version != STATUS_MARKER_VERSION {
        return EnrollmentStatusCache::default();
    }
    EnrollmentStatusCache {
        installation_initialized: marker.installation_initialized,
        enrollment_envelope_present: marker.enrollment_envelope_present,
        pending_operation_recorded: marker.pending_operation_recorded,
    }
}

fn persist_status_marker(path: Option<&Path>, marker: &EnrollmentStatusMarker) {
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

fn validate_pending_fields(
    operation_id: &str,
    operation_kind: &str,
    expected_current_sha256: Option<&str>,
    installation_binding_sha256: Option<&str>,
    created_at: &str,
) -> Result<(), String> {
    let valid_kind = matches!(operation_kind, "ACTIVATE" | "RENEW" | "REVOKE");
    let expected_shape = if operation_kind == "ACTIVATE" {
        expected_current_sha256.is_none()
    } else {
        expected_current_sha256.is_some_and(valid_sha256)
    };
    if Uuid::parse_str(operation_id).is_err()
        || !valid_kind
        || !expected_shape
        || installation_binding_sha256.is_some_and(|value| !valid_sha256(value))
        || !created_at.ends_with('Z')
        || DateTime::parse_from_rfc3339(created_at)
            .map(|value| value.with_timezone(&Utc))
            .is_err()
    {
        return Err("待决远程操作标记字段无效；未改变本机状态。".to_string());
    }
    Ok(())
}

fn operation_kind_label(operation_kind: &str) -> &'static str {
    match operation_kind {
        "ACTIVATE" => "激活",
        "RENEW" => "续期",
        "REVOKE" => "撤销",
        _ => "登记",
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
        INSTALLATION_ACCOUNT, LOCAL_DISABLE_CONFIRMATION, PENDING_OPERATION_ACCOUNT, StoreFailure,
        envelope_sha256,
    };
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    use sha2::{Digest, Sha256};
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct MemoryStore {
        values: Mutex<HashMap<String, String>>,
        reads: AtomicUsize,
    }

    impl MemoryStore {
        fn reads(&self) -> usize {
            self.reads.load(Ordering::SeqCst)
        }
    }

    impl CredentialStore for MemoryStore {
        fn get(&self, account: &str) -> Result<Option<String>, StoreFailure> {
            self.reads.fetch_add(1, Ordering::SeqCst);
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
    fn settings_status_never_reads_the_keychain_store() {
        let store = Arc::new(MemoryStore::default());
        {
            let mut values = store.values.lock().unwrap();
            values.insert(
                INSTALLATION_ACCOUNT.to_string(),
                STANDARD.encode([3_u8; 32]),
            );
            values.insert(
                ENROLLMENT_ACCOUNT.to_string(),
                r#"{"credential":{},"signature":"recorded"}"#.to_string(),
            );
        }
        let vault = EnrollmentVault::with_store(store.clone());

        let status = vault.status();

        assert_eq!(store.reads(), 0, "opening Settings must not probe Keychain");
        assert_eq!(status.phase, "NOT_INITIALIZED");
        assert!(status.message.contains("未读取系统钥匙串"));
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
        let expected = envelope_sha256(envelope);
        let operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        vault
            .begin_remote_operation(
                operation_id,
                "REVOKE",
                Some(&expected),
                "2026-08-10T12:00:00Z",
            )
            .unwrap();
        assert_eq!(vault.status().phase, "REMOTE_OPERATION_PENDING");
        assert!(
            vault
                .commit_remote_revocation_operation(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    &expected,
                )
                .is_err()
        );
        assert!(
            store
                .values
                .lock()
                .unwrap()
                .contains_key(ENROLLMENT_ACCOUNT)
        );
        let status = vault
            .commit_remote_revocation_operation(operation_id, &expected)
            .unwrap();
        assert_eq!(status.phase, "REMOTE_REVOKED_CONFIRMED");
        assert!(
            !store
                .values
                .lock()
                .unwrap()
                .contains_key(ENROLLMENT_ACCOUNT)
        );
        assert!(vault.pending_remote_operation().is_err());
    }

    #[test]
    fn pending_activation_survives_restart_and_commits_with_the_exact_marker() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
            .unwrap();
        let operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        let pending = vault
            .begin_remote_operation(operation_id, "ACTIVATE", None, "2026-08-10T12:00:00Z")
            .unwrap();
        assert_eq!(pending.operation_kind, "ACTIVATE");

        let restarted = EnrollmentVault::with_store(store.clone());
        assert_eq!(
            restarted.pending_remote_operation().unwrap().operation_id,
            operation_id
        );
        assert!(
            restarted
                .begin_remote_operation(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "ACTIVATE",
                    None,
                    "2026-08-10T12:01:00Z",
                )
                .is_err()
        );
        let envelope = r#"{"credential":{},"signature":"activated"}"#;
        let status = restarted
            .commit_remote_enrollment_operation(
                operation_id,
                "ACTIVATE",
                envelope,
                &envelope_sha256(envelope),
                &format!("{:x}", Sha256::digest([7_u8; 32])),
                None,
            )
            .unwrap();
        assert_eq!(status.phase, "CREDENTIAL_PRESENT_UNVERIFIED");
        assert_eq!(
            store.values.lock().unwrap().get(ENROLLMENT_ACCOUNT),
            Some(&envelope.to_string())
        );
        assert!(restarted.pending_remote_operation().is_err());
    }

    #[test]
    fn rejected_operation_clears_only_the_exact_marker_and_preserves_enrollment() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
            .unwrap();
        let envelope = r#"{"credential":{},"signature":"current"}"#;
        store
            .values
            .lock()
            .unwrap()
            .insert(ENROLLMENT_ACCOUNT.to_string(), envelope.to_string());
        let expected = envelope_sha256(envelope);
        let operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        vault
            .begin_remote_operation(
                operation_id,
                "RENEW",
                Some(&expected),
                "2026-08-10T12:00:00Z",
            )
            .unwrap();
        assert!(
            vault
                .clear_rejected_remote_operation("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
                .is_err()
        );
        assert!(vault.pending_remote_operation().is_ok());
        let status = vault.clear_rejected_remote_operation(operation_id).unwrap();
        assert_eq!(status.phase, "REMOTE_OPERATION_REJECTED");
        assert_eq!(
            store.values.lock().unwrap().get(ENROLLMENT_ACCOUNT),
            Some(&envelope.to_string())
        );
    }

    #[test]
    fn malformed_pending_marker_is_not_probed_by_status_and_blocks_explicit_operations() {
        let store = Arc::new(MemoryStore::default());
        let vault = EnrollmentVault::with_store(store.clone());
        vault
            .initialize_installation_with_secret(INITIALIZE_CONFIRMATION, Some([7_u8; 32]))
            .unwrap();
        store.values.lock().unwrap().insert(
            PENDING_OPERATION_ACCOUNT.to_string(),
            r#"{"version":1,"operation_id":"forged"}"#.to_string(),
        );
        assert_eq!(vault.status().phase, "INSTALLATION_READY");
        assert!(vault.pending_remote_operation().is_err());
        assert!(vault.pending_remote_operation().is_err());
        assert!(
            vault
                .begin_remote_operation(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "ACTIVATE",
                    None,
                    "2026-08-10T12:00:00Z",
                )
                .is_err()
        );
    }
}
