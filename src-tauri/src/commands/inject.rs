use arboard::Clipboard;
use enigo::{Direction, Enigo, Key, Keyboard, Settings as EnigoSettings};
use std::path::Path;
use windows::Win32::Foundation::{CloseHandle, HWND};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED};
use windows::Win32::System::ProcessStatus::GetModuleFileNameExW;
use windows::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetClassNameW, GetForegroundWindow, GetWindowThreadProcessId,
};

// Matches context.rs ComGuard — arboard touches OLE clipboard APIs which require COM.
struct ComGuard;

impl ComGuard {
    fn init() -> Self {
        unsafe {
            let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        }
        Self
    }
}

impl Drop for ComGuard {
    fn drop(&mut self) {
        unsafe { CoUninitialize() }
    }
}

fn get_window_class(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    let len = unsafe { GetClassNameW(hwnd, &mut buf) };
    if len <= 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buf[..len as usize])
}

fn get_exe_name_for_hwnd(hwnd: HWND) -> String {
    unsafe {
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid == 0 {
            return String::new();
        }
        let Ok(handle) = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid)
        else {
            return String::new();
        };
        let mut buf = [0u16; 1024];
        let len = GetModuleFileNameExW(handle, None, &mut buf);
        let _ = CloseHandle(handle);
        if len == 0 {
            return String::new();
        }
        let path = String::from_utf16_lossy(&buf[..len as usize]);
        Path::new(&path)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase()
    }
}

fn is_electron_window(hwnd: HWND) -> bool {
    if get_window_class(hwnd) == "Chrome_WidgetWin_1" {
        return true;
    }
    get_exe_name_for_hwnd(hwnd).contains("electron")
}

fn inject_sync(text: &str) -> Result<(), String> {
    let _com = ComGuard::init();

    let hwnd: HWND = unsafe { GetForegroundWindow() };
    let electron = hwnd.0 != 0 && is_electron_window(hwnd);

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

    if electron {
        // Move cursor to end of input field without selecting all text.
        // Ctrl+A in Electron selects the entire document — forbidden.
        enigo
            .key(Key::End, Direction::Click)
            .map_err(|e| e.to_string())?;
        std::thread::sleep(std::time::Duration::from_millis(30));
    }

    enigo
        .key(Key::Control, Direction::Press)
        .map_err(|e| e.to_string())?;
    enigo
        .key(Key::Unicode('v'), Direction::Click)
        .map_err(|e| e.to_string())?;
    enigo
        .key(Key::Control, Direction::Release)
        .map_err(|e| e.to_string())?;

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
