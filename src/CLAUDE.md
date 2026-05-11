# src/CLAUDE.md — React Rules

## Scope
Rules for React frontend only. Root CLAUDE.md has full IPC schema and build order.

---

## Tech Stack — Exact Versions

```json
{
  "@tauri-apps/api": "^2.0.0",
  "@tauri-apps/plugin-shell": "^2.0.0",
  "@floating-ui/react": "^0.26.0",
  "react": "^18.3.0",
  "react-dom": "^18.3.0",
  "react-router-dom": "^6.26.0"
}
```

```json
{
  "devDependencies": {
    "@tauri-apps/cli": "^2.0.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0"
  }
}
```

- No Redux, no Zustand — `useReducer` + React context
- No direct audio capture — sidecar owns all audio
- `tsconfig.json`: `"strict": true` — no `any` escapes

---

## Tauri API Usage

```ts
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
```

- Never `window.__TAURI__` directly
- All `invoke<T>()` calls must declare return type generic
- Rust errors arrive as rejected promises with string message — always catch

---

## Sidecar Port Discovery

Sidecar may run on port 8000–8009 (port conflict fallback). All HTTP and WebSocket URLs must use a dynamic port:

```ts
const DEFAULT_PORT = 8000;

async function discoverSidecarPort(): Promise<number> {
  // Try ports 8000-8009 until one responds to /health
  for (let port = 8000; port <= 8009; port++) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(500) });
      if (resp.ok) return port;
    } catch {
      continue;
    }
  }
  return DEFAULT_PORT; // fallback; sidecar not yet ready
}

// Store in module-level ref; re-discover after sidecar restart
let sidecarPort = DEFAULT_PORT;
```

All HTTP fetch calls and the WebSocket URL must use `sidecarPort`:
```ts
const WS_URL = () => `ws://127.0.0.1:${sidecarPort}/ws/dictation`;
const API = (path: string) => `http://127.0.0.1:${sidecarPort}${path}`;
```

Re-run `discoverSidecarPort()` when `sidecar-restarting` event fires, before attempting reconnect.

---

## State Machine

Single `useReducer` at App root. All async outcomes dispatch actions — no ad-hoc `useState` for flow state.

```ts
type AppState =
  | { status: "waiting_for_models" }           // sidecar pre-warming
  | { status: "degraded"; missing: string[] }  // some models failed
  | { status: "idle" }
  | { status: "recording" }                    // hotkey held, mic open
  | { status: "streaming"; partial: string }   // receiving partial_updates
  | { status: "finalizing" }                   // handoff_ready received, Canary done
  | { status: "injecting" }                    // inject_text in progress
  | { status: "error"; message: string }

type AppAction =
  | { type: "MODELS_READY" }
  | { type: "MODELS_DEGRADED"; missing: string[] }
  | { type: "START_RECORDING" }
  | { type: "STOP_RECORDING" }
  | { type: "CANCEL_RECORDING" }
  | { type: "PARTIAL_UPDATE"; content: string }
  | { type: "HANDOFF_READY"; canaryTranscript: string }
  | { type: "INJECTION_DONE" }
  | { type: "SIDECAR_RESTARTING" }
  | { type: "ERROR"; message: string }
  | { type: "RESET" }
