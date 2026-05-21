# sidecar/CLAUDE.md — Python ML Rules

## Scope
Rules for Python sidecar only. Root CLAUDE.md has full IPC schema, endpoint specs, and build order.

---

## Runtime Environment

- Python 3.11 (not 3.12 — bitsandbytes wheel availability)
- All paths via `sys._MEIPASS` when frozen:
  ```python
  import sys, os
  BASE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
  MODEL_DIR = os.path.join(BASE, "models")
  DATA_DIR = os.path.join(os.path.expanduser("~"), ".vox", "data")
  ```
- `DATA_DIR` lives in user home, not `_MEIPASS` — survives app updates
- Entry point: `main.py` → `uvicorn.run(app, host="127.0.0.1", port=8000)`
- Port conflict: try 8001–8009 if 8000 is taken; write chosen port to `DATA_DIR/port.lock`; Rust re-reads this file on every health poll iteration until sidecar responds

---

## requirements.txt — Pinned Versions

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
pyaudio==0.2.14
faster-whisper==1.0.3
ctranslate2==4.4.0
transformers==4.44.2
bitsandbytes==0.43.3
torch==2.4.1+cu121
torchaudio==2.4.1+cu121
scipy==1.14.1
numpy==1.26.4
peft==0.12.0
noisereduce==3.0.2
llama-cpp-python==0.2.90
pydantic==2.8.2
httpx==0.27.2
groq==0.9.0
```

Install torch with CUDA index: `pip install torch==2.4.1+cu121 --index-url https://download.pytorch.org/whl/cu121`

**GTX 1650 (Turing sm_75) — llama-cpp-python CUDA wheel:** Default PyPI wheel is CPU-only. Install the CUDA-enabled build:
```
pip install llama-cpp-python==0.2.90 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121
```
Verify GPU layers active: `n_gpu_layers=-1` in `Llama(...)` constructor; if CUDA wheel is wrong, all layers silently fall to CPU.

**4GB VRAM note (this machine):** `transformers` and `bitsandbytes` are still required (fine-tuning pipeline uses them). However, Canary model is not loaded at runtime — `distil-whisper/distil-large-v3` via `faster-whisper` is used instead. Both Turbo and distil-large-v3 download to `MODEL_DIR` via faster-whisper's `download_root`.

---

## FastAPI Structure

```
main.py               # app init, lifespan, startup VRAM check
routers/
  dictation.py        # WebSocket /ws/dictation
  text_processing.py  # POST /process-text
  finetuning.py       # POST /finetune/start, GET /finetune/status, POST /finetune/toggle-collection
  health.py           # GET /health, GET /audio/devices
  vocabulary.py       # POST /vocabulary
models/
  dual_loader.py      # VRAM-aware load Turbo + final pass model (distil-large-v3 on this machine)
  prompt_router.py    # exe name → profile → system prompt
  llm_engine.py       # llama-cpp-python wrapper with asyncio lock
audio/
  capture.py          # PyAudio 16kHz/mono/float32 + mic disconnect detection
  pipeline.py         # chunking + overlap + noise suppression + session cap
  vad.py              # energy gate + Silero VAD wrapper
data/
  vocabulary.json     # persisted custom vocab
  passive_log.jsonl   # consent-gated correction pairs
  settings.json       # { "passive_collection_enabled": false }
```

---

## VRAM-Aware Startup Load Plan

At startup, check available VRAM before loading models:

```python
import torch

def get_vram_free_mb() -> int:
    if not torch.cuda.is_available():
        return 0
    free, total = torch.cuda.mem_get_info()
    return free // (1024 * 1024)

def determine_load_plan(vram_mb: int) -> dict:
    if vram_mb >= 7500:
        return {"turbo": "cuda", "final_pass": "canary_cuda", "llm": "cuda"}
    elif vram_mb >= 5000:
        return {"turbo": "cuda", "final_pass": "canary_cpu", "llm": "cuda"}
    elif vram_mb >= 3000:
        # 4GB GPU: use distil-large-v3 sequential (unload Turbo, run distil, reload for next session)
        return {"turbo": "cuda", "final_pass": "distil_sequential", "llm": "cuda"}
    else:
        return {"turbo": "cpu", "final_pass": "skip", "llm": "cpu"}
```

