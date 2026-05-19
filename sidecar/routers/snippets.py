import json
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

MAX_EXPANSION_CHARS = 5_000
MAX_SNIPPET_COUNT = 500


def _snippets_path(data_dir: str) -> str:
    return os.path.join(data_dir, "snippets.json")


def _load_snippets(data_dir: str) -> dict[str, str]:
    try:
        with open(_snippets_path(data_dir)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_snippets(data_dir: str, snippets: dict[str, str]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(_snippets_path(data_dir), "w") as f:
        json.dump(snippets, f)


class SnippetRequest(BaseModel):
    trigger: str
    expansion: str


@router.get("/snippets")
async def list_snippets() -> dict:
    import main as state
    return _load_snippets(state.DATA_DIR)


@router.post("/snippets")
async def add_snippet(req: SnippetRequest) -> dict:
    import main as state
    trigger = req.trigger.strip()
    if not trigger:
        raise HTTPException(status_code=400, detail="trigger cannot be empty")
    if len(req.expansion) > MAX_EXPANSION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"expansion exceeds {MAX_EXPANSION_CHARS} character limit",
        )
    snippets = _load_snippets(state.DATA_DIR)
    if len(snippets) >= MAX_SNIPPET_COUNT and trigger not in snippets:
        raise HTTPException(
            status_code=400,
            detail=f"snippet limit ({MAX_SNIPPET_COUNT}) reached — delete one first",
        )
    snippets[trigger] = req.expansion
    _save_snippets(state.DATA_DIR, snippets)
    return {"ok": True}


@router.delete("/snippets/{trigger}")
async def delete_snippet(trigger: str) -> dict:
    import main as state
    snippets = _load_snippets(state.DATA_DIR)
    if trigger not in snippets:
        raise HTTPException(status_code=404, detail="trigger not found")
    del snippets[trigger]
    _save_snippets(state.DATA_DIR, snippets)
    return {"ok": True}
