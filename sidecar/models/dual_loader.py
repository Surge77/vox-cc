import gc
import os
import sys
import numpy as np

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.path.join(BASE, "models")

DISTIL_MODEL_ID = "Systran/faster-distil-whisper-large-v3"


def _get_load_plan() -> dict:
    import main as state
    return state._load_plan


def _get_vocab_prompt() -> str:
    from audio.pipeline import load_vocabulary_prompt
    return load_vocabulary_prompt()


def load_distil():
    from faster_whisper import WhisperModel
    return WhisperModel(
        DISTIL_MODEL_ID,
        device="cuda",
        compute_type="int8",
        download_root=MODEL_DIR,
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

        vocab_prompt = _get_vocab_prompt()
        segments, _ = distil.transcribe(
            audio_np,
            language="en",
            beam_size=5,
            word_timestamps=False,
            initial_prompt=vocab_prompt or None,
        )
        result = " ".join(seg.text.strip() for seg in segments)

    except Exception:
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
        os.path.join(MODEL_DIR, "whisper-large-v3-turbo-ct2"),
        device=load_plan["turbo"],
        compute_type="int8" if load_plan["turbo"] == "cuda" else "float32",
    )
