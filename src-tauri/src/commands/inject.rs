use arboard::Clipboard;
use enigo::{Direction, Enigo, Key, Keyboard, Settings as EnigoSettings};
use std::path::Path;
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, WPARAM};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED};
use windows::Win32::System::DataExchange::{
    CloseClipboard, GetClipboardSequenceNumber, OpenClipboard, RegisterClipboardFormatW,
    SetClipboardData,
};
use windows::Win32::System::ProcessStatus::GetModuleFileNameExW;
use windows::Win32::System::Threading::{
    AttachThreadInput, GetCurrentThreadId, OpenProcess, OpenProcessToken,
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
};
use windows::Win32::Security::{GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY};
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, GetClassNameW, GetForegroundWindow, GetWindowThreadProcessId,
    IsWindow, IsWindowVisible, SendMessageW, SetForegroundWindow, WM_PASTE,
};
use windows::core::w;

// EM_REPLACESEL (0x00C2) is defined in CommCtrl.h / Win32_UI_Controls which we
// don't feature-flag.  Define the constant directly — the value is stable ABI.
const EM_REPLACESEL: u32 = 0x00C2;

// ---------------------------------------------------------------------------
// COM guard (same pattern as context.rs)
// ---------------------------------------------------------------------------
struct ComGuard;
impl ComGuard {
    fn init() -> Self {
        unsafe { let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED); }
        Self
    }
}
impl Drop for ComGuard {
    fn drop(&mut self) { unsafe { CoUninitialize() } }
}

// ---------------------------------------------------------------------------
// Window / process helpers
// ---------------------------------------------------------------------------
fn get_window_class(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    let len = unsafe { GetClassNameW(hwnd, &mut buf) };
    if len <= 0 { return String::new(); }
    String::from_utf16_lossy(&buf[..len as usize])
}

fn get_exe_name_for_hwnd(hwnd: HWND) -> String {
    unsafe {
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid == 0 { return String::new(); }
        let Ok(handle) = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid)
        else { return String::new(); };
        let mut buf = [0u16; 1024];
        let len = GetModuleFileNameExW(handle, None, &mut buf);
        let _ = CloseHandle(handle);
        if len == 0 { return String::new(); }
        let path = String::from_utf16_lossy(&buf[..len as usize]);
        Path::new(&path)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase()
    }
}

/// True for any Chromium-embedded or Electron-style window that must NOT
/// receive Ctrl+A (it selects the full document instead of the field).
fn is_chromium_window(hwnd: HWND) -> bool {
    let class = get_window_class(hwnd);
    // CEF, Electron, Chrome, Edge, VS Code, Figma all share this class
    if class == "Chrome_WidgetWin_1" || class == "Chrome_WidgetWin_0" {
        return true;
    }
    let exe = get_exe_name_for_hwnd(hwnd);
    // Broad net: any exe that embeds a browser engine
    matches!(exe.as_str(),
        "electron" | "code" | "code - insiders" | "discord" | "slack" | "teams" |
        "figma" | "notion" | "obsidian" | "linear" | "arc" | "chrome" | "msedge" |
        "brave" | "opera" | "vivaldi"
    ) || exe.contains("electron") || exe.contains("cef")
}

/// True for standard Win32 edit controls where EM_REPLACESEL works reliably.
fn is_standard_edit_control(hwnd: HWND) -> bool {
    let class = get_window_class(hwnd);
    // Win32 Edit, RichEdit variants, Scintilla (Notepad++, some IDEs)
    matches!(class.to_lowercase().as_str(),
        "edit" | "richedit" | "richedit20w" | "richedit20a" |
        "richedit50w" | "richedit60w" | "scintilla"
    )
}

// ---------------------------------------------------------------------------
// Elevation helpers (unchanged from original)
// ---------------------------------------------------------------------------
fn is_process_elevated(pid: u32) -> bool {
    unsafe {
        let Ok(proc) = OpenProcess(PROCESS_QUERY_INFORMATION, false, pid) else { return false; };
        let mut token = windows::Win32::Foundation::HANDLE::default();
        if OpenProcessToken(proc, TOKEN_QUERY, &mut token).is_err() {
            let _ = CloseHandle(proc);
            return false;
        }
        let mut elev = TOKEN_ELEVATION::default();
        let mut ret_len = 0u32;
        let ok = GetTokenInformation(
            token, TokenElevation,
            Some(&mut elev as *mut _ as *mut std::ffi::c_void),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut ret_len,
        ).is_ok();
        let _ = CloseHandle(token);
        let _ = CloseHandle(proc);
        ok && elev.TokenIsElevated != 0
    }
}

