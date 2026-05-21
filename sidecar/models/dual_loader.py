import gc
import logging
import os
import sys
import numpy as np

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)  # dist/sidecar/ — models/ is sibling to exe
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.environ.get("VOX_MODEL_DIR") or os.path.join(BASE, "models")

DISTIL_MODEL_ID = "Systran/faster-distil-whisper-large-v3"


def _get_load_plan() -> dict:
    import main as state
    return state._load_plan


def _get_vocab_prompt() -> str:
    from audio.pipeline import load_vocabulary_prompt
    return load_vocabulary_prompt()


def load_distil():
    from faster_whisper import WhisperModel
    # Prefer fine-tuned model if produced by /finetune/start
    lora_ct2 = os.path.join(MODEL_DIR, "distil-lora-ct2")
    if os.path.isfile(os.path.join(lora_ct2, "model.bin")):
        return WhisperModel(lora_ct2, device="cuda", compute_type="int8")
    return WhisperModel(
        DISTIL_MODEL_ID,
        device="cuda",
        compute_type="int8",
    )


def _peak_normalize(audio: np.ndarray, target_peak: float = 0.6) -> np.ndarray:
    """Peak-normalize quiet audio to target_peak. Uses PEAK not RMS — silences
    don't pull the gain factor down (the bug that caused old AGC to hard-clip).
    Skips work when audio is already at/above target. Caps gain at 8x so pure
    silence doesn't get amplified into noise."""
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak >= target_peak or peak < 0.001:
        return audio
    gain = min(target_peak / peak, 8.0)
    return (audio * gain).astype(np.float32)


def _pre_emphasis(audio: np.ndarray, alpha: float = 0.97) -> np.ndarray:
    """Standard pre-emphasis high-pass filter: boosts 2-8kHz range where
    consonants live. Helps Whisper on quiet mics where fricatives are
    barely audible. y[n] = x[n] - alpha * x[n-1]."""
    if len(audio) < 2:
        return audio
    out = np.empty_like(audio)
    out[0] = audio[0]
    out[1:] = audio[1:] - alpha * audio[:-1]
    return out


def run_distil_final_pass(audio_np: np.ndarray, turbo_model_ref: list) -> tuple[str, float]:
    """
    Sequential swap: null Turbo ref, load distil, transcribe full session audio, unload distil.
    turbo_model_ref[0] stays None after return — begin_stream calls reload_turbo_if_needed.
    Returns (text, avg_logprob) — avg_logprob lets the caller flag low-confidence transcripts.

    Latency: beam_size=3 (was 5) → ~500-1000 ms decode savings at ~0.3 pt WER
    cost. beam=1 forbidden (phonetic errors documented); beam=3 keeps
    multi-hypothesis search.
    """
    import torch
    from audio.vad import is_hallucination, vad_trim_audio
    result = ""
    avg_logprob = 0.0
    distil = None
    try:
        turbo_model_ref[0] = None
        gc.collect()
        torch.cuda.empty_cache()

        distil = load_distil()
        trimmed = vad_trim_audio(audio_np)

        original_s = len(audio_np) / 16000
        trimmed_s = len(trimmed) / 16000
        if trimmed_s < original_s * 0.9:
            logger.info("VAD-trim: %.2fs → %.2fs (%.0f%% saved)",
                        original_s, trimmed_s, 100 * (1 - trimmed_s / max(original_s, 0.01)))
        # Peak-normalize (gentle, replaces old broken AGC) then pre-emphasis
        # (boost consonant range). Both help the quiet Intel SST signal without
        # the session-RMS clipping bug the old AGC had.
        audio_feed = _pre_emphasis(_peak_normalize(trimmed))

        vocab_prompt = _get_vocab_prompt()
        # Multi-language: same setting as the Turbo streaming path. None means
        # Whisper auto-detects per session.
        from audio.pipeline import load_language_setting
        segments, _ = distil.transcribe(
            audio_feed,
            language=load_language_setting(),
            beam_size=3,                       # beam=5→3: ~30% decode savings, ~0.3pt WER cost
            condition_on_previous_text=False,
            word_timestamps=True,
            initial_prompt=vocab_prompt or None,
        )
        parts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            text = seg.text.strip()
            if text and not is_hallucination(text, seg.end - seg.start):
                parts.append(text)
                if getattr(seg, "avg_logprob", None) is not None:
                    logprobs.append(seg.avg_logprob)
        result = " ".join(parts)
        avg_logprob = float(np.mean(logprobs)) if logprobs else 0.0
        logger.info(
            "distil final pass: %d chars, audio %.1fs, avg_logprob=%.3f",
            len(result), len(audio_np) / 16000, avg_logprob,
        )

    except Exception:
        logger.exception("distil final pass failed — falling back to Turbo text")
        result = ""

    finally:
        if distil is not None:
            del distil
        gc.collect()
        try:
            import torch as _t
            _t.cuda.empty_cache()
        except Exception:
            pass

    return result, avg_logprob


def reload_turbo_if_needed(turbo_model_ref: list) -> None:
    """Lazy Turbo reload — called at begin_stream if distil_sequential unloaded it."""
    if turbo_model_ref[0] is not None:
        return
    from faster_whisper import WhisperModel
    load_plan = _get_load_plan()
    turbo_model_ref[0] = WhisperModel(
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        device=load_plan["turbo"],
        compute_type="int8" if load_plan["turbo"] == "cuda" else "float32",
    )
