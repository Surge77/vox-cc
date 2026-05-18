import asyncio
import os
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class ProcessTextRequest(BaseModel):
    raw_transcript: str
    context_string: str = ""
    executable_name: str = ""
    window_title: str = ""
    inferred_extension: str | None = None
    text_preceding_cursor: str = ""
    text_succeeding_cursor: str = ""
    use_local_llm: bool = True
    custom_vocabulary: list[str] = []
    style: str = "auto"


class ProcessTextResponse(BaseModel):
    cleaned_text: str
    error: str | None = None


@router.post("/process-text", response_model=ProcessTextResponse)
async def process_text(req: ProcessTextRequest):
    import main as state
    from models.prompt_router import render_system_prompt

    system_prompt = render_system_prompt(
        req.executable_name,
        preceding_text=req.text_preceding_cursor,
        vocabulary=req.custom_vocabulary or [],
        style=req.style,
    )
    raw = req.raw_transcript.strip()

    if not raw:
        return ProcessTextResponse(cleaned_text="")

    cleaned = raw
    error = None
    try:
        if req.use_local_llm and state._models_state.get("llm"):
            llm = state._llm_ref[0]
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_local, llm, system_prompt, raw),
                timeout=15.0,
            )
            cleaned = result if result else raw
        else:
            groq_key = _load_groq_key()
            if not groq_key:
                return ProcessTextResponse(cleaned_text=raw, error="no groq key")
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_groq, groq_key, system_prompt, raw),
                timeout=15.0,
            )
            cleaned = result if result else raw
    except Exception as e:
        error = str(e)

    cleaned = _expand_snippets(state.DATA_DIR, cleaned)
    _append_passive_log(state.DATA_DIR, raw, cleaned, req.executable_name)
    return ProcessTextResponse(cleaned_text=cleaned, error=error)


def _run_local(llm, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_llm
    return run_llm(llm, system_prompt, raw)


def _run_groq(api_key: str, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_groq
    return run_groq(api_key, system_prompt, raw)


def _load_groq_key() -> str:
    data_dir = os.path.join(os.path.expanduser("~"), ".vox", "data")
    key_path = os.path.join(data_dir, "groq_key.txt")
    try:
        with open(key_path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _expand_snippets(data_dir: str, text: str) -> str:
    import json
    import re
    try:
        with open(os.path.join(data_dir, "snippets.json")) as f:
            snippets: dict[str, str] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return text
    for trigger, expansion in snippets.items():
        text = re.sub(rf"(?i)\b{re.escape(trigger)}\b", expansion, text)
    return text


def _append_passive_log(data_dir: str, raw: str, cleaned: str, executable_name: str) -> None:
    import json
    from datetime import datetime, timezone
    settings_path = os.path.join(data_dir, "settings.json")
    try:
        with open(settings_path) as f:
            if not json.load(f).get("passive_collection_enabled", False):
                return
    except (FileNotFoundError, json.JSONDecodeError):
        return

    from models.prompt_router import get_profile
    import main as state
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_asr": raw,
        "user_edited": cleaned,
        "profile": get_profile(executable_name),
    }
    if state._pending_audio_uuid:
        entry["audio_file"] = f"{state._pending_audio_uuid}.npy"
        state._pending_audio_uuid = None

    log_path = os.path.join(data_dir, "passive_log.jsonl")
    os.makedirs(data_dir, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
