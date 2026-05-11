import numpy as np
import noisereduce as nr

SILENCE_RMS_THRESHOLD = 0.01
NOISE_RMS_FLOOR = 0.005

HALLUCINATION_PHRASES = {
    "thank you", "thanks for watching", "you", ".", "...",
    "♪", "♫", "bye", "bye bye", "subscribe", "like and subscribe",
}


def should_transcribe(audio_chunk: np.ndarray) -> bool:
    rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
    return rms > SILENCE_RMS_THRESHOLD


def is_hallucination(transcript: str, audio_duration_s: float) -> bool:
    stripped = transcript.strip().lower().rstrip(".")
    if stripped in HALLUCINATION_PHRASES:
        return True
    if audio_duration_s > 3.0:
        word_count = len(transcript.split())
        min_expected = audio_duration_s * 0.3
        if word_count < min_expected:
            return True
    return False


def suppress_noise(audio: np.ndarray, rate: int) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms <= NOISE_RMS_FLOOR:
        return audio
    return nr.reduce_noise(
        y=audio,
        sr=rate,
        stationary=False,
        prop_decrease=0.75,
    ).astype(np.float32)
