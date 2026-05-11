# Vox — Global Architecture Directives

## Target Hardware Configuration

This project is being built and tested on the following machine. All VRAM budgets, load plans, and model choices must account for this configuration:

| Component | Spec |
|-----------|------|
| CPU | Intel Core i5-12450H, 8 cores, 2.0 GHz base |
| RAM | 16 GB |
| GPU | NVIDIA GeForce GTX 1650, **4 GB VRAM** |
| OS | Windows 11 Home x64 |
| Python | 3.11.9 |
| Rust | 1.90.0 (stable-x86_64-pc-windows-msvc) |
| Node | v24.13.1 |
| NVIDIA Driver | 591.86 |

**VRAM constraint impact:** 4GB VRAM triggers the `canary: "skip"` branch of the load plan. `nvidia/canary-qwen-2.5b` (3.2GB, 8-bit) cannot coexist with Turbo (1.2GB) in 4GB.

**Workaround — distil-whisper final pass:** Use `distil-whisper/distil-large-v3` as the final pass model instead of Canary. Same faster-whisper API, ~1.5GB INT8, ~6.9% WER (vs Turbo's 10.6%). Load sequentially: unload Turbo → load distil-large-v3 → run final pass → reload Turbo for next session. Eliminates HuggingFace/bitsandbytes dependency entirely. This is the recommended configuration for GPUs with ≤4GB VRAM.

---

## Project Overview

Windows-exclusive, local speech-to-text dictation desktop application.
No audio or text ever leaves the machine. Zero cloud dependency for core pipeline.

**Stack:**
- **Frontend:** React 18 + TypeScript, rendered via Edge WebView2 (Tauri v2)
- **Backend:** Rust (Tauri v2), target triple `x86_64-pc-windows-msvc`
- **ML Sidecar:** Python 3.11, FastAPI + uvicorn, packaged with PyInstaller (`--onedir`)
- **ASR (streaming preview):** `openai/whisper-large-v3-turbo` via faster-whisper CTranslate2, INT8
- **ASR (final accuracy pass):** `nvidia/canary-qwen-2.5b` via Transformers, 8-bit bitsandbytes (8GB+ GPU); `distil-whisper/distil-large-v3` via faster-whisper INT8 sequential swap (≤4GB GPU — **this machine**)
- **LLM post-processing:** `qwen2.5-3b-instruct.Q4_K_M.gguf` via llama-cpp-python, `n_gpu_layers=-1`

---

## Directory Structure

```
vox/
├── CLAUDE.md                          ← this file
├── package.json
├── src/                               ← React frontend
│   ├── CLAUDE.md
│   ├── App.tsx
│   ├── Settings.tsx
│   ├── StreamingOverlay.tsx
│   ├── FineTuningDashboard.tsx
│   └── CustomVocabulary.tsx
├── sidecar/                           ← Python ML sidecar
│   ├── CLAUDE.md
│   ├── main.py
│   ├── requirements.txt
│   ├── sidecar.spec
│   ├── routers/
│   │   ├── dictation.py
│   │   ├── text_processing.py
│   │   ├── finetuning.py
│   │   ├── health.py
│   │   └── vocabulary.py
│   ├── models/
│   │   ├── dual_loader.py
│   │   ├── prompt_router.py
│   │   └── llm_engine.py
│   ├── audio/
│   │   ├── capture.py
│   │   ├── pipeline.py
│   │   └── vad.py
│   ├── prompts/
│   │   ├── code_editor.txt
│   │   ├── email_client.txt
│   │   ├── chat_messaging.txt
│   │   ├── document_editor.txt
│   │   ├── terminal_interface.txt
│   │   └── neutral_fallback.txt
│   └── data/                          ← runtime data, lives in ~/.vox/data/
│       ├── passive_log.jsonl
│       ├── vocabulary.json
│       ├── settings.json
│       ├── finetune_progress.json
│       └── port.lock
└── src-tauri/                         ← Rust backend
    ├── CLAUDE.md
    ├── Cargo.toml
    ├── build.rs
    ├── tauri.conf.json
    ├── capabilities/
    │   └── default.json
    ├── binaries/
    │   └── sidecar-x86_64-pc-windows-msvc.exe
    └── src/
        ├── main.rs
        ├── lib.rs
        ├── commands/
        │   ├── mod.rs
        │   ├── context.rs
        │   ├── inject.rs
        │   └── windows.rs
        └── sidecar/
            ├── manager.rs
            └── events.rs
```

---

## IPC Schema

### Process Topology

```
React (WebView2)  ←─ Tauri events/commands ─→  Rust (main process)
      │                                                │
      │                                    spawns + monitors sidecar
      │                                                ↓
      └──────── WebSocket / HTTP (localhost:8000–8009) ──→ Python FastAPI
```

Rust's only role at runtime: spawn/monitor sidecar, listen for global hotkeys, execute UIA context extraction, execute clipboard injection, manage windows. Rust does NOT proxy audio or transcript data — the React frontend connects directly to the FastAPI sidecar.

### Tauri Events (Rust → Frontend)

| Event | Payload | Trigger |
|---|---|---|
| `hotkey-pressed` | `{}` | Global hotkey pressed |
| `hotkey-released` | `{}` | Global hotkey released |
| `models-ready` | `{}` | Sidecar pre-warm complete |
| `sidecar-degraded` | `{ "missing": ["final_pass" \| "llm"] }` | Model failed to load at startup |
| `sidecar-restarting` | `{}` | Rust detected sidecar crash, restarting |

### Tauri Commands (Frontend → Rust)

| Command | Returns | Purpose |
|---|---|---|
| `get_focused_context` | `Result<DeepContextPayload, String>` | UIA text extraction |
| `inject_text` | `Result<(), String>` | Clipboard swap + Ctrl+V |
| `open_settings_window` | `Result<(), String>` | Open Settings in second window |
| `open_finetune_window` | `Result<(), String>` | Open FineTune in second window |

### WebSocket: `ws://127.0.0.1:8000/ws/dictation`

**Client → Server:**
```json
{ "command": "begin_stream" }
{ "command": "terminate_stream" }
{ "command": "cancel_stream" }
```

**Server → Client:**
```json
{ "type": "partial_update", "content": "<accumulated_turbo_text>" }
{ "type": "handoff_ready", "canary_transcript": "<full_canary_text>" }
{ "type": "error", "message": "<description>" }
```

Protocol flow:
1. `begin_stream` → sidecar opens mic, starts Turbo streaming
2. `partial_update` messages arrive as Turbo processes 1000ms chunks
3. On `terminate_stream`: sidecar flushes audio buffer, runs final pass model on entire session audio (distil-large-v3 on this machine), sends `handoff_ready` with `canary_transcript`
4. Frontend POSTs `canary_transcript` (not Turbo output) to `/process-text` for LLM cleaning
5. On `cancel_stream`: sidecar stops mic immediately, no final pass, no HTTP call

`canary_transcript` field name is fixed wire format — always used regardless of which final pass model ran. If final pass fails, sidecar falls back to accumulated Turbo text.

### HTTP Endpoints

#### `POST http://127.0.0.1:8000/process-text`

**Request:**
```json
{
  "raw_transcript": "string",
  "context_string": "string",
  "executable_name": "string",
  "window_title": "string",
  "inferred_extension": "string | null",
  "text_preceding_cursor": "string",
  "text_succeeding_cursor": "string",
  "use_local_llm": true,
  "custom_vocabulary": ["word1", "word2"]
}
```

**Response (success):**
```json
{ "cleaned_text": "string" }
```

**Response (failure — always inject raw rather than failing):**
```json
{ "cleaned_text": "<raw_transcript>", "error": "optional detail" }
```

#### `GET http://127.0.0.1:8000/health`

**Response:**
```json
{
  "status": "ok",
  "models": {
    "turbo": true,
    "final_pass": true,
    "llm": true
  },
  "final_pass_type": "distil_sequential",
  "cuda": true,
  "vram_free_mb": 1800
}
```

Used by Rust health polling loop. `status: "ok"` even if some models failed — use `models` map to determine degraded mode. `final_pass_type` identifies which final pass model is active (`canary_cuda`, `canary_cpu`, `distil_sequential`, or `skip`).

#### `GET http://127.0.0.1:8000/audio/devices`

**Response:**
```json
{
  "devices": [
    { "index": 0, "name": "Microphone (Realtek)", "default": true },
    { "index": 1, "name": "Headset (USB)", "default": false }
  ]
}
```

#### `POST http://127.0.0.1:8000/vocabulary`

**Request:**
```json
{ "words": ["Kubernetes", "PyTorch", "Tauri"] }
```

**Response:**
```json
{ "ok": true }
```

Updates the `initial_prompt` string injected into faster-whisper on next `begin_stream`. Persists to `data/vocabulary.json`.

#### `POST http://127.0.0.1:8000/finetune/start`

**Request:**
```json
{ "epochs": 3, "learning_rate": 3e-4 }
```

**Response:**
```json
{ "job_id": "uuid", "status": "started" }
```

#### `GET http://127.0.0.1:8000/finetune/status`

**Response:**
```json
{
  "status": "running | idle | complete | error",
  "progress": 0.42,
  "epoch": 1,
  "total_epochs": 3,
  "samples": 142,
  "error": null
}
```

#### `POST http://127.0.0.1:8000/finetune/toggle-collection`

**Request:**
```json
{ "enabled": true }
```

**Response:**
```json
{ "ok": true, "enabled": true }
```

Toggles passive data collection. State persisted to `DATA_DIR/settings.json`. Frontend calls this from FineTuningDashboard toggle switch.

#### `POST http://127.0.0.1:8000/finetune/groq-test`

**Request:**
```json
{ "api_key": "gsk_...", "raw_transcript": "string" }
```

**Response:**
```json
{ "cleaned_text": "string", "ok": true }
```
or on key failure:
```json
{ "cleaned_text": "", "ok": false, "error": "string" }
```

Saves the API key to `DATA_DIR/groq_key.txt` and validates it with a live call. Used only in Settings to verify API key. Frontend stores `hasGroqKey: boolean` only after `ok: true`.

### `DeepContextPayload` (Rust struct, serialized to JSON)

```rust
pub struct DeepContextPayload {
    pub executable_name: String,
    pub window_title: String,
    pub inferred_extension: Option<String>,
    pub text_preceding_cursor: String,  // max 300 chars
    pub text_succeeding_cursor: String, // max 100 chars
}
```

---

## Absolute Constraints

### Platform
- Target OS: **Windows only** (`x86_64-pc-windows-msvc`)
- Never suggest `xdotool`, `libinput`, `macos-accessibility-client`, `objc2`, or any non-Windows crate
- Tauri uses **Edge WebView2** — no Chromium bundled
- Rust toolchain: `stable-x86_64-pc-windows-msvc` (not GNU)

### CUDA Environment
- Minimum: CUDA Toolkit 12.1 + cuDNN 8.9
- Verify: `python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"`
- Failure → silent CPU fallback → latency targets unachievable
- CUDA OOM is a recoverable error — never crash; degrade gracefully

### Audio Pipeline
- All audio fed to Whisper: **16kHz, mono, float32**
- Consumer mics at 44.1kHz/48kHz — always resample via `scipy.signal.resample_poly`
- Apply noise suppression (noisereduce) before resampling when SNR is low
- Feeding unresampled audio = complete transcription failure

### Streaming Chunking
- Chunk size: **1000ms**, trailing overlap: **200ms**
- Deduplication: `word_timestamps=True`, filter by `word.start > last_valid_timestamp`
- Never feed isolated 1-second non-overlapping chunks (catastrophic context loss)
- LLM post-processing runs **only on final pass model output** (distil-large-v3 or Canary depending on hardware), never on Turbo partial tokens
- Hallucination gate: if audio RMS < 0.01 for entire chunk, do not send to Whisper
- **Max session duration: 60 seconds** — auto-terminate stream if ring buffer exceeds 960,000 samples (16kHz × 60s); send `handoff_ready` automatically; prevents unbounded memory growth
- `beam_size` is faster-whisper API only; HuggingFace `.generate()` uses `num_beams` — never mix these in the same call

### VRAM Budget

| Model | VRAM | Notes |
|-------|------|-------|
| Turbo INT8 | ~1.2 GB | Always loaded |
| Canary 8-bit | ~3.2 GB | 8GB+ GPUs only |
| distil-large-v3 INT8 | ~1.5 GB | 4GB GPU alternative to Canary |
| LLM Q4_K_M | ~2.0 GB | Always loaded |
| **Total (8GB+ config)** | **~6.4 GB** | |
| **Total (4GB config)** | **~4.7 GB sequential** | Turbo+distil+LLM, never all at once |

- **8GB+ GPU:** load all three simultaneously (Turbo + Canary + LLM)
- **6GB GPU:** Canary CPU offload during LLM inference; sequential
- **4GB GPU (this machine):** replace Canary with distil-large-v3; sequential load (unload Turbo before distil pass, unload distil before LLM)
- Check VRAM at startup; adjust load plan accordingly; emit `sidecar-degraded` event if any model fails

### Latency Target

| GPU | Final pass | E2E after hotkey release |
|-----|-----------|--------------------------|
| RTX 3060 (8GB) | Canary CUDA ~120ms | **<300ms** ✓ |
| **GTX 1650 (4GB, this machine)** | distil_sequential ~3–5s | **~5–6s** |
| Any GPU | Final pass skipped | ~80ms (LLM only) |

GTX 1650 note: distil_sequential adds 3–5s (unload Turbo, load distil, transcribe, unload distil). The <300ms target is physically impossible on 4GB hardware with a final pass model. Design the UX to show a "Processing..." spinner during this window — the user experience goal is accurate text, not sub-300ms injection.

- Pre-warm Turbo with zero tensor on startup; distil-large-v3 is NOT pre-warmed (sequential load only)
- LLM pre-warm with single "ping" message

### LLM Residency
- `llama-cpp-python` in-process within FastAPI, `n_gpu_layers=-1`
- **Never** Ollama at runtime — cold-start penalty destroys latency
- Ollama acceptable as offline CPU-only fallback toggle
- Groq API (cloud) acceptable as explicit user opt-in fallback; never default

### Text Injection
- Primary: clipboard swap + Ctrl+V (`enigo` + `arboard`)
- Secondary: UIA `SetValue` where TextPattern2 available
- **Never** sequential character typing — unstable for long text
- Restore original clipboard after injection (150ms delay before restore)

### PyInstaller Packaging
- Mode: **`--onedir`** — NEVER `--onefile`
- Output: `dist/sidecar/sidecar.exe` → copy to `src-tauri/binaries/sidecar-x86_64-pc-windows-msvc.exe`
- Model paths: `sys._MEIPASS` for all bundled assets
- Windows Defender frequently flags PyInstaller output — document exclusion path for users

### Fine-Tuning

Training target is `distil-whisper/distil-large-v3` (756M params) on this machine — NOT Canary (2.5B params).

| Config | 12GB GPU | **4GB GPU (this machine)** |
|--------|----------|---------------------------|
| LoRA r | 32 | **8** |
| alpha | 64 | **16** |
| batch size | 4 | **1** |
| grad accumulation | 4 | **8** (effective batch = 8) |
| LLM during training | loaded | **unloaded** (free 2GB VRAM first) |
| FP16 | ✓ | ✓ |
| gradient checkpointing | ✓ | ✓ |

On 4GB: unload LLM before training (`llm = None; gc.collect(); torch.cuda.empty_cache()`), reload after. Training VRAM with r=8: ~2.5–3GB → fits in 4GB. Set `_training_active = True` to block dictation during training (LLM is unloaded).

- Training: isolated subprocess (HTTP trigger, not Tauri sidecar re-spawn)
- Output: convert via `ct2-transformers-converter --quantization int8 --output_dir ./ct2-model`
- Passive data: only collect with explicit user consent; never transmit

---

## Build Milestones (strict order)

**Phase 1 — Python ML Engine (pure Python, no Tauri)**

1. **Sidecar foundation** — `main.py`, `health.py`, `audio/capture.py`, `audio/vad.py`, VRAM detection, port.lock; gate: `GET /health` returns correct VRAM + device list
2. **Turbo transcription + WebSocket streaming** — `audio/pipeline.py` ring buffer + overlap + hallucination gate; `routers/dictation.py` with begin/terminate/cancel; partial_update sends full accumulated text; handoff_ready sends Turbo text as fallback (Canary not yet loaded); gate: `wscat` test confirms partial_update fires during speech and handoff_ready fires on terminate
3. **Final pass model + VRAM-aware load** — `models/dual_loader.py` with load plan; on this machine loads distil-large-v3 sequential; CUDA prewarm for Turbo only (distil is sequential-loaded, not pre-warmed); handoff_ready now sends final pass output instead of Turbo fallback; gate: `/health` shows `turbo: true, final_pass: true`; distil-large-v3 WER visibly better than Turbo on domain test sentence (~6.9% vs ~10.6%)
4. **LLM post-processing + prompt routing** — `models/llm_engine.py`, `models/prompt_router.py`, `prompts/*.txt`, `routers/text_processing.py`; Groq fallback path; gate: `POST /process-text` with `executable_name: "code"` → no trailing periods; Groq path works when `use_local_llm: false`
5. **Custom vocabulary + passive logging** — `routers/vocabulary.py`, `vocabulary.json`, `initial_prompt` injection per chunk; passive log schema; `POST /finetune/toggle-collection`; gate: add "Kubernetes" → transcribe it → correctly spelled
6. **Fine-tuning pipeline** — `routers/finetuning.py`, LoRA training subprocess, `finetune/status` polling, `finetune/groq-test` key validation; gate: training starts with 50+ samples, progress updates in `finetune_progress.json`
7. **PyInstaller packaging** — `sidecar.spec` with all CUDA + cuDNN DLLs, hiddenimports; gate: `sidecar.exe` boots in fresh `cmd.exe` with no Python env, all three models load, wscat test passes against packaged binary

**Phase 2 — Application Shell (first usable build)**

8. **Tauri shell + sidecar spawn + health polling** — `sidecar/manager.rs`, `sidecar/events.rs`, port.lock reading, crash recovery, `models-ready` / `sidecar-degraded` events, React loading screen; gate: app launches → "Ready" or "Degraded" within 30s; kill sidecar manually → auto-restarts
9. **React state machine + WebSocket client** — App.tsx reducer (all 8 states + transitions), `useWebSocket` hook with exponential backoff reconnect, all Tauri event listeners, `discoverSidecarPort` on startup and restart; gate: state transitions log correctly in console; WebSocket reconnects after sidecar restart; no state gets stuck on error
10. **Global hotkey + end-to-end raw flow** — `Ctrl+Shift+Space` wired; hotkey → begin_stream → partial_update → terminate_stream → handoff_ready → process-text → inject_text fully connected; gate: speak into Notepad → correct formatted text appears; original clipboard restored ← **FIRST COMPLETE WORKING BUILD**

**Phase 3 — Intelligence + Polish**

11. **UIA context extraction + Electron injection fix** — `commands/context.rs`, Electron window class detection, `End` key prefix for Electron paste, elevation mismatch warning; gate: dictating into VS Code fires `code_editor` prompt (no trailing period); dictating into Outlook fires `email` prompt
12. **Streaming overlay + window management** — `StreamingOverlay.tsx`, programmatic window positioning at screen bottom center on startup, partial_update → live text, finalizing spinner, hide after injection; gate: overlay appears during speech, spinner during processing, disappears cleanly after injection
13. **Custom vocabulary UI + Settings** — `CustomVocabulary.tsx`, `Settings.tsx`, Groq API key flow, audio device selector; gate: add word in UI → save → take effect on next session
14. **Fine-tuning dashboard** — `FineTuningDashboard.tsx`, passive collection toggle, training progress polling; gate: toggle enables logging, training starts from UI, progress updates, completes without crashing

Do not mix milestones in one session. Do not build Tauri before sidecar is verified standalone. Do not implement LLM post-processing before the final pass model is wired (M3) — LLM input is the final pass transcript, not raw Turbo text.

---

## Windows-Specific Gotchas

| Issue | Impact | Mitigation |
|-------|--------|------------|
| cuDNN DLLs in different path than CUDA toolkit | PyInstaller packaging fails silently | cuDNN `cudnn64_8.dll` is at `C:\Program Files\NVIDIA\CUDNN\v8.x\bin\`, NOT in CUDA toolkit `bin\` |
| `Ctrl+Shift+Space` conflicts with Windows IME | Hotkey silently stolen by IME on CJK language packs | Document; offer alternative `Ctrl+Alt+Space` binding in Settings |
| enigo cannot inject into UAC-elevated processes | Dictation fails silently in admin-elevated VS, terminals | Detect elevation mismatch via `GetTokenInformation`; show user-facing warning |
| PyAudio requires VC++ Redistributable | Sidecar crashes on systems without MSVC runtime | Bundle `vcredist_x64.exe` in installer or use NSIS to check/install |
| SmartScreen blocks unsigned PyInstaller binary | First-launch blocked on Windows 10/11 | Document: right-click → Properties → Unblock, or buy code-signing cert |
| Windows 11 Clipboard History (`Win+V`) | arboard `set_text` adds every injection to history | Known behavior; document; not a bug |
| Edge WebView2 not present on old Windows 10 | Tauri fails to launch with cryptic error | NSIS installer should check for WebView2 and install `MicrosoftEdgeWebview2Setup.exe` if absent |
| Windows Defender real-time protection | Flags PyInstaller binaries as suspicious, may quarantine | Add `src-tauri/binaries/` to Defender exclusions; document in README |
| PyAudio mic permission denied by Windows privacy | `pyaudio.Open()` returns silence or `IOError` | Call `pyaudio.PyAudio().get_device_info_by_index()` at startup; if mic count = 0, emit error to frontend |
| llama-cpp-python CUDA wheel (GTX 1650, Turing sm_75) | Default PyPI wheel is CPU-only; GPU not used | Install CUDA wheel: `pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121` — this targets sm_75 (Turing) |
| distil_sequential VRAM during swap | After Turbo unload + before distil load: brief window where neither model is in VRAM | LLM (2.0GB) stays loaded throughout swap — only Turbo↔distil swap; never unload LLM during dictation |

---

## AI Workflow Rules

- **Never mix domains in one prompt.** Rust edits and Python edits are separate sessions.
- **Pin all crate and package versions** — do not `cargo add` without explicit version.
- **No `.unwrap()` in production Rust.** Propagate `Result<T, String>`.
- **Use `/plan` before** any complex Rust FFI or PyInstaller spec changes.
- **Warn immediately** if any of these appear in Rust: `xdotool`, `macos-`, `libxdo`, `CoreML`, `objc`.
- **IPC protocol is fixed:** WebSocket for streaming, HTTP for final processing. No stdio pipes.
- **`canary_transcript` field from `handoff_ready` is the input to `/process-text`** — field name is fixed wire format; content is final pass output (distil-large-v3 on this machine) or Turbo fallback. Never use raw Turbo partial accumulation.
