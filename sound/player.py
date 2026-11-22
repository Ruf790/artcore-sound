"""Playback through PortAudio (sounddevice): on Windows the WASAPI output, the default device or the one picked in
Settings, with the document's own channels (5.1 plays as 5.1) and rate (Windows converts when the device runs at
another one). A reader thread keeps about half a second of sound ready, so the audio callback never touches the
disk; the callback only copies. Without a sound card (CI, ARTCORE_AUDIO=null) a timer plays into nothing at the
same pace, so the play cursor and the meters behave the same.

The preview of an effect (0.3) plays through an fx chain run in the reader thread, never in the callback: what the
reader prepares goes through it before the queue, and the meters show the processed sound. With an effect the reader
keeps less ready (AHEAD_FX), so a slider is heard within a tenth of a second."""
from __future__ import annotations

import collections
import os
import threading
import time

import numpy as np

BLOCK = 2048                   # frames per chunk the reader prepares
AHEAD = 0.5                    # seconds kept ready
AHEAD_FX = 0.12                # … while an effect is previewed (its changes are heard sooner)
BLOCK_FX = 1024


def _sd():
    if os.environ.get("ARTCORE_AUDIO") == "null":
        return None
    try:
        import sounddevice as sd
        return sd
    except Exception:          # no PortAudio (OSError) or no package
        return None


def hostapi_index(sd) -> int:
    """WASAPI on Windows, else PortAudio's default host API."""
    try:
        for i, h in enumerate(sd.query_hostapis()):
            if "WASAPI" in h["name"]:
                return i
        return sd.default.hostapi
    except Exception:
        return -1


def output_devices() -> list[dict]:
    """[{"name", "channels", "rate", "default"}] of the output host API (WASAPI on Windows)."""
    sd = _sd()
    if sd is None:
        return []
    try:
        api = hostapi_index(sd)
        hosts = sd.query_hostapis()
        default = hosts[api]["default_output_device"] if api >= 0 else -1
        out = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and (api < 0 or dev["hostapi"] == api):
                out.append({"index": i, "name": dev["name"], "channels": dev["max_output_channels"],
                            "rate": int(dev["default_samplerate"]), "default": i == default})
        return out
    except Exception:
        return []


def find_device(name: str) -> int | None:
    """The PortAudio index of the output named `name` (None = the default of the host API)."""
    devs = output_devices()
    if name:
        for d in devs:
            if d["name"] == name:
                return d["index"]
    for d in devs:
        if d["default"]:
            return d["index"]
    return devs[0]["index"] if devs else None


def mix_matrix(labels, out_ch: int) -> np.ndarray:
    """(in, out) gains: the same channels when the output has room, a standard downmix to stereo (centre and
    surrounds at −3 dB, LFE left out) or mono otherwise."""
    labels = list(labels)
    n = len(labels)
    m = np.zeros((n, out_ch), np.float32)
    if out_ch >= n:
        m[np.arange(n), np.arange(n)] = 1.0
        return m
    if out_ch == 1:
        m[:, 0] = 1.0 / n
        return m
    if out_ch == 2:
        k = 0.7071
        for i, lab in enumerate(labels):
            if lab in ("L", "1"):
                m[i, 0] = 1
            elif lab in ("R", "2"):
                m[i, 1] = 1
            elif lab in ("C", "M", "S", "Cs"):
                m[i] = k
            elif lab.startswith("L") and lab != "LFE":
                m[i, 0] = k
            elif lab.startswith("R"):
                m[i, 1] = k
        norm = m.sum(axis=0).max()
        return m / max(1.0, norm)
    m[np.arange(out_ch), np.arange(out_ch)] = 1.0
    return m


