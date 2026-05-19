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
# 0.5 is the model's recommended default; lower = more sensitive
SILERO_THRESHOLD = 0.35

HALLUCINATION_PHRASES = {
    "thank you", "thanks for watching", "you", ".", "...",
    "♪", "♫", "bye", "bye bye", "subscribe", "like and subscribe",
}


# ---------------------------------------------------------------------------
# Silero VAD — lazy-loaded singleton
# ---------------------------------------------------------------------------
_silero_model = None
_silero_available = None  # None = not yet checked


def _load_silero():
    """Load Silero VAD model once. Returns (model, True) or (None, False)."""
    global _silero_model, _silero_available
    if _silero_available is not None:
        return _silero_model, _silero_available
    try:
        model, _ = torch.hub.load(
            "snakers4/silero-vad",
            "silero_vad",
            trust_repo=True,
        )
        model.eval()
        _silero_model = model
        _silero_available = True
        logger.info("Silero VAD loaded successfully")
        return model, True
    except Exception as e:
        logger.warning(f"Silero VAD unavailable, falling back to RMS gate: {e}")
        _silero_model = None
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
def apply_agc(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    """Normalize audio to a target RMS level. Prevents quiet mics from
    producing sub-threshold signals that get dropped by the VAD.
    - target_rms=0.08 maps to roughly -22 dBFS, comfortable for Whisper
    - Gain is capped at +26 dB (20x) to avoid amplifying pure background noise
    - Signals below digital silence floor are returned unchanged (no point amplifying zeros)
    """
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 1e-7:
        return audio  # true silence — don't amplify
    gain = target_rms / rms
    gain = min(gain, 20.0)  # cap at ~26 dB
    result = audio * gain
    # Soft-clip to prevent hard clipping artifacts
    result = np.tanh(result).astype(np.float32)
    return result


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
