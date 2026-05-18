import asyncio
import json
import logging
import os
import numpy as np

from audio.capture import AudioCapture, SAMPLE_RATE
from audio.vad import should_transcribe, is_hallucination, suppress_noise, apply_agc

logger = logging.getLogger(__name__)

CHUNK_MS = 500
OVERLAP_MS = 150
CHUNK_SAMPLES = CHUNK_MS * SAMPLE_RATE // 1000      # 8000
OVERLAP_SAMPLES = OVERLAP_MS * SAMPLE_RATE // 1000  # 2400
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
    def __init__(self, turbo_model_ref: list, load_plan: dict):
        self._turbo_ref = turbo_model_ref
        self._load_plan = load_plan
        self._ring: list[np.ndarray] = []
        self._session_words: list[str] = []
        self._prev_tail: list[str] = []      # last few words for textual overlap dedup
        self._vocab_prompt: str | None = None  # cached for session lifetime
        self._capture: AudioCapture | None = None
        self._active = False

    def _get_vocab_prompt(self) -> str:
        if self._vocab_prompt is None:
            self._vocab_prompt = load_vocabulary_prompt()
        return self._vocab_prompt

    def open_mic(self, device_index: int | None = None) -> None:
        self._capture = AudioCapture(device_index=device_index)
        self._capture.open()
        self._active = True

    def stop_capture(self) -> None:
        """Signal the capture loop to stop. Does NOT close the PyAudio stream."""
        self._active = False

    def close_stream(self) -> None:
        """Close the PyAudio stream. Must only be called after the capture loop task has finished."""
        if self._capture:
            self._capture.close()
            self._capture = None

    def close_mic(self) -> None:
        """Legacy: stop + close in one call. Safe only when no capture thread is running."""
        self.stop_capture()
        self.close_stream()

    def is_active(self) -> bool:
        return self._active

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
                language="en",
                beam_size=1,
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
