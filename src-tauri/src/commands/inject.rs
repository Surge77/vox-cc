use arboard::Clipboard;
use enigo::{Direction, Enigo, Key, Keyboard, Settings as EnigoSettings};

fn inject_sync(text: &str) -> Result<(), String> {
    let mut attempts = 0u8;
    let mut clipboard = loop {
        match Clipboard::new() {
            Ok(c) => break c,
            Err(e) if attempts < 3 => {
                attempts += 1;
                std::thread::sleep(std::time::Duration::from_millis(100));
                let _ = e;
            }
            Err(e) => return Err(e.to_string()),
        }
    };

    let saved = clipboard.get_text().unwrap_or_default();
    clipboard.set_text(text).map_err(|e| e.to_string())?;
    // Give clipboard viewers time to see new content before Ctrl+V
    std::thread::sleep(std::time::Duration::from_millis(50));

    let mut enigo = Enigo::new(&EnigoSettings::default()).map_err(|e| e.to_string())?;
    enigo.key(Key::Control, Direction::Press).map_err(|e| e.to_string())?;
    enigo.key(Key::Unicode('v'), Direction::Click).map_err(|e| e.to_string())?;
    enigo.key(Key::Control, Direction::Release).map_err(|e| e.to_string())?;

    // Wait for target app to finish processing the paste before restoring original clipboard
    std::thread::sleep(std::time::Duration::from_millis(150));
    clipboard.set_text(&saved).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn inject_text(text: String) -> Result<(), String> {
    // spawn_blocking required — arboard touches COM, cannot run on async executor
    tokio::task::spawn_blocking(move || inject_sync(&text))
        .await
        .map_err(|e| e.to_string())?
}
