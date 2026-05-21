mod commands;
mod sidecar;

use std::sync::Mutex;
use tauri::{Emitter, Manager};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;

pub struct SidecarChild(pub Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

/// Stores the HWND of the user's target window, captured at the instant the hotkey is
/// pressed — before the overlay is shown. Both `inject_text` and `get_focused_context`
/// read this instead of re-querying `GetForegroundWindow()` at their own call time.
pub struct TargetHwnd(pub Mutex<Option<isize>>);

/// Records the most recent successful injection so the user can undo + re-record
/// via Ctrl+Shift+Z. Tuple: (target_hwnd_raw, injected_text_len). Cleared after
/// a successful undo so repeated presses do not loop.
pub struct LastInject(pub Mutex<Option<(isize, usize)>>);

/// In-flight target HWND for streaming inject. Set by `begin_stream_inject`,
/// read by every `inject_text_chunk` call during the stream, cleared by
/// `end_stream_inject`. Kept separate from `TargetHwnd` (which is a single-shot
/// take()-on-inject slot) so the stream survives the consume.
pub struct StreamTarget(pub Mutex<Option<isize>>);

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .manage(SidecarChild(Mutex::new(None)))
        .manage(TargetHwnd(Mutex::new(None)))
        .manage(LastInject(Mutex::new(None)))
        .manage(StreamTarget(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            commands::audio::ensure_mic_volume,
            commands::context::get_focused_context,
            commands::hotkey::set_hotkey,
            commands::inject::inject_text,
            commands::inject::inject_keypress,
            commands::inject::begin_stream_inject,
            commands::inject::inject_text_chunk,
            commands::inject::end_stream_inject,
            commands::inject::undo_last_inject,
            commands::windows::position_overlay,
            commands::windows::hide_main_window,
            commands::windows::open_settings_window,
            commands::windows::open_finetune_window,
            commands::windows::open_history_window,
        ])
        .setup(setup_app)
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn setup_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let window = app.get_webview_window("main").expect("main window missing");

    // Raw Win32 FFI — avoids windows crate version conflicts with Tauri internals.
    {
        extern "system" {
            fn DwmSetWindowAttribute(
                hwnd: *mut core::ffi::c_void,
                dwattr: u32,
                pvattr: *const core::ffi::c_void,
                cbattr: u32,
            ) -> i32;
            fn GetWindowLongW(hwnd: *mut core::ffi::c_void, n_index: i32) -> i32;
            fn SetWindowLongW(hwnd: *mut core::ffi::c_void, n_index: i32, dw_new_long: i32) -> i32;
        }
        if let Ok(hwnd) = window.hwnd() {
            unsafe {
                // Disable DWM NC shadow and transition animation.
                let policy: u32 = 1; // DWMNCRP_DISABLED
                DwmSetWindowAttribute(hwnd.0, 2, std::ptr::addr_of!(policy) as *const _, 4);
                let no_anim: u32 = 1; // DWMWA_TRANSITIONS_FORCEDISABLED
                DwmSetWindowAttribute(hwnd.0, 3, std::ptr::addr_of!(no_anim) as *const _, 4);

                // WS_EX_NOACTIVATE: click doesn't steal keyboard focus.
                // WS_EX_TRANSPARENT: mouse events pass through to window beneath —
                // capsule is display-only, no interaction needed.
                const GWL_EXSTYLE: i32 = -20;
                const WS_EX_NOACTIVATE: i32 = 0x0800_0000;
                const WS_EX_TRANSPARENT: i32 = 0x0000_0020;
                let ex = GetWindowLongW(hwnd.0, GWL_EXSTYLE);
                SetWindowLongW(hwnd.0, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT);
            }
        }
    }

    // Capsule is display-only — all clicks must pass through to the window below.
    // set_ignore_cursor_events propagates WS_EX_TRANSPARENT to the inner WebView2
    // HWND, not just the outer host HWND where the manual Win32 code above runs.
    let _ = window.set_ignore_cursor_events(true);

    // Park off-screen and show once so DWM allocates compositing resources.
    // Window stays always-visible; hotkey PRESSED moves it on-screen,
    // hotkey RELEASED moves it back to -10000 (effectively hidden, no flash).
    let _ = window.set_position(tauri::PhysicalPosition::new(-10000i32, -10000i32));
    let _ = window.show();

    // Spawn sidecar + start health polling + crash monitor
    let handle = app.handle().clone();
    match sidecar::manager::spawn_sidecar(&handle) {
        Ok((rx, child)) => {
            *app.state::<SidecarChild>().0.lock().expect("state lock") = Some(child);
            tauri::async_runtime::spawn(sidecar::manager::await_sidecar_ready(handle.clone()));
            tauri::async_runtime::spawn(sidecar::manager::monitor_sidecar(rx, handle.clone()));
        }
        Err(e) => {
            eprintln!("Failed to spawn sidecar: {e}");
            app.emit(
                "sidecar-degraded",
                serde_json::json!({ "missing": ["all"] }),
            )
            .ok();
        }
    }

    // Boost default capture device to 100% if Windows mic volume is low.
    // Near-zero RMS (rms < 0.002) in the sidecar logs means mic volume was set
    // too low in Windows Sound Settings — this fixes it automatically at launch.
    let mic_status = commands::audio::ensure_mic_volume();
    eprintln!("{mic_status}");

    // Register global hotkey (persisted in ~/.vox/data/hotkey.txt, default Ctrl+Shift+Space)
    {
        let saved = commands::hotkey::read_saved_hotkey();
        commands::hotkey::register_hotkey(app.handle(), &saved)?;
    }

    // Register Ctrl+Shift+Z undo-and-re-record hotkey.
    // Failure is non-fatal — if another app holds this combo, log and continue.
    if let Err(e) = commands::hotkey::register_undo_hotkey(app.handle()) {
        eprintln!("[hotkey] undo hotkey not registered: {e}");
    }
    // Register Ctrl+Shift+C continuous-mode toggle.
    if let Err(e) = commands::hotkey::register_continuous_hotkey(app.handle()) {
        eprintln!("[hotkey] continuous hotkey not registered: {e}");
    }

    // System tray
    let open_item = MenuItemBuilder::with_id("open_settings", "Settings").build(app)?;
    let history_item = MenuItemBuilder::with_id("open_history", "History").build(app)?;
    let devtools_item = MenuItemBuilder::with_id("devtools", "Debug: Open DevTools").build(app)?;
    let quit_item = MenuItemBuilder::with_id("quit", "Quit Vox").build(app)?;
    let menu = MenuBuilder::new(app)
        .items(&[&open_item, &history_item, &devtools_item, &quit_item])
        .build()?;

    let app_handle = app.handle().clone();
    let icon = app
        .default_window_icon()
        .cloned()
        .expect("app icon not configured");

    TrayIconBuilder::new()
        .icon(icon)
        .menu(&menu)
        .tooltip("Vox — Hold your hotkey to dictate")
        .on_menu_event(move |_, event| match event.id.as_ref() {
            "open_settings" => {
                if let Err(e) = commands::windows::open_settings_window_inner(&app_handle) {
                    eprintln!("Settings window error: {e}");
                }
            }
            "open_history" => {
                if let Err(e) = commands::windows::open_history_window_inner(&app_handle) {
                    eprintln!("History window error: {e}");
                }
            }
            "devtools" => {
                if let Some(w) = app_handle.get_webview_window("main") {
                    let _ = w.show();
                    w.open_devtools();
                }
            }
            "quit" => {
                if let Some(state) = app_handle.try_state::<SidecarChild>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
                std::process::exit(0);
            }
            _ => {}
        })
        .build(app)?;

    // First-run welcome toast — fires once per fresh install
    {
        use tauri_plugin_notification::NotificationExt;
        let marker = dirs::home_dir()
            .map(|h| h.join(".vox").join("data").join("first_run_complete.txt"));
        if let Some(ref path) = marker {
            if !path.exists() {
                let saved = commands::hotkey::read_saved_hotkey();
                let display = saved.replace("CommandOrControl", "Ctrl");
                let _ = app
                    .notification()
                    .builder()
                    .title("Vox is running")
                    .body(format!(
                        "Hold {display} anywhere to dictate. Right-click the tray icon for Settings."
                    ))
                    .show();
                if let Some(parent) = path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                let _ = std::fs::write(path, "");
            }
        }
    }

    Ok(())
}
