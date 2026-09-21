use std::sync::mpsc::{Receiver, sync_channel};
use tauri::AppHandle;
use zeroize::Zeroizing;

use crate::model_provider_vault::ModelProvider;

pub(crate) type ModelApiKeyPromptResult = Result<Option<Zeroizing<String>>, String>;

/// The key remains inside this native prompt and Rust process. It is never a
/// WebView value, a Tauri command parameter, or a browser-side state value.
pub(crate) fn schedule_model_api_key_prompt(
    app: &AppHandle,
    provider: ModelProvider,
) -> Result<Receiver<ModelApiKeyPromptResult>, String> {
    let (sender, receiver) = sync_channel(1);
    app.run_on_main_thread(move || {
        let _ = sender.send(show_model_api_key_prompt(provider));
    })
    .map_err(|_| "无法打开 macOS 原生 API Key 输入框；未写入任何密钥。".to_string())?;
    Ok(receiver)
}

#[cfg(target_os = "macos")]
fn show_model_api_key_prompt(provider: ModelProvider) -> ModelApiKeyPromptResult {
    use objc2::MainThreadMarker;
    use objc2::rc::autoreleasepool;
    use objc2_app_kit::{NSAlert, NSAlertFirstButtonReturn, NSAlertStyle, NSSecureTextField};
    use objc2_foundation::{NSPoint, NSRect, NSSize, NSString};

    autoreleasepool(|_| {
        let mtm = MainThreadMarker::new()
            .ok_or_else(|| "原生 API Key 输入框未在 macOS 主线程运行；未写入密钥。".to_string())?;
        let alert = NSAlert::new(mtm);
        let field = NSSecureTextField::new(mtm);
        let title = NSString::from_str(&format!("配置 {} API Key", provider.display_name()));
        let explanation = NSString::from_str(
            "密钥只保存到这台 Mac 的系统钥匙串。页面、日志、案卷数据库和外部请求审计都不会保存或显示密钥。配置密钥本身不授权上传任何案件材料。",
        );
        let placeholder = NSString::from_str("API Key");
        let save = NSString::from_str("安全保存");
        let cancel = NSString::from_str("取消");
        alert.setAlertStyle(NSAlertStyle::Informational);
        alert.setMessageText(&title);
        alert.setInformativeText(&explanation);
        field.setFrame(NSRect::new(
            NSPoint::new(0.0, 0.0),
            NSSize::new(420.0, 26.0),
        ));
        field.setPlaceholderString(Some(&placeholder));
        alert.setAccessoryView(Some(&field));
        alert.addButtonWithTitle(&save);
        alert.addButtonWithTitle(&cancel);
        let response = alert.runModal();
        let key = Zeroizing::new(field.stringValue().to_string());
        field.setStringValue(&NSString::from_str(""));
        if response != NSAlertFirstButtonReturn {
            return Ok(None);
        }
        Ok(Some(key))
    })
}

#[cfg(not(target_os = "macos"))]
fn show_model_api_key_prompt(_provider: ModelProvider) -> ModelApiKeyPromptResult {
    Err("原生 API Key 输入仅支持 macOS 桌面应用。".to_string())
}