Write load plan to state; expose via `/health` response so Rust can emit `sidecar-degraded` with correct `missing` list.

---

## Audio Capture

- Library: `pyaudio`
- Format: `pyaudio.paFloat32`, channels=1, rate=16000
- Chunk size: 1024 frames
- Device: default; override via `VOX_AUDIO_DEVICE` env var (index integer)
- Resample consumer mics using `scipy.signal.resample_poly`:
  ```python
  from scipy.signal import resample_poly
  audio_16k = resample_poly(audio_raw, 16000, original_rate).astype(np.float32)
  ```
- Normalize int16 input: `audio / 32768.0`

### Audio Device Enumeration (GET /audio/devices)

```python
import pyaudio

def list_audio_devices() -> list[dict]:
    pa = pyaudio.PyAudio()
    devices = []
    default_idx = pa.get_default_input_device_info()["index"]
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:  # input-capable only
            devices.append({
                "index": i,
                "name": info["name"],
                "default": i == default_idx,
            })
    pa.terminate()
    return devices
```

### Mic Disconnect Handling

Wrap PyAudio read in try/except; on `IOError` (error -9988 "stream closed" or -9999 "unanticipated host error"):
```python
try:
    raw = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
except IOError as e:
    # mic unplugged or permission revoked mid-session
    await ws.send_json({"type": "error", "message": f"Microphone error: {e}"})
    session_active = False
    ring_buffer.clear()
    return
```

On startup mic permission check:
```python
pa = pyaudio.PyAudio()
if pa.get_device_count() == 0 or pa.get_default_input_device_info()["maxInputChannels"] == 0:
    # No mic access — Windows privacy settings blocking
    raise RuntimeError("No microphone input detected. Check Windows Privacy → Microphone settings.")
```

### Noise Suppression

Apply before resampling when SNR is low:
```python
import noisereduce as nr

def suppress_noise(audio: np.ndarray, rate: int) -> np.ndarray:
    return nr.reduce_noise(y=audio, sr=rate, stationary=False, prop_decrease=0.75).astype(np.float32)
```

- `stationary=False` handles dynamic noise (fans, keyboards, AC)
- `prop_decrease=0.75` — aggressive enough to help Whisper; not so aggressive it removes speech
- Apply only if RMS > 0.005 (non-silence); skip on pure silence to avoid artifacts
- Adds ~5ms overhead on CPU; acceptable

---

## Hallucination Gate

Whisper hallucinates on silence ("Thank you.", "you", music notes, "♪"). Gate before sending to Turbo:

```python
def should_transcribe(audio_chunk: np.ndarray) -> bool:
    rms = np.sqrt(np.mean(audio_chunk ** 2))
    return rms > 0.01  # silence threshold

def is_hallucination(transcript: str, audio_duration_s: float) -> bool:
    HALLUCINATION_PHRASES = {
        "thank you", "thanks for watching", "you", ".", "...",
        "♪", "♫", "bye", "bye bye",
    }
    stripped = transcript.strip().lower().rstrip(".")
    # also reject transcripts implausibly short for audio duration
    # typical speech: ~2.5 words/sec; flag if ratio < 0.3
    word_count = len(transcript.split())
    min_expected = audio_duration_s * 0.3
    return stripped in HALLUCINATION_PHRASES or (audio_duration_s > 3.0 and word_count < min_expected)
```

If `is_hallucination` returns True, discard segment — do not accumulate into transcript.

---

## Streaming Chunk Algorithm

