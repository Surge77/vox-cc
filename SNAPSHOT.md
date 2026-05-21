# Vox — Project Snapshot (2026-05-21)

Read this first if you're returning to the project after a break. Captures the state at hiatus + how to resume + what NOT to redo.

---

## Status

- **Last commit on `main`:** `7436ef2` — `feat(polish): Tier 2.5 — capsule redesign, async inject, latency cuts`
- **Working tree clean.** `origin/main` synced.
- **Shipped:** M1–M14 (foundations) + Tier 1 (4 features) + Tier 2 (8 features) + Tier 2.5 polish (capsule, async inject, beam=3, short-audio bypass, continuous-mode fix, held-key release, scrollable secondary windows). Full list in [ROADMAP.md](ROADMAP.md).
- **Tag:** `v0.1.0-pre-hiatus` marks this snapshot — `git checkout v0.1.0-pre-hiatus` to return to exactly this state.

## What works today

- Push-to-talk on `Ctrl+Shift+Space`, continuous mode toggle on `Ctrl+Shift+C`
- Live streaming Turbo preview + distil-Whisper final pass (sequential swap on 4 GB GPU)
- Local Qwen 2.5 3B LLM cleanup with per-app context (code editor / email / chat / docs / terminal profiles)
- Clipboard-swap inject with async restore — capsule disappears the instant Ctrl+V dispatches
- 11-band FFT waveform in compact pill capsule, frozen-baseline animation on `finalizing`/`injecting`/`preview`
- Custom vocabulary auto-learn, voice commands, transcript preview (Esc to cancel), history window

## Hardware reality (don't waste time fighting these)

- **Intel SST mic ceiling.** Peak ~0.10 even at 100 % Windows boost. Software cannot recover audio the mic does not produce. For accuracy gains: buy an external USB mic.
- **GTX 1650 4 GB distil decode ~3-5 s.** Floor for the final-pass model size. Sub-second only possible on 8 GB+ with Canary path.
- **8.5 GB of models on disk.** Auto-downloaded on first run (Turbo + distil) or manual (Qwen GGUF).
- Read CLAUDE.md → "Failed Experiments — Do Not Repeat" before touching audio capture, AGC, beam size, or chunk sizing.

## Resume in 7 steps

```powershell
# 1. Pull latest
git pull origin main

# 2. Recreate venv (.venv is gitignored)
cd sidecar
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install Python deps
pip install -r requirements.txt
pip install torch==2.4.1+cu121 --index-url https://download.pytorch.org/whl/cu121
pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121

# 4. Verify CUDA wired
python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
# Expected: True 12.1

# 5. Restore Node deps + Tauri target (gitignored)
cd ..
npm install
rustup target add x86_64-pc-windows-msvc

# 6. Download models — see README.md "Models" section.
#    Turbo + distil pull automatically on first sidecar startup via
#    faster-whisper download_root. Qwen GGUF is manual:
#    huggingface-cli download Qwen/Qwen2.5-3B-Instruct-GGUF \
#      qwen2.5-3b-instruct-q4_k_m.gguf --local-dir sidecar/models/

# 7. Run
npm run dev:clean
```

First boot takes ~30-60 s — model files download + CUDA prewarm.

## Next planned work (when you return)

### Polish (do first — finishes Tier 2.5 line)

1. **External-mic onboarding nudge.** Detect SST device name → toast user once: "Plug in a USB mic for 2× accuracy."
2. **Multi-language live verification.** Tier 2 #5 shipped untested. Pick Hindi or Spanish, dictate, confirm Devanagari/Latin survives LLM cleanup. Fix `models/prompt_router.py` if not.
3. **Test coverage for new paths.** Selection-aware inject, voice-command matcher, vocab auto-learn — all new in Tier 2, all untested. Bump pytest 142 → 175+.
4. **Continuous-mode distinct capsule color.** Already noted as deferred in [ROADMAP.md](ROADMAP.md). Small.

### Tier 3 Wave A (regression infra — before any external user)

5. **GitHub Actions CI** (#13, 4 h) — `cargo check` + `tsc --noEmit` + `pytest sidecar/` on every push. Tagged builds produce installer + sidecar zip.
6. **Test coverage to 60 %** (#14, 8 h) — `audio/pipeline.py`, `audio/vad.py`, `models/dual_loader.py`, capsule snapshots.

### Tier 3 Wave B (distribution — only before first beta tester)

7. NSIS polish (#18) → code signing cert (#15, $200/yr) → auto-update (#16) → opt-in telemetry (#17).

Order: 1 → 2 → 3 → 5 → 6 → (week of daily-driver usage, zero crashes) → 7.

## Known unfinished / deferred

- **Browser URL extraction** in `get_focused_context`. `windows-0.52` `IUIAutomation` `VARIANT` shape blocked it. Field exists in `DeepContextPayload` (`app_url: Option<String>`) but stays `None`.
- **LLM kv-cache prewarm.** Tried on `hotkey-pressed` → llama-cpp-python is not thread-safe; concurrent prewarm + `/process-text` on the same `Llama` instance caused hangs. Backend endpoint + helper removed. Only revisit with a proper `asyncio.Lock` wrapping every llama-cpp invocation including the prewarm path.
- **Continuous mode** force-writes `autoStopEnabled=true`. The held-key-release fast-terminate in `App.tsx` compensates, but the underlying coupling is ugly. Decouple eventually.
- **Settings drift.** Some sidecar settings (`audio_device_index`, `exclusive_mode`, `prefer_pyaudio`) are read once at sidecar startup. Changing them requires a sidecar restart. Document or fix.

## Things to skim on return

1. `git log --oneline -10` — last 10 commits
2. [CLAUDE.md](CLAUDE.md) — architecture + IPC schema + failed experiments (~330 lines, all load-bearing)
3. [sidecar/CLAUDE.md](sidecar/CLAUDE.md) — Python sidecar specifics (~860 lines, audio + models + PyInstaller)
4. [ROADMAP.md](ROADMAP.md) → "Tier 2.5" + "Tier 3" sections
5. This file (you're already here)

## Local-only files (NOT in repo, need re-creation)

| File / dir | How to restore |
|------------|---------------|
| `sidecar/.venv/` | `python -m venv .venv` + pip install steps above |
| `sidecar/models/` (8.5 GB) | First sidecar run + manual Qwen download |
| `node_modules/` | `npm install` |
| `src-tauri/target/` | `cargo build` (first build ~5-10 min cold) |
| `src-tauri/binaries/sidecar-*.exe` | `pyinstaller sidecar.spec` then copy (DISTRIBUTION.md) |
| `~/.vox/data/` (vocab, history, port.lock) | Auto-created on first sidecar start; empty until used |
| `CLAUDE.local.md` | Machine-specific. Not needed elsewhere. |

## Cleanup before hiatus (optional, recovers ~37 GB)

```powershell
cargo clean --manifest-path src-tauri/Cargo.toml   # frees ~37 GB Rust build cache
# Recreate on resume by running `npm run dev:clean` — first build ~5 min cold.
```

`sidecar/.venv/` (~7 GB) and `sidecar/models/` (~8.5 GB) are safe to delete too, but you'll re-download ~12 GB on resume. Trade disk for time.
