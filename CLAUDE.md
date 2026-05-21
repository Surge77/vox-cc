# Vox — Global Architecture Directives

## Target Hardware Configuration

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

**4 GB VRAM → distil_sequential load plan.** Canary can't coexist with Turbo in 4 GB. Use `distil-whisper/distil-large-v3` (~1.5 GB INT8, ~6.9 % WER) as final-pass. Load sequentially: unload Turbo → load distil → run → reload Turbo. Eliminates HuggingFace/bitsandbytes.

---

## Project Overview

Windows-only local STT dictation. No audio/text leaves the machine.

**Stack:**
- React 18 + TS in Edge WebView2 (Tauri v2)
- Rust backend, `x86_64-pc-windows-msvc`
- Python 3.11 sidecar, FastAPI + uvicorn, PyInstaller `--onedir`
- Turbo (streaming) via faster-whisper CT2 INT8
- Final pass: Canary 8-bit (8 GB+) or distil-large-v3 INT8 sequential (≤4 GB — this machine)
- LLM: `qwen2.5-3b-instruct.Q4_K_M.gguf` via llama-cpp-python, `n_gpu_layers=-1`

---

## Commands

| Action | Command |
|--------|---------|
| **Dev (full app — preferred)** | `npm run dev:clean` |
| Dev (raw, no pre-flight) | `cargo tauri dev` |
| Kill ghost procs only | `npm run kill-ghosts` |
| Sidecar only | `cd sidecar && .\.venv\Scripts\activate && python main.py` |
| Rust type check | `cargo check --manifest-path src-tauri/Cargo.toml` |
| TS type check | `npx tsc --noEmit` |
| Build sidecar exe | `cd sidecar && pyinstaller sidecar.spec` |
| Bundle Tauri app | `cargo tauri build` |
| Health check | `curl http://127.0.0.1:$(cat ~/.vox/data/port.lock)/health` |

**Always `npm run dev:clean`.** Wrapper (`scripts/run.ps1`): kills stale `vox.exe`, kills `python.exe` pointing at this checkout, kills python holding CUDA ctx (via `nvidia-smi`), deletes stale `port.lock`, waits 1.5 s, runs `cargo tauri dev`. Bypass → ghost VRAM proc holds 3 GB → only PC reboot frees it.

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

Rust only: spawn/monitor sidecar, global hotkeys, UIA context, clipboard inject, window mgmt. React connects directly to sidecar — Rust does NOT proxy audio/transcripts.

### Tauri Events (Rust → Frontend)

| Event | Payload | Trigger |
|---|---|---|
| `hotkey-pressed` / `hotkey-released` | `{}` | Global hotkey |
| `models-ready` | `{}` | Sidecar pre-warm complete |
| `sidecar-degraded` | `{ missing: ["final_pass"\|"llm"] }` | Model load failed |
| `sidecar-restarting` | `{}` | Crash detected |
| `undo-pressed` | `{}` | Ctrl+Shift+Z (Rust already sent Ctrl+Z to target) |
| `continuous-toggle` | `{}` | Ctrl+Shift+C |

### Tauri Commands (Frontend → Rust)

| Command | Purpose |
|---|---|
| `get_focused_context` | UIA text extraction → `DeepContextPayload` |
| `inject_text` | Clipboard swap + Ctrl+V; selection-aware (Delete first if selection) |
| `inject_keypress` | Plays key combo (`ctrl+shift+a`, `enter+enter`) via enigo; voice cmds |
| `begin_stream_inject` / `inject_text_chunk` / `end_stream_inject` | SSE streaming inject path; chunk uses `enigo.text()` (no clipboard) |
| `undo_last_inject` | Send Ctrl+Z to last-inject target |
| `open_settings_window` / `open_finetune_window` / `open_history_window` | Secondary windows |

### WebSocket: `ws://127.0.0.1:<port>/ws/dictation`

**Client → Server:** `{"command": "begin_stream" | "terminate_stream" | "cancel_stream" | "set_device"}`

**Server → Client message types:**
`partial_update` (turbo text), `handoff_ready` (final-pass output in `canary_transcript` field — fixed wire name regardless of model), `stream_started`, `stream_stopped`, `processing_status` (stage: trim|load|decode|clean), `audio_level` (level + 11 bands), `low_confidence` (avg_logprob < -1.0), `device_changed`, `error`.

