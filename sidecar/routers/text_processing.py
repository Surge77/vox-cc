import asyncio
import json
import os
import queue
import re
import threading
import time
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()


class ProcessTextRequest(BaseModel):
    raw_transcript: str
    context_string: str = ""
    executable_name: str = ""
    window_title: str = ""
    inferred_extension: str | None = None
    text_preceding_cursor: str = ""
    text_succeeding_cursor: str = ""
    use_local_llm: bool = True
    custom_vocabulary: list[str] = []
    style: str = "auto"
    skip_llm_if_simple: bool = False
    # Tier 2 #8 — extended app context. None when not applicable / UIA failed.
    app_url: str | None = None
    app_file_path: str | None = None


class ProcessTextResponse(BaseModel):
    cleaned_text: str
    error: str | None = None
    skipped_llm: bool = False
    # Tier 2 #6 — when raw transcript matches a voice command, the sidecar
    # short-circuits LLM cleanup and returns the key combo for the Tauri
    # layer to replay via inject_keypress. cleaned_text is empty in that case.
    command_key: str | None = None


# ── Smart skip-LLM heuristic ──────────────────────────────────────────────────
# When distil output is short + grammatically complete, the LLM cleanup pass
# rarely changes anything. Skipping saves ~1.5 s per dictation.

SKIP_LLM_MAX_CHARS = 60
_ASR_MARKERS = ("<unk>", "[noise]", "[music]", "[laughter]", "♪", "♫")
_SENTENCE_RE = re.compile(r"^[A-Z][^<>\[\]]*[.!?]$")


def is_simple_clean_text(text: str) -> bool:
    """True iff text is short enough + grammatically complete + no ASR markers
    + no all-caps words (which usually signal acronyms the LLM should handle).
    """
    stripped = text.strip()
    if not stripped or len(stripped) > SKIP_LLM_MAX_CHARS:
        return False
    low = stripped.lower()
    if any(m in low for m in _ASR_MARKERS):
        return False
    if not _SENTENCE_RE.match(stripped):
        return False
    # All-caps tokens longer than 1 char usually need vocab/expansion (IDE, API)
    for word in stripped.split():
        bare = word.strip(",.!?;:\"'()")
        if len(bare) > 1 and bare.isupper():
            return False
    return True


@router.post("/process-text", response_model=ProcessTextResponse)
async def process_text(req: ProcessTextRequest):
    import main as state
    from models.prompt_router import render_system_prompt

    system_prompt = render_system_prompt(
        req.executable_name,
        preceding_text=req.text_preceding_cursor,
        vocabulary=req.custom_vocabulary or [],
        style=req.style,
    )
    raw = req.raw_transcript.strip()

    if not raw:
        return ProcessTextResponse(cleaned_text="")

    # Tier 2 #6 — voice commands take priority over LLM cleanup.
    from routers.voice_commands import load_commands, match_command
    cmd = match_command(raw, load_commands(state.DATA_DIR))
    if cmd is not None:
        return ProcessTextResponse(cleaned_text="", command_key=cmd)

    if req.skip_llm_if_simple and is_simple_clean_text(raw):
        cleaned = _expand_snippets(state.DATA_DIR, raw)
        _append_passive_log(state.DATA_DIR, raw, cleaned, req.executable_name, None)
        return ProcessTextResponse(cleaned_text=cleaned, skipped_llm=True)

    cleaned = raw
    error = None
    llm_ms: int | None = None
    try:
        t0 = time.monotonic()
        if req.use_local_llm and state._models_state.get("llm"):
            llm = state._llm_ref[0]
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_local, llm, system_prompt, raw),
                timeout=15.0,
            )
            cleaned = result if result else raw
        else:
            groq_key = _load_groq_key()
            if not groq_key:
                return ProcessTextResponse(cleaned_text=raw, error="no groq key")
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_groq, groq_key, system_prompt, raw),
                timeout=15.0,
            )
            cleaned = result if result else raw
        llm_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        error = str(e)

    cleaned = _expand_snippets(state.DATA_DIR, cleaned)
    _append_passive_log(state.DATA_DIR, raw, cleaned, req.executable_name, llm_ms)
    return ProcessTextResponse(cleaned_text=cleaned, error=error)


