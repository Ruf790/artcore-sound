"""Recording from a microphone or a line input (0.4): a WASAPI input through PortAudio (sounddevice). The audio
callback only copies each block into a queue and notes its peak; a writer thread takes the blocks from there into a
SourceWriter on disk, so a recording of hours needs no more memory than a few seconds of queue. A queue that grows
past QUEUE_SECONDS (the disk cannot keep up) drops blocks; those and PortAudio's own input overflows are counted.

Before recording the input is monitored («ready»: the meters move, nothing is written). Options: a pre-roll — the
last N seconds before Record are kept and go first into the recording; a level trigger — after Record the writing
starts with the first block over the threshold (with the pre-roll before it); markers set while recording
(positions from the recording's start).

The input can also be heard (2.0, off by default — a microphone near the speakers howls): each block goes to a
player.InputMonitor too, which plays it at the listening volume through the playback device.

Without a sound card (CI, ARTCORE_AUDIO=null) a null input plays a tone (440 Hz at −12 dBFS, 660 Hz on the second
channel) at the pace of the clock, through the same callback."""
from __future__ import annotations

import collections
import math
import os
import threading
import time

import numpy as np

from . import store

QUEUE_SECONDS = 10.0
NULL_NAME = "null"


def _sd():
    if os.environ.get("ARTCORE_AUDIO") == "null":
        return None
    try:
        import sounddevice as sd
        return sd
    except Exception:
        return None


def input_devices() -> list[dict]:
    """[{"index", "name", "channels", "rate", "default", "loopback"}] of the input host API (WASAPI on Windows);
    without PortAudio the null input."""
    sd = _sd()
    if sd is None:
        return [{"index": None, "name": NULL_NAME, "channels": 2, "rate": 48000, "default": True,
                 "loopback": False, "null": True}]
    from .player import hostapi_index
    try:
        api = hostapi_index(sd)
        hosts = sd.query_hostapis()
        default = hosts[api]["default_input_device"] if api >= 0 else -1
        out = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and (api < 0 or dev["hostapi"] == api):
                name = dev["name"]
                out.append({"index": i, "name": name, "channels": int(dev["max_input_channels"]),
                            "rate": int(dev["default_samplerate"]), "default": i == default,
                            "loopback": "loopback" in name.lower(), "null": False})
        return out
    except Exception:
        return []


def probe_input() -> str:
    """For the self-test: open and close the default input for a moment → a line saying what happened."""
    sd = _sd()
    if sd is None:
        return "null input"
    devs = input_devices()
    if not devs:
        return "no input devices"
    dev = next((d for d in devs if d["default"]), devs[0])
    loop = sum(1 for d in devs if d["loopback"])
    try:
        extra = None
        try:
            extra = sd.WasapiSettings(auto_convert=True)
        except Exception:
            extra = None
        st = sd.InputStream(samplerate=dev["rate"], channels=min(2, dev["channels"]), dtype="float32",
                            device=dev["index"], extra_settings=extra)
        st.start()
        time.sleep(0.2)
        st.stop()
        st.close()
        return f"input «{dev['name']}» opened ({len(devs)} inputs, {loop} loopback)"
    except Exception as e:                    # noqa: BLE001
        return f"input «{dev['name']}» did not open: {e} ({len(devs)} inputs, {loop} loopback)"


class _NullInput:
    """A tone at the pace of the clock, into the same callback as a real input."""

    def __init__(self, rate: int, channels: int, callback, block: int = 1024):
        self.rate, self.channels, self.cb, self.block = rate, channels, callback, block
        self._run = False
        self._th = None
        self._n = 0

    def start(self):
        self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        t_next = time.monotonic()
        freqs = [440.0 * (1.5 ** (c % 2)) for c in range(self.channels)]
        while self._run:
            t = (self._n + np.arange(self.block)) / self.rate
            x = np.stack([0.25 * np.sin(2 * math.pi * f * t) for f in freqs], axis=1).astype(np.float32)
            self.cb(x, self.block, None, None)
            self._n += self.block
            t_next += self.block / self.rate
            time.sleep(max(0.0, t_next - time.monotonic()))

    def stop(self):
        self._run = False
        if self._th is not None and self._th is not threading.current_thread():
            self._th.join(1.0)

    def close(self):
        pass