**Flow:** begin_stream → partial_update stream → terminate_stream → sidecar runs final pass → handoff_ready → frontend POSTs `canary_transcript` to `/process-text` → inject. cancel_stream skips final pass + LLM. Auto-stop: sidecar emits stream_stopped + handoff_ready on its own (SilenceDetector); frontend ignores hotkey-released while armed.

### HTTP Endpoints (read sidecar/routers/ for exact schemas)

- `POST /process-text` — LLM cleanup. Body has `raw_transcript`, `context_string`, `executable_name`, `window_title`, `inferred_extension`, `text_preceding_cursor`, `text_succeeding_cursor`, `use_local_llm`, `custom_vocabulary`, `skip_llm_if_simple`, `app_file_path`. Returns `{cleaned_text, skipped_llm, command_key?}`. On failure returns `cleaned_text=raw_transcript` (never fails — always inject something).
- `POST /process-text/stream` — same input, SSE stream. `event: delta` per LLM chunk, `event: done` at end.
- `POST /settings/dictation` — `{auto_stop_enabled, auto_stop_threshold_ms (500-5000), auto_learn_enabled}`. Read once at capture-loop start.
- `POST /settings/language` — `{language: "en"|"hi"|"es"|...|"auto"}`. Whitelist in `audio/pipeline.SUPPORTED_LANGUAGES`. Non-English → prompt_router appends "preserve script, never translate".
- `GET /history?q&limit` — passive log. Gated by `history_logging_enabled` + `retention_days != 0`.
- `GET|POST|DELETE /voice-commands` — trigger → key-combo map. Built-ins: scratch that, delete that, undo that, select all, copy/paste that, new line, new paragraph, tab, backspace, escape.
- `GET /health` — `{status, models: {turbo, final_pass, llm}, final_pass_type: "canary_cuda"|"canary_cpu"|"distil_sequential"|"skip", cuda, vram_free_mb}`.
- `GET /audio/devices` — list.
- `POST /vocabulary` — `{words: [...]}` → faster-whisper `initial_prompt` next session.
- `POST /finetune/start` / `GET /finetune/status` / `POST /finetune/toggle-collection` / `POST /finetune/groq-test`.

### `DeepContextPayload` (Rust → JSON)

```rust
pub struct DeepContextPayload {
    pub executable_name: String,
    pub window_title: String,
    pub inferred_extension: Option<String>,
    pub text_preceding_cursor: String,  // max 300 chars
    pub text_succeeding_cursor: String, // max 100 chars
    pub app_file_path: Option<String>,  // IDE title parser
    pub app_url: Option<String>,        // TODO — UIA VARIANT in windows-0.52
}
```

---

## Absolute Constraints

### Platform
- Windows only (`x86_64-pc-windows-msvc`). Never `xdotool`, `libinput`, `macos-`, `objc2`, CoreML.
- Tauri uses Edge WebView2 (no bundled Chromium).

### CUDA
- Min: CUDA Toolkit 12.1 + cuDNN 8.9. Verify: `python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"`.
- CUDA OOM is recoverable — degrade, never crash.

### Audio
- All Whisper input: **16kHz mono float32**. Resample via `scipy.signal.resample_poly`. Apply noisereduce before resample if low SNR.
- Streaming: **1000ms chunks, 200ms overlap**. `word_timestamps=True`, filter `word.start > last_valid_timestamp`. Never feed isolated non-overlapping 1s chunks.
- LLM runs **only on final-pass output**, never on turbo partials.
- Hallucination gate: RMS < 0.01 for full chunk → skip Whisper.
- Max session: 60 s (960k samples @ 16 kHz) → auto-terminate.
- `beam_size` is faster-whisper API; HF `.generate()` uses `num_beams` — never mix.

### VRAM Budget

| Model | VRAM |
|-------|------|
| Turbo INT8 | ~1.2 GB |
| Canary 8-bit | ~3.2 GB (8 GB+ only) |
| distil-large-v3 INT8 | ~1.5 GB (4 GB GPU alt) |
| LLM Q4_K_M | ~2.0 GB |

- 8 GB+: all three simultaneously.
- 6 GB: Canary CPU-offload during LLM; sequential.
- **4 GB (this machine):** distil replaces Canary; sequential (Turbo unload → distil → Turbo reload). LLM stays loaded throughout — never unload during dictation.

### Latency Target

