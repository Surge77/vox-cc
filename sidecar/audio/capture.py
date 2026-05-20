"""
audio/capture.py — 16kHz mono float32 microphone capture for Windows.

Backend priority (highest to lowest):
  1. sounddevice WASAPI Exclusive  — bypasses Windows audio engine entirely.
                                     No enhancements, lowest latency, but
                                     locks the device (other apps lose access).
  2. sounddevice WASAPI Shared     — normal shared mode, enhancements may apply.
  3. PyAudio WASAPI default        — legacy fallback.
  4. PyAudio MME default           — last resort.

Every blocking read is wrapped in a watchdog thread.  If PortAudio hangs in a
native C call (no exception, just never returns), the watchdog detects it after
_WATCHDOG_TIMEOUT seconds and triggers the reconnect path.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_FRAMES = 1024

# ---------------------------------------------------------------------------
# Reconnect policy
# ---------------------------------------------------------------------------
_MAX_RECONNECT_ATTEMPTS = 5
_RECONNECT_BASE_DELAY   = 0.5   # seconds; doubles each attempt

# ---------------------------------------------------------------------------
# Watchdog — how long to wait for a single stream.read() call before declaring
# PortAudio hung.  2 seconds is generous even for large buffers.
# ---------------------------------------------------------------------------
_WATCHDOG_TIMEOUT = 2.0  # seconds


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def resample_to_16k(audio: np.ndarray, original_rate: int) -> np.ndarray:
    if original_rate == SAMPLE_RATE:
        return audio.astype(np.float32)
    return resample_poly(audio, SAMPLE_RATE, original_rate).astype(np.float32)


def _cp1252_fixup(name: str) -> str:
    """PyAudio on Windows decodes device names as CP1252 — repair UTF-8 double-encode."""
    try:
        return name.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return name


# ---------------------------------------------------------------------------
# Public helpers (used by routers / UI)
# ---------------------------------------------------------------------------

def list_audio_devices() -> list[dict]:
    import pyaudio
    pa = pyaudio.PyAudio()
    devices: list[dict] = []
    try:
        default_idx = pa.get_default_input_device_info()["index"]
    except OSError:
        default_idx = -1
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            name = _cp1252_fixup(info["name"])
            bt_keywords = ("bluetooth", " bt ", "airpods", "airplay", "wireless")
            devices.append({
                "index": i,
                "name": name,
                "default": i == default_idx,
                "bluetooth_warning": any(k in name.lower() for k in bt_keywords),
            })
    pa.terminate()
    return devices


def check_mic_permission() -> None:
    import pyaudio
    pa = pyaudio.PyAudio()
    has_input = any(
        pa.get_device_info_by_index(i)["maxInputChannels"] > 0
        for i in range(pa.get_device_count())
    )
    pa.terminate()
    if not has_input:
        raise RuntimeError(
            "No microphone input detected. "
            "Check Windows Privacy → Microphone settings."
        )


# ---------------------------------------------------------------------------
# Internal: sounddevice backend
# ---------------------------------------------------------------------------

def _sd_open_exclusive(device_index: int | None, native_rate: int, native_chunk: int):
    """Return an open sounddevice.InputStream in WASAPI Exclusive mode, or raise."""
    import sounddevice as sd
    wasapi = sd.WasapiSettings(exclusive=True)
    stream = sd.InputStream(
        device=device_index,
        samplerate=native_rate,
        channels=1,
        dtype="float32",
        blocksize=native_chunk,
        extra_settings=wasapi,
    )
    stream.start()
    return stream


def _sd_open_shared(device_index: int | None, native_rate: int, native_chunk: int):
    """Return an open sounddevice.InputStream in WASAPI Shared mode, or raise."""
    import sounddevice as sd
    stream = sd.InputStream(
        device=device_index,
        samplerate=native_rate,
        channels=1,
        dtype="float32",
        blocksize=native_chunk,
    )
    stream.start()
    return stream


# ---------------------------------------------------------------------------
# Internal: PyAudio backend
# ---------------------------------------------------------------------------

def _pa_wasapi_default(pa) -> int | None:
    """Return WASAPI host API's default input device index, or None."""
    import pyaudio
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        if info.get("type") == pyaudio.paWASAPI:
            dev = info.get("defaultInputDevice", -1)
            return int(dev) if dev >= 0 else None
    return None


