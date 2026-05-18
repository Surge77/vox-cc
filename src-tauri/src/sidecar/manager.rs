use crate::SidecarChild;
use tauri::{Emitter, Manager};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

pub fn spawn_sidecar(
    app: &tauri::AppHandle,
) -> Result<
    (
        tokio::sync::mpsc::Receiver<CommandEvent>,
        tauri_plugin_shell::process::CommandChild,
    ),
    String,
> {
    // In dev mode, spawn Python directly from venv — no PyInstaller rebuild needed on Python edits.
    // In release mode, Tauri bundles everything correctly; use the managed sidecar path.
    #[cfg(debug_assertions)]
    {
        let sidecar_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("sidecar");
        let python_exe = sidecar_dir
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        let model_dir = sidecar_dir
            .join("models")
            .canonicalize()
            .unwrap_or_else(|_| std::path::PathBuf::from("sidecar/models"));

        app.shell()
            .command(python_exe.to_str().unwrap_or("python.exe"))
            .current_dir(sidecar_dir.to_str().unwrap_or("."))
            .arg("main.py")
            .env("VOX_MODEL_DIR", model_dir.to_str().unwrap_or(""))
            .spawn()
            .map_err(|e| e.to_string())
    }
    #[cfg(not(debug_assertions))]
    {
        app.shell()
            .sidecar("sidecar")
            .map_err(|e| e.to_string())?
            .spawn()
            .map_err(|e| e.to_string())
    }
}

fn read_port_lock() -> Option<u16> {
    let home = dirs::home_dir()?;
    let path = home.join(".vox").join("data").join("port.lock");
    let content = std::fs::read_to_string(path).ok()?;
    content.trim().parse::<u16>().ok()
}

pub async fn await_sidecar_ready(app: tauri::AppHandle) {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(500))
        .build()
        .unwrap_or_else(|_| reqwest::Client::new());

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
    let mut poll_count: u32 = 0;

    println!("[vox] health-poll: starting (120s deadline)");

    loop {
        if std::time::Instant::now() > deadline {
            println!("[vox] health-poll: TIMEOUT after {} polls — emitting sidecar-degraded", poll_count);
            app.emit(
                super::events::SIDECAR_DEGRADED,
                serde_json::json!({ "missing": ["all"] }),
            )
            .ok();
            return;
        }

        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        poll_count += 1;

        let port = read_port_lock().unwrap_or(8000);
        let url = format!("http://127.0.0.1:{}/health", port);

        if poll_count % 25 == 1 {
            println!("[vox] health-poll: attempt {} — GET {}", poll_count, url);
        }

        match client.get(&url).send().await {
            Err(e) => {
                if poll_count % 25 == 1 {
                    println!("[vox] health-poll: no response yet ({})", e);
                }
            }
            Ok(resp) => {
                if resp.status().is_success() {
                    let body: serde_json::Value = resp.json().await.unwrap_or_default();
                    println!("[vox] health-poll: OK after {} polls — body: {}", poll_count, body);
                    let models = &body["models"];

                    let final_pass_type = body["final_pass_type"].as_str().unwrap_or("");
                    let mut missing: Vec<String> = Vec::new();
                    if models["turbo"] != true {
                        missing.push("turbo".into());
                    }
                    // "skip" means VRAM too low for a final-pass model — intentional, not a failure.
                    if models["final_pass"] != true && final_pass_type != "skip" {
                        missing.push("final_pass".into());
                    }
                    if models["llm"] != true {
                        missing.push("llm".into());
                    }

                    if !missing.is_empty() {
                        println!("[vox] health-poll: degraded — missing: {:?}", missing);
                        app.emit(
                            super::events::SIDECAR_DEGRADED,
                            serde_json::json!({ "missing": missing }),
                        )
                        .ok();
                    }

                    // Emit exact port so frontend WS connects to the right port without scanning
                    app.emit(super::events::SIDECAR_PORT, serde_json::json!({ "port": port })).ok();
                    println!("[vox] health-poll: emitting models-ready (port={})", port);
                    app.emit(super::events::MODELS_READY, ()).ok();
                    return;
                } else {
                    if poll_count % 25 == 1 {
                        println!("[vox] health-poll: HTTP {} (not ready yet)", resp.status());
                    }
                }
            }
        }
    }
}

pub async fn monitor_sidecar(
    mut rx: tokio::sync::mpsc::Receiver<CommandEvent>,
    app: tauri::AppHandle,
) {
    while let Some(event) = rx.recv().await {
        match &event {
            CommandEvent::Stdout(bytes) => {
                if let Ok(line) = std::str::from_utf8(bytes) {
                    print!("[sidecar] {line}");
                }
            }
            CommandEvent::Stderr(bytes) => {
                if let Ok(line) = std::str::from_utf8(bytes) {
                    eprint!("[sidecar-err] {line}");
                }
            }
            _ => {}
        }
        if let CommandEvent::Terminated(payload) = event {
            let clean_exit = payload.code == Some(0);
            if !clean_exit {
                app.emit(super::events::SIDECAR_RESTARTING, ()).ok();
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;

                match spawn_sidecar(&app) {
                    Ok((new_rx, new_child)) => {
                        if let Some(state) = app.try_state::<SidecarChild>() {
                            if let Ok(mut guard) = state.0.lock() {
                                *guard = Some(new_child);
                            }
                        }
                        tauri::async_runtime::spawn(await_sidecar_ready(app.clone()));
                        rx = new_rx;
                        continue;
                    }
                    Err(e) => {
                        eprintln!("Sidecar respawn failed: {e}");
                        app.emit(
                            super::events::SIDECAR_DEGRADED,
                            serde_json::json!({ "missing": ["all"] }),
                        )
                        .ok();
                        return;
                    }
                }
            }
            return; // clean exit (app shutdown)
        }
    }
}
