"""The sound on disk. A Source is one raw float32 file (interleaved frames × channels) opened as a memmap, with its
peaks precomputed at several levels: level 0 holds the min / max of every 256 frames, each next level of every 4
blocks of the one below (1024, 4096 … frames). A document never copies sound in memory: it is a list of pieces of
sources (doc.py), and every edit or effect writes only the frames it changed into a new source.

All the working files of a run live in one session folder (Session); a lock file in it tells a later run whether
the folder is still in use, so the folders of crashed runs are removed at the next start."""
from __future__ import annotations

import itertools
import os
import shutil
import threading
import uuid

import numpy as np

BASE = 256                 # frames per block at level 0
FACTOR = 4                 # blocks of a level per block of the next one
MIN_BLOCKS = 4             # the last level has at least this many blocks
CHUNK = 1 << 16            # frames per read / write step (64 K frames: 1.5 MB for 5.1 float)


class Source:
    """A finished, read-only raw float32 file + its peak levels. kind "zero" = silence (no file)."""

    _ids = itertools.count(1)

    def __init__(self, path: str, frames: int, channels: int, levels: list | None = None, kind: str = "data"):
        self.id = next(Source._ids)
        self.path, self.frames, self.channels, self.kind = path, int(frames), int(channels), kind
        self.data = None
        if kind == "data" and self.frames:
            self.data = np.memmap(path, dtype=np.float32, mode="r", shape=(self.frames, self.channels))
        self.levels = levels or []          # [np.ndarray (blocks, channels, 2)] for BASE, BASE*4 …

    @classmethod
    def zero(cls, channels: int) -> "Source":
        """Silence of any length (pieces of it say how long)."""
        return cls("", 0, channels, kind="zero")

    def block(self, level: int) -> int:
        return BASE * FACTOR ** level

    def read(self, start: int, n: int, out: np.ndarray | None = None) -> np.ndarray:
        if out is None:
            out = np.empty((n, self.channels), np.float32)
        if self.kind == "zero":
            out[:n] = 0
        else:
            out[:n] = self.data[start:start + n]
        return out

    def size_bytes(self) -> int:
        n = self.frames * self.channels * 4 if self.kind == "data" else 0
        return n + sum(lv.nbytes for lv in self.levels)

    def close(self):
        self.data = None
        self.levels = []


def _block_peaks(x: np.ndarray) -> np.ndarray:
    """(n, ch) frames → (ceil(n / BASE), ch, 2) min / max per block."""
    n, ch = x.shape
    full = n // BASE
    parts = []
    if full:
        v = x[:full * BASE].reshape(full, BASE, ch)
        parts.append(np.stack((v.min(axis=1), v.max(axis=1)), axis=-1))
    if n % BASE:
        r = x[full * BASE:]
        parts.append(np.stack((r.min(axis=0), r.max(axis=0)), axis=-1)[None])
    return np.concatenate(parts) if len(parts) > 1 else parts[0]


def _reduce(lv: np.ndarray) -> np.ndarray:
    """A level → the next one (every FACTOR blocks into one; a short tail block too)."""
    n = lv.shape[0]
    full = n // FACTOR
    parts = []
    if full:
        v = lv[:full * FACTOR].reshape(full, FACTOR, lv.shape[1], 2)
        parts.append(np.stack((v[..., 0].min(axis=1), v[..., 1].max(axis=1)), axis=-1))
    if n % FACTOR:
        r = lv[full * FACTOR:]
        parts.append(np.stack((r[..., 0].min(axis=0), r[..., 1].max(axis=0)), axis=-1)[None])
    return np.concatenate(parts) if len(parts) > 1 else parts[0]


class SourceWriter:
    """Builds a source: append() float32 frames of any length; finish() → Source with all its peak levels.
    Level 0 is written alongside the sound; the levels above are made from it at the end, in chunks, so even a
    three-hour 5.1 film needs only a few megabytes of memory."""

    def __init__(self, folder: str, channels: int):
        self.channels = int(channels)
        self.path = os.path.join(folder, f"{uuid.uuid4().hex[:12]}.f32")
        self._f = open(self.path, "wb")
        self._pk = open(self.path + ".p0", "wb")
        self._tail = np.empty((0, self.channels), np.float32)   # frames not yet a whole block
        self.frames = 0
        self._blocks0 = 0

    def append(self, x: np.ndarray) -> None:
        x = np.ascontiguousarray(x, dtype=np.float32).reshape(-1, self.channels)
        if not len(x):
            return
        self._f.write(x.tobytes())
        self.frames += len(x)
        if len(self._tail):
            x = np.concatenate((self._tail, x))
        full = len(x) // BASE * BASE
        if full:
            pk = _block_peaks(x[:full])
            self._pk.write(pk.tobytes())
            self._blocks0 += len(pk)
        self._tail = x[full:].copy()

    def abort(self) -> None:
        for f in (self._f, self._pk):
            try:
                f.close()
            except OSError:
                pass
        for p in (self.path, self.path + ".p0"):
            try:
                os.remove(p)
            except OSError:
                pass

    def finish(self) -> Source:
        try:
            return self._finish()
        except BaseException:
            self.abort()
            for k in range(1, 16):
                try:
                    os.remove(f"{self.path}.p{k}")
                except OSError:
                    pass
            raise

    def _finish(self) -> Source:
        if len(self._tail):
            pk = _block_peaks(self._tail)
            self._pk.write(pk.tobytes())
            self._blocks0 += len(pk)
        self._f.close()
        self._pk.close()
        ch = self.channels
        if not self.frames:
            self.abort()
            return Source("", 0, ch, kind="data")
        levels = [np.memmap(self.path + ".p0", dtype=np.float32, mode="r", shape=(self._blocks0, ch, 2))]
        k = 0
        while levels[-1].shape[0] > MIN_BLOCKS * FACTOR:
            prev = levels[-1]
            k += 1
            path = f"{self.path}.p{k}"
            step = FACTOR * 65536                     # blocks of the level below per step
            n = 0
            with open(path, "wb") as f:
                for i in range(0, prev.shape[0], step):
                    nxt = _reduce(np.asarray(prev[i:i + step]))
                    f.write(nxt.tobytes())
                    n += nxt.shape[0]
            levels.append(np.memmap(path, dtype=np.float32, mode="r", shape=(n, ch, 2)))
        return Source(self.path, self.frames, ch, levels)


