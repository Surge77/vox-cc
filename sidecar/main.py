import sys
import os
import json
import logging
import signal
import socket
import asyncio
import warnings

# Fix 6: CUDA unified memory — allows Turbo + LLM to share VRAM gracefully.
# When VRAM fills up, CUDA automatically pages tensors to system RAM over PCIe
# instead of crashing with OOM. Must be set before any torch/CUDA import.
os.environ.setdefault("GGML_CUDA_ENABLE_UNIFIED_MEMORY", "1")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from contextlib import asynccontextmanager

# Suppress HF hub symlink warning on Windows without Developer Mode (files download fine without symlinks)
warnings.filterwarnings("ignore", message=".*symlinks.*", category=UserWarning)

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)  # dist/sidecar/ — models/ is sibling to exe, not inside _internal/
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("VOX_MODEL_DIR") or os.path.join(BASE, "models")
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
_llm_loading: bool = False       # True while LLM loads in background; health reports llm:true during this window
_pending_audio_uuid: str | None = None  # set by terminate_stream when collection on; cleared by /process-text
_pending_latencies: dict | None = None  # set by terminate_stream; merged into passive_log by /process-text
_mic_ref: list = [None]  # PersistentMicCapture | None — opened once at lifespan, reused across sessions


PID_LOCK = os.path.join(os.path.expanduser("~"), ".vox", "data", "sidecar.pid")


def kill_previous_sidecar() -> None:
    """Kill any previous sidecar holding VRAM. Must run before VRAM detection."""
    try:
        with open(PID_LOCK) as f:
            old_pid = int(f.read().strip())
        if old_pid == os.getpid():
            return
        try:
            os.kill(old_pid, signal.SIGTERM)
            print(f"Killed previous sidecar (PID {old_pid}) — waiting for VRAM release...")
            import time
            time.sleep(1.5)  # GPU driver needs ~1s to reclaim CUDA context after process dies
        except (ProcessLookupError, OSError):
            pass  # Already dead
    except (FileNotFoundError, ValueError):
        pass  # No previous PID file
    finally:
        os.makedirs(os.path.dirname(PID_LOCK), exist_ok=True)
        with open(PID_LOCK, "w") as f:
            f.write(str(os.getpid()))


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


