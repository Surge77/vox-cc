import asyncio
import json
import os
import sys
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


# --- toggle-collection ---

class ToggleCollectionRequest(BaseModel):
    enabled: bool


@router.post("/finetune/toggle-collection")
async def toggle_collection(req: ToggleCollectionRequest):
    import main as state
    settings = _load_settings(state.DATA_DIR)
    settings["passive_collection_enabled"] = req.enabled
    _save_settings(state.DATA_DIR, settings)
    return {"ok": True, "enabled": req.enabled}


def _load_settings(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, "settings.json")) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"passive_collection_enabled": False}


def _save_settings(data_dir: str, settings: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "settings.json"), "w") as f:
        json.dump(settings, f)


# --- /finetune/start ---

class FinetuneStartRequest(BaseModel):
    epochs: int = 3
    learning_rate: float = 3e-4


@router.post("/finetune/start")
async def finetune_start(req: FinetuneStartRequest):
    import main as state
    if state._session_active:
        raise HTTPException(status_code=409, detail="Dictation session active")
    if state._training_active:
        raise HTTPException(status_code=409, detail="Training already running")

    sample_count = _count_training_samples(state.DATA_DIR)
    if sample_count < 50:
        raise HTTPException(status_code=400, detail=f"Need 50+ samples with audio, have {sample_count}")

    job_id = str(uuid.uuid4())
    state._training_active = True

    _unload_for_training(state)
    _write_progress(state.DATA_DIR, "running", 0.0, 0, req.epochs, sample_count)

    asyncio.ensure_future(_run_training_task(state, req.epochs, req.learning_rate, sample_count))
    return {"job_id": job_id, "status": "started"}


async def _run_training_task(state, epochs: int, lr: float, sample_count: int) -> None:
    train_script = os.path.join(os.path.dirname(__file__), "..", "training", "train.py")
    output_dir = os.path.join(state.DATA_DIR, "training_output")
    os.makedirs(output_dir, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, train_script,
            "--data-dir", state.DATA_DIR,
            "--model-dir", state.MODEL_DIR,
            "--epochs", str(epochs),
            "--lr", str(lr),
            "--output-dir", output_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
        if proc.returncode != 0:
            _write_progress(state.DATA_DIR, "error", 0.0, 0, epochs, sample_count,
                            "Training process failed")
    except Exception as e:
        _write_progress(state.DATA_DIR, "error", 0.0, 0, epochs, sample_count, str(e))
    finally:
        state._training_active = False
        await asyncio.to_thread(_reload_after_training, state)


def _unload_for_training(state) -> None:
    import gc
    try:
        import torch
        state._llm_ref[0] = None
        state._models_state["llm"] = False
        state._turbo_model_ref[0] = None
        state._models_state["turbo"] = False
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass


def _reload_after_training(state) -> None:
    import gc
    try:
        from models.dual_loader import reload_turbo_if_needed
        reload_turbo_if_needed(state._turbo_model_ref)
        state._models_state["turbo"] = state._turbo_model_ref[0] is not None
    except Exception as e:
        print(f"WARNING: Turbo reload after training failed: {e}", file=sys.stderr)

    try:
        from models.llm_engine import load_llm, prewarm_llm
        llm_path = os.path.join(state.MODEL_DIR, "qwen2.5-3b-instruct-q4_k_m.gguf")
        if os.path.isfile(llm_path):
            state._llm_ref[0] = load_llm()
            state._models_state["llm"] = True
            prewarm_llm(state._llm_ref[0])
    except Exception as e:
        print(f"WARNING: LLM reload after training failed: {e}", file=sys.stderr)
        state._models_state["llm"] = False


def _count_training_samples(data_dir: str) -> int:
    log_path = os.path.join(data_dir, "passive_log.jsonl")
    try:
        count = 0
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if "audio_file" in entry:
                        audio_path = os.path.join(data_dir, "audio_clips", entry["audio_file"])
                        if os.path.isfile(audio_path):
                            count += 1
                except json.JSONDecodeError:
                    continue
        return count
    except FileNotFoundError:
        return 0


def _write_progress(data_dir: str, status: str, progress: float, epoch: int,
                    total_epochs: int, samples: int, error: str | None = None) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "finetune_progress.json"), "w") as f:
        json.dump({
            "status": status,
            "progress": progress,
            "epoch": epoch,
            "total_epochs": total_epochs,
            "samples": samples,
            "error": error,
        }, f)


# --- /finetune/status ---

@router.get("/finetune/status")
async def finetune_status():
    import main as state
    sample_count = _count_training_samples(state.DATA_DIR)
    progress_path = os.path.join(state.DATA_DIR, "finetune_progress.json")
    try:
        with open(progress_path) as f:
            data = json.load(f)
        data["samples"] = sample_count
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "status": "idle",
            "progress": 0.0,
            "epoch": 0,
            "total_epochs": 0,
            "samples": sample_count,
            "error": None,
        }


# --- /finetune/groq-test ---

class GroqTestRequest(BaseModel):
    api_key: str
    raw_transcript: str = "hello world"


@router.post("/finetune/groq-test")
async def groq_test(req: GroqTestRequest):
    import main as state
    key_path = os.path.join(state.DATA_DIR, "groq_key.txt")
    os.makedirs(state.DATA_DIR, exist_ok=True)
    with open(key_path, "w") as f:
        f.write(req.api_key.strip())
    try:
        from models.llm_engine import run_groq
        from models.prompt_router import get_system_prompt
        system = get_system_prompt(None)
        result = await asyncio.wait_for(
            asyncio.to_thread(run_groq, req.api_key.strip(), system, req.raw_transcript),
            timeout=15.0,
        )
        return {"cleaned_text": result, "ok": True}
    except Exception as e:
        return {"cleaned_text": "", "ok": False, "error": str(e)}
