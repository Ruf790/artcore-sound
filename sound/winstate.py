"""Remembering the windows (1.0): the size and place of every resizable dialog (the effects, statistics, spectrum,
recording, regions, hotkeys) by its class, kept in the settings and given back the next time it opens. main.py's
event filter calls restore() on the first show and save() when a window hides."""
from __future__ import annotations

from PySide6.QtCore import QByteArray

REMEMBER = {"EffectDialog", "StatsDialog", "SpectrumDialog", "SpectrogramDialog", "RecordDialog", "RegionsDialog",
            "KeysDialog", "StepDialog", "RecoveryDialog"}
cfg: dict | None = None             # the settings of the running program (main.py sets it)


def _key(w) -> str:
    name = type(w).__name__
    if name == "EffectDialog":                      # each effect keeps its own size (the EQ is wide, delay small)
        name += ":" + str(getattr(w, "key", ""))
    return name


def wanted(w) -> bool:
    return cfg is not None and type(w).__name__ in REMEMBER


def restore(w) -> bool:
    if not wanted(w):
        return False
    geo = (cfg.get("dialogs") or {}).get(_key(w))
    if not geo:
        return False
    return bool(w.restoreGeometry(QByteArray.fromBase64(geo.encode())))


def save(w) -> None:
    if not wanted(w):
        return
    from . import config
    cfg.setdefault("dialogs", {})[_key(w)] = bytes(w.saveGeometry().toBase64()).decode()
    config.save(cfg)
