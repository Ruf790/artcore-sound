"""The equalizers: biquads after R. Bristow-Johnson's «Audio EQ Cookbook» run by scipy's sosfilt with its state kept
between chunks. Parametric: 7 bands, each off or a high-pass, low shelf, bell, high shelf or low-pass with its
frequency, gain and Q. Graphic: 10 bells on the octaves 31.5 Hz … 16 kHz. Every band is always one section (a band
that is off is a pass-through), so moving a band on the fly keeps the filter state of the others."""
from __future__ import annotations

import math

import numpy as np
from scipy import signal

from .base import Effect, Param, Processor, undb

TYPES = ("hp", "lowshelf", "peak", "highshelf", "lp")
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def biquad(kind: str, f: float, gain_db: float, q: float, rate: int) -> tuple:
    """One RBJ section (b0, b1, b2, 1, a1, a2). A frequency over 0.49 × rate is held there; a low-pass that high,
    or a bell / shelf of 0 dB, is a pass-through."""
    nyq = rate / 2.0
    if kind == "lp" and f >= 0.98 * nyq:
        return IDENTITY
    f = min(max(f, 1.0), 0.49 * rate)
    if kind in ("peak", "lowshelf", "highshelf") and abs(gain_db) < 1e-6:
        return IDENTITY
    q = max(q, 0.05)
    w0 = 2 * math.pi * f / rate
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    A = 10 ** (gain_db / 40.0)                                           # noqa: N806
    if kind == "peak":
        b = (1 + alpha * A, -2 * cw, 1 - alpha * A)
        a = (1 + alpha / A, -2 * cw, 1 - alpha / A)
    elif kind == "lowshelf":
        sa = 2 * math.sqrt(A) * alpha
        b = (A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa))
        a = ((A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa)
    elif kind == "highshelf":
        sa = 2 * math.sqrt(A) * alpha
        b = (A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa))
        a = ((A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa)
    elif kind == "lp":
        b = ((1 - cw) / 2, 1 - cw, (1 - cw) / 2)
        a = (1 + alpha, -2 * cw, 1 - alpha)
    elif kind == "hp":
        b = ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2)
        a = (1 + alpha, -2 * cw, 1 - alpha)
    else:
        return IDENTITY
    a0 = a[0]
    return (b[0] / a0, b[1] / a0, b[2] / a0, 1.0, a[1] / a0, a[2] / a0)


class EqProcessor(Processor):
    """bands() → [(kind, f, gain, q)] per section, from the effect; the sections and the output gain."""

    def configure(self, old: dict) -> None:
        sos = np.array([biquad(k, f, g, q, self.rate) for k, f, g, q in self.effect.bands(self.p)], np.float64)
        if not len(sos):
            sos = np.array([IDENTITY])
        if not old or getattr(self, "zi", None) is None or self.zi.shape[0] != len(sos):
            self.zi = np.zeros((len(sos), 2, self.ch))
        self.sos = sos
        self.gain = float(undb(self.p.get("out", 0.0)))

    def _process(self, x):
        y, self.zi = signal.sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y * self.gain if self.gain != 1.0 else y

    def response(self, freqs) -> np.ndarray:
        """|H| in dB at the frequencies (the graph)."""
        return response_db(self.sos, freqs, self.rate) + 20 * math.log10(self.gain)


def response_db(sos, freqs, rate: int) -> np.ndarray:
    _w, h = signal.sosfreqz(sos, worN=np.asarray(freqs, np.float64), fs=rate)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))


def sos_of(effect, values: dict, rate: int) -> np.ndarray:
    return np.array([biquad(k, f, g, q, rate) for k, f, g, q in effect.bands(effect.clean(values))])


# ── parametric ────────────────────────────────────────────────
N_BANDS = 7
BAND_DEFAULTS = (("hp", 30.0, 0.0, 0.71, False), ("lowshelf", 100.0, 0.0, 0.71, True),
                 ("peak", 300.0, 0.0, 1.0, True), ("peak", 1000.0, 0.0, 1.0, True),
                 ("peak", 3500.0, 0.0, 1.0, True), ("highshelf", 9000.0, 0.0, 0.71, True),
                 ("lp", 18000.0, 0.0, 0.71, False))
TYPE_CHOICES = tuple((k, "eq_t_" + k) for k in TYPES)