```

Reducer rules:
- `START_RECORDING` only valid from `idle` — ignore if already recording
- `STOP_RECORDING` valid from `recording` — transitions to `finalizing` (Canary pass in progress)
- `CANCEL_RECORDING` valid from `recording` or `streaming` — reset to `idle`
- `HANDOFF_READY` transitions from `finalizing` to `injecting`
- `INJECTION_DONE` transitions from `injecting` to `idle`
- `SIDECAR_RESTARTING` from any state → `waiting_for_models`; re-run `discoverSidecarPort()` before WS reconnect
- `ERROR` from any state — show error, auto-reset after 4s via `setTimeout → dispatch(RESET)`

---

## Tauri Event Listeners

Correct async useEffect pattern (note: useEffect callback is sync; use `.then()` for promise):

```ts
useEffect(() => {
  let unlisten: (() => void) | undefined;

  listen<null>("hotkey-pressed", () => {
    dispatch({ type: "START_RECORDING" });
  }).then(fn => { unlisten = fn; });

  return () => { unlisten?.(); };
}, []);
```

Register all Tauri event listeners in one `useEffect` in `App.tsx`:

```ts
useEffect(() => {
  const cleanups: Array<() => void> = [];
  
  const reg = async () => {
    cleanups.push(await listen<null>("hotkey-pressed", () => {
      dispatch({ type: "START_RECORDING" });
    }));
    
    cleanups.push(await listen<null>("hotkey-released", () => {
      // terminateStream sends the WebSocket command; STOP_RECORDING drives state machine
      terminateStream();
      dispatch({ type: "STOP_RECORDING" });
    }));
    
    cleanups.push(await listen<null>("models-ready", () => {
      dispatch({ type: "MODELS_READY" });
    }));
    
    cleanups.push(await listen<{ missing: string[] }>("sidecar-degraded", (e) => {
      dispatch({ type: "MODELS_DEGRADED", missing: e.payload.missing });
    }));
    
    cleanups.push(await listen<null>("sidecar-restarting", () => {
      dispatch({ type: "SIDECAR_RESTARTING" });
    }));
  };
  
  reg();
  return () => cleanups.forEach(fn => fn());
}, []);
```

---

## WebSocket — Direct to Sidecar

```ts
function useWebSocket(dispatch: Dispatch<AppAction>, onHandoff: (t: string) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout>>();

  const retryDelayRef = useRef(500); // separate from retryRef (which holds timer ID)

  const connect = useCallback(() => {
    const ws = new WebSocket(WS_URL());
    wsRef.current = ws;

    ws.onopen = () => {
      retryDelayRef.current = 500; // reset backoff on successful connect
    };

    ws.onmessage = (event) => {
      const msg: WsMessage = JSON.parse(event.data);
      if (msg.type === "partial_update" && msg.content) {
        dispatch({ type: "PARTIAL_UPDATE", content: msg.content });
      } else if (msg.type === "handoff_ready" && msg.canary_transcript) {
        dispatch({ type: "HANDOFF_READY", canaryTranscript: msg.canary_transcript });
        onHandoff(msg.canary_transcript);
      } else if (msg.type === "error") {
        dispatch({ type: "ERROR", message: msg.message ?? "Sidecar error" });
      }
    };

    ws.onclose = () => {
      // exponential backoff: use retryDelayRef (not retryRef which is a timer ID)
      const delay = Math.min(30000, retryDelayRef.current);
      retryDelayRef.current = delay * 2;
      retryRef.current = setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();
  }, [dispatch, onHandoff]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      clearTimeout(retryRef.current);
    };
  }, [connect]);

  return wsRef;
}
```

Send commands:
```ts
const beginStream = () => wsRef.current?.send(JSON.stringify({ command: "begin_stream" }));
const terminateStream = () => wsRef.current?.send(JSON.stringify({ command: "terminate_stream" }));
const cancelStream = () => wsRef.current?.send(JSON.stringify({ command: "cancel_stream" }));
```

---

## Context Capture + Injection Flow

Capture context at hotkey-press moment (target window still focused):

```ts
// in START_RECORDING handler
const contextRef = useRef<DeepContextPayload | null>(null);

// on hotkey-pressed:
invoke<DeepContextPayload>("get_focused_context")
  .then(ctx => { contextRef.current = ctx; })
  .catch(() => { contextRef.current = null; });
