use arboard::Clipboard;
use enigo::{Direction, Enigo, Key, Keyboard, Settings as EnigoSettings};
use std::path::Path;
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, WPARAM};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_APARTMENTTHREADED,
};
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
use windows::Win32::UI::Accessibility::{
    CUIAutomation8, IUIAutomation, IUIAutomationTextPattern2, UIA_PATTERN_ID,
};
use windows::Win32::UI::Input::KeyboardAndMouse::SetFocus;
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, GetClassNameW, GetForegroundWindow, GetWindowThreadProcessId,
    IsWindow, IsWindowVisible, SendMessageW, SetForegroundWindow, WM_PASTE,
};
use windows::core::{w, ComInterface};

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
// Selection-aware dictation: query the focused element's text selection via
// UIA so the caller can replace it instead of inserting alongside it.
//
// Returns true iff the focused text element has a non-empty selection.
// Any UIA failure / element-not-text returns false (safe default — no Delete).
//
// Cost: ~30-150 ms depending on the app. COM is already initialized by
// ComGuard before this is called, and `cast` is the v0.52 ComInterface API.
// ---------------------------------------------------------------------------
fn has_non_empty_selection() -> bool {
    (|| -> Option<bool> {
        unsafe {
            let automation: IUIAutomation =
                CoCreateInstance(&CUIAutomation8, None, CLSCTX_ALL).ok()?;
            let focused = automation.GetFocusedElement().ok()?;
            // UIA_TextPattern2Id == 10024 (matches context.rs)
            let pattern: IUIAutomationTextPattern2 = focused
                .GetCurrentPattern(UIA_PATTERN_ID(10024))
                .ok()?
                .cast::<IUIAutomationTextPattern2>()
                .ok()?;
            let sel_array = pattern.GetSelection().ok()?;
            let range = sel_array.GetElement(0).ok()?;
            // GetText(-1) returns the full selected text; -1 = no length limit.
            // A non-selected caret usually returns "" but some controls return a
            // single character — guard with chars().count() > 0 of trimmed text.
            let text = range.GetText(-1).ok()?.to_string();
            Some(!text.is_empty())
        }
    })()
    .unwrap_or(false)
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

    // Selection-aware inject (Tier 2 #7): if the focused text element has a
    // non-empty selection, send Delete first so apps that don't replace on
    // Ctrl+V (rare but exists for custom edit controls) still see the dictated
    // text replace the selection. Ctrl+V on a selection in standard Win32 and
    // Chromium controls already replaces — this Delete is defensive insurance,
    // not a functional change for the common case. Skips on UIA failure.
    let had_selection = has_non_empty_selection();
    if had_selection {
        eprintln!("[inject] selection detected → sending Delete to clear before paste");
        if let Ok(mut pre_enigo) = Enigo::new(&EnigoSettings::default()) {
            let _ = pre_enigo.key(Key::Delete, Direction::Click);
            // Small settle delay so the Delete is consumed before the paste
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
    }

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

/// Types `text` directly into `hwnd_raw` via enigo's text() API — no
/// clipboard touch, so no Win+V history pollution and no risk of restoring
/// the user's previous clipboard mid-stream. Designed for chunked use from
/// the SSE streaming flow: caller invokes once per LLM delta.
fn inject_text_chunk_sync(text: &str, hwnd_raw: isize) -> Result<(), String> {
    let _com = ComGuard::init();
    let target: HWND = if hwnd_raw != 0
        && unsafe { IsWindow(HWND(hwnd_raw)).as_bool() }
        && unsafe { IsWindowVisible(HWND(hwnd_raw)).as_bool() }
    {
        HWND(hwnd_raw)
    } else {
        unsafe { GetForegroundWindow() }
    };
    if target.0 == 0 {
        return Err("no target window".to_string());
    }
    // UAC guard — silent failure here would let the LLM stream into nothing
    let mut target_pid = 0u32;
    unsafe { GetWindowThreadProcessId(target, Some(&mut target_pid)); }
    if target_pid != 0 && !is_self_elevated() && is_process_elevated(target_pid) {
        return Err("elevation_mismatch".to_string());
    }
    force_foreground(target);
    let mut enigo = Enigo::new(&EnigoSettings::default()).map_err(|e| e.to_string())?;
    enigo.text(text).map_err(|e| e.to_string())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tier 2 #6 — voice command keypress injection.
// Parses combo strings like "ctrl+a", "enter", "ctrl+shift+left" and emits
// them via enigo to the saved TargetHwnd.
// ---------------------------------------------------------------------------

fn parse_key_token(token: &str) -> Option<Key> {
    Some(match token.trim().to_lowercase().as_str() {
        "ctrl" | "control" => Key::Control,
        "shift" => Key::Shift,
        "alt" => Key::Alt,
        "meta" | "win" | "super" | "cmd" => Key::Meta,
        "enter" | "return" => Key::Return,
        "tab" => Key::Tab,
        "escape" | "esc" => Key::Escape,
        "backspace" => Key::Backspace,
        "delete" | "del" => Key::Delete,
        "space" => Key::Space,
        "up" => Key::UpArrow,
        "down" => Key::DownArrow,
        "left" => Key::LeftArrow,
        "right" => Key::RightArrow,
        "home" => Key::Home,
        "end" => Key::End,
        "pageup" => Key::PageUp,
        "pagedown" => Key::PageDown,
        single if single.chars().count() == 1 => {
            Key::Unicode(single.chars().next()?)
        }
        _ => return None,
    })
}

/// Splits a combo like "ctrl+shift+a" into modifiers + final key.
/// Returns (modifiers, final). Empty modifiers list when the combo is just
/// a standalone key like "enter".
fn parse_combo(combo: &str) -> Option<(Vec<Key>, Key)> {
    let parts: Vec<&str> = combo.split('+').collect();
    if parts.is_empty() {
        return None;
    }
    let (last, rest) = parts.split_last()?;
    let final_key = parse_key_token(last)?;
    let modifiers: Vec<Key> = rest
        .iter()
        .filter_map(|t| parse_key_token(t))
        .collect();
    // Reject combos that had unknown modifier tokens
    if modifiers.len() != rest.len() {
        return None;
    }
    Some((modifiers, final_key))
}

fn play_combo(combo: &str, hwnd_raw: isize) -> Result<(), String> {
    let _com = ComGuard::init();
    let target: HWND = if hwnd_raw != 0
        && unsafe { IsWindow(HWND(hwnd_raw)).as_bool() }
        && unsafe { IsWindowVisible(HWND(hwnd_raw)).as_bool() }
    {
        HWND(hwnd_raw)
    } else {
        unsafe { GetForegroundWindow() }
    };
    if target.0 == 0 {
        return Err("no target window".to_string());
    }
    force_foreground(target);
    std::thread::sleep(std::time::Duration::from_millis(30));

    // Voice command "enter+enter" → two Enter presses sequentially.
    // Split on '+' BUT inside parse_combo we treat '+'-separated tokens
    // as a single chord. For sequences, the convention here is: a combo
    // containing the same key twice (like "enter+enter") is interpreted
    // by parse_combo as modifier(Enter) + final(Enter), which is not what
    // we want. So we special-case repeated identical-token combos as
    // sequential presses.
    let tokens: Vec<&str> = combo.split('+').collect();
    let all_same = tokens.len() > 1
        && tokens
            .iter()
            .all(|t| t.trim().to_lowercase() == tokens[0].trim().to_lowercase());
    if all_same {
        let key = parse_key_token(tokens[0]).ok_or_else(|| format!("unknown key: {}", tokens[0]))?;
        let mut enigo = Enigo::new(&EnigoSettings::default()).map_err(|e| e.to_string())?;
        for _ in tokens.iter() {
            enigo.key(key, Direction::Click).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }

    let (modifiers, final_key) =
        parse_combo(combo).ok_or_else(|| format!("invalid combo: {combo}"))?;
    let mut enigo = Enigo::new(&EnigoSettings::default()).map_err(|e| e.to_string())?;
    for m in &modifiers {
        enigo.key(*m, Direction::Press).map_err(|e| e.to_string())?;
    }
    enigo.key(final_key, Direction::Click).map_err(|e| e.to_string())?;
    for m in modifiers.iter().rev() {
        enigo.key(*m, Direction::Release).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn inject_keypress(
    combo: String,
    hwnd_state: tauri::State<'_, crate::TargetHwnd>,
) -> Result<(), String> {
    // Use the snapshotted target HWND if available — fall back to the current
    // foreground window. We don't consume the TargetHwnd slot here so a
    // command does not interfere with a queued inject_text on the same press.
    let raw = hwnd_state.0.lock().map_err(|e| e.to_string())?.unwrap_or(0);
    tokio::task::spawn_blocking(move || play_combo(&combo, raw))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod combo_tests {
    use super::*;

    #[test]
    fn parses_simple_key() {
        let (mods, key) = parse_combo("enter").unwrap();
        assert!(mods.is_empty());
        assert!(matches!(key, Key::Return));
    }

    #[test]
    fn parses_modifier_combo() {
        let (mods, key) = parse_combo("ctrl+a").unwrap();
        assert_eq!(mods.len(), 1);
        assert!(matches!(mods[0], Key::Control));
        assert!(matches!(key, Key::Unicode('a')));
    }

    #[test]
    fn parses_multi_modifier_combo() {
        let (mods, key) = parse_combo("ctrl+shift+left").unwrap();
        assert_eq!(mods.len(), 2);
        assert!(matches!(mods[0], Key::Control));
        assert!(matches!(mods[1], Key::Shift));
        assert!(matches!(key, Key::LeftArrow));
    }

    #[test]
    fn case_insensitive() {
        assert!(parse_combo("CTRL+A").is_some());
        assert!(parse_combo("Ctrl+Shift+Left").is_some());
    }

    #[test]
    fn rejects_unknown_token() {
        assert!(parse_combo("ctrl+xyzzy").is_none());
        assert!(parse_combo("garble+a").is_none());
    }

    #[test]
    fn rejects_empty() {
        // split on '+' of "" yields [""]. parse_key_token("") → no match.
        assert!(parse_combo("").is_none());
    }

    #[test]
    fn function_keys_not_supported_yet() {
        // F-keys would need explicit entries; verify they don't silently
        // map to single-char Unicode.
        assert!(parse_combo("f1").is_none());
    }
}

/// Sends Ctrl+Z to `hwnd_raw` (or the foreground window if 0/stale). Used by
/// the undo hotkey. Synchronous; runs on whichever thread invokes it.
pub fn undo_in_target(hwnd_raw: isize) {
    let _com = ComGuard::init();
    let target: HWND = if hwnd_raw != 0
        && unsafe { IsWindow(HWND(hwnd_raw)).as_bool() }
        && unsafe { IsWindowVisible(HWND(hwnd_raw)).as_bool() }
    {
        HWND(hwnd_raw)
    } else {
        unsafe { GetForegroundWindow() }
    };
    if target.0 == 0 {
        return;
    }
    force_foreground(target);
    std::thread::sleep(std::time::Duration::from_millis(30));
    if let Ok(mut enigo) = Enigo::new(&EnigoSettings::default()) {
        let _ = enigo.key(Key::Control, Direction::Press);
        let _ = enigo.key(Key::Unicode('z'), Direction::Click);
        let _ = enigo.key(Key::Control, Direction::Release);
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------
#[tauri::command]
pub async fn inject_text(
    text: String,
    hwnd_state: tauri::State<'_, crate::TargetHwnd>,
    last_inject: tauri::State<'_, crate::LastInject>,
) -> Result<(), String> {
    let raw = hwnd_state.0.lock().map_err(|e| e.to_string())?.take();
    let text_len = text.chars().count();
    let text_for_thread = text.clone();
    let raw_for_thread = raw;
    let result = tokio::task::spawn_blocking(move || inject_sync(&text_for_thread, raw_for_thread))
        .await
        .map_err(|e| e.to_string())?;
    if result.is_ok() {
        // Record the actual HWND used so undo targets the same window — fall
        // back to the captured raw value if Win32 lookups upstream cleared it.
        let resolved = raw.unwrap_or(0);
        if let Ok(mut guard) = last_inject.0.lock() {
            *guard = Some((resolved, text_len));
        }
    }
    result
}

/// Begin a streaming-inject session. Takes the captured target HWND from
/// `TargetHwnd` and stores it in `StreamTarget` so subsequent `inject_text_chunk`
/// calls can target the right window across the LLM token stream. Also runs
/// the selection-aware Delete pre-step once, before any deltas arrive —
/// per-chunk selection checks would re-query UIA on every token, adding
/// 30–150 ms each.
#[tauri::command]
pub async fn begin_stream_inject(
    hwnd_state: tauri::State<'_, crate::TargetHwnd>,
    stream_target: tauri::State<'_, crate::StreamTarget>,
) -> Result<(), String> {
    let raw = hwnd_state.0.lock().map_err(|e| e.to_string())?.take();
    *stream_target.0.lock().map_err(|e| e.to_string())? = raw;
    // Do the selection check + Delete on a worker thread so we don't block
    // the async runtime with UIA / enigo calls.
    let hwnd_raw = raw.unwrap_or(0);
    if hwnd_raw == 0 {
        return Ok(());
    }
    tokio::task::spawn_blocking(move || {
        let _com = ComGuard::init();
        let target = HWND(hwnd_raw);
        if !unsafe { IsWindow(target).as_bool() } {
            return;
        }
        force_foreground(target);
        std::thread::sleep(std::time::Duration::from_millis(30));
        if has_non_empty_selection() {
            if let Ok(mut enigo) = Enigo::new(&EnigoSettings::default()) {
                let _ = enigo.key(Key::Delete, Direction::Click);
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
    })
    .await
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn inject_text_chunk(
    text: String,
    stream_target: tauri::State<'_, crate::StreamTarget>,
) -> Result<(), String> {
    let raw = stream_target
        .0
        .lock()
        .map_err(|e| e.to_string())?
        .unwrap_or(0);
    tokio::task::spawn_blocking(move || inject_text_chunk_sync(&text, raw))
        .await
        .map_err(|e| e.to_string())?
}

/// Close out a streaming-inject session: records final state for undo
/// (so Ctrl+Shift+Z still works after a streamed injection) and clears
/// the in-flight target.
#[tauri::command]
pub async fn end_stream_inject(
    total_len: usize,
    stream_target: tauri::State<'_, crate::StreamTarget>,
    last_inject: tauri::State<'_, crate::LastInject>,
) -> Result<(), String> {
    let raw = stream_target.0.lock().map_err(|e| e.to_string())?.take();
    let resolved = raw.unwrap_or(0);
    if let Ok(mut guard) = last_inject.0.lock() {
        *guard = Some((resolved, total_len));
    }
    Ok(())
}

#[tauri::command]
pub async fn undo_last_inject(
    last_inject: tauri::State<'_, crate::LastInject>,
) -> Result<bool, String> {
    let snapshot = last_inject.0.lock().map_err(|e| e.to_string())?.take();
    let Some((hwnd_raw, _len)) = snapshot else {
        return Ok(false);
    };
    tokio::task::spawn_blocking(move || undo_in_target(hwnd_raw))
        .await
        .map_err(|e| e.to_string())?;
    Ok(true)
}
