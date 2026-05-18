import os
import sys

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROMPTS_DIR = os.path.join(BASE, "prompts")

_EXEC_MAP = {
    "code": "code_editor",
    "code - insiders": "code_editor",
    "cursor": "code_editor",
    "idea64": "code_editor",
    "pycharm64": "code_editor",
    "sublime_text": "code_editor",
    "notepad++": "code_editor",
    "outlook": "email_client",
    "thunderbird": "email_client",
    "msedge": "email_client",
    "slack": "chat_messaging",
    "discord": "chat_messaging",
    "teams": "chat_messaging",
    "signal": "chat_messaging",
    "cmd": "terminal_interface",
    "powershell": "terminal_interface",
    "pwsh": "terminal_interface",
    "windowsterminal": "terminal_interface",
    "wt": "terminal_interface",
    "winword": "document_editor",
    "soffice": "document_editor",
    "notepad": "document_editor",
    "wordpad": "document_editor",
}


def _load_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def get_profile(executable_name: str) -> str:
    key = (executable_name or "").lower().replace(".exe", "").strip()
    return _EXEC_MAP.get(key, "neutral_fallback")


def get_system_prompt(executable_name: str) -> str:
    profile = get_profile(executable_name)
    try:
        return _load_prompt(profile)
    except FileNotFoundError:
        return _load_prompt("neutral_fallback")


def render_system_prompt(
    executable_name: str,
    preceding_text: str = "",
    vocabulary: list[str] | None = None,
) -> str:
    """Return system prompt with optional context appended."""
    profile = get_profile(executable_name)
    try:
        base = _load_prompt(profile)
    except FileNotFoundError:
        base = _load_prompt("neutral_fallback")

    context_parts: list[str] = []
    if vocabulary:
        vocab_str = ", ".join(vocabulary[:50])
        context_parts.append(f"Custom terms to preserve exactly as spelled: {vocab_str}.")
    if preceding_text.strip():
        context_parts.append(
            f"Context (text immediately before cursor):\n{preceding_text[:300]}"
        )

    if context_parts:
        return base + "\n\n" + "\n".join(context_parts)
    return base