def _pa_open(pa, device_index: int | None, native_rate: int, native_chunk: int):
    """Try paInt16 then paFloat32 for the given device; return (stream, using_int16).
    paInt16 must come first — Intel SST mic returns silent zeros on paFloat32 (known driver bug)."""
    import pyaudio
    base = dict(channels=1, rate=native_rate, input=True,
                frames_per_buffer=native_chunk)
    if device_index is not None:
        base["input_device_index"] = device_index
    for fmt, is_int16 in [(pyaudio.paInt16, True), (pyaudio.paFloat32, False)]:
        try:
            stream = pa.open(format=fmt, **base)
            return stream, is_int16
        except OSError:
            continue
    raise RuntimeError(f"PyAudio: no working format for device {device_index}")


# ---------------------------------------------------------------------------
# Watchdog read helper
# ---------------------------------------------------------------------------

def _read_with_watchdog(fn, timeout: float = _WATCHDOG_TIMEOUT):
    """
    Run fn() in a daemon thread.  If it does not return within `timeout`
    seconds, raise OSError("PortAudio read timed out") so the caller can
    trigger reconnect.  The hung thread is abandoned — it will eventually
    be cleaned up when the process exits or the device resets.
    """
    result: list = [None]
    exc:    list = [None]

    def _run():
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_run, daemon=True, name="audio-watchdog")
    t.start()
    t.join(timeout)

    if t.is_alive():
        raise OSError(
            f"PortAudio read timed out after {timeout:.1f}s — "
            "device may be hung or disconnected"
        )
    if exc[0] is not None:
        raise exc[0]
    return result[0]


# ---------------------------------------------------------------------------
# AudioCapture — public API (same interface as before)
# ---------------------------------------------------------------------------

