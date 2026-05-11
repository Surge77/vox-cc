import sys
import os
import json
import logging
import socket
import asyncio
import warnings

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from contextlib import asynccontextmanager

# Suppress HF hub symlink warning on Windows without Developer Mode (files download fine without symlinks)
warnings.filterwarnings("ignore", message=".*symlinks.*", category=UserWarning)

import uvicorn
from fastapi import FastAPI

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "models")
DATA_DIR = os.path.join(os.path.expanduser("~"), ".vox", "data")

# Module-level state — routers import these at call time to avoid circular imports
_load_plan: dict = {"turbo": "cpu", "final_pass": "skip", "llm": "cpu"}
_vram_free_mb: int = 0
_cuda_available: bool = False
_models_state: dict = {"turbo": False, "final_pass": False, "llm": False}
_session_active: bool = False
_training_active: bool = False
_turbo_model_ref: list = [None]  # list so M3 distil_sequential can null it in place
_llm_ref: list = [None]


def get_vram_free_mb() -> int:
    try:
        import torch
        if not torch.cuda.is_available():
            return 0
        free, _ = torch.cuda.mem_get_info()
        return free // (1024 * 1024)
    except Exception:
        return 0


def determine_load_plan(vram_mb: int) -> dict:
    if vram_mb >= 7500:
        return {"turbo": "cuda", "final_pass": "canary_cuda", "llm": "cuda"}
    elif vram_mb >= 5000:
        return {"turbo": "cuda", "final_pass": "canary_cpu", "llm": "cuda"}
    elif vram_mb >= 3000:
        return {"turbo": "cuda", "final_pass": "distil_sequential", "llm": "cuda"}
    else:
        return {"turbo": "cpu", "final_pass": "skip", "llm": "cpu"}


def find_free_port() -> int:
    for port in range(8000, 8010):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range 8000-8009")


def write_port_lock(port: int) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = os.path.join(DATA_DIR, "port.lock")
    with open(lock_path, "w") as f:
        f.write(str(port))


def read_port_lock() -> int | None:
    try:
        with open(os.path.join(DATA_DIR, "port.lock")) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def startup() -> None:
    global _load_plan, _vram_free_mb, _cuda_available, _models_state, _turbo_model_ref, _llm_ref

    try:
        import torch
        _cuda_available = torch.cuda.is_available()
    except ImportError:
        _cuda_available = False

    _vram_free_mb = get_vram_free_mb()
    _load_plan = determine_load_plan(_vram_free_mb)

    print(f"CUDA: {_cuda_available}, VRAM free: {_vram_free_mb}MB, load plan: {_load_plan}")

    try:
        from audio.capture import check_mic_permission
        check_mic_permission()
    except RuntimeError as e:
        print(f"WARNING: {e}", file=sys.stderr)

    try:
        import numpy as np
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download

        # HF repo has safetensors format; faster-whisper needs CTranslate2 (model.bin).
        # Download to staging dir then convert with ct2-transformers-converter on first run.
        turbo_hf = os.path.join(MODEL_DIR, "whisper-large-v3-turbo")
        turbo_ct2 = os.path.join(MODEL_DIR, "whisper-large-v3-turbo-ct2")

        if not os.path.isfile(os.path.join(turbo_ct2, "model.bin")):
            if not os.path.isfile(os.path.join(turbo_hf, "model.safetensors")):
                print("Downloading whisper-large-v3-turbo (first run)...")
                snapshot_download(
                    repo_id="openai/whisper-large-v3-turbo",
                    local_dir=turbo_hf,
                )
                print("Download complete.")
            print("Converting to CTranslate2 INT8...")
            import subprocess
            ct2_exe = os.path.join(
                os.path.dirname(sys.executable),
                "ct2-transformers-converter.exe",
            )
            subprocess.run(
                [ct2_exe, "--model", turbo_hf, "--output_dir", turbo_ct2,
                 "--quantization", "int8", "--force"],
                check=True,
            )
            print("Conversion complete.")
            import shutil
            shutil.copy2(os.path.join(turbo_hf, "preprocessor_config.json"), os.path.join(turbo_ct2, "preprocessor_config.json"))

        turbo = WhisperModel(
            turbo_ct2,
            device=_load_plan["turbo"],
            compute_type="int8" if _load_plan["turbo"] == "cuda" else "float32",
        )
        _turbo_model_ref[0] = turbo
        _models_state["turbo"] = True
        print("Turbo loaded.")

        # Use low-amplitude noise so CUDA encoder actually runs (zeros skip CUDA ops)
        dummy = (np.random.randn(16000) * 0.05).astype(np.float32)
        list(turbo.transcribe(dummy, language="en", beam_size=1))
        print("Turbo prewarmed.")
    except Exception as e:
        print(f"WARNING: Turbo load failed: {e}", file=sys.stderr)
        _models_state["turbo"] = False

    if _load_plan.get("final_pass") == "distil_sequential":
        try:
            from models.dual_loader import load_distil
            import gc
            print("Verifying distil-large-v3 (may download on first run)...")
            distil_check = load_distil()
            del distil_check
            gc.collect()
            import torch as _torch
            _torch.cuda.empty_cache()
            _models_state["final_pass"] = True
            print("distil-large-v3 verified.")
        except Exception as e:
            print(f"WARNING: distil-large-v3 unavailable: {e}", file=sys.stderr)
            _models_state["final_pass"] = False

    if _load_plan.get("llm") in ("cuda", "cpu"):
        try:
            from models.llm_engine import load_llm, prewarm_llm
            llm_path = os.path.join(MODEL_DIR, "qwen2.5-3b-instruct-q4_k_m.gguf")
            if os.path.isfile(llm_path):
                print("Loading LLM...")
                _llm_ref[0] = load_llm()
                _models_state["llm"] = True
                print("LLM loaded. Prewarming...")
                prewarm_llm(_llm_ref[0])
                print("LLM prewarmed.")
            else:
                print(f"WARNING: LLM model not found at {llm_path}", file=sys.stderr)
                _models_state["llm"] = False
        except Exception as e:
            print(f"WARNING: LLM load failed: {e}", file=sys.stderr)
            _models_state["llm"] = False


def cleanup() -> None:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(startup)
    yield
    cleanup()


app = FastAPI(lifespan=lifespan)

from routers.health import router as health_router
from routers.dictation import router as dictation_router
from routers.text_processing import router as text_processing_router
from routers.vocabulary import router as vocabulary_router
from routers.finetuning import router as finetuning_router
app.include_router(health_router)
app.include_router(dictation_router)
app.include_router(text_processing_router)
app.include_router(vocabulary_router)
app.include_router(finetuning_router)


if __name__ == "__main__":
    port = find_free_port()
    write_port_lock(port)
    print(f"Starting sidecar on port {port}")
    uvicorn.run("main:app", host="127.0.0.1", port=port, log_level="warning")
