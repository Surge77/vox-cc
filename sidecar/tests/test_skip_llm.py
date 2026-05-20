"""Tests for the smart skip-LLM heuristic in routers/text_processing.py.

Pure-function tests; no FastAPI / state needed.
"""
import pytest

from routers.text_processing import is_simple_clean_text, SKIP_LLM_MAX_CHARS


# ── Positive cases — should skip LLM ─────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "Hello world.",
        "How are you doing today?",
        "Stop the press!",
        "I want a coffee.",
        "The meeting is at three.",
    ],
)
def test_simple_sentences_skip(text):
    assert is_simple_clean_text(text)


# ── Negative cases ───────────────────────────────────────────────────────────

def test_lowercase_first_letter_blocks():
    assert not is_simple_clean_text("hello world.")


def test_no_terminator_blocks():
    assert not is_simple_clean_text("Hello world")


def test_too_long_blocks():
    text = "A" + "a" * SKIP_LLM_MAX_CHARS + "."  # exceeds limit
    assert not is_simple_clean_text(text)


def test_empty_blocks():
    assert not is_simple_clean_text("")
    assert not is_simple_clean_text("   ")


@pytest.mark.parametrize(
    "marker",
    ["<unk>", "[noise]", "[music]", "[laughter]", "♪", "♫"],
)
def test_asr_markers_block(marker):
    assert not is_simple_clean_text(f"Hello {marker} world.")


def test_all_caps_word_blocks():
    # "API" is an acronym — LLM may need to format it
    assert not is_simple_clean_text("Call the API now.")


def test_single_letter_caps_ok():
    # "A" alone is fine — typical article usage
    assert is_simple_clean_text("A quick brown fox.")


def test_trailing_whitespace_ok():
    assert is_simple_clean_text("  Hello there.  ")


def test_boundary_max_chars():
    # exactly SKIP_LLM_MAX_CHARS should pass; +1 should fail
    body = "a" * (SKIP_LLM_MAX_CHARS - 2)
    at_limit = "A" + body + "."  # length == SKIP_LLM_MAX_CHARS
    assert len(at_limit) == SKIP_LLM_MAX_CHARS
    assert is_simple_clean_text(at_limit)

    over_limit = "A" + body + "a" + "."  # length == SKIP_LLM_MAX_CHARS + 1
    assert len(over_limit) == SKIP_LLM_MAX_CHARS + 1
    assert not is_simple_clean_text(over_limit)