```python
CHUNK_MS = 1000      # 16000 samples
OVERLAP_MS = 200     # 3200 samples
OVERLAP_SAMPLES = OVERLAP_MS * 16000 // 1000  # 3200
SAMPLE_RATE = 16000

ring_buffer: list[np.ndarray] = []  # list of numpy chunks; concat only for Canary (O(1) per append)
last_valid_timestamp = 0.0
session_words: list[str] = []  # running accumulated words for full session transcript display

async def process_chunk(chunk: np.ndarray, ws):
    if not should_transcribe(chunk):
        return  # silence gate

    # prepend overlap from previous chunk (last OVERLAP_SAMPLES of last chunk)
    overlap = ring_buffer[-1][-OVERLAP_SAMPLES:] if ring_buffer else np.array([], dtype=np.float32)
    feed = np.concatenate([overlap, chunk]) if len(overlap) else chunk

    ring_buffer.append(chunk)  # O(1); concatenate to full session audio only at Canary time

    segments, _ = turbo.transcribe(
        feed,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        language="en",
        beam_size=1,           # streaming: never > 1
        initial_prompt=load_vocabulary_prompt(),
    )

    for seg in segments:
        for word in seg.words:
            if word.start > last_valid_timestamp:
                if not is_hallucination(word.word, word.end - word.start):
                    session_words.append(word.word)
                    last_valid_timestamp = word.end

    if session_words:
        # content is the FULL accumulated transcript so far, not just this chunk's new words
        await ws.send_json({"type": "partial_update", "content": " ".join(session_words)})
```

On `terminate_stream` (or auto-terminate at 60s session cap):
1. Flush remaining audio in ring buffer; `session_words` is already full
2. Run final pass model on full session audio with **30-second timeout**:
   ```python
   import concurrent.futures
   full_audio = np.concatenate(ring_buffer).astype(np.float32)

   # Dispatch based on load_plan["final_pass"]:
   if load_plan["final_pass"] == "distil_sequential":
       # Sequential swap: runs in thread (blocks while Turbo is unloaded)
       with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
           future = executor.submit(run_distil_final_pass, full_audio, turbo_model_ref)
           try:
               final_result = future.result(timeout=30.0)
           except (concurrent.futures.TimeoutError, Exception):
               final_result = ""  # fall back to turbo text; Turbo reload still happens in run_distil_final_pass finally block
   elif load_plan["final_pass"] in ("canary_cuda", "canary_cpu"):
       with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
           future = executor.submit(run_canary, full_audio)
           try:
               final_result = future.result(timeout=30.0)
           except (concurrent.futures.TimeoutError, Exception):
               final_result = ""
   else:
       final_result = ""  # "skip" — no final pass model
   ```
3. If `final_result` is empty → use `" ".join(session_words)` (accumulated Turbo text) as fallback
4. Send `{"type": "handoff_ready", "canary_transcript": final_result or " ".join(session_words)}`
5. `ring_buffer.clear(); session_words.clear(); last_valid_timestamp = 0.0`

Session cap enforcement:
```python
MAX_SAMPLES = 16000 * 60  # 60 seconds

# in process_chunk, after ring_buffer.append():
if sum(len(c) for c in ring_buffer) >= MAX_SAMPLES:
    await handle_terminate_stream(ws)  # auto-terminate
    return
```

On `cancel_stream`:
1. Stop mic immediately
2. `ring_buffer.clear(); session_words.clear()`
3. No Canary pass, no message sent

---

## Turbo Model (faster-whisper)

```python
from faster_whisper import WhisperModel

turbo = WhisperModel(
    "large-v3-turbo",
    device=load_plan["turbo"],   # "cuda" or "cpu"
    compute_type="int8" if load_plan["turbo"] == "cuda" else "float32",
    download_root=MODEL_DIR,
)
```

- VRAM: ~1.2GB (INT8 CUDA)
- WER: ~10.6% — streaming preview only
- `beam_size=1` always for streaming
- `language="en"` always — no per-chunk auto-detect (adds ~30ms)
- `initial_prompt`: inject custom vocabulary string on every chunk

