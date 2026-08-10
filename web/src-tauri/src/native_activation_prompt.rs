use std::sync::mpsc::{Receiver, sync_channel};
use tauri::AppHandle;
use zeroize::Zeroizing;

pub(crate) type ActivationPromptResult = Result<Option<Zeroizing<String>>, String>;

/// Schedule a secure OS-owned prompt. The activation secret is returned only
/// to the Rust parent process and never becomes a Tauri command argument or a
/// WebView value.
pub(crate) fn schedule_activation_prompt(
    app: &AppHandle,
) -> Result<Receiver<ActivationPromptResult>, String> {
    let (sender, receiver) = sync_channel(1);
    app.run_on_main_thread(move || {
        let _ = sender.send(show_activation_prompt());
    })
    .map_err(|_| "无法打开 macOS 原生激活码输入框；未联系律所服务。".to_string())?;
    Ok(receiver)
}

pub(crate) fn valid_activation_secret(value: &str) -> bool {
    let bytes = value.as_bytes();
    (24..=256).contains(&bytes.len()) && bytes.iter().all(u8::is_ascii_graphic)
}

#[cfg(target_os = "macos")]
fn show_activation_prompt() -> ActivationPromptResult {
    use objc2::MainThreadMarker;
    use objc2::rc::autoreleasepool;
    use objc2_app_kit::{NSAlert, NSAlertFirstButtonReturn, NSAlertStyle, NSSecureTextField};
    use objc2_foundation::{NSPoint, NSRect, NSSize, NSString};

    autoreleasepool(|_| {
        let mtm = MainThreadMarker::new()
            .ok_or_else(|| "原生激活码输入框未在 macOS 主线程运行；未执行激活。".to_string())?;
        let alert = NSAlert::new(mtm);
        let field = NSSecureTextField::new(mtm);
        let title = NSString::from_str("激活律所律师登记");
        let explanation = NSString::from_str(
            "请输入律所管理员发放的一次性激活码。激活码不会显示在页面、日志或本机状态中，也不能用来选择人员、律所或案件角色。",
        );
        let placeholder = NSString::from_str("一次性激活码");
        let activate = NSString::from_str("安全激活");
        let cancel = NSString::from_str("取消");
        alert.setAlertStyle(NSAlertStyle::Informational);
        alert.setMessageText(&title);
        alert.setInformativeText(&explanation);
        field.setFrame(NSRect::new(
            NSPoint::new(0.0, 0.0),
            NSSize::new(360.0, 26.0),
        ));
        field.setPlaceholderString(Some(&placeholder));
        alert.setAccessoryView(Some(&field));
        alert.addButtonWithTitle(&activate);
        alert.addButtonWithTitle(&cancel);
        let response = alert.runModal();
        let secret = Zeroizing::new(field.stringValue().to_string());
        field.setStringValue(&NSString::from_str(""));
        if response != NSAlertFirstButtonReturn {
            return Ok(None);
        }
        if !valid_activation_secret(secret.as_str()) {
            return Err("激活码格式无效；未联系律所服务。".to_string());
        }
        Ok(Some(secret))
    })
}

#[cfg(not(target_os = "macos"))]
fn show_activation_prompt() -> ActivationPromptResult {
    Err("原生激活码输入仅支持 macOS 桌面应用。".to_string())
}

#[cfg(test)]
mod tests {
    use super::valid_activation_secret;

    #[test]
    fn activation_secret_shape_is_strict_ascii_and_bounded() {
        assert!(valid_activation_secret(&"A".repeat(24)));
        assert!(valid_activation_secret(&"z".repeat(256)));
        assert!(!valid_activation_secret(&"A".repeat(23)));
        assert!(!valid_activation_secret(&"A".repeat(257)));
        assert!(!valid_activation_secret(&format!("{} ", "A".repeat(24))));
        assert!(!valid_activation_secret(&format!("{}中", "A".repeat(24))));
    }
}