class Player:
    """One playback at a time. play(doc, start, end, loop); position() is the frame being heard now."""

    def __init__(self, device: str = "", latency: str = "low"):
        self.device_name, self.latency = device, latency
        self.state = "stop"                 # stop | play | pause
        self.doc = None
        self.start = self.end = self.origin = 0
        self.loop = False
        self.error = ""
        self.backend = ""                    # "wasapi" / "portaudio" / "null"
        self.out_channels = 0
        self.out_rate = 0
        self._lock = threading.Lock()
        self._q: collections.deque = collections.deque()
        self._qframes = 0
        self._next = 0                       # the next frame the reader reads
        self._eof = False
        self._clock = None                   # (frame at the start of the last buffer, its dac time, now-fn)
        self._levels = None
        self._stream = None
        self._reader = None
        self._run = False
        self._cur = None                     # the chunk being played: [pos, data, used, peaks]
        self._paused_at = 0
        self.fx = None                       # an fx.PreviewChain (or anything with process(x) → y) while previewing
        self.finished = threading.Event()

    # ── control ───────────────────────────────────────────────
    def play(self, doc, start: int, end: int | None = None, loop: bool = False, fx=None) -> bool:
        self.stop()
        self.fx = fx
        end = doc.frames if end is None else min(end, doc.frames)
        if end - start <= 0:
            return False
        self.doc, self.start, self.end, self.loop = doc, int(start), int(end), bool(loop)
        self.origin = self.start
        return self._begin(self.start)

    def _begin(self, at: int) -> bool:
        self.error = ""
        self.finished.clear()
        self._q.clear()
        self._qframes = 0
        self._cur = None
        self._next = at
        self._eof = False
        self._clock = (at, time.monotonic(), time.monotonic)
        self._levels = np.zeros(self.doc.channels, np.float32)
        if not self._open():
            return False
        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._fill(int(self.out_rate * (0.06 if self.fx is not None else 0.15)))  # ready before the first callback
        self._reader.start()
        self.state = "play"
        try:
            self._stream.start()
        except Exception as e:
            self.error = str(e)
            self.stop()
            return False
        return True

    def pause(self) -> None:
        if self.state == "play":
            self._paused_at = self.position()
            self._close()
            self.state = "pause"

    def resume(self) -> bool:
        if self.state != "pause" or self.doc is None:
            return False
        at = self._paused_at
        if at >= self.end:
            at = self.start
        return self._begin(at)

    def stop(self) -> None:
        self._close()
        self.state = "stop"

    def _close(self) -> None:
        self._run = False
        st, self._stream = self._stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        r, self._reader = self._reader, None
        if r is not None and r is not threading.current_thread():
            r.join(1.0)

    # ── where we are ──────────────────────────────────────────
    def position(self) -> int:
        """The frame heard now (an estimate from the stream's clock)."""
        if self.state == "pause":
            return self._paused_at
        if self.state != "play" or self._clock is None:
            return self.origin
        pos, t0, now = self._clock
        p = pos + (now() - t0) * self.doc.rate
        span = self.end - self.start
        if self.loop and span > 0 and p >= self.end:
            p = self.start + (p - self.start) % span
        return int(max(self.start, min(self.end, p)))

    def levels(self) -> np.ndarray | None:
        """The peak of each document channel in the sound being heard (None when stopped)."""
        return None if self.state != "play" else self._levels

    @property
    def done(self) -> bool:
        return self.finished.is_set()

    # ── the output ────────────────────────────────────────────
    def _open(self) -> bool:
        sd = _sd()
        doc = self.doc
        if sd is not None:
            dev = find_device(self.device_name)
            if dev is not None:
                info = sd.query_devices(dev)
                api = sd.query_hostapis(info["hostapi"])["name"]
                extra = None
                if "WASAPI" in api:
                    try:
                        extra = sd.WasapiSettings(auto_convert=True)
                    except TypeError:
                        extra = None
                tries = [doc.channels, min(doc.channels, int(info["max_output_channels"])), 2]
                for ch in dict.fromkeys(c for c in tries if c > 0):
                    try:
                        self._mix = mix_matrix(doc.labels, ch) if ch != doc.channels else None
                        self._stream = sd.OutputStream(samplerate=doc.rate, channels=ch, dtype="float32",
                                                       device=dev, latency=self.latency, blocksize=0,
                                                       callback=self._callback, extra_settings=extra,
                                                       finished_callback=self.finished.set)
                        self.out_channels, self.out_rate = ch, doc.rate
                        self.backend = "wasapi" if "WASAPI" in api else "portaudio"
                        st = self._stream
                        self._clock = (self._next, st.time, lambda st=st: st.time)
                        return True
                    except Exception as e:
                        self.error = str(e)
                        self._stream = None
        # no sound card: play into nothing at the same pace
        self._mix = None
        self.out_channels, self.out_rate, self.backend = doc.channels, doc.rate, "null"
        self._stream = _NullStream(doc.rate, doc.channels, self._callback, self.finished.set)
        self._clock = (self._next, time.monotonic(), time.monotonic)
        return True

    def _read_loop(self) -> None:
        want = int(self.out_rate * (AHEAD_FX if self.fx is not None else AHEAD))
        while self._run:
            if self._qframes < want and not self._eof:
                self._fill(want)
            else:
                time.sleep(0.01)

    def _fill(self, want: int) -> None:
        doc = self.doc
        while self._qframes < want and not self._eof:
            p = self._next
            if p >= self.end:
                if self.loop:
                    p = self.start
                else:
                    self._eof = True
                    break
            n = min(BLOCK if self.fx is None else BLOCK_FX, self.end - p)
            x = doc.read(p, n)
            if self.fx is not None:
                x = self.fx.process(x)
            peaks = np.abs(x).max(axis=0) if n else np.zeros(doc.channels, np.float32)
            if self._mix is not None:
                x = x @ self._mix
            with self._lock:
                self._q.append([p, np.ascontiguousarray(x, np.float32), 0, peaks])
                self._qframes += n
            self._next = p + n

    def _callback(self, outdata, frames, tinfo, status) -> None:
        filled = 0
        first_pos = None
        with self._lock:
            while filled < frames:
                if self._cur is None:
                    if not self._q:
                        break
                    self._cur = self._q.popleft()
                pos, data, used, peaks = self._cur
                if first_pos is None:
                    first_pos = pos + used
                    self._levels = peaks
                k = min(frames - filled, len(data) - used)
                outdata[filled:filled + k] = data[used:used + k]
                filled += k
                self._qframes -= k
                if used + k >= len(data):
                    self._cur = None
                else:
                    self._cur[2] = used + k
        if filled < frames:
            outdata[filled:] = 0
        if first_pos is not None and self._clock is not None:
            try:
                dac = tinfo.outputBufferDacTime if tinfo is not None else 0.0
            except AttributeError:
                dac = 0.0
            _p, _t, now = self._clock
            self._clock = (first_pos, dac or now(), now)
        if filled == 0 and self._eof:
            self._levels = np.zeros_like(self._levels)
            sd = _sd()
            if sd is not None and self.backend != "null":
                raise sd.CallbackStop()
            raise StopIteration()


class _NullStream:
    """The same callback, called by a timer at the pace of the sound card that is not there."""

    def __init__(self, rate: int, channels: int, callback, finished):
        self.rate, self.channels, self.cb, self.finished = rate, channels, callback, finished
        self._run = False
        self._th = None
        self.blocksize = 1024

    def start(self):
        self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        buf = np.zeros((self.blocksize, self.channels), np.float32)
        t_next = time.monotonic()
        try:
            while self._run:
                self.cb(buf, self.blocksize, None, None)
                t_next += self.blocksize / self.rate
                time.sleep(max(0.0, t_next - time.monotonic()))
        except StopIteration:
            pass
        self.finished()

    def stop(self):
        self._run = False
        if self._th is not None and self._th is not threading.current_thread():
            self._th.join(1.0)

    def close(self):
        pass