fn is_self_elevated() -> bool {
    use windows::Win32::System::Threading::GetCurrentProcess;
    unsafe {
        let proc = GetCurrentProcess();
        let mut token = windows::Win32::Foundation::HANDLE::default();
        if OpenProcessToken(proc, TOKEN_QUERY, &mut token).is_err() { return false; }
        let mut elev = TOKEN_ELEVATION::default();
        let mut ret_len = 0u32;
        let ok = GetTokenInformation(
            token, TokenElevation,
            Some(&mut elev as *mut _ as *mut std::ffi::c_void),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut ret_len,
        ).is_ok();
        let _ = CloseHandle(token);
        ok && elev.TokenIsElevated != 0
    }
}

// ---------------------------------------------------------------------------
// Fix 1: Reliable foreground focus via AttachThreadInput
// ---------------------------------------------------------------------------
/// Brings `hwnd` to the foreground reliably by temporarily attaching our
/// thread's input queue to the target thread's queue. Without this,
/// SetForegroundWindow silently fails when another process owns the lock.
fn force_foreground(hwnd: HWND) {
    unsafe {
        let our_tid = GetCurrentThreadId();
        let mut target_pid = 0u32;
        let target_tid = GetWindowThreadProcessId(hwnd, Some(&mut target_pid));

        // Attach input queues so SetForegroundWindow is allowed
        if target_tid != 0 && target_tid != our_tid {
            let _ = AttachThreadInput(our_tid, target_tid, true);
        }

        let _ = SetForegroundWindow(hwnd);
        let _ = BringWindowToTop(hwnd);
        let _ = SetFocus(hwnd);

        if target_tid != 0 && target_tid != our_tid {
            let _ = AttachThreadInput(our_tid, target_tid, false);
        }
    }
}

// ---------------------------------------------------------------------------
// Fix 2: Clipboard privacy — exclude from Windows Clipboard History (Win+V)
// ---------------------------------------------------------------------------
/// Adds the `ExcludeClipboardContentFromMonitorProcessing` sentinel to the
/// current clipboard contents. This prevents Windows Clipboard History and
/// cloud sync from storing dictated text — identical to password-manager behaviour.
fn mark_clipboard_private() {
    unsafe {
        // RegisterClipboardFormatW is idempotent — same name → same ID every call
        let fmt = RegisterClipboardFormatW(w!("ExcludeClipboardContentFromMonitorProcessing"));
        if fmt == 0 { return; }

        // Open clipboard (HWND(0) = no owner) and append the sentinel marker.
        // We must not call EmptyClipboard here — that would erase the text we
        // just set via arboard. Adding an extra format to an already-open owner
        // requires the clipboard owner to add it; since we ARE the owner (we
        // just set data via arboard which opened/closed already), we must
        // re-open and add the format without clearing.
        if OpenClipboard(HWND(0)).is_ok() {
            // NULL handle = format is present but carries no data — that's the
            // correct sentinel expected by Windows Clipboard History.
            let _ = SetClipboardData(fmt, None);
            let _ = CloseClipboard();
        }
    }
}

// ---------------------------------------------------------------------------
// Fix 3: EM_REPLACESEL fast-path (no clipboard, no focus change required)
// ---------------------------------------------------------------------------
/// Injects text directly into a standard Win32/RichEdit control using
/// EM_REPLACESEL. This inserts at the current cursor position without
/// touching the clipboard or requiring foreground focus. Returns true on
/// apparent success (the message was delivered; edit controls return 0 always
/// so we optimistically assume success when the class matches).
fn try_em_replacesel(hwnd: HWND, text: &str) -> bool {
    // Encode text as wide string with null terminator
    let wide: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        // wParam = TRUE → allow undo; lParam = pointer to wide string
        SendMessageW(
            hwnd,
            EM_REPLACESEL,
            WPARAM(1),
            LPARAM(wide.as_ptr() as isize),
        );
    }
    true
}

// ---------------------------------------------------------------------------
// Fix 4: WM_PASTE fallback for standard edit controls
// ---------------------------------------------------------------------------
#[allow(dead_code)]
fn send_wm_paste(hwnd: HWND) {
    unsafe { SendMessageW(hwnd, WM_PASTE, WPARAM(0), LPARAM(0)); }
}

