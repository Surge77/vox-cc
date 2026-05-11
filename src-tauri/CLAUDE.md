# src-tauri/CLAUDE.md — Rust Rules

## Scope
Rules for Rust/Tauri layer only. Root CLAUDE.md has IPC schema, endpoint specs, and build order.

---

## Toolchain

- Target: `stable-x86_64-pc-windows-msvc` — not GNU
- Requires "Desktop development with C++" in VS Build Tools 2022
- Verify: `rustup show` must list `x86_64-pc-windows-msvc` as active
- `LIBCLANG_PATH` env var if bindgen in dep tree (`.cargo/config.toml`):
  ```toml
  [env]
  LIBCLANG_PATH = "C:\\Program Files\\LLVM\\bin"
  ```

---

## Cargo.toml — Exact Crate Versions

```toml
[dependencies]
tauri = { version = "2", features = ["devtools", "tray-icon"] }
tauri-plugin-shell = "2"
tauri-plugin-global-shortcut = "2"
tauri-plugin-notification = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.12", features = ["json"] }   # health poll HTTP client
dirs = "5.0"                                           # cross-platform home dir (~/.vox)

uiautomation = "0.3.7"
enigo = "0.2.1"
arboard = "3.4.0"
windows = { version = "0.52.0", features = [
    "Win32_UI_Accessibility",
    "Win32_Foundation",
    "Win32_UI_WindowsAndMessaging",
    "Win32_System_Com",
    "Win32_System_Threading",
    "Win32_System_ProcessStatus",
    "Win32_Security",               # GetTokenInformation for UAC elevation check
] }

[build-dependencies]
tauri-build = { version = "2", features = [] }
```

Do not upgrade crate versions without testing — `uiautomation` 0.3.x API differs from 0.4.x.

---

## Project Structure

```
src-tauri/
  src/
    main.rs               # CoInitializeEx, app builder, tray, window setup
    lib.rs                # invoke_handler registration
    commands/
      mod.rs
      context.rs          # get_focused_context
      inject.rs           # inject_text
      windows.rs          # open_settings_window, open_finetune_window
    sidecar/
      manager.rs          # spawn, health poll, crash recovery, port.lock reading
      events.rs           # emit hotkey-*, models-ready, sidecar-degraded, sidecar-restarting
  tauri.conf.json
  capabilities/
    default.json
  build.rs
  icons/                  # required by Tauri build
    icon.ico
    tray.ico
```

Note: `commands/finetune.rs` does NOT exist — fine-tuning is triggered by React via `POST /finetune/start` HTTP directly to sidecar, not via a Tauri command.

---

## COM Threading Rules

UIA requires STA (Single-Threaded Apartment). Violating this causes `E_INVALIDINTERFACE`.

```rust
// in main.rs, FIRST LINE of main(), before anything else
unsafe {
    windows::Win32::System::Com::CoInitializeEx(
        None,
        windows::Win32::System::Com::COINIT_APARTMENTTHREADED,
    ).ok().expect("CoInitializeEx STA init failed");
}
```

- Call `CoInitializeEx` exactly once on main thread before any UIA or Win32 call
- UIA commands run on this thread via `tokio::task::spawn_blocking`
- Never call UIA from `async fn` — threading violation panic

---

## Sidecar Lifecycle Manager

File: `src/sidecar/manager.rs`

### Startup

```rust
pub fn spawn_sidecar(app: &tauri::AppHandle) -> Result<tauri_plugin_shell::process::CommandChild, String> {
    use tauri_plugin_shell::ShellExt;
    let sidecar = app.shell()
        .sidecar("sidecar")
        .map_err(|e| e.to_string())?;
    sidecar.spawn().map_err(|e| e.to_string())
}
```

### Port Lock Reading

```rust
fn read_port_lock() -> Option<u16> {
    let home = dirs::home_dir()?;
    let path = home.join(".vox").join("data").join("port.lock");
    let content = std::fs::read_to_string(path).ok()?;
    content.trim().parse::<u16>().ok()
}
```

