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
    app.shell()
        .sidecar("sidecar")
        .map_err(|e| e.to_string())?
        .spawn()
        .map_err(|e| e.to_string())
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

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);

    loop {
        if std::time::Instant::now() > deadline {
            app.emit(
                super::events::SIDECAR_DEGRADED,
                serde_json::json!({ "missing": ["all"] }),
            )
            .ok();
            return;
        }

        tokio::time::sleep(std::time::Duration::from_millis(200)).await;

        let port = read_port_lock().unwrap_or(8000);
        let url = format!("http://127.0.0.1:{}/health", port);

        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                let body: serde_json::Value = resp.json().await.unwrap_or_default();
                let models = &body["models"];

                let mut missing: Vec<String> = Vec::new();
                if models["final_pass"] != true {
                    missing.push("final_pass".into());
                }
                if models["llm"] != true {
                    missing.push("llm".into());
                }

                if !missing.is_empty() {
                    app.emit(
                        super::events::SIDECAR_DEGRADED,
                        serde_json::json!({ "missing": missing }),
                    )
                    .ok();
                }

                app.emit(super::events::MODELS_READY, ()).ok();
                return;
            }
        }
    }
}

pub async fn monitor_sidecar(
    mut rx: tokio::sync::mpsc::Receiver<CommandEvent>,
    app: tauri::AppHandle,
) {
    while let Some(event) = rx.recv().await {
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
