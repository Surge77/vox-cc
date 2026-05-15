// TODO M10: full UIA SetValue + clipboard inject with Electron detection
#[tauri::command]
pub async fn inject_text(text: String) -> Result<(), String> {
    tokio::task::spawn_blocking(move || {
        use arboard::Clipboard;
        let mut cb = Clipboard::new().map_err(|e| e.to_string())?;
        let saved = cb.get_text().unwrap_or_default();
        cb.set_text(&text).map_err(|e| e.to_string())?;
        std::thread::sleep(std::time::Duration::from_millis(50));
        // Ctrl+V via enigo (full impl in M10)
        cb.set_text(&saved).map_err(|e| e.to_string())?;
        Ok::<(), String>(())
    })
    .await
    .map_err(|e| e.to_string())?
}
