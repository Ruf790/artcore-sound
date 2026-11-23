"""Restoration (2.0): hum removal, clipping repair, the de-esser and crackle removal.

Hum removal: the mains frequency (50 or 60 Hz, or found by itself) and its harmonics cut by narrow notches. The
frequency is followed as it drifts: every quarter of a second the spectrum of one second of sound around that moment
(all channels mixed, zero-padded FFT, log-parabolic interpolation of each harmonic's peak) gives the fundamental as the
mean of harmonic / k weighted by how far each peak stands over its surroundings, and the notches move there. A notch
of finite depth is b = g·a + (1 − g)·b₀ over the pole pair of the narrow notch b₀ / a (scipy.signal.iirnotch,
−3 dB bandwidth `width`).
«Auto» picks 50 or 60 by which comb stands out more, and changes its mind only for a clear difference.

Clipping repair: a clipped stretch is a run of samples at the clip level (a set level, or the loudest so far, within a
tolerance), one sign. Its samples are rebuilt by least-squares AR interpolation (the AR model of the block around it)
under the condition that they lie beyond the clip level — solved by an active set: what comes out under the level is
pinned to it and the rest solved again. A long run may be drawn by a cubic spline instead. The whole sound is then
lowered by the headroom, so the rebuilt peaks fit under 0 dBFS.

De-esser: the sibilance band (a band-pass around the frequency) watched by a peak follower (instant attack, a set
release, the channels linked); above the threshold the band (split mode — the rest is the input minus the band, so the
two always add back up to it) or the whole sound (wide mode) is turned down, up to the set amount.

Crackle removal: blocks of 2048 frames at fixed positions, half overlapping. The AR model of each block finds the
impulses as click removal does (the forward and the backward prediction errors standing out), but with a lower
threshold and only short ones (a crackle is a millisecond at most) — hundreds a second on a worn record. Their samples
are all filled at once, by one least-squares AR interpolation over the whole block (a sparse system: one solve per
block, not one per crackle). The blocks' corrections (filled minus recorded) are cross-faded (periodic Hann, they add
up to one), so the sound itself never goes through a window."""
from __future__ import annotations

import math

import numpy as np
from scipy import signal, sparse
from scipy.interpolate import CubicSpline
from scipy.sparse.linalg import spsolve

from .base import Effect, History, Param, Processor, release_hold
from .restore import ar_coeffs, find_clicks

# ── hum removal ───────────────────────────────────────────────
HUM_WIN = 1.0                   # seconds of sound each estimate looks at
HUM_HOP = 0.25                  # an estimate (and the notches moved) every this many seconds
HUM_NFFT = 1 << 16
HUM_EST_HARM = 8                # the harmonics the frequency is estimated from
HUM_SPAN = 1.0                  # ± Hz around the base the fundamental is looked for
HUM_PROM = 12.0                 # dB a peak must stand over its surroundings (noise alone reaches ~9)
HUM_MIN_PEAKS = 2               # … and at least this many harmonics must
HUM_RANGE = 90.0                # dB under the loudest component of the window: below that nothing counts
HUM_SWITCH = 12.0               # «auto»: the other base must stand out this many dB more (summed) to switch


def _hum_spectrum(seg: np.ndarray, rate: int) -> tuple[np.ndarray, float]:
    """The log magnitude of a window of mono sound and the width of a bin."""
    n = len(seg)
    nfft = max(HUM_NFFT, 1 << int(math.ceil(math.log2(n))))
    spec = np.abs(np.fft.rfft(seg * np.hanning(n), nfft))
    return 20.0 * np.log10(np.maximum(spec, 1e-12)), rate / nfft


