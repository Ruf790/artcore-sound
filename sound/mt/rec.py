"""Recording onto tracks (3.0). Every armed track records from its own input (a device and its channels; tracks on
the same device share one input stream), into a WAV float file in the project's audio folder, while the project
plays. The audio callbacks only copy; a writer thread puts the blocks into the files.

Where a recording goes on the timeline comes from the clocks, not from guesses: the player knows at which time
(the stream's DAC time) it plays which frame of the project, an input block knows at which time it was captured (its
ADC time); what the performer played along with frame F reached the input when F was heard, so the first captured
frame belongs at F(adc time). That takes in every buffer of the sound card, the driver and Windows, on any card; what
is left (the converters, a cable) is the Settings' offset in milliseconds.

Punch-in / out: only [a, b) of a pass becomes a clip (with short fades at the seams). A pass over the same span as an
earlier recorded clip becomes a new take of it; with the loop on, every whole pass of the loop is a take.

Input monitoring (off by default — a microphone near the speakers howls): the armed / monitored tracks' input goes,
at their volume and pan, straight to the output through player.InputMonitor — not through the mix, which runs
ahead of what is heard and would be late."""
from __future__ import annotations

import collections
import os
import threading
import time

import numpy as np

from .. import record as rec0
from ..i18n import t
from . import media as mmedia
from .engine import pan_gains

QUEUE_SECONDS = 10.0


def device_for(name: str) -> dict | None:
    devs = rec0.input_devices()
    if name:
        for d in devs:
            if d["name"] == name:
                return d
    return next((d for d in devs if d.get("default")), devs[0] if devs else None)


def track_inputs(tr, dev: dict | None) -> list[int]:
    """The input channels a track records: its own choice, else the first ones."""
    n = int(dev["channels"]) if dev else 2
    if tr.input_ch:
        return [c for c in tr.input_ch if 0 <= c < n] or [0]
    want = 1 if tr.channels == 1 else min(2, n)
    return list(range(want))


class _Input:
    """One device's input stream; the callback stamps each block with its capture time."""

    def __init__(self, owner, key: str, dev: dict | None, channels: int, rate: int):
        self.owner, self.key, self.dev = owner, key, dev
        self.channels, self.rate = channels, rate
        self.stream = None
        self.error = ""
        self.null = False
        self.overflows = 0
        self.peaks = np.zeros(channels, np.float32)

    def open(self) -> bool:
        sd = rec0._sd()
        if sd is None or self.dev is None or self.dev.get("null"):
            self.stream = rec0._NullInput(self.rate, self.channels, self._callback)
            self.null = True
        else:
            try:
                extra = None
                try:
                    extra = sd.WasapiSettings(auto_convert=True)
                except Exception:                       # noqa: BLE001
                    extra = None
                self.stream = sd.InputStream(samplerate=self.rate, channels=self.channels, dtype="float32",
                                             device=self.dev["index"], latency="low", blocksize=0,
                                             callback=self._callback, extra_settings=extra)
            except Exception as e:                      # noqa: BLE001
                self.error = str(e) or type(e).__name__
                self.stream = None
                return False
        try:
            self.stream.start()
        except Exception as e:                          # noqa: BLE001
            self.error = str(e) or type(e).__name__
            self.close()
            return False
        return True

    def _callback(self, indata, frames, tinfo, status) -> None:
        if status is not None and getattr(status, "input_overflow", False):
            self.overflows += 1
        x = np.array(indata, np.float32, copy=True).reshape(-1, self.channels)
        if tinfo is not None:
            try:
                adc = float(tinfo.inputBufferAdcTime) or None
            except (AttributeError, TypeError):
                adc = None
        else:
            adc = None
        if adc is None:                                 # the null input (or no time): now minus the block
            adc = self.owner.now() - len(x) / self.rate
        self.owner._push(self, x, adc)

    def close(self) -> None:
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:                           # noqa: BLE001
                pass


class TrackRec:
    def __init__(self, tid: int, name: str, key: str, chans: list[int], channels: int):
        self.tid, self.name, self.key, self.chans, self.channels = tid, name, key, chans, channels
        self.writer = None
        self.path = ""
        self.start = None                 # the timeline frame of the file's first frame
        self.peak = np.zeros(channels, np.float32)