beginStream();
```

After `handoff_ready` + Canary transcript arrives:

```ts
async function handleHandoff(canaryTranscript: string) {
  const ctx = contextRef.current;
  const body: ProcessTextRequest = {
    raw_transcript: canaryTranscript,   // ← Canary output, not Turbo accumulated
    context_string: ctx?.window_title ?? "",
    executable_name: ctx?.executable_name ?? "",
    window_title: ctx?.window_title ?? "",
    inferred_extension: ctx?.inferred_extension ?? null,
    text_preceding_cursor: ctx?.text_preceding_cursor ?? "",
    text_succeeding_cursor: ctx?.text_succeeding_cursor ?? "",
    use_local_llm: settings.useLlm && !settings.useGroq,
    custom_vocabulary: settings.vocabulary,
  };

  try {
    const resp = await fetch(API("/process-text"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(45000), // 45s — covers max 30s Canary + 10s LLM
    });
    const { cleaned_text, error } = await resp.json();
    if (error) console.warn("process-text error:", error); // non-fatal; cleaned_text is raw fallback
    await invoke("inject_text", { text: cleaned_text });
  } catch (e) {
    // fetch failed entirely — inject raw transcript as last resort
    await invoke("inject_text", { text: canaryTranscript });
  } finally {
    dispatch({ type: "INJECTION_DONE" });
    contextRef.current = null;
  }
}
```

---

## Cancel Recording

`CANCEL_RECORDING` can be triggered by:
- Second hotkey press while recording (toggle)
- Escape key listener on main window

```ts
cleanups.push(await listen<null>("hotkey-pressed", () => {
  if (stateRef.current.status === "recording" || stateRef.current.status === "streaming") {
    cancelStream();
    dispatch({ type: "CANCEL_RECORDING" });
  } else if (stateRef.current.status === "idle") {
    dispatch({ type: "START_RECORDING" });
    beginStream();
  }
}));
```

Use `useRef` for state in event listeners (closure captures stale state otherwise).

---

## Components

### `StreamingOverlay`

- Transparent window via Tauri — renders as always-on-top overlay
- Positioned at screen bottom center by Rust on startup (`set_position` in setup hook); do not hardcode position in CSS or tauri.conf.json
- Visible states: `recording`, `streaming`, `finalizing`, `injecting`
- `recording`: pulsing mic icon
- `streaming`: live text with cursor blink
- `finalizing`: spinner + "Processing..."
- `injecting`: brief "Injecting..." then fade out
- `degraded` banner: shows which models are missing
- Fade out duration: 200ms on transition to `idle`

```tsx
<StreamingOverlay
  status={state.status}
  partial={state.status === "streaming" ? state.partial : ""}
  degraded={state.status === "degraded" ? state.missing : []}
/>
```

### `Settings`

Route: `/settings` (separate Tauri window, opened via `invoke("open_settings_window")`)

Controls:
- LLM toggle: `use_local_llm` boolean → persisted to `localStorage["vox_settings"]`
- LLM provider: Local / Groq (when Groq selected, show API key field)
- Groq API key: **NOT stored in localStorage** — POSTed to sidecar via `POST /finetune/groq-test` which also saves it to `DATA_DIR/groq_key.txt`. Frontend only stores a boolean `hasGroqKey` and sends the key once on save. Never expose the key again in UI after save.
  ```ts
  async function saveGroqKey(apiKey: string) {
    const resp = await fetch(API("/finetune/groq-test"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, raw_transcript: "test" }),
    });
    const data = await resp.json();
    if (data.ok) {
      updateSettings({ hasGroqKey: true });
    } else {
      // show error: data.error
    }
  }
  ```
- Audio device: dropdown from `GET /audio/devices` on mount
- Hotkey: read-only display of current binding (`Ctrl+Shift+Space`)

Settings shape:
```ts
interface VoxSettings {
  useLlm: boolean;
  useGroq: boolean;
  hasGroqKey: boolean;   // true if key was sent to sidecar; key itself NOT stored in frontend
  audioDeviceIndex: number;
  vocabulary: string[];  // mirrors CustomVocabulary state; also POSTed to /vocabulary on change
  passiveCollectionEnabled: boolean;
}
```

Persist on every change: `localStorage.setItem("vox_settings", JSON.stringify(settings))`.

`passiveCollectionEnabled` changes must also call `POST /finetune/toggle-collection` to sync sidecar state:
```ts
async function setPassiveCollection(enabled: boolean) {
  updateSettings({ passiveCollectionEnabled: enabled });
  await fetch(API("/finetune/toggle-collection"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}
```

### `CustomVocabulary`

Route: embedded in `Settings` or standalone `/vocabulary`

- Text area for adding custom words/phrases (one per line)
- "Save" → `POST /vocabulary` with `{ words: [...] }`
- Show confirmation toast on success
- Changes take effect on next recording session (no restart)

```tsx
const saveVocabulary = async () => {
  await fetch(API("/vocabulary"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ words: vocabularyLines }),
  });
  setConfirmed(true);
  setTimeout(() => setConfirmed(false), 2000);
};
```

### `FineTuningDashboard`

Route: `/finetune` (separate Tauri window)

- On mount: fetch `/finetune/status` to get current `samples` count and `status`
- Passive collection toggle → call `setPassiveCollection(enabled)` from Settings (same function, importable)
- Sample count display: from `/finetune/status` response `.samples`
- "Start Training" button: disabled if `samples < 50`, `status === "running"`, or `models-ready` not received
- Start training:
  ```ts
  await fetch(API("/finetune/start"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ epochs: 3, learning_rate: 3e-4 }),
  });
  ```
- Training progress: poll `API("/finetune/status")` every 2s while `status === "running"`
- Show `epoch` / `total_epochs` and `progress` (0–1 float → percentage)
- On `status === "complete"`: show success toast; stop polling

### Training/Dictation Mutual Exclusion

Both directions must be enforced:

```ts
// 1. Block training start if dictation is active
const trainingDisabled =
  samples < 50 ||
  finetuneStatus.status === "running" ||
  state.status !== "idle"; // dictation in any non-idle state