def _run_local(llm, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_llm
    return run_llm(llm, system_prompt, raw)


def _run_groq(api_key: str, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_groq
    return run_groq(api_key, system_prompt, raw)


def _load_groq_key() -> str:
    data_dir = os.path.join(os.path.expanduser("~"), ".vox", "data")
    key_path = os.path.join(data_dir, "groq_key.txt")
    try:
        with open(key_path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _expand_snippets(data_dir: str, text: str) -> str:
    import json
    import re
    try:
        with open(os.path.join(data_dir, "snippets.json")) as f:
            snippets: dict[str, str] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return text
    for trigger, expansion in snippets.items():
        text = re.sub(rf"(?i)\b{re.escape(trigger)}\b", expansion, text)
    return text


# Vocabulary auto-learning helpers (Tier 2 #12) ──────────────────────────────
# Common English stopwords that should NEVER be auto-added even if the LLM
# inserts them. Keeps the vocab list focused on domain terms.
_AUTOLEARN_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "they", "them",
    "their", "have", "will", "would", "could", "should", "about", "after",
    "before", "your", "you're", "what", "when", "where", "which", "while",
    "into", "than", "then", "been", "being", "were", "more", "some",
    "also", "just", "only", "very", "much", "still", "even", "back",
    "over", "under", "such", "here", "there", "these", "those", "other",
}
_AUTOLEARN_MIN_LEN = 4


def _tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric word tokens. Strips punctuation."""
    return re.findall(r"[A-Za-z][A-Za-z0-9]+", text.lower())


def extract_new_tokens(raw: str, cleaned: str) -> list[str]:
    """Return tokens present in `cleaned` but not in `raw` that look like
    candidate domain-specific vocab words: ≥4 chars, alphanumeric, not a
    stopword. Deduplicated, order-preserving.

    Used by the auto-learn pipeline to flag terms the LLM corrected into the
    transcript that the user may want to add to their custom vocabulary.
    """
    raw_tokens = set(_tokenize(raw))
    seen: set[str] = set()
    out: list[str] = []
    for tok in _tokenize(cleaned):
        if tok in raw_tokens or tok in seen:
            continue
        if len(tok) < _AUTOLEARN_MIN_LEN:
            continue
        if tok in _AUTOLEARN_STOPWORDS:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _maybe_auto_learn_vocab(data_dir: str, raw: str, cleaned: str) -> None:
    """Append new tokens to vocabulary.json when auto-learn is enabled.

    Reads `auto_learn_enabled` from settings.json — gated by user opt-in.
    Preserves the existing vocab order; new tokens append at the end.
    """
    import json
    settings_path = os.path.join(data_dir, "settings.json")
    try:
        with open(settings_path) as f:
            if not json.load(f).get("auto_learn_enabled", False):
                return
    except (FileNotFoundError, json.JSONDecodeError):
        return

    new_tokens = extract_new_tokens(raw, cleaned)
    if not new_tokens:
        return

    vocab_path = os.path.join(data_dir, "vocabulary.json")
    try:
        with open(vocab_path) as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = []
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    existing_lc = {w.lower() for w in existing if isinstance(w, str)}
    added = [tok for tok in new_tokens if tok not in existing_lc]
    if not added:
        return

    os.makedirs(data_dir, exist_ok=True)
    with open(vocab_path, "w") as f:
        json.dump(existing + added, f, indent=2)


def _append_passive_log(
    data_dir: str,
    raw: str,
    cleaned: str,
    executable_name: str,
    llm_ms: int | None = None,
) -> None:
    # Auto-learn vocab runs independently of passive logging: a user can
    # opt into vocab learning without sharing transcripts with the
    # fine-tuning collector.
    _maybe_auto_learn_vocab(data_dir, raw, cleaned)

    import json
    from datetime import datetime, timezone
    # History logging is enabled by default — the user can disable it via
    # the privacy retention slider (retention_days=0 stops collection).
    # Passive_collection_enabled still gates the heavier audio-clip save
    # (handled separately in routers/dictation._maybe_save_audio).
    settings_path = os.path.join(data_dir, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
            if settings.get("history_logging_enabled", True) is False:
                return
            if settings.get("retention_days") == 0:
                return
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # default behavior is to log

    from models.prompt_router import get_profile
    import main as state
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_asr": raw,
        "user_edited": cleaned,
        "profile": get_profile(executable_name),
    }
    if state._pending_audio_uuid:
        entry["audio_file"] = f"{state._pending_audio_uuid}.npy"
        state._pending_audio_uuid = None
    if state._pending_latencies:
        entry.update(state._pending_latencies)
        state._pending_latencies = None
    if llm_ms is not None:
        entry["llm_ms"] = llm_ms

    log_path = os.path.join(data_dir, "passive_log.jsonl")
    os.makedirs(data_dir, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── SSE streaming variant of /process-text ────────────────────────────────────
# Used by the frontend's "Stream text as the LLM generates" feature: instead of
# waiting for the full cleaned transcript, callers receive each delta as it is
# produced. The Rust side types each delta with `inject_text_chunk`.


def _sse_pack(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/process-text/stream")
async def process_text_stream(req: ProcessTextRequest):
    import main as state
    from models.prompt_router import render_system_prompt

    system_prompt = render_system_prompt(
        req.executable_name,
        preceding_text=req.text_preceding_cursor,
        vocabulary=req.custom_vocabulary or [],
        style=req.style,
    )
    raw = req.raw_transcript.strip()

    # Skip-LLM heuristic still applies in streaming mode: a one-line response
    # is cheap to send and avoids spinning up the model for trivial inputs.
    if not raw:

        async def _empty():
            yield _sse_pack("done", {"cleaned_text": ""})

        return StreamingResponse(_empty(), media_type="text/event-stream")

    # Voice command short-circuit (Tier 2 #6) — applies in streaming mode too.
    from routers.voice_commands import load_commands, match_command
    cmd = match_command(raw, load_commands(state.DATA_DIR))
    if cmd is not None:

        async def _cmd():
            yield _sse_pack("done", {"cleaned_text": "", "command_key": cmd})

        return StreamingResponse(_cmd(), media_type="text/event-stream")

    if req.skip_llm_if_simple and is_simple_clean_text(raw):
        cleaned = _expand_snippets(state.DATA_DIR, raw)
        _append_passive_log(state.DATA_DIR, raw, cleaned, req.executable_name, None)

        async def _skip():
            yield _sse_pack("delta", {"text": cleaned})
            yield _sse_pack("done", {"cleaned_text": cleaned, "skipped_llm": True})

        return StreamingResponse(_skip(), media_type="text/event-stream")

    # Producer thread drives the (sync) llama-cpp generator; queue bridges into
    # asyncio. The sentinel `None` marks completion; exceptions propagate as a
    # second-item tuple so the async side can emit an SSE error.
    use_local = req.use_local_llm and bool(state._models_state.get("llm"))
    groq_key = "" if use_local else _load_groq_key()

    if not use_local and not groq_key:

        async def _no_key():
            yield _sse_pack("delta", {"text": raw})
            yield _sse_pack(
                "done", {"cleaned_text": raw, "error": "no groq key"}
            )

        return StreamingResponse(_no_key(), media_type="text/event-stream")

    q: "queue.Queue[tuple[str | None, str | None]]" = queue.Queue()

    def _producer():
        try:
            from models.llm_engine import run_llm_stream, run_groq_stream
            if use_local:
                stream_iter = run_llm_stream(state._llm_ref[0], system_prompt, raw)
            else:
                stream_iter = run_groq_stream(groq_key, system_prompt, raw)
            for delta in stream_iter:
                q.put((delta, None))
        except Exception as e:  # noqa: BLE001 — propagate to client
            q.put((None, str(e)))
            return
        q.put((None, None))

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    async def _stream():
        full_text_parts: list[str] = []
        error: str | None = None
        t0 = time.monotonic()
        while True:
            try:
                delta, err = await asyncio.to_thread(q.get, True, 30.0)
            except queue.Empty:
                error = "timeout"
                break
            if err is not None:
                error = err
                break
            if delta is None:
                break
            full_text_parts.append(delta)
            yield _sse_pack("delta", {"text": delta})

        cleaned = "".join(full_text_parts).strip() or raw
        cleaned = _expand_snippets(state.DATA_DIR, cleaned)
        llm_ms = int((time.monotonic() - t0) * 1000)
        _append_passive_log(
            state.DATA_DIR, raw, cleaned, req.executable_name, llm_ms
        )
        yield _sse_pack(
            "done", {"cleaned_text": cleaned, "error": error}
        )

    return StreamingResponse(_stream(), media_type="text/event-stream")
