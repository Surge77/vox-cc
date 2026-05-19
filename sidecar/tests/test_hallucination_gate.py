"""
Tests for audio/vad.py — hallucination gate and VAD silence detection.
torch/noisereduce are stubbed in conftest.py.  After import we force
_silero_available = False so should_transcribe uses the RMS fallback path
(no real model loaded).
"""
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

import audio.vad as vad

# Force RMS fallback — Silero can't run in a stub environment
vad._silero_available = False
vad._silero_model = None

from audio.vad import (
    DIGITAL_SILENCE_RMS,
    HALLUCINATION_PHRASES,
    apply_agc,
    is_hallucination,
    should_transcribe,
    _dedup_overlap,
)

# pull in the pipeline helper too
from audio.pipeline import _dedup_overlap  # noqa: F811


# ── is_hallucination ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", list(HALLUCINATION_PHRASES))
def test_known_phrases_flagged(phrase):
    assert is_hallucination(phrase, 1.0)


def test_known_phrase_with_punctuation(phrase="Thank you."):
    assert is_hallucination(phrase, 1.0)


def test_normal_speech_not_flagged():
    assert not is_hallucination("Hello, how are you doing today?", 2.0)


def test_short_transcript_long_audio_flagged():
    # 1 word over 10s → 0.3 * 10 = 3 words expected minimum
    assert is_hallucination("hi", 10.0)


def test_short_transcript_short_audio_ok():
    # Under 3s threshold — word count not checked
    assert not is_hallucination("hi", 2.9)


def test_boundary_exactly_3s_not_checked():
    # audio_duration_s > 3.0 is the condition; exactly 3.0 is not flagged by word count
    assert not is_hallucination("hi", 3.0)


def test_sufficient_words_not_flagged():
    # 5 words in 4s → 0.3 * 4 = 1.2 expected minimum; 5 > 1.2 → ok
    assert not is_hallucination("one two three four five", 4.0)


# ── should_transcribe (RMS fallback path) ────────────────────────────────────

def test_digital_silence_rejected():
    audio = np.zeros(8000, dtype=np.float32)
    assert not should_transcribe(audio)


def test_very_quiet_signal_rejected():
    # RMS = 0.0003 < DIGITAL_SILENCE_RMS (0.0005)
    audio = np.full(8000, 0.0003, dtype=np.float32)
    assert not should_transcribe(audio)


def test_speech_level_accepted():
    # RMS = 0.05 — typical conversational level
    audio = np.full(8000, 0.05, dtype=np.float32)
    assert should_transcribe(audio)


def test_rms_fallback_threshold():
    # RMS just above 0.002 (RMS fallback threshold when Silero unavailable)
    audio = np.full(8000, 0.003, dtype=np.float32)
    assert should_transcribe(audio)


def test_rms_fallback_below_threshold():
    # RMS = 0.001 — below both DIGITAL_SILENCE_RMS (0.0005) is false, but also
    # falls through to RMS fallback 0.002 threshold
    audio = np.full(8000, 0.001, dtype=np.float32)
    # RMS(0.001) = 0.001 < DIGITAL_SILENCE_RMS(0.0005)? No, 0.001 > 0.0005.
    # So it reaches _silero_has_speech → fallback path → 0.001 < 0.002 → False
    assert not should_transcribe(audio)


# ── apply_agc ────────────────────────────────────────────────────────────────

def test_agc_amplifies_quiet_signal():
    audio = np.full(8000, 0.01, dtype=np.float32)
    result = apply_agc(audio, target_rms=0.08)
    rms = float(np.sqrt(np.mean(result ** 2)))
    # tanh soft-clip will prevent exact target, but should be louder than input
    assert rms > 0.01


def test_agc_does_not_amplify_silence():
    audio = np.zeros(8000, dtype=np.float32)
    result = apply_agc(audio)
    assert np.allclose(result, 0.0)


def test_agc_caps_gain():
    # Very quiet signal — gain would be >20x without cap
    audio = np.full(8000, 1e-5, dtype=np.float32)
    result = apply_agc(audio, target_rms=0.08)
    # With 20x cap and tanh soft-clip, output should not be huge
    assert float(np.max(np.abs(result))) < 2.0


# ── _dedup_overlap (pipeline helper) ─────────────────────────────────────────

def test_dedup_removes_repeated_prefix():
    prev = ["hello", "world"]
    new_text = "world this is new"
    result = _dedup_overlap(prev, new_text)
    assert result == "this is new"


def test_dedup_no_overlap():
    prev = ["hello"]
    new_text = "completely different"
    assert _dedup_overlap(prev, new_text) == "completely different"


def test_dedup_empty_prev():
    assert _dedup_overlap([], "some text") == "some text"


def test_dedup_empty_new():
    assert _dedup_overlap(["hello"], "") == ""


def test_dedup_case_insensitive():
    prev = ["Hello", "World"]
    new_text = "world foo bar"
    result = _dedup_overlap(prev, new_text)
    assert result == "foo bar"
