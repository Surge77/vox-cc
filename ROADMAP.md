# Vox — Roadmap

Where the project goes next. Items are organized by tier (sprint priority),
then by impact × effort. Each item has enough detail to pick up cold and
start implementing.

> **State at 2026-05-20:** Core dictation pipeline is production-quality.
> Persistent mic + 3 s pre-roll captures speech reliably. FFT spectrum
> waveform feels alive. VAD-trim + peak-norm + pre-emphasis cut latency
> ~3 s. Three layers of doc + memory prevent re-introducing past bugs.
> Hardware ceiling on this machine: Intel SST mic peak ~0.10, distil
> decode dominates remaining latency. See `CLAUDE.md` for architecture
> and `CLAUDE.local.md` for what was tried and rejected.

---

## Legend

- **Impact** — perceived value to the user, not just engineering elegance
- **Effort** — estimated hours of focused work on this hardware
- **Risk** — chance the implementation introduces regressions (low / med / high)
- **Depends on** — prerequisite tasks

---

## Tier 1 — Next sprint (recommended)

Ship these first. Together = ~12 hours. Move app from "good" to "delightful."

### 1. Auto-stop on silence

**Impact: huge** — UX: 3h effort, low risk

Push-to-talk feels primitive. Detect ~1.5 s of post-speech silence with
Silero VAD and auto-terminate. Hotkey becomes "tap to start" instead of
"hold to talk."

- Settings toggle: "Auto-stop after silence" + slider for threshold (1-3 s)
- Implementation: in `routers/dictation.py::_capture_loop`, monitor a
  rolling buffer of RMS values from the queue. When `last_silence_ms >
  threshold` AND `session_speech_detected`, call same path as
  `terminate_stream`.
- Keep push-to-talk mode as fallback (user setting)

### 2. Streaming inject (type as LLM generates)

**Impact: massive perceived speed** — 4h, medium risk

Currently text appears all at once after the full pipeline completes.
Stream LLM output character-by-character via clipboard + repeated paste,
OR via SendInput keystrokes. Feels instant even if total time same.