Use `dirs::home_dir()` (from `dirs` crate) — never hardcode `C:\Users\...` or use `~` string expansion in Rust.

### Health Poll Loop

Run in background task after spawn. Re-reads port.lock on every iteration because the sidecar writes this file after startup (may not exist yet at first poll):

```rust
pub async fn await_sidecar_ready(app: tauri::AppHandle) {
    let client = reqwest::Client::new();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    
    loop {
        if std::time::Instant::now() > deadline {
            // sidecar failed to start — emit error, continue (degraded)
            app.emit("sidecar-degraded", serde_json::json!({"missing": ["all"]})).ok();
            return;
        }
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        // Re-read port every iteration — sidecar writes port.lock after binding
        let port = read_port_lock().unwrap_or(8000);
        let url = format!("http://127.0.0.1:{}/health", port);
        if let Ok(resp) = client.get(&url).timeout(std::time::Duration::from_millis(500)).send().await {
            if resp.status().is_success() {
                let body: serde_json::Value = resp.json().await.unwrap_or_default();
                let models = &body["models"];
                let mut missing: Vec<&str> = vec![];
                if models["canary"] != true { missing.push("canary"); }
                if models["llm"] != true { missing.push("llm"); }
                if !missing.is_empty() {
                    app.emit("sidecar-degraded", serde_json::json!({"missing": missing})).ok();
                }
                app.emit("models-ready", ()).ok();
                return;
            }
        }
    }
}
```

### Crash Recovery

Monitor sidecar process; restart on unexpected exit:

```rust
pub async fn monitor_sidecar(mut child: tauri_plugin_shell::process::CommandChild, app: tauri::AppHandle) {
    loop {
        match child.wait().await {
            Ok(status) => {
                if !status.success() {
                    app.emit("sidecar-restarting", ()).ok();
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    match spawn_sidecar(&app) {
                        Ok(new_child) => {
                            tokio::spawn(await_sidecar_ready(app.clone()));
                            child = new_child;
                        }
                        Err(e) => {
                            eprintln!("Sidecar respawn failed: {e}");
                            app.emit("sidecar-degraded", serde_json::json!({"missing": ["all"]})).ok();
                            return;
                        }
                    }
                } else {
                    return; // clean exit (app shutdown)
                }
            }
            Err(e) => {
                eprintln!("Sidecar monitor error: {e}");
                return;
            }
        }
    }
}
```

### Graceful Shutdown

Hold sidecar child process handle in a `Mutex<Option<tauri_plugin_shell::process::CommandChild>>` managed state. On app quit, kill it:

```rust
// In lib.rs, register state:
struct SidecarChild(Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

// After spawn_sidecar:
app.manage(SidecarChild(Mutex::new(Some(child))));

// In tray quit handler or on_window_event:
fn quit_app(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<SidecarChild>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
    std::process::exit(0);
}
```

Never use `AtomicU32` PID + Win32 `TerminateProcess` — Tauri's `CommandChild` already owns the process handle; use `.kill()` on it directly.

---

## System Tray

```rust
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::menu::{MenuBuilder, MenuItemBuilder};

let open_settings = MenuItemBuilder::with_id("open_settings", "Settings").build(app)?;
let quit = MenuItemBuilder::with_id("quit", "Quit Vox").build(app)?;
let menu = MenuBuilder::new(app).items(&[&open_settings, &quit]).build()?;

TrayIconBuilder::new()
    .icon(app.default_window_icon().unwrap().clone())
    .menu(&menu)
    .tooltip("Vox — Press Ctrl+Shift+Space to dictate")
    .on_menu_event(|app, event| match event.id.as_ref() {
        "open_settings" => { let _ = open_settings_window(app); }
        "quit" => { std::process::exit(0); }
        _ => {}
    })
    .build(app)?;
```

App minimizes to tray on main window close — does not quit. Main window is transparent/frameless overlay; it should never show in taskbar.

---

## Window Configuration

`tauri.conf.json` windows section:

```json
{
  "windows": [
    {
      "label": "main",
      "title": "Vox",
      "transparent": true,
      "decorations": false,
      "alwaysOnTop": true,
      "skipTaskbar": true,
      "visible": false,
      "width": 420,
      "height": 120,
      "resizable": false,
      "fullscreen": false
    }
  ]
}
```

Note: `x` and `y` are omitted intentionally. Position is set programmatically on startup to center the overlay at the bottom of the primary monitor (see below). Hardcoding `x:0, y:0` places it at the top-left corner — wrong for an overlay.

### Main Window Positioning (Programmatic)

On app startup (in the Tauri setup hook, after main window is created), position the overlay at the horizontal center of the primary monitor, 80px above the bottom edge:

```rust
use tauri::Manager;

// In setup hook:
let window = app.get_webview_window("main").unwrap();
if let Some(monitor) = window.primary_monitor().ok().flatten() {
    let monitor_size = monitor.size();
    let window_size = window.outer_size().unwrap_or_default();
    let x = (monitor_size.width as i32 - window_size.width as i32) / 2;
    let y = monitor_size.height as i32 - window_size.height as i32 - 80;
    let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
}
```

This runs once at startup. The window position is fixed for the session; it does not follow the cursor.

### Main Window Show/Hide

Main window starts `visible: false`. The React frontend shows/hides it based on state:

```ts
// Show when recording starts (hotkey-pressed handler in App.tsx)
import { getCurrentWindow } from "@tauri-apps/api/window";

listen<null>("hotkey-pressed", async () => {
  await getCurrentWindow().show();
  dispatch({ type: "START_RECORDING" });
});

// Hide when returning to idle (after INJECTION_DONE or CANCEL_RECORDING)
// In reducer side effect or useEffect watching state.status:
useEffect(() => {
  if (state.status === "idle") {
    getCurrentWindow().hide();
  }
}, [state.status]);
```

Window stays on top of all other windows while visible (`alwaysOnTop: true`). `skipTaskbar: true` keeps it out of the Windows taskbar.

Settings and FineTune windows are created programmatically (not in conf) via `tauri::WebviewWindowBuilder`:

```rust
pub fn open_settings_window(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("settings") {
        w.set_focus().map_err(|e| e.to_string())?;
        return Ok(());
    }
    tauri::WebviewWindowBuilder::new(app, "settings", tauri::WebviewUrl::App("/settings".into()))
        .title("Vox Settings")
        .inner_size(600.0, 500.0)
        .resizable(false)
        .decorations(true)
        .skip_taskbar(false)
        .build()
        .map(|_| ())
        .map_err(|e| e.to_string())
}
```

Same pattern for `open_finetune_window` with label `"finetune"` and path `"/finetune"`.

---

## `get_focused_context` Command

File: `src/commands/context.rs`

Algorithm (in order, short-circuit on success):
1. `GetForegroundWindow()` → HWND; return default if null
2. `executable_name`: `GetWindowThreadProcessId` → `OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)` → `GetModuleFileNameExW` → `Path::file_name()` → lowercase, strip `.exe`
3. `window_title`: `GetWindowTextW`
4. `inferred_extension`: check title for `.py .ts .rs .go .cpp .cs .md .txt`; fallback to exe-name heuristic
5. UIA `TextPattern2` — extract text before and after cursor:
   ```rust
   // Get element from foreground window
   let ui_auto = UIAutomation::new().map_err(|_| ())?;
   let root = ui_auto.element_from_handle(hwnd).map_err(|_| ())?;
   // Traverse to focused element (TextPattern may be on child, not root)
   let focused = ui_auto.get_focused_element().map_err(|_| ())?;
   
   // Try TextPattern2 on focused element
   let pattern: IUIAutomationTextPattern2 = focused.get_pattern().map_err(|_| ())?;
   let selection_ranges = pattern.GetSelection().map_err(|_| ())?;
   let selection = selection_ranges.GetElement(0).map_err(|_| ())?;
   
   // Range from doc start to cursor: clone selection, move start to doc beginning
   let doc_start = pattern.DocumentRange().map_err(|_| ())?;
   let preceding_range = doc_start.Clone().map_err(|_| ())?;
   preceding_range.MoveEndpointByRange(
       TextPatternRangeEndpoint_End,
       &selection,
       TextPatternRangeEndpoint_Start,
   ).map_err(|_| ())?;
   let preceding_text = preceding_range.GetText(-1).unwrap_or_default();
   
   // Range from cursor to doc end
   let doc_end = pattern.DocumentRange().map_err(|_| ())?;
   let succeeding_range = doc_end.Clone().map_err(|_| ())?;
   succeeding_range.MoveEndpointByRange(
       TextPatternRangeEndpoint_Start,
       &selection,
       TextPatternRangeEndpoint_End,
   ).map_err(|_| ())?;
   let succeeding_text = succeeding_range.GetText(200).unwrap_or_default(); // limit chars
   ```