---

## Final Pass Model — Hardware-Conditional

`load_plan["final_pass"]` determines which final pass model loads:

| Value | Model | VRAM | WER | GPU |
|-------|-------|------|-----|-----|
| `canary_cuda` | `nvidia/canary-qwen-2.5b` 8-bit HuggingFace | 3.2 GB | 5.63% | 8GB+ |
| `canary_cpu` | same, device_map="cpu", float32 | 0 VRAM / ~10GB RAM | 5.63% | 6GB |
| `distil_sequential` | `distil-whisper/distil-large-v3` INT8 faster-whisper | 1.5 GB (sequential) | 6.9% | **4GB** |
| `skip` | none | 0 | Turbo only 10.6% | <3GB |

**This machine uses `distil_sequential`.** Sequential means: on `terminate_stream`, unload Turbo from VRAM (`del turbo_model; torch.cuda.empty_cache()`), load distil-large-v3, run final pass, unload distil-large-v3, restore Turbo reference for next session (lazy reload on next `begin_stream`).

---

## Distil-Whisper Final Pass (`distil_sequential`)

```python
from faster_whisper import WhisperModel

DISTIL_MODEL_ID = "distil-whisper/distil-large-v3"

# Loaded only when needed (sequential — not resident like Turbo)
def load_distil() -> WhisperModel:
    return WhisperModel(
        DISTIL_MODEL_ID,
        device="cuda",
        compute_type="int8",
        download_root=MODEL_DIR,
    )

def run_distil_final_pass(audio_np: np.ndarray, turbo_model_ref: list) -> str:
    """
    turbo_model_ref is a list[WhisperModel | None]. Always restores cleanup in
    finally block even on exception — begin_stream checks and reloads Turbo safely.
    LLM stays loaded throughout (2.0GB VRAM held). Only Turbo↔distil swaps.
    """
    import gc
    result = ""
    distil = None
    try:
        # 1. Unload Turbo (free ~1.2GB; LLM 2.0GB stays → 2.0GB used)
        turbo_model_ref[0] = None
        gc.collect()
        torch.cuda.empty_cache()

        # 2. Load distil (~1.5GB VRAM; total with LLM = ~3.5GB → fits in 4GB)
        distil = load_distil()

        # 3. Transcribe full session audio
        segments, _ = distil.transcribe(
            audio_np,
            language="en",
            beam_size=5,
            word_timestamps=False,
            initial_prompt=load_vocabulary_prompt(),
        )
        result = " ".join(seg.text.strip() for seg in segments)

    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        result = ""  # caller uses session_words fallback

    except Exception:
        result = ""  # any failure → fallback

    finally:
        # 4. Always unload distil; turbo_model_ref[0] stays None → lazy reload in begin_stream
        if distil is not None:
            del distil
        gc.collect()
        torch.cuda.empty_cache()

    return result


def reload_turbo_if_needed(turbo_model_ref: list) -> None:
    """Call at start of begin_stream. Reloads Turbo if distil_sequential unloaded it."""
    if turbo_model_ref[0] is None:
        turbo_model_ref[0] = WhisperModel(
            "large-v3-turbo",
            device=load_plan["turbo"],
            compute_type="int8" if load_plan["turbo"] == "cuda" else "float32",
            download_root=MODEL_DIR,
        )
```

Gate in `begin_stream`: call `reload_turbo_if_needed(turbo_model_ref)` before starting mic. Blocks until Turbo is loaded (~1–2s on first post-distil session).

---

## Canary Model (final pass)

