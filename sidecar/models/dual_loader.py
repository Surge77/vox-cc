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


def run_distil_final_pass(audio_np: np.ndarray, turbo_model_ref: list) -> str:
    """
    Sequential swap: null Turbo ref, load distil, transcribe full session audio, unload distil.
    turbo_model_ref[0] stays None after return — begin_stream calls reload_turbo_if_needed.
    """
    import torch
    result = ""
    distil = None
    try:
        turbo_model_ref[0] = None
        gc.collect()
        torch.cuda.empty_cache()

        distil = load_distil()

        # DEBUG: AGC bypass test — peak ~0.12, AGC 13x gain causes hard clipping at 1.0.
        # distil-large-v3 handles quiet audio natively; clipped audio = garbage transcription.
        from audio.vad import is_hallucination
        audio_feed = audio_np

        vocab_prompt = _get_vocab_prompt()
        segments, _ = distil.transcribe(
            audio_feed,
            language="en",
            beam_size=5,                       # spec value — greedy beam=1 caused phonetic errors (fox→fog/bronze)
            condition_on_previous_text=False,
            word_timestamps=True,
            initial_prompt=vocab_prompt or None,
        )
        parts = []
        for seg in segments:
            text = seg.text.strip()
            if text and not is_hallucination(text, seg.end - seg.start):
                parts.append(text)
        result = " ".join(parts)
        logger.info("distil final pass: %d chars, audio %.1fs", len(result), len(audio_np) / 16000)

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

    return result


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
