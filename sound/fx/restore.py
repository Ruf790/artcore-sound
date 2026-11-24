"""Restoration: noise reduction by a noise sample and click removal.

Noise reduction is a spectral gate over an STFT (sqrt-Hann windows, a hop of a quarter frame, ~43 ms frames): a bin is
let through when its power, smoothed over time, stands `sensitivity` dB over the noise sample's mean power there, and
held down by `reduction` dB otherwise. What keeps it free of «musical noise» (lone bins popping open in the noise) is
the smoothing of the decision: the gate of each bin opens ahead of the sound (attack, a look-ahead), closes slowly
(release), both moving in dB at a set speed, and the gains are then averaged over neighbouring frequencies — so a bin
that pops up alone in the noise for a frame barely opens; a bin 6 dB or more over the threshold is the sound itself
and stays open (the averaging would otherwise dull a quiet tone among closed neighbours). «Noise only» plays what is
taken away instead.

Click removal works on blocks of 4096 frames at fixed positions, with context on both sides: an AR model (order 24,
Yule–Walker on the block) predicts each sample; the clicks are where the prediction error stands out — more than k
robust standard deviations (the median of |e| / 0.6745), k set by the sensitivity; a click longer than the longest
allowed is music, not a click. The samples of a click are filled by least-squares AR interpolation (LSAR): the values
that make the prediction error over the click and p samples each side smallest, the samples around kept."""
from __future__ import annotations

import math

import numpy as np
from scipy import fft as sfft
from scipy import linalg, ndimage, signal

from . import base
from .base import Effect, Param, Processor


# ── noise reduction ───────────────────────────────────────────
STRONG = 10 ** 0.6            # 6 dB over the threshold: a bin of the sound, not of the noise (never smoothed shut)
def frame_size(rate: int) -> int:
    """2048 at 44.1 / 48 kHz: about 43 ms, a power of two."""
    return int(2 ** round(math.log2(max(256, 0.0427 * rate))))


def _window(n: int) -> np.ndarray:
    return np.sqrt(signal.get_window("hann", n, fftbins=True))