def _band_params():
    out = []
    for i, (kind, f, g, q, on) in enumerate(BAND_DEFAULTS, 1):
        grp = f"b{i}"
        out += [Param(f"b{i}_on", "bool", default=on, label="eq_on", group=grp),
                Param(f"b{i}_type", "choice", default=kind, choices=TYPE_CHOICES, label="eq_type", group=grp),
                Param(f"b{i}_freq", lo=20.0, hi=20000.0, default=f, unit="Hz", decimals=0, log=True,
                      label="eq_freq", group=grp),
                Param(f"b{i}_gain", lo=-24.0, hi=24.0, default=g, unit="dB", decimals=1, label="eq_gain", group=grp),
                Param(f"b{i}_q", lo=0.1, hi=18.0, default=q, decimals=2, log=True, label="eq_q", group=grp)]
    return out


def _eq_preset(**bands):
    """b3=(gain), b2=(gain, freq)… → the values of the bands named; the rest at the defaults (gain 0)."""
    v = {}
    for k, spec in bands.items():
        if k == "out":
            v["out"] = spec
            continue
        if not isinstance(spec, tuple):
            spec = (spec,)
        v[f"{k}_on"] = True
        v[f"{k}_gain"] = spec[0]
        if len(spec) > 1:
            v[f"{k}_freq"] = spec[1]
        if len(spec) > 2:
            v[f"{k}_q"] = spec[2]
    return v


class ParametricEQ(Effect):
    key = "eq"
    name = "fx_eq"
    params = _band_params() + [Param("out", lo=-24.0, hi=24.0, default=0.0, unit="dB", label="fx_out")]
    proc_class = EqProcessor
    presets = {
        "pr_flat": {},
        "pr_voice": {"b1_on": True, "b1_freq": 80.0, **_eq_preset(b3=(-3.0, 250.0, 1.0), b5=(3.0, 3500.0, 0.9),
                                                                  b6=(2.0, 10000.0))},
        "pr_bass": _eq_preset(b2=(6.0, 90.0), b3=(-2.0, 350.0, 1.2)),
        "pr_bright": _eq_preset(b5=(2.0, 4000.0, 0.8), b6=(5.0, 9000.0)),
        "pr_warm": _eq_preset(b2=(3.0, 150.0), b6=(-4.0, 7000.0)),
        "pr_rumble": {"b1_on": True, "b1_freq": 80.0, "b1_q": 0.71},
        "pr_phone": {"b1_on": True, "b1_freq": 400.0, "b7_on": True, "b7_freq": 3000.0, "b7_type": "lp",
                     **_eq_preset(b4=(6.0, 1500.0, 1.0))},
    }

    def bands(self, p: dict):
        out = []
        for i in range(1, N_BANDS + 1):
            if p[f"b{i}_on"]:
                out.append((p[f"b{i}_type"], p[f"b{i}_freq"], p[f"b{i}_gain"], p[f"b{i}_q"]))
            else:
                out.append(("off", 1000.0, 0.0, 1.0))
        return out


# ── graphic ───────────────────────────────────────────────────
GEQ_FREQS = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
GEQ_Q = 1.41                    # one octave between the −3 dB points of a band


def _geq_label(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


class GraphicEQ(Effect):
    key = "geq"
    name = "fx_geq"
    params = [Param(f"g{i}", lo=-12.0, hi=12.0, default=0.0, unit="dB", label="", group=_geq_label(f))
              for i, f in enumerate(GEQ_FREQS)] + \
        [Param("out", lo=-24.0, hi=24.0, default=0.0, unit="dB", label="fx_out")]
    proc_class = EqProcessor
    presets = {
        "pr_flat": {},
        "pr_loudness": {"g0": 6.0, "g1": 4.0, "g2": 2.0, "g7": 1.5, "g8": 3.0, "g9": 4.0, "out": -3.0},
        "pr_bass": {"g0": 5.0, "g1": 5.0, "g2": 3.0, "g3": 1.0},
        "pr_treble": {"g7": 2.0, "g8": 4.0, "g9": 5.0},
        "pr_voice": {"g0": -6.0, "g1": -3.0, "g5": 1.0, "g6": 3.0, "g7": 3.0, "g8": 1.0},
        "pr_vshape": {"g0": 4.0, "g1": 3.0, "g2": 1.0, "g4": -2.0, "g5": -2.0, "g7": 1.0, "g8": 3.0, "g9": 4.0},
    }

    def bands(self, p: dict):
        return [("peak", f, p[f"g{i}"], GEQ_Q) for i, f in enumerate(GEQ_FREQS)]
