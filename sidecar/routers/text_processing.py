import asyncio
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


class ProcessTextResponse(BaseModel):
    cleaned_text: str
    error: str | None = None


@router.post("/process-text", response_model=ProcessTextResponse)
async def process_text(req: ProcessTextRequest):
    import main as state
    from models.prompt_router import get_system_prompt

    system_prompt = get_system_prompt(req.executable_name)
    raw = req.raw_transcript.strip()

    if not raw:
        return ProcessTextResponse(cleaned_text="")

    try:
        if req.use_local_llm and state._models_state.get("llm"):
            llm = state._llm_ref[0]
            loop = asyncio.get_event_loop()
            cleaned = await asyncio.wait_for(
                loop.run_in_executor(None, _run_local, llm, system_prompt, raw),
                timeout=15.0,
            )
        else:
            groq_key = _load_groq_key()
            if not groq_key:
                return ProcessTextResponse(cleaned_text=raw, error="no groq key")
            loop = asyncio.get_event_loop()
            cleaned = await asyncio.wait_for(
                loop.run_in_executor(None, _run_groq, groq_key, system_prompt, raw),
                timeout=15.0,
            )
        return ProcessTextResponse(cleaned_text=cleaned if cleaned else raw)
    except Exception as e:
        return ProcessTextResponse(cleaned_text=raw, error=str(e))


def _run_local(llm, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_llm
    return run_llm(llm, system_prompt, raw)


def _run_groq(api_key: str, system_prompt: str, raw: str) -> str:
    from models.llm_engine import run_groq
    return run_groq(api_key, system_prompt, raw)


def _load_groq_key() -> str:
    import os
    data_dir = os.path.join(os.path.expanduser("~"), ".vox", "data")
    key_path = os.path.join(data_dir, "groq_key.txt")
    try:
        with open(key_path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