// 2. Block dictation start if training is running
// In hotkey-pressed handler:
if (finetuneStatus.status === "running") {
  // ignore hotkey — do not dispatch START_RECORDING
  return;
}
```

Display a user-facing message when dictation is blocked by training: "Training in progress — please wait."

---

## TypeScript Types

```ts
interface DeepContextPayload {
  executable_name: string;
  window_title: string;
  inferred_extension: string | null;
  text_preceding_cursor: string;
  text_succeeding_cursor: string;
}

interface ProcessTextRequest {
  raw_transcript: string;
  context_string: string;
  executable_name: string;
  window_title: string;
  inferred_extension: string | null;
  text_preceding_cursor: string;
  text_succeeding_cursor: string;
  use_local_llm: boolean;
  custom_vocabulary: string[];
}

interface WsMessage {
  type: "partial_update" | "handoff_ready" | "error";
  content?: string;          // partial_update
  canary_transcript?: string; // handoff_ready
  message?: string;          // error
}

interface AudioDevice {
  index: number;
  name: string;
  default: boolean;
}

interface FinetuneStatus {
  status: "running" | "idle" | "complete" | "error";
  progress: number;
  epoch: number;
  total_epochs: number;
  samples: number;
  error: string | null;
}
```

---

## Routing

```tsx
// main.tsx
<BrowserRouter>
  <Routes>
    <Route path="/" element={<App />} />
    <Route path="/settings" element={<Settings />} />
    <Route path="/finetune" element={<FineTuningDashboard />} />
  </Routes>
</BrowserRouter>
```

- `App` runs in transparent main window — event listeners, overlay, state machine
- Settings and FineTune run in separate opaque Tauri windows
- Each window has its own React tree; use `localStorage` for cross-window state

---

## State Ref Pattern for Stale Closures

When Tauri event listeners need current state, use a ref synced to state:

```ts
const stateRef = useRef(state);
useEffect(() => { stateRef.current = state; }, [state]);
```

Always read `stateRef.current` inside `listen` callbacks, not `state`.

---

## Settings Persistence Pattern

```ts
const SETTINGS_KEY = "vox_settings";

function loadSettings(): VoxSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...JSON.parse(raw) } : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(s: VoxSettings) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}
```

Spread `DEFAULT_SETTINGS` first so new fields added in future versions get their defaults.

---

## Constraints (Never Violate)

1. No audio capture in React — sidecar owns audio
2. WebSocket and HTTP URLs use `sidecarPort` variable — never hardcode `8000`
3. All `listen` subscriptions cleaned up in `useEffect` return
4. `get_focused_context` invoked at hotkey-press moment, stored in ref — not re-queried after release
5. State transitions only via `dispatch` — no direct `setState` for flow control
6. `@tauri-apps/api` v2 imports — no v1 shims
7. No `any` types — all payloads have TypeScript interfaces
8. `canary_transcript` from `handoff_ready` is the input to `/process-text` — never the accumulated Turbo partial text
9. `stateRef` pattern in all Tauri event listener closures — prevents stale state reads
10. Settings persisted to `localStorage` on every change — survive app restart
11. Groq API key never stored in `localStorage` — send once to sidecar, store `hasGroqKey: boolean` only
12. `/process-text` fetch has 45s timeout (`AbortSignal.timeout(45000)`) — always inject raw on timeout
13. `passiveCollectionEnabled` toggle must sync to sidecar via `POST /finetune/toggle-collection`
14. Re-run `discoverSidecarPort()` on `sidecar-restarting` event before WebSocket reconnect
