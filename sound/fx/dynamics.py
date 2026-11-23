"""The compressor and the limiter, sample-accurate and without a loop over the samples.

The compressor: the level of each sample (peak), the static curve (threshold, ratio, a soft knee of W dB) gives the
gain reduction R wanted; the channels are linked by `link` (100 %: all follow the loudest); the release is the peak
hold R'[t] = max(R[t], r·R'[t−1]) (exact, computed as a running maximum in the log domain — base.release_hold), the
attack a one-pole low-pass on R' («release, then attack»: the decoupled detector of Giannoulis, Massberg & Reiss).

The limiter looks ahead L frames: the reduction needed (the level over the ceiling, all channels linked; with true
peak from 4× oversampling) → its maximum over the last L frames → the release hold → its average over the last L
frames, and the sound delayed by L − 1. Every reduction averaged for a frame is at least the one it needs, so the
output never goes over the ceiling (the sample peak exactly; the true peak to within a few hundredths of a dB)."""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage, signal

from ..analysis import OVERSAMPLE, _tp_filter
from .base import Effect, Fifo, History, Param, Processor, db, release_hold, undb


def static_gr(level_db: np.ndarray, threshold: float, ratio: float, knee: float) -> np.ndarray:
    """The gain reduction (dB, ≥ 0) of the compressor's static curve for input levels in dB."""
    d = level_db - threshold
    slope = 1.0 - 1.0 / ratio
    gr = np.where(d > 0, d * slope, 0.0)
    if knee > 0:
        k = np.abs(2 * d) <= knee
        gr = np.where(k, slope * (d + knee / 2) ** 2 / (2 * knee), gr)
        gr = np.where(2 * d < -knee, 0.0, gr)
    return np.maximum(gr, 0.0)


def _coef(ms: float, rate: int) -> float:
    """exp(−1 / (τ·rate)) for a time constant τ in ms."""
    return math.exp(-1000.0 / (max(ms, 0.01) * rate))


class CompressorProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.att_a = _coef(p["attack"], self.rate)
        self.log_r = math.log(_coef(p["release"], self.rate))
        self.makeup = float(p["makeup"])
        if not old:
            self.hold = np.zeros(self.ch)
            self.zi = np.zeros((1, self.ch))

    def _process(self, x):
        p = self.p
        lvl = db(x)
        gr = static_gr(lvl, p["threshold"], p["ratio"], p["knee"])
        link = p["link"] / 100.0
        if self.ch > 1 and link > 0:
            gr = link * gr.max(axis=1, keepdims=True) + (1 - link) * gr
        held = release_hold(gr, self.hold, self.log_r)
        self.hold = held[-1].copy()
        a = self.att_a
        smooth, self.zi = signal.lfilter([1 - a], [1, -a], held, axis=0, zi=self.zi)
        self.gr_db = float(smooth.max())
        return x * undb(self.makeup - smooth)

    def curve(self, level_db) -> np.ndarray:
        """The output level for a steady input level (the graph)."""
        p = self.p
        return level_db - static_gr(np.asarray(level_db, np.float64), p["threshold"], p["ratio"], p["knee"]) + \
            self.makeup


class Compressor(Effect):
    key = "comp"
    name = "fx_comp"
    params = [Param("threshold", lo=-60.0, hi=0.0, default=-18.0, unit="dB", label="cp_threshold"),
              Param("ratio", lo=1.0, hi=20.0, default=4.0, unit=": 1", decimals=1, log=True, label="cp_ratio"),
              Param("knee", lo=0.0, hi=24.0, default=6.0, unit="dB", label="cp_knee"),
              Param("attack", lo=0.1, hi=200.0, default=10.0, unit="ms", decimals=1, log=True, label="cp_attack"),
              Param("release", lo=5.0, hi=2000.0, default=150.0, unit="ms", decimals=0, log=True, label="cp_release"),
              Param("makeup", lo=0.0, hi=24.0, default=0.0, unit="dB", label="cp_makeup"),
              Param("link", lo=0.0, hi=100.0, default=100.0, unit="%", decimals=0, label="cp_link")]
    proc_class = CompressorProc
    presets = {
        "pr_gentle": {"threshold": -20.0, "ratio": 2.0, "knee": 10.0, "attack": 20.0, "release": 250.0,
                      "makeup": 3.0},
        "pr_voice": {"threshold": -24.0, "ratio": 3.5, "knee": 6.0, "attack": 5.0, "release": 120.0, "makeup": 6.0},
        "pr_drums": {"threshold": -16.0, "ratio": 5.0, "knee": 3.0, "attack": 15.0, "release": 80.0, "makeup": 4.0},
        "pr_master": {"threshold": -12.0, "ratio": 1.8, "knee": 8.0, "attack": 30.0, "release": 400.0,
                      "makeup": 2.0},
        "pr_film": {"threshold": -30.0, "ratio": 3.0, "knee": 12.0, "attack": 10.0, "release": 600.0,
                    "makeup": 8.0, "link": 100.0},
    }


# ── the limiter ───────────────────────────────────────────────
_phases = [_tp_filter[p::OVERSAMPLE] for p in range(OVERSAMPLE)]
TP_DELAY = (len(_tp_filter) - 1) // 2 // OVERSAMPLE      # 6: phase p of input t is the time t + p/4 − 6
TP_TAPS = max(len(h) for h in _phases)
# the phases as one matrix: column p, row k = tap k of phase p (reversed: row 0 is the oldest input)
_TP_MATRIX = np.zeros((TP_TAPS, OVERSAMPLE))
for _p, _h in enumerate(_phases):
    _TP_MATRIX[TP_TAPS - len(_h):, _p] = _h[::-1]
