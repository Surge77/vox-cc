from fastapi import APIRouter
from audio.capture import list_audio_devices

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