```python
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
import torch

CANARY_MODEL_ID = "nvidia/canary-qwen-2.5b"

if load_plan["final_pass"] == "canary_cuda":
    canary = AutoModelForSpeechSeq2Seq.from_pretrained(
        CANARY_MODEL_ID,
        torch_dtype=torch.float16,
        load_in_8bit=True,
        device_map="cuda:0",
        cache_dir=MODEL_DIR,
    )
elif load_plan["final_pass"] == "canary_cpu":
    canary = AutoModelForSpeechSeq2Seq.from_pretrained(
        CANARY_MODEL_ID,
        torch_dtype=torch.float32,
        device_map="cpu",
        cache_dir=MODEL_DIR,
    )
else:
    canary = None  # distil_sequential or skip — Canary not loaded

processor = AutoProcessor.from_pretrained(CANARY_MODEL_ID, cache_dir=MODEL_DIR)
```

Canary inference (final pass only, never streaming chunks):
```python
def run_canary(audio_np: np.ndarray) -> str:
    if canary is None:
        return ""  # degraded — caller falls back to turbo text
    inputs = processor(audio_np, sampling_rate=16000, return_tensors="pt")
    inputs = {k: v.to(canary.device) for k, v in inputs.items()}
    with torch.no_grad():
        # HuggingFace generate uses num_beams, NOT beam_size (that's faster-whisper)
        ids = canary.generate(**inputs, num_beams=5)
    return processor.batch_decode(ids, skip_special_tokens=True)[0]
```

**Critical:** `beam_size` is faster-whisper API. HuggingFace `.generate()` uses `num_beams`. Never mix these.

- VRAM: ~3.2GB (8-bit CUDA)
- WER: ~5.63%
- `num_beams=5` for final pass — latency acceptable since this is post-recording

---

## CUDA Pre-Warming

Run in FastAPI lifespan before accepting connections:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(prewarm)
    yield
    cleanup()

def prewarm():
    if turbo:
        # force CTranslate2 CUDA kernel compile
        dummy = np.zeros(16000, dtype=np.float32)
        list(turbo.transcribe(dummy, language="en"))
    
    if canary and load_plan["final_pass"] == "canary_cuda":
        # Must pass audio through processor first — cannot pass raw tensor directly
        dummy_np = np.zeros(16000, dtype=np.float32)
        dummy_inputs = processor(dummy_np, sampling_rate=16000, return_tensors="pt")
        dummy_inputs = {k: v.to("cuda") for k, v in dummy_inputs.items()}
        with torch.no_grad():
            canary.generate(**dummy_inputs, num_beams=1)
        torch.cuda.empty_cache()

    # distil_sequential: NOT pre-warmed — loaded only at terminate_stream time (sequential swap)

    if llm:
        llm.create_chat_completion(
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
```

Expected prewarm time: ~2500ms RTX 3060 / ~4000ms GTX 1650 (Turbo + LLM only; distil not pre-warmed). Rust polls `/health` in loop; `models-ready` fires only after this completes.

---

## LLM Engine (llama-cpp-python)

```python
import asyncio
from llama_cpp import Llama

_llm_lock = asyncio.Lock()

llm = Llama(
    model_path=os.path.join(MODEL_DIR, "qwen2.5-3b-instruct.Q4_K_M.gguf"),
    n_gpu_layers=-1 if load_plan["llm"] == "cuda" else 0,
    n_ctx=2048,
    verbose=False,
)

async def run_llm(system_prompt: str, text: str) -> str:
    async with _llm_lock:          # serialize — llama.cpp not thread-safe
        return await asyncio.to_thread(_run_llm_sync, system_prompt, text)

def _run_llm_sync(system_prompt: str, text: str) -> str:
    response = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        max_tokens=512,
        temperature=0.1,
    )
    return response["choices"][0]["message"]["content"]
```

- `_llm_lock` prevents concurrent inference — llama.cpp is not thread-safe
- Instance stays resident for process lifetime; never reload per request
- Groq fallback: when `use_local_llm=false`, call Groq API (`groq` Python package); key from `DATA_DIR/groq_key.txt`

### Groq Fallback

Use the `groq` Python SDK (already in requirements.txt), not raw httpx. The SDK is synchronous; wrap in `asyncio.to_thread`:

```python
from groq import Groq