class Recorder:
    """open(tracks) → ready (meters, monitoring); record(clock) → writing; stop() → [(tid, PcmSource, start)]."""

    def __init__(self, project, cache: str, folder: str, rate: int, monitor=None, offset_frames: int = 0):
        self.p, self.cache, self.folder, self.rate = project, cache, folder, int(rate)
        self.monitor = monitor
        self.offset = int(offset_frames)
        self.inputs: dict = {}
        self.tracks: dict = {}            # tid → TrackRec (armed)
        self.mon: dict = {}               # tid → (key, chans, gains) of the monitored tracks
        self.state = "closed"
        self.error = ""
        self.clock = None                 # fn(time) → timeline frame (the player's), while recording
        self.now = time.monotonic
        self._q: collections.deque = collections.deque()
        self._qframes = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._run = False
        self._th = None
        self.dropped = 0
        self.levels_in: dict = {}

    # ── the inputs ────────────────────────────────────────────
    def open(self, armed, monitored=()) -> bool:
        """armed / monitored: tracks. → False (and error) when an input did not open."""
        self.close()
        self.error = ""
        want: dict = {}
        plan = []
        for tr in list(armed) + [x for x in monitored if x not in armed]:
            dev = device_for(tr.input)
            key = dev["name"] if dev else "null"
            chans = track_inputs(tr, dev)
            k = want.setdefault(key, [dev, 0])
            k[1] = max(k[1], max(chans) + 1)
            plan.append((tr, key, chans))
        for key, (dev, n) in want.items():
            n = max(1, min(n, int(dev["channels"]) if dev else n))
            inp = _Input(self, key, dev, n, self.rate)
            if not inp.open():
                self.error = t("mt_rec_input_err", key, inp.error)
                self.close()
                return False
            self.inputs[key] = inp
            if inp.null:
                self.now = time.monotonic
            else:
                st = inp.stream
                self.now = lambda st=st: st.time
        armed_ids = {x.id for x in armed}
        for tr, key, chans in plan:
            if tr.id in armed_ids:
                self.tracks[tr.id] = TrackRec(tr.id, tr.name, key, chans, tr.channels)
            if self.monitor is not None and tr.id in {x.id for x in monitored}:
                self.mon[tr.id] = (key, chans, tr)
        if self.monitor is not None and self.mon:
            self.monitor.open(self.rate, self.p.channels, self.p.labels)
        self._run = True
        self._th = threading.Thread(target=self._write_loop, name="mt-rec", daemon=True)
        self._th.start()
        self.state = "ready"
        return True

    def _push(self, inp: _Input, x: np.ndarray, adc: float) -> None:
        pk = np.abs(x).max(axis=0) if len(x) else np.zeros(inp.channels, np.float32)
        with self._lock:
            inp.peaks = np.maximum(inp.peaks, pk)
            if self.state == "rec":
                if self.clock is not None:            # the first block of each track: where it goes
                    for tr in self.tracks.values():
                        if tr.key == inp.key and tr.start is None:
                            tr.start = int(round(self.clock(adc))) - self.offset
                if self._qframes + len(x) > QUEUE_SECONDS * self.rate:
                    self.dropped += 1
                else:
                    self._q.append((inp.key, x, adc))
                    self._qframes += len(x)
        if self.monitor is not None and self.mon:
            self._monitor(inp, x)
        self._wake.set()

    def _monitor(self, inp: _Input, x: np.ndarray) -> None:
        """The monitored tracks of this input, at their fader and pan, into the project's channels."""
        from ..player import mix_matrix
        out = None
        labels = list(self.p.labels)
        li = labels.index("L") if "L" in labels else 0
        ri = labels.index("R") if "R" in labels else min(1, len(labels) - 1)
        for tid, (key, chans, tr0) in list(self.mon.items()):
            if key != inp.key:
                continue
            tr = self.p.track(tid) or tr0
            if tr.mute:
                continue
            y = x[:, [c for c in chans if c < x.shape[1]]]
            if not y.shape[1]:
                continue
            g = 0.0 if tr.gain_db <= -60 else 10 ** (tr.gain_db / 20)
            o = np.zeros((len(y), len(labels)), np.float32)
            if y.shape[1] == 1:
                gl, gr = pan_gains(1, tr.pan)
                o[:, li] += y[:, 0] * gl * g
                o[:, ri] += y[:, 0] * gr * g
            elif y.shape[1] == 2:
                gl, gr = pan_gains(2, tr.pan)
                o[:, li] += y[:, 0] * gl * g
                o[:, ri] += y[:, 1] * gr * g
            else:
                o += (y @ mix_matrix([str(i) for i in range(y.shape[1])], len(labels))) * g
            out = o if out is None else out + o
        if out is not None:
            self.monitor.push(out)

    def levels(self) -> dict:
        """The input peak of every armed / monitored track since the last call."""
        with self._lock:
            per = {k: inp.peaks for k, inp in self.inputs.items()}
            for inp in self.inputs.values():
                inp.peaks = np.zeros(inp.channels, np.float32)
        out = {}
        for tid, tr in self.tracks.items():
            pk = per.get(tr.key)
            if pk is not None:
                out[tid] = pk[[c for c in tr.chans if c < len(pk)]]
        for tid, (key, chans, _tr) in self.mon.items():
            if tid not in out and key in per:
                out[tid] = per[key][[c for c in chans if c < len(per[key])]]
        return out

    # ── recording ─────────────────────────────────────────────
    def record(self, clock, names: dict | None = None) -> bool:
        """Start writing; clock(time) → the project frame being heard at that time."""
        if self.state != "ready" or not self.tracks:
            return False
        os.makedirs(self.folder, exist_ok=True)
        for tid, tr in self.tracks.items():
            stem = (names or {}).get(tid) or tr.name
            tr.path = mmedia.unique_path(self.folder, stem)
            base = os.path.join(self.cache, "r" + os.urandom(10).hex() + ".pk")
            tr.writer = mmedia.WavWriter(tr.path, tr.channels, self.rate, base)
            tr.start = None
        self.clock = clock
        with self._lock:
            self._q.clear()
            self._qframes = 0
            self.dropped = 0
            for inp in self.inputs.values():
                inp.overflows = 0
            self.state = "rec"
        return True

    def recorded(self) -> dict:
        """tid → (start, frames so far) — the timeline shows the recording growing."""
        return {tid: (tr.start, tr.writer.frames) for tid, tr in self.tracks.items()
                if tr.writer is not None and tr.start is not None}

    def _write_loop(self) -> None:
        while self._run:
            self._wake.wait(0.05)
            self._wake.clear()
            self._drain()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._q:
                    return
                key, x, adc = self._q.popleft()
                self._qframes -= len(x)
            for tr in self.tracks.values():
                if tr.key != key or tr.writer is None:
                    continue
                y = x[:, [c for c in tr.chans if c < x.shape[1]]]
                if y.shape[1] != tr.channels:
                    y = np.repeat(y[:, :1], tr.channels, axis=1) if y.shape[1] < tr.channels else \
                        y.mean(axis=1, keepdims=True)
                tr.writer.append(y)

    def stop(self) -> list:
        """→ [(tid, PcmSource, timeline start)] of the tracks that recorded anything."""
        if self.state != "rec":
            return []
        with self._lock:
            self.state = "ready"
        self._drain()
        out = []
        for tid, tr in self.tracks.items():
            w, tr.writer = tr.writer, None
            if w is None:
                continue
            if not w.frames or tr.start is None:
                w.abort()
                continue
            src = w.finish()
            mmedia.remember_peaks(src, self.cache)
            out.append((tid, src, tr.start))
        self.clock = None
        return out

    def overflows(self) -> int:
        return self.dropped + sum(i.overflows for i in self.inputs.values())

    def close(self) -> None:
        with self._lock:
            self.state = "closed"
        for tr in self.tracks.values():
            if tr.writer is not None:
                tr.writer.abort()
                tr.writer = None
        self._run = False
        self._wake.set()
        for inp in self.inputs.values():
            inp.close()
        self.inputs = {}
        th, self._th = self._th, None
        if th is not None and th is not threading.current_thread():
            th.join(1.0)
        if self.monitor is not None:
            self.monitor.close()
        self.tracks = {}
        self.mon = {}


