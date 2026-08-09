use serde::Serialize;
use std::path::Path;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;
use uuid::Uuid;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct SelectedCaseFolder {
    selected_root: String,
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
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![select_case_folder])
        .run(tauri::generate_context!())
        .expect("桌面应用启动失败");
}

#[cfg(test)]
mod tests {
    use super::{validate_matter_id, validate_selected_root};
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
}
