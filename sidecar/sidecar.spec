# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all

SIDECAR_DIR = os.path.abspath(".")  # always run pyinstaller from sidecar/
VENV_SITE   = os.path.join(SIDECAR_DIR, ".venv", "Lib", "site-packages")
TORCH_LIB   = os.path.join(VENV_SITE, "torch", "lib")
LLAMA_LIB   = os.path.join(VENV_SITE, "llama_cpp", "lib")

# Collect before Analysis — all return (src, dest) 2-tuples; Analysis normalises to 3-tuples
# Must NOT add to a.binaries post-Analysis (already 3-tuples → mixing crashes PyInstaller 6.x)
ct2_binaries = collect_dynamic_libs("ctranslate2")
nr_datas, nr_binaries, nr_hidden = collect_all("noisereduce")

a = Analysis(
    ["main.py"],
    pathex=[SIDECAR_DIR],
    binaries=[
        # CUDA runtime — sourced from torch/lib (no standalone CUDA toolkit on this machine)
        (os.path.join(TORCH_LIB, "cudart64_12.dll"),          "."),
        (os.path.join(TORCH_LIB, "cublas64_12.dll"),          "."),
        (os.path.join(TORCH_LIB, "cublasLt64_12.dll"),        "."),
        (os.path.join(TORCH_LIB, "nvrtc64_120_0.dll"),        "."),
        (os.path.join(TORCH_LIB, "cufft64_11.dll"),           "."),
        (os.path.join(TORCH_LIB, "nvrtc-builtins64_121.dll"), "."),
        # llama_cpp native libs (must preserve subdir path)
        (os.path.join(LLAMA_LIB, "ggml.dll"),  "llama_cpp/lib"),
        (os.path.join(LLAMA_LIB, "llama.dll"), "llama_cpp/lib"),
        (os.path.join(LLAMA_LIB, "llava.dll"), "llama_cpp/lib"),
    ] + ct2_binaries + nr_binaries,
    datas=[
        ("prompts/*.txt", "prompts"),
        # Models NOT bundled — copy manually after build via: xcopy /E /I models dist\sidecar\models
    ] + nr_datas,
    hiddenimports=[
        # faster-whisper / ctranslate2
        "faster_whisper",
        "ctranslate2",
        "ctranslate2.converters",
        # llama_cpp
        "llama_cpp",
        "llama_cpp.llama_cpp",
        # ML / audio
        "noisereduce",
        "scipy.signal",
        "scipy._lib.messagestream",
        "scipy.special._ufuncs_cxx",
        # cloud
        "groq",
        # fine-tuning libs (imported by training subprocess at runtime)
        "peft",
        "peft.tuners.lora",
        "transformers",
        "transformers.models.whisper",
        # audio
        "pyaudio",
        # uvicorn lazy-loaded protocols (all required — none auto-discovered)
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # fastapi / websockets
        "fastapi",
        "websockets",
        # local sidecar modules
        "routers.health",
        "routers.dictation",
        "routers.text_processing",
        "routers.vocabulary",
        "routers.finetuning",
        "models.dual_loader",
        "models.llm_engine",
        "models.prompt_router",
        "audio.capture",
        "audio.pipeline",
        "audio.vad",
        "training",
        "training.train",
    ] + nr_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sidecar",
    debug=False,
    strip=False,
    upx=False,   # UPX breaks CUDA DLLs — never enable
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="sidecar",
)
