"""Windows bits: dark title bar, taskbar id, the installer mutex, ffmpeg without a console window, Explorer.
A no-op elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"
MUTEX_NAME = "ArtCoreSoundMutex"       # same as AppMutex in setup.iss
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW
_mutex = None


def popen_flags() -> dict:
    return {"creationflags": NO_WINDOW} if IS_WIN else {}


def init_process(app_id: str) -> None:
    if not IS_WIN:
        return
    import ctypes
    global _mutex
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass
    try:
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except Exception:
        pass


def release_mutex() -> None:
    """Let the installer run (it waits for AppMutex) — before a self-update."""
    global _mutex
    if IS_WIN and _mutex:
        import ctypes
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex)
        except Exception:
            pass
        _mutex = None


def dark_title_bar(win_id: int, dark: bool = True) -> None:
    """DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 on early Windows 10 builds)."""
    if not IS_WIN:
        return
    import ctypes
    val = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):
        try:
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(win_id, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        except Exception:
            pass


def show_in_explorer(path: str) -> None:
    if IS_WIN:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(os.path.normpath(path if os.path.isdir(path) else os.path.dirname(path)))
    else:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            subprocess.Popen(["xdg-open", target])
        except OSError:
            pass


def free_space(path: str) -> int:
    """Bytes free on the drive of path (-1 when unknown)."""
    import shutil
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1
