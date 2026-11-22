"""Analysis of a range of a document, read chunk by chunk (any length, a three-hour 5.1 film included): the sample
peak, the true peak (4× oversampling), RMS, DC offset and clipped samples per channel, and the loudness after
ITU-R BS.1770-4 / EBU R128 — the K filter, 400 ms blocks every 100 ms, the absolute gate at −70 LUFS and the
relative one 10 LU under, channel weights (LFE left out, the surrounds ×1.41): the integrated loudness, the
loudness range (EBU Tech 3342: 3 s blocks, −20 LU gate, 10th … 95th percentile) and the maximum momentary and
short-term loudness. The results agree with ffmpeg's ebur128 filter.

The analysis reads a snapshot of the document's pieces, so it can run in a thread while the window goes on; an edit
makes the window drop the result (doc.version)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from . import store
from .media import Progress

ABS_GATE = -70.0              # LUFS
REL_GATE = -10.0              # LU under the absolute-gated loudness (integrated)
LRA_GATE = -20.0              # LU (loudness range)
CLIP = 0.9999                 # |sample| from here on counts as clipped (−0.0009 dBFS; 16-bit full scale is 0.99997)
SURROUND = {"Ls", "Rs", "Lb", "Rb", "Cs", "S"}       # ×1.41 (BS.1770: the side / back channels)
OVERSAMPLE = 4
TP_TAPS = 49                  # the interpolation filter: 12 taps per phase, as BS.1770-4 Annex 2
TP_MARGIN = 0.5               # a block more than 6 dB under the peak so far cannot hold the true peak
TP_BLOCK = 256
DENORMAL_GUARD = 1e-20

_tp_filter = OVERSAMPLE * signal.firwin(TP_TAPS, 1.0 / OVERSAMPLE, window=("kaiser", 7.0))
TP_CTX = -(-(TP_TAPS - 1) // OVERSAMPLE)           # frames of context each side of an oversampled stretch


def weights(labels) -> np.ndarray:
    return np.array([0.0 if lab == "LFE" else 1.41 if lab in SURROUND else 1.0 for lab in labels])


def k_filter(rate: int) -> np.ndarray:
    """The K weighting (a high shelf + the RLB high pass) as two biquad sections for this rate (the analogue
    prototype of BS.1770 put through the bilinear transform, as libebur128 / ffmpeg do)."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10 ** (gain / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0,
             1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1 + k / q + k * k
    hp = [1.0, -2.0, 1.0, 1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    return np.array([shelf, hp])


def lufs(power) -> float:
    """Below 1e-30 (−300 LUFS) is digital silence (the denormal guard leaves ~1e-40 there)."""
    return -0.691 + 10 * math.log10(power) if power > 1e-30 else float("-inf")


def to_db(v: float) -> float:
    return 20 * math.log10(v) if v > 0 else float("-inf")


@dataclass
class ChannelStats:
    label: str
    peak: float = 0.0             # the largest |sample| (linear)
    true_peak: float = 0.0        # linear; NaN when not measured
    min: float = 0.0
    max: float = 0.0
    rms: float = 0.0              # linear
    dc: float = 0.0               # the mean, linear (×100 = %)
    clipped: int = 0


@dataclass
class Stats:
    frames: int
    rate: int
    channels: list = field(default_factory=list)
    integrated: float = float("-inf")          # LUFS
    lra: float = 0.0                           # LU
    momentary_max: float = float("-inf")       # LUFS
    short_term_max: float = float("-inf")      # LUFS
    threshold: float = float("-inf")           # the relative gate of the integrated loudness, LUFS
    loudness: bool = True                      # were the loudness figures measured
    has_true_peak: bool = True

    @property
    def peak(self) -> float:
        return max((c.peak for c in self.channels), default=0.0)

    @property
    def true_peak(self) -> float:
        return max((c.true_peak for c in self.channels), default=0.0)

    @property
    def rms(self) -> float:
        """Of all the channels together."""
        return math.sqrt(sum(c.rms ** 2 for c in self.channels) / max(1, len(self.channels)))

    @property
    def clipped(self) -> int:
        return sum(c.clipped for c in self.channels)


class Analyzer:
    """Feed frames in order (any chunk sizes), then result()."""

    def __init__(self, rate: int, labels, loudness: bool = True, true_peak: bool = True):
        self.rate, self.labels = int(rate), list(labels)
        ch = self.ch = len(self.labels)
        self.loudness, self.true_peak = loudness, true_peak
        self.n = 0
        self.sum = np.zeros(ch)
        self.sq = np.zeros(ch)
        self.mn = np.full(ch, np.inf)
        self.mx = np.full(ch, -np.inf)
        self.clipped = np.zeros(ch, np.int64)
        self.tp = np.zeros(ch)
        self._tp_tail = np.zeros((ch, TP_CTX))            # the frames before the next chunk (zeros before the start)
        if loudness:
            self.w = weights(self.labels)
            self.sos = k_filter(self.rate)
            self.zi = np.zeros((self.sos.shape[0], ch, 2))
            self.hop = max(1, self.rate // 10)            # 100 ms
            self._part = np.zeros(ch)                     # Σ y² of the 100 ms segment not yet whole
            self._part_n = 0
            self._segs: list = []                         # Σ y² per channel of every whole 100 ms segment

    def feed(self, x: np.ndarray) -> None:
        if not len(x):
            return
        x = np.ascontiguousarray(np.asarray(x).T, np.float64)          # (channels, frames): each row contiguous
        self.sum += x.sum(axis=1)
        self.sq += np.einsum("ij,ij->i", x, x)
        self.mn = np.minimum(self.mn, x.min(axis=1))
        self.mx = np.maximum(self.mx, x.max(axis=1))
        self.clipped += (np.abs(x) >= CLIP).sum(axis=1)
        if self.true_peak:
            self._feed_tp(x)
        if self.loudness:
            self._feed_loudness(x)
        self.n += x.shape[1]

    # ── true peak ────────────────────────────────────────────
    def _feed_tp(self, x: np.ndarray) -> None:
        seg = np.concatenate((self._tp_tail, x), axis=1)
        self._tp_tail = seg[:, -TP_CTX:].copy()
        self._tp_segment(seg)

    def _tp_segment(self, seg: np.ndarray) -> None:
        """Oversample the stretches of seg (channels × frames; its first TP_CTX frames are context from before)
        that may hold a new true peak of their channel: the blocks within 6 dB of the largest peak so far. An output
        sample is taken only when all the frames its filter needs are in seg, so chunk boundaries change nothing."""
        n = seg.shape[1]
        nb = -(-n // TP_BLOCK)
        a = np.abs(seg)
        if nb * TP_BLOCK > n:
            a = np.concatenate((a, np.zeros((self.ch, nb * TP_BLOCK - n))), axis=1)
        bp = a.reshape(self.ch, nb, TP_BLOCK).max(axis=2)
        thr = TP_MARGIN * np.maximum(self.tp, bp.max(axis=1))
        lo = TP_TAPS - 1
        for c in range(self.ch):
            if bp[c].max() <= 0:
                continue                                     # digital silence
            idx = np.flatnonzero(bp[c] >= thr[c])
            if not len(idx):
                continue
            for r in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
                s = max(0, r[0] * TP_BLOCK - TP_CTX)
                e = min(n, (r[-1] + 1) * TP_BLOCK + TP_CTX)
                hi = OVERSAMPLE * (e - s)          # output j needs the input frames (j − TAPS + 1) / 4 … j / 4
                if hi <= lo:
                    continue
                y = signal.upfirdn(_tp_filter, seg[c, s:e], up=OVERSAMPLE)[lo:hi]
                self.tp[c] = max(self.tp[c], y.max(), -y.min())

    # ── loudness ─────────────────────────────────────────────
    def _feed_loudness(self, x: np.ndarray) -> None:
        # a −400 dB signal at the Nyquist frequency keeps the filter state out of the denormal numbers (silence
        # would make the filter twenty times slower); its sign follows the frame number, so chunks do not matter
        n = x.shape[1]
        guard = np.where((np.arange(self.n, self.n + n) & 1).astype(bool), -DENORMAL_GUARD, DENORMAL_GUARD)
        y, self.zi = signal.sosfilt(self.sos, x + guard, axis=-1, zi=self.zi)
        y *= y
        hop = self.hop
        pos = 0
        if self._part_n:
            k = min(hop - self._part_n, n)
            self._part += y[:, :k].sum(axis=1)
            self._part_n += k
            pos = k
            if self._part_n == hop:
                self._segs.append(self._part.copy())
                self._part[:] = 0
                self._part_n = 0
        whole = (n - pos) // hop
        if whole:
            self._segs.extend(y[:, pos:pos + whole * hop].reshape(self.ch, whole, hop).sum(axis=2).T)
            pos += whole * hop
        if pos < n:
            self._part += y[:, pos:].sum(axis=1)
            self._part_n += n - pos

    def _blocks(self, segs: np.ndarray, n_seg: int) -> np.ndarray:
        """The weighted mean power of every block of n_seg segments, a segment apart."""
        if len(segs) < n_seg:
            return np.zeros(0)
        power = segs @ self.w / self.hop
        c = np.concatenate(([0.0], np.cumsum(power)))
        return (c[n_seg:] - c[:-n_seg]) / n_seg

    def context(self, before: np.ndarray) -> None:
        """The frames just before the range (a selection inside a file): the true peak at the range's start is
        then computed with its real neighbours instead of silence. Call before feed()."""
        if self.true_peak and len(before):
            x = np.asarray(before, np.float64)[-TP_CTX:].T
            self._tp_tail[:, TP_CTX - x.shape[1]:] = x

    def result(self, after: np.ndarray | None = None) -> Stats:
        """after: the frames just after the range (the same for its end)."""
        ch, n = self.ch, max(1, self.n)
        st = Stats(self.n, self.rate, loudness=self.loudness, has_true_peak=self.true_peak)
        for i, lab in enumerate(self.labels):
            mn = float(self.mn[i]) if self.n else 0.0
            mx = float(self.mx[i]) if self.n else 0.0
            pk = max(abs(mn), abs(mx))
            tp = max(pk, float(self.tp[i])) if self.true_peak else float("nan")
            st.channels.append(ChannelStats(lab, pk, tp, mn, mx, math.sqrt(self.sq[i] / n), self.sum[i] / n,
                                            int(self.clipped[i])))
        if self.true_peak and self.n:
            # the last frames: their filter support runs past the end (zeros there, as before the start)
            post = np.zeros((ch, TP_CTX))
            if after is not None and len(after):
                x = np.asarray(after, np.float64)[:TP_CTX].T
                post[:, :x.shape[1]] = x
            self._tp_segment(np.concatenate((self._tp_tail, post), axis=1))
            for i, c in enumerate(st.channels):
                c.true_peak = max(c.true_peak, float(self.tp[i]))
        if self.loudness:
            segs = np.array(self._segs).reshape(-1, ch)
            m = self._blocks(segs, 4)
            if len(m):
                st.momentary_max = lufs(float(m.max()))
                above = m[m > 10 ** ((ABS_GATE + 0.691) / 10)]
                if len(above):
                    rel = lufs(float(above.mean())) + REL_GATE
                    st.threshold = rel
                    gated = above[above > 10 ** ((rel + 0.691) / 10)]
                    st.integrated = lufs(float(gated.mean())) if len(gated) else float("-inf")
            s = self._blocks(segs, 30)
            if len(s):
                st.short_term_max = lufs(float(s.max()))
                st.lra = loudness_range(s)
        return st


def loudness_range(power: np.ndarray) -> float:
    """EBU Tech 3342 from the powers of the 3 s blocks: gated at −70 LUFS and 20 LU under their mean, the 95th
    minus the 10th percentile (nearest rank, as ffmpeg)."""
    p = power[power > 10 ** ((ABS_GATE + 0.691) / 10)]
    if not len(p):
        return 0.0
    rel = lufs(float(p.mean())) + LRA_GATE
    p = np.sort(p[p > 10 ** ((rel + 0.691) / 10)])
    if len(p) < 2:
        return 0.0
    loud = -0.691 + 10 * np.log10(p)

    def rank(q: float) -> float:
        k = int(q * len(loud) + 0.5)
        return float(loud[max(0, min(len(loud) - 1, k - 1))])
    return max(0.0, rank(0.95) - rank(0.10))


def snapshot_chunks(pieces, channels: int, size: int = store.CHUNK):
    """The frames of a list of pieces (Document.slice) chunk by chunk — a fixed copy of the piece list, so the
    document may change meanwhile."""
    buf = np.empty((size, channels), np.float32)
    fill = 0
    for src, off, n in pieces:
        pos = 0
        while pos < n:
            k = min(size - fill, n - pos)
            src.read(off + pos, k, buf[fill:fill + k])
            fill += k
            pos += k
            if fill == size:
                yield buf
                fill = 0
    if fill:
        yield buf[:fill]


def analyze(d, a: int = 0, b: int | None = None, progress: Progress | None = None, loudness: bool = True,
            true_peak: bool = True) -> Stats:
    """Analyse frames [a, b) of a document (the whole of it by default)."""
    progress = progress or Progress()
    b = d.frames if b is None else b
    a, b = max(0, int(a)), min(d.frames, int(b))
    an = Analyzer(d.rate, d.labels, loudness, true_peak)
    pieces = d.slice(a, b)
    before = list(snapshot_chunks(d.slice(a - TP_CTX, a), d.channels)) if true_peak and a > 0 else []
    after = list(snapshot_chunks(d.slice(b, b + TP_CTX), d.channels)) if true_peak and b < d.frames else []
    if before:
        an.context(before[0])
    total = max(1, b - a)
    done = 0
    progress(0.0)
    for x in snapshot_chunks(pieces, d.channels):
        an.feed(x)
        done += len(x)
        progress(done / total)
    return an.result(after[0] if after else None)