- llama.cpp supports streaming via `stream=True` in `create_chat_completion`
- New endpoint `POST /process-text/stream` returns SSE
- Rust side: type each char as it arrives via SendInput (avoid clipboard
  churn — that's slow and pollutes history)
- Need to detect target HWND at hotkey-release moment, type into it
- Risk: keystroke injection races with user typing if they don't wait

### 3. Inline correction / undo shortcut

**Impact: huge for power users** — 3h, low risk

`Ctrl+Shift+Z` after an injection: undo the inject (`Ctrl+Z`) + restart
recording with the previous context. Recover from mistranscriptions
without manually deleting + retrying.

- New global hotkey: `Ctrl+Shift+Z`
- Store last-injected text length + target HWND
- On press: send `Ctrl+Z` to target, then dispatch `START_RECORDING`
- Tray menu option for the same

### 4. Smart skip-LLM heuristic

**Impact: -1.5 s on simple dictations** — 30 min, medium risk

When distil output is short (< 60 chars) AND grammatically complete
(starts with caps, ends in `.!?`, no obvious typos), skip
`/process-text` and inject distil directly. Saves the LLM round-trip.

- `routers/text_processing.py`: pre-check before LLM call
- Heuristic: `^[A-Z].*[.!?]$` AND no all-caps words AND no obvious
  ASR markers (`<unk>`, `[noise]`)
- Add Settings toggle: "Skip cleanup for simple dictations"

---

## Tier 2 — Feature depth

Ship after Tier 1. Each adds new use cases or significant ergonomics.

### 5. Multi-language support

**Impact: audience expansion** — 4h, low risk

Hardcoded `language="en"` everywhere. Add a language selector in Settings
(EN, HI, ES, FR, DE, JA, ZH cover most users), plus "auto-detect" option
(`language=None` → Whisper detects per session).

- Settings: dropdown bound to localStorage `vox_settings.language`
- `audio/pipeline.py`, `models/dual_loader.py`: parameterize `language`
- `models/prompt_router.py`: load prompts per-language (`prompts/en/`, `prompts/hi/`)
- Distil-large-v3 is multilingual; no model change needed

### 6. Voice commands

**Impact: power feature** — 8h, medium risk

"Delete that", "select all", "new line", "scratch that", custom commands.
Recognized as commands NOT text.

- Command vocabulary in `data/voice_commands.json`
- After distil, before LLM, scan for command patterns
- Commands map to Rust commands (`inject_keypress`, `inject_text`)
- Custom commands via Settings UI: "When I say X, do Y"

### 7. Selection-aware dictation

**Impact: natural editing** — 2h, low risk

If text is selected when hotkey pressed, replace selection instead of
inserting. Standard editor behavior, currently missing.

- `commands/inject.rs`: detect selection via UIA `GetSelection`
- If selection exists: send `Delete` before `Ctrl+V`

### 8. Better app context

**Impact: +5-10 % accuracy in apps** — 6h, medium risk

Beyond exe name + extension: read URL from focused browser tab, file path
from focused IDE pane, current cell from spreadsheet.

- Per-app UIA queries in `commands/context.rs`
- Browser: read URL bar (`AutomationId="urlbar"` for Edge / `omnibox` Chrome)
- VS Code / Cursor: read active file path from title bar pattern
- Pass new fields in `DeepContextPayload`; LLM prompts use them

### 9. Inline transcript preview

**Impact: trust signal** — 3h, low risk

Before injection, show full transcript in capsule for ~500 ms with
option to press Esc to cancel. User catches obvious mistakes.

- New state `previewing` between `finalizing` and `injecting`
- Capsule shows transcript + "Press Esc to cancel"
- Settings toggle (off by default — adds 500 ms)

### 10. Continuous dictation mode

**Impact: long-form workflow** — 6h, medium risk

Toggle "Continuous mode" via hotkey or tray. Mic stays open. Silero
auto-segments speech. Each segment goes through pipeline + injects.

- New state machine: `idle ↔ continuous_listening ↔ continuous_processing`
- Re-uses existing pipeline per segment
- Visual indicator that continuous is active (distinct capsule color)
- Auto-pause on system idle / lock screen

### 11. History view

**Impact: massive trust signal** — 6h, low risk

Searchable list of last 200 dictations. Copy-to-clipboard, replay-as-audio
(if `passive_collection_enabled` saved the WAV), re-inject.

- New Tauri window `/history`
- Reads `data/passive_log.jsonl`
- Audio replay: load `.npy` from `data/audio_clips/` via sounddevice
- Search: substring match + date filter

### 12. Vocabulary auto-learning

**Impact: gets smarter over time** — 4h, low risk

When user manually edits an injected text (or via the new correction
shortcut), detect the edit and add new tokens to `vocabulary.json`.

- Compare original injection vs final clipboard contents after some delay
- Diff to find new tokens, add to vocab
- Settings toggle: "Auto-learn from corrections"

---

## Tier 3 — Distribution + quality

Ship before sharing with beta users.

### 13. CI pipeline

**Impact: sustainable dev** — 4h, low risk

GitHub Actions:
- Push: `cargo check`, `npx tsc --noEmit`, `pytest sidecar/`
- Release tag: build sidecar exe + Tauri bundle + draft GitHub Release

### 14. Test coverage to 60 %

**Impact: catch regressions** — 8h, low risk

Existing pytest covers snippets, replay, hallucination gate. Add:
- `audio/pipeline.py` — dedup, ring buffer, session cap
- `audio/vad.py` — vad_trim_audio with fixture WAVs
- `models/dual_loader.py` — peak_normalize, pre_emphasis (with synthetic audio)
- Snapshot tests for `StreamingOverlay` via Vitest + jsdom

### 15. Code signing certificate

**Impact: eliminates SmartScreen** — $200/yr + 1h setup

Buy DigiCert / Sectigo cert. Tauri config:
```json
"windows": { "certificateThumbprint": "..." }
```
Eliminates "Windows protected your PC" warning on first run.

### 16. Auto-update

**Impact: recurring users** — 6h, low risk

Tauri's `updater` plugin. Check on launch, download in background, install
on next quit. Manifest hosted on GitHub Releases.

### 17. Telemetry (opt-in)

**Impact: bug visibility** — 4h, low risk

Sentry crash reporting. Off by default; Settings toggle to enable.
Send: crash stacks, sidecar startup failures, model load errors.
NEVER send: audio, transcripts, paths.

### 18. NSIS installer polish

**Impact: cleaner install** — 3h, low risk

- Check WebView2 presence, install bootstrapper if missing
- Add Defender exclusion for `src-tauri/binaries/`
- License agreement (MIT)
- Optional: install to Program Files vs per-user

---

## Tier 4 — Long-term vision

Months out. Bigger swings.

### 19. Cross-platform

**Impact: 5× audience** — 3 weeks per platform

- macOS: replace Win32-specific code (UIA → Accessibility API, enigo
  works cross-platform, audio backend portable). Big rewrite of
  `commands/context.rs` + `commands/inject.rs`.
- Linux: AT-SPI for context, libxdo for injection. Wayland is harder
  (no global hotkeys, security boundaries).

### 20. Wake word

**Impact: always-on UX** — 8h, low risk

"Hey Vox" detected by lightweight model (e.g., openWakeWord). No hotkey
needed. Pair with continuous mode (#10).

### 21. AI assistant mode

**Impact: beyond transcription** — 2 weeks

Voice-driven knowledge work:
- "Summarize this PDF" → reads focused PDF, dictates summary
- "Draft a reply to this email" → reads thread context, generates draft
- "Find me the line where I defined `apply_agc`" → grep + jump

Multi-modal LLM (Qwen-VL or similar) reads screen + responds with text.

### 22. Plugin system

**Impact: extensibility** — 1 week + ongoing

JS/TS plugins. Each plugin can:
- Register voice commands
- Modify the system prompt
- Listen to dictation events
- Add UI panels in Settings

### 23. Mobile companion

**Impact: notes anywhere** — 4 weeks

React Native iOS/Android app. Voice notes sync to desktop via cloud
(user provides cloud account). Or LAN sync via local discovery.

### 24. Cloud sync (opt-in)

**Impact: multi-device** — 1 week

E2E-encrypted sync of:
- vocabulary
- custom snippets
- LoRA fine-tuned model
- Settings

User provides their own object storage (S3 / R2 / Drive). Never stores
audio or transcripts in cloud.

### 25. Web companion

**Impact: settings from browser** — 1 week

`vox.local:7777` — manage vocabulary, view history, edit prompts from
any device on the LAN.

---

## Wild ideas — parking lot

Don't implement without strong signal these are wanted.

- **Speaker diarization** — meeting notes mode with multiple speakers labeled
- **Audio recording mode** — save full session WAV alongside transcript
- **Real-time translation** — speak EN, inject HI
- **Voice cloning replay** — TTS your transcript back in your own voice
- **Per-app profile** — switch vocab + style per app automatically
- **Network-aware mode** — Groq fallback only when on trusted networks
- **Battery-aware downshift** — laptop on battery → Turbo-only mode
- **Privacy "incognito" mode** — no passive log, no WAV dump, this session forgotten
- **Backup/restore single-file** — everything in one zip
- **Export to Markdown** — transcript → MD with timestamps
- **Spell-check overlay** — flag low-confidence words with one-key fixes
- **Smart paragraph breaks** — long monologue → auto-paragraphs by pause length
- **Number disambiguation** — "one two three" → "123" or "one two three" by context
- **Punctuation commands** — "period", "question mark", "new paragraph" forced
- **Snippet recording** — say a phrase + name, replay by saying name
- **Code-mode shortcuts** — say "open paren", "semicolon", auto-formatted
- **Multi-user mode** — different vocab per Windows user account

---

## Hardware-bound items

These need GPU / mic upgrade to be worth implementing on the current
machine:

- **Resident distil + Turbo** (no swap) → needs 8 GB GPU. Cuts ~800 ms
  per session.
- **Whisper-large-v3 full (non-distil)** → 6.5 % WER vs distil's 7.0 %.
  Needs ~3 GB more VRAM.
- **Realtime transcription** (sub-1s) → needs much faster GPU or smaller
  model. distil-small (4× faster, ~9 % WER) is the realistic trade.
- **Per-band noise gate** → needs cleaner mic. SST DSP already aggressive.

---

## Suggested order

If you have **one weekend**:
- #1 Auto-stop on silence + #4 Smart skip-LLM + #7 Selection-aware

If you have **one week**:
- All of Tier 1 (#1-4) + #5 Multi-language + #11 History view

If you have **one month**:
- Tier 1 + Tier 2 + #13 CI + #14 Tests

Beyond that, pick one Tier 4 and commit a quarter.

---

## How to use this file

Pick an item. Read CLAUDE.md (project rules) + the relevant memory entries
in `~/.claude/projects/.../memory/`. Branch off main: `feature/<short>`.
Implement following existing patterns. Run `npm run dev:clean` to test.
Commit with conventional-commit format. PR + self-merge.

When done: update this file (mark item as ✓ shipped + commit hash) and
add a new entry to CLAUDE.md "Failed Experiments" if anything was tried
and rejected during the work.
