#[derive(serde::Serialize, Default)]
pub struct DeepContextPayload {
    pub executable_name: String,
    pub window_title: String,
    pub inferred_extension: Option<String>,
    pub text_preceding_cursor: String,
    pub text_succeeding_cursor: String,
}

// TODO M11: real UIA implementation (GetForegroundWindow, TextPattern2, etc.)
#[tauri::command]
pub async fn get_focused_context() -> Result<DeepContextPayload, String> {
    tokio::task::spawn_blocking(|| Ok::<_, String>(DeepContextPayload::default()))
        .await
        .map_err(|e| e.to_string())?
}
