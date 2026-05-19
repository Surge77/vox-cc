# Vox

> Windows-native, fully offline speech-to-text dictation powered by Whisper, distil-Whisper, and a local LLM — zero cloud dependency for the core pipeline.

---

## What it does

Vox listens to your microphone via a global hotkey (`Ctrl+Shift+Space`), streams a live preview through Whisper Turbo, runs a high-accuracy final pass with distil-Whisper (or Canary on 8 GB+ GPUs), cleans the transcript with a local LLM (Qwen 2.5), and injects the result directly into the focused window. No audio, text, or transcript ever leaves the machine.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 18 + TypeScript, Tauri v2 (Edge WebView2) |
| Backend | Rust (`x86_64-pc-windows-msvc`) |
| ML Sidecar | Python 3.11, FastAPI + uvicorn |
| Streaming ASR | `openai/whisper-large-v3-turbo` via faster-whisper (CTranslate2 INT8) |
| Final-pass ASR | `distil-whisper/distil-large-v3` (≤4 GB GPU) · `nvidia/canary-qwen-2.5b` (8 GB+ GPU) |
| LLM post-processing | `qwen2.5-3b-instruct.Q4_K_M.gguf` via llama-cpp-python |

---

## Requirements

| Dependency | Version |
|---|---|
| Windows | 10 / 11 x64 |
| Python | 3.11.x |
| Rust toolchain | `stable-x86_64-pc-windows-msvc` |
| Node.js | v18+ |
| CUDA Toolkit | 12.1+ |
| cuDNN | 8.9+ |
| NVIDIA Driver | 520+ |

> **GPU note:** The core pipeline runs on 4 GB VRAM (GTX 1650 class). 8 GB+ unlocks the Canary final-pass model for lower WER and sub-300ms injection latency.

---

## Setup

### 1. Clone the repo

```bash
git clone git@github.com:Surge77/vox-cc.git
cd vox-cc
```

### 2. Install Node dependencies

```bash
npm install
```

### 3. Set up the Python sidecar

```bash
cd sidecar
python -m venv .venv
.\.venv\Scripts\activate

pip install -r requirements.txt

# GPU build of llama-cpp-python (Turing sm_75 — GTX 1650)
pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121
```

### 4. Install Rust toolchain

```bash
rustup target add x86_64-pc-windows-msvc
```

### 5. Verify CUDA

```bash
python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
```

---

## Running

| Task | Command |
|---|---|
| Full app (dev) | `cargo tauri dev` |
| Sidecar only | `cd sidecar && .\.venv\Scripts\activate && python main.py` |
| Health check | `curl http://127.0.0.1:8000/health` |
| TS type check | `npx tsc --noEmit` |
| Rust type check | `cargo check --manifest-path src-tauri/Cargo.toml` |

---

## Project Structure

```
vox/
├── src/               ← React frontend (App.tsx, Settings, Overlay, etc.)
├── src-tauri/         ← Rust backend (hotkeys, sidecar spawn, UIA injection)
├── sidecar/           ← Python ML sidecar (FastAPI, Whisper, LLM, audio)
│   ├── audio/         ← Capture, VAD (Silero), pipeline
│   ├── models/        ← Dual-loader, LLM engine, prompt router
│   ├── routers/       ← HTTP + WebSocket endpoints
│   └── prompts/       ← Context-aware LLM prompt templates
└── dist/              ← Build output
```

---

## How it works

1. **Hotkey down** (`Ctrl+Shift+Space`) → WebSocket `begin_stream` → sidecar opens mic
2. Turbo processes 1000 ms chunks with 200 ms overlap → `partial_update` messages stream live text to the overlay
3. **Hotkey up** → `terminate_stream` → sidecar flushes the ring buffer and runs the final-pass model
4. Frontend POSTs the final transcript to `/process-text` → LLM cleans it using context from the focused window
5. Rust injects the cleaned text via clipboard swap + `Ctrl+V`; original clipboard is restored after 150 ms

---

## Windows-specific notes

- **Windows Defender** may flag the PyInstaller sidecar binary. Add `src-tauri/binaries/` to Defender exclusions.
- **SmartScreen** may block unsigned builds on first launch. Right-click → Properties → Unblock.
- **Ctrl+Shift+Space** conflicts with Windows IME on CJK language packs. An alternative binding (`Ctrl+Alt+Space`) is available in Settings.
- **Edge WebView2** must be installed (pre-installed on Windows 11; the installer checks and installs it on Windows 10).

---

## License

MIT — see [LICENSE](LICENSE).
