"""Tests for audio.pipeline.resolve_language — multi-language setting parser."""
import pytest

from audio.pipeline import SUPPORTED_LANGUAGES, resolve_language


# ── Auto / empty / None → None (Whisper auto-detect) ──────────────────────────

@pytest.mark.parametrize("value", [None, "", "auto", "AUTO", "  auto  "])
def test_auto_returns_none(value):
    assert resolve_language(value) is None


# ── Supported codes passed through ───────────────────────────────────────────

@pytest.mark.parametrize("code", sorted(SUPPORTED_LANGUAGES))
def test_supported_codes_round_trip(code):
    assert resolve_language(code) == code


def test_uppercase_normalized():
    assert resolve_language("EN") == "en"
    assert resolve_language(" Hi ") == "hi"


# ── Unsupported / junk → safe fallback to English ────────────────────────────

@pytest.mark.parametrize("value", ["xyz", "klingon", "123", "en-US"])
def test_unsupported_falls_back_to_english(value):
    # Falls back to "en" — we never pass junk to Whisper
    assert resolve_language(value) == "en"


def test_no_silent_acceptance_of_locale_tags():
    # "en-US" is a BCP-47 locale tag; faster-whisper expects just "en".
    # We treat the full tag as unsupported and fall back to "en".
    assert resolve_language("en-US") == "en"
