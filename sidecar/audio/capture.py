import pyaudio
import numpy as np
from scipy.signal import resample_poly

SAMPLE_RATE = 16000
CHUNK_FRAMES = 1024
FORMAT = pyaudio.paFloat32
CHANNELS = 1


def list_audio_devices() -> list[dict]:
    pa = pyaudio.PyAudio()
    devices = []
    try:
        default_idx = pa.get_default_input_device_info()["index"]
    except OSError:
        default_idx = -1
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            name = info["name"]
            # PyAudio on Windows decodes device names as CP1252; fix UTF-8 double-encode
            try:
                name = name.encode("cp1252").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
            bt_keywords = ("bluetooth", " bt ", "airpods", "airplay", "wireless")
            is_bt = any(kw in name.lower() for kw in bt_keywords)
            devices.append({
                "index": i,
                "name": name,
                "default": i == default_idx,
                "bluetooth_warning": is_bt,
            })
    pa.terminate()
    return devices


def check_mic_permission() -> None:
    pa = pyaudio.PyAudio()
    count = pa.get_device_count()
    has_input = False
    for i in range(count):
        try:
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                has_input = True
                break
        except OSError:
            continue
    pa.terminate()
    if not has_input:
        raise RuntimeError(
            "No microphone input detected. Check Windows Privacy → Microphone settings."
        )


def resample_to_16k(audio_raw: np.ndarray, original_rate: int) -> np.ndarray:
    if original_rate == SAMPLE_RATE:
        return audio_raw.astype(np.float32)
    return resample_poly(audio_raw, SAMPLE_RATE, original_rate).astype(np.float32)


def normalize_int16(audio: np.ndarray) -> np.ndarray:
    return (audio / 32768.0).astype(np.float32)


def _wasapi_default_input(pa: pyaudio.PyAudio) -> int | None:
    """Return WASAPI host API's default input device index, or None if unavailable."""
    for i in range(pa.get_host_api_count()):
        info = pa.get_host_api_info_by_index(i)
        if info.get("type") == pyaudio.paWASAPI:
            dev = info.get("defaultInputDevice", -1)
            return int(dev) if dev >= 0 else None
    return None


class AudioCapture:
    """
    16kHz mono float32 capture with device/format fallback chain:
      1. WASAPI default, paFloat32
      2. WASAPI default, paInt16  (Intel SST array requires int16 in shared mode)
      3. MME default,   paFloat32
      4. MME default,   paInt16
    Opens at device native sample rate, resamples to 16kHz in read_chunk.
    """

    def __init__(self, device_index: int | None = None):
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None
        self._device_index = device_index
        self._native_rate: int = SAMPLE_RATE
        self._native_chunk: int = CHUNK_FRAMES
        self._using_int16: bool = False

    def open(self) -> None:
        self._pa = pyaudio.PyAudio()

        if self._device_index is not None:
            candidates = [self._device_index]
        else:
            candidates: list[int | None] = []
            wasapi_dev = _wasapi_default_input(self._pa)
            if wasapi_dev is not None:
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
        for dev_idx in candidates:
            native_rate = SAMPLE_RATE
            if dev_idx is not None:
                try:
                    info = self._pa.get_device_info_by_index(dev_idx)
                    native_rate = int(info.get("defaultSampleRate", SAMPLE_RATE))
                except OSError:
                    continue

            native_chunk = int(CHUNK_FRAMES * native_rate / SAMPLE_RATE)
            base_kwargs = dict(channels=CHANNELS, rate=native_rate, input=True,
                               frames_per_buffer=native_chunk)
            if dev_idx is not None:
                base_kwargs["input_device_index"] = dev_idx

            for fmt, is_int16 in [(pyaudio.paFloat32, False), (pyaudio.paInt16, True)]:
                try:
                    self._stream = self._pa.open(format=fmt, **base_kwargs)
                    self._native_rate = native_rate
                    self._native_chunk = native_chunk
                    self._using_int16 = is_int16
                    name = (self._pa.get_device_info_by_index(dev_idx).get("name", "?")
                            if dev_idx is not None else "system default")
                    fmt_str = "int16" if is_int16 else "float32"
                    print(f"[AudioCapture] {name} idx={dev_idx} {native_rate}Hz {fmt_str}")
                    return
                except OSError as e:
                    last_err = e
                    continue

        raise RuntimeError(f"Could not open any audio input device: {last_err}")

    def read_chunk(self) -> np.ndarray:
        if self._stream is None:
            raise RuntimeError("AudioCapture not opened")
        raw = self._stream.read(self._native_chunk, exception_on_overflow=False)
        if self._using_int16:
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            audio = np.frombuffer(raw, dtype=np.float32).copy()
        if self._native_rate != SAMPLE_RATE:
            audio = resample_to_16k(audio, self._native_rate)
        return audio

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except OSError:
                pass
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except OSError:
                pass
            self._pa = None
