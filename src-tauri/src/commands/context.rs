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
    /// Active URL when the focused app is a known browser. None on
    /// non-browser apps or UIA failure.
    pub app_url: Option<String>,
    /// Active source file when the focused app is a known IDE / code editor.
    /// Extracted from the window title via a tolerant pattern parser.
    pub app_file_path: Option<String>,
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

// ---------------------------------------------------------------------------
// Better app context (Tier 2 #8) — IDE / editor file path extraction.
// Pure string parser — testable without UIA. Handles the title formats used
// by VS Code, Cursor, JetBrains IDEs, Sublime, and similar editors.
// ---------------------------------------------------------------------------
const IDE_EXES: &[&str] = &[
    "code", "code - insiders", "cursor", "windsurf",
    "idea", "idea64", "pycharm", "pycharm64", "rider", "rider64",
    "clion", "clion64", "goland", "goland64", "webstorm", "webstorm64",
    "rubymine", "phpstorm",
    "sublime_text", "atom", "notepad++", "gedit",
];

const BROWSER_EXES: &[&str] = &[
    "chrome", "msedge", "firefox", "brave", "arc", "opera", "vivaldi",
];

const KNOWN_EXTS: &[&str] = &[
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".cpp", ".c", ".h",
    ".hpp", ".cs", ".java", ".kt", ".swift", ".rb", ".php", ".lua", ".sh",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".css",
    ".scss", ".sql",
];

/// Extracts a likely source filename from the window title of a known IDE.
/// Window titles vary by editor — the common pattern is "<file> <sep>
/// <project> <sep> <editor name>" with sep ∈ { " - ", " — ", " – " }.
/// Returns the first token in the split that contains a known extension.
pub fn parse_ide_file_path(title: &str) -> Option<String> {
    if title.is_empty() {
        return None;
    }
    // Try the most common separators, in order. Only walk a separator if it
    // actually appears in the title — otherwise `split` returns the whole
    // string as one segment and we'd false-match any ".ext" substring.
    for sep in &[" - ", " — ", " – ", " | "] {
        if !title.contains(sep) {
            continue;
        }
        for part in title.split(sep) {
            let trimmed = part.trim().trim_start_matches('●').trim();
            if trimmed.is_empty() {
                continue;
            }
            // Strip trailing "[...]" annotation (JetBrains "[pycharm]",
            // Cursor "[Working Tree]") so the LLM sees just the filename.
            let cleaned = trimmed.split('[').next().unwrap_or(trimmed).trim();
            let lc = cleaned.to_lowercase();
            if KNOWN_EXTS.iter().any(|ext| lc.contains(ext)) {
                return Some(cleaned.to_string());
            }
        }
        // We tried the separator that's actually present; don't fall back to
        // others on the same title (would re-yield false positives).
        break;
    }
    None
}

fn is_browser_exe(exe: &str) -> bool {
    BROWSER_EXES.contains(&exe)
}

fn is_ide_exe(exe: &str) -> bool {
    IDE_EXES.iter().any(|e| *e == exe || exe.starts_with(e))
}

#[cfg(test)]
mod parse_tests {
    use super::*;

    #[test]
    fn vs_code_classic_title() {
        assert_eq!(
            parse_ide_file_path("main.py - vox - Visual Studio Code"),
            Some("main.py".to_string())
        );
    }

    #[test]
    fn vs_code_dirty_marker() {
        assert_eq!(
            parse_ide_file_path("● App.tsx - vox - Visual Studio Code"),
            Some("App.tsx".to_string())
        );
    }

    #[test]
    fn cursor_em_dash() {
        assert_eq!(
            parse_ide_file_path("App.tsx — vox — Cursor"),
            Some("App.tsx".to_string())
        );
    }

    #[test]
    fn jetbrains_pattern() {
        assert_eq!(
            parse_ide_file_path("vox - main.py [pycharm]"),
            Some("main.py".to_string())
        );
    }

    #[test]
    fn no_file_in_title_returns_none() {
        assert_eq!(parse_ide_file_path("vox - Visual Studio Code"), None);
        assert_eq!(parse_ide_file_path("Untitled — Cursor"), None);
    }

    #[test]
    fn empty_title_returns_none() {
        assert_eq!(parse_ide_file_path(""), None);
    }

    #[test]
    fn picks_first_extension_match() {
        // Title has two candidates — we keep the first (typically the open file)
        let out = parse_ide_file_path("foo.py - bar.txt - Editor");
        assert_eq!(out, Some("foo.py".to_string()));
    }
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

fn get_focused_context_sync(target_hwnd_raw: Option<isize>) -> DeepContextPayload {
    let _com = ComGuard::init();
    let hwnd: HWND = match target_hwnd_raw {
        Some(raw) if raw != 0 => HWND(raw),
        _ => unsafe { GetForegroundWindow() },
    };
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

    // App-context enrichment (Tier 2 #8): browser URL + IDE file path.
    // Both are best-effort — failures degrade silently to None.
    let app_file_path = if is_ide_exe(&exe) {
        parse_ide_file_path(&title)
    } else {
        None
    };
    // Browser URL extraction via UIA AutomationId lookup is non-trivial in
    // windows-0.52 (CreatePropertyCondition expects a non-trivial VARIANT
    // shape). Deferred — for now the page title still arrives via window_title
    // which is enough context for most LLM prompts.
    let app_url: Option<String> = if is_browser_exe(&exe) {
        // TODO(tier3): UIA descendant search for urlbar / omnibox / urlbar-input.
        None
    } else {
        None
    };

    DeepContextPayload {
        executable_name: exe,
        window_title: title,
        inferred_extension,
        text_preceding_cursor,
        text_succeeding_cursor,
        app_url,
        app_file_path,
    }
}


#[tauri::command]
pub async fn get_focused_context(
    hwnd_state: tauri::State<'_, crate::TargetHwnd>,
) -> Result<DeepContextPayload, String> {
    // Read but do NOT clear — inject_text needs the same HWND after this returns.
    let raw = *hwnd_state.0.lock().map_err(|e| e.to_string())?;
    // Never returns Err — all failures degrade to default payload
    tokio::task::spawn_blocking(move || Ok::<_, String>(get_focused_context_sync(raw)))
        .await
        .unwrap_or_else(|_| Ok(DeepContextPayload::default()))
}
