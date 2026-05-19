# Security Policy

## Supported Versions

Vox is currently in active development (pre-1.0). Security fixes are applied to the `main` branch only.

| Version | Supported |
|---|---|
| `main` (latest) | ✅ |
| Older tagged releases | ❌ |

---

## Privacy Model

Vox is designed to be entirely local. By default:

- **No audio leaves the machine.** Microphone input is processed in-process by the Python sidecar and discarded after transcription.
- **No transcripts leave the machine.** All ASR and LLM inference runs locally.
- **No telemetry.** The application makes no outbound network requests during normal operation.

### Optional cloud paths (explicit opt-in only)

| Feature | Network call | How to disable |
|---|---|---|
| Groq API fallback | POST to `api.groq.com` | Do not configure a Groq API key in Settings |
| Fine-tuning data collection | Writes to local `passive_log.jsonl` only | Toggle off in FineTuning dashboard |

If you configure a Groq API key, raw transcripts will be sent to Groq's servers for LLM post-processing. This is an explicit user action and is clearly labeled in the UI.

---

## Threat Model

| Attack surface | Notes |
|---|---|
| Local HTTP API (`127.0.0.1:8000`) | Bound to loopback only; not accessible from the network. |
| WebSocket (`ws://127.0.0.1:8000`) | Same — loopback only. |
| Clipboard injection | Text is injected via clipboard swap + Ctrl+V. The original clipboard is restored after 150 ms. |
| PyInstaller binary | Windows Defender may flag the sidecar binary. See the README for exclusion instructions. |
| Model files | Downloaded from Hugging Face at first run. Verify checksums if operating in a high-security environment. |

---

## Reporting a Vulnerability

**Do not open a public GitHub Issue for security vulnerabilities.**

Please report security issues privately:

1. Go to the repository's **Security** tab on GitHub.
2. Click **"Report a vulnerability"** to open a private advisory.
3. Include: a description of the issue, reproduction steps, potential impact, and your contact info if you'd like a response.

We aim to acknowledge reports within **72 hours** and provide an initial assessment within **7 days**.

If the GitHub private advisory form is unavailable, contact the maintainer directly via the email listed in the GitHub profile.

---

## Out of Scope

The following are known behaviors, not vulnerabilities:

- Windows Defender flagging the PyInstaller sidecar binary (common for all PyInstaller apps; not a supply-chain issue)
- Clipboard history (`Win+V`) showing injected text (Windows OS behavior; not controllable by the app)
- The local HTTP API being accessible to other processes running as the same user on the same machine
