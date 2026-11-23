"""Dub sync (2.0): moving and retiming the whole sound of a document against its film.

- shift: silence put before the sound, or its start cut (ms or video frames) — one undo step;
- retime: the length times L, with the pitch kept (rubberband / atempo) or changed with it like a film run at another
  speed (the rate declared differently, then resampled back) — PAL 25 ↔ 23.976 / 24 and any factor; together with a
  lead (a shift in the new time) and the markers moved the same way, as one undo step;
- two points: two markers of the document go to two times of the film (or of the reference) → L and the lead;
- estimate: the automatic sync with a reference track (the film's original sound, say). Both sounds become an onset
  envelope at 100 values a second (spectral flux in four bands, mono, chunk by chunk — a film of hours costs a few
  megabytes); a cross-correlation through the FFT finds the offset for each candidate speed (1 and the film speeds)
  and keeps the sharpest; windows along the film then measure the local offsets around it and a robust line through
  them gives the fine speed and the offset. The confidence comes from how far the peak stands out and how many
  windows agree. Applying it is the retime above.

Mapping: a frame p of the document becomes p·L + lead (its time in the reference / the film)."""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field

import numpy as np

from . import ff, store
from .media import Progress, _stderr_reader, pipe_frames

ENV_RATE = 100.0                     # onset envelope values a second
BANDS = (80.0, 300.0, 1000.0, 3000.0, 3900.0)
REF_RATE = 8000                      # the reference is decoded at this rate (mono)
FPS = (("23.976", 24000 / 1001), ("24", 24.0), ("25", 25.0), ("29.97", 30000 / 1001), ("30", 30.0))
# (label, from fps, to fps): a dub timed for `from` played against a video at `to` — the length times from / to
SPEEDS = (("25 → 23.976", 25.0, 24000 / 1001), ("25 → 24", 25.0, 24.0), ("24 → 23.976", 24.0, 24000 / 1001),
          ("23.976 → 25", 24000 / 1001, 25.0), ("24 → 25", 24.0, 25.0), ("23.976 → 24", 24000 / 1001, 24.0))


# ── shift ─────────────────────────────────────────────────────
def shift(d, frames: int, label: str = "shift") -> None:
    """frames > 0: that much silence before the sound; < 0: its start cut. Markers move with the sound."""
    frames = int(frames)
    if frames > 0:
        d.insert_silence(0, frames, label)
    elif frames < 0:
        d.delete(0, min(d.frames, -frames), label)


def ms_to_frames(ms: float, rate: int) -> int:
    return int(round(ms * rate / 1000.0))


def video_frames_to_frames(n: float, fps: float, rate: int) -> int:
    return int(round(n / fps * rate))


# ── retime ────────────────────────────────────────────────────
def retime_filter(L: float, rate: int, keep_pitch: bool) -> str:
    """The ffmpeg filter that makes the sound L times as long."""
    from .fx.tempo import atempo_chain, has_rubberband
    if keep_pitch:
        if has_rubberband():
            return (f"rubberband=tempo={1.0 / L:.12f}:pitch=1:pitchq=quality:channels=together:"
                    "transients=mixed")
        return ",".join(atempo_chain(1.0 / L))
    s, r2 = rate_pair(L, rate)
    # declared at s, resampled to r2 (the samples × r2 / s), then declared at the document's rate again
    return f"asetrate={s},aresample={r2},asetrate={rate}"