def startup_critical() -> None:
    """Load Turbo + verify distil files exist. Returns quickly (~10-15s). LLM loads in background after."""
    global _load_plan, _vram_free_mb, _cuda_available, _models_state, _turbo_model_ref

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

        # Pre-built CTranslate2 model — no safetensors download or conversion needed.
        # Avoids ctranslate2 version/format mismatch from manual conversion.
        # Pre-built CT2 model in global HF cache — no download_root so HF default cache is used.
        TURBO_MODEL_ID = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        turbo = WhisperModel(
            TURBO_MODEL_ID,
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

    try:
        from audio.vad import prewarm_silero
        prewarm_silero()
        print("Silero VAD prewarmed.")
    except Exception as e:
        print(f"WARNING: Silero VAD prewarm failed: {e}", file=sys.stderr)

    if _load_plan.get("final_pass") == "skip":
        # Intentional — VRAM < 3000MB, Turbo-only mode. Not a failure.
        _models_state["final_pass"] = True

    elif _load_plan.get("final_pass") == "distil_sequential":
        # File-existence check only — no trial load. Loading 1.5GB just to verify files is wasteful.
        # load_distil() uses global HF cache (no download_root), so check there.
        # Do NOT check MODEL_DIR — stale local CT2 conversion may be incompatible with current ct2 version.
        try:
            from huggingface_hub import constants as _hf_c
            _hf_hub_cache = _hf_c.HF_HUB_CACHE
        except (ImportError, AttributeError):
            _hf_hub_cache = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
        distil_hf_dir = os.path.join(_hf_hub_cache, "models--Systran--faster-distil-whisper-large-v3")
        distil_lora = os.path.join(MODEL_DIR, "distil-lora-ct2")
        if os.path.isdir(distil_hf_dir) or os.path.isdir(distil_lora):
            _models_state["final_pass"] = True
            print("distil-large-v3 files verified.")
        else:
            # First run: pre-download so first terminate_stream doesn't block on download
            try:
                from models.dual_loader import load_distil
                import gc
                print("Downloading distil-large-v3 (first run)...")
                distil_check = load_distil()
                del distil_check
                gc.collect()
                import torch as _torch
                _torch.cuda.empty_cache()
                _models_state["final_pass"] = True
                print("distil-large-v3 downloaded.")
            except Exception as e:
                print(f"WARNING: distil-large-v3 unavailable: {e}", file=sys.stderr)
                _models_state["final_pass"] = False


def startup_llm() -> None:
    """Load LLM in background after Turbo is ready. Sets _llm_loading during this window."""
    global _models_state, _llm_ref, _llm_loading

    if _load_plan.get("llm") not in ("cuda", "cpu"):
        return

    _llm_loading = True
    try:
        from models.llm_engine import load_llm, prewarm_llm
        llm_path = os.path.join(MODEL_DIR, "qwen2.5-3b-instruct-q4_k_m.gguf")
        if os.path.isfile(llm_path):
            print("Loading LLM (background)...")
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
    finally:
        _llm_loading = False


def cleanup() -> None:
    mic = _mic_ref[0]
    if mic is not None:
        try:
            mic.stop()
        except Exception:
            pass
        _mic_ref[0] = None


def _cleanup_old_logs(data_dir: str) -> None:
    """Delete passive_log entries and audio clips older than retention_days.
    If retention_days is 0, passive collection is also disabled."""
    import json
    from datetime import datetime, timedelta, timezone
    settings_path = os.path.join(data_dir, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    retention_days = settings.get("retention_days")
    if retention_days is None:
        return
    if retention_days == 0:
        settings["passive_collection_enabled"] = False
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f)
        except Exception:
            pass
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    log_path = os.path.join(data_dir, "passive_log.jsonl")
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        kept = []
        for line in lines:
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts >= cutoff:
                    kept.append(line)
            except (ValueError, KeyError):
                kept.append(line)
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except FileNotFoundError:
        pass
    clips_dir = os.path.join(data_dir, "audio_clips")
    if os.path.isdir(clips_dir):
        for fname in os.listdir(clips_dir):
            fpath = os.path.join(clips_dir, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                if mtime < cutoff:
                    os.remove(fpath)
            except Exception:
                pass


def start_persistent_mic() -> None:
    """Open the mic once at sidecar startup and keep it open for the process
    lifetime. Subsequent sessions snapshot a pre-roll from this stream — this
    is what eliminates the ~2s WASAPI warmup latency that was silently
    chopping off the first words of every dictation."""
    global _mic_ref
    try:
        from audio.capture import AudioCapture, PersistentMicCapture
        import json
        settings: dict = {}
        try:
            with open(os.path.join(DATA_DIR, "settings.json")) as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        cap = AudioCapture(
            device_index=settings.get("audio_device_index"),
            exclusive_mode=settings.get("exclusive_mode", False),
            prefer_pyaudio=settings.get("prefer_pyaudio", False),
        )
        mic = PersistentMicCapture(cap, preroll_seconds=3.0)
        mic.start()
        _mic_ref[0] = mic
        print("Persistent mic started (pre-roll 3.0s).")
    except Exception as e:
        print(f"WARNING: Persistent mic failed to start: {e}", file=sys.stderr)
        _mic_ref[0] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _llm_loading
    # Critical path: Turbo + distil file check (~10-15s). Unblocks health endpoint.
    await asyncio.to_thread(startup_critical)
    # Persistent mic: open BEFORE LLM background load so it's warm by the
    # time the user can possibly press the hotkey.
    await asyncio.to_thread(start_persistent_mic)
    # Set flag BEFORE yield so the first health poll after startup sees llm:true.
    # Without this, a poll between yield and task-start sees llm=false → sidecar-degraded.
    _llm_loading = True
    asyncio.create_task(asyncio.to_thread(startup_llm))
    _cleanup_old_logs(DATA_DIR)
    yield
    cleanup()


app = FastAPI(lifespan=lifespan)

# CORS — localhost-only. Allows Vite dev (localhost:1420) and Tauri WebView2 origins.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|tauri://localhost|https?://tauri\.localhost)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers.health import router as health_router
from routers.dictation import router as dictation_router
from routers.text_processing import router as text_processing_router
from routers.vocabulary import router as vocabulary_router
from routers.finetuning import router as finetuning_router
from routers.snippets import router as snippets_router
from routers.replay import router as replay_router
app.include_router(health_router)
app.include_router(dictation_router)
app.include_router(text_processing_router)
app.include_router(vocabulary_router)
app.include_router(finetuning_router)
app.include_router(snippets_router)
app.include_router(replay_router)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--train-subprocess":
        # Frozen-build re-exec path: strip sentinel and dispatch to training.train.
        # In a venv (dev), finetuning.py calls train.py directly; this branch only
        # activates when sys.executable is the frozen sidecar.exe.
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from training import train  # bundled inside PYZ via hiddenimports
        train.main()
        sys.exit(0)
    kill_previous_sidecar()  # free VRAM before detecting it
    port = find_free_port()
    write_port_lock(port)
    print(f"Starting sidecar on port {port}")
    uvicorn.run("main:app", host="127.0.0.1", port=port, log_level="warning")
