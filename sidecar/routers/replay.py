import json
import os
import tempfile
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

MAX_CORRECTION_CHARS = 50_000


class CorrectionRequest(BaseModel):
    user_corrected_text: str


@router.post("/replay/log-correction")
async def log_correction(req: CorrectionRequest) -> dict:
    """Update the user_edited field of the last passive_log entry."""
    if len(req.user_corrected_text) > MAX_CORRECTION_CHARS:
        raise HTTPException(status_code=400, detail="correction text too long")

    import main as state
    log_path = os.path.join(state.DATA_DIR, "passive_log.jsonl")
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no log entries")

    if not lines:
        raise HTTPException(status_code=404, detail="no log entries")

    try:
        last = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(status_code=422, detail="last entry malformed")

    last["user_edited"] = req.user_corrected_text
    last["corrected"] = True
    last["corrected_at"] = datetime.now(timezone.utc).isoformat()
    lines[-1] = json.dumps(last) + "\n"

    # Atomic write: write to sibling temp file then rename — prevents partial reads
    dir_path = os.path.dirname(log_path)
    os.makedirs(dir_path, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=dir_path, delete=False, suffix=".tmp"
    ) as tmp:
        tmp.writelines(lines)
        tmp_path = tmp.name
    os.replace(tmp_path, log_path)

    return {"ok": True}


@router.get("/replay/stats")
async def replay_stats() -> dict:
    """Return aggregate stats from passive_log.jsonl."""
    import main as state
    log_path = os.path.join(state.DATA_DIR, "passive_log.jsonl")
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {
            "session_count": 0,
            "correction_count": 0,
            "avg_final_pass_ms": None,
            "avg_llm_ms": None,
        }

    session_count = 0
    correction_count = 0
    final_pass_values: list[int] = []
    llm_values: list[int] = []

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_count += 1
        if entry.get("corrected"):
            correction_count += 1
        if "final_pass_ms" in entry:
            final_pass_values.append(entry["final_pass_ms"])
        if "llm_ms" in entry:
            llm_values.append(entry["llm_ms"])

    return {
        "session_count": session_count,
        "correction_count": correction_count,
        "avg_final_pass_ms": (
            int(sum(final_pass_values) / len(final_pass_values))
            if final_pass_values
            else None
        ),
        "avg_llm_ms": (
            int(sum(llm_values) / len(llm_values)) if llm_values else None
        ),
    }