6. If UIA fails at any step: return empty strings for cursor context (non-fatal)
7. Truncate: `text_preceding_cursor` = last 300 chars of `preceding_text`; `text_succeeding_cursor` = first 100 chars of `succeeding_text`

Return type:
```rust
#[derive(serde::Serialize, Default)]
pub struct DeepContextPayload {
    pub executable_name: String,
    pub window_title: String,
    pub inferred_extension: Option<String>,
    pub text_preceding_cursor: String,
    pub text_succeeding_cursor: String,
}
```

Any OS error → `Ok(DeepContextPayload::default())` — never propagate panics to frontend.

Must run in `tokio::task::spawn_blocking` due to COM threading requirement:
```rust
#[tauri::command]
pub async fn get_focused_context() -> Result<DeepContextPayload, String> {
    tokio::task::spawn_blocking(get_focused_context_sync)
        .await
        .map_err(|e| e.to_string())?
}
```

---

## `inject_text` Command

File: `src/commands/inject.rs`

Strategy order: try UIA SetValue → fallback to clipboard swap.

Clipboard swap:
```rust
use arboard::Clipboard;
use enigo::{Enigo, Key, Keyboard, Settings};

fn clipboard_inject(text: &str) -> Result<(), String> {
    let mut clipboard = Clipboard::new().map_err(|e| e.to_string())?;
    let saved = clipboard.get_text().unwrap_or_default();
    
    clipboard.set_text(text).map_err(|e| e.to_string())?;
    std::thread::sleep(std::time::Duration::from_millis(50));
    
    let mut enigo = Enigo::new(&Settings::default()).map_err(|e| e.to_string())?;
    enigo.key(Key::Control, enigo::Direction::Press).map_err(|e| e.to_string())?;
    enigo.key(Key::Unicode('v'), enigo::Direction::Click).map_err(|e| e.to_string())?;
    enigo.key(Key::Control, enigo::Direction::Release).map_err(|e| e.to_string())?;
    
    std::thread::sleep(std::time::Duration::from_millis(150));
    clipboard.set_text(&saved).map_err(|e| e.to_string())?;
    Ok(())
}
```

Electron/Chrome detection — check window class name:
```rust
fn get_window_class(hwnd: HWND) -> String {
    let mut buf = [0u16; 256];
    unsafe {
        windows::Win32::UI::WindowsAndMessaging::GetClassNameW(hwnd, &mut buf);
    }
    String::from_utf16_lossy(&buf).trim_matches('\0').to_string()
}

fn is_electron_window(hwnd: HWND, exe_name: &str) -> bool {
    let class = get_window_class(hwnd);
    class == "Chrome_WidgetWin_1" || exe_name.contains("electron")
}
```

If `is_electron_window`:
- Send `End` key to ensure cursor at end of input field before paste
- Do NOT send `Ctrl+A` — that selects ALL text, replacing entire document

Clipboard retry on "already in use":
```rust
fn clipboard_inject(text: &str) -> Result<(), String> {
    let mut attempts = 0;
    let mut clipboard = loop {
        match arboard::Clipboard::new() {
            Ok(c) => break c,
            Err(e) if attempts < 3 => {
                attempts += 1;
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            Err(e) => return Err(e.to_string()),
        }
    };
    // ... rest of inject
}
```

