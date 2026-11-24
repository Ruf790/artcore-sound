"""The film effects (2.0): the channel matrix and the dialogue boost — in the effects' frame, so they have the live
preview, presets and batch steps, and give the same result whole or in chunks (chans.py has the rest of the 5.1
tools)."""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

from .base import Effect, Param, Processor

MAXC = 8
OFF_DB = -60.0                       # a matrix cell at its bottom: no signal


# ── the channel matrix ────────────────────────────────────────
def _cell(o: int, i: int) -> str:
    return f"m{o}{i}"


def _matrix_params() -> list:
    ps = []
    for o in range(MAXC):
        for i in range(MAXC):
            ps.append(Param(_cell(o, i), lo=OFF_DB, hi=12.0, default=0.0 if o == i else OFF_DB, unit="dB",
                            decimals=1, label="cm_cell"))
    ps.append(Param("normalize", "bool", default=False, label="cm_normalize"))
    return ps


def identity(n: int = MAXC) -> dict:
    return {_cell(o, i): (0.0 if o == i else OFF_DB) for o in range(MAXC) for i in range(MAXC)}


def _swap(a: int, b: int) -> dict:
    v = identity()
    v[_cell(a, a)] = v[_cell(b, b)] = OFF_DB
    v[_cell(a, b)] = v[_cell(b, a)] = 0.0
    return v


def _mute(k: int) -> dict:
    v = identity()
    v[_cell(k, k)] = OFF_DB
    return v


def gains(values: dict, n: int) -> np.ndarray:
    """(n inputs × n outputs) linear gains."""
    g = np.zeros((n, n))
    for o in range(n):
        for i in range(n):
            db = float(values.get(_cell(o, i), OFF_DB))
            g[i, o] = 0.0 if db <= OFF_DB else 10 ** (db / 20.0)
    if values.get("normalize"):
        s = np.abs(g).sum(axis=0).max()
        if s > 1.0:
            g /= s
    return g


class _MatrixProc(Processor):
    def configure(self, old: dict) -> None:
        self.g = gains(self.p, self.ch)

    def _process(self, x: np.ndarray) -> np.ndarray:
        return x @ self.g


class ChannelMatrix(Effect):
    key = "chmatrix"
    name = "cm_title"
    params = _matrix_params()
    proc_class = _MatrixProc
    presets = {"cm_p_identity": identity(), "cm_p_swap_lr": _swap(0, 1), "cm_p_swap_c_lfe": _swap(2, 3),
               "cm_p_swap_sides": _swap(4, 5), "cm_p_mute_lfe": _mute(3),
               "cm_p_center_phantom": {**_mute(2), _cell(0, 2): -3.0, _cell(1, 2): -3.0}}

    def label(self, values: dict) -> str:
        from .i18n import t
        return t(self.name)


# ── dialog boost ──────────────────────────────────────────────
class _DialogProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.gb = 10 ** (p["boost"] / 20.0)
        self.go = 10 ** (p["others"] / 20.0)
        key = (p["lo"], p["hi"], self.rate)
        if not old or (old.get("lo"), old.get("hi")) != key[:2] or not hasattr(self, "sos"):
            hi = min(p["hi"], self.rate * 0.45)
            lo = min(p["lo"], hi * 0.9)
            self.sos = butter(2, [lo, hi], btype="bandpass", fs=self.rate, output="sos")
            self.zi = np.zeros((self.sos.shape[0], 2))
        self.labs = [s.upper() for s in self.labels]

    def _process(self, x: np.ndarray) -> np.ndarray:
        y = x.copy()
        if "C" in self.labs and self.ch >= 3:
            c = self.labs.index("C")
            for k in range(self.ch):
                if k == c:
                    y[:, k] = x[:, k] * self.gb
                elif self.labs[k] != "LFE":
                    y[:, k] = x[:, k] * self.go
            return y
        if self.ch == 2:
            m = 0.5 * (x[:, 0] + x[:, 1])
            s = 0.5 * (x[:, 0] - x[:, 1])
            band, self.zi = sosfilt(self.sos, m, zi=self.zi)
            m2 = (m - band) * self.go + band * self.gb
            s2 = s * self.go
            y[:, 0], y[:, 1] = m2 + s2, m2 - s2
            return y
        band, self.zi = sosfilt(self.sos, x[:, 0], zi=self.zi)
        y[:, 0] = (x[:, 0] - band) * self.go + band * self.gb
        if self.ch > 1:
            y[:, 1:] = x[:, 1:] * self.go
        return y


class DialogBoost(Effect):
    key = "dialog"
    name = "db_title"
    params = [Param("boost", lo=0.0, hi=15.0, default=6.0, unit="dB", decimals=1, label="db_boost"),
              Param("others", lo=-15.0, hi=0.0, default=-3.0, unit="dB", decimals=1, label="db_others"),
              Param("lo", lo=80.0, hi=1000.0, default=200.0, unit="Hz", decimals=0, log=True, label="db_lo"),
              Param("hi", lo=1500.0, hi=12000.0, default=4000.0, unit="Hz", decimals=0, log=True, label="db_hi")]
    proc_class = _DialogProc
    presets = {"db_p_soft": {"boost": 3.0, "others": -1.5, "lo": 200.0, "hi": 4000.0},
               "db_p_strong": {"boost": 8.0, "others": -4.0, "lo": 250.0, "hi": 3500.0},
               "db_p_night": {"boost": 6.0, "others": -8.0, "lo": 200.0, "hi": 5000.0}}
