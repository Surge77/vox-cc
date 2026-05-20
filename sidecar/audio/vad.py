import gzip
import numpy as np
import noisereduce as nr
import torch
import logging

logger = logging.getLogger(__name__)

# --- Thresholds ---
# RMS floor: if the signal is below this, it's truly digital silence (mic off / muted)
# Only used as a fast-path skip before running Silero VAD
DIGITAL_SILENCE_RMS = 0.0005
NOISE_RMS_FLOOR = 0.002

# Silero VAD speech probability threshold (0.0–1.0)
# Lowered from 0.35 to 0.25 to catch quieter speech that was previously gated out.
# The VAD still rejects true silence (rms < DIGITAL_SILENCE_RMS) before Silero runs.
SILERO_THRESHOLD = 0.25

HALLUCINATION_PHRASES = {
    "thank you", "thanks for watching", "you", ".", "...",
    "♪", "♫", "bye", "bye bye", "subscribe", "like and subscribe",
}


# ---------------------------------------------------------------------------
# Silero VAD — lazy-loaded singleton
# ---------------------------------------------------------------------------
_silero_model = None
_silero_utils = None      # tuple of helper fns from torch.hub return
_silero_available = None  # None = not yet checked


def _load_silero():
    """Load Silero VAD model once. Returns (model, True) or (None, False).
    Also populates _silero_utils with the helper tuple (get_speech_timestamps, …)
    so vad_trim_audio can use the batched timestamp utility."""
    global _silero_model, _silero_utils, _silero_available
    if _silero_available is not None:
        return _silero_model, _silero_available
    try:
        model, utils = torch.hub.load(
            "snakers4/silero-vad",
            "silero_vad",
            trust_repo=True,
        )
        model.eval()
        _silero_model = model
        _silero_utils = utils
        _silero_available = True
        logger.info("Silero VAD loaded successfully")
        return model, True
    except Exception as e:
        logger.warning(f"Silero VAD unavailable, falling back to RMS gate: {e}")
        _silero_model = None
        _silero_utils = None
        _silero_available = False
        return None, False


def _silero_has_speech(audio_chunk: np.ndarray, sample_rate: int = 16000) -> bool:
    """Run Silero VAD on an audio chunk. Splits into 512-sample frames and
    returns True if ANY frame exceeds the speech probability threshold.
    This makes the detector sensitive to speech that starts partway through
    a larger chunk (unlike a single RMS average over the whole window)."""
    model, available = _load_silero()
    if not available:
        # Fallback: lenient RMS gate (much lower than the old 0.005)
        rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
        return rms > 0.002

    FRAME_SIZE = 512  # 32ms at 16kHz — Silero's optimal window
    tensor = torch.from_numpy(audio_chunk).float()

    # Check frames across the chunk; short-circuit on first speech detection
    for start in range(0, len(tensor) - FRAME_SIZE + 1, FRAME_SIZE):
        frame = tensor[start : start + FRAME_SIZE]
        prob = model(frame.unsqueeze(0), sample_rate).item()
        if prob > SILERO_THRESHOLD:
            return True

    return False


# ---------------------------------------------------------------------------
# AGC — Automatic Gain Control
# ---------------------------------------------------------------------------
def apply_agc(audio: np.ndarray, target_rms: float = 0.12) -> np.ndarray:
    """Normalize audio to a target RMS level. Prevents quiet mics from
    producing sub-threshold signals that get dropped by the VAD.
    - target_rms=0.12 maps to roughly -18 dBFS — louder than the old 0.08
      so Whisper receives a stronger signal from quiet microphones
    - Gain is capped at +32 dB (40x) to avoid amplifying pure background noise
    - Signals below digital silence floor are returned unchanged (no point amplifying zeros)
    """
    current_rms = float(np.sqrt(np.mean(audio ** 2)))
    if current_rms < DIGITAL_SILENCE_RMS:
        return audio  # truly silent — no point amplifying
    gain = target_rms / current_rms
    gain = min(gain, 40.0)  # cap at ~32 dB — handles very quiet mics (rms ~0.003)
    return (audio * gain).clip(-1.0, 1.0)   # Soft-clip to prevent hard clipping artifacts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def should_transcribe(audio_chunk: np.ndarray) -> bool:
    """Determine if an audio chunk contains speech worth transcribing.
    Uses Silero VAD (neural) with RMS fast-path for digital silence."""
    # Fast path: skip truly silent audio (mic muted / disconnected)
    rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
    if rms < DIGITAL_SILENCE_RMS:
        return False
    return _silero_has_speech(audio_chunk)


