mod commands;
mod sidecar;

use std::sync::Mutex;
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;

pub struct SidecarChild(pub Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .manage(SidecarChild(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            commands::context::get_focused_context,
            commands::inject::inject_text,
            commands::windows::position_overlay,
            commands::windows::hide_main_window,
            commands::windows::open_settings_window,
            commands::windows::open_finetune_window,
        ])
        .setup(setup_app)
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn setup_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let window = app.get_webview_window("main").expect("main window missing");

    // Disable DWM shadow and transitions via raw FFI (avoids windows crate version conflicts).
    {
        extern "system" {
            fn DwmSetWindowAttribute(
                hwnd: *mut core::ffi::c_void,
                dwattr: u32,
                pvattr: *const core::ffi::c_void,
                cbattr: u32,
            ) -> i32;
        }
        if let Ok(hwnd) = window.hwnd() {
            unsafe {
                let policy: u32 = 1; // DWMNCRP_DISABLED
                DwmSetWindowAttribute(hwnd.0, 2, std::ptr::addr_of!(policy) as *const _, 4);
                let no_anim: u32 = 1; // DWMWA_TRANSITIONS_FORCEDISABLED
                DwmSetWindowAttribute(hwnd.0, 3, std::ptr::addr_of!(no_anim) as *const _, 4);
            }
        }
    }

    // Park the window off-screen and show it once. We never hide/show again — only
    // set_position() moves it. This avoids the Windows transparent-window show-flash
    // (show() triggers a compositing cycle before WebView2 has rendered its first frame,
    // causing a brief rectangle artifact). set_position() is atomic with no such flash.
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

    // Register global hotkey Ctrl+Shift+Space
    {
        let handle2 = app.handle().clone();
        app.global_shortcut().on_shortcut(
            "CommandOrControl+Shift+Space",
            move |_, _, event| match event.state() {
                ShortcutState::Pressed => {
                    // Emit only — React renders the pill first, then calls
                    // invoke("position_overlay") via useEffect after the browser
                    // has painted. Window moves on-screen with content already ready.
                    println!("[vox] hotkey: PRESSED");
                    handle2.emit("hotkey-pressed", ()).ok();
                }
                ShortcutState::Released => {
                    println!("[vox] hotkey: RELEASED — moving window off-screen");
                    if let Some(w) = handle2.get_webview_window("main") {
                        let _ = w.set_position(tauri::PhysicalPosition::new(-10000i32, -10000i32));
                    }
                    handle2.emit("hotkey-released", ()).ok();
                }
            },
        )?;
    }

    // System tray
    let open_item = MenuItemBuilder::with_id("open_settings", "Settings").build(app)?;
    let devtools_item = MenuItemBuilder::with_id("devtools", "Debug: Open DevTools").build(app)?;
    let quit_item = MenuItemBuilder::with_id("quit", "Quit Vox").build(app)?;
    let menu = MenuBuilder::new(app)
        .items(&[&open_item, &devtools_item, &quit_item])
        .build()?;

    let app_handle = app.handle().clone();
    let icon = app
        .default_window_icon()
        .cloned()
        .expect("app icon not configured");

    TrayIconBuilder::new()
        .icon(icon)
        .menu(&menu)
        .tooltip("Vox — Press Ctrl+Shift+Space to dictate")
        .on_menu_event(move |_, event| match event.id.as_ref() {
            "open_settings" => {
                if let Err(e) = commands::windows::open_settings_window_inner(&app_handle) {
                    eprintln!("Settings window error: {e}");
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

    Ok(())
}