| GPU | Final pass | E2E after release |
|-----|-----------|-------------------|
| RTX 3060 (8 GB) | Canary CUDA ~120 ms | **<300 ms** |
| **GTX 1650 (4 GB)** | distil_sequential ~3–5 s | **~4–6 s** |
| Any | Final pass skipped | ~80 ms (LLM only) |

<300 ms physically impossible on 4 GB with final pass. UX shows multi-stage spinner.

- Pre-warm Turbo zero tensor at startup. distil NOT pre-warmed (sequential).
- LLM pre-warm with single "ping".

### LLM Residency
- llama-cpp-python in-process, `n_gpu_layers=-1`.
- Never Ollama at runtime (cold-start kills latency). OK as offline CPU fallback toggle.
- Groq cloud OK as explicit opt-in; never default.

### Text Injection
- Primary: clipboard swap + Ctrl+V (`enigo` + `arboard`).
- Secondary: UIA `SetValue` where TextPattern2 available.
- Never sequential character typing for long text.
- Restore original clipboard 150 ms after paste.

### PyInstaller
- **`--onedir`** never `--onefile`. Output → `dist/sidecar/sidecar.exe` → copy to `src-tauri/binaries/sidecar-x86_64-pc-windows-msvc.exe`.
- Bundled assets via `sys._MEIPASS`. Windows Defender flags PyInstaller — document exclusion.

### Fine-Tuning

Train target: distil-large-v3 (756M), NOT Canary (2.5B).

| Config | 12 GB | **4 GB (this machine)** |
|--------|-------|------------------------|
| LoRA r / alpha | 32 / 64 | **8 / 16** |
| batch / grad_accum | 4 / 4 | **1 / 8** (eff 8) |
| LLM during training | loaded | **unloaded** (free 2 GB) |
| FP16 + grad checkpointing | ✓ | ✓ |

On 4 GB: unload LLM (`llm=None; gc.collect(); torch.cuda.empty_cache()`), reload after. `_training_active=True` blocks dictation. Training subprocess (HTTP trigger, not sidecar re-spawn). Convert via `ct2-transformers-converter --quantization int8`. Passive data: explicit consent only.

---

## Windows-Specific Gotchas