`inject_text` command also runs in `spawn_blocking` — arboard touches COM.

---

## Global Hotkey

```rust
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

app.global_shortcut().on_shortcut("CommandOrControl+Shift+Space", move |app, _shortcut, event| {
    match event.state() {
        ShortcutState::Pressed  => { app.emit("hotkey-pressed", ()).ok(); }
        ShortcutState::Released => { app.emit("hotkey-released", ()).ok(); }
    }
})?;
```

- Hotkey fires even when Vox window is not focused — that is the point
- If `get_focused_context` must be called at press time, emit `hotkey-pressed` first, then call it async

---

## Tauri Configuration

`capabilities/default.json`:
```json
{
  "identifier": "default",
  "windows": ["main", "settings", "finetune"],
  "permissions": [
    "core:default",
    "shell:allow-execute",
    "shell:allow-spawn",
    "global-shortcut:allow-register",
    "global-shortcut:allow-unregister"
  ]
}
```

`tauri.conf.json` bundle section:
```json
{
  "bundle": {
    "active": true,
    "targets": ["nsis"],
    "externalBin": ["binaries/sidecar"],
    "windows": {
      "digestAlgorithm": "sha256",
      "certificateThumbprint": null
    }
  }
}
```

---

## Common Windows Rust Errors Reference

| Error | Cause | Fix |
|-------|-------|-----|
| `0x80004005 E_FAIL` on UIA | Element destroyed between query and read | Wrap in `?`, return default |
| `0x800401F0 CO_E_NOTINITIALIZED` | COM not initialized | `CoInitializeEx` before any UIA |
| `linker error: cannot find -lWindowsApp` | GNU toolchain | Switch to MSVC |
| `failed to run custom build command for windows-sys` | Missing Windows SDK | Install "Windows 10 SDK" in VS Build Tools |
| `arboard: clipboard already in use` | Enigo/arboard conflict | 50ms sleep between set and paste |
| `tauri_plugin_shell: binary not found` | Wrong sidecar suffix | Binary must match `{name}-x86_64-pc-windows-msvc.exe` |
| `COINIT_MULTITHREADED conflict` | Third-party crate called CoInitialize first | Move CoInitializeEx to absolute first line of main |
| `inject_text fails silently on UAC-elevated target` | enigo runs as standard user, target runs as admin | Detect with `GetTokenInformation`; show warning: "Run Vox as Administrator to dictate into elevated apps" |
| `sidecar binary not found at runtime` | Tauri looks for exact name `sidecar-x86_64-pc-windows-msvc.exe` | Verify filename exactly; Tauri adds the triple suffix automatically — do not include triple in `externalBin` path |

---

## Code Quality Rules

- No `.unwrap()` in production — use `?` / `.unwrap_or_default()` / `.map_err(|e| e.to_string())`
- `unsafe` allowed only for `CoInitializeEx` call; document with comment
- All `#[tauri::command]` return `Result<T, String>` — never panic across FFI boundary
- `spawn_blocking` for UIA/Win32/COM calls from async context
- Shared state: `tauri::State<Arc<Mutex<T>>>` — no `static mut`
- Sidecar child handle: `SidecarChild(Mutex<Option<CommandChild>>)` managed state — call `.kill()` on `CommandChild` directly, never `AtomicU32` + `TerminateProcess`

---

## Constraints (Never Violate)

1. MSVC toolchain only — no GNU
2. `CoInitializeEx(COINIT_APARTMENTTHREADED)` as first line of `main()`, before Tauri builder
3. UIA/COM calls inside `spawn_blocking`
4. Clipboard restore: 150ms minimum after Ctrl+V
5. Sidecar spawned once in setup hook; auto-restarted by monitor on crash
6. `models-ready` emitted only after health check passes AND VRAM plan is known
7. `sidecar-degraded` emitted with `missing` array when any model absent
8. Main window: transparent, no decorations, `skipTaskbar: true`
9. App exits to tray on window close — not process exit
10. Kill sidecar on actual app quit before `std::process::exit`
