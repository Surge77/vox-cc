use std::path::Path;
use windows::Win32::Foundation::{CloseHandle, HWND};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_APARTMENTTHREADED,
};
use windows::Win32::System::ProcessStatus::GetModuleFileNameExW;
use windows::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
};
use windows::Win32::UI::Accessibility::{
    CUIAutomation8, IUIAutomation, IUIAutomationTextPattern2, IUIAutomationTextRangeArray,
    TextPatternRangeEndpoint_End, TextPatternRangeEndpoint_Start, UIA_PATTERN_ID,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetForegroundWindow, GetWindowTextW, GetWindowThreadProcessId,
};
use windows::core::ComInterface;

#[derive(serde::Serialize, Default)]
pub struct DeepContextPayload {
    pub executable_name: String,
    pub window_title: String,
    pub inferred_extension: Option<String>,
    pub text_preceding_cursor: String, // max 300 chars
    pub text_succeeding_cursor: String, // max 100 chars
}

// RAII guard: initializes COM on the current thread, uninitializes on drop.
// spawn_blocking threads are not the main thread and have no COM by default.
// S_OK = fresh init, S_FALSE = already init (ref count++), RPC_E_CHANGED_MODE = different
// model already active. All three leave COM usable on this thread — ignore return value.
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

fn get_exe_name(hwnd: HWND) -> String {
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

fn get_window_title(hwnd: HWND) -> String {
    let mut buf = [0u16; 512];
    let len = unsafe { GetWindowTextW(hwnd, &mut buf) };
    if len <= 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buf[..len as usize])
}

fn infer_extension(title: &str, exe: &str) -> Option<String> {
    let title_lc = title.to_lowercase();
    for ext in &[".py", ".ts", ".tsx", ".rs", ".go", ".cpp", ".cs", ".md", ".txt", ".js"] {
        if title_lc.contains(ext) {
            return Some((*ext).to_string());
        }
    }
    let ext = match exe {
        e if e.contains("code") || e.contains("vscode") => ".ts",
        e if e.contains("pycharm") || e.contains("python") => ".py",
        e if e.contains("rider") || e.contains("clion") => ".cs",
        e if e.contains("goland") => ".go",
        e if e.contains("notepad") => ".txt",
        _ => return None,
    };
    Some(ext.to_string())
}

// Returns (preceding_text, succeeding_text). Empty strings on any UIA failure.
fn extract_cursor_context() -> (String, String) {
    (|| -> Option<(String, String)> {
        unsafe {
            let automation: IUIAutomation =
                CoCreateInstance(&CUIAutomation8, None, CLSCTX_ALL).ok()?;

            let focused = automation.GetFocusedElement().ok()?;

            // Pattern ID 10024 = UIA_TextPattern2Id
            let pattern: IUIAutomationTextPattern2 = focused
                .GetCurrentPattern(UIA_PATTERN_ID(10024))
                .ok()?
                .cast::<IUIAutomationTextPattern2>()
                .ok()?;

            let sel_array: IUIAutomationTextRangeArray = pattern.GetSelection().ok()?;
            let caret = sel_array.GetElement(0).ok()?;

            // Preceding: from doc start to caret start
            let preceding_range = pattern.DocumentRange().ok()?.Clone().ok()?;
            preceding_range
                .MoveEndpointByRange(
                    TextPatternRangeEndpoint_End,
                    &caret,
                    TextPatternRangeEndpoint_Start,
                )
                .ok()?;
            let preceding = preceding_range
                .GetText(-1)
                .map(|b| b.to_string())
                .unwrap_or_default();

            // Succeeding: from caret end to doc end
            let succeeding_range = pattern.DocumentRange().ok()?.Clone().ok()?;
            succeeding_range
                .MoveEndpointByRange(
                    TextPatternRangeEndpoint_Start,
                    &caret,
                    TextPatternRangeEndpoint_End,
                )
                .ok()?;
            let succeeding = succeeding_range
                .GetText(200)
                .map(|b| b.to_string())
                .unwrap_or_default();

            Some((preceding, succeeding))
        }
    })()
    .unwrap_or_default()
}

fn get_focused_context_sync() -> DeepContextPayload {
    let _com = ComGuard::init();
    let hwnd: HWND = unsafe { GetForegroundWindow() };
    if hwnd.0 == 0 {
        return DeepContextPayload::default();
    }

    let exe = get_exe_name(hwnd);
    let title = get_window_title(hwnd);
    let inferred_extension = infer_extension(&title, &exe);
    let (preceding_raw, succeeding_raw) = extract_cursor_context();

    // Truncate: last 300 chars of preceding, first 100 of succeeding
    let text_preceding_cursor: String = {
        let chars: Vec<char> = preceding_raw.chars().collect();
        let start = chars.len().saturating_sub(300);
        chars[start..].iter().collect()
    };
    let text_succeeding_cursor: String = succeeding_raw.chars().take(100).collect();

    DeepContextPayload {
        executable_name: exe,
        window_title: title,
        inferred_extension,
        text_preceding_cursor,
        text_succeeding_cursor,
    }
}

#[tauri::command]
pub async fn get_focused_context() -> Result<DeepContextPayload, String> {
    // Never returns Err — all failures degrade to default payload
    tokio::task::spawn_blocking(|| Ok::<_, String>(get_focused_context_sync()))
        .await
        .unwrap_or_else(|_| Ok(DeepContextPayload::default()))
}
