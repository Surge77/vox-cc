use tauri::Manager;

fn open_window(
    app: &tauri::AppHandle,
    label: &str,
    title: &str,
    path: &str,
    width: f64,
    height: f64,
) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(label) {
        return w.set_focus().map_err(|e| e.to_string());
    }
    tauri::WebviewWindowBuilder::new(app, label, tauri::WebviewUrl::App(path.into()))
        .title(title)
        .inner_size(width, height)
        .resizable(false)
        .skip_taskbar(false)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

pub fn open_settings_window_inner(app: &tauri::AppHandle) -> Result<(), String> {
    open_window(app, "settings", "Vox Settings", "/settings", 600.0, 500.0)
}

pub fn open_finetune_window_inner(app: &tauri::AppHandle) -> Result<(), String> {
    open_window(
        app,
        "finetune",
        "Vox Fine-tuning",
        "/finetune",
        700.0,
        600.0,
    )
}

#[tauri::command]
pub fn position_overlay(app: tauri::AppHandle) -> Result<(), String> {
    let w = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    let ms = app
        .primary_monitor()
        .ok()
        .flatten()
        .map(|m| *m.size())
        .unwrap_or(tauri::PhysicalSize::new(1920, 1080));
    let ws = w.outer_size().unwrap_or(tauri::PhysicalSize::new(420, 80));
    let x = (ms.width as i32 - ws.width as i32) / 2;
    let y = ms.height as i32 - ws.height as i32 - 80;
    w.set_position(tauri::PhysicalPosition::new(x, y))
        .map_err(|e| e.to_string())?;
    w.show().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn hide_main_window(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("main") {
        w.hide().map_err(|e| e.to_string())
    } else {
        Ok(())
    }
}

#[tauri::command]
pub fn open_settings_window(app: tauri::AppHandle) -> Result<(), String> {
    open_settings_window_inner(&app)
}

#[tauri::command]
pub fn open_finetune_window(app: tauri::AppHandle) -> Result<(), String> {
    open_finetune_window_inner(&app)
}