def _run_groq_sync(system_prompt: str, text: str, api_key: str) -> str:
    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        max_tokens=512,
        temperature=0.1,
    )
    return resp.choices[0].message.content

async def run_groq(system_prompt: str, text: str, api_key: str) -> str:
    return await asyncio.to_thread(_run_groq_sync, system_prompt, text, api_key)
```

Never use Groq as silent default — only when user explicitly sets it in Settings.

---

## Prompt Router

File: `models/prompt_router.py`

Maps `executable_name` (basename, lowercased, `.exe` stripped) → profile → system prompt.

| Profile | Exe examples | Extension override |
|---------|-------------|-------------------|
| `code_editor` | code, cursor, pycharm, idea, vim, nvim | `.py .ts .rs .go .cpp .cs` |
| `email_client` | outlook, thunderbird | subject line cues |
| `chat_messaging` | teams, slack, discord, telegram | — |
| `document_editor` | winword, libreoffice, notepad, obsidian | `.md .txt .docx` |
| `terminal_interface` | windowsterminal, cmd, powershell, pwsh | — |
| `neutral_fallback` | everything else | — |

Resolution order: `inferred_extension` first → `executable_name` dict → `neutral_fallback`.

Each profile has a system prompt template in `prompts/{profile}.txt`. Template variables:
- `{preceding}` — text before cursor (max 300 chars)
- `{vocabulary}` — custom vocabulary hint list

Example (`prompts/code_editor.txt`):
```
You are a code dictation assistant. Fix ASR errors in the dictated text.
Preserve technical terms, variable names, and syntax. Do not add explanations.
Context (code before cursor): {preceding}
Custom terms: {vocabulary}
Return only the corrected text, nothing else.
```

---

## Custom Vocabulary

File: `data/vocabulary.json` — `["word1", "word2", ...]`

`initial_prompt` construction for Whisper. Whisper `initial_prompt` max is ~224 tokens (~900 characters). Truncate:
```python
MAX_PROMPT_CHARS = 800

def load_vocabulary_prompt() -> str:
    try:
        with open(os.path.join(DATA_DIR, "vocabulary.json")) as f:
            words = json.load(f)
        if not words:
            return ""
        prompt = ", ".join(words)
        return prompt[:MAX_PROMPT_CHARS]  # truncate to stay within Whisper token limit
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
```

`POST /vocabulary` replaces the file; takes effect on next `begin_stream`. No restart required.

---

## `/process-text` Endpoint

Note: `custom_vocabulary` in the HTTP request schema is sent by the frontend but the sidecar reads vocabulary from `DATA_DIR/vocabulary.json` (kept current via `POST /vocabulary`). The payload field is accepted but unused — sidecar state is authoritative.

```python
@router.post("/process-text")
async def process_text(payload: ProcessTextRequest) -> ProcessTextResponse:
    try:
        profile = prompt_router.route(payload.executable_name, payload.inferred_extension)
        vocabulary = load_vocabulary_prompt()  # reads from DATA_DIR/vocabulary.json
        system_prompt = render_prompt(profile, payload.text_preceding_cursor, vocabulary)
        
        if payload.use_local_llm and llm is not None:
            cleaned = await run_llm(system_prompt, payload.raw_transcript)
        elif not payload.use_local_llm:
            api_key = load_groq_key()
            if api_key:
                cleaned = await run_groq(system_prompt, payload.raw_transcript, api_key)
            else:
                cleaned = payload.raw_transcript
        else:
            cleaned = payload.raw_transcript  # LLM not loaded
        
        return {"cleaned_text": cleaned}
    except Exception as e:
        return {"cleaned_text": payload.raw_transcript, "error": str(e)}
```

---

## Groq API Key Endpoint

`POST /finetune/groq-test` — saves key and validates it with a test call. Registered in `routers/finetuning.py`.

```python
GROQ_KEY_PATH = os.path.join(DATA_DIR, "groq_key.txt")

