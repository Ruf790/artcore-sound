"""The mixer of a project (3.0). One engine both plays (the player reads it as it reads a document: rate, channels,
labels, frames, read(pos, n)) and renders (mixdown, stems) — so what is heard is what is written, frame for frame.

Each block: every audio track mixes its clips (the later on top, its fades showing what is under it), runs its effect
chain, its polarity, fader, volume envelope and mute / solo, then is panned into where it goes — the master or a bus —
and into the buses it sends to. Buses (in the order their routing needs) do the same with what reaches them; the
master's chain and fader come last. Work is in float64; the result float32.

Plug-in delay compensation: every chain reports its latency; whatever reaches a bus or the master is delayed so that
everything arriving there lines up with the latest path, and the project's whole latency is made up for by running
the graph that many frames ahead (the first frames out after a start are thrown away). Effects and their latency
change while playing; the alignment follows (a short glitch at that moment).

Live: the reader asks for 1024 frames at a time, about 0.1 s ahead, so a fader or a mute is heard within that; a
change ramps over the block (no clicks). The disk is read by a prefetch thread a few seconds ahead of the play
position into a small cache (Prefetch), so the mixing never waits for the disk. Tracks with effects run in parallel
threads (numpy and the plug-in host's pipe let the GIL go). A new effect (a VST3 plug-in loads for a few seconds) is
built in a thread of its own; until then the track plays without it."""
from __future__ import annotations

import collections
import concurrent.futures
import os
import threading
import time

import numpy as np

from .. import fx as fxmod
from ..fx.base import Fifo
from ..player import mix_matrix
from ..proc import curve_gain
from .model import ENV_MIN_DB, GAIN_MIN_DB, MASTER, Track, env_at

LIVE_BLOCK = 1024
RENDER_BLOCK = 16384
PF_CHUNK = 1 << 15              # frames per prefetch chunk
PF_AHEAD = 3.0                  # seconds the prefetch keeps read ahead of the play position
PF_BUDGET = 48 << 20            # bytes of prefetched sound at most
METER_KEEP = 2.0                # seconds of meter values kept (looked up by the heard position)


def track_effects() -> list[str]:
    """The effects a track's chain can hold: the real-time ones (tempo / pitch changes the length — not here)."""
    return [k for k, e in fxmod.EFFECTS.items() if e.realtime and k not in ("tempo",)]


def track_labels(t: Track, project_labels) -> tuple:
    if t.channels == 1:
        return ("M",)
    if t.channels == 2:
        return ("L", "R")
    if len(project_labels) == t.channels:
        return tuple(project_labels)
    from ..ff import labels
    return tuple(labels(t.channels, ""))


def adapt(x: np.ndarray, src_labels, ch: int, dst_labels) -> np.ndarray:
    """A medium's frames → a track's channels: a mono file on a stereo track plays on both sides, a stereo one on
    a mono track is mixed (½ + ½), 5.1 on stereo is the player's standard downmix."""
    n = x.shape[1]
    if n == ch:
        return x
    if n == 1:
        if ch == 2:
            return np.repeat(x, 2, axis=1)
        out = np.zeros((len(x), ch), x.dtype)
        i = list(dst_labels).index("C") if "C" in dst_labels else 0
        out[:, i] = x[:, 0]
        return out
    if ch == 1:
        return x.mean(axis=1, keepdims=True)
    if n == 2 and ch > 2:
        out = np.zeros((len(x), ch), x.dtype)
        out[:, :2] = x
        return out
    return x @ mix_matrix(src_labels or [str(i) for i in range(n)], ch).astype(x.dtype)


def lr_index(labels) -> tuple[int, int]:
    labels = list(labels)
    li = labels.index("L") if "L" in labels else 0
    ri = labels.index("R") if "R" in labels else min(1, len(labels) - 1)
    return li, ri


def pan_gains(ch: int, pan):
    """(left, right) gains of a mono track (constant power, −3 dB in the middle) or a stereo one (balance)."""
    pan = np.clip(pan, -1.0, 1.0)
    if ch == 1:
        th = (pan + 1.0) * (np.pi / 4.0)
        return np.cos(th), np.sin(th)
    return np.minimum(1.0, 1.0 - pan), np.minimum(1.0, 1.0 + pan)


