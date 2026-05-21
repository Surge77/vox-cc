# Vox

> Windows-native offline speech-to-text dictation. Microphone → Whisper Turbo (live preview) → distil-Whisper (final pass) → local LLM cleanup → text injected into the focused window. No audio, transcript, or cleanup prompt ever leaves the machine.

![status](https://img.shields.io/badge/status-Tier_2.5_shipped-success) ![platform](https://img.shields.io/badge/platform-Windows_10/11_x64-blue) ![license](https://img.shields.io/badge/license-MIT-green)

---

## Status (2026-05-21)

**Daily-driver feature-complete.** All milestones M1–M14 + Tier 1 + Tier 2 + Tier 2.5 polish shipped. See [ROADMAP.md](ROADMAP.md) for the full backlog and [SNAPSHOT.md](SNAPSHOT.md) for the current paused state if you're picking this back up after a break.

Next planned work (Tier 3): GitHub Actions CI, test coverage to 60 %, then distribution polish (code signing, auto-update, NSIS WebView2 bootstrap).

---

## What it does

Press `Ctrl+Shift+Space`, speak, release — your words appear in whatever app has focus. Or toggle `Ctrl+Shift+C` for continuous mode (auto-stop on silence, auto-restart after inject).

### Features (shipped)

| Tier | Feature |
|------|---------|
| Core | Streaming Whisper Turbo preview (1000 ms chunks, 200 ms overlap) |
| Core | distil-Whisper final pass (sequential swap on ≤4 GB GPUs) |
| Core | Local LLM cleanup (Qwen 2.5 3B Q4_K_M via llama-cpp-python) |
| Core | Clipboard-swap injection with original-clipboard restore |
| Core | Context-aware prompts (code editor / email / chat / docs / terminal) |
| Tier 1 | Skip-LLM for trivial transcripts, auto-stop, undo + re-record, streaming inject |
| Tier 2 | Selection-aware inject (deletes existing selection before paste) |
| Tier 2 | Multi-language (English + Hindi/Spanish/etc., script preservation) |
| Tier 2 | Custom vocabulary with auto-learn from corrections |
| Tier 2 | Transcript preview before inject (Esc to cancel) |
| Tier 2 | Voice commands ("scratch that", "new line", custom triggers) |
| Tier 2 | History window (transcript log with search) |
| Tier 2 | Continuous mode (`Ctrl+Shift+C`) |
| Tier 2 | IDE file-path detection (VS Code / Cursor / JetBrains titles) |
| Tier 2.5 | Squared compact capsule with 11-band FFT waveform |
| Tier 2.5 | Async clipboard restore — capsule disappears immediately on inject |
| Tier 2.5 | Short-audio bypass — single words skip distil, ship Turbo text |

### Latency (GTX 1650 4 GB / Intel SST mic)

| Scenario | End-to-end |
|----------|-----------|
| Short utterance (<1.5 s) — bypass path | ~1 s |
| Standard 3-5 word phrase | ~4-6 s |
| LLM-skipped (trivial transcript) | ~3-4 s |

On 8 GB+ GPUs with Canary final-pass, sub-300 ms is achievable. See [CLAUDE.md](CLAUDE.md) for VRAM budget and load plans.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + TypeScript, Tauri v2 (Edge WebView2) |
| Backend | Rust (`x86_64-pc-windows-msvc`) |
| ML Sidecar | Python 3.11, FastAPI + uvicorn |
| Streaming ASR | `openai/whisper-large-v3-turbo` via faster-whisper (CTranslate2 INT8) |
| Final-pass ASR | `Systran/faster-distil-whisper-large-v3` (4 GB GPU) · `nvidia/canary-qwen-2.5b` (8 GB+ GPU) |
| LLM | `qwen2.5-3b-instruct.Q4_K_M.gguf` via llama-cpp-python (CUDA build) |
| Audio capture | `sounddevice` (WASAPI Shared) persistent mic + 3 s pre-roll |
| VAD | Silero VAD |
| Text injection | `enigo` + `arboard` (clipboard swap + Ctrl+V) |

---

## Requirements

| Dependency | Version |
|---|---|
| Windows | 10 / 11 x64 |
| Python | 3.11.x (NOT 3.12 — bitsandbytes wheel availability) |
| Rust toolchain | `stable-x86_64-pc-windows-msvc` |
| Node.js | v18+ |
| CUDA Toolkit | 12.1 |
| cuDNN | 8.9 (separate install path from CUDA Toolkit) |
| NVIDIA Driver | 591+ |
| GPU | GTX 1650 4 GB minimum (RTX 30xx 8 GB+ unlocks Canary path) |
| Disk | ~20 GB free for models + dependencies during dev |

> **Why 4 GB is the floor:** distil-Whisper INT8 (~1.5 GB) + LLM Q4 (~2 GB) live in VRAM together; Turbo (~1.2 GB) hot-swaps with distil at final-pass time. See [CLAUDE.md](CLAUDE.md) → "VRAM Budget".

---

## Dev setup (from a fresh clone)

```bash
git clone git@github.com:Surge77/vox-cc.git
cd vox-cc
npm install
```

### Sidecar

```powershell
cd sidecar
python -m venv .venv
.\.venv\Scripts\activate

pip install -r requirements.txt

# CUDA-enabled torch (NOT the default CPU wheel)
pip install torch==2.4.1+cu121 --index-url https://download.pytorch.org/whl/cu121

# CUDA build of llama-cpp-python for Turing sm_75 (GTX 1650). Replace cu121
# with the wheel matching your CUDA install if different.
pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121
```

### Models (~8.5 GB total, download once)

| Model | Source | Path |
|-------|--------|------|
| Whisper Turbo CT2 | `openai/whisper-large-v3-turbo` via faster-whisper | `sidecar/models/whisper-large-v3-turbo-ct2/` |
| distil-Whisper CT2 | `Systran/faster-distil-whisper-large-v3` | `sidecar/models/models--Systran--faster-distil-whisper-large-v3/` |
| Qwen 2.5 3B Q4_K_M | `Qwen/Qwen2.5-3B-Instruct-GGUF` on HuggingFace | `sidecar/models/qwen2.5-3b-instruct-q4_k_m.gguf` |

Turbo + distil auto-download via `faster_whisper.WhisperModel` `download_root`. Qwen GGUF is a manual `huggingface-cli download` or browser download.

### Verify CUDA + Rust

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
# Should print: True 12.1

rustup target add x86_64-pc-windows-msvc
```

---

## Running

| Task | Command |
|---|---|
| **Full app (always use this)** | `npm run dev:clean` |
| Kill ghost VRAM procs only | `npm run kill-ghosts` |
| Sidecar standalone (debug) | `cd sidecar && .\.venv\Scripts\activate && python main.py` |
| TS type check | `npx tsc --noEmit` |
| Rust type check | `cargo check --manifest-path src-tauri/Cargo.toml` |
| Health (port from `~/.vox/data/port.lock`) | `curl http://127.0.0.1:$PORT/health` |

> **Why `npm run dev:clean`:** raw `cargo tauri dev` can leave orphan Python processes holding the CUDA context. The wrapper kills stale `vox.exe` + `python.exe` pointing at this checkout + any python holding CUDA context (via `nvidia-smi`), deletes `port.lock`, then runs `cargo tauri dev`. Skipping it can require a reboot to free VRAM.

---

## Building installers

See [DISTRIBUTION.md](DISTRIBUTION.md) for the full release packaging steps. Short version:

```powershell
cd sidecar; .\.venv\Scripts\activate
pyinstaller sidecar.spec
copy dist\sidecar\sidecar.exe ..\src-tauri\binaries\sidecar-x86_64-pc-windows-msvc.exe

cd ..
cargo tauri build
# Produces: src-tauri\target\release\bundle\nsis\Vox_<ver>_x64-setup.exe
# (sidecar _internal/ ships as a separate ~12 GB zip — exceeds NSIS 2 GB cap)
```

PyInstaller uses **`--onedir`** never `--onefile` — see [CLAUDE.md](CLAUDE.md) constraints section.

---

## Project Structure

```
vox/
├── src/                      React frontend
│   ├── App.tsx               State machine + Tauri/WS bridge
│   ├── StreamingOverlay.tsx  Capsule UI + 11-band FFT waveform
│   ├── Settings.tsx          Hotkey / mic / language / LLM toggles
│   ├── History.tsx           Transcript log window
│   └── FineTuningDashboard.tsx
│
├── src-tauri/                Rust backend
│   └── src/
│       ├── main.rs           Tauri builder, hotkey registration, COM init
│       └── commands/         inject, context (UIA), window mgmt
│
├── sidecar/                  Python ML sidecar
│   ├── main.py               FastAPI app + lifespan + persistent mic
│   ├── audio/                capture (WASAPI), pipeline, VAD
│   ├── models/               dual_loader (Turbo↔distil), llm_engine, prompt_router
│   ├── routers/              dictation (WS), text_processing, finetuning, health
│   └── prompts/              per-profile system prompts
│
├── CLAUDE.md                 Architecture, IPC schema, failed experiments
├── ROADMAP.md                Tiered backlog (Tier 1/2/2.5 shipped, Tier 3+ pending)
├── SNAPSHOT.md               Current paused state — read first on return
├── DISTRIBUTION.md           Release packaging + installer steps
├── CONTRIBUTING.md           PR rules + dev workflow
└── SECURITY.md               Security policy
```

---

## How it works (one-pass)

1. `Ctrl+Shift+Space` (Rust global hotkey) → fires `hotkey-pressed` Tauri event → React opens WS `begin_stream`
2. Sidecar's persistent mic (open since startup) snapshots its 3-second pre-roll into a session ring buffer, then bridges new chunks via `asyncio.Queue`
3. Turbo transcribes 1000 ms chunks with 200 ms overlap → `partial_update` WS messages stream live text to the capsule
4. Release (or auto-stop on silence) → `terminate_stream` → sidecar unloads Turbo, loads distil, runs VAD-trim + peak-normalize + pre-emphasis, decodes (`beam_size=3`), reloads Turbo for next session
5. Sidecar emits `handoff_ready` with the final transcript in the `canary_transcript` field (wire name fixed)
6. Frontend POSTs to `/process-text` → LLM cleans using context from the focused window (UIA-extracted)
7. Rust injects via clipboard swap + `Ctrl+V`; original clipboard restored on a background thread 150 ms later

---

## Windows gotchas

| Issue | Mitigation |
|-------|-----------|
| Windows Defender flags PyInstaller output | Add `src-tauri/binaries/` to Defender exclusions before first build |
| SmartScreen blocks unsigned installer | Right-click → Properties → Unblock, or sign (Tier 3 #15) |
| `Ctrl+Shift+Space` ↔ CJK IME conflict | Settings → bind `Ctrl+Alt+Space` instead |
| Edge WebView2 missing on old Win10 | NSIS bootstrapper (Tier 3 #18) — for now, install manually |
| Mic privacy blocked | Settings → Privacy → Microphone — must be enabled for the app |
| Ghost Python proc holds CUDA after crash | Always use `npm run dev:clean`. If still stuck, reboot. |

Intel SST microphones (most laptops) have a hardware ceiling around peak 0.10 even with Windows boost at 100 %. For meaningful accuracy gains beyond Vox's defaults, use an external USB mic.

---

## Documentation map

| Doc | Read when |
|-----|-----------|
| **CLAUDE.md** | First. Full architecture, IPC schema, constraints, failed experiments. |
| **sidecar/CLAUDE.md** | Editing the Python sidecar. Audio capture, model loading, fine-tuning, PyInstaller rules. |
| **ROADMAP.md** | Picking the next feature. Tier 1/2/2.5 shipped; Tier 3+ open. |
| **SNAPSHOT.md** | Returning to the project after a break. Resume steps + known-paused state. |
| **DISTRIBUTION.md** | Cutting a release. Build sidecar, bundle Tauri, ship installer + sidecar zip. |
| **CONTRIBUTING.md** | Opening a PR. One domain per PR, conventional commits, type-gates green. |
| **SECURITY.md** | Reporting a vulnerability. |

---

## License

MIT — see [LICENSE](LICENSE).
