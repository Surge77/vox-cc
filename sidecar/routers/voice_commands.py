"""Voice commands router (Tier 2 #6).

Recognizes spoken triggers like "scratch that" as commands instead of text,
returning a key combination the Tauri layer can play back via inject_keypress.

Defaults are baked into the module; users can add / override via
POST /voice-commands and remove via DELETE /voice-commands/{trigger}.
Persisted to DATA_DIR/voice_commands.json.
"""
import json
import os
import re
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


# Built-in command set. Triggers are stored lowercased + trimmed. Key combos
# use the same string format as the Tauri-side parser: lowercased components
# joined by "+", e.g. "ctrl+z", "ctrl+shift+a", "enter".
DEFAULT_COMMANDS: dict[str, str] = {
    "scratch that": "ctrl+z",
    "delete that": "ctrl+z",
    "undo that": "ctrl+z",
    "select all": "ctrl+a",
    "copy that": "ctrl+c",
    "paste that": "ctrl+v",
    "new line": "enter",
    "new paragraph": "enter+enter",
    "tab": "tab",
    "backspace": "backspace",
    "escape": "escape",
}


_COMMAND_FILE = "voice_commands.json"


def _commands_path(data_dir: str) -> str:
    return os.path.join(data_dir, _COMMAND_FILE)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used for both
    stored triggers and live transcript comparison."""
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def load_commands(data_dir: str) -> dict[str, str]:
    """Returns the effective command map: defaults overlaid with the user's
    customizations. User entries override defaults at the same trigger."""
    merged = dict(DEFAULT_COMMANDS)
    try:
        with open(_commands_path(data_dir)) as f:
            user = json.load(f)
        if isinstance(user, dict):
            for trigger, combo in user.items():
                if isinstance(trigger, str) and isinstance(combo, str):
                    merged[_normalize(trigger)] = combo.lower()
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return merged


def match_command(text: str, commands: dict[str, str]) -> str | None:
    """If `text` (after normalization) matches a known command trigger
    exactly, return its key combo string. Otherwise None.

    Exact match keeps the matcher predictable — a long dictation containing
    "select all" as a substring should NOT trigger the command."""
    normalized = _normalize(text)
    if not normalized:
        return None
    return commands.get(normalized)


# ── HTTP API ─────────────────────────────────────────────────────────────────


class AddCommandRequest(BaseModel):
    trigger: str
    key_combo: str


def _save_user_commands(data_dir: str, user_commands: dict[str, str]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(_commands_path(data_dir), "w") as f:
        json.dump(user_commands, f, indent=2)


def _load_user_commands(data_dir: str) -> dict[str, str]:
    try:
        with open(_commands_path(data_dir)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@router.get("/voice-commands")
async def get_commands():
    """Returns merged (defaults + user) command set so the Settings UI can
    show what's currently active."""
    import main as state
    return {
        "defaults": DEFAULT_COMMANDS,
        "user": _load_user_commands(state.DATA_DIR),
        "effective": load_commands(state.DATA_DIR),
    }


@router.post("/voice-commands")
async def add_command(req: AddCommandRequest):
    import main as state
    trigger = _normalize(req.trigger)
    if not trigger:
        return {"ok": False, "error": "trigger cannot be empty"}
    user = _load_user_commands(state.DATA_DIR)
    user[trigger] = req.key_combo.strip().lower()
    _save_user_commands(state.DATA_DIR, user)
    return {"ok": True, "trigger": trigger, "key_combo": user[trigger]}


@router.delete("/voice-commands/{trigger}")
async def remove_command(trigger: str):
    import main as state
    user = _load_user_commands(state.DATA_DIR)
    normalized = _normalize(trigger)
    if normalized in user:
        del user[normalized]
        _save_user_commands(state.DATA_DIR, user)
        return {"ok": True}
    return {"ok": False, "error": "trigger not found"}
