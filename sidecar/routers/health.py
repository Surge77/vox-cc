import json
import os
import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel
from audio.capture import list_audio_devices, AudioCapture, SAMPLE_RATE

router = APIRouter()


@router.get("/health")
async def health():
    import main as app_state
    final_pass_type = app_state._load_plan.get("final_pass", "skip")
    models = dict(app_state._models_state)
    # Report llm:true while LLM is still loading in background — prevents premature sidecar-degraded
    if app_state._llm_loading:
        models["llm"] = True
    return {
        "status": "ok",
        "models": models,
        "final_pass_type": final_pass_type,
        "cuda": app_state._cuda_available,
        "vram_free_mb": app_state._vram_free_mb,
    }


@router.get("/audio/devices")
async def audio_devices():
    devices = list_audio_devices()
    return {"devices": devices}


@router.get("/audio/diagnose")
async def audio_diagnose():
    from audio.vad import should_transcribe
    capture = None
    try:
        devices = list_audio_devices()
        default_device = next((d for d in devices if d["default"]), None)
        device_name = default_device["name"] if default_device else "unknown"
        capture = AudioCapture()
        capture.open()
        samples: list[np.ndarray] = []
        target = int(SAMPLE_RATE * 1.5)  # 1.5 s of audio
        collected = 0
        while collected < target:
            chunk = capture.read_chunk()
            samples.append(chunk)
            collected += len(chunk)
        audio = np.concatenate(samples)
        rms = float(np.sqrt(np.mean(audio ** 2)))
        peak = float(np.max(np.abs(audio)))
        clipping = peak > 0.95
        vad_active = should_transcribe(audio)
        return {
            "rms": round(rms, 5),
            "peak": round(peak, 5),
            "clipping": clipping,
            "vad_active": vad_active,
            "device_name": device_name,
            "sample_rate": SAMPLE_RATE,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        if capture is not None:
            try:
                capture.close()
            except Exception:
                pass


class AudioSettingsRequest(BaseModel):
    agc: bool | None = None
    noise_reduction: bool | None = None


@router.post("/settings/audio")
async def set_audio_settings(req: AudioSettingsRequest):
    import main as state
    settings_path = os.path.join(state.DATA_DIR, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}
    if req.agc is not None:
        settings["agc_enabled"] = req.agc
    if req.noise_reduction is not None:
        settings["noise_reduction_enabled"] = req.noise_reduction
    os.makedirs(state.DATA_DIR, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f)
    return {"ok": True}


class PrivacySettingsRequest(BaseModel):
    retention_days: int | None = None


@router.post("/settings/privacy")
async def set_privacy_settings(req: PrivacySettingsRequest):
    import main as state
    settings_path = os.path.join(state.DATA_DIR, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}
    settings["retention_days"] = req.retention_days
    if req.retention_days == 0:
        settings["passive_collection_enabled"] = False
    os.makedirs(state.DATA_DIR, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f)
    return {"ok": True}