@router.post("/finetune/groq-test")
async def groq_test(payload: GroqTestRequest) -> dict:
    # Save key regardless; test will reveal if it's invalid
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(GROQ_KEY_PATH, "w") as f:
        f.write(payload.api_key.strip())
    try:
        result = await run_groq("You are helpful.", payload.raw_transcript or "hello", payload.api_key.strip())
        return {"cleaned_text": result, "ok": True}
    except Exception as e:
        return {"cleaned_text": "", "ok": False, "error": str(e)}

def load_groq_key() -> str:
    try:
        with open(GROQ_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
```

Request: `{ "api_key": "gsk_...", "raw_transcript": "test phrase" }`

---

## Fine-Tuning Pipeline

`POST /finetune/start` triggers in background thread — never blocks HTTP response.

LoRA config:
```python
from peft import LoraConfig

lora_config = LoraConfig(
    r=32,
    lora_alpha=64,
    target_modules=["q_proj", "v_proj"],
    lora_dropout=0.05,
    bias="none",
    task_type="SEQ_2_SEQ_LM",
)
```

Training target: `distil-whisper/distil-large-v3` (756M params) on this machine (NOT Canary 2.5B).

Training constraints — hardware-conditional:

| Config | 12GB GPU | **4GB GPU (this machine)** |
|--------|----------|---------------------------|
| LoRA r | 32 | **8** |
| alpha | 64 | **16** |
| batch size | 4 | **1** |
| grad_accumulation | 4 | **8** |
| LLM during training | loaded | **unload first** |

4GB procedure: Before training subprocess starts, unload LLM (`llm = None; gc.collect(); torch.cuda.empty_cache()`). `_training_active = True` blocks dictation (which would try to reload Turbo/run distil). After training completes, reload LLM.

- FP16 + gradient checkpointing always
- `GET /finetune/status` reads `DATA_DIR/finetune_progress.json`; returns default if file absent:
  ```python
  DEFAULT_STATUS = {"status": "idle", "progress": 0.0, "epoch": 0, "total_epochs": 0, "samples": 0, "error": None}
  ```
  Sample count (`samples`) is always computed by counting lines in `passive_log.jsonl`, not stored in progress file.
- Input: `data/passive_log.jsonl` pairs `{"raw_asr": "...", "user_edited": "..."}`
- Output: `ct2-transformers-converter --model ./lora-merged --quantization float16 --output_dir ./ct2-model`
- Progress written to `data/finetune_progress.json`; `GET /finetune/status` reads this

Passive data schema:
```json
{"timestamp": "ISO8601", "raw_asr": "...", "user_edited": "...", "profile": "code_editor"}
```

### `POST /finetune/toggle-collection`

```python
@router.post("/finetune/toggle-collection")
async def toggle_collection(payload: ToggleCollectionRequest) -> dict:
    settings = load_sidecar_settings()
    settings["passive_collection_enabled"] = payload.enabled
    save_sidecar_settings(settings)
    return {"ok": True, "enabled": payload.enabled}

def load_sidecar_settings() -> dict:
    path = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"passive_collection_enabled": False}