def noise_profile(frames_iter, rate: int, channels: int) -> dict:
    """The mean power of the noise per bin and channel, from (n, ch) chunks of a noise-only stretch."""
    n = frame_size(rate)
    hop = n // 4
    win = _window(n)
    acc = np.zeros((n // 2 + 1, channels))
    count = 0
    rest = np.zeros((0, channels))
    for x in frames_iter:
        x = np.concatenate((rest, np.asarray(x, np.float64)))
        k = 0
        while k + n <= len(x):
            spec = np.fft.rfft(x[k:k + n] * win[:, None], axis=0)
            acc += np.abs(spec) ** 2
            count += 1
            k += hop
        rest = x[k:]
    if not count:
        raise ValueError("short")
    return {"rate": rate, "channels": channels, "n": n, "power": acc / count, "frames": count}


class NoiseProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        prof = self.effect.profile
        n = frame_size(self.rate)
        self.n, self.hop = n, n // 4
        bins = n // 2 + 1
        if prof is None or prof.get("n") != n:
            self.noise = None                               # no sample (or another rate): passes the sound
        else:
            pw = prof["power"]
            if pw.shape[1] != self.ch:
                pw = np.repeat(pw.mean(axis=1, keepdims=True), self.ch, axis=1)
            self.noise = pw.T.copy()                        # (ch, bins)
        self.red = float(p["reduction"])
        self.thr = 10 ** (p["sensitivity"] / 10.0)
        fps = self.rate / self.hop                          # frames per second
        self.t_alpha = math.exp(-1.0 / max(1e-6, p["smooth_t"] / 1000.0 * fps)) if p["smooth_t"] > 0 else 0.0
        self.rel_step = self.red / max(1.0, p["release"] / 1000.0 * fps)
        self.att_step = self.red / max(1e-9, p["attack"] / 1000.0 * fps) if p["attack"] > 0 else float("inf")
        hz = self.rate / n
        self.f_width = int(round(p["smooth_f"] / hz))
        self.residual = bool(p["residual"])
        self.look = int(math.ceil(self.effect.MAX_ATTACK / 1000.0 * fps))
        if not old:
            self.win = _window(n)
            self.inp = np.zeros((n - self.hop, self.ch))     # the frames of the next window before the new hop
            self.acc = np.zeros((n, self.ch))                 # overlap-add
            self.spec_q = np.zeros((0, self.ch, bins), complex)   # the spectra waiting for their gains (look-ahead)
            self.gate_q = np.zeros((0, self.ch, bins))          # their gate decisions (dB)
            self.strong_q = np.zeros((0, self.ch, bins), bool)  # the bins far over the threshold
            self.p_smooth = np.zeros((self.ch, bins))
            self.g_rel = np.full((self.ch, bins), -self.red)
            self.pending = np.zeros((0, self.ch))
            # the overlap-add puts out the time −(n − hop) first; the look-ahead and a hop of slack are queued
            self.out = np.zeros((self.hop * (self.look + 1), self.ch))
            self.latency = n + self.hop * self.look

    def _gates(self, spec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The gate decisions (dB, 0 = open) of frames (F, ch, bins), with the time smoothing and the release, and
        which bins stand STRONG dB over the threshold (those stay open whatever the smoothing over frequency)."""
        out = np.empty(spec.shape)
        strong = np.zeros(spec.shape, bool)
        if self.noise is None:
            out[:] = 0.0
            return out, strong
        pw = spec.real ** 2 + spec.imag ** 2
        a = self.t_alpha
        thr = self.noise * self.thr
        for i in range(len(spec)):
            self.p_smooth = a * self.p_smooth + (1 - a) * pw[i] if a else pw[i]
            gate = np.where(self.p_smooth > thr, 0.0, -self.red)
            self.g_rel = np.maximum(gate, self.g_rel - self.rel_step)
            out[i] = self.g_rel
            strong[i] = pw[i] > thr * STRONG
        return out, strong

    def _process(self, x):
        n, hop = self.n, self.hop
        self.pending = np.concatenate((self.pending, x))
        while len(self.pending) >= hop:
            nf = min(32, len(self.pending) // hop)            # 32 frames at a time: a few MB for 5.1
            stream = np.concatenate((self.inp, self.pending[:nf * hop]))
            self.pending = self.pending[nf * hop:]
            self.inp = stream[nf * hop:]
            frames = np.lib.stride_tricks.sliding_window_view(stream, n, axis=0)[::hop][:nf]   # (nf, ch, n)
            spec = sfft.rfft(frames * self.win, axis=2, workers=base.FFT_WORKERS)          # (nf, ch, bins)
            gates, strong = self._gates(spec)
            self.spec_q = np.concatenate((self.spec_q, spec)) if len(self.spec_q) else spec
            self.gate_q = np.concatenate((self.gate_q, gates)) if len(self.gate_q) else gates
            self.strong_q = np.concatenate((self.strong_q, strong)) if len(self.strong_q) else strong
            m = len(self.spec_q) - self.look
            if m > 0:
                if self.att_step != float("inf"):
                    # the attack: g[t] = max over j ≥ 0 of gate[t + j] − j·step — a reverse running maximum of
                    # gate[k] − k·step, shifted back by t·step (frames past the look-ahead never win: the gate
                    # spans only the reduction)
                    k = np.arange(len(self.gate_q), dtype=np.float64)[:, None, None] * self.att_step
                    v = np.ascontiguousarray((self.gate_q - k)[::-1])
                    g = (np.maximum.accumulate(v, axis=0)[::-1] + k)[:m]
                else:
                    g = self.gate_q[:m].copy()
                if self.f_width > 0:
                    g = ndimage.uniform_filter1d(g, 2 * self.f_width + 1, axis=2, mode="nearest")
                    g = np.where(self.strong_q[:m], 0.0, g)
                if self.noise is None:
                    gain = np.zeros_like(g) if self.residual else np.ones_like(g)
                else:
                    gain = np.exp(g * (math.log(10.0) / 20.0))
                    if self.residual:
                        gain = 1.0 - gain
                y = sfft.irfft(self.spec_q[:m] * gain, n=n, axis=2, workers=base.FFT_WORKERS)  # (m, ch, n)
                y *= self.win * 0.5
                self.spec_q, self.gate_q, self.strong_q = self.spec_q[m:], self.gate_q[m:], self.strong_q[m:]
                # overlap-add of the m frames at once: hop block i of the output is the sum of quarter k of
                # frame i − k (k = 0 … 3) and of what the frames before left in acc
                q = n // hop
                z = np.zeros((m + q, self.ch, hop))
                z[:q] += self.acc.reshape(q, hop, self.ch).transpose(0, 2, 1)
                yq = y.reshape(m, self.ch, q, hop)
                for j in range(q):
                    z[j:j + m] += yq[:, :, j]
                ready = z[:m].transpose(0, 2, 1).reshape(m * hop, self.ch)
                self.acc = z[m:].transpose(0, 2, 1).reshape(n, self.ch).copy()
                self.out = np.concatenate((self.out, ready))
        out, self.out = self.out[:len(x)], self.out[len(x):]
        return out


class NoiseReduction(Effect):
    key = "nr"
    name = "fx_nr"
    MAX_ATTACK = 100.0
    params = [Param("reduction", lo=0.0, hi=48.0, default=15.0, unit="dB", decimals=1, label="nr_reduction"),
              Param("sensitivity", lo=0.0, hi=24.0, default=6.0, unit="dB", decimals=1, label="nr_sensitivity"),
              Param("smooth_f", lo=0.0, hi=1000.0, default=150.0, unit="Hz", decimals=0, label="nr_smooth_f"),
              Param("smooth_t", lo=0.0, hi=200.0, default=30.0, unit="ms", decimals=0, label="nr_smooth_t"),
              Param("attack", lo=0.0, hi=MAX_ATTACK, default=20.0, unit="ms", decimals=0, label="nr_attack"),
              Param("release", lo=10.0, hi=1000.0, default=150.0, unit="ms", decimals=0, log=True,
                    label="nr_release"),
              Param("residual", "bool", default=False, label="nr_residual")]
    proc_class = NoiseProc
    presets = {
        "pr_light": {"reduction": 9.0, "sensitivity": 6.0, "smooth_f": 200.0, "release": 200.0},
        "pr_medium": {},
        "pr_strong": {"reduction": 24.0, "sensitivity": 8.0, "smooth_f": 250.0, "smooth_t": 40.0, "release": 120.0},
        "pr_hiss": {"reduction": 18.0, "sensitivity": 4.0, "smooth_f": 300.0, "attack": 10.0, "release": 100.0},
        "pr_voice": {"reduction": 12.0, "sensitivity": 7.0, "smooth_f": 120.0, "attack": 30.0, "release": 250.0},
    }

    def __init__(self):
        super().__init__()
        self.profile = None                 # noise_profile(), taken by «Take a noise sample» (for the session)


# ── adaptive noise reduction (2.0) ────────────────────────────
# The same gate with no noise sample: the noise of each bin is followed as the sound goes (minimum statistics,
# R. Martin 2001): the power smoothed over time, its minimum over the last `window` seconds (kept as U sub-windows,
# so the oldest drops out a piece at a time), times a bias — speech and music fall to the noise floor now and then
# in every bin, the noise never rises over it. «Voice»: a short window (the noise followed closely, pauses are
# short); «music»: a long one (a held note is not taken for noise) and a slower, gentler gate.
MS_RISE = 0.9                   # the smoothing of the power on the way up …
MS_FALL = 0.7                   # … and down: a short pause reaches the noise floor
MS_SUBS = 8
AUTO_MODES = {
    # bias: the mean noise power over the minimum of the smoothed one (measured on white noise), so the estimate is the
    # noise's mean power as a noise sample gives it and the sensitivity means the same in both
    "voice": {"window": 1.0, "bias": 5.1, "smooth_f": 200.0, "smooth_t": 30.0, "attack": 15.0, "release": 150.0},
    "music": {"window": 3.0, "bias": 6.4, "smooth_f": 120.0, "smooth_t": 50.0, "attack": 30.0, "release": 300.0},
}


class AutoNoiseProc(NoiseProc):
    def configure(self, old: dict) -> None:
        mode = AUTO_MODES.get(self.p.get("mode"), AUTO_MODES["voice"])
        self.p = {**mode, **self.p}
        super().configure(old)
        fps = self.rate / self.hop
        self.sub_len = max(1, int(math.ceil(mode["window"] * fps / MS_SUBS)))
        bins = self.n // 2 + 1
        if not old:
            self.ms_p = None
            self.cur_min = np.full((self.ch, bins), np.inf)
            self.subs = np.full((MS_SUBS, self.ch, bins), np.inf)
            self.subs_min = np.full((self.ch, bins), np.inf)
            self.sub_i = 0
            self.sub_n = 0
        self.noise = self.subs_min                     # not None: the gate works (on the noise followed)

    def noise_now(self) -> np.ndarray:
        return self.p["bias"] * np.minimum(self.subs_min, self.cur_min)

    def _gates(self, spec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        out = np.empty(spec.shape)
        strong = np.zeros(spec.shape, bool)
        pw = spec.real ** 2 + spec.imag ** 2
        a = self.t_alpha
        for i in range(len(spec)):
            if self.ms_p is None:
                self.ms_p = pw[i].copy()
            else:
                al = np.where(pw[i] > self.ms_p, MS_RISE, MS_FALL)
                self.ms_p = al * self.ms_p + (1 - al) * pw[i]
            self.cur_min = np.minimum(self.cur_min, self.ms_p)
            self.sub_n += 1
            if self.sub_n >= self.sub_len:
                self.subs[self.sub_i] = self.cur_min
                self.sub_i = (self.sub_i + 1) % MS_SUBS
                self.subs_min = self.subs.min(axis=0)
                self.cur_min = np.full_like(self.cur_min, np.inf)
                self.sub_n = 0
            thr = self.noise_now() * self.thr
            self.p_smooth = a * self.p_smooth + (1 - a) * pw[i] if a else pw[i]
            gate = np.where(self.p_smooth > thr, 0.0, -self.red)
            self.g_rel = np.maximum(gate, self.g_rel - self.rel_step)
            out[i] = self.g_rel
            strong[i] = pw[i] > thr * STRONG
        self.noise = self.subs_min
        return out, strong


class AutoNoiseReduction(Effect):
    key = "nr_auto"
    name = "fx_nr_auto"
    MAX_ATTACK = 100.0
    profile = None
    params = [Param("mode", "choice", default="voice", choices=(("voice", "na_voice"), ("music", "na_music")),
                    label="na_mode"),
              Param("reduction", lo=0.0, hi=48.0, default=12.0, unit="dB", decimals=1, label="nr_reduction"),
              Param("sensitivity", lo=0.0, hi=24.0, default=6.0, unit="dB", decimals=1, label="nr_sensitivity"),
              Param("residual", "bool", default=False, label="nr_residual")]
    proc_class = AutoNoiseProc
    presets = {
        "pr_light": {"reduction": 8.0},
        "pr_medium": {},
        "pr_strong": {"reduction": 20.0, "sensitivity": 8.0},
        "pr_na_music": {"mode": "music", "reduction": 9.0, "sensitivity": 5.0},
    }


# ── click removal ─────────────────────────────────────────────
ORDER = 24
BLOCK = 4096


def ar_coeffs(x: np.ndarray, p: int = ORDER) -> np.ndarray:
    """[1, a1 … ap]: e[t] = x[t] + Σ a_k x[t−k] is the prediction error (Yule–Walker on a Hann-windowed x)."""
    w = x * np.hanning(len(x))
    n = len(w)
    r = np.array([w[:n - k] @ w[k:] for k in range(p + 1)]) / n
    if r[0] <= 1e-18:
        return np.r_[1.0, np.zeros(p)]
    r[0] *= 1.0 + 1e-6                                   # a little white noise: the system stays well posed
    a = linalg.solve_toeplitz(r[:p], -r[1:p + 1])
    return np.r_[1.0, a]


def lsar_fill(x: np.ndarray, a: np.ndarray, s: int, e: int) -> np.ndarray:
    """x[s:e] replaced by the least-squares AR interpolation from x around it (x has ≥ p samples each side)."""
    p = len(a) - 1
    lo, hi = s - p, e + p
    seg = x[lo:hi]
    m = len(seg)
    rows = m - p                                         # the errors e[t] for t = lo + p … hi − 1
    A = np.zeros((rows, m))                              # noqa: N806
    for k in range(p + 1):
        A[np.arange(rows), np.arange(rows) + p - k] = a[k]
    miss = np.arange(s - lo, e - lo)
    known = np.setdiff1d(np.arange(m), miss)
    Am, Ak = A[:, miss], A[:, known]                     # noqa: N806
    rhs = -Ak @ seg[known]
    sol, *_ = np.linalg.lstsq(Am, rhs, rcond=None)
    out = x.copy()
    out[s:e] = sol
    return out


def threshold_k(sensitivity: float) -> float:
    """0 … 100 → 10 … 3 robust standard deviations."""
    return 10.0 - 7.0 * sensitivity / 100.0


def _sigma(e: np.ndarray) -> float:
    return float(np.median(np.abs(e)) / 0.6745) if len(e) else 0.0


def find_clicks(seg: np.ndarray, a: np.ndarray, k: float, max_len: int) -> list[tuple[int, int]]:
    """Clicks in seg (one channel): [(start, end)]. The forward prediction error of a click spreads over the p
    samples after it, the backward one over the p before it: a click starts where the forward error stands out and
    ends where the backward one does."""
    p = len(a) - 1
    n = len(seg)
    if n <= 2 * p + 2:
        return []
    ef = signal.lfilter(a, [1.0], seg)
    eb = signal.lfilter(a, [1.0], seg[::-1])[::-1]
    ef[:p] = 0
    eb[n - p:] = 0
    sf, sb = _sigma(ef[p:]), _sigma(eb[:n - p])
    if sf <= 1e-12 or sb <= 1e-12:
        return []
    hf = np.abs(ef) > k * sf
    hb = np.abs(eb) > k * sb
    hot = np.flatnonzero(hf | hb)
    if not len(hot):
        return []
    out = []
    for g in np.split(hot, np.flatnonzero(np.diff(hot) > 3) + 1):
        fs, bs = g[hf[g]], g[hb[g]]
        if not len(fs) or not len(bs):
            continue                                     # one-sided: not an impulse
        s, e = int(fs.min()), int(bs.max()) + 1          # the forward error starts at the click, the backward ends
        if e <= s:
            s, e = int(min(fs.min(), bs.min())), int(max(fs.max(), bs.max())) + 1
        s, e = s - 1, e + 1
        if e - s > max_len + 2:
            continue                                     # too long for a click: music
        out.append((max(0, s), min(n, e)))
    return out


class DeclickProc(Processor):
    def configure(self, old: dict) -> None:
        p = self.p
        self.k = threshold_k(p["sensitivity"])
        self.max_len = max(1, int(round(p["max_len"] * self.rate / 1000.0)))
        self.ctx = ORDER + 2 * max(1, int(round(self.effect.MAX_LEN * self.rate / 1000.0))) + 16
        if not old:
            self.hist = np.zeros((self.ctx, self.ch))     # the frames before the block (context)
            self.buf = np.zeros((0, self.ch))
            self.latency = BLOCK + self.ctx
            self.out = np.zeros((self.latency, self.ch))
            self.found = 0
            self.pos = 0                                   # the absolute position of the next block
            self.marks: list = []                          # click starts found (absolute frames, input timeline)

    def _block(self, seg: np.ndarray) -> np.ndarray:
        """seg = context + block + context; → the block repaired."""
        c, n = self.ctx, BLOCK
        res = seg[c:c + n].copy()
        for ch in range(self.ch):
            x = seg[:, ch]
            if np.abs(x).max() <= 0:
                continue
            a = ar_coeffs(x)
            p = len(a) - 1
            fixed = x.copy()
            for s, e in find_clicks(x, a, self.k, self.max_len):
                if e <= c or s >= c + n or s < p or e > len(x) - p:
                    continue
                fixed = lsar_fill(fixed, a, s, e)
                if c <= s < c + n:
                    self.found += 1
                    self.marks.append(self.pos - c + s)
            res[:, ch] = fixed[c:c + n]
        return res

    def _process(self, x):
        self.buf = np.concatenate((self.buf, x))
        made = []
        while len(self.buf) >= BLOCK + self.ctx:
            seg = np.concatenate((self.hist, self.buf[:BLOCK + self.ctx]))
            made.append(self._block(seg))
            self.hist = self.buf[BLOCK - self.ctx:BLOCK]
            self.buf = self.buf[BLOCK:]
            self.pos += BLOCK
        if made:
            self.out = np.concatenate([self.out] + made)
        y, self.out = self.out[:len(x)], self.out[len(x):]
        return y


class Declick(Effect):
    key = "declick"
    name = "fx_declick"
    MAX_LEN = 5.0
    params = [Param("sensitivity", lo=0.0, hi=100.0, default=50.0, unit="%", decimals=0, label="dc_sensitivity"),
              Param("max_len", lo=0.1, hi=MAX_LEN, default=1.5, unit="ms", decimals=1, label="dc_max_len")]
    proc_class = DeclickProc
    presets = {
        "pr_vinyl": {"sensitivity": 55.0, "max_len": 2.0},
        "pr_light": {"sensitivity": 30.0, "max_len": 1.0},
        "pr_strong": {"sensitivity": 80.0, "max_len": 3.0},
        "pr_digital": {"sensitivity": 60.0, "max_len": 0.5},
    }


def scan_clicks(d, a: int, b: int, values: dict, progress=None) -> list[int]:
    """Where the clicks of [a, b) are (absolute frames of the document), as Apply would find them."""
    eff = Declick()
    proc = eff.processor(d.rate, d.labels, values)
    proc.pos = a
    for _pos, x in d.chunks(a, b):
        proc.process(x)
        if progress:
            progress((_pos + len(x) - a) / max(1, b - a))
    k = proc.latency
    while k > 0:
        n = min(65536, k)
        proc.process(np.zeros((n, d.channels), np.float32))
        k -= n
    return sorted({m for m in proc.marks if a <= m < b})
