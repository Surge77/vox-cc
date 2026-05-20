use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

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
                // Capture the focused window BEFORE the overlay is shown.
                // GetForegroundWindow() is volatile — any show/position call can
                // change the foreground. Reading it here guarantees we get the
                // user's actual target, not the Vox overlay or the Vox main process.
                let raw_hwnd = unsafe { GetForegroundWindow().0 };
                if let Ok(mut guard) = handle.state::<crate::TargetHwnd>().0.lock() {
                    *guard = if raw_hwnd != 0 { Some(raw_hwnd) } else { None };
                }

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
                    // SW_SHOWNOACTIVATE (= 4): show without stealing keyboard focus.
                    // WS_EX_NOACTIVATE only blocks click-driven activation; programmatic
                    // ShowWindow(SW_SHOW) bypasses it. Raw FFI avoids the windows 0.52 vs
                    // 0.61 HWND type mismatch between our crate and Tauri's dep tree.
                    if let Ok(overlay_hwnd) = w.hwnd() {
                        extern "system" {
                            fn ShowWindow(hwnd: *mut core::ffi::c_void, n_cmd_show: i32) -> i32;
                        }
                        unsafe { ShowWindow(overlay_hwnd.0, 4); }
                    }
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
    // Re-arm the undo hotkey too — `unregister_all` cleared it.
    register_undo_hotkey(&app).ok();
    if let Some(path) = hotkey_file() {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&path, &hotkey).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Registers Ctrl+Shift+Z as a global undo-and-re-record shortcut.
/// On press: undo the last injection in the target window (Ctrl+Z) and emit
/// `undo-pressed` so the frontend can immediately restart recording with the
/// original context.
pub fn register_undo_hotkey(app: &tauri::AppHandle) -> Result<(), String> {
    let handle = app.clone();
    app.global_shortcut()
        .on_shortcut("CommandOrControl+Shift+Z", move |_, _, event| {
            // Only act on press — Released would double-fire the undo.
            if !matches!(event.state(), ShortcutState::Pressed) {
                return;
            }
            // Snapshot last-inject state and undo on the target's behalf.
            // Failures are silent — there is nothing to undo if state is empty.
            let snapshot = handle
                .try_state::<crate::LastInject>()
                .and_then(|s| s.0.lock().ok().map(|g| *g))
                .flatten();
            if let Some((hwnd_raw, _len)) = snapshot {
                crate::commands::inject::undo_in_target(hwnd_raw);
                if let Some(state) = handle.try_state::<crate::LastInject>() {
                    if let Ok(mut g) = state.0.lock() {
                        *g = None;
                    }
                }
            }
            handle.emit("undo-pressed", ()).ok();
        })
        .map_err(|e| e.to_string())
}
