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
    # effects (0.3)
    "fx_last":       {},            # effect key → the values last applied
    "fx_presets":    {},            # effect key → {name: values} (the user's own presets; "vst3:<id>" a plugin's)
    "vst3_folders":  [],            # 2.0: where the VST3 plugins are looked for ([] — the standard folders)
    "vst3_recent":   [],            # the plugins last opened (their keys), first in the menu
    "vst_preset_dir": "",           # the folder of the .vstpreset files last opened or saved
    # 2.0: Audio CD
    "cd_drive":      "",            # the drive last ripped from
    "cd_rip_mode":   "files",       # files | tracks (into the editor) | disc (one file, a region per track)
    "cd_rip_format": "flac",
    "cd_rip_option": {},            # format → option
    "cd_rip_folder": "",
    "cd_rip_template": "{artist} - {album}/{n:2} - {title}",
    "cd_secure":     True,          # each track read twice, the pieces that differ read again
    "cd_offset":     0,             # the drive's read offset, frames (AccurateRip's list)
    "cd_mb_auto":    True,          # no CD-TEXT: MusicBrainz asked at once
    "cd_gap":        2.0,           # seconds between the burnt tracks
    "cd_cdtext":     True,
    "cd_translit":   True,          # CD-TEXT has no Cyrillic: Latin letters
    "cd_burn_mode":  "dao",         # dao (raw image, CD-TEXT) | tao (track by track)
    "cd_speed":      0,             # ×, 0 — the fastest
    "cd_burn_artist": "",
    "fx_keep_tail":  True,          # delay / reverb: ring on past the selection
    # analysis, markers, recording (0.4)
    "view_mode":     "wave",        # wave | spec | both
    "spectrogram":   {},            # spectrogram.Settings
    "spectrum":      {},            # spectrum.Settings of Tools → Spectrum
    "spectrum_panel": {},           # … of the panel at the bottom
    "spectrum_panel_on": True,
    "side_split":    [],            # the meters / the marker list
    "record":        {},            # the Record window: device, channels, rate, pre-roll, trigger
    "markers_dir":   "",
    # 1.0
    "main_split":    [],            # the waveform | the side panel
    "dialogs":       {},            # the size and place of each resizable dialog (winstate.py)
    "gain_db":       3.0,           # Process → Volume
    "silence_s":     1.0,           # Edit → Insert silence
    "resample_rate": 0,             # Process → Sample rate
    "channels_to":   0,             # Process → Channels
    "batch_last":    {},            # the batch window's job when it was last closed
    "batch_geometry": "",
    "batch_split":   [],
    "noise_dir":     "",            # where noise samples (*.acnoise) were saved / loaded
    # 2.0
    "play_volume_db": 0.0,          # the listening volume (only what is heard, never the file)
    "play_muted":    False,
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
    if cfg.get("view_mode") not in ("wave", "spec", "both"):
        cfg["view_mode"] = "wave"
    cfg["spectrum_panel_on"] = bool(cfg.get("spectrum_panel_on"))
    for k in ("save_opts", "fx_last", "fx_presets", "spectrogram", "spectrum", "spectrum_panel", "record"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    cfg["fx_keep_tail"] = bool(cfg.get("fx_keep_tail"))
    for k in ("loop", "follow", "return_cursor", "check_updates"):
        cfg[k] = bool(cfg.get(k))
    for k in ("device", "temp_dir", "last_dir", "save_dir", "save_format", "vst_preset_dir"):
        if not isinstance(cfg.get(k), str):
            cfg[k] = DEFAULTS[k]
    for k in ("vst3_folders", "vst3_recent"):          # 2.0
        v = cfg.get(k)
        cfg[k] = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return cfg


def save(cfg: dict) -> None:
    write("config.json", cfg)


def add_recent(cfg: dict, path: str) -> None:
    path = os.path.abspath(path)
    rec = [p for p in cfg.get("recent", []) if os.path.normcase(p) != os.path.normcase(path)]
    cfg["recent"] = [path] + rec[:RECENT_MAX - 1]