def open_source(path: str, frames: int, channels: int) -> Source:
    """A finished source again from its files (the recovery after a crash): the sound and its peak levels, as
    SourceWriter left them. ValueError when a file is missing or of the wrong size."""
    if not frames:
        return Source("", 0, channels, kind="data")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ValueError(f"missing {path}") from e
    if size != frames * channels * 4:
        raise ValueError(f"damaged {path}")
    levels = []
    k = 0
    while os.path.isfile(f"{path}.p{k}"):
        n = os.path.getsize(f"{path}.p{k}") // (channels * 8)
        if not n:
            break
        levels.append(np.memmap(f"{path}.p{k}", dtype=np.float32, mode="r", shape=(n, channels, 2)))
        k += 1
    if not levels or levels[0].shape[0] != -(-frames // BASE):
        raise ValueError(f"no peaks for {path}")
    return Source(path, frames, channels, levels)


def from_array(folder: str, x: np.ndarray) -> Source:
    """A source made from frames in memory (small things: tests, a short effect result)."""
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    w = SourceWriter(folder, x.shape[1])
    for i in range(0, len(x), CHUNK):
        w.append(x[i:i + CHUNK])
    return w.finish()


def source_minmax(src: Source, s0: int, s1: int, level: int) -> list[tuple[int, int, int, np.ndarray]]:
    """The min / max of the source frames [s0, s1) as pieces (start, step, end, (n, ch, 2) array): entry i of a
    piece covers [start + i·step, min(start + (i+1)·step, end)). Whole blocks of `level` inside the range, the
    edges from the levels below and, under level 0, the frames themselves — every frame of the range is in exactly
    one entry, so the result is exact."""
    out: list = []
    if s1 <= s0:
        return out
    if src.kind == "zero":
        out.append((s0, s1 - s0, s1, np.zeros((1, src.channels, 2), np.float32)))
        return out
    _mm(src, s0, s1, min(level, len(src.levels) - 1), out)
    return out


def _mm(src: Source, s0: int, s1: int, level: int, out: list) -> None:
    if level < 0:
        x = src.data[s0:s1]
        out.append((s0, s1 - s0, s1, np.stack((x.min(axis=0), x.max(axis=0)), axis=-1)[None]))
        return
    b = src.block(level)
    nb = src.levels[level].shape[0]
    b0 = -(-s0 // b)                                   # the first whole block
    b1 = nb if s1 >= src.frames else min(s1 // b, nb)  # the short last block is whole when the range reaches it
    if b1 <= b0:
        _mm(src, s0, s1, level - 1, out)
        return
    if s0 < b0 * b:
        _mm(src, s0, b0 * b, level - 1, out)
    end = min(b1 * b, src.frames)
    out.append((b0 * b, b, end, np.asarray(src.levels[level][b0:b1])))
    if end < s1:
        _mm(src, end, s1, level - 1, out)


# ── the session folder ────────────────────────────────────────
class Session:
    """The working folder of this run: <root>\\s-<id>\\ with a lock file. Every document gets its own subfolder."""

    def __init__(self, root: str, clean: bool = True):
        from PySide6.QtCore import QLockFile
        self.root = root
        os.makedirs(root, exist_ok=True)
        if clean:
            clean_stale(root)
        self.path = os.path.join(root, "s-" + uuid.uuid4().hex[:10])
        os.makedirs(self.path)
        self._lock = QLockFile(os.path.join(self.path, "lock"))
        self._lock.setStaleLockTime(0)
        self._lock.tryLock(100)
        self._n = itertools.count(1)
        self._mutex = threading.Lock()
        self._adopted: list = []

    def adopt(self, path: str) -> None:
        """Take over the folder of a run that is gone (its documents were recovered): locked while this run lives,
        removed with this session."""
        from PySide6.QtCore import QLockFile
        if path == self.path or any(p == path for p, _l in self._adopted):
            return
        lock = QLockFile(os.path.join(path, "lock"))
        lock.setStaleLockTime(0)
        lock.tryLock(100)
        self._adopted.append((path, lock))

    def folder(self) -> str:
        with self._mutex:
            p = os.path.join(self.path, f"d{next(self._n)}")
        os.makedirs(p, exist_ok=True)
        return p

    def used_bytes(self) -> int:
        total = 0
        for dp, _, fs in os.walk(self.path):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total

    def close(self) -> None:
        try:
            self._lock.unlock()
        except Exception:
            pass
        shutil.rmtree(self.path, ignore_errors=True)
        for path, lock in self._adopted:
            try:
                lock.unlock()
            except Exception:
                pass
            shutil.rmtree(path, ignore_errors=True)
        self._adopted = []


def clean_stale(root: str) -> int:
    """Remove the session folders of runs that are gone (their lock is free). → how many were removed."""
    from PySide6.QtCore import QLockFile
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(root, name)
        if not (name.startswith("s-") and os.path.isdir(p)):
            continue
        lock = QLockFile(os.path.join(p, "lock"))
        lock.setStaleLockTime(0)
        if lock.tryLock(0):
            lock.unlock()
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n


def remove_folder(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