// ---------------------------------------------------------------------------
// Core injection logic
// ---------------------------------------------------------------------------
fn inject_sync(text: &str, target_hwnd_raw: Option<isize>) -> Result<(), String> {
    let _com = ComGuard::init();

    // Fix 3: Stale HWND guard
    // The HWND was captured at keydown; Whisper inference takes 600-1000ms.
    // If the user closed that window, HWND is now invalid — injecting into it
    // would silently fail or go to a ghost handle. Validate before use.
    let hwnd: HWND = match target_hwnd_raw {
        Some(raw) if raw != 0 => {
            let candidate = HWND(raw);
            let still_alive = unsafe { IsWindow(candidate).as_bool() }
                && unsafe { IsWindowVisible(candidate).as_bool() };
            if still_alive {
                candidate
            } else {
                eprintln!(
                    "[inject] stored HWND={} is stale (window closed/hidden) — \
                     falling back to GetForegroundWindow",
                    raw
                );
                unsafe { GetForegroundWindow() }
            }
        }
        _ => {
            eprintln!("[inject] warn: no stored HWND, falling back to GetForegroundWindow");
            unsafe { GetForegroundWindow() }
        }
    };

    let exe_name = get_exe_name_for_hwnd(hwnd);
    eprintln!(
        "[inject] target_hwnd={} target_exe={} text_len={}",
        hwnd.0, exe_name, text.len()
    );

    // UAC guard: standard-user Vox cannot inject into elevated processes
    let mut target_pid = 0u32;
    unsafe { GetWindowThreadProcessId(hwnd, Some(&mut target_pid)); }
    if target_pid != 0 && !is_self_elevated() && is_process_elevated(target_pid) {
        return Err("elevation_mismatch".to_string());
    }

    // ------------------------------------------------------------------
    // Fast path: standard Win32 edit control → EM_REPLACESEL
    // No clipboard access, no focus change, works even without foreground.
    // ------------------------------------------------------------------
    if is_standard_edit_control(hwnd) {
        eprintln!("[inject] fast-path: EM_REPLACESEL to edit control");
        try_em_replacesel(hwnd, text);
        return Ok(());
    }

    // ------------------------------------------------------------------
    // General path: clipboard + Ctrl+V
    // ------------------------------------------------------------------
    let is_chromium = is_chromium_window(hwnd);

    // Open clipboard with retry
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

    // Fix 1: reliable foreground via AttachThreadInput
    force_foreground(hwnd);
    std::thread::sleep(std::time::Duration::from_millis(30));

    // Save previous clipboard contents for later restore
    let saved = clipboard.get_text().unwrap_or_default();

    // Set our text
    clipboard.set_text(text).map_err(|e| e.to_string())?;
    eprintln!("[inject] clipboard set text_len={}", text.len());

    // Fix 2: mark as private — exclude from Clipboard History / cloud sync
    mark_clipboard_private();

    // Fix 4: snapshot sequence number to detect external clipboard changes
    let seq_before_paste = unsafe { GetClipboardSequenceNumber() };

    // Allow clipboard viewers to see new content before Ctrl+V
    std::thread::sleep(std::time::Duration::from_millis(50));

    let mut enigo = Enigo::new(&EnigoSettings::default()).map_err(|e| e.to_string())?;

    if is_chromium {
        // In Chromium apps Ctrl+A selects the whole document, so just move to end
        enigo.key(Key::End, Direction::Click).map_err(|e| e.to_string())?;
        std::thread::sleep(std::time::Duration::from_millis(30));
    }

    enigo.key(Key::Control, Direction::Press).map_err(|e| e.to_string())?;
    enigo.key(Key::Unicode('v'), Direction::Click).map_err(|e| e.to_string())?;
    enigo.key(Key::Control, Direction::Release).map_err(|e| e.to_string())?;
    eprintln!("[inject] sent Ctrl+V to hwnd={}", hwnd.0);

    // Wait for target app to consume the clipboard paste.
    // 150 ms is the original delay — still needed for slow apps (Word, Outlook).
    std::thread::sleep(std::time::Duration::from_millis(150));

    // Restore clipboard only if no external process changed it while we waited.
    // seq_before_paste was captured AFTER our own set_text + mark_clipboard_private,
    // so under normal conditions seq_after_paste == seq_before_paste (unchanged).
    // If it advanced further, another app wrote to clipboard — leave it alone.
    let seq_after_paste = unsafe { GetClipboardSequenceNumber() };
    if seq_after_paste == seq_before_paste {
        // Nothing changed clipboard after our write → safe to restore user's content
        if let Err(e) = clipboard.set_text(&saved) {
            eprintln!("[inject] warn: could not restore clipboard: {e}");
        } else {
            eprintln!("[inject] done clipboard restored");
        }
    } else {
        eprintln!(
            "[inject] skipped clipboard restore: external write detected (seq {} → {})",
            seq_before_paste, seq_after_paste
        );
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Tauri command
// ---------------------------------------------------------------------------
#[tauri::command]
pub async fn inject_text(
    text: String,
    hwnd_state: tauri::State<'_, crate::TargetHwnd>,
) -> Result<(), String> {
    let raw = hwnd_state.0.lock().map_err(|e| e.to_string())?.take();
    tokio::task::spawn_blocking(move || inject_sync(&text, raw))
        .await
        .map_err(|e| e.to_string())?
}
