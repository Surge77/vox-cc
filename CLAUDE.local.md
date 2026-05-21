# Vox — Local Machine Config (not committed)

## Hardware

| Component | Value |
|-----------|-------|
| GPU | GTX 1650 4 GB — distil_sequential always active |
| VRAM free (clean start) | ~3294 MB |
| Python venv | `sidecar/.venv` |

## Local Paths

| Asset | Path |
|-------|------|
| Turbo CT2 model | `sidecar/models/whisper-large-v3-turbo-ct2/` |
| distil-large-v3 (CT2) | `sidecar/models/models--Systran--faster-distil-whisper-large-v3/` |
| LLM GGUF | `sidecar/models/qwen2.5-3b-instruct-q4_k_m.gguf` (2007 MB) |
| Runtime data dir | `C:\Users\tdmne\.vox\data\` |
| Port lock | `C:\Users\tdmne\.vox\data\port.lock` |

## Git Remote

`https://github.com/Surge77/vox-cc.git` (main)

## Milestone Status

All 14 milestones complete + committed. Post-M14 shipped: customizable hotkey, snippets, replay router, writing styles + AGC/NR toggles, latency tracking, NSIS installer w/ WebView2 bootstrapper, onboarding toast, Windows mic auto-boost, WAV-dump diagnostic, `npm run dev:clean`, persistent mic + 3s pre-roll, Tier 1 (skip-LLM, auto-stop, undo-rerecord, streaming inject), Tier 2 (selection-aware inject, multi-language, vocab auto-learn, transcript preview, IDE file path, voice commands, history view, continuous mode).

**Venv note:** ctranslate2 = 4.7.1 (requirements.txt pins 4.4.0 — diverged). Turbo model.bin reconverted 2026-05-19 for 4.7.1 format.

For per-feature detail, see `git log` and the commit hashes below.

## Recent commits (2026-05-20)

| Commit | What |
|---|---|
| `613e0b3` | Tier 2 — selection-aware inject, multi-lang, vocab auto-learn, preview, IDE context, voice cmds, history, continuous mode |
| `a6d91b3` | Tier 1 — skip-LLM, auto-stop, undo+rerecord, streaming inject, instant-listen capsule, latency cuts |
| `2ba14f5` | Waveform stays visible during streaming + looser bar motion |
| `dae56c5` | FFT spectrum, VAD-trim, multi-stage progress, hot-swap mic, confidence gating, peak-norm, pre-emphasis |
| `9a284ce` | Persistent mic + pre-roll + `npm run dev:clean` |

All pushed to `Surge77/vox-cc` main.

## What works now (as of 2026-05-20)

- **Capture:** persistent mic, 3 s pre-roll, sounddevice WASAPI Shared float32 on Intel SST. Hot-swap via WS `set_device`.
- **Streaming preview:** Turbo 1000 ms / 200 ms overlap / beam=1 / vocab prompt.
- **Final pass:** distil-large-v3 sequential (Turbo unload → distil → reload). Pre-decode: VAD-trim + peak-norm + pre-emphasis. beam=5, `condition_on_previous_text=False`. Emits `low_confidence` if avg_logprob < -1.0.
- **LLM cleanup:** Qwen3B-Q4 via `/process-text`. Skippable per-request. Snippet expansion after.
- **UI:** 11 FFT bands → 11 bars driven independently. Bars + partial text co-render. Multi-stage processing_status during finalizing.
- **Idle meter:** 30 Hz `audio_level`+bands while WS open. Capsule reacts to ambient sound.
- **Process hygiene:** `npm run dev:clean` mandatory.

## Dev Workflow

```powershell
# STANDARD — always
npm run dev:clean

# Triage only
npm run kill-ghosts

# Sidecar standalone (gate testing)
cd sidecar; .\.venv\Scripts\activate; python main.py

# Health
$port = Get-Content $env:USERPROFILE\.vox\data\port.lock
curl http://127.0.0.1:$port/health

# PyInstaller build
cd sidecar; .\.venv\Scripts\activate
pyinstaller sidecar.spec
copy dist\sidecar\sidecar.exe ..\src-tauri\binaries\sidecar-x86_64-pc-windows-msvc.exe
```

## Critical Local Gotchas

- **Always `npm run dev:clean`** — never raw `cargo tauri dev`. Ghost VRAM proc → PC reboot.
- **Intel SST mic is hardware bottleneck.** Peak ~0.10 even at boost 100 %. distil accuracy ceiling is the mic, not the model. Plug external USB for testing. See "Failed Experiments" in CLAUDE.md before "fixing" capture.py.
- **Port lock:** never hardcode 8000. Read `~/.vox/data/port.lock`. If missing after ghost cleanup, default 8000.
- **distil source:** `Systran/faster-distil-whisper-large-v3` (CT2), NOT `distil-whisper/distil-large-v3` (safetensors).
- **COM STA:** `CoInitializeEx(COINIT_APARTMENTTHREADED)` must precede Tauri builder in `main.rs`.
- **UIA on spawn_blocking:** init COM inside the closure — tokio threads don't inherit.
- **Settings `audio_device_index`, `exclusive_mode`, `prefer_pyaudio`** read ONCE at sidecar startup, not per session. Change → restart sidecar.

## WAV-dump debugging

`sidecar/routers/dictation.py::_dump_session_debug` writes after every session:
- `~/.vox/data/debug/last_raw.wav` — raw audio fed to ring buffer
- `~/.vox/data/debug/last_agc.wav` — post-AGC (for compare)

If `last_raw.wav` silent → capture problem (mic/SST/Windows). If raw clean but agc distorted → AGC clipping.

Per-second RMS scan:
```powershell
cd sidecar; .\.venv\Scripts\activate
python -c "import wave,numpy as np; w=wave.open(r'C:\Users\tdmne\.vox\data\debug\last_raw.wav','rb'); d=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)/32768; sr=w.getframerate(); [print(f't={i/sr:.1f}s rms={np.sqrt(np.mean(d[i:i+sr]**2)):.4f}') for i in range(0,len(d),sr)]"
```
