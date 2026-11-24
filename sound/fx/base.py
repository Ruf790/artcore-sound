"""The frame of the effects. An Effect describes its parameters (kind, range, unit, default) and makes a Processor:
a stateful filter fed the sound chunk by chunk — n frames in, exactly n frames out, whatever n is. All its state
(filter memories, ring buffers, the frames of a block not yet whole) lives between the calls, and whatever works on
blocks (an STFT, AR blocks) cuts them at absolute positions, so the result of one call on the whole and of many calls
on chunks of any lengths is the same. A processor may lag behind its input by `latency` frames (a look-ahead, an
STFT); render() and the preview make up for it.

render() runs an effect over a range of a document into a new source, chunk by chunk (only a chunk and the effect's
state in memory — a three-hour 5.1 film too). With «keep the tail» an effect that rings on (delay, reverb) goes on
past the end of the range and its tail is added to the sound that follows — not inserted.

PreviewChain is what the player runs in its reader thread while a dialog previews an effect: parameter changes are
picked up at the next block, Bypass fades between the effect and the dry sound (delayed by the effect's latency so the
two line up)."""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field

import numpy as np

from .. import store
from ..media import Progress

FFT_WORKERS = max(1, os.cpu_count() or 1)   # threads of the effects' FFTs (1 in the batch's worker processes)
SILENT = 1e-5                 # −100 dBFS: a tail quieter than this for TAIL_QUIET seconds has ended
TAIL_QUIET = 0.25


@dataclass
class Param:
    """One parameter of an effect. kind: float | int | bool | choice. A choice's `choices` are (value, i18n key)."""
    key: str
    kind: str = "float"
    lo: float = 0.0
    hi: float = 1.0
    default: object = 0.0
    unit: str = ""
    decimals: int = 1
    log: bool = False             # the slider moves on a logarithmic scale (frequencies, times)
    choices: tuple = ()
    label: str = ""               # i18n key (default "fx_" + key)
    group: str = ""
    step: float = 0.0

    def clean(self, v):
        if self.kind == "bool":
            return bool(v)
        if self.kind == "choice":
            vals = [c[0] for c in self.choices]
            return v if v in vals else self.default
        try:
            v = float(v)
        except (TypeError, ValueError):
            return self.default
        if not math.isfinite(v):
            return self.default
        v = min(self.hi, max(self.lo, v))
        return int(round(v)) if self.kind == "int" else v


class Processor:
    """Base of the stateful processors. Subclasses set up in configure() (called by set()) and work in _process()
    on float64 (n, channels) arrays."""
    latency = 0

    def __init__(self, effect: "Effect", rate: int, labels, values: dict):
        self.effect = effect
        self.rate = int(rate)
        self.labels = list(labels)
        self.ch = len(self.labels)
        self.gr_db = 0.0                       # the gain reduction of the last block (compressor, limiter), dB
        self.p: dict = {}
        self.set(values)

    def set(self, values: dict) -> None:
        old = self.p
        self.p = self.effect.clean(values)
        self.configure(old)

    def configure(self, old: dict) -> None:
        pass

    def reset(self) -> None:
        """Forget the sound so far (the state as at the start). configure(old) with an empty `old` builds all of
        the state anew."""
        self.configure({})

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64).reshape(-1, self.ch)
        if not len(x):
            return np.zeros((0, self.ch), np.float32)
        return self._process(x).astype(np.float32, copy=False)

    def _process(self, x: np.ndarray) -> np.ndarray:
        return x

    def tail_frames(self) -> int:
        """How long the effect rings on after its input stops (0: not at all)."""
        return 0


class Effect:
    """key, name (i18n key), params; processor() makes the processor. realtime=False: rendered (through ffmpeg),
    the preview prepares a piece ahead. has_tail: the «keep the tail» option applies."""
    key = ""
    name = ""
    params: list = []
    realtime = True
    has_tail = False
    proc_class = Processor
    presets: dict = {}                # i18n key → values (the built-in presets)

    def __init__(self):
        self.by_key = {p.key: p for p in self.params}

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def clean(self, values: dict | None) -> dict:
        values = values or {}
        return {p.key: p.clean(values.get(p.key, p.default)) for p in self.params}

    def processor(self, rate: int, labels, values: dict) -> Processor:
        return self.proc_class(self, rate, labels, values)

    def label(self, values: dict) -> str:
        """The undo step's name."""
        from ..i18n import t
        return t(self.name)


# ── running an effect over a range ────────────────────────────
def render(d, a: int, b: int, effect: Effect, values: dict, progress: Progress | None = None,
           keep_tail: bool = False, info: dict | None = None) -> store.Source:
    """The frames of [a, b) of the document through the effect → a new source. Its length is b − a, or more with
    keep_tail: the tail past b added to the document's own frames there (silence past the end) — the source then
    takes the place of [a, a + its length). info["proc"]: the processor afterwards (what it counted)."""
    if not effect.realtime:
        return effect.render(d, a, b, values, progress)
    progress = progress or Progress()
    proc = effect.processor(d.rate, d.labels, values)
    if info is not None:
        info["proc"] = proc
    lat = proc.latency
    tail = proc.tail_frames() if keep_tail and effect.has_tail else 0
    n_sel = b - a
    total = max(1, n_sel + tail)
    w = store.SourceWriter(d.folder, d.channels)
    skip = lat
    done = 0
    try:
        for _pos, x in d.chunks(a, b):
            y = proc.process(x)
            if skip:
                k = min(skip, len(y))
                y = y[k:]
                skip -= k
            w.append(y)
            done += len(x)
            progress(done / total)
        owed = n_sel - w.frames                       # the last `lat` frames are still inside the effect
        while owed > 0:
            k = min(store.CHUNK, owed + skip)
            y = proc.process(np.zeros((k, d.channels), np.float32))
            if skip:
                j = min(skip, len(y))
                y = y[j:]
                skip -= j
            y = y[:owed]
            w.append(y)
            owed -= len(y)
        pos, quiet = b, 0
        limit = int(TAIL_QUIET * d.rate)
        while pos < b + tail:
            k = min(store.CHUNK, b + tail - pos)
            y = proc.process(np.zeros((k, d.channels), np.float32))
            pk = float(np.abs(y).max()) if len(y) else 0.0
            quiet = quiet + k if pk < SILENT else 0
            w.append(y + d.read(pos, k))
            pos += k
            progress((n_sel + pos - b) / total)
            if quiet >= limit:
                break
    except BaseException:
        w.abort()
        raise
    finally:
        close = getattr(proc, "close", None)          # 2.0: a VST3 plugin's host process ends with the render
        if close is not None:
            close()
    return w.finish()


