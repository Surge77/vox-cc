"""Tests for routers.voice_commands — trigger matcher + normalization."""
from routers.voice_commands import DEFAULT_COMMANDS, _normalize, match_command


# ── Normalization ────────────────────────────────────────────────────────────

def test_normalize_lowercases():
    assert _normalize("Scratch That") == "scratch that"


def test_normalize_strips_punctuation():
    assert _normalize("scratch that.") == "scratch that"
    assert _normalize("select all!") == "select all"
    assert _normalize("'select all'") == "select all"


def test_normalize_collapses_whitespace():
    assert _normalize("  scratch   that  ") == "scratch that"
    assert _normalize("scratch\tthat") == "scratch that"


def test_normalize_empty():
    assert _normalize("") == ""
    assert _normalize("   ") == ""
    assert _normalize("...") == ""


# ── match_command ────────────────────────────────────────────────────────────

def test_default_commands_match():
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("scratch that", cmds) == "ctrl+z"
    assert match_command("select all", cmds) == "ctrl+a"
    assert match_command("new line", cmds) == "enter"
    assert match_command("new paragraph", cmds) == "enter+enter"


def test_match_is_case_insensitive_via_normalize():
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("Select All", cmds) == "ctrl+a"
    assert match_command("SCRATCH THAT", cmds) == "ctrl+z"


def test_match_strips_punctuation():
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("Scratch that.", cmds) == "ctrl+z"
    assert match_command("Select all!", cmds) == "ctrl+a"


def test_non_command_text_returns_none():
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("hello world", cmds) is None
    assert match_command("scratch that and then more text", cmds) is None
    # Substring should NOT trigger — exact match only
    assert match_command("I would like to select all the rows", cmds) is None


def test_empty_text_returns_none():
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("", cmds) is None
    assert match_command("   ", cmds) is None


def test_user_override_takes_precedence():
    # Simulate a user customization that overrides a default
    cmds = dict(DEFAULT_COMMANDS)
    cmds["scratch that"] = "ctrl+shift+z"  # custom redo instead of undo
    assert match_command("scratch that", cmds) == "ctrl+shift+z"


def test_custom_user_command():
    cmds = dict(DEFAULT_COMMANDS)
    cmds["go home"] = "home"
    assert match_command("go home", cmds) == "home"


def test_unknown_default_keys_absent():
    # Make sure we don't accidentally match the dictionary key list
    cmds = dict(DEFAULT_COMMANDS)
    assert match_command("ctrl+z", cmds) is None