def _comb(lm: np.ndarray, hz: float, f0: float, harmonics: int) -> tuple[float, float, int]:
    """(weighted fundamental, summed prominence, harmonics found) of the comb near f0: each harmonic k searched
    ± k·HUM_SPAN Hz."""
    wsum = fsum = score = 0.0
    found = 0
    nb = len(lm)
    floor = float(lm.max()) - HUM_RANGE
    for k in range(1, harmonics + 1):
        c = k * f0 / hz
        span = max(2, int(math.ceil(k * HUM_SPAN / hz)))
        lo, hi = int(c - span), int(c + span) + 1
        if lo < 2 or hi + 2 * span >= nb:
            break
        i = lo + int(np.argmax(lm[lo:hi]))
        if i in (lo, hi - 1):
            continue                                     # a slope (of a louder sound nearby), not a peak
        # over both sides: the skirt of a loud tone next to it raises one side, and that one counts
        left, right = lm[max(1, lo - 3 * span):lo - span], lm[hi + span:hi + 3 * span]
        prom = float(lm[i] - max(np.median(left), np.median(right))) if len(left) and len(right) else 0.0
        if prom < HUM_PROM or lm[i] < floor:
            continue
        y0, y1, y2 = lm[i - 1], lm[i], lm[i + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den < 0 else 0.0
        fk = (i + max(-0.5, min(0.5, off))) * hz
        w = prom - HUM_PROM + 1.0
        found += 1
        wsum += w
        fsum += w * fk / k
        score += prom
    if found < HUM_MIN_PEAKS:
        return 0.0, 0.0, 0
    return fsum / wsum, score, found


def estimate_hum(seg: np.ndarray, rate: int, base: float | None) -> tuple[float, float, float, int]:
    """(fundamental, its score, the base it belongs to, harmonics found) of a mono window; base None: 50 or 60,
    whichever stands out. The fundamental is 0 when no comb stands out."""
    lm, hz = _hum_spectrum(np.asarray(seg, np.float64), rate)
    bases = (base,) if base else (50.0, 60.0)
    best = (0.0, 0.0, bases[0], 0)
    for b in bases:
        f, s, n = _comb(lm, hz, b, HUM_EST_HARM)
        if s > best[1]:
            best = (f, s, b, n)
    return best


def notch_sos(f0: float, count: int, width: float, depth: float, rate: int) -> np.ndarray:
    """The notches of harmonics 1 … count of f0 (bandwidth `width` Hz, `depth` dB) as second-order sections."""
    g = 10.0 ** (-depth / 20.0)
    out = []
    for k in range(1, count + 1):
        f = k * f0
        if f >= 0.45 * rate:
            out.append([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])          # a pass-through section keeps the shape
            continue
        b0, a = signal.iirnotch(f, f / width, fs=rate)          # 0 at f, 0 dB away from it
        out.append([*(g * a + (1.0 - g) * b0), *a])
    return np.array(out, np.float64)


class DehumProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.fixed_base = None if p["base"] == "auto" else float(p["base"])
        self.count = int(p["harmonics"])
        self.width, self.depth = float(p["width"]), float(p["depth"])
        self.track = bool(p["track"])
        self.residual = bool(p["residual"])
        if not old:
            self.B = max(256, int(round(HUM_HOP * self.rate)))
            self.W = max(1024, int(round(HUM_WIN * self.rate)))
            self.look = self.W // 2 + self.B // 2
            self.latency = self.look
            self.n_in = 0                                   # frames received
            self.block = 0                                  # the next block to filter
            self.inq = np.zeros((0, self.ch))               # frames not yet filtered (from block·B on)
            self.mono = np.zeros(0)                         # the mixed sound from mono_base on
            self.mono_base = 0
            self.out = np.zeros((self.latency, self.ch))
            self.base = self.fixed_base or 50.0
            self.f0 = 0.0                                   # the fundamental followed (0: none found yet)
            self.score = 0.0
            self.sos = None
            self.zi = None
            self.sos_key = None
        elif self.fixed_base and abs(self.base - self.fixed_base) > 1e-9:
            self.base, self.f0 = self.fixed_base, 0.0           # the notches follow at the next block

    def _target(self) -> float:
        return self.f0 if self.f0 > 0 else (self.fixed_base or 0.0)

    def _design(self) -> None:
        f = self._target()
        if f <= 0:
            self.sos, self.sos_key = None, None
            return
        sos = notch_sos(f, self.count, self.width, self.depth, self.rate)
        if self.zi is None or self.zi.shape[0] != len(sos):
            self.zi = np.zeros((len(sos), 2, self.ch))
        self.sos, self.sos_key = sos, (f, self.count, self.width, self.depth)

    def _estimate(self, b: int) -> None:
        c = b * self.B + self.B // 2
        s, e = c - self.W // 2, c + self.W // 2
        i0, i1 = s - self.mono_base, e - self.mono_base
        seg = self.mono[max(0, i0):i1]
        if i0 < 0:
            seg = np.concatenate((np.zeros(-i0), seg))
        if self.track or self.f0 <= 0:
            base = self.fixed_base
            if base is None:
                f, sc, bb, _n = estimate_hum(seg, self.rate, None)
                if f > 0 and (self.f0 <= 0 or bb == self.base or sc > self.score + HUM_SWITCH):
                    self.base = bb
                elif f > 0 and bb != self.base:                       # the other base, not clearly: keep ours
                    f, sc, _bb, _n = estimate_hum(seg, self.rate, self.base)
            else:
                f, sc, _bb, _n = estimate_hum(seg, self.rate, base)
            if f > 0:
                f = min(self.base + HUM_SPAN, max(self.base - HUM_SPAN, f))
                self.f0 = f if self.f0 <= 0 or abs(self.f0 - self.base) > HUM_SPAN else 0.4 * self.f0 + 0.6 * f
                self.score = sc
        # forget the mixed sound no later window needs
        drop = (c + self.B - self.W // 2) - self.mono_base
        if drop > 0:
            self.mono = self.mono[drop:]
            self.mono_base += drop

    def _process(self, x):
        self.inq = np.concatenate((self.inq, x))
        self.mono = np.concatenate((self.mono, x.mean(axis=1)))
        self.n_in += len(x)
        made = []
        while self.block * self.B + self.look <= self.n_in:
            self._estimate(self.block)
            key = (self._target(), self.count, self.width, self.depth)
            if key != self.sos_key:
                self._design()
            blk, self.inq = self.inq[:self.B], self.inq[self.B:]
            if self.sos is None:
                y = blk.copy()
            else:
                y, self.zi = signal.sosfilt(self.sos, blk, axis=0, zi=self.zi)
            made.append(blk - y if self.residual else y)
            self.block += 1
        if made:
            self.out = np.concatenate([self.out] + made)
        y, self.out = self.out[:len(x)], self.out[len(x):]
        return y


class Dehum(Effect):
    key = "dehum"
    name = "fx_dehum"
    params = [Param("base", "choice", default="auto",
                    choices=(("auto", "dh_auto"), ("50", "dh_50"), ("60", "dh_60")), label="dh_base"),
              Param("harmonics", "int", lo=1, hi=40, default=12, decimals=0, label="dh_harmonics"),
              Param("width", lo=0.3, hi=10.0, default=2.0, unit="Hz", decimals=1, log=True, label="dh_width"),
              Param("depth", lo=6.0, hi=80.0, default=40.0, unit="dB", decimals=0, label="dh_depth"),
              Param("track", "bool", default=True, label="dh_track"),
              Param("residual", "bool", default=False, label="dh_residual")]
    proc_class = DehumProc
    presets = {
        "pr_dh_50": {"base": "50", "harmonics": 12},
        "pr_dh_60": {"base": "60", "harmonics": 12},
        "pr_dh_buzz": {"harmonics": 30, "width": 3.0, "depth": 50.0},
        "pr_light": {"harmonics": 4, "width": 1.0, "depth": 24.0},
    }


def detect_hum(d, a: int, b: int, seconds: float = 6.0) -> dict:
    """The hum of [a, b) (its first seconds): {"f0", "base", "score", "prominence" (dB a harmonic stands over its
    surroundings, on average)}; f0 0 when none stands out."""
    rate = d.rate
    w = int(HUM_WIN * rate)
    n = min(b - a, int(seconds * rate))
    if n < w // 2:
        return {"f0": 0.0, "base": 0.0, "score": 0.0, "prominence": 0.0}
    x = d.read(a, n).astype(np.float64).mean(axis=1)
    found = []
    for s in range(0, max(1, n - w + 1), w // 2):
        f, sc, base, n = estimate_hum(x[s:s + w], rate, None)
        if f > 0:
            found.append((f, sc, base, n))
    if not found:
        return {"f0": 0.0, "base": 0.0, "score": 0.0, "prominence": 0.0}
    base = max((50.0, 60.0), key=lambda bb: sum(it[1] for it in found if it[2] == bb))
    mine = [it for it in found if it[2] == base]
    return {"f0": float(np.median([it[0] for it in mine])), "base": base,
            "score": float(np.mean([it[1] for it in mine])),
            "prominence": float(np.mean([it[1] / it[3] for it in mine]))}


# ── clipping repair ───────────────────────────────────────────
CLIP_ORDER = 32
CLIP_BLOCK = 4096
SPLINE_FROM = 400               # runs longer than this (frames) go to the spline in «auto» method
MAX_REBUILT = 4.0               # a rebuilt sample at most this many times the clip level (+12 dB)


def find_runs(x: np.ndarray, level: float, min_len: int, max_len: int) -> list[tuple[int, int, int]]:
    """Runs of |x| ≥ level of one sign: [(start, end, sign)], min_len ≤ length ≤ max_len."""
    if level <= 0:
        return []
    sgn = np.where(x >= level, 1, np.where(x <= -level, -1, 0))
    hot = np.flatnonzero(sgn)
    if not len(hot):
        return []
    cut = np.flatnonzero((np.diff(hot) > 1) | (np.diff(sgn[hot]) != 0)) + 1
    out = []
    for g in np.split(hot, cut):
        n = len(g)
        if min_len <= n <= max_len:
            out.append((int(g[0]), int(g[-1]) + 1, int(sgn[g[0]])))
    return out


def ar_matrix(m: int, a: np.ndarray):
    """The banded prediction-error matrix of m samples (rows: the errors e[p … m − 1]), sparse."""
    p = len(a) - 1
    rows = m - p
    return sparse.diags([np.full(rows, a[k]) for k in range(p + 1)], [p - k for k in range(p + 1)],
                        shape=(rows, m), format="csc")


def lsar_clipped(x: np.ndarray, a: np.ndarray, runs, level: float) -> np.ndarray:
    """x with the samples of the runs [(s, e, sign)] rebuilt by least-squares AR interpolation, each at least as far
    out as it was recorded (the clip level, or a little over it within the tolerance) in its run's sign — an active
    set: what falls short is pinned there and the rest solved again. All the runs of x in one sparse system; x has
    ≥ p samples around them."""
    A = ar_matrix(len(x), a)                                              # noqa: N806
    miss = np.concatenate([np.arange(s, e) for s, e, _g in runs])
    sign = np.concatenate([np.full(e - s, g, np.float64) for s, e, g in runs])
    floor = np.maximum(np.abs(x[miss]), level)                # the recorded value: the true one was beyond it
    free = np.ones(len(miss), bool)
    vals = x[miss].copy()
    for _it in range(8):
        cols = miss[free]
        if not len(cols):
            break
        fixed = x.copy()
        fixed[miss[~free]] = sign[~free] * floor[~free]
        fixed[cols] = 0.0
        Am = A[:, cols]                                                  # noqa: N806
        rhs = -(Am.T @ (A @ fixed))
        sol = np.atleast_1d(spsolve((Am.T @ Am).tocsc(), rhs))
        vals[free] = sol
        vals[~free] = sign[~free] * floor[~free]
        short = free & (sign * vals < floor)
        if not short.any():
            break
        free &= ~short
    vals = sign * np.clip(sign * vals, floor, MAX_REBUILT * level)
    out = x.copy()
    out[miss] = vals
    return out


def spline_clipped(x: np.ndarray, s: int, e: int, sign: int, level: float) -> np.ndarray:
    """One long run drawn by a cubic spline through 4 samples each side, kept beyond the level."""
    k = np.r_[np.arange(s - 4, s), np.arange(e, e + 4)]
    cs = CubicSpline(k, x[k])
    v = cs(np.arange(s, e))
    out = x.copy()
    out[s:e] = sign * np.clip(sign * v, np.maximum(np.abs(x[s:e]), level), MAX_REBUILT * level)
    return out


class DeclipProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.auto = bool(p["auto"])
        self.level_set = 10.0 ** (p["threshold"] / 20.0)
        self.tol = 10.0 ** (-p["tolerance"] / 20.0)
        self.min_len = int(p["min_len"])
        self.max_len = max(self.min_len, int(round(p["max_len"] * self.rate / 1000.0)))
        self.method = p["method"]
        self.gain = 10.0 ** (-p["headroom"] / 20.0)
        self.residual = bool(p["residual"])
        if not old:
            mx = int(round(self.effect.MAX_LEN * self.rate / 1000.0))
            self.ctx = CLIP_ORDER + mx + 16
            self.hist = np.zeros((self.ctx, self.ch))
            self.buf = np.zeros((0, self.ch))
            self.latency = CLIP_BLOCK + self.ctx
            self.out = np.zeros((self.latency, self.ch))
            self.top = 0.0                                   # the loudest sample so far («auto» clip level)
            self.pos = 0
            self.found = 0
            self.frames_fixed = 0
            self.peak = 0.0                                  # the loudest rebuilt sample (before the headroom)
            self.marks: list = []

    def level(self) -> float:
        return (self.top if self.auto else self.level_set) * self.tol

    def _block(self, seg: np.ndarray) -> np.ndarray:
        c, n = self.ctx, CLIP_BLOCK
        res = seg[c:c + n].copy()
        if self.auto:
            self.top = max(self.top, float(np.abs(seg).max()))
        lvl = self.level()
        p = CLIP_ORDER
        for ch in range(self.ch):
            x = seg[:, ch]
            runs = [r for r in find_runs(x, lvl, self.min_len, self.max_len)
                    if r[1] > c and r[0] < c + n and r[0] >= p + 4 and r[1] <= len(x) - p - 4]
            if not runs:
                continue
            fixed = x.copy()
            ar_runs = []
            for s, e, sg in runs:
                if self.method == "spline" or (self.method == "auto" and e - s > SPLINE_FROM):
                    fixed = spline_clipped(fixed, s, e, sg, lvl)
                else:
                    ar_runs.append((s, e, sg))
            if ar_runs:
                fixed = lsar_clipped(fixed, ar_coeffs(x, p), ar_runs, lvl)
            for s, e, _sg in runs:
                if c <= s < c + n:
                    self.found += 1
                    self.frames_fixed += e - s
                    self.marks.append(self.pos - c + s)
                    self.peak = max(self.peak, float(np.abs(fixed[s:e]).max()))
            res[:, ch] = fixed[c:c + n]
        if self.residual:
            return (res - seg[c:c + n]) * self.gain
        return res * self.gain

    def _process(self, x):
        self.buf = np.concatenate((self.buf, x))
        made = []
        while len(self.buf) >= CLIP_BLOCK + self.ctx:
            seg = np.concatenate((self.hist, self.buf[:CLIP_BLOCK + self.ctx]))
            made.append(self._block(seg))
            self.hist = self.buf[CLIP_BLOCK - self.ctx:CLIP_BLOCK]
            self.buf = self.buf[CLIP_BLOCK:]
            self.pos += CLIP_BLOCK
        if made:
            self.out = np.concatenate([self.out] + made)
        y, self.out = self.out[:len(x)], self.out[len(x):]
        return y


class Declip(Effect):
    key = "declip"
    name = "fx_declip"
    MAX_LEN = 20.0
    params = [Param("auto", "bool", default=True, label="cl_auto"),
              Param("threshold", lo=-24.0, hi=0.0, default=-0.1, unit="dBFS", decimals=2, label="cl_threshold"),
              Param("tolerance", lo=0.0, hi=3.0, default=0.05, unit="dB", decimals=2, label="cl_tolerance"),
              Param("min_len", "int", lo=1, hi=32, default=3, decimals=0, label="cl_min_len"),
              Param("max_len", lo=0.1, hi=MAX_LEN, default=10.0, unit="ms", decimals=1, label="cl_max_len"),
              Param("method", "choice", default="auto",
                    choices=(("auto", "cl_m_auto"), ("ar", "cl_m_ar"), ("spline", "cl_m_spline")),
                    label="cl_method"),
              Param("headroom", lo=0.0, hi=12.0, default=3.0, unit="dB", decimals=1, label="cl_headroom"),
              Param("residual", "bool", default=False, label="cl_residual")]
    proc_class = DeclipProc
    presets = {
        "pr_light": {"tolerance": 0.02, "min_len": 4, "headroom": 2.0},
        "pr_medium": {},
        "pr_strong": {"tolerance": 0.3, "min_len": 2, "max_len": 20.0, "headroom": 6.0},
        "pr_cl_mp3": {"tolerance": 0.8, "min_len": 3, "headroom": 4.0},
    }


def scan_clips(d, a: int, b: int, values: dict, progress=None) -> dict:
    """The clipped runs of [a, b) as Apply would find them: {"marks" (absolute starts), "runs", "frames", "peak_db"
    (the loudest rebuilt sample, dBFS, before the headroom), "need_db" (the headroom it needs to stay under
    −0.1 dBFS)}."""
    eff = Declip()
    proc = eff.processor(d.rate, d.labels, values)
    proc.pos = a
    for pos, x in d.chunks(a, b):
        proc.process(x)
        if progress:
            progress((pos + len(x) - a) / max(1, b - a))
    k = proc.latency
    while k > 0:
        n = min(65536, k)
        proc.process(np.zeros((n, d.channels), np.float32))
        k -= n
    marks = sorted({m for m in proc.marks if a <= m < b})
    pk = 20 * math.log10(proc.peak) if proc.peak > 0 else -math.inf
    return {"marks": marks, "runs": proc.found, "frames": proc.frames_fixed, "peak_db": pk,
            "need_db": max(0.0, pk + 0.1) if proc.peak > 0 else 0.0}


# ── de-esser ──────────────────────────────────────────────────
def band_sos(f: float, q: float, rate: int) -> np.ndarray:
    """An RBJ band-pass (0 dB at the centre) as one second-order section."""
    f = min(f, 0.45 * rate)
    w = 2 * math.pi * f / rate
    al = math.sin(w) / (2 * q)
    b = np.array([al, 0.0, -al])
    a = np.array([1 + al, -2 * math.cos(w), 1 - al])
    return np.r_[b / a[0], a / a[0]][None, :]


class DeessProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.sos = band_sos(p["freq"], p["q"], self.rate)
        self.thr = float(p["threshold"])
        self.max_red = float(p["reduction"])
        self.log_r = -1000.0 / (max(1.0, p["release"]) * self.rate)
        self.split = p["mode"] == "split"
        self.listen = bool(p["listen"])
        if not old:
            self.zi = np.zeros((1, 2, self.ch))
            self.hold = np.zeros(1)

    def _process(self, x):
        band, self.zi = signal.sosfilt(self.sos, x, axis=0, zi=self.zi)
        r = np.abs(band).max(axis=1, keepdims=True)                     # the channels linked
        env = release_hold(r, self.hold, self.log_r)
        self.hold = env[-1].copy()
        lvl = 20.0 * np.log10(np.maximum(env[:, 0], 1e-12))
        gr = np.clip(lvl - self.thr, 0.0, self.max_red)
        self.gr_db = float(gr.max()) if len(gr) else 0.0
        g = (10.0 ** (-gr / 20.0))[:, None]
        if self.split:
            return band * (1.0 - g) if self.listen else x - band + band * g
        return x * (1.0 - g) if self.listen else x * g


class Deess(Effect):
    key = "deess"
    name = "fx_deess"
    params = [Param("freq", lo=2000.0, hi=12000.0, default=6000.0, unit="Hz", decimals=0, log=True, label="ds_freq"),
              Param("q", lo=0.5, hi=4.0, default=1.2, decimals=2, log=True, label="ds_q"),
              Param("threshold", lo=-60.0, hi=0.0, default=-30.0, unit="dB", decimals=1, label="ds_threshold"),
              Param("reduction", lo=0.0, hi=24.0, default=8.0, unit="dB", decimals=1, label="ds_reduction"),
              Param("release", lo=5.0, hi=300.0, default=60.0, unit="ms", decimals=0, log=True, label="ds_release"),
              Param("mode", "choice", default="split", choices=(("split", "ds_split"), ("wide", "ds_wide")),
                    label="ds_mode"),
              Param("listen", "bool", default=False, label="ds_listen")]
    proc_class = DeessProc
    presets = {
        "pr_ds_male": {"freq": 5000.0, "threshold": -28.0},
        "pr_ds_female": {"freq": 7500.0, "threshold": -30.0},
        "pr_light": {"reduction": 4.0, "threshold": -24.0},
        "pr_strong": {"reduction": 14.0, "threshold": -36.0},
    }


# ── crackle removal ───────────────────────────────────────────
CR_BLOCK = 2048
CR_ORDER = 20
CR_MAX_MS = 1.0                 # a crackle is this long at most
CR_MAX_SHARE = 0.25             # more of a block than this «crackling» is music: the block is left alone


def crackle_k(sensitivity: float) -> float:
    """0 … 100 → 7 … 3 robust standard deviations."""
    return 7.0 - 4.0 * sensitivity / 100.0


def lsar_fill_many(x: np.ndarray, a: np.ndarray, miss: np.ndarray) -> np.ndarray:
    """x with all the samples at `miss` filled at once by least-squares AR interpolation (min ‖A x‖² over them, A the
    banded prediction-error matrix of the whole of x)."""
    m = len(x)
    A = ar_matrix(m, a)                                                  # noqa: N806
    known = np.ones(m, bool)
    known[miss] = False
    Am, Ak = A[:, miss], A[:, known]                                     # noqa: N806
    rhs = -(Am.T @ (Ak @ x[known]))
    sol = spsolve((Am.T @ Am).tocsc(), rhs)
    out = x.copy()
    out[miss] = sol
    return out


class DecrackleProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.k = crackle_k(p["sensitivity"])
        self.max_len = max(1, int(round(CR_MAX_MS * self.rate / 1000.0)))
        self.amount = p["amount"] / 100.0
        self.residual = bool(p["residual"])
        if not old:
            n = CR_BLOCK
            self.H = n // 2
            self.win = signal.get_window("hann", n, fftbins=True)        # periodic: halves add up to one
            self.stream = History(n - self.H + CR_ORDER, self.ch)       # the frames the next block needs before
            self.pending = np.zeros((0, self.ch))
            self.corr = np.zeros((n - self.H, self.ch))                  # the corrections still overlapping
            self.dry = History(n - self.H, self.ch)                      # the input, delayed to line up
            # a frame is final once the block after the one ending with it is done: a whole block late
            self.latency = n
            self.out = np.zeros((self.H, self.ch))
            self.found = 0

    def _correction(self, seg: np.ndarray) -> np.ndarray:
        """seg = CR_ORDER frames before + the block → the crackle of the block (n, ch)."""
        p, n = CR_ORDER, CR_BLOCK
        c = np.zeros((n, self.ch))
        for ch in range(self.ch):
            x = seg[:, ch]
            if np.abs(x).max() <= 1e-9:
                continue
            a = ar_coeffs(x, p)
            hits = [(s, e) for s, e in find_clicks(x, a, self.k, self.max_len) if s >= p and e <= len(x) - p]
            if not hits:
                continue
            miss = np.unique(np.concatenate([np.arange(s, e) for s, e in hits]))
            if len(miss) > CR_MAX_SHARE * n:
                continue
            self.found += len(hits)
            c[:, ch] = (x - lsar_fill_many(x, a, miss))[p:]
        return c

    def _process(self, x):
        n, H = CR_BLOCK, self.H
        self.pending = np.concatenate((self.pending, x))
        made = []
        while len(self.pending) >= H:
            new = self.pending[:H]
            self.pending = self.pending[H:]
            seg = self.stream.join(new)                       # p + n frames: the block ends with `new`
            cor = self._correction(seg) * self.win[:, None]
            acc = np.concatenate((self.corr, np.zeros((H, self.ch)))) + cor
            ready, self.corr = acc[:H], acc[H:]
            dry = self.dry.join(new)[:H]
            made.append(ready * self.amount if self.residual else dry - ready * self.amount)
        if made:
            self.out = np.concatenate([self.out] + made)
        y, self.out = self.out[:len(x)], self.out[len(x):]
        return y


class Decrackle(Effect):
    key = "decrackle"
    name = "fx_decrackle"
    params = [Param("sensitivity", lo=0.0, hi=100.0, default=50.0, unit="%", decimals=0, label="cr_sensitivity"),
              Param("amount", lo=0.0, hi=100.0, default=100.0, unit="%", decimals=0, label="cr_amount"),
              Param("residual", "bool", default=False, label="cr_residual")]
    proc_class = DecrackleProc
    presets = {
        "pr_light": {"sensitivity": 30.0},
        "pr_medium": {},
        "pr_strong": {"sensitivity": 75.0},
        "pr_cr_shellac": {"sensitivity": 85.0, "amount": 90.0},
    }