class Recorder:
    """open() → «ready» (monitoring); record() → «rec» (or «armed» waiting for the trigger); pause() / resume();
    stop() → the recorded Source; close()."""

    def __init__(self):
        self.state = "closed"            # closed | ready | armed | rec | pause
        self.error = ""
        self.backend = ""
        self.device = ""
        self.rate = 48000
        self.channels = 2
        self.frames = 0                  # written so far
        self.overflows = 0
        self.dropped_frames = 0
        self.markers: list[int] = []     # positions from the recording's start
        self.peak_input = 0.0
        self._stream = None
        self._lock = threading.Lock()            # the queue (the callback holds it only to append)
        self._wlock = threading.Lock()           # the writer (held while a block goes to the disk)
        self._q: collections.deque = collections.deque()
        self._qframes = 0
        self._levels = None
        self._writer = None
        self._wthread = None
        self._wake = threading.Event()
        self._run = False
        self._pre: collections.deque = collections.deque()
        self._pre_frames = 0
        self.pre_roll = 0                # frames kept before Record
        self.trigger = None              # linear level that starts the writing (armed), None = at once
        self._folder = ""
        self.monitor = None              # a player.InputMonitor while the input is heard (2.0)

    # ── the input ─────────────────────────────────────────────
    def open(self, device: dict | None, channels: int, rate: int) -> bool:
        self.close()
        self.error = ""
        self.channels, self.rate = int(channels), int(rate)
        self._levels = np.zeros(self.channels, np.float32)
        sd = _sd()
        if sd is None or device is None or device.get("null"):
            self._stream = _NullInput(self.rate, self.channels, self._callback)
            self.backend, self.device = "null", NULL_NAME
        else:
            try:
                extra = None
                try:
                    extra = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    extra = None
                self._stream = sd.InputStream(samplerate=self.rate, channels=self.channels, dtype="float32",
                                              device=device["index"], latency="high", blocksize=0,
                                              callback=self._callback, extra_settings=extra)
                self.backend, self.device = "wasapi", device["name"]
            except Exception as e:            # noqa: BLE001
                self.error = str(e) or type(e).__name__
                self._stream = None
                return False
        self._run = True
        self._wthread = threading.Thread(target=self._write_loop, daemon=True)
        self._wthread.start()
        try:
            self._stream.start()
        except Exception as e:                # noqa: BLE001
            self.error = str(e) or type(e).__name__
            self.close()
            return False
        self.state = "ready"
        return True

    def _callback(self, indata, frames, tinfo, status) -> None:
        if status is not None and getattr(status, "input_overflow", False):
            self.overflows += 1
        x = np.array(indata, np.float32, copy=True).reshape(-1, self.channels)
        pk = np.abs(x).max(axis=0) if len(x) else np.zeros(self.channels, np.float32)
        with self._lock:
            self._levels = np.maximum(self._levels, pk) if self._levels is not None else pk
            if self._qframes + len(x) > QUEUE_SECONDS * self.rate:
                self.overflows += 1
                self.dropped_frames += len(x)
            else:
                self._q.append(x)
                self._qframes += len(x)
        mon = self.monitor
        if mon is not None:
            mon.push(x)
        self._wake.set()

    def levels(self) -> np.ndarray | None:
        """The peak of each channel since the last call (None when closed)."""
        with self._lock:
            lv = self._levels
            if lv is None:
                return None
            self._levels = np.zeros_like(lv)
        return lv

    def queued_seconds(self) -> float:
        return self._qframes / max(1, self.rate)

    # ── recording ─────────────────────────────────────────────
    def record(self, folder: str, pre_roll: float = 0.0, trigger_db: float | None = None) -> bool:
        if self.state not in ("ready",):
            return False
        self._folder = folder
        self.frames = 0
        self.overflows = 0
        self.dropped_frames = 0
        self.markers = []
        self.peak_input = 0.0
        self.trigger = None if trigger_db is None else 10 ** (trigger_db / 20.0)
        w = store.SourceWriter(folder, self.channels)
        with self._wlock:
            with self._lock:
                self._writer = w
                self.state = "armed" if self.trigger is not None else "rec"
                pre = list(self._pre) if self.state == "rec" else []
                if pre:
                    self._pre.clear()
                    self._pre_frames = 0
            for x in pre:
                w.append(x)
                self.frames += len(x)
        self._wake.set()
        return True

    def set_pre_roll(self, seconds: float) -> None:
        self.pre_roll = max(0, int(seconds * self.rate))

    def pause(self) -> None:
        if self.state in ("rec", "armed"):
            self.state = "pause"

    def resume(self) -> None:
        if self.state == "pause":
            self.state = "rec"

    def add_marker(self) -> int | None:
        """A marker at the frame being recorded now (None when not recording)."""
        if self.state not in ("rec", "pause"):
            return None
        pos = self.frames + self._qframes
        self.markers.append(pos)
        return pos

    def stop(self) -> store.Source | None:
        """Stop recording (the input stays open, «ready»); → the recording (None if nothing was recorded)."""
        with self._wlock:                          # the writer thread is not between two blocks now
            with self._lock:
                st = self.state
                if st not in ("rec", "armed", "pause"):
                    return None
                rest = list(self._q) if st == "rec" else []
                self._q.clear()
                self._qframes = 0
                w, self._writer = self._writer, None
                self.state = "ready"
            for x in rest:
                w.append(x)
                self.frames += len(x)
        if w is None:
            return None
        if not w.frames:
            w.abort()
            return None
        src = w.finish()
        self.markers = [min(m, src.frames) for m in self.markers]
        return src

    def _write_loop(self) -> None:
        """Blocks from the queue to the disk (the lock the callback takes is held only to pop a block)."""
        while self._run:
            self._wake.wait(0.05)
            self._wake.clear()
            while self._run:
                with self._wlock:
                    with self._lock:
                        if not self._q:
                            break
                        x = self._q.popleft()
                        self._qframes -= len(x)
                        st = self.state
                        w = self._writer
                        if w is not None and st == "armed" and \
                                float(np.abs(x).max(initial=0.0)) >= (self.trigger or 0.0):
                            self.state = st = "rec"
                            pre = list(self._pre)
                            self._pre.clear()
                            self._pre_frames = 0
                        else:
                            pre = []
                    if w is not None and st == "rec":
                        for y in pre:
                            w.append(y)
                            self.frames += len(y)
                        w.append(x)
                        self.frames += len(x)
                        self.peak_input = max(self.peak_input, float(np.abs(x).max(initial=0.0)))
                    elif st in ("ready", "armed") and self.pre_roll:
                        with self._lock:
                            self._pre.append(x)
                            self._pre_frames += len(x)
                            while self._pre and self._pre_frames - len(self._pre[0]) >= self.pre_roll:
                                self._pre_frames -= len(self._pre.popleft())

    def close(self) -> None:
        with self._wlock:
            with self._lock:
                w, self._writer = self._writer, None
        if w is not None:
            w.abort()
        self._run = False
        self._wake.set()
        st, self._stream = self._stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        th, self._wthread = self._wthread, None
        if th is not None and th is not threading.current_thread():
            th.join(1.0)
        with self._lock:
            self._q.clear()
            self._qframes = 0
            self._pre.clear()
            self._pre_frames = 0
        self.state = "closed"
