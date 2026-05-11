import asyncio
import json
import os
import numpy as np

from audio.capture import AudioCapture, SAMPLE_RATE
from audio.vad import should_transcribe, is_hallucination, suppress_noise

CHUNK_MS = 1000
OVERLAP_MS = 200
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


class DictationSession:
    def __init__(self, turbo_model_ref: list, load_plan: dict):
        self._turbo_ref = turbo_model_ref
        self._load_plan = load_plan
        self._ring: list[np.ndarray] = []      # raw chunks; used for M3 full-session audio + overlap
        self._session_words: list[str] = []    # accumulated words across all chunks
        self._last_valid_ts: float = 0.0
        self._capture: AudioCapture | None = None
        self._active = False

    def open_mic(self, device_index: int | None = None) -> None:
        self._capture = AudioCapture(device_index=device_index)
        self._capture.open()
        self._active = True

    def close_mic(self) -> None:
        self._active = False
        if self._capture:
            self._capture.close()
            self._capture = None

    def is_active(self) -> bool:
        return self._active

    async def process_chunk(self, chunk: np.ndarray, ws) -> bool:
        """
        Process one audio chunk. Returns True if session should auto-terminate (60s cap).
        ring stores raw audio for M3 final-pass faithfulness; Turbo gets noise-suppressed feed.
        """
        if not should_transcribe(chunk):
            return False

        # overlap from previous raw chunk tail (get before appending current)
        overlap = self._ring[-1][-OVERLAP_SAMPLES:] if self._ring else np.array([], dtype=np.float32)

        # store raw chunk in ring
        self._ring.append(chunk)

        total = sum(len(c) for c in self._ring)
        if total >= MAX_SAMPLES:
            return True

        turbo = self._turbo_ref[0]
        if turbo is None:
            return False

        # build feed: overlap + current, then suppress noise on full feed
        feed = np.concatenate([overlap, chunk]) if len(overlap) else chunk.copy()
        feed = suppress_noise(feed, SAMPLE_RATE)

        vocab_prompt = load_vocabulary_prompt()
        try:
            segments, _ = turbo.transcribe(
                feed,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                language="en",
                beam_size=1,
                initial_prompt=vocab_prompt or None,
            )
            segments = list(segments)
        except Exception:
            return False

        # Per-chunk timestamp filter:
        # - First chunk (no overlap): accept from 0.0
        # - Subsequent chunks: skip overlap region (first 0.2s of feed are old audio)
        # Cross-chunk last_valid_ts is NOT used here — timestamps reset each feed.
        OVERLAP_DUR = OVERLAP_SAMPLES / SAMPLE_RATE  # 0.2s
        filter_ts = OVERLAP_DUR if len(self._ring) > 1 else 0.0

        for seg in segments:
            for word in (seg.words or []):
                if word.start >= filter_ts:
                    w = word.word.strip()
                    if w and not is_hallucination(w, word.end - word.start):
                        self._session_words.append(w)
                        filter_ts = word.end  # deduplicate within this chunk only

        if self._session_words:
            await ws.send_json({
                "type": "partial_update",
                "content": " ".join(self._session_words),
            })

        return False

    def build_turbo_fallback(self) -> str:
        return " ".join(self._session_words)

    def get_full_audio(self) -> np.ndarray:
        """Concatenate raw ring buffer. Called by M3 final-pass logic."""
        if not self._ring:
            return np.array([], dtype=np.float32)
        return np.concatenate(self._ring).astype(np.float32)

    def reset(self) -> None:
        self._ring.clear()
        self._session_words.clear()
        self._last_valid_ts = 0.0
