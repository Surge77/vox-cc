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


def get_system_prompt(executable_name: str) -> str:
    key = (executable_name or "").lower().replace(".exe", "").strip()
    profile = _EXEC_MAP.get(key, "neutral_fallback")
    try:
        return _load_prompt(profile)
    except FileNotFoundError:
        return _load_prompt("neutral_fallback")
