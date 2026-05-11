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
            devices.append({
                "index": i,
                "name": name,
                "default": i == default_idx,
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


class AudioCapture:
    """
    Thin wrapper around PyAudio for 16kHz mono float32 capture.
    Full implementation in M2 (dictation router).
    """

    def __init__(self, device_index: int | None = None):
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None
        self._device_index = device_index

    def open(self) -> None:
        self._pa = pyaudio.PyAudio()
        kwargs = dict(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
        )
        if self._device_index is not None:
            kwargs["input_device_index"] = self._device_index
        self._stream = self._pa.open(**kwargs)

    def read_chunk(self) -> np.ndarray:
        if self._stream is None:
            raise RuntimeError("AudioCapture not opened")
        raw = self._stream.read(CHUNK_FRAMES, exception_on_overflow=False)
        return np.frombuffer(raw, dtype=np.float32).copy()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