| Issue | Mitigation |
|-------|------------|
| cuDNN DLLs in separate path | `cudnn64_8.dll` at `C:\Program Files\NVIDIA\CUDNN\v8.x\bin\`, NOT in CUDA `bin\` |
| `Ctrl+Shift+Space` ↔ CJK IME conflict | Offer `Ctrl+Alt+Space` alternative in Settings |
| enigo can't inject into UAC-elevated procs | Detect mismatch via `GetTokenInformation`; warn user |
| PyAudio needs VC++ Redist | Bundle `vcredist_x64.exe` or NSIS check |
| SmartScreen blocks unsigned PyInstaller exe | Doc: right-click → Unblock, or sign |
| Win11 Clipboard History (`Win+V`) | arboard adds every inject to history — known, document |
| WebView2 absent on old Win10 | NSIS check + bootstrap installer |
| Defender quarantines PyInstaller output | Add `src-tauri/binaries/` to exclusions |
| Mic privacy denied | Check `pyaudio.PyAudio().get_device_info_by_index()` at startup; emit error if 0 mics |
| llama-cpp-python CUDA wheel (Turing sm_75) | `pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121` |
| distil_sequential VRAM swap gap | LLM (2 GB) stays loaded throughout — only Turbo↔distil swap |

---

## AI Workflow Rules

- **Type check after every change.** Rust: `cargo check`. TS: `tsc --noEmit`. Not done if errors.
- **Minimal changes only.** No unrelated refactoring, no "while I'm here" improvements.
- **Two approaches? Explain both, ask.** Never silently pick.
- **Never mix domains in one prompt.** Rust ≠ Python session.
- **Pin all versions.** Never `cargo add` without explicit version.
- **No `.unwrap()` in production Rust.** Propagate `Result<T, String>`.
- **Use `/plan`** before complex Rust FFI or PyInstaller spec changes.
- **Warn if any appear in Rust:** `xdotool`, `macos-`, `libxdo`, `CoreML`, `objc`.
- **IPC fixed:** WebSocket streaming, HTTP final. No stdio pipes.
- **`canary_transcript` from `handoff_ready` is the input to `/process-text`** — fixed wire name; content = final pass output (distil here) or Turbo fallback. Never raw Turbo partials.
- **Separate commit per logical change.**

---

## Audio Capture Architecture (DO NOT REVERT)

**Persistent mic + 3 s pre-roll.** Mic opened once at sidecar startup (`main.py::start_persistent_mic`), stays open process lifetime. Background daemon (`PersistentMicCapture._run` in `audio/capture.py`) reads continuously, maintains 3 s circular pre-roll deque + fan-out listener list.

Session start (`DictationSession.start_session`): snapshot pre-roll into session ring (captures audio from BEFORE hotkey + during WASAPI warmup). Listener bridges chunks from bg thread → session `asyncio.Queue` via `loop.call_soon_threadsafe`.

Session end: detach listener, mic stays open. No per-session reopen.

**Why:** sounddevice WASAPI Shared on Intel SST returns zeros first ~1–2 s after stream-open. Per-session open/close (old design) silently dropped first 1–2 s of speech. Verified: 9-word pangram captured fully where old design returned 4.

**Constraints:**
- Mic is shared state (`state._mic_ref[0]`). Never re-create per session.
- Hot-swap via WS `{"command": "set_device", "device_index": N}` while idle. `PersistentMicCapture.swap_device` reopens AudioCapture without restarting reader thread. Refuses during active session.
- Listener callbacks run on bg thread — always bridge via `call_soon_threadsafe`. Direct asyncio.Queue.put_nowait from wrong thread deadlocks.
- `read_chunk()` returns `None` on timeout (not the chunk). Re-check `_active`.
- `_dump_session_debug` writes `~/.vox/data/debug/last_raw.wav` every session — leave in place.

---

## Real-Time Metering & Capsule Waveform

**11-band FFT (log-spaced 80 Hz – 7.8 kHz, log-compressed), 30 Hz idle meter.** `PersistentMicCapture._run` computes `_compute_band_amplitudes` every chunk. Exposed via `current_metering() -> (level, bands)`.

`_idle_meter_loop` runs for WS-connection lifetime (NOT just sessions), emits `audio_level` @ 30 Hz with bands. **Single source of truth** — capture loop must NOT emit `audio_level`.

`StreamingOverlay::useWaveformAnimation` drives bars from bands with per-bar fast-attack / slow-decay. Falls back to compound-sine if bands absent.

**Constraints:**
- Bars + partial text RENDER TOGETHER during streaming (bars left, text right with `direction: rtl` + `<bdo dir="ltr">`). Never swap.
- Bar smoothing: attack 0.85 / decay 0.72 / gamma 0.5. Compact pill (Variant #10) caps `MAX_BAR_H=17` for a denser look — earlier variants used 34 px. Keep ±8 % wobble.
- Frozen states (`finalizing`/`injecting`/`previewing`/`error`/`degraded`) ease bars to a uniform low baseline (`FROZEN_BASELINE`) over ~250 ms — bars never freeze mid-wave. The inline spinner does the work of conveying "still processing".

---

## Latency Optimization (post-release)

Pipeline: drain → VAD-trim → distil load → distil decode → LLM clean → inject. Distil ~70 % of time.

- **VAD-trim before distil** (`audio/vad.vad_trim_audio`, Silero `get_speech_timestamps`, 120 ms padding). Cuts distil decode 30–40 %.
- **Peak-normalize + pre-emphasis** before distil (`dual_loader.py`). PEAK not RMS (silences can't corrupt). Pre-emphasis alpha=0.97.
- **Multi-stage `processing_status`** events (trim/load/decode/clean) replace generic spinner.
- **Drain:** silence target `0.40 * SAMPLE_RATE`, hard cap 0.8 s.
- **LLM `max_tokens=256`** (all 4 entry points — run_llm/run_groq/run_llm_stream/run_groq_stream).
- **Confidence gating.** `run_distil_final_pass` returns `(text, avg_logprob)`. `low_confidence` WS event if < -1.0.
- **Async WAV dump.** `_dump_session_debug_async` snapshots audio on caller thread, writes in bg task.

E2E post-release: ~4–6 s (was ~7–10 s). Distil decode is floor.

---

## Failed Experiments — Do Not Repeat

- **AGC on full-session audio before distil.** Long silences → session RMS ~0.005 → 13–40× gain → speech peaks clipped → gibberish. **Replaced by peak-normalize (uses PEAK) + pre-emphasis.** AGC only OK on per-chunk Turbo (post-Silero gate).
- **`beam_size=1` in distil.** Phonetic substitutions ("fox"→"fog"/"bronze"). Use `beam_size=3` — saves ~30 % decode for ~0.3 pt WER vs the original `beam_size=5`. Never drop below 3.
- **LLM kv-cache prewarm on hotkey-pressed** (`/llm/prewarm` + `prewarm_kv_cache`). llama-cpp-python is NOT thread-safe; concurrent prewarm + `/process-text` on the same `Llama` instance caused hangs and slowdowns. Only safe behind a proper `asyncio.Lock` that wraps every invocation (prewarm + run + stream). Endpoint and helper removed.
- **Synchronous clipboard restore in `inject_sync`.** Tauri command blocked an extra ~150 ms waiting for paste-consume + clipboard restore. Now offloaded to `std::thread::spawn` with a fresh `Clipboard::new` (arboard is `!Send` on Windows). Capsule disappears immediately after Ctrl+V dispatch.
- **Re-reading `vox_settings.continuousMode` on every `hotkey-pressed`.** Ctrl+Shift+C historically force-wrote `continuousMode=true` and never cleared it — every later push-to-talk session looped back to recording after inject. Continuous mode is now session-only, armed solely via the Ctrl+Shift+C listener.
- **Bar `MAX_BAR_H=34` in capsule.** Variant #10 compact pill caps at 17 px for a denser look. 34 was used on the older rounded capsule — do not restore without redesigning the surrounding pill geometry.
- **`condition_on_previous_text=True` in distil.** Amplifies intra-utterance errors. Stay `False`.
- **`CHUNK_MS=500` in `audio/pipeline.py`.** Halves Turbo context — hurts streaming accuracy. Spec is 1000 ms.
- **`prefer_pyaudio=True`.** Realtek jack empty → OPEN_FAIL; PyAudio SST peak 0.0001; MME junk peak 1.0. Keep `prefer_pyaudio: false`. sounddevice WASAPI Shared is least-bad here.
- **WASAPI Exclusive (`exclusive_mode: true`).** `PaErrorCode -9984` on Intel SST → silent fallback to Shared. Doesn't bypass enhancements on SST.
- **Mic indexes 4 / 10 / 11 / 18-20.** All OPEN_FAIL (jack empty or routed via SST aggregator). Only viable: SST variants (0, 1, 9) — all gated by same DSP.
- **Per-session AudioCapture (open on hotkey, close on terminate).** First 1–2 s dropped on Intel SST. **Replaced by persistent mic + pre-roll** (see Audio Capture Architecture).
- **Mutually exclusive bars-OR-text capsule** (`isListening ? bars : text`). Felt like mic stopped. Render both — bars left, text right rtl.
- **Per-chunk `audio_level` from `_capture_loop` alongside 30 Hz idle meter.** Duplicate events. Idle meter is single source.
- **Bar smoothing decay 0.88 / max 28 px / gamma 0.7.** Stiff. Use 0.72 / 34 / 0.5 + ±8 % wobble.
- **No VAD-trim before distil.** Decoded silence at ~1× real-time. VAD now mandatory.
- **Generic "Enhancing accuracy…" spinner.** Opaque. Use multi-stage `processing_status`.
- **Drain max 2.0 s.** Wasted time. Cut to 0.8 s (silence detector terminates earlier anyway).
- **Trusting `kill_previous_sidecar` alone.** Only knows PID in `pid.lock` — crash before write leaves orphan holding CUDA ctx. `npm run dev:clean` is the load-bearing cleanup.
- **Manual `wmic process where ... delete`.** Replaced by `npm run dev:clean`.

Intel SST mic hardware ceiling: peak ~0.10 even with Windows boost 100 %, enhancements off. Only real fixes: external USB mic or 3.5 mm into Realtek jack. Software can't recover audio the mic isn't producing.

---

## Out of Scope

- `sidecar/data/` — runtime data (vocabulary.json, passive_log.jsonl, settings.json, port.lock).
- `sidecar/models/` — large binary downloads.
- `src-tauri/binaries/` — PyInstaller output; regen via `pyinstaller sidecar.spec`.
- `sidecar/sidecar.spec` — no edits without `/plan` (DLL paths machine-specific).
- `src-tauri/icons/` — placeholder ICOs.
- `sidecar/training/` — LoRA subprocess; never invoke from session code.
- `CLAUDE.local.md` — machine-specific notes.
