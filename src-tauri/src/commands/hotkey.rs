use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

fn hotkey_file() -> Option<std::path::PathBuf> {
    dirs::home_dir().map(|h| h.join(".vox").join("data").join("hotkey.txt"))
}

pub fn read_saved_hotkey() -> String {
    hotkey_file()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "CommandOrControl+Shift+Space".to_string())
}

pub fn register_hotkey(app: &tauri::AppHandle, hotkey: &str) -> Result<(), String> {
    let handle = app.clone();
    app.global_shortcut()
        .on_shortcut(hotkey, move |_, _, event| match event.state() {
            ShortcutState::Pressed => {
                if let Some(w) = handle.get_webview_window("main") {
                    let ms = handle
                        .primary_monitor()
                        .ok()
                        .flatten()
                        .map(|m| *m.size())
                        .unwrap_or(tauri::PhysicalSize::new(1920, 1080));
                    let ws = w
                        .outer_size()
                        .unwrap_or(tauri::PhysicalSize::new(420, 80));
                    let x = (ms.width as i32 - ws.width as i32) / 2;
                    let y = ms.height as i32 - ws.height as i32 - 80;
                    let _ = w.set_position(tauri::PhysicalPosition::new(x, y));
                    let _ = w.show();
                }
                handle.emit("hotkey-pressed", ()).ok();
            }
            ShortcutState::Released => {
                handle.emit("hotkey-released", ()).ok();
            }
        })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn set_hotkey(app: tauri::AppHandle, hotkey: String) -> Result<(), String> {
    app.global_shortcut()
        .unregister_all()
        .map_err(|e| e.to_string())?;
    register_hotkey(&app, &hotkey)?;
    if let Some(path) = hotkey_file() {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&path, &hotkey).map_err(|e| e.to_string())?;
    }
    Ok(())
}