class AudioCapture:
    """
    Unified audio capture with three-tier backend selection and watchdog reads.

    Backend selection at open() time:
      1. sounddevice WASAPI Exclusive  (opt-in via exclusive_mode=True in settings)
      2. sounddevice WASAPI Shared
      3. PyAudio WASAPI → MME fallback

    set exclusive_mode=True in settings.json to bypass Windows audio enhancements.
    """

    def __init__(self, device_index: int | None = None, exclusive_mode: bool = False,
                 prefer_pyaudio: bool = False):
        self._device_index    = device_index
        self._exclusive_mode  = exclusive_mode
        self._prefer_pyaudio  = prefer_pyaudio  # Skip sounddevice tiers — fixes Intel SST silent-zero bug
        self._native_rate     = SAMPLE_RATE
        self._native_chunk    = CHUNK_FRAMES
        self._backend: str    = ""          # "sd_exclusive" | "sd_shared" | "pyaudio"
        self._sd_stream       = None        # sounddevice.InputStream
        self._pa              = None        # pyaudio.PyAudio instance
        self._pa_stream       = None        # pyaudio.Stream
        self._pa_int16: bool  = False
        self._reconnect_count = 0

    # ------------------------------------------------------------------
    # open()
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the best available audio backend."""
        self._close_resources()

        dev = self._device_index
        native_rate, native_chunk = self._query_native_rate(dev)
        self._native_rate  = native_rate
        self._native_chunk = native_chunk

        # Log all available input devices so we can confirm the right one is used.
        try:
            import sounddevice as _sd
            all_devs = _sd.query_devices()
            default_in = _sd.query_devices(kind='input')
            logger.warning(
                "[AudioCapture] Default input device: '%s' (index=%s, rate=%.0fHz)",
                default_in['name'], default_in.get('index', '?'),
                default_in['default_samplerate'],
            )
            input_devs = [
                f"  [{i}] {d['name']} (in={d['max_input_channels']}ch)"
                for i, d in enumerate(all_devs)
                if d['max_input_channels'] > 0
            ]
            logger.warning("[AudioCapture] Available input devices:\n%s", "\n".join(input_devs))
        except Exception as _e:
            logger.warning("[AudioCapture] Could not enumerate devices: %s", _e)

        # --- Tier 0: skip all sounddevice tiers when prefer_pyaudio is set ---
        # Intel SST mic returns silent zeros on sounddevice WASAPI float32 — known driver bug.
        # PyAudio paInt16 path (Tier 3) is the documented workaround.
        if self._prefer_pyaudio:
            logger.warning("[AudioCapture] prefer_pyaudio=True — skipping sounddevice tiers")
            self._open_pyaudio_path(dev, native_rate, native_chunk)
            return

        # --- Tier 1: sounddevice exclusive (bypasses all Windows enhancements) ---
        if self._exclusive_mode:
            try:
                self._sd_stream = _sd_open_exclusive(dev, native_rate, native_chunk)
                self._backend = "sd_exclusive"
                logger.warning(
                    "[AudioCapture] BACKEND=sounddevice WASAPI EXCLUSIVE "
                    "device=%s rate=%dHz — enhancements bypassed. "
                    "Other apps cannot use this mic while recording.",
                    dev, native_rate,
                )
                self._reconnect_count = 0
                return
            except Exception as e:
                logger.warning(
                    "[AudioCapture] WASAPI Exclusive failed (%s), "
                    "falling through to shared mode", e
                )

        # --- Tier 2: sounddevice shared ---
        try:
            self._sd_stream = _sd_open_shared(dev, native_rate, native_chunk)
            self._backend = "sd_shared"
            logger.warning(
                "[AudioCapture] BACKEND=sounddevice WASAPI SHARED device=%s rate=%dHz. "
                "TIP: set 'exclusive_mode': true in settings to bypass "
                "Windows audio enhancements.",
                dev, native_rate,
            )
            self._reconnect_count = 0
            return
        except Exception as e:
            logger.warning(
                "[AudioCapture] sounddevice shared failed (%s), "
                "falling through to PyAudio", e
            )

        # --- Tier 3: PyAudio WASAPI → MME ---
        self._open_pyaudio_path(dev, native_rate, native_chunk)

    def _open_pyaudio_path(self, dev: int | None, native_rate: int, native_chunk: int) -> None:
        """Open PyAudio backend (paInt16 preferred, paFloat32 fallback). Used by Tier 3
        or directly by Tier 0 when prefer_pyaudio is set (Intel SST workaround)."""
        import pyaudio
        self._pa = pyaudio.PyAudio()
        candidates: list[int | None] = []
        # If user explicitly chose a device, honor it first
        if dev is not None:
            candidates.append(dev)
        wasapi_dev = _pa_wasapi_default(self._pa)
        if wasapi_dev is not None and wasapi_dev not in candidates:
            candidates.append(wasapi_dev)
        try:
            mme_dev = self._pa.get_default_input_device_info()["index"]
            if mme_dev not in candidates:
                candidates.append(mme_dev)
        except OSError:
            pass
        if not candidates:
            candidates.append(None)

        last_err: Exception = RuntimeError("No input devices found")
        for cand in candidates:
            if cand is not None:
                try:
                    info = self._pa.get_device_info_by_index(cand)
                    native_rate  = int(info.get("defaultSampleRate", SAMPLE_RATE))
                    native_chunk = int(CHUNK_FRAMES * native_rate / SAMPLE_RATE)
                    self._native_rate  = native_rate
                    self._native_chunk = native_chunk
                except OSError:
                    continue
            try:
                stream, is_int16 = _pa_open(self._pa, cand, native_rate, native_chunk)
                self._pa_stream = stream
                self._pa_int16  = is_int16
                self._backend   = "pyaudio"
                logger.warning(
                    "[AudioCapture] BACKEND=PyAudio device=%s rate=%dHz fmt=%s",
                    cand, native_rate, "int16" if is_int16 else "float32",
                )
                self._reconnect_count = 0
                return
            except Exception as e:
                last_err = e

        raise RuntimeError(f"PyAudio backend failed: {last_err}")

    # ------------------------------------------------------------------
    # _query_native_rate — sounddevice path needs rate before opening
    # ------------------------------------------------------------------

    def _query_native_rate(self, device_index: int | None) -> tuple[int, int]:
        """Best-effort query of the device's native sample rate."""
        if device_index is None:
            return SAMPLE_RATE, CHUNK_FRAMES
        try:
            import sounddevice as sd
            info = sd.query_devices(device_index)
            rate = int(info.get("default_samplerate", SAMPLE_RATE))
            chunk = int(CHUNK_FRAMES * rate / SAMPLE_RATE)
            return rate, chunk
        except Exception:
            pass
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            info = pa.get_device_info_by_index(device_index)
            pa.terminate()
            rate = int(info.get("defaultSampleRate", SAMPLE_RATE))
            chunk = int(CHUNK_FRAMES * rate / SAMPLE_RATE)
            return rate, chunk
        except Exception:
            return SAMPLE_RATE, CHUNK_FRAMES

    # ------------------------------------------------------------------
    # read_chunk — with watchdog
    # ------------------------------------------------------------------

    def read_chunk(self) -> np.ndarray:
        """
        Read one chunk of audio (≈32ms at 16kHz).
        Wrapped in a watchdog thread — if PortAudio hangs for > 2 seconds,
        OSError is raised and the caller should trigger reconnect.
        """
        try:
            raw = _read_with_watchdog(self._do_read)
            self._reconnect_count = 0   # successful read resets counter
            return raw
        except OSError as e:
            logger.warning("[AudioCapture] read error — %s — reconnecting", e)
            self._reconnect()
            # One more attempt after reconnect; let any further error propagate
            return _read_with_watchdog(self._do_read)

    def _do_read(self) -> np.ndarray:
        """Actual blocking read — runs inside the watchdog thread."""
        if self._backend in ("sd_exclusive", "sd_shared"):
            data, _overflowed = self._sd_stream.read(self._native_chunk)
            audio = np.asarray(data, dtype=np.float32).flatten()
        elif self._backend == "pyaudio":
            raw = self._pa_stream.read(self._native_chunk, exception_on_overflow=False)
            if self._pa_int16:
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.frombuffer(raw, dtype=np.float32).copy()
        else:
            raise RuntimeError("AudioCapture not opened")

        if self._native_rate != SAMPLE_RATE:
            audio = resample_to_16k(audio, self._native_rate)
        return audio

    # ------------------------------------------------------------------
    # _reconnect — exponential backoff, re-opens full backend stack
    # ------------------------------------------------------------------

    def _reconnect(self) -> None:
        self._reconnect_count += 1
        if self._reconnect_count > _MAX_RECONNECT_ATTEMPTS:
            raise RuntimeError(
                f"Audio device failed {_MAX_RECONNECT_ATTEMPTS} consecutive "
                "reconnect attempts — check microphone connection."
            )
        delay = _RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1))
        logger.warning(
            "[AudioCapture] reconnect attempt %d/%d in %.1fs",
            self._reconnect_count, _MAX_RECONNECT_ATTEMPTS, delay,
        )
        self._close_resources()
        time.sleep(delay)
        self.open()

    # ------------------------------------------------------------------
    # stop / close
    # ------------------------------------------------------------------

    def stop_capture(self) -> None:
        """Signal the capture loop to stop (pipeline.py compatibility)."""
        # Actual stop is managed by DictationSession._active flag
        pass

    def close_stream(self) -> None:
        """Close underlying stream resources."""
        self._close_resources()

    def close(self) -> None:
        self._close_resources()

    def _close_resources(self) -> None:
        """Tear down whichever backend is currently active."""
        if self._sd_stream is not None:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None

        if self._pa_stream is not None:
            try:
                self._pa_stream.stop_stream()
                self._pa_stream.close()
            except Exception:
                pass
            self._pa_stream = None

        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

        self._backend = ""