def is_hallucination(transcript: str, audio_duration_s: float) -> bool:
    normalized = transcript.strip().lower()
    stripped = normalized.rstrip(".")
    if normalized in HALLUCINATION_PHRASES or stripped in HALLUCINATION_PHRASES:
        return True
    if audio_duration_s > 3.0:
        word_count = len(transcript.split())
        min_expected = audio_duration_s * 0.3
        if word_count < min_expected:
            return True
    # Compression ratio test — matches faster-whisper's own hallucination guard.
    # Hallucinated text is repetitive/looping and compresses far more than real speech.
    # Ratio = uncompressed / compressed; threshold of 2.4 is faster-whisper's default.
    # Require >= 50 chars so gzip's ~18-byte header doesn't dominate the result.
    if len(transcript) >= 50:
        encoded = transcript.encode("utf-8")
        ratio = len(encoded) / len(gzip.compress(encoded))
        if ratio > 2.4:
            return True
    return False


def prewarm_silero() -> None:
    """Pre-load Silero VAD during startup so the first recording doesn't stall 2-3s on model load."""
    _load_silero()


def suppress_noise(audio: np.ndarray, rate: int) -> np.ndarray:
    """Apply noise reduction. Uses a gentler setting than before (0.5 vs 0.75)
    to avoid destroying speech harmonics and sibilants."""
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms <= NOISE_RMS_FLOOR:
        return audio
    return nr.reduce_noise(
        y=audio,
        sr=rate,
        stationary=False,
        prop_decrease=0.5,  # was 0.75 — too aggressive, destroyed speech
    ).astype(np.float32)


def vad_trim_audio(audio: np.ndarray, sample_rate: int = 16000, padding_ms: int = 120) -> np.ndarray:
    """Strip silences from session audio before final-pass transcription.
    Uses Silero VAD to find speech regions, concatenates them with `padding_ms`
    on each end. A typical 8-second session with 3-4s of silence shrinks to
    4-5s of audio — distil transcribes ~40% faster with no accuracy loss.

    Falls back to the original audio on any error (never returns empty)."""
    if len(audio) < 1600:  # < 100ms — not worth trimming
        return audio
    model, available = _load_silero()
    if not available or _silero_utils is None:
        return audio
    try:
        get_speech_timestamps = _silero_utils[0]
        tensor = torch.from_numpy(audio).float()
        # min_speech_duration_ms=80 catches short words; speech_pad_ms is
        # baked-in padding (additional to our concat padding below).
        timestamps = get_speech_timestamps(
            tensor,
            model,
            sampling_rate=sample_rate,
            min_speech_duration_ms=80,
            min_silence_duration_ms=200,
            speech_pad_ms=30,
            threshold=0.4,
        )
        if not timestamps:
            return audio  # Silero found no speech — let distil decide
        pad = int(padding_ms * sample_rate / 1000)
        parts: list[np.ndarray] = []
        for ts in timestamps:
            s = max(0, int(ts["start"]) - pad)
            e = min(len(audio), int(ts["end"]) + pad)
            if e > s:
                parts.append(audio[s:e])
        if not parts:
            return audio
        return np.concatenate(parts).astype(np.float32)
    except Exception:
        logger.exception("vad_trim_audio failed — using original audio")
        return audio


# ---------------------------------------------------------------------------
# Auto-stop silence detector — stateful, pure-Python.
# Fed one chunk at a time from the capture loop; reports when post-speech
# silence has lasted longer than the configured threshold.
# ---------------------------------------------------------------------------
class SilenceDetector:
    """Tracks rolling post-speech silence over streamed audio chunks.

    Designed for use by `routers/dictation._capture_loop` to implement
    auto-stop-on-silence: ignore leading silence (user hasn't spoken yet),
    arm after the first speech-level chunk, then count contiguous silent
    samples until threshold is exceeded.

    Pure logic — no I/O. Sample rate fixed at 16000 to match the pipeline."""

    def __init__(
        self,
        threshold_ms: int,
        floor_rms: float = NOISE_RMS_FLOOR,
        sample_rate: int = 16000,
    ):
        self.threshold_samples = max(1, int(threshold_ms * sample_rate / 1000))
        self.floor_rms = floor_rms
        self._speech_seen = False
        self._silence_samples = 0

    def feed(self, chunk: np.ndarray) -> bool:
        """Returns True iff post-speech silence has exceeded threshold."""
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        if rms >= self.floor_rms:
            self._speech_seen = True
            self._silence_samples = 0
            return False
        if not self._speech_seen:
            # Still waiting for the user to start speaking
            return False
        self._silence_samples += len(chunk)
        return self._silence_samples >= self.threshold_samples

    def reset(self) -> None:
        self._speech_seen = False
        self._silence_samples = 0