_TP_MATRIX_T = np.ascontiguousarray(_TP_MATRIX.T)


class LimiterProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        L = max(1, int(round(p["lookahead"] * self.rate / 1000.0)))               # noqa: N806
        tp = bool(p["true_peak"])
        self.drive = float(undb(p["gain"]))
        self.ceil_db = float(p["ceiling"])
        self.ceil = float(undb(self.ceil_db))
        self.log_r = math.log(_coef(p["release"], self.rate))
        if not old or (L, tp) != (getattr(self, "L", None), getattr(self, "tp", None)):
            self.L, self.tp = L, tp
            self.req = History(L - 1, 1)                        # the reductions needed, the last L − 1
            self.held = History(L - 1, 1)
            self.hold = np.zeros(1)
            self.xdelay = Fifo(L - 1 + (TP_DELAY if tp else 0), self.ch)
            self.tp_hist = np.zeros((TP_TAPS - 1, self.ch)) if tp else None   # the inputs before this block
            self.tp_prev = 0.0
            self.tp_x = Fifo(TP_DELAY, self.ch) if tp else None
            self.latency = L - 1 + (TP_DELAY if tp else 0)

    def _level(self, x):
        """The level of every frame over all channels, (n, 1): |x|, or with true peak the largest of the
        oversampled points around each frame — those of frame t − TP_DELAY, as the input lags by that much on this
        path. The four phases of the oversampling filter are one matrix product over a sliding window of the input
        (BLAS), not four filters a channel."""
        if not self.tp:
            return np.abs(x).max(axis=1, keepdims=True)
        n = len(x)
        ext = np.concatenate((self.tp_hist, x))
        self.tp_hist = ext[len(ext) - (TP_TAPS - 1):].copy()
        now = late = None
        for c in range(self.ch):
            col = np.ascontiguousarray(ext[:, c])
            win = np.lib.stride_tricks.sliding_window_view(col, TP_TAPS)          # (n, taps), no copy
            y = _TP_MATRIX_T @ win.T                                               # (phases, n), rows contiguous
            np.abs(y, out=y)
            lt = np.maximum(np.maximum(y[1], y[2]), y[3])      # those of input t − 1: τ − ¾ … τ − ¼
            nw = np.maximum(lt, y[0])                          # the times τ … τ + ¾ (τ = t − TP_DELAY)
            if now is None:
                now, late = nw, lt
            else:
                np.maximum(now, nw, out=now)
                np.maximum(late, lt, out=late)
        before = np.empty(n)
        before[0] = self.tp_prev
        before[1:] = late[:-1]
        self.tp_prev = float(late[-1]) if n else self.tp_prev
        xd = np.abs(self.tp_x(x)).max(axis=1)
        return np.maximum(xd, np.maximum(now, before))[:, None]

    def _process(self, x):
        x = x * self.drive
        lvl = self._level(x)
        need = np.maximum(db(lvl) - self.ceil_db, 0.0)
        L = self.L                                                                # noqa: N806
        cat = self.req.join(need)
        if L > 1:
            mx = ndimage.maximum_filter1d(cat, size=L, axis=0, mode="nearest")[L - 1 - (L - 1) // 2:][:len(need)]
        else:
            mx = cat
        held = release_hold(mx, self.hold, self.log_r)
        self.hold = held[-1].copy()
        cs = np.cumsum(np.concatenate((np.zeros((1, 1)), self.held.join(held))), axis=0)
        avg = (cs[L:] - cs[:-L]) / L
        self.gr_db = float(avg.max()) if len(avg) else 0.0
        y = self.xdelay(x) * undb(-avg)
        if not self.tp:                                  # the float rounding of the gain: the ceiling exactly
            np.clip(y, -self.ceil, self.ceil, out=y)
        return y

    def tail_frames(self) -> int:
        return 0


class Limiter(Effect):
    key = "limit"
    name = "fx_limit"
    params = [Param("gain", lo=0.0, hi=24.0, default=0.0, unit="dB", label="lm_gain"),
              Param("ceiling", lo=-12.0, hi=0.0, default=-1.0, unit="dB", decimals=1, label="lm_ceiling"),
              Param("true_peak", "bool", default=True, label="lm_tp"),
              Param("lookahead", lo=0.5, hi=10.0, default=3.0, unit="ms", decimals=1, label="lm_lookahead"),
              Param("release", lo=5.0, hi=1000.0, default=80.0, unit="ms", decimals=0, log=True, label="cp_release")]
    proc_class = LimiterProc
    presets = {
        "pr_safe": {"gain": 0.0, "ceiling": -1.0, "true_peak": True},
        "pr_loud": {"gain": 6.0, "ceiling": -1.0, "true_peak": True, "release": 60.0},
        "pr_stream": {"gain": 3.0, "ceiling": -1.0, "true_peak": True, "lookahead": 5.0, "release": 120.0},
        "pr_broadcast": {"gain": 0.0, "ceiling": -2.0, "true_peak": True, "lookahead": 5.0, "release": 150.0},
    }