def place(project, results, punch=None, loop: bool = False, label: str = "record") -> list:
    """The recordings as clips — one undo step. results: [(tid, media id, start, frames)]; punch (a, b): only that
    part of each pass; loop: the recording went round [a, b) — each whole pass is a take. → the new clip ids."""
    from dataclasses import replace
    from .model import AUTO_XFADE, Clip, Take, new_id
    changes, adds, ids = {}, {}, []
    xf = int(AUTO_XFADE * project.rate)
    for tid, mid, start, n in results:
        tr = project.track(tid)
        if tr is None:
            continue
        if punch is not None:
            a, b = punch
            span = b - a
            passes = []
            if loop and span > 0:
                first = a - start                     # where in the file the first pass's a is
                k = 0
                while first + k * span < 0:
                    k += 1
                while first + (k + 1) * span <= n:
                    passes.append(first + k * span)
                    k += 1
                if not passes and first < n:
                    passes.append(max(0, first))
            else:
                off = max(0, a - start)
                if off < n:
                    passes.append(off)
            if not passes:
                continue
            length = min(span, n - passes[-1]) if not loop else span
            takes = tuple(Take(mid, o, t("mt_take_name", i + 1)) for i, o in enumerate(passes))
            same = next((c for c in tr.clips if c.start == a and c.length == length), None)
            if same is not None:
                nc = replace(same, takes=same.takes + takes, take=len(same.takes) + len(takes) - 1)
                changes[same.id] = nc
                ids.append(same.id)
                continue
            c = Clip(new_id(), a, max(1, length), takes, len(takes) - 1, fade_in=min(xf, length // 4),
                     fade_out=min(xf, length // 4))
        else:
            s = max(0, start)
            off = s - start
            if off >= n:
                continue
            c = Clip(new_id(), s, n - off, (Take(mid, off, t("mt_take_name", 1)),))
            c = project._overlap_fades(tr.clips, c)
        adds.setdefault(tid, []).append(c)
        ids.append(c.id)
    if changes or adds:
        project._replace_clips(label, changes, adds, sel=ids)
    return ids
