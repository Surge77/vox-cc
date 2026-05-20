"""
runtime_hook_path.py — PyInstaller runtime hook.

Prepends the frozen app's extraction directory (_MEIPASS) to PATH so that
bundled DLLs (CUDA, MKL, OpenMP, ctranslate2) are found before any
system-wide CUDA installation.

Without this, a system CUDA toolkit in PATH can shadow the bundled DLLs,
causing WinError 1114 (DLL initialization failed) at startup.
"""
import os
import sys

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Prepend so our bundled DLLs always win over system PATH entries
    meipass = sys._MEIPASS
    current_path = os.environ.get("PATH", "")
    if meipass not in current_path:
        os.environ["PATH"] = meipass + os.pathsep + current_path
