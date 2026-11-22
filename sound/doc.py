"""A document: the sound as a list of pieces (source, offset, frames) — a piece table. Cutting, pasting, deleting,
silence and trimming only rearrange pieces; an effect writes the frames it changed into a new source and puts a
piece of it in their place. Every state of the list is kept in the history, so undo and redo are unlimited and
cost a few bytes per step; the sound itself stays on disk (store.py)."""
from __future__ import annotations

import bisect
import itertools
import math
import os
from dataclasses import dataclass, field

import numpy as np

from . import store
from .store import BASE, FACTOR, Source

Piece = tuple            # (Source, offset in the source, frames)
RAW_SPP = BASE * FACTOR  # frames per column below which the view reads the frames themselves


@dataclass
class State:
    pieces: tuple
    label: str
    rate: int
    channels: int
    labels: tuple
    layout: str
    sel: tuple | None
    cursor: int
    id: int = 0
    sel_before: tuple | None = None              # the selection / cursor just before this step (undo gives
    cursor_before: int = 0                       # them back)


@dataclass
class Clip:
    """What Copy / Cut put on the clipboard: pieces of sources (no sound copied)."""
    pieces: tuple
    frames: int
    rate: int
    channels: int
    labels: tuple
    layout: str = ""


_state_ids = itertools.count(1)


def _norm(pieces) -> tuple:
    """Drop empty pieces, join neighbours that continue each other in the same source."""
    out: list = []
    for src, off, n in pieces:
        if n <= 0:
            continue
        if out:
            ps, po, pn = out[-1]
            if ps is src and (src.kind == "zero" or po + pn == off):
                out[-1] = (ps, po, pn + n)
                continue
        out.append((src, off, n))
    return tuple(out)


