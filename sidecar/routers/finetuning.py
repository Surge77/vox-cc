import json
import os
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


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
