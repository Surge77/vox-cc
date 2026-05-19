"""
Stub out heavy ML/audio dependencies so sidecar modules can be imported in
a plain pytest environment that has no GPU, no PyAudio, no torch install.
"""
import sys
import types
from unittest.mock import MagicMock

_HEAVY_DEPS = [
    "torch",
    "noisereduce",
    "faster_whisper",
    "llama_cpp",
    "bitsandbytes",
    "transformers",
    "peft",
    "groq",
    "pyaudio",
    "scipy",
    "scipy.signal",
    "uvicorn",
    "huggingface_hub",
    # Stub only the hardware-dependent leaf — NOT the "audio" package itself.
    # audio.vad and audio.pipeline are real modules; they see the stubs above.
    "audio.capture",
    "models",
    "models.dual_loader",
    "models.prompt_router",
    "models.llm_engine",
    "training",
    "training.train",
]

for _dep in _HEAVY_DEPS:
    if _dep not in sys.modules:
        sys.modules[_dep] = MagicMock()