# ---------------------------------------------------------------------------
# PersistentMicCapture — always-on background reader with pre-roll buffer
# ---------------------------------------------------------------------------
#
# Why this exists: sounddevice WASAPI Shared on Intel SST mic takes ~2 seconds
# after stream-open before it starts producing real audio (returns zeros until
# the driver wakes up). With per-session open/close, the user's first 2s of
# speech is silently dropped — they see "the quick brown fox jumps over the
# lazy dog" turn into "A quick brown fog" because only the tail was captured.
#
# Fix: open the mic once at sidecar startup, keep a background thread
# continuously reading, and store the last 3 seconds in a circular buffer.
# When a dictation session starts, snapshot that buffer as the session's
# pre-roll. Mic is already warm + audio from BEFORE the hotkey press is
# included.

class PersistentMicCapture:
    """Single always-on AudioCapture with pre-roll buffer + listener fan-out.

    Background daemon thread continuously reads chunks and:
      1. Appends to a bounded deque (pre-roll, default 3 seconds)
      2. Calls each registered listener with the chunk
    Listeners are responsible for forwarding chunks to their consumer
    (typically an asyncio.Queue via loop.call_soon_threadsafe).
    """

    def __init__(self, capture: "AudioCapture", preroll_seconds: float = 3.0):
        self._capture = capture
        # Chunk size is the native chunk after capture.open(); fall back to
        # CHUNK_FRAMES for the math here — actual chunks may vary slightly
        # after resampling. The deque maxlen is conservative.
        chunks_per_sec = max(1, SAMPLE_RATE // CHUNK_FRAMES)
        self._preroll_maxlen = max(1, int(preroll_seconds * chunks_per_sec))
        self._preroll: deque[np.ndarray] = deque(maxlen=self._preroll_maxlen)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listeners: list[Callable[[np.ndarray], None]] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._capture.open()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="persistent-mic"
        )
        self._thread.start()
        self._started = True
        logger.warning(
            "[PersistentMic] started — preroll=%d chunks (~%.1fs)",
            self._preroll_maxlen,
            self._preroll_maxlen * CHUNK_FRAMES / SAMPLE_RATE,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._capture.close()
        except Exception:
            pass
        self._started = False
        with self._lock:
            self._preroll.clear()
            self._listeners.clear()

    def _run(self) -> None:
        """Background read loop. Survives transient read errors via reconnect
        logic in AudioCapture; logs and backs off on repeated failure."""
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                chunk = self._capture.read_chunk()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    logger.warning(
                        "[PersistentMic] 5 consecutive read errors (%s) — sleeping 1s",
                        e,
                    )
                    time.sleep(1.0)
                else:
                    time.sleep(0.1)
                continue

            with self._lock:
                self._preroll.append(chunk)
                listeners = list(self._listeners)

            # Fan out to listeners OUTSIDE the lock so a slow consumer can't
            # block the read loop (the thread bridging into asyncio must be
            # responsive or the queue fills up and drops chunks).
            for cb in listeners:
                try:
                    cb(chunk)
                except Exception:
                    logger.exception("[PersistentMic] listener raised")

    def snapshot_preroll(self) -> list[np.ndarray]:
        """Return a copy of the current pre-roll buffer. Safe to mutate the
        returned list. Each chunk inside is a numpy array — do not mutate
        those (they are still referenced by the deque)."""
        with self._lock:
            return list(self._preroll)

    def add_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass
