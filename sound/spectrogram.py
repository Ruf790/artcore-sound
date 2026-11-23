"""The spectrogram of the view (0.4), computed only for what is on screen, in tiles, in the background.

A tile is TILE screen columns at the view's zoom (spp frames a column) × the rows of a lane. Column c holds frames
[c·spp, (c+1)·spp): zoomed in, one FFT centred on it; zoomed out (a whole film), up to MAX_SEG FFTs spread over it
with their powers averaged — so a tile costs the same at any zoom, and a 3-hour 5.1 film takes as long as a song.
The levels are kept quantized to a byte (Q_LO … Q_HI dB, 0 = no sound there), so the palette and the dB range of the
picture are only the colour table it is drawn with: changing them recomputes nothing.

A tile's key holds the settings that shape it (FFT size, frequency scale, rows), the zoom, its index and the piece
list under it (sources, offsets, lengths). After an edit the tiles whose sound did not change — before the edit, or
everywhere after an undo — have the same key and come from the cache; the others are computed anew. The cache is an
LRU limited in bytes. Workers read a fixed copy of the piece list, so an edit meanwhile changes nothing for them."""
from __future__ import annotations

import bisect
import collections
import math
import threading
from dataclasses import asdict, dataclass

import numpy as np
from scipy import fft as sfft

from .spectrum import window

TILE = 256                     # columns per tile
MAX_SEG = 8                    # FFTs averaged per column at most
Q_LO, Q_HI = -180.0, 6.0       # dB range of the stored bytes (1 … 255)
FFT_SIZES = (256, 512, 1024, 2048, 4096, 8192, 16384)
SCALES = ("lin", "log", "mel")
LOG_FMIN = 20.0
CACHE_MB = 64

PALETTES = {
    "magma":   ("#000004", "#1d1147", "#51127c", "#822681", "#b63679", "#e65164", "#fb8861", "#fec287", "#fcfdbf"),
    "fire":    ("#000000", "#1a0050", "#6a0080", "#c0105a", "#ff4010", "#ff9a00", "#ffe040", "#ffffff"),
    "viridis": ("#440154", "#46327e", "#365c8d", "#277f8e", "#1fa187", "#4ac16d", "#a0da39", "#fde725"),
    "gray":    ("#000000", "#ffffff"),
    "ink":     ("#ffffff", "#dbe9f6", "#a6cee4", "#5aa1cf", "#2a6fb4", "#0b3d7e", "#041a3d"),
    "paper":   ("#ffffff", "#fff1c1", "#fdc56e", "#f6863a", "#d9452b", "#99152f", "#3b0a28"),
}
DARK_DEFAULT, LIGHT_DEFAULT = "magma", "ink"


@dataclass
class Settings:
    fft: int = 2048
    scale: str = "log"
    db_lo: float = -110.0
    db_hi: float = 0.0
    palette_dark: str = DARK_DEFAULT
    palette_light: str = LIGHT_DEFAULT

    @classmethod
    def from_dict(cls, v) -> "Settings":
        s = cls()
        v = v if isinstance(v, dict) else {}
        try:
            n = int(v.get("fft", s.fft))
            s.fft = n if n in FFT_SIZES else s.fft
        except (TypeError, ValueError):
            pass
        s.scale = v.get("scale") if v.get("scale") in SCALES else s.scale
        for k in ("db_lo", "db_hi"):
            try:
                setattr(s, k, float(v.get(k, getattr(s, k))))
            except (TypeError, ValueError):
                pass
        s.db_lo = max(-180.0, min(-20.0, s.db_lo))
        s.db_hi = max(s.db_lo + 20.0, min(6.0, s.db_hi))
        for k, dflt in (("palette_dark", DARK_DEFAULT), ("palette_light", LIGHT_DEFAULT)):
            if v.get(k) in PALETTES:
                setattr(s, k, v[k])
            else:
                setattr(s, k, dflt)
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def palette(self, dark: bool) -> str:
        return self.palette_dark if dark else self.palette_light


# ── the frequency axis ────────────────────────────────────────
def _mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f, np.float64) / 700.0)


def _imel(m):
    return 700.0 * (10.0 ** (np.asarray(m, np.float64) / 2595.0) - 1.0)


def f_range(rate: int, scale: str) -> tuple[float, float]:
    top = rate / 2.0
    return (min(LOG_FMIN, top / 4), top) if scale == "log" else (0.0, top)


def y_frac(f, rate: int, scale: str):
    """0 at the bottom of the lane … 1 at the top, for frequency f."""
    lo, hi = f_range(rate, scale)
    f = np.asarray(f, np.float64)
    if scale == "log":
        return np.log(np.maximum(f, lo) / lo) / math.log(hi / lo)
    if scale == "mel":
        return _mel(f) / _mel(hi)
    return f / hi


def f_of_frac(u, rate: int, scale: str):
    lo, hi = f_range(rate, scale)
    u = np.clip(np.asarray(u, np.float64), 0.0, 1.0)
    if scale == "log":
        return lo * (hi / lo) ** u
    if scale == "mel":
        return _imel(u * _mel(hi))
    return u * hi


def row_map(rows: int, rate: int, fft: int, scale: str):
    """How the FFT bins make the rows (row 0 at the top): (lo, wide, centre) — a row that spans at least one bin
    takes the highest power of its bins [lo[i], lo[i+1]); a narrower one the power interpolated at its centre."""
    edges = f_of_frac(np.arange(rows + 1) / rows, rate, scale)          # bottom → top
    bw = rate / fft
    nb = fft // 2 + 1
    idx = np.clip(np.floor(edges / bw).astype(np.int64), 0, nb - 1)
    lo = idx[:-1]
    wide = idx[1:] - lo >= 2
    centre = np.clip((edges[:-1] + edges[1:]) / 2 / bw, 0, nb - 1)
    return lo, wide, centre


def to_rows(power: np.ndarray, rmap) -> np.ndarray:
    """(cols, bins) → (cols, rows) bottom → top."""
    lo, wide, centre = rmap
    mx = np.maximum.reduceat(power, lo, axis=1)
    i0 = np.floor(centre).astype(np.int64)
    i1 = np.minimum(i0 + 1, power.shape[1] - 1)
    fr = centre - i0
    interp = power[:, i0] * (1 - fr) + power[:, i1] * fr
    return np.where(wide[None, :], mx, interp)


def quantize(db: np.ndarray) -> np.ndarray:
    q = np.rint((db - Q_LO) / (Q_HI - Q_LO) * 254.0) + 1.0
    return np.clip(q, 1, 255).astype(np.uint8)


# ── reading a fixed copy of the pieces ────────────────────────
class PieceReader:
    """Frames of a piece list that covers document frames [base, base + Σn) — zeros outside."""

    def __init__(self, pieces, base: int, channels: int):
        self.pieces = list(pieces)
        self.base = int(base)
        self.ch = channels
        self.starts = [self.base]
        for _s, _o, n in self.pieces:
            self.starts.append(self.starts[-1] + n)

    def read(self, pos: int, n: int) -> np.ndarray:
        out = np.zeros((n, self.ch), np.float32)
        a, b = pos, pos + n
        i = max(0, bisect.bisect_right(self.starts, a) - 1)
        while i < len(self.pieces) and self.starts[i] < b:
            src, off, k = self.pieces[i]
            s0 = self.starts[i]
            lo, hi = max(a, s0), min(b, s0 + k)
            if hi > lo:
                src.read(off + lo - s0, hi - lo, out[lo - a:hi - a])
            i += 1
        return out


def tile_span(tile: int, spp: float, fft: int) -> tuple[int, int]:
    """The document frames a tile needs (its columns ± half an FFT)."""
    a = int(math.floor(tile * TILE * spp)) - fft
    b = int(math.ceil((tile + 1) * TILE * spp)) + fft
    return max(0, a), max(0, b)


