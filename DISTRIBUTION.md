# Vox — Distribution Guide

Vox ships in two parts because the ML runtime (`_internal/`) exceeds the NSIS 2 GB installer limit.

## Downloads

| File | Size | Contents |
|------|------|----------|
| `Vox-setup.exe` | ~68 MB | Vox app + sidecar launcher |
| `vox-sidecar-internal.zip` | ~12 GB | Python runtime, CUDA DLLs, torch, ctranslate2, llama-cpp |

Both files must be present for Vox to work. Models are a separate download (see below).

---

## Installation Steps

1. Run `Vox-setup.exe` — installs to `%LOCALAPPDATA%\vox\`
2. Unzip `vox-sidecar-internal.zip` into the install folder:
   - The zip contains one folder named `_internal`
   - After unzip, `%LOCALAPPDATA%\vox\_internal\` must exist
   - `sidecar.exe` and `_internal\` must be siblings in the same directory

**Verify the path is correct:**
```
%LOCALAPPDATA%\vox\
  Vox.exe
  sidecar.exe        ← copied by installer
  _internal\         ← from the zip
    python311.dll
    _ctranslate2.pyd
    ...
```

3. Download models and place in `%LOCALAPPDATA%\vox\_internal\models\`:
   - `whisper-large-v3-turbo-ct2\` — streaming ASR (~800 MB)
   - `models--Systran--faster-distil-whisper-large-v3\` — final pass ASR (~1.5 GB)
   - `qwen2.5-3b-instruct-q4_k_m.gguf` — LLM post-processing (~2 GB)

---

## Prerequisites

| Requirement | Where to get |
|-------------|-------------|
| Windows 10/11 x64 | — |
| NVIDIA GPU (GTX 1650 or better) | — |
| NVIDIA Driver 591+ | nvidia.com/drivers |
| CUDA Toolkit 12.1 | developer.nvidia.com/cuda-12-1-0-download-archive |
| cuDNN 8.9 | developer.nvidia.com/cudnn |
| Microsoft Edge WebView2 | Installed automatically by `Vox-setup.exe` |
| Visual C++ Redistributable 2022 | aka.ms/vs/17/release/vc_redist.x64.exe |

---

## Windows Defender

Windows Defender may flag `sidecar.exe` or files in `_internal\` as suspicious. This is a false positive caused by PyInstaller's self-extracting bundle format.

**Add an exclusion before unzipping:**

1. Open Windows Security → Virus & threat protection → Manage settings
2. Scroll to Exclusions → Add or remove exclusions
3. Add folder: `%LOCALAPPDATA%\vox`

---

## SmartScreen Warning on First Launch

If Windows SmartScreen blocks `Vox-setup.exe`:

1. Right-click `Vox-setup.exe` → Properties
2. Check "Unblock" at the bottom → Apply
3. Run the installer again

---

## Packaging a New Release (for developers)

```powershell
# 1. Build the Python sidecar
cd sidecar
.\.venv\Scripts\activate
pyinstaller sidecar.spec

# 2. Copy sidecar.exe to Tauri binaries
copy dist\sidecar\sidecar.exe ..\src-tauri\binaries\sidecar-x86_64-pc-windows-msvc.exe

# 3. Build the Tauri installer
cd ..
cargo tauri build        # produces src-tauri/target/release/bundle/nsis/Vox_*_x64-setup.exe

# 4. Package _internal/ as a separate download
npm run pack-sidecar     # produces sidecar/dist/vox-sidecar-internal.zip
```

Ship both `Vox_*_x64-setup.exe` and `vox-sidecar-internal.zip` together.

---

## Fine-Tuning Note

The in-app fine-tuning pipeline produces a merged HuggingFace model checkpoint. The CT2 quantization step (`ct2-transformers-converter`) requires a Python environment with the converter installed — it is not available inside the frozen sidecar binary. To quantize a fine-tuned model for use in Vox:

```powershell
pip install ctranslate2
ct2-transformers-converter --model <merged-model-dir> --quantization int8 --output_dir <ct2-output-dir>
```

Then replace `_internal\models\models--Systran--faster-distil-whisper-large-v3\` with the CT2 output.
