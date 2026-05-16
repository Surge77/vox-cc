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
            commands::windows::hide_main_window,
            commands::windows::open_settings_window,
            commands::windows::open_finetune_window,
        ])
        .setup(setup_app)
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn setup_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    // Position main window at bottom center of primary monitor
    let window = app.get_webview_window("main").expect("main window missing");
    if let Some(monitor) = window.primary_monitor().ok().flatten() {
        let ms = monitor.size();
        let ws = window.outer_size().unwrap_or_default();
        let x = (ms.width as i32 - ws.width as i32) / 2;
        let y = ms.height as i32 - ws.height as i32 - 80;
        let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
    }
    // Do NOT show window here — stays hidden until hotkey pressed

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
                    println!("[vox] hotkey: PRESSED");
                    handle2.emit("hotkey-pressed", ()).ok();
                }
                ShortcutState::Released => {
                    println!("[vox] hotkey: RELEASED");
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
