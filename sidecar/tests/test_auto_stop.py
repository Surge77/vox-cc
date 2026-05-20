"""Tests for the SilenceDetector used by auto-stop-on-silence.

Stateful but pure-Python; no I/O, no torch needed for the detector itself.
"""
import numpy as np
import pytest

import audio.vad as vad

# Force RMS-fallback Silero gating (no real model in test env)
vad._silero_available = False
vad._silero_model = None

from audio.vad import SilenceDetector, NOISE_RMS_FLOOR


# Each chunk in the pipeline is CHUNK_SAMPLES = 16000 samples = 1 s @ 16 kHz.
# Use smaller fake chunks so we don't need huge arrays in tests.
SR = 16000


def _silence(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


def _speech(n: int, amp: float = 0.05) -> np.ndarray:
    # Constant-amplitude signal — RMS = amp
    return np.full(n, amp, dtype=np.float32)


def test_no_speech_yet_never_stops():
    det = SilenceDetector(threshold_ms=500)
    for _ in range(20):
        assert not det.feed(_silence(SR))  # 20s of leading silence is fine


def test_stops_after_threshold_silence():
    det = SilenceDetector(threshold_ms=500)  # 500ms = 8000 samples
    assert not det.feed(_speech(SR))  # arm with 1 s of speech
    # 400ms silence — under threshold
    assert not det.feed(_silence(int(0.4 * SR)))
    # additional 200ms — total 600ms > 500ms threshold → fires
    assert det.feed(_silence(int(0.2 * SR)))


def test_speech_chunk_resets_counter():
    det = SilenceDetector(threshold_ms=500)
    det.feed(_speech(SR))  # armed
    det.feed(_silence(int(0.4 * SR)))  # 400ms silence
    det.feed(_speech(int(0.1 * SR)))  # short speech — should reset counter
    # Now 400ms more silence should NOT fire because counter was reset
    assert not det.feed(_silence(int(0.4 * SR)))
    # but adding enough more does fire
    assert det.feed(_silence(int(0.2 * SR)))


def test_single_silence_chunk_above_threshold_fires():
    det = SilenceDetector(threshold_ms=300)
    det.feed(_speech(SR))
    # one 500ms silence chunk — single feed, exceeds threshold
    assert det.feed(_silence(int(0.5 * SR)))


def test_reset_clears_state():
    det = SilenceDetector(threshold_ms=500)
    det.feed(_speech(SR))
    det.feed(_silence(int(0.6 * SR)))  # would have fired
    det.reset()
    # post-reset: silence alone shouldn't fire (back to "no speech yet")
    assert not det.feed(_silence(int(0.6 * SR)))


def test_threshold_clamped_to_at_least_one_sample():
    # Edge case: threshold_ms = 0 — should still produce a usable detector
    det = SilenceDetector(threshold_ms=0)
    det.feed(_speech(SR))
    # Any silence at all fires (threshold = 1 sample)
    assert det.feed(_silence(10))


def test_floor_rms_boundary():
    # Signal at exactly NOISE_RMS_FLOOR should count as speech (>=)
    det = SilenceDetector(threshold_ms=500)
    boundary = _speech(SR, amp=NOISE_RMS_FLOOR)
    assert not det.feed(boundary)
    # Now armed — verify silence count still works
    assert det.feed(_silence(int(0.6 * SR)))


def test_empty_chunk_handled():
    det = SilenceDetector(threshold_ms=500)
    det.feed(_speech(SR))
    # Empty array — treated as silence but doesn't accumulate samples
    assert not det.feed(np.array([], dtype=np.float32))