def rate_pair(L: float, rate: int) -> tuple[int, int]:
    """Whole-number rates (s, r2) with r2 / s = L as closely as can be (25 → 23.976: 47952 → 50000, exact)."""
    from fractions import Fraction
    for lim in (100000, 20000, 5000, 1000):
        f = Fraction(L).limit_denominator(lim)
        p, q = f.numerator, f.denominator
        k = max(1, -(-rate // 2 // q))                       # s at least half the rate
        if p * k <= 768000:
            return q * k, p * k
    return int(round(rate / L)), rate


def retime(d, L: float, keep_pitch: bool, lead: int = 0, progress: Progress | None = None) -> store.Source:
    """The whole sound L times as long, `lead` frames later (cut when < 0) → a new source."""
    from .proc import pipe_through
    lead_in = int(round(lead / L))
    start = max(0, -lead_in)

    def reader():
        if lead_in > 0:
            left = lead_in
            while left > 0:
                k = min(store.CHUNK, left)
                yield np.zeros((k, d.channels), np.float32)
                left -= k
        for _p, x in d.chunks(start, d.frames):
            yield x
    n_in = max(1, d.frames - start + max(0, lead_in))
    return pipe_through(reader(), n_in, d.rate, d.channels, d.layout, d.folder, d.rate, d.channels, d.layout,
                        retime_filter(L, d.rate, keep_pitch), progress)


def apply_retime(d, src: store.Source, L: float, lead: int, label: str) -> None:
    """The retimed source in place of the whole sound, the markers moved by p → p·L + lead, one undo step."""
    from dataclasses import replace as dc_replace
    ms = []
    for m in d.markers:
        p = int(round(m.pos * L + lead))
        e = None if m.end is None else int(round(m.end * L + lead))
        if (e if e is not None else p) < 0 or p > src.frames:
            continue
        ms.append(dc_replace(m, pos=max(0, p), end=None if e is None else min(src.frames, e)))
    d._apply(label, [(src, 0, src.frames)], None, 0, tuple(ms))


def two_points(p1: int, p2: int, t1: float, t2: float, rate: int) -> tuple[float, int]:
    """Markers at frames p1, p2 must sound at the times t1, t2 (seconds) → (L, lead in frames)."""
    if p2 == p1:
        raise ValueError("the two points are the same")
    L = (t2 - t1) * rate / (p2 - p1)
    if not (0.5 < L < 2.0):
        raise ValueError("speed out of range")
    return L, int(round(t1 * rate - p1 * L))


def speed_name(L: float) -> str:
    """«25 → 23.976» when L is one of the film speeds (within 0.01 %), else «×1.00427»."""
    for name, a, b in SPEEDS:
        if abs(L - a / b) < 1e-4 * L:
            return name
    return f"×{L:.6f}".rstrip("0")


# ── the onset envelope ────────────────────────────────────────
class Envelope:
    """Mono frames in (any chunk sizes) → spectral flux values at ENV_RATE (frames of 2 hops, Hann)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.hop = max(1, int(round(rate / ENV_RATE)))
        self.env_rate = rate / self.hop
        self.n = 2 * self.hop
        self.nfft = 1 << (self.n - 1).bit_length()
        f = np.fft.rfftfreq(self.nfft, 1.0 / rate)
        self.band_idx = [np.where((f >= lo) & (f < hi))[0] for lo, hi in zip(BANDS[:-1], BANDS[1:])]
        self.win = np.hanning(self.n).astype(np.float32)
        self.buf = np.zeros(self.hop, np.float32)        # the hop before the next one
        self.prev = None
        self.out: list = []

    def feed(self, mono: np.ndarray) -> None:
        x = np.concatenate((self.buf, np.asarray(mono, np.float32).ravel()))
        k = (len(x) - self.hop) // self.hop                 # whole frames
        if k <= 0:
            self.buf = x
            return
        idx = np.arange(k)[:, None] * self.hop + np.arange(self.n)[None, :]
        fr = x[idx] * self.win
        sp = np.abs(np.fft.rfft(fr, self.nfft, axis=1)) ** 2
        e = np.stack([np.log10(sp[:, b].sum(axis=1) + 1e-10) for b in self.band_idx], axis=1)
        prev = self.prev if self.prev is not None else e[:1]
        flux = np.maximum(0.0, np.diff(np.concatenate((prev, e)), axis=0)).sum(axis=1)
        self.prev = e[-1:]
        self.out.append(flux.astype(np.float32))
        self.buf = x[k * self.hop:]

    def result(self) -> np.ndarray:
        return np.concatenate(self.out) if self.out else np.zeros(0, np.float32)


def envelope_of_doc(d, progress: Progress | None = None, lo: float = 0.0, hi: float = 1.0) -> np.ndarray:
    progress = progress or Progress()
    from scipy.signal import resample_poly
    env = Envelope(REF_RATE)
    g = math.gcd(int(d.rate), REF_RATE)
    up, down = REF_RATE // g, int(d.rate) // g
    for pos, x in d.chunks(0, d.frames):
        m = x.mean(axis=1)
        env.feed(resample_poly(m, up, down).astype(np.float32) if (up, down) != (1, 1) else m)
        progress(lo + (hi - lo) * (pos + len(x)) / max(1, d.frames))
    return env.result()


def envelope_of_file(path: str, track_pos: int, progress: Progress | None = None, seconds: float = 0.0,
                     lo: float = 0.0, hi: float = 1.0) -> np.ndarray:
    """The reference: a track of a file, decoded by ffmpeg straight to mono at REF_RATE."""
    progress = progress or Progress()
    args = [ff.ffmpeg(), "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", f"0:a:{track_pos}",
            "-ac", "1", "-ar", str(REF_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = ff.popen(args, stdout=subprocess.PIPE)
    err = _stderr_reader(proc)
    env = Envelope(REF_RATE)
    total = max(1.0, seconds * REF_RATE)
    got = 0
    try:
        for x in pipe_frames(proc.stdout, 1):
            env.feed(x[:, 0])
            got += len(x)
            progress(lo + (hi - lo) * min(1.0, got / total))
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError("\n".join(err[-4:]) or "ffmpeg")
    return env.result()


# ── the estimate ──────────────────────────────────────────────
@dataclass
class Estimate:
    offset: float = 0.0            # seconds: the document's start sounds at this time of the reference
    L: float = 1.0                 # the length factor (reference time per document time)
    confidence: float = 0.0        # 0 … 1
    peak: float = 0.0              # how far the correlation peak stands out (z-score)
    windows: int = 0               # windows measured …
    agree: int = 0                 # … and those on the line
    residual_ms: float = 0.0       # their spread around it
    candidates: list = field(default_factory=list)   # (L, z) of the tried speeds

    def lead(self, rate: int) -> int:
        return int(round(self.offset * rate))


def _norm(e: np.ndarray) -> np.ndarray:
    """The envelope with its slow level taken out (1 s moving mean) and scaled to unit spread."""
    e = np.asarray(e, np.float64)
    if len(e) < 3:
        return e
    k = int(ENV_RATE)
    c = np.cumsum(np.concatenate(([0.0], e)))
    lo = np.clip(np.arange(len(e)) - k // 2, 0, len(e))
    hi = np.clip(np.arange(len(e)) + k // 2 + 1, 0, len(e))
    mean = (c[hi] - c[lo]) / np.maximum(1, hi - lo)
    x = e - mean
    x = np.minimum(x, np.percentile(x, 99.5) if len(x) > 200 else x.max())   # a few loud bangs do not decide
    s = x.std()
    return x / s if s > 0 else x


def _stretch(e: np.ndarray, L: float) -> np.ndarray:
    """The envelope on the reference's time axis: value at t' = e(t' / L)."""
    if abs(L - 1.0) < 1e-12:
        return e
    n = int(len(e) * L)
    return np.interp(np.arange(n) / L, np.arange(len(e)), e)


def _xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, int]:
    """c[k] = Σ a[t] · b[t + lag] for lag = k − (len(a) − 1)."""
    n = len(a) + len(b) - 1
    nf = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.conj(np.fft.rfft(a, nf)) * np.fft.rfft(b, nf), nf)
    c = np.concatenate((c[nf - (len(a) - 1):], c[:len(b)])) if len(a) > 1 else c[:len(b)]
    return c, len(a) - 1


def _peak(c: np.ndarray) -> tuple[float, float, float]:
    """(position with parabolic refinement, value, z-score against the rest)."""
    i = int(np.argmax(c))
    v = float(c[i])
    p = float(i)
    if 0 < i < len(c) - 1:
        y0, y1, y2 = c[i - 1], c[i], c[i + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            p = i + 0.5 * (y0 - y2) / den
    guard = max(3, int(0.5 * ENV_RATE))
    rest = np.concatenate((c[:max(0, i - guard)], c[i + guard:]))
    z = (v - rest.mean()) / (rest.std() + 1e-12) if len(rest) > 10 else 0.0
    return p, v, float(z)


def estimate(env_doc: np.ndarray, env_ref: np.ndarray, speeds: bool = True, window_s: float = 60.0,
             search_s: float = 3.0) -> Estimate:
    """Where the document sits in the reference and at what speed (both envelopes at ENV_RATE)."""
    a, b = _norm(env_doc), _norm(env_ref)
    if len(a) < ENV_RATE * 2 or len(b) < ENV_RATE * 2:
        raise ValueError("too short")
    cands = [1.0]
    if speeds:
        for _n, f0, f1 in SPEEDS:
            cands.append(f0 / f1)
    best = None
    tried = []
    for L in cands:
        s = _stretch(a, L)
        c, zero = _xcorr(s, b)
        p, _v, z = _peak(c)
        tried.append((L, z))
        if best is None or z > best[2]:
            best = (L, (p - zero) / ENV_RATE, z)
    L, off, z0 = best
    est = Estimate(offset=off, L=L, peak=z0, candidates=tried)
    # windows along the document: the local offset of each around the line found
    dur = len(a) / ENV_RATE
    win = min(window_s, max(10.0, dur / 4))
    step = win / 2
    pts = []
    t = 0.0
    while t + win <= dur + 1e-9:
        i0, i1 = int(t * ENV_RATE), int((t + win) * ENV_RATE)
        seg = a[i0:i1]
        if seg.std() > 0.2:
            mid = t + win / 2
            pred = mid * L + off                          # where its middle should be in the reference
            r0 = int((pred - win / 2 - search_s) * ENV_RATE)
            r1 = int((pred + win / 2 + search_s) * ENV_RATE)
            if r0 >= 0 and r1 <= len(b):
                segs = _stretch(seg, L)
                c, zero = _xcorr(segs, b[r0:r1])
                p, _v, z = _peak(c)
                if z > 6.0:
                    ref_start = (r0 + (p - zero)) / ENV_RATE     # where the stretched window starts in the ref
                    pts.append((t, ref_start, z))
        t += step
    est.windows = len(pts)
    if len(pts) >= 3:
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        w = np.array([p[2] for p in pts])
        keep = np.ones(len(x), bool)
        for _ in range(3):
            A = np.stack((x[keep], np.ones(keep.sum())), axis=1)
            sol, *_r = np.linalg.lstsq(A * w[keep, None], y[keep] * w[keep], rcond=None)
            res = y - (sol[0] * x + sol[1])
            lim = max(0.02, 3 * np.median(np.abs(res[keep])) * 1.4826)
            nk = np.abs(res) <= lim
            if nk.sum() < 3 or (nk == keep).all():
                keep = nk if nk.sum() >= 3 else keep
                break
            keep = nk
        slope, icept = float(sol[0]), float(sol[1])
        span = float(x[keep].max() - x[keep].min()) if keep.any() else 0.0
        spread = float(np.std(res[keep])) if keep.sum() > 1 else 0.0
        # the candidate speed (1, or a film speed) stays unless the windows clearly say otherwise: over the span
        # measured the difference must move the sound by more than the windows' own spread (+ 10 ms)
        if 0.5 < slope < 2.0 and abs(slope / L - 1) < 0.01 and abs(slope - L) * span > 3 * spread + 0.010:
            est.L, est.offset = slope, icept
        else:
            k = keep
            est.offset = float(np.sum((y[k] - L * x[k]) * w[k]) / np.sum(w[k]))
            res = y - (L * x + est.offset)
        est.agree = int(keep.sum())
        est.residual_ms = float(np.std(res[keep]) * 1000) if keep.sum() > 1 else 0.0
    agree = est.agree / est.windows if est.windows else (0.5 if z0 > 12 else 0.0)
    est.confidence = float(max(0.0, min(1.0, (z0 - 8.0) / 12.0)) * (0.4 + 0.6 * agree))
    return est