def compute_tile(reader: PieceReader, frames: int, rate: int, spp: float, tile: int, rows: int, fft: int,
                 scale: str, chs) -> np.ndarray:
    """(len(chs), rows, TILE) bytes, row 0 = the highest frequency; 0 where the document has ended."""
    chs = list(chs)
    w = window("hann", fft)
    gain = (2.0 / w.sum()) ** 2
    m = max(1, min(MAX_SEG, int(math.ceil(spp / fft))))
    c0 = tile * TILE
    cols = np.arange(c0, c0 + TILE)
    starts = cols * spp
    alive = starts < frames
    out = np.zeros((len(chs), rows, TILE), np.uint8)
    if not alive.any():
        return out
    rmap = row_map(rows, rate, fft, scale)
    # segment centres: m per column, evenly over it
    centres = (starts[:, None] + (np.arange(m)[None, :] + 0.5) * spp / m).ravel()
    seg0 = np.floor(centres - fft / 2).astype(np.int64)
    lo_f, hi_f = int(seg0.min()), int(seg0.max()) + fft
    span = hi_f - lo_f
    group = max(1, (1 << 19) // (m * fft * len(chs)))         # columns per batch (½ M samples, a few MB)
    whole = None
    whole_t = None
    all_chs = list(chs) == list(range(reader.ch))
    if span <= max(8 * fft, 1 << 20):                         # near columns: read once (≤ 1 M frames)
        whole = reader.read(lo_f, span)
        whole_t = np.ascontiguousarray(whole.T if all_chs else whole[:, np.asarray(chs)].T)
        whole = None                                          # only the channel-major copy is kept
    live = np.flatnonzero(alive)
    chs_a = np.asarray(chs)
    dense = m * fft >= 0.5 * spp                              # the segments cover most of a column: read it whole
    ar = np.arange(fft)
    for g0 in range(0, len(live), group):
        cs = live[g0:g0 + group]
        sidx = (cs[:, None] * m + np.arange(m)[None, :]).ravel()
        st = seg0[sidx]
        # x: (channels, segments, fft), filled channel-major so the FFT runs over contiguous rows; float64 — the
        # bottom of the dB scale (−150 dB and below) is under float32's rounding
        x = np.empty((len(chs), len(st), fft), np.float64)
        if whole_t is not None:
            x[:] = whole_t[:, (st - lo_f)[:, None] + ar[None, :]]
        elif dense:                                           # a read per column, the segments cut from it
            for k in range(len(cs)):
                s0 = int(st[k * m])
                blk = reader.read(s0, int(st[k * m + m - 1]) + fft - s0)
                bt = blk.T if all_chs else blk[:, chs_a].T
                x[:, k * m:(k + 1) * m] = bt[:, (st[k * m:(k + 1) * m] - s0)[:, None] + ar[None, :]]
        else:
            for k, p0 in enumerate(st):
                blk = reader.read(int(p0), fft)
                x[:, k] = blk.T if all_chs else blk[:, chs_a].T
        x *= w                                                # in place
        # through scipy's FFT: it lets the other tile thread run meanwhile
        spec = sfft.rfft(x, axis=2)
        pw = (spec.real * spec.real + spec.imag * spec.imag) * gain
        pw = pw.reshape(len(chs), len(cs), m, -1).mean(axis=2)                    # (ch, cols, bins)
        for j in range(len(chs)):
            r = to_rows(pw[j], rmap)
            with np.errstate(divide="ignore"):
                db = 10.0 * np.log10(np.maximum(r, 1e-30))
            out[j][:, cs] = quantize(db)[:, ::-1].T
    return out


# ── the cache ─────────────────────────────────────────────────
class TileCache:
    """An LRU of tiles limited in bytes."""

    def __init__(self, limit_bytes: int = CACHE_MB << 20):
        self.limit = int(limit_bytes)
        self._d: collections.OrderedDict = collections.OrderedDict()
        self.bytes = 0
        self.hits = self.misses = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is None:
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key, arr: np.ndarray) -> None:
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.bytes -= old.nbytes
            self._d[key] = arr
            self.bytes += arr.nbytes
            while self.bytes > self.limit and len(self._d) > 1:
                _k, v = self._d.popitem(last=False)
                self.bytes -= v.nbytes

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.bytes = 0


# ── the background workers ────────────────────────────────────
@dataclass
class Job:
    key: tuple
    reader: PieceReader
    frames: int
    rate: int
    spp: float
    tile: int
    rows: int
    fft: int
    scale: str
    chs: tuple


class Engine:
    """Tiles on request: want(jobs) says what the view needs now (most wanted first); the workers compute what the
    cache lacks and call on_ready() from their thread (the view posts an update). Requests the view no longer wants
    are dropped."""

    def __init__(self, cache: TileCache | None = None, workers: int = 2, on_ready=None):
        self.cache = cache if cache is not None else TileCache()
        self.on_ready = on_ready
        self._cv = threading.Condition()
        self._queue: list[Job] = []
        self._busy: set = set()
        self._stop = False
        self.computed = 0
        self.errors = 0
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(max(1, workers))]
        for th in self._threads:
            th.start()

    def want(self, jobs: list[Job]) -> None:
        with self._cv:
            self._queue = [j for j in jobs if j.key not in self._busy]
            self._cv.notify_all()

    def pending(self) -> int:
        with self._cv:
            return len(self._queue) + len(self._busy)

    def idle(self) -> bool:
        return self.pending() == 0

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                job = self._queue.pop(0)
                if job.key in self._busy:
                    continue
                self._busy.add(job.key)
            try:
                if self.cache.get(job.key) is None:
                    arr = compute_tile(job.reader, job.frames, job.rate, job.spp, job.tile, job.rows, job.fft,
                                       job.scale, job.chs)
                    self.cache.put(job.key, arr)
                    self.computed += 1
            except Exception:                    # noqa: BLE001 — a closed document under a worker
                self.errors += 1
            finally:
                with self._cv:
                    self._busy.discard(job.key)
            if self.on_ready is not None:
                try:
                    self.on_ready()
                except Exception:                # noqa: BLE001
                    pass

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._queue = []
            self._cv.notify_all()


def piece_key(pieces) -> tuple:
    return tuple((src.id, off, n) for src, off, n in pieces)


def jobs_for_view(d, start: float, spp: float, width: int, rows: int, st: Settings, chs=None,
                  margin: int = 1) -> tuple[list[int], dict]:
    """The tiles a view [start, start + width·spp) needs (and `margin` either side): their indices (the visible
    ones first, from the middle out) and {tile: Job}."""
    chs = tuple(range(d.channels) if chs is None else chs)
    c0 = int(math.floor(start / spp))
    c1 = int(math.ceil((start + width * spp) / spp))
    t0, t1 = c0 // TILE, max(c0, c1 - 1) // TILE
    last = max(0, int(math.ceil(d.frames / spp)) - 1) // TILE
    vis = list(range(max(0, t0), min(last, t1) + 1))
    mid = (t0 + t1) / 2
    vis.sort(key=lambda i: abs(i - mid))
    extra = [i for k in range(1, margin + 1) for i in (t0 - k, t1 + k) if 0 <= i <= last]
    order = vis + extra
    jobs = {}
    sppk = round(spp, 9)
    for i in order:
        a, b = tile_span(i, spp, st.fft)
        pieces = d.slice(a, b)
        key = (st.fft, st.scale, d.rate, sppk, i, rows, chs, piece_key(pieces), a)
        jobs[i] = Job(key, PieceReader(pieces, a, d.channels), d.frames, d.rate, spp, i, rows, st.fft, st.scale, chs)
    return order, jobs


def color_table(palette: str, db_lo: float, db_hi: float) -> list[int]:
    """256 ARGB colours: 0 transparent, 1 … 255 the stored levels mapped through the dB range onto the palette."""
    stops = PALETTES.get(palette) or PALETTES[DARK_DEFAULT]
    rgb = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in stops], np.float64)
    q = np.arange(256)
    lv = Q_LO + (q - 1) / 254.0 * (Q_HI - Q_LO)
    u = np.clip((lv - db_lo) / max(1e-6, db_hi - db_lo), 0.0, 1.0) * (len(rgb) - 1)
    i0 = np.minimum(np.floor(u).astype(int), len(rgb) - 2) if len(rgb) > 1 else np.zeros(256, int)
    fr = (u - i0)[:, None]
    col = rgb[i0] * (1 - fr) + rgb[np.minimum(i0 + 1, len(rgb) - 1)] * fr
    out = [0]
    for r, g, b in col[1:].astype(int):
        out.append((0xFF << 24) | (int(r) << 16) | (int(g) << 8) | int(b))
    return out


def freq_ticks(rate: int, scale: str, height: float, min_px: float = 16.0) -> list[float]:
    """The frequencies to label on a lane of this height (at least min_px apart)."""
    if scale == "lin":
        top = rate / 2
        for step in (100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            if height * step / top >= min_px * 1.6:
                break
        cand = list(np.arange(step, top, step))
    else:
        cand = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 40000]
        if scale == "mel":
            cand = [100, 250, 500, 1000, 2000, 4000, 8000, 16000]
        cand = [f for f in cand if f < rate / 2]
    out, last = [], -1e9
    for f in cand:
        y = float(y_frac(f, rate, scale)) * height
        if y - last >= min_px and height - y >= 8:
            out.append(float(f))
            last = y
    return out


def fmt_freq(f: float) -> str:
    if f >= 1000:
        k = f / 1000
        return f"{k:.0f}k" if abs(k - round(k)) < 1e-9 else f"{k:.1f}k"
    return f"{f:.0f}"