def save_sidecar_settings(settings: dict) -> None:
    path = os.path.join(DATA_DIR, "settings.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f)
```

`passive_collection_enabled` is read from `DATA_DIR/settings.json` on every append — not cached in memory (survives sidecar restart with correct state).

Only write to passive log if `passive_collection_enabled` is True.

---

## PyInstaller Rules

Spec file: `sidecar/sidecar.spec`

```python
# CUDA toolkit DLLs (in CUDA toolkit bin\)
CUDA_BIN = "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.1/bin"
# cuDNN DLLs (in SEPARATE cuDNN install path — NOT in CUDA toolkit)
CUDNN_BIN = "C:/Program Files/NVIDIA/CUDNN/v8.9/bin"

datas=[
    (os.path.join(MODEL_DIR, "*"), "models"),
    ("prompts/*.txt", "prompts"),
    # CUDA runtime
    (f"{CUDA_BIN}/cudart64_12.dll", "."),
    (f"{CUDA_BIN}/cublas64_12.dll", "."),
    (f"{CUDA_BIN}/cublasLt64_12.dll", "."),
    (f"{CUDA_BIN}/cufft64_11.dll", "."),
    # cuDNN — DIFFERENT PATH than CUDA toolkit
    (f"{CUDNN_BIN}/cudnn64_8.dll", "."),
    (f"{CUDNN_BIN}/cudnn_ops_infer64_8.dll", "."),
    (f"{CUDNN_BIN}/cudnn_cnn_infer64_8.dll", "."),
],
hiddenimports=[
    "faster_whisper",
    "ctranslate2",
    "llama_cpp",
    "bitsandbytes",
    "noisereduce",
    "scipy.signal",
    "scipy._lib.messagestream",
    "groq",
],
```

Additional collections:
- `collect_dynamic_libs("llama_cpp")` — captures llama-cpp-python CUDA kernels
- `collect_dynamic_libs("ctranslate2")` — captures CTranslate2 kernels
- `collect_all("noisereduce")` — noisereduce uses pkg_resources for data files
- Output name: `sidecar-x86_64-pc-windows-msvc.exe`
- Windows Defender flags PyInstaller output — users must add `src-tauri/binaries/` to exclusions
- SmartScreen blocks unsigned binary on first launch — right-click → Properties → Unblock, or obtain code-signing cert

---

## Error Handling

- All endpoints return HTTP 200; errors in `{"error": "..."}` field alongside `cleaned_text` fallback
- `torch.cuda.OutOfMemoryError` → `torch.cuda.empty_cache()` → retry with CPU fallback → return raw transcript
- Model load failure → log stderr → mark model absent in state → continue; Rust reads `/health` and emits `sidecar-degraded`
- Never `sys.exit()` from handlers — Tauri interprets process death as crash

---

## Concurrent Session Handling

```python
_session_active: bool = False  # True from begin_stream until terminate/cancel complete
_training_active: bool = False  # True from finetune/start until training thread exits
```

Guard logic (enforced at WebSocket handler and `/finetune/start` handler):

```python
# In ws/dictation begin_stream handler:
if _session_active:
    await ws.send_json({"type": "error", "message": "Session already active"})
    return
if _training_active:
    await ws.send_json({"type": "error", "message": "Training in progress"})
    return
_session_active = True

# In /finetune/start handler:
if _session_active:
    raise HTTPException(status_code=409, detail="Dictation session active")
if _training_active:
    raise HTTPException(status_code=409, detail="Training already running")
_training_active = True
# ... launch training thread; set _training_active = False in finally block
```

- LLM requests serialized via `_llm_lock`
- Final pass inference: not concurrent — only one final pass per session; distil_sequential enforces this via `_session_active` flag

---

## Constraints (Never Violate)

1. Audio to Whisper: 16kHz / mono / float32 — no exceptions
2. Turbo = streaming preview only; final pass model (distil-large-v3 on this machine, Canary on 8GB+) = final pass on full session audio
3. `canary_transcript` field in `handoff_ready` is the correct input to `/process-text` — field name is fixed wire format; content is final pass output or Turbo fallback. Never send raw Turbo partial accumulation.
4. `_llm_lock` must wrap every LLM inference call
5. `sys._MEIPASS` path for all bundled assets in frozen build
6. `--onedir` PyInstaller; never `--onefile`
7. CUDA pre-warm must complete before Rust `/health` gets `status: "ok"`
8. No cloud calls for core pipeline; Groq only when user explicitly sets API key
9. Hallucination gate and silence gate before every Turbo call
10. Custom vocabulary `initial_prompt` injected on every Turbo chunk, not just first
