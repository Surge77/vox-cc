import json
import os
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class VocabularyRequest(BaseModel):
    words: list[str]


@router.post("/vocabulary")
async def set_vocabulary(req: VocabularyRequest):
    import main as state
    os.makedirs(state.DATA_DIR, exist_ok=True)
    with open(os.path.join(state.DATA_DIR, "vocabulary.json"), "w") as f:
        json.dump(req.words, f)
    return {"ok": True}
