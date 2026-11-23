"""Spectral editing (2.0): a selection in time and frequency drawn on the spectrogram, and what is done inside it.

The selection is a list of shapes — rectangles, lasso polygons, brush strokes — each adding to it or taking away from
it (in order), each on its own channels (the one it was drawn on, or all of them). The shapes keep their points in frames and Hz, so zooming and scrolling move
nothing; a polygon's edges and a brush's round tip are straight / round on the frequency scale it was drawn with
(`scale`), which is also the scale it is rasterized on.

Rasterized for the frames of an STFT (each frame's centre) and the bins (their frequencies) it is a mask 0 … 1, its
edges softened by a Gaussian (`feather` ms in time, Hz in frequency). The STFT: sqrt-Hann windows of ~43 ms, a hop of a
quarter — analysis and synthesis together reconstruct the sound exactly — so where the mask is 0 the sound comes back
as it was (to float precision): only the frames that touch the selection are computed, and the stretch they cover is
put back into the document as one undo step, without a seam at its ends. A selection over a whole film is processed
a block of frames at a time; nothing but that block is in memory.

The actions: attenuate by N dB, erase (−∞), boost by N dB — a gain per bin, 1 − m·(1 − g) — and fill from the
neighbours: in every bin the level inside the selection is interpolated (in dB, over time) between the nearest frames
outside it on either side (the mean power of a few of them), the phase kept, mixed in by the mask. That one needs the whole stretch at once (60 s at
most). The preview plays the result, or only what the selection holds (the gain is the mask itself)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import fft as sfft
from scipy import ndimage

from . import store
from .fx import base as fxbase
from .fx.restore import frame_size
from .media import Progress
from .spectrogram import f_of_frac, y_frac

ACTIONS = ("attenuate", "erase", "fill", "boost")
FILL_MAX_S = 60.0
FILL_AVG = 9                      # frames whose power makes the level next to the selection
# frames given new levels no longer line up with their neighbours: overlapped they add up as uncorrelated, to the
# mean of the squared Hann (3/8 at a hop of a quarter) of the power — the fill is raised by as much
FILL_LIFT = 0.5 * math.log(8.0 / 3.0)
BLOCK = 128                       # STFT frames computed at a time
MIN_GAIN = 1e-6                   # erase: −120 dB


@dataclass
class Shape:
    kind: str                             # rect | lasso | brush
    pts: list                             # rect: two corners (frame, Hz); lasso: the polygon; brush: the stroke
    op: str = "add"                       # add | sub
    rx: float = 0.0                       # brush: its radius in frames …
    ry: float = 0.0                       # … and as a part of the frequency axis (0 … 1)
    chs: tuple | None = None              # its channels (None: the selection's)


@dataclass
class SpecSel:
    rate: int
    scale: str = "log"
    channels: tuple = ()                  # the channels of the shapes that name none
    shapes: list = field(default_factory=list)

    def chs_of(self, s: Shape) -> tuple:
        return tuple(self.channels if s.chs is None else s.chs)

    def all_channels(self) -> tuple:
        out = set()
        for s in self.shapes:
            if s.op == "add":
                out.update(self.chs_of(s))
        return tuple(sorted(out))

    def empty(self) -> bool:
        return not self.all_channels()

    def frac(self, hz):
        return y_frac(hz, self.rate, self.scale)

    def hz(self, frac):
        return f_of_frac(frac, self.rate, self.scale)

    def add(self, shape: Shape) -> None:
        if shape.kind == "brush" and shape.pts:
            shape.pts = densify(shape.pts, shape.rx, shape.ry, self)
        self.shapes.append(shape)

    def span(self) -> tuple[int, int]:
        """The frames the added shapes cover: [a, b)."""
        lo, hi = math.inf, -math.inf
        for s in self.shapes:
            if s.op != "add" or not s.pts:
                continue
            ts = [p[0] for p in s.pts]
            lo = min(lo, min(ts) - s.rx)
            hi = max(hi, max(ts) + s.rx)
        if lo > hi:
            return 0, 0
        return int(math.floor(max(0.0, lo))), int(math.ceil(hi)) + 1

    def freq_span(self) -> tuple[float, float]:
        lo, hi = math.inf, -math.inf
        for s in self.shapes:
            if s.op != "add" or not s.pts:
                continue
            ys = [float(self.frac(p[1])) for p in s.pts]
            lo, hi = min(lo, min(ys) - s.ry), max(hi, max(ys) + s.ry)
        if lo > hi:
            return 0.0, 0.0
        return float(self.hz(max(0.0, lo))), float(self.hz(min(1.0, hi)))

    # ── the mask ──────────────────────────────────────────────
    def mask(self, times: np.ndarray, bin_hz: np.ndarray, ch: int | None = None) -> np.ndarray:
        """(len(times), len(bin_hz)): 1 inside the selection (of channel ch; None: any), 0 outside (hard edges)."""
        out = np.zeros((len(times), len(bin_hz)), bool)
        if not len(times):
            return out.astype(np.float64)
        ys = np.asarray(self.frac(np.maximum(bin_hz, 1e-9)), np.float64)
        t0, t1 = float(times[0]), float(times[-1])
        for s in self.shapes:
            if not s.pts or (ch is not None and ch not in self.chs_of(s)):
                continue
            m = _raster(s, times, ys, self, t0, t1)
            if m is None:
                continue
            if s.op == "add":
                out |= m
            else:
                out &= ~m
        return out.astype(np.float64)


def densify(pts, rx: float, ry: float, sel: SpecSel) -> list:
    """A brush stroke with its points at most a third of the radius apart (the union of its round tips is then the
    stroke)."""
    if len(pts) < 2 or rx <= 0 or ry <= 0:
        return list(pts)
    out = [pts[0]]
    for (ta, fa), (tb, fb) in zip(pts, pts[1:]):
        ya, yb = float(sel.frac(fa)), float(sel.frac(fb))
        d = math.hypot((tb - ta) / rx, (yb - ya) / ry)
        k = max(1, int(math.ceil(d * 3)))
        for i in range(1, k + 1):
            u = i / k
            out.append((ta + (tb - ta) * u, float(sel.hz(ya + (yb - ya) * u))))
    return out


def _raster(s: Shape, times: np.ndarray, ys: np.ndarray, sel: SpecSel, t0: float, t1: float):
    nt, nb = len(times), len(ys)
    if s.kind == "rect":
        (ta, fa), (tb, fb) = s.pts[0], s.pts[-1]
        ta, tb = min(ta, tb), max(ta, tb)
        if tb < t0 or ta > t1:
            return None
        ya, yb = sorted((float(sel.frac(max(fa, 1e-9))), float(sel.frac(max(fb, 1e-9)))))
        rows = (ys >= ya) & (ys <= yb)
        cols = (times >= ta) & (times <= tb)
        return cols[:, None] & rows[None, :]
    if s.kind == "lasso":
        if len(s.pts) < 3:
            return None
        xs = np.array([p[0] for p in s.pts], np.float64)
        if xs.max() < t0 or xs.min() > t1:
            return None
        py = np.asarray(sel.frac(np.maximum([p[1] for p in s.pts], 1e-9)), np.float64)
        x1, y1 = xs, py
        x2, y2 = np.roll(xs, -1), np.roll(py, -1)
        out = np.zeros((nt, nb), bool)
        for k in np.flatnonzero((ys >= py.min()) & (ys <= py.max())):
            y = ys[k]
            hit = ((y1 <= y) & (y < y2)) | ((y2 <= y) & (y < y1))
            if not hit.any():
                continue
            xc = np.sort(x1[hit] + (y - y1[hit]) * (x2[hit] - x1[hit]) / (y2[hit] - y1[hit]))
            for a, b in zip(xc[0::2], xc[1::2]):                        # even-odd: inside between pairs
                i0, i1 = np.searchsorted(times, a, "left"), np.searchsorted(times, b, "right")
                out[i0:i1, k] = True
        return out
    if s.kind == "brush":
        if s.rx <= 0 or s.ry <= 0:
            return None
        out = np.zeros((nt, nb), bool)
        any_in = False
        for tp, fp in s.pts:
            if tp + s.rx < t0 or tp - s.rx > t1:
                continue
            any_in = True
            yp = float(sel.frac(max(fp, 1e-9)))
            rows = np.flatnonzero(np.abs(ys - yp) < s.ry)
            if not len(rows):
                continue
            half = s.rx * np.sqrt(1.0 - ((ys[rows] - yp) / s.ry) ** 2)
            i0 = np.searchsorted(times, tp - half, "left")
            i1 = np.searchsorted(times, tp + half, "right")
            for r, a, b in zip(rows, i0, i1):
                out[a:b, r] = True
        return out if any_in else None
    return None


# ── processing ────────────────────────────────────────────────
@dataclass
class Params:
    action: str = "attenuate"
    db: float = 12.0                       # attenuate / boost by
    feather_ms: float = 10.0
    feather_hz: float = 30.0
    what: str = "result"                   # result | selection (only what the selection holds)


def stft_shape(rate: int) -> tuple[int, int]:
    n = frame_size(rate)
    return n, n // 4


def region(sel: SpecSel, frames: int, p: Params | None = None) -> tuple[int, int]:
    """The stretch of the document the selection changes: its span, the feathering and a window each side."""
    n, _hop = stft_shape(sel.rate)
    a, b = sel.span()
    pad = n + int(3 * (p.feather_ms if p else 0.0) / 1000.0 * sel.rate)
    return max(0, a - pad), min(frames, b + pad)


def _read(d, start: int, n: int) -> np.ndarray:
    """n frames from `start` (zeros before 0 and past the end)."""
    out = np.zeros((n, d.channels), np.float32)
    a, b = max(0, start), min(d.frames, start + n)
    if b > a:
        out[a - start:b - start] = d.read(a, b - a)
    return out


class _Masker:
    """The softened mask of frames [j0, j1) (all bins), computed with enough frames around for the blur."""

    def __init__(self, sel: SpecSel, n: int, hop: int, p: Params):
        self.sel, self.n, self.hop = sel, n, hop
        self.freqs = np.arange(n // 2 + 1) * sel.rate / n
        self.st = p.feather_ms / 1000.0 * sel.rate / hop               # sigma in frames
        self.sf = p.feather_hz / (sel.rate / n)                          # sigma in bins
        self.ctx = int(math.ceil(3 * self.st)) + 1

    def __call__(self, j0: int, j1: int, ch: int | None = None) -> np.ndarray:
        c = self.ctx
        times = (np.arange(j0 - c, j1 + c) * self.hop + self.n / 2).astype(np.float64)
        m = self.sel.mask(times, self.freqs, ch)
        if self.st > 0.05 or self.sf > 0.05:
            m = ndimage.gaussian_filter(m, (max(self.st, 1e-3), max(self.sf, 1e-3)), mode="nearest", truncate=3.0)
        return np.clip(m[c:c + (j1 - j0)], 0.0, 1.0)


def _gain(m: np.ndarray, p: Params) -> np.ndarray:
    if p.what == "selection":
        return m
    if p.action == "erase":
        g = MIN_GAIN
    elif p.action == "boost":
        g = 10.0 ** (abs(p.db) / 20.0)
    else:
        g = 10.0 ** (-abs(p.db) / 20.0)
    return 1.0 - m * (1.0 - g)


def apply(d, sel: SpecSel, p: Params, progress: Progress | None = None,
          limit: tuple[int, int] | None = None) -> tuple[int, int, store.Source]:
    """The selection processed: (a, b, source) — the source takes the place of [a, b) of the document (the same
    length). limit: only that much of the region (the preview)."""
    progress = progress or Progress()
    if sel.empty():
        raise ValueError("empty")
    n, hop = stft_shape(d.rate)
    ra, rb = region(sel, d.frames, p)
    if limit is not None:
        ra, rb = max(ra, limit[0]), min(rb, limit[1])
    if rb <= ra:
        raise ValueError("empty")
    if p.action == "fill" and p.what == "result" and rb - ra > FILL_MAX_S * d.rate:
        raise ValueError("fill_long")
    chs = [c for c in sel.all_channels() if 0 <= c < d.channels]
    win = np.sqrt(np.hanning(n + 1)[:n])                                 # periodic sqrt-Hann
    j0 = (ra - n) // hop + 1                                             # the first frame that reaches into ra
    j1 = (rb - 1) // hop + 1                                             # past the last frame that starts before rb
    masker = _Masker(sel, n, hop, p)
    w = store.SourceWriter(d.folder, d.channels)
    try:
        if p.action == "fill" and p.what == "result":
            out = _fill(d, chs, ra, rb, j0, j1, n, hop, win, masker, progress)
            for k in range(0, len(out), store.CHUNK):
                w.append(out[k:k + store.CHUNK])
            return ra, rb, w.finish()
        acc = np.zeros((n, len(chs)))                    # overlap-add from frame position `ap` on
        ap = j0 * hop
        done = ra                                        # the output is written up to here
        for jb in range(j0, j1, BLOCK):
            je = min(j1, jb + BLOCK)
            x = _read(d, jb * hop, (je - jb - 1) * hop + n).astype(np.float64)
            if chs:
                g = np.stack([_gain(masker(jb, je, c), p) for c in chs], axis=1)     # (frames, ch, bins)
                frames = np.lib.stride_tricks.sliding_window_view(x[:, chs], n, axis=0)[::hop][:je - jb]
                spec = sfft.rfft(frames * win, axis=2, workers=fxbase.FFT_WORKERS)    # (f, ch, bins)
                y = sfft.irfft(spec * g, n=n, axis=2, workers=fxbase.FFT_WORKERS) * win * 0.5
                need = (je - jb - 1) * hop + n
                ext = np.zeros((need + (jb * hop - ap), len(chs)))
                ext[:len(acc)] += acc[:len(ext)]
                base = jb * hop - ap
                for i in range(je - jb):
                    ext[base + i * hop: base + i * hop + n] += y[i].T
            # samples before je·hop are complete now (every frame over them is in)
            final_end = min(rb, je * hop)
            if final_end > done:
                seg = _read(d, done, final_end - done)
                if chs:
                    seg[:, chs] = ext[done - ap: final_end - ap]
                w.append(seg)
                done = final_end
            if chs:
                keep_from = je * hop
                acc = ext[keep_from - ap:]
                ap = keep_from
            progress((done - ra) / max(1, rb - ra))
        if done < rb:
            w.append(_read(d, done, rb - done))
    except BaseException:
        w.abort()
        raise
    return ra, rb, w.finish()


def _fill(d, chs, ra, rb, j0, j1, n, hop, win, masker, progress) -> np.ndarray:
    """Fill from the neighbours over [ra, rb), the whole stretch at once (≤ FILL_MAX_S)."""
    x = _read(d, j0 * hop, (j1 - j0 - 1) * hop + n).astype(np.float64)
    out = x.copy()
    nf = j1 - j0
    idx = np.arange(nf)
    for ci, c in enumerate(chs):
        m = masker(j0, j1, c)                                            # (frames, bins)
        frames = np.lib.stride_tricks.sliding_window_view(x[:, c], n)[::hop][:nf]
        spec = sfft.rfft(frames * win, axis=1, workers=fxbase.FFT_WORKERS)    # (frames, bins)
        lm = np.log(np.maximum(np.abs(spec), 1e-12))
        new = lm.copy()
        # the level around: the mean power of the nearest frames outside the selection (a single frame's level
        # of noise is too low on average, and too unsteady)
        ref_m = (m <= 0.01).astype(np.float64)
        pw = np.abs(spec) ** 2
        num = ndimage.uniform_filter1d(pw * ref_m, FILL_AVG, axis=0, mode="nearest")
        den = ndimage.uniform_filter1d(ref_m, FILL_AVG, axis=0, mode="nearest")
        level = 0.5 * np.log(np.maximum(num / np.maximum(den, 1e-9), 1e-24)) + FILL_LIFT
        for k in range(spec.shape[1]):
            inside = m[:, k] > 0.01
            if not inside.any():
                continue
            ref = ~inside
            if not ref.any():
                continue
            new[inside, k] = np.interp(idx[inside], idx[ref], level[ref, k])
        mag = np.exp((1.0 - m) * lm + m * new)
        spec2 = spec / np.maximum(np.abs(spec), 1e-12) * mag
        y = sfft.irfft(spec2, n=n, axis=1, workers=fxbase.FFT_WORKERS) * win * 0.5
        acc = np.zeros(len(x))
        for i in range(nf):
            acc[i * hop:i * hop + n] += y[i]
        out[:, c] = acc
        progress((ci + 1) / max(1, len(chs)))
    off = ra - j0 * hop
    return out[off:off + (rb - ra)].astype(np.float32)
