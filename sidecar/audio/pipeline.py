import asyncio
import json
import logging
import os
import numpy as np

from audio.capture import AudioCapture, PersistentMicCapture, SAMPLE_RATE
from audio.vad import should_transcribe, is_hallucination, suppress_noise, apply_agc

logger = logging.getLogger(__name__)

CHUNK_MS = 1000      # spec value — 500ms halved Whisper's context window
OVERLAP_MS = 200     # spec value — covers cross-chunk word boundaries
CHUNK_SAMPLES = CHUNK_MS * SAMPLE_RATE // 1000      # 16000
OVERLAP_SAMPLES = OVERLAP_MS * SAMPLE_RATE // 1000  # 3200
MAX_SAMPLES = SAMPLE_RATE * 60                       # 960000 — 60s cap

MAX_PROMPT_CHARS = 800


def load_vocabulary_prompt() -> str:
    from main import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, "vocabulary.json")) as f:
            words = json.load(f)
        return ", ".join(words)[:MAX_PROMPT_CHARS] if words else ""
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def _read_audio_settings() -> dict:
    from main import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, "settings.json")) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Whitelist of language codes accepted by faster-whisper. "auto" → None →
# Whisper performs auto-detect per session. Anything else: keep "en" default
# behavior unless user picks a supported code.
SUPPORTED_LANGUAGES = {"en", "hi", "es", "fr", "de", "ja", "zh", "pt", "ru", "it", "ko", "ar", "nl"}


def resolve_language(code: str | None) -> str | None:
    """Map a settings value to the `language` argument for Whisper.transcribe.

    - "auto" / "" / None → None (Whisper auto-detect)
    - one of SUPPORTED_LANGUAGES → that code
    - anything else → fall back to "en" for safety (avoids passing junk into
      the model and getting unexpected behavior).
    """
    if not code:
        return None
    normalized = code.strip().lower()
    if normalized == "auto":
        return None
    if normalized in SUPPORTED_LANGUAGES:
        return normalized
    return "en"


def load_language_setting() -> str | None:
    return resolve_language(_read_audio_settings().get("language"))


def _dedup_overlap(prev_tail: list[str], new_text: str) -> str:
    """Remove from new_text any prefix that repeats the suffix of prev_tail.
    Handles the 200ms overlap region where Turbo may re-transcribe earlier words."""
    new_words = new_text.split()
    if not prev_tail or not new_words:
        return new_text
    for n in range(min(len(prev_tail), len(new_words), 6), 0, -1):
        if [w.lower() for w in prev_tail[-n:]] == [w.lower() for w in new_words[:n]]:
            return " ".join(new_words[n:])
    return new_text