@dataclass
class Document:
    rate: int
    channels: int
    folder: str                                  # this document's working folder (session subfolder)
    labels: tuple = ()
    layout: str = ""
    name: str = ""
    path: str = ""                               # the file it came from / was saved to
    info: object = None                          # ff.FileInfo of the opened file
    track: object = None                         # ff.Track that was opened
    save_format: str = ""                        # the format Save writes without asking (export.FORMATS key)
    save_opts: dict = field(default_factory=dict)
    sel: tuple | None = None                     # (start, end) frames, end exclusive
    cursor: int = 0
    view_start: float = 0.0                      # the first frame at the left edge of the wave view
    spp: float = 0.0                             # frames per pixel (0 = fit the whole file)

    def __post_init__(self):
        self.pieces: tuple = ()
        self._starts: list = [0]
        self.frames = 0
        self.history: list[State] = []
        self.pos = -1
        self.version = 0                          # changes with every edit / undo (caches use it)
        self.saved_id = 0
        self.zero = Source.zero(self.channels)
        if not self.labels:
            from .ff import labels as _labels
            self.labels = tuple(_labels(self.channels, self.layout))

    # ── pieces ────────────────────────────────────────────────
    def _set(self, pieces) -> None:
        self.pieces = _norm(pieces)
        starts = [0]
        for _s, _o, n in self.pieces:
            starts.append(starts[-1] + n)
        self._starts = starts
        self.frames = starts[-1]
        self.version += 1

    def set_initial(self, src: Source, label: str = "") -> None:
        self._set([(src, 0, src.frames)])
        self.history = []
        self.pos = -1
        self._push(label or "open")
        self.saved_id = self.history[-1].id

    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    def slice(self, a: int, b: int) -> list:
        """The pieces covering frames [a, b) of the document."""
        a, b = max(0, a), min(self.frames, b)
        if b <= a:
            return []
        out = []
        i = bisect.bisect_right(self._starts, a) - 1
        while i < len(self.pieces) and self._starts[i] < b:
            src, off, n = self.pieces[i]
            s0 = self._starts[i]
            lo, hi = max(a, s0), min(b, s0 + n)
            if hi > lo:
                out.append((src, off + lo - s0, hi - lo))
            i += 1
        return out

    def read(self, start: int, n: int) -> np.ndarray:
        """Frames [start, start + n) as a (n, channels) float32 array (zeros past the end)."""
        out = np.zeros((max(0, n), self.channels), np.float32)
        pos = 0
        for src, off, k in self.slice(start + 0, start + n):
            src.read(off, k, out[pos:pos + k])
            pos += k
        return out

    def chunks(self, a: int, b: int, size: int = store.CHUNK):
        """Yield (position, frames) over [a, b) in steps of `size` frames."""
        pos = a
        while pos < b:
            n = min(size, b - pos)
            yield pos, self.read(pos, n)
            pos += n

    # ── the waveform ──────────────────────────────────────────
    def minmax(self, start: float, spp: float, ncols: int, channels=None) -> np.ndarray:
        """Min / max of each of ncols columns of spp frames from `start`: (ncols, channels, 2), NaN past the end.
        spp ≥ 1. Below RAW_SPP frames per column from the frames themselves; above, from the peak level whose blocks
        are at most a quarter of a column (a block that straddles two columns counts for both, so a peak is never
        lost — the envelope is at most a quarter column wider than exact)."""
        chs = list(range(self.channels)) if channels is None else list(channels)
        res = np.full((ncols, len(chs), 2), np.nan, np.float32)
        if ncols <= 0 or self.frames <= 0 or spp <= 0:
            return res
        start = max(0.0, float(start))
        end = min(float(self.frames), start + spp * ncols)
        if end <= start:
            return res
        if spp < RAW_SPP:
            # from the frames themselves, a group of columns at a time (≤ 256 K frames read at once)
            group = max(1, int(262144 // spp))
            for c0 in range(0, ncols, group):
                c1 = min(ncols, c0 + group)
                s0 = start + c0 * spp
                if s0 >= self.frames:
                    break
                self._raw_columns(res, c0, c1, s0, spp, chs)
            return res
        level = max(0, int(math.floor(math.log(spp / (BASE * FACTOR), FACTOR) + 1e-9)))
        mins = np.full((ncols, len(chs)), np.inf, np.float32)
        maxs = np.full((ncols, len(chs)), -np.inf, np.float32)
        a, b = int(start), int(math.ceil(end))
        i = bisect.bisect_right(self._starts, a) - 1
        while i < len(self.pieces) and self._starts[i] < b:
            src, off, n = self.pieces[i]
            d0 = self._starts[i]
            lo, hi = max(a, d0), min(b, d0 + n)
            for p, step, pend, arr in store.source_minmax(src, off + lo - d0, off + hi - d0, level):
                k = arr.shape[0]
                dp = d0 - off + p + np.arange(k, dtype=np.int64) * step
                de = np.minimum(dp + step, d0 - off + pend)
                ca = np.clip(((dp - start) / spp).astype(np.int64), 0, ncols - 1)
                cb = np.clip(((de - 1 - start) / spp).astype(np.int64), 0, ncols - 1)
                v = arr[:, chs]
                for c in (ca, cb):
                    np.minimum.at(mins, c, v[..., 0])
                    np.maximum.at(maxs, c, v[..., 1])
            i += 1
        got = np.isfinite(mins[:, 0])
        res[got, :, 0] = mins[got]
        res[got, :, 1] = maxs[got]
        # a column between two blocks that got nothing (rounding) takes its left neighbour
        last = int(min(ncols - 1, (end - 1 - start) // spp))
        for c in np.flatnonzero(~got[:last + 1]):
            if c:
                res[c] = res[c - 1]
        return res

    def _raw_columns(self, res: np.ndarray, c0: int, c1: int, s0: float, spp: float, chs: list) -> None:
        a = int(s0)
        end = min(float(self.frames), s0 + spp * (c1 - c0))
        n = int(math.ceil(end)) - a
        x = self.read(a, n)
        if len(chs) != self.channels:
            x = x[:, chs]
        edges = np.floor(s0 + np.arange(c1 - c0) * spp).astype(np.int64) - a
        ok = edges < n
        e = edges[ok]
        if not len(e):
            return
        e = np.maximum.accumulate(e)
        uniq, first = np.unique(e, return_index=True)
        cols = c0 + np.flatnonzero(ok)[first]
        res[cols, :, 0] = np.minimum.reduceat(x, uniq, axis=0)
        res[cols, :, 1] = np.maximum.reduceat(x, uniq, axis=0)
        filled = np.zeros(c1 - c0, bool)
        filled[cols - c0] = True
        for c in np.flatnonzero(ok & ~filled):          # columns that share a frame with the one before (spp ≈ 1)
            res[c0 + c] = res[c0 + c - 1]

    def peak(self, a: int = 0, b: int | None = None) -> np.ndarray:
        """The largest |sample| per channel in [a, b) — exact, from the peak levels."""
        b = self.frames if b is None else b
        if b <= a:
            return np.zeros(self.channels, np.float32)
        mm = self.minmax(a, max(1.0, float(b - a)), 1)
        return np.nan_to_num(np.maximum(-mm[0, :, 0], mm[0, :, 1]))

    # ── history ───────────────────────────────────────────────
    def _push(self, label: str, sel_before=None, cursor_before: int = 0) -> None:
        st = State(self.pieces, label, self.rate, self.channels, tuple(self.labels), self.layout, self.sel,
                   self.cursor, next(_state_ids), sel_before, cursor_before)
        del self.history[self.pos + 1:]
        self.history.append(st)
        self.pos = len(self.history) - 1

    def _restore(self, st: State) -> None:
        if (st.rate, st.channels) != (self.rate, self.channels):
            self.zero = Source.zero(st.channels)
        self.rate, self.channels, self.labels, self.layout = st.rate, st.channels, st.labels, st.layout
        self._set(st.pieces)
        self.sel = self._clip_sel(st.sel)
        self.cursor = min(st.cursor, self.frames)

    def can_undo(self) -> bool:
        return self.pos > 0

    def can_redo(self) -> bool:
        return self.pos < len(self.history) - 1

    def undo_label(self) -> str:
        return self.history[self.pos].label if self.can_undo() else ""

    def redo_label(self) -> str:
        return self.history[self.pos + 1].label if self.can_redo() else ""

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        cur = self.history[self.pos]
        self.pos -= 1
        self._restore(self.history[self.pos])
        self.sel = self._clip_sel(cur.sel_before)
        self.cursor = min(cur.cursor_before, self.frames)
        return True

    def redo(self) -> bool:
        if not self.can_redo():
            return False
        self.pos += 1
        self._restore(self.history[self.pos])
        return True

    def goto_state(self, i: int) -> None:
        """Jump to a step of the history (the Undo history list)."""
        if 0 <= i < len(self.history) and i != self.pos:
            back = i < self.pos
            nxt = self.history[i + 1] if back else None
            self.pos = i
            self._restore(self.history[i])
            if nxt is not None:
                self.sel, self.cursor = self._clip_sel(nxt.sel_before), min(nxt.cursor_before, self.frames)

    @property
    def dirty(self) -> bool:
        return bool(self.history) and self.history[self.pos].id != self.saved_id

    def mark_saved(self) -> None:
        if self.history:
            self.saved_id = self.history[self.pos].id

    def _clip_sel(self, sel):
        if not sel:
            return None
        a, b = max(0, min(sel[0], self.frames)), max(0, min(sel[1], self.frames))
        return (a, b) if b > a else None

    def _apply(self, label: str, pieces, sel=None, cursor=None, **meta) -> None:
        before = (self.sel, self.cursor)
        for k, v in meta.items():
            setattr(self, k, v)
        if "channels" in meta:
            self.zero = Source.zero(self.channels)
        self._set(pieces)
        self.sel = self._clip_sel(sel)
        self.cursor = max(0, min(self.frames, self.cursor if cursor is None else cursor))
        self._push(label, *before)

    # ── edits ─────────────────────────────────────────────────
    def _range(self, a, b) -> tuple[int, int]:
        a, b = int(a), int(b)
        if a > b:
            a, b = b, a
        return max(0, a), min(self.frames, b)

    def copy(self, a: int, b: int) -> Clip | None:
        a, b = self._range(a, b)
        if b <= a:
            return None
        return Clip(tuple(self.slice(a, b)), b - a, self.rate, self.channels, tuple(self.labels), self.layout)

    def delete(self, a: int, b: int, label: str = "delete") -> None:
        a, b = self._range(a, b)
        if b <= a:
            return
        self._apply(label, self.slice(0, a) + self.slice(b, self.frames), None, a)

    def cut(self, a: int, b: int, label: str = "cut") -> Clip | None:
        clip = self.copy(a, b)
        if clip:
            self.delete(a, b, label)
        return clip

    def fits(self, clip: Clip) -> bool:
        return clip.rate == self.rate and clip.channels == self.channels

    def paste(self, clip: Clip, a: int, b: int | None = None, label: str = "paste") -> None:
        """Insert the clip at a, or put it in place of [a, b) (a selection). The clip must fit (fits())."""
        if not self.fits(clip):
            raise ValueError("the clip has another rate / number of channels")
        a, b = self._range(a, a if b is None else b)
        self._apply(label, self.slice(0, a) + list(clip.pieces) + self.slice(b, self.frames),
                    (a, a + clip.frames), a + clip.frames)

    def silence(self, a: int, b: int, label: str = "silence") -> None:
        a, b = self._range(a, b)
        if b <= a:
            return
        self._apply(label, self.slice(0, a) + [(self.zero, 0, b - a)] + self.slice(b, self.frames), (a, b), a)

    def insert_silence(self, at: int, n: int, label: str = "insert silence") -> None:
        at = max(0, min(self.frames, int(at)))
        if n <= 0:
            return
        self._apply(label, self.slice(0, at) + [(self.zero, 0, int(n))] + self.slice(at, self.frames),
                    (at, at + int(n)), at)

    def trim(self, a: int, b: int, label: str = "trim") -> None:
        """Keep only [a, b) (Sound Forge's «Trim/Crop»)."""
        a, b = self._range(a, b)
        if b <= a or (a, b) == (0, self.frames):
            return
        self._apply(label, self.slice(a, b), None, 0)

    def keep(self, a: int, b: int, a2: int, b2: int, label: str = "trim silence") -> None:
        """Of [a, b) keep only [a2, b2) — the rest of the document stays (Обрезать тишину: no sound is written).
        A selection becomes the part kept."""
        a, b = self._range(a, b)
        a2, b2 = max(a, int(a2)), min(b, int(b2))
        if b2 <= a2 or (a2, b2) == (a, b):
            return
        whole = (a, b) == (0, self.frames)
        sel = None if whole or not self.sel else (a, a + b2 - a2)
        self._apply(label, self.slice(0, a) + self.slice(a2, b2) + self.slice(b, self.frames), sel, a)

    def replace(self, a: int, b: int, src: Source, label: str, sel=..., **meta) -> None:
        """Put the whole source in place of [a, b) — the result of an effect. The source may be longer or
        shorter (tempo, resample); with meta (rate, channels …) the whole document is replaced."""
        a, b = self._range(a, b)
        new = [(src, 0, src.frames)]
        if meta:
            pieces, s = new, (a, a + src.frames) if sel is ... else sel
        else:
            pieces = self.slice(0, a) + new + self.slice(b, self.frames)
            s = (a, a + src.frames) if sel is ... else sel
        self._apply(label, pieces, s, a, **meta)

    def sources(self) -> set:
        """Every source the history still uses (the working files to keep)."""
        out = set()
        for st in self.history:
            for src, _o, _n in st.pieces:
                out.add(src)
        return out

    def disk_bytes(self) -> int:
        return sum(s.size_bytes() for s in self.sources())

    def close(self) -> None:
        for s in self.sources():
            s.close()
        self.history = []
        self.pieces = ()
        store.remove_folder(self.folder)


def new_document(folder: str, rate: int, channels: int, layout: str = "", name: str = "") -> Document:
    from .ff import labels, layout_for
    lay = layout_for(channels, layout)
    d = Document(rate=rate, channels=channels, folder=folder, labels=tuple(labels(channels, lay)), layout=lay,
                 name=name)
    d.set_initial(store.from_array(folder, np.zeros((0, channels), np.float32)), "new")
    return d


def fmt_time(frames: float, rate: int, ms: bool = True) -> str:
    """0:01:02.345 (hours always shown, as the counters of audio editors)."""
    if rate <= 0:
        return "0:00:00.000"
    sec = max(0.0, frames / rate)
    total_ms = int(round(sec * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{msec:03d}" if ms else f"{h}:{m:02d}:{s:02d}"


def parse_time(text: str, rate: int) -> int | None:
    """«1:02.5», «0:01:02.500», «62.5», «2976000 smp» → frames (None if not a time)."""
    s = text.strip().replace(",", ".").lower()
    if not s:
        return None
    try:
        if s.endswith(("smp", "s#", "samples")):
            return max(0, int(float(s.rstrip("smpleas#").strip())))
        parts = [float(x) for x in s.split(":")]
    except ValueError:
        return None
    if len(parts) > 3 or any(p < 0 for p in parts):
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return int(round(sec * rate))


def folder_size(path: str) -> int:
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total
