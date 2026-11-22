"""Settings in %APPDATA%\\ArtCoreSound\\config.json; the working files (the sound of open documents, undo data,
peaks) in a temp folder. A portable.ini next to the exe keeps the settings in .\\Data instead."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

DEFAULTS = {
    "language":      "ru",
    "theme":         "dark",        # dark | light | auto (as Windows apps)
    "look":          "classic",     # classic (flat, square, compact) | modern (rounded)
    "check_updates": True,
    "last_update_check": 0,
    "update_notified": "",
    "geometry":      "",
    "splitter":      [],
    "last_dir":      "",
    "save_dir":      "",
    "recent":        [],            # the last opened files (Файл → Последние)
    "temp_dir":      "",            # where the working files go ("" = %TEMP%\ArtCoreSound)
    # playback
    "device":        "",            # output device name ("" = the Windows default, WASAPI)
    "latency":       "low",         # low | high (PortAudio's suggested latency)
    "loop":          False,
    "follow":        True,          # the view scrolls with the play cursor
    "return_cursor": True,          # stop → the cursor goes back to where playback started
    # saving
    "save_format":   "wav",
    "save_opts":     {},            # format → {"bits": "24", "bitrate": 320, …}
}

RECENT_MAX = 12


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable() -> bool:
    return os.path.isfile(os.path.join(app_dir(), "portable.ini"))


def data_dir() -> str:
    override = os.environ.get("ARTCORE_SOUND_DATA")          # tests
    if override:
        path = override
    elif is_portable():
        path = os.path.join(app_dir(), "Data")
    else:
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, "ArtCoreSound")
    os.makedirs(path, exist_ok=True)
    return path


def work_root(cfg: dict | None = None) -> str:
    """The folder for the working files of this run (a subfolder per process, removed on exit)."""
    base = (cfg or {}).get("temp_dir") or os.environ.get("ARTCORE_SOUND_TEMP") or \
        os.path.join(tempfile.gettempdir(), "ArtCoreSound")
    os.makedirs(base, exist_ok=True)
    return base


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def read(name: str):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write(name: str, data) -> None:
    path = _path(name)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    saved = read("config.json")
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    if cfg.get("language") not in ("ru", "en"):
        cfg["language"] = "ru"
    if cfg.get("theme") not in ("dark", "light", "auto"):
        cfg["theme"] = "dark"
    if cfg.get("look") not in ("classic", "modern"):
        cfg["look"] = "classic"
    if cfg.get("latency") not in ("low", "high"):
        cfg["latency"] = "low"
    if not isinstance(cfg.get("recent"), list):
        cfg["recent"] = []
    cfg["recent"] = [p for p in cfg["recent"] if isinstance(p, str)][:RECENT_MAX]
    if not isinstance(cfg.get("save_opts"), dict):
        cfg["save_opts"] = {}
    for k in ("loop", "follow", "return_cursor", "check_updates"):
        cfg[k] = bool(cfg.get(k))
    for k in ("device", "temp_dir", "last_dir", "save_dir", "save_format"):
        if not isinstance(cfg.get(k), str):
            cfg[k] = DEFAULTS[k]
    return cfg


def save(cfg: dict) -> None:
    write("config.json", cfg)


def add_recent(cfg: dict, path: str) -> None:
    path = os.path.abspath(path)
    rec = [p for p in cfg.get("recent", []) if os.path.normcase(p) != os.path.normcase(path)]
    cfg["recent"] = [path] + rec[:RECENT_MAX - 1]