# ── the prefetch ──────────────────────────────────────────────
class Prefetch:
    """Chunks of the media read ahead of the play position (a thread), LRU within PF_BUDGET bytes."""

    def __init__(self):
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._want: list = []                # (src, first chunk, last chunk) to read
        self._wake = threading.Event()
        self._run = True
        self.misses = 0
        self.hits = 0
        self._th = threading.Thread(target=self._loop, name="mt-prefetch", daemon=True)
        self._th.start()

    def close(self) -> None:
        self._run = False
        self._wake.set()
        with self._lock:
            self._cache.clear()
            self._bytes = 0

    def _get(self, src, k: int) -> np.ndarray | None:
        with self._lock:
            arr = self._cache.get((id(src), k))
            if arr is not None:
                self._cache.move_to_end((id(src), k))
            return arr

    def _put(self, src, k: int, arr: np.ndarray) -> None:
        with self._lock:
            key = (id(src), k)
            if key in self._cache:
                return
            self._cache[key] = arr
            self._bytes += arr.nbytes
            while self._bytes > PF_BUDGET and len(self._cache) > 1:
                _k, old = self._cache.popitem(last=False)
                self._bytes -= old.nbytes

    def _chunk(self, src, k: int) -> np.ndarray:
        arr = self._get(src, k)
        if arr is None:
            a = k * PF_CHUNK
            arr = src.read(a, min(PF_CHUNK, max(0, src.frames - a)))
            self._put(src, k, arr)
            return arr
        return arr

    def read(self, src, off: int, n: int) -> np.ndarray:
        out = np.zeros((n, src.channels), np.float32)
        a, b = max(0, off), min(src.frames, off + n)
        if b <= a:
            return out
        for k in range(a // PF_CHUNK, (b - 1) // PF_CHUNK + 1):
            arr = self._get(src, k)
            if arr is None:
                self.misses += 1
                arr = self._chunk(src, k)
            else:
                self.hits += 1
            c0 = k * PF_CHUNK
            s, e = max(a, c0), min(b, c0 + len(arr))
            if e > s:
                out[s - off:e - off] = arr[s - c0:e - c0]
        return out

    def ahead(self, wants: list) -> None:
        """wants: (src, from frame, to frame) to have ready."""
        with self._lock:
            self._want = [(s, max(0, a) // PF_CHUNK, max(0, min(s.frames, b) - 1) // PF_CHUNK)
                          for s, a, b in wants if b > a and s is not None and s.frames]
        self._wake.set()

    def _loop(self) -> None:
        while self._run:
            self._wake.wait(0.2)
            self._wake.clear()
            with self._lock:
                todo = list(self._want)
            for s, k0, k1 in todo:            # nearest first (the list is in play order)
                for k in range(k0, k1 + 1):
                    if not self._run:
                        return
                    if self._get(s, k) is None:
                        try:
                            self._chunk(s, k)
                        except Exception:                  # noqa: BLE001 — a file gone: read where needed
                            break


# ── a track's effects at run time ─────────────────────────────
class Chain:
    """The processors of a chain's slots that are not bypassed; new ones built in a thread (live) or at once."""

    def __init__(self, engine: "Engine", labels):
        self.engine = engine
        self.labels = list(labels)
        self.procs: dict = {}               # slot id → [key, values, processor]
        self.building: set = set()
        self.errors: dict = {}              # slot id → message
        self.order: tuple = ()
        self.host = None                    # the VST3 host all plug-ins of this chain share (vst/client.SharedHost)

    @property
    def latency(self) -> int:
        return sum(int(getattr(self.procs[i][2], "latency", 0) or 0) for i in self.order if i in self.procs)

    def sync(self, slots, live: bool) -> None:
        want = [s for s in slots if not s.bypass and s.id not in self.errors]
        ids = {s.id for s in slots}
        for sid in [k for k in list(self.procs) if k not in ids]:
            self._drop(sid)
        for s in want:
            cur = self.procs.get(s.id)
            if cur is not None and cur[0] != s.key:
                self._drop(s.id)
                cur = None
            if cur is None:
                if s.id in self.building:
                    continue
                if live:
                    self.building.add(s.id)
                    threading.Thread(target=self._build, args=(s,), daemon=True).start()
                else:
                    self._build(s)
            elif cur[1] is not s.values and cur[1] != s.values:
                try:
                    cur[2].set(s.values)
                    cur[1] = s.values
                except Exception as e:                    # noqa: BLE001
                    self.errors[s.id] = str(e) or type(e).__name__
                    self._drop(s.id)
        self.order = tuple(s.id for s in want if s.id in self.procs)

    def _build(self, s) -> None:
        try:
            eff = fxmod.get(s.key)
            if s.key == "vst3":
                if self.host is None:
                    from ..vst.client import SharedHost
                    self.host = SharedHost()
                proc = eff.processor(self.engine.rate, self.labels, s.values, host=self.host)
            else:
                proc = eff.processor(self.engine.rate, self.labels, s.values)
            self.procs[s.id] = [s.key, s.values, proc]
        except Exception as e:                            # noqa: BLE001 — the chain goes on without it
            self.errors[s.id] = str(e) or type(e).__name__
        finally:
            self.building.discard(s.id)
            self.engine.changed = True

    def _drop(self, sid) -> None:
        cur = self.procs.pop(sid, None)
        if cur is not None:
            close = getattr(cur[2], "close", None)
            if close is not None:
                try:
                    close()
                except Exception:                         # noqa: BLE001
                    pass

    def reset(self) -> None:
        for sid in self.order:
            p = self.procs.get(sid)
            if p is not None:
                try:
                    p[2].reset()
                except Exception:                         # noqa: BLE001
                    pass

    def process(self, x: np.ndarray) -> np.ndarray:
        for sid in self.order:
            p = self.procs.get(sid)
            if p is None:
                continue
            try:
                x = p[2].process(x).astype(np.float64)
            except Exception as e:                        # noqa: BLE001 — a crashed plug-in: the track goes dry
                self.errors[sid] = str(e) or type(e).__name__
                self.engine.changed = True
        return x

    def close(self) -> None:
        for sid in list(self.procs):
            self._drop(sid)
        if self.host is not None:
            self.host.close()
            self.host = None


class Node:
    """The run-time state of a track, a bus or the master."""

    def __init__(self, engine, t: Track, labels):
        self.id = t.id
        self.labels = tuple(labels)
        self.channels = len(labels)
        self.chain = Chain(engine, labels)
        self.gain = None                    # the fader's gain at the end of the last block (the ramp)
        self.pan = None
        self.sends: dict = {}               # bus id → last send gain
        self.fifos: dict = {}               # (to, kind) → Fifo of the delay compensation
        self.peak = np.zeros(self.channels, np.float32)


class Engine:
    """The mix of a project. For the player: rate, channels, labels, frames, live, read(pos, n)."""

    def __init__(self, project, live: bool = True):
        self.project = project
        self.live = live
        self.rate = project.rate
        self.channels = project.channels
        self.labels = project.labels
        self.nodes: dict = {}
        self.next = None                    # the frame read() continues from without a reset
        self.cursor = 0                     # where the graph reads its inputs (ahead by the latency)
        self.skip = 0
        self.latency = 0
        self.changed = True                 # recompute the routing / the compensation at the next block
        self._plan = None
        self._lock = threading.RLock()
        self.meters: collections.deque = collections.deque()
        self.prefetch = Prefetch() if live else None
        self._pool = None
        self.pool_size = max(1, min(8, (os.cpu_count() or 2) - 1))
        self.blocks = 0
        self.late = 0                      # blocks that took longer than their own length (live)
        self.extra_input = None            # rec.py: (track id → frames) to monitor live into a track
        self.state = None
        self.extend = 0                    # while recording: the project plays on past its end (silence)

    # ── the player's view ─────────────────────────────────────
    @property
    def frames(self) -> int:
        return max(self.project.frames, self.extend)

    @property
    def errors(self) -> dict:
        """slot id → message of the effects that failed (read from the window's thread while the reader mixes)."""
        out = {}
        for n in list(self.nodes.values()):
            out.update(dict(n.chain.errors))
        return out

    def close(self) -> None:
        for n in self.nodes.values():
            n.chain.close()
        self.nodes = {}
        if self.prefetch is not None:
            self.prefetch.close()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def read(self, pos: int, n: int) -> np.ndarray:
        with self._lock:
            if pos != self.next:
                self.seek(pos)
            t0 = time.perf_counter()
            y = self._out(n)
            self.next = pos + n
            if self.live:
                self.blocks += 1
                if time.perf_counter() - t0 > n / self.rate:
                    self.late += 1
            return y.astype(np.float32)

    def seek(self, pos: int) -> None:
        """Start again at `pos`: the effects forget their sound, the graph runs the latency ahead."""
        with self._lock:
            st = self._sync()
            for nd in self.nodes.values():
                nd.chain.reset()
                for f in nd.fifos.values():
                    f.buf[:] = 0
                nd.gain = None
                nd.pan = None
                nd.sends = {}
            self._plan_routes(st)
            self.cursor = int(pos)
            self.skip = self.latency
            self.next = int(pos)
            self.meters.clear()

    def _out(self, n: int) -> np.ndarray:
        parts = []
        got = 0
        while got < n:
            want = n - got + self.skip
            k = min(want, LIVE_BLOCK if self.live else RENDER_BLOCK)
            y = self._block(self.cursor, k)
            self.cursor += k
            if self.skip:
                d = min(self.skip, len(y))
                y = y[d:]
                self.skip -= d
            if len(y):
                parts.append(y)
                got += len(y)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    # ── the graph ─────────────────────────────────────────────
    def _sync(self):
        """The nodes for the project's current state (made / dropped), their chains up to date."""
        p = self.project
        st = (p.tracks, p.master)
        if st is self.state and not self.changed:
            return st
        tracks, master = st
        live_ids = {t.id for t in tracks} | {MASTER}
        for tid in [k for k in self.nodes if k not in live_ids]:
            self.nodes.pop(tid).chain.close()
        for t in tracks + (master,):
            labels = self.labels if t.kind in ("bus", "master") else track_labels(t, self.labels)
            nd = self.nodes.get(t.id)
            if nd is None or nd.labels != tuple(labels):
                if nd is not None:
                    nd.chain.close()
                nd = self.nodes[t.id] = Node(self, t, labels)
            nd.chain.sync(t.fx, self.live)
        self.state = st
        self._plan_routes(st)
        self.changed = False
        return st

    def _plan_routes(self, st) -> None:
        """The order of the buses and the delays of the compensation."""
        tracks, master = st
        by = {t.id: t for t in tracks}
        by[MASTER] = master
        feeds: dict = collections.defaultdict(list)          # dest → [(src id, kind)]
        for t in tracks:
            out = t.output if t.output in by and (t.output == MASTER or by[t.output].kind == "bus") else MASTER
            feeds[out].append((t.id, "out"))
            for s in t.sends:
                if s.bus in by and by[s.bus].kind == "bus" and s.bus != t.id:
                    feeds[s.bus].append((t.id, "send"))
        lat = {tid: self.nodes[tid].chain.latency for tid in by if tid in self.nodes}
        arrive: dict = {}
        out_lat: dict = {}
        order: list = []
        visiting: set = set()

        def resolve(tid):
            if tid in out_lat:
                return out_lat[tid]
            if tid in visiting:                       # a loop the model should not allow: cut it
                return 0
            visiting.add(tid)
            t = by[tid]
            if t.kind == "audio":
                out_lat[tid] = lat.get(tid, 0)
            else:
                a = max((resolve(s) for s, _k in feeds.get(tid, [])), default=0)
                arrive[tid] = a
                out_lat[tid] = a + lat.get(tid, 0)
                if t.kind == "bus":
                    order.append(tid)
            visiting.discard(tid)
            return out_lat[tid]

        for t in tracks:
            resolve(t.id)
        resolve(MASTER)
        for dest, srcs in feeds.items():
            a = arrive.get(dest, 0)
            for sid, kind in srcs:
                nd = self.nodes.get(sid)
                if nd is None:
                    continue
                d = max(0, a - out_lat.get(sid, 0))
                f = nd.fifos.get((dest, kind))
                if f is None or len(f.buf) != d or f.buf.shape[1] != len(self.nodes[dest].labels):
                    nd.fifos[(dest, kind)] = Fifo(d, len(self.nodes[dest].labels))
        self.latency = out_lat.get(MASTER, 0)
        self._plan = (by, feeds, order)

    def _solo(self, tracks) -> set | None:
        """The ids that sound when something is soloed (None: nothing is)."""
        soloed = [t for t in tracks if t.solo]
        if not soloed:
            return None
        by = {t.id: t for t in tracks}
        on = set()
        for t in soloed:
            on.add(t.id)
            cur = t                                    # what it goes through must sound too
            seen = set()
            while cur.output != MASTER and cur.output in by and cur.output not in seen:
                seen.add(cur.output)
                on.add(cur.output)
                cur = by[cur.output]
            for s in t.sends:
                on.add(s.bus)
        for t in tracks:                               # a soloed bus: what goes into it sounds
            if t.kind == "audio" and (t.output in on and by.get(t.output) is not None and by[t.output].solo):
                on.add(t.id)
        return on

    def _block(self, c: int, n: int) -> np.ndarray:
        st = self._sync()
        tracks, master = st
        by, feeds, order = self._plan
        solo = self._solo(tracks)
        frames = np.arange(c, c + n, dtype=np.float64)
        ins: dict = {}                                    # dest id → summed input
        audio = [t for t in tracks if t.kind == "audio"]
        if not self.live:                                 # a render: a silent track need not be mixed at all
            audio = [t for t in audio if not t.mute and (solo is None or t.id in solo)]
        results = {}
        heavy = [t for t in audio if self.nodes[t.id].chain.order]
        if self.live and len(heavy) >= 2 and self.pool_size > 1:
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(self.pool_size, thread_name_prefix="mt-mix")
            futs = {t.id: self._pool.submit(self._track, t, c, n) for t in heavy}
            for t in audio:
                if t.id not in futs:
                    results[t.id] = self._track(t, c, n)
            for tid, f in futs.items():
                results[tid] = f.result()
        else:
            for t in audio:
                results[t.id] = self._track(t, c, n)
        for t in audio:
            self._route(t, results[t.id], c, n, frames, solo, ins)
        for bid in order:
            b = by[bid]
            nd = self.nodes[bid]
            x = ins.pop(bid, None)
            if x is None:
                x = np.zeros((n, nd.channels), np.float64)
            x = nd.chain.process(x)
            self._route(b, x, c, n, frames, solo, ins)
        nd = self.nodes[MASTER]
        x = ins.pop(MASTER, None)
        if x is None:
            x = np.zeros((n, nd.channels), np.float64)
        x = nd.chain.process(x)
        g = self._fader(nd, master, frames, True)
        x = x * (g[:, None] if isinstance(g, np.ndarray) else g)
        nd.peak = np.abs(x).max(axis=0).astype(np.float32) if n else nd.peak
        self._note_meters(c, n)
        if self.prefetch is not None:
            self._prefetch(tracks, c + n)
        return x

    # ── one track ─────────────────────────────────────────────
    def _track(self, t: Track, c: int, n: int) -> np.ndarray:
        """Its clips mixed and its chain run (before the fader)."""
        nd = self.nodes[t.id]
        x = self.clips_input(t, c, n, nd.labels)
        if self.extra_input is not None:
            extra = self.extra_input(t.id, c, n)
            if extra is not None:
                x = x + adapt(np.asarray(extra, np.float64), None, nd.channels, nd.labels)
        return nd.chain.process(x)

    def clips_input(self, t: Track, c: int, n: int, labels) -> np.ndarray:
        ch = len(labels)
        acc = np.zeros((n, ch), np.float64)
        media = self.project.media
        for cl in t.clips:
            if cl.muted:
                continue
            s, e = max(c, cl.start), min(c + n, cl.end)
            if e <= s:
                continue
            m = media.get(cl.media)
            src = m.src if m is not None else None
            k = e - s
            if src is not None:
                off = cl.offset + (s - cl.start)
                raw = self.prefetch.read(src, off, k) if self.prefetch is not None else src.read(off, k)
                x = adapt(raw.astype(np.float64), m.labels, ch, labels)
            else:
                x = np.zeros((k, ch), np.float64)
            g = 0.0 if cl.gain_db <= GAIN_MIN_DB else 10.0 ** (cl.gain_db / 20.0)
            rel = np.arange(s - cl.start, e - cl.start, dtype=np.float64)
            alpha = None
            if cl.fade_in and rel[0] < cl.fade_in:
                alpha = np.where(rel < cl.fade_in, curve_gain(cl.curve_in, rel / cl.fade_in), 1.0)
            if cl.fade_out and rel[-1] >= cl.length - cl.fade_out:
                a2 = np.where(rel >= cl.length - cl.fade_out,
                              curve_gain(cl.curve_out, (cl.length - rel) / cl.fade_out), 1.0)
                alpha = a2 if alpha is None else alpha * a2
            seg = acc[s - c:e - c]
            if alpha is None:
                seg[...] = x * g if g != 1.0 else x
            else:
                seg[...] = seg * (1.0 - alpha)[:, None] + x * (alpha * g)[:, None]
        return acc

    def _fader(self, nd: Node, t: Track, frames: np.ndarray, on: bool):
        """The fader × the volume envelope × mute, ramped from the last block: a scalar or an array."""
        target = 0.0 if (not on or t.mute) else (0.0 if t.gain_db <= GAIN_MIN_DB else 10.0 ** (t.gain_db / 20.0))
        prev = target if nd.gain is None else nd.gain
        nd.gain = target
        n = len(frames)
        g = target if prev == target else prev + (target - prev) * (np.arange(1, n + 1) / n)
        if t.vol_env and t.env_on:
            db = env_at(t.vol_env, frames, 0.0)
            ev = np.where(db <= ENV_MIN_DB, 0.0, 10.0 ** (db / 20.0))
            g = ev * g
        return g

    def _route(self, t: Track, x: np.ndarray, c: int, n: int, frames, solo, ins: dict) -> None:
        nd = self.nodes[t.id]
        on = solo is None or t.id in solo
        if t.phase:
            x = -x
        pre = x
        g = self._fader(nd, t, frames, on)
        post = x * (g[:, None] if isinstance(g, np.ndarray) else g)
        nd.peak = np.abs(post).max(axis=0).astype(np.float32) if n else nd.peak
        by = self._plan[0]
        out = t.output if t.output in by else MASTER
        if out != MASTER and by[out].kind != "bus":
            out = MASTER
        pan = env_at(t.pan_env, frames, t.pan) if (t.pan_env and t.env_on) else t.pan
        if not isinstance(pan, np.ndarray):
            p0 = t.pan if nd.pan is None else nd.pan
            nd.pan = t.pan
            if p0 != t.pan:
                pan = p0 + (t.pan - p0) * (np.arange(1, n + 1) / n)
        self._add(nd, out, "out", self._pan(nd, post, out, pan), ins)
        for s in t.sends:
            if s.bus not in by or by[s.bus].kind != "bus" or s.bus == t.id:
                continue
            sg = 0.0 if s.db <= GAIN_MIN_DB else 10.0 ** (s.db / 20.0)
            if s.pre and (t.mute or not on):
                sg = 0.0
            prev = nd.sends.get(s.bus, sg)
            nd.sends[s.bus] = sg
            src = pre if s.pre else post
            gg = sg if prev == sg else prev + (sg - prev) * (np.arange(1, n + 1) / n)
            y = src * (gg[:, None] if isinstance(gg, np.ndarray) else gg)
            self._add(nd, s.bus, "send", self._pan(nd, y, s.bus, pan if not s.pre else t.pan), ins)

    def _pan(self, nd: Node, x: np.ndarray, dest: int, pan) -> np.ndarray:
        dl = self.nodes[dest].labels
        dch = len(dl)
        ch = nd.channels
        if ch == dch and ch > 2:                       # 5.1 into 5.1: channel to channel, no pan
            return x
        if ch > 2:
            x = x @ mix_matrix(nd.labels, 2).astype(np.float64)
            ch = 2
        gl, gr = pan_gains(ch, pan)
        li, ri = lr_index(dl)
        y = np.zeros((len(x), dch), np.float64)
        y[:, li] = x[:, 0] * gl
        y[:, ri] = x[:, 0 if ch == 1 else 1] * gr
        return y

    def _add(self, nd: Node, dest: int, kind: str, y: np.ndarray, ins: dict) -> None:
        f = nd.fifos.get((dest, kind))
        if f is not None:
            y = f(y)
        cur = ins.get(dest)
        ins[dest] = y if cur is None else cur + y

    # ── meters ────────────────────────────────────────────────
    def _note_meters(self, c: int, n: int) -> None:
        pos = c - self.latency                         # the timeline frame these output frames are
        self.meters.append((pos, n, {k: nd.peak for k, nd in self.nodes.items()}))
        keep = int(METER_KEEP * self.rate)
        while self.meters and self.meters[0][0] + self.meters[0][1] < pos - keep:
            self.meters.popleft()

    def meters_at(self, pos: int) -> dict:
        """The peaks of every node in the block that holds the heard frame `pos` (live)."""
        best = None
        try:
            items = list(self.meters)
        except RuntimeError:                          # appended to by the reader right now
            return {}
        for p, n, pk in items:
            if p <= pos < p + n:
                return pk
            if p <= pos:
                best = pk
        return best or {}

    def _prefetch(self, tracks, c: int) -> None:
        if self.blocks % 8:
            return
        a, b = c, c + int(PF_AHEAD * self.rate)
        wants = []
        media = self.project.media
        for t in tracks:
            for cl in t.clips:
                if cl.start < b and cl.end > a and not cl.muted:
                    m = media.get(cl.media)
                    if m is not None and m.src is not None:
                        s = cl.offset + max(0, a - cl.start)
                        e = cl.offset + min(cl.length, b - cl.start)
                        wants.append((m.src, s, e))
        wants.sort(key=lambda w: w[1])
        self.prefetch.ahead(wants)


# ── rendering ─────────────────────────────────────────────────
def render(project, a: int, b: int, sink, progress=None, solo_tracks=None, tail: bool = False) -> int:
    """The mix of [a, b) into sink(frames) in blocks (an offline engine of its own). solo_tracks: only these track
    ids sound (a stem) — as if they were soloed. tail: go on past b while the effects still ring (up to 30 s).
    → the frames written."""
    eng = Engine(_SoloView(project, solo_tracks) if solo_tracks is not None else project, live=False)
    try:
        eng.seek(a)
        done = 0
        total = max(1, b - a)
        pos = a
        while pos < b:
            k = min(RENDER_BLOCK * 4, b - pos)
            sink(eng.read(pos, k))
            pos += k
            done += k
            if progress is not None:
                progress(done / total)
        if tail:
            quiet = 0
            limit = int(0.25 * project.rate)
            end = b + int(30 * project.rate)
            while pos < end and quiet < limit:
                k = min(RENDER_BLOCK, end - pos)
                y = eng.read(pos, k)
                pk = float(np.abs(y).max()) if len(y) else 0.0
                quiet = quiet + k if pk < 1e-5 else 0
                sink(y)
                pos += k
                done += k
        return done
    finally:
        eng.close()


class _SoloView:
    """A project seen with only some tracks sounding (stems): the others muted, solo flags cleared."""

    def __init__(self, project, ids):
        from dataclasses import replace
        self._p = project
        ids = set(ids)
        self.tracks = tuple(replace(t, solo=False, mute=t.mute or (t.kind == "audio" and t.id not in ids))
                            for t in project.tracks)
        self.master = project.master

    def __getattr__(self, k):
        return getattr(self._p, k)
