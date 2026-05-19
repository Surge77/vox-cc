# Contributing to Vox

Thanks for your interest in contributing. This document explains how the project is structured for contributors and what to keep in mind before opening a PR.

---

## Ground Rules

- **Windows only.** The entire stack targets `x86_64-pc-windows-msvc`. Do not introduce cross-platform abstractions, Linux crates, or macOS APIs.
- **Minimal, focused changes.** Each PR should do exactly one thing. No drive-by refactors or style sweeps unrelated to the fix.
- **One domain per PR.** Rust changes and Python changes belong in separate PRs. Do not mix frontend and sidecar work.
- **Pin all versions.** Never open a dep with an unpinned range unless discussed first.
- **No `.unwrap()` in production Rust.** Propagate `Result<T, String>` properly.

---

## Development Setup

Follow the [README](README.md) setup section to get the full stack running locally before making any changes.

After setup, verify the baseline works:

```bash
# Sidecar health
curl http://127.0.0.1:8000/health
# Should return: { "status": "ok", ... }

# Rust type check
cargo check --manifest-path src-tauri/Cargo.toml

# TypeScript type check
npx tsc --noEmit
```

---

## Making Changes

### Sidecar (Python)

- Activate the venv before editing: `cd sidecar && .\.venv\Scripts\activate`
- Keep the FastAPI router structure — new endpoints belong in `sidecar/routers/`
- Audio pipeline changes go in `sidecar/audio/`; model loading changes go in `sidecar/models/`
- All audio fed to Whisper must be **16 kHz, mono, float32** — resampling is mandatory
- Do not modify `sidecar/sidecar.spec` without a detailed plan — DLL paths are machine-specific

### Rust (src-tauri)

- Rust's only runtime role is: spawn/monitor sidecar, global hotkeys, UIA context extraction, clipboard injection, window management
- Rust does **not** proxy audio or transcript data
- Never add `xdotool`, `libxdo`, `macos-*`, `CoreML`, or `objc` crates

### Frontend (React / TypeScript)

- State machine lives in `App.tsx` — keep all 8 states and their transitions intact
- New UI panels go in separate `.tsx` files; don't grow `App.tsx` further
- The WebSocket protocol (`begin_stream`, `terminate_stream`, `cancel_stream`) and HTTP routes are fixed wire format — do not rename fields

---

## Commit Style

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(sidecar): add Silero VAD confidence threshold config
fix(rust): handle port.lock read race on sidecar restart
docs: update VRAM budget table for distil-large-v3
```

One logical change per commit. M11 and M12 are different commits. Bug fixes and features are different commits.

---

## Pull Request Checklist

Before opening a PR, confirm:

- [ ] `cargo check` passes with zero errors
- [ ] `npx tsc --noEmit` passes with zero errors
- [ ] Sidecar starts cleanly: `python main.py` → `GET /health` returns `status: ok`
- [ ] No new `.unwrap()` calls in Rust
- [ ] No cross-platform or non-Windows code introduced
- [ ] PR title follows Conventional Commits format
- [ ] PR touches only one domain (Python **or** Rust **or** frontend)

---

## Reporting Issues

Open a GitHub Issue with:

- OS version and NVIDIA driver version
- GPU model and VRAM
- Output of `GET /health`
- Full Python traceback or Rust panic if applicable
- Steps to reproduce

Do **not** include audio recordings or transcripts that contain personal information.