# ── the preview ───────────────────────────────────────────────
class PreviewChain:
    """The effect between the player's reader and its queue. set() / set_bypass() from the window's thread take
    effect at the next block; process() runs in the reader thread."""
    FADE = 512                         # frames of the crossfade when Bypass changes

    def __init__(self, effect: Effect, rate: int, labels, values: dict):
        self.effect = effect
        self.rate, self.labels = rate, list(labels)
        self.proc = effect.processor(rate, labels, values)
        self._lock = threading.Lock()
        self._pending = None
        self.bypass = False
        self._mix = 1.0                   # 1 = the effect, 0 = dry
        self._dry = np.zeros((self.proc.latency, len(self.labels)), np.float64)
        self.error = ""
        self.blocks = 0                   # how many blocks went through (tests)
        self.values = self.proc.p

    def set(self, values: dict) -> None:
        with self._lock:
            self._pending = dict(values)

    def set_bypass(self, on: bool) -> None:
        self.bypass = bool(on)

    @property
    def gr_db(self) -> float:
        return self.proc.gr_db

    def process(self, x: np.ndarray) -> np.ndarray:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            try:
                self.proc.set(pending)
                self.values = self.proc.p
            except Exception as e:                       # noqa: BLE001 — the preview goes on dry
                self.error = str(e)
        lat = self.proc.latency
        if self._dry.shape[0] != lat:                       # a look-ahead changed: the dry delay follows
            self._dry = np.zeros((lat, len(self.labels)), np.float64)
        x64 = np.asarray(x, np.float64)
        try:
            wet = self.proc.process(x).astype(np.float64)
        except Exception as e:                               # noqa: BLE001
            self.error = str(e)
            wet = x64
        if lat:
            buf = np.concatenate((self._dry, x64))
            dry, self._dry = buf[:len(x64)], buf[len(x64):]
        else:
            dry = x64
        target = 0.0 if self.bypass else 1.0
        n = len(x64)
        if self._mix == target or not n:
            g = np.full(n, self._mix)
        else:
            step = (1.0 if target > self._mix else -1.0) / self.FADE
            g = np.clip(self._mix + step * np.arange(1, n + 1), 0.0, 1.0)
            self._mix = float(g[-1])
        self.blocks += 1
        return (dry * (1.0 - g)[:, None] + wet * g[:, None]).astype(np.float32)


# ── helpers for the processors ────────────────────────────────
def db(x):
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-12))


def undb(v):
    return 10.0 ** (np.asarray(v, np.float64) / 20.0)


def mix_gains(mix: float) -> tuple[float, float]:
    """A dry / wet balance 0…1 → (dry gain, wet gain): both full at 50 %, one fading out towards its end."""
    return min(1.0, 2.0 * (1.0 - mix)), min(1.0, 2.0 * mix)


def release_hold(r: np.ndarray, prev: np.ndarray, log_r: float) -> np.ndarray:
    """R'[t] = max(R[t], e^log_r · R'[t−1]) along axis 0 (every column), from R'[−1] = prev — the peak hold with an
    exponential release, computed exactly without a loop: in the log domain it is a running maximum of
    log R[k] − k·log_r, shifted back by t·log_r."""
    n = r.shape[0]
    t = np.arange(n, dtype=np.float64)[:, None]
    with np.errstate(divide="ignore"):
        lr = np.log(np.maximum(r, 0.0))
        base = np.log(np.maximum(prev, 0.0)) + log_r
    acc = np.maximum.accumulate(lr - t * log_r, axis=0)
    acc = np.maximum(acc, base[None, :])
    out = np.exp(acc + t * log_r)
    return out


class Fifo:
    """Frames in, the same frames out `delay` frames later (zeros at first)."""

    def __init__(self, delay: int, ch: int):
        self.buf = np.zeros((max(0, int(delay)), ch), np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if not len(self.buf):
            return x
        cat = np.concatenate((self.buf, x))
        out, self.buf = cat[:len(x)], cat[len(x):]
        return out


@dataclass
class History:
    """The last `size` rows of a stream (for sliding windows across chunks)."""
    size: int
    width: int
    fill: float = 0.0
    rows: np.ndarray = field(default=None)

    def __post_init__(self):
        self.rows = np.full((self.size, self.width), self.fill, np.float64)

    def join(self, x: np.ndarray) -> np.ndarray:
        """History + x; the history becomes the last `size` rows of that."""
        cat = np.concatenate((self.rows, x.reshape(-1, self.width)))
        if self.size:
            self.rows = cat[len(cat) - self.size:].copy()
        return cat