class DictationSession:
    """One dictation session backed by a shared PersistentMicCapture.

    The mic is owned by main.py and stays open for the sidecar's lifetime.
    A session subscribes a listener that fans chunks from the background
    mic thread into this session's asyncio.Queue (via call_soon_threadsafe).
    On start_session() the last ~3s of pre-roll audio is copied into the
    ring buffer so the user's first words — spoken during WASAPI warmup or
    even slightly before the hotkey was pressed — are not lost."""

    def __init__(
        self,
        mic: PersistentMicCapture,
        turbo_model_ref: list,
        load_plan: dict,
        loop: asyncio.AbstractEventLoop,
    ):
        self._mic = mic
        self._turbo_ref = turbo_model_ref
        self._load_plan = load_plan
        self._loop = loop
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._ring: list[np.ndarray] = []
        self._session_words: list[str] = []
        self._prev_tail: list[str] = []
        self._vocab_prompt: str | None = None
        self._active = False
        self._listener = None

    def _get_vocab_prompt(self) -> str:
        if self._vocab_prompt is None:
            self._vocab_prompt = load_vocabulary_prompt()
        return self._vocab_prompt

    def _on_chunk_from_mic_thread(self, chunk: np.ndarray) -> None:
        """Listener callback. Runs on PersistentMic background thread —
        bridges into asyncio via the loop's thread-safe scheduler."""
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        except RuntimeError:
            # Loop already closed (sidecar shutting down); drop silently.
            pass

    def start_session(self) -> None:
        """Snapshot pre-roll into ring buffer + subscribe to live chunks.
        Must be called from the asyncio loop thread (read by _loop)."""
        preroll = self._mic.snapshot_preroll()
        if preroll:
            self._ring.extend(preroll)
        self._listener = self._on_chunk_from_mic_thread
        self._mic.add_listener(self._listener)
        self._active = True

    def stop_session(self) -> None:
        """Stop accepting new chunks. Pre-existing queue contents stay so the
        capture loop can drain them. Safe to call multiple times."""
        self._active = False
        if self._listener is not None:
            self._mic.remove_listener(self._listener)
            self._listener = None

    # Legacy aliases kept for any caller not yet migrated.
    def stop_capture(self) -> None:
        self._active = False

    def close_stream(self) -> None:
        self.stop_session()

    def close_mic(self) -> None:
        self.stop_session()

    def is_active(self) -> bool:
        return self._active

    async def read_chunk(self, timeout: float = 0.2) -> np.ndarray | None:
        """Pull next chunk from queue. Returns None on timeout so the caller
        can re-check the _active flag."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def flush_partial(self, chunk: np.ndarray) -> None:
        """Append remaining partial audio to ring so distil final pass gets complete session audio."""
        if len(chunk) > 0:
            self._ring.append(chunk)

    async def process_chunk(self, chunk: np.ndarray, ws) -> bool:
        """
        Process one audio chunk. Returns True if session should auto-terminate (60s cap).
        Ring always stores every raw chunk so distil final pass gets complete session audio.
        Flow: Silero VAD gate (chunk level) → AGC + noise suppress → Turbo → textual dedup.
        """
        # Always store raw audio — distil needs the full unprocessed session
        overlap = self._ring[-1][-OVERLAP_SAMPLES:] if self._ring else np.array([], dtype=np.float32)
        self._ring.append(chunk)

        total = sum(len(c) for c in self._ring)
        if total >= MAX_SAMPLES:
            return True

        # Silero VAD gate on raw chunk — don't run Turbo on silence
        if not should_transcribe(chunk):
            return False

        turbo = self._turbo_ref[0]
        if turbo is None:
            return False

        # Build Turbo feed: overlap + current chunk. AGC and NR are configurable via settings.
        raw_feed = np.concatenate([overlap, chunk]) if len(overlap) else chunk.copy()
        audio_settings = _read_audio_settings()
        feed = apply_agc(raw_feed) if audio_settings.get("agc_enabled", True) else raw_feed
        if audio_settings.get("noise_reduction_enabled", False):
            feed = suppress_noise(feed, SAMPLE_RATE)

        vocab_prompt = self._get_vocab_prompt()
        try:
            segments, _ = turbo.transcribe(
                feed,
                word_timestamps=False,   # textual dedup instead — avoids expensive timestamp scan
                language=load_language_setting(),  # None → auto-detect; else supported code
                beam_size=1,
                condition_on_previous_text=False,  # prevents hallucination feedback loops
                initial_prompt=vocab_prompt or None,
            )
            segments = list(segments)
        except Exception:
            return False

        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            if is_hallucination(text, seg.end - seg.start):
                continue
            deduped = _dedup_overlap(self._prev_tail, text)
            if deduped.strip():
                words = deduped.split()
                self._session_words.extend(words)
                self._prev_tail = self._session_words[-6:]

        if self._session_words:
            await ws.send_json({
                "type": "partial_update",
                "content": " ".join(self._session_words),
            })

        return False

    def build_turbo_fallback(self) -> str:
        return " ".join(self._session_words)

    def get_full_audio(self) -> np.ndarray:
        """Concatenate raw ring buffer. Called by final-pass logic."""
        if not self._ring:
            return np.array([], dtype=np.float32)
        return np.concatenate(self._ring).astype(np.float32)

    def reset(self) -> None:
        self._ring.clear()
        self._session_words.clear()
        self._prev_tail.clear()
        self._vocab_prompt = None
