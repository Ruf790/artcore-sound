"""The spectrum of a range (0.4): Welch's method — windowed FFT segments half a segment apart, their powers averaged
— over the whole range read chunk by chunk (a film too: only the accumulator and one segment in memory). The
segments start at fixed offsets from the range's start, so the result does not depend on how the frames are cut into
chunks. Levels are in dBFS of a sine's amplitude: a full-scale sine at the middle of a bin reads 0 dB, whatever the
window (the window's coherent gain is taken out); the flat-top window reads it right between bins too.

Also the octave smoothing (1/3, 1/6), the main peak (parabolic interpolation over three bins), the note of a
frequency (A4 = 440 Hz) and the live spectrum of the frames the player is playing."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import signal

from . import store
from .media import Progress

FFT_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
WINDOWS = ("hann", "blackmanharris", "flattop")
SMOOTH = (0, 3, 6)                 # 1/N octave; 0 = none
NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FLOOR_DB = -200.0


@dataclass
class Settings:
    fft: int = 8192
    window: str = "hann"
    log_freq: bool = True
    db_lo: float = -120.0
    db_hi: float = 0.0
    mode: str = "split"            # split: a curve per channel; sum: one curve of the channels' mix
    smooth: int = 0

    @classmethod
    def from_dict(cls, v) -> "Settings":
        s = cls()
        v = v if isinstance(v, dict) else {}
        try:
            fft = int(v.get("fft", s.fft))
            s.fft = fft if fft in FFT_SIZES else s.fft
        except (TypeError, ValueError):
            pass
        s.window = v.get("window") if v.get("window") in WINDOWS else s.window
        s.log_freq = bool(v.get("log_freq", s.log_freq))
        for k in ("db_lo", "db_hi"):
            try:
                setattr(s, k, float(v.get(k, getattr(s, k))))
            except (TypeError, ValueError):
                pass
        s.db_lo = max(-200.0, min(-20.0, s.db_lo))
        s.db_hi = max(s.db_lo + 20.0, min(24.0, s.db_hi))
        s.mode = v.get("mode") if v.get("mode") in ("split", "sum") else s.mode
        try:
            sm = int(v.get("smooth", s.smooth))
            s.smooth = sm if sm in SMOOTH else 0
        except (TypeError, ValueError):
            pass
        return s

    def to_dict(self) -> dict:
        return asdict(self)


def window(name: str, n: int) -> np.ndarray:
    return signal.get_window(name if name in WINDOWS else "hann", n, fftbins=True).astype(np.float64)


@dataclass
class Result:
    rate: int
    fft: int
    freqs: np.ndarray              # (bins,)
    power: np.ndarray              # (curves, bins) linear (amplitude²); levels() → dB
    labels: list
    frames: int = 0
    segments: int = 0
    window: str = "hann"

    def levels(self, smooth: int = 0) -> np.ndarray:
        p = self.power if not smooth else smooth_octave(self.freqs, self.power, smooth)
        with np.errstate(divide="ignore"):
            return np.maximum(FLOOR_DB, 10.0 * np.log10(np.maximum(p, 1e-30)))


class Welch:
    """Feed frames in order (chunks of any length), then result()."""

    def __init__(self, rate: int, fft: int, win: str, channels: int, labels=None, mode: str = "split"):
        self.rate, self.n, self.mode = int(rate), int(fft), mode
        self.hop = self.n // 2
        self.win_name = win
        self.w = window(win, self.n)
        self.scale = 2.0 / self.w.sum()                      # amplitude of a sine from |X|
        self.ch = channels
        self.curves = 1 if mode == "sum" else channels
        self.labels = (["Σ"] if mode == "sum" else list(labels or [str(i + 1) for i in range(channels)]))
        self.acc = np.zeros((self.curves, self.n // 2 + 1))
        self.count = 0
        self.frames = 0
        self._buf = np.zeros((0, self.curves), np.float64)   # frames from the next segment's start on

    def _prep(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64).reshape(-1, self.ch)
        return x.mean(axis=1, keepdims=True) if self.mode == "sum" else x

    def feed(self, x: np.ndarray) -> None:
        x = self._prep(x)
        if not len(x):
            return
        self.frames += len(x)
        buf = np.concatenate((self._buf, x)) if len(self._buf) else x
        n, hop = self.n, self.hop
        k = (len(buf) - n) // hop + 1 if len(buf) >= n else 0
        if k > 0:
            step = max(1, 2 ** 22 // (n * self.curves))      # segments per batch: ≤ 32 MB of float64
            for s0 in range(0, k, step):
                s1 = min(k, s0 + step)
                idx = (np.arange(s0, s1) * hop)[:, None] + np.arange(n)[None, :]
                seg = buf[idx]                                 # (segs, n, curves)
                spec = np.fft.rfft(seg * self.w[None, :, None], axis=1)
                self.acc += (np.abs(spec) ** 2).sum(axis=0).T
            self.count += k
            buf = buf[k * hop:]
        self._buf = buf.copy()

    def result(self) -> Result:
        n = self.n
        acc, count = self.acc, self.count
        if not count and len(self._buf):
            # a range shorter than a segment: its own window over the frames it has, zero-padded to the FFT size
            m = len(self._buf)
            w = window(self.win_name, m)
            spec = np.fft.rfft(self._buf * w[:, None], n=n, axis=0)
            acc = (np.abs(spec) ** 2).T
            count = 1
            scale = 2.0 / max(1e-12, w.sum())
        else:
            scale = self.scale
        power = acc / max(1, count) * scale ** 2
        power[:, 0] /= 4.0                                   # DC (and Nyquist) are not mirrored: no ×2 there
        power[:, -1] /= 4.0
        freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        return Result(self.rate, n, freqs, power, self.labels, self.frames, count, self.win_name)


def spectrum(d, a: int, b: int, st: Settings, progress: Progress | None = None) -> Result:
    """The Welch spectrum of frames [a, b) of a document (a fixed copy of its piece list: the document may change
    meanwhile)."""
    from .analysis import snapshot_chunks
    progress = progress or Progress()
    a, b = max(0, int(a)), min(d.frames, int(b))
    w = Welch(d.rate, st.fft, st.window, d.channels, d.labels, st.mode)
    total = max(1, b - a)
    done = 0
    progress(0.0)
    for x in snapshot_chunks(d.slice(a, b), d.channels, store.CHUNK):
        w.feed(x)
        done += len(x)
        progress(done / total)
    return w.result()


def of_frames(x: np.ndarray, rate: int, st: Settings, labels=None) -> Result:
    """The spectrum of a few frames in memory (the live spectrum of what plays): Welch over them."""
    x = np.asarray(x, np.float64)
    n = min(st.fft, max(64, 1 << int(math.log2(max(64, len(x))))))
    w = Welch(rate, n, st.window, x.shape[1], labels, st.mode)
    w.feed(x)
    return w.result()


def smooth_octave(freqs: np.ndarray, power: np.ndarray, frac: int) -> np.ndarray:
    """The mean power over 1/frac of an octave around every bin (log-spaced band, so the curve stays as wide at
    100 Hz as at 10 kHz)."""
    if not frac:
        return power
    k = 2.0 ** (1.0 / (2 * frac))
    lo = np.searchsorted(freqs, freqs / k, side="left")
    hi = np.searchsorted(freqs, freqs * k, side="right")
    hi = np.maximum(hi, lo + 1)
    c = np.concatenate((np.zeros((power.shape[0], 1)), np.cumsum(power, axis=1)), axis=1)
    out = (c[:, hi] - c[:, lo]) / (hi - lo)
    out[:, 0] = power[:, 0]
    return out


def peak(freqs: np.ndarray, levels_db: np.ndarray, f_min: float = 10.0, win: str = "hann") -> tuple[float, float]:
    """(frequency, level dB) of the highest point of a curve, refined between bins by a parabola (the flat-top
    window's top is flat: its highest bin is the level already, only the frequency is refined)."""
    lv = np.asarray(levels_db, np.float64)
    ok = np.flatnonzero(freqs >= f_min)
    if not len(ok):
        return 0.0, FLOOR_DB
    i = int(ok[np.argmax(lv[ok])])
    if 0 < i < len(lv) - 1:
        y0, y1, y2 = lv[i - 1], lv[i], lv[i + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            dx = 0.5 * (y0 - y2) / den
            df = freqs[1] - freqs[0]
            lvl = y1 if win == "flattop" else y1 - 0.25 * (y0 - y2) * dx
            return float(freqs[i] + dx * df), float(lvl)
    return float(freqs[i]), float(lv[i])


def note(f: float) -> str:
    """«A4 +0 ct» for 440 Hz (A4 = 440); "" for frequencies out of the musical range."""
    if not (16.0 <= f <= 30000.0):
        return ""
    n = 12.0 * math.log2(f / 440.0) + 69.0
    k = int(round(n))
    ct = int(round((n - k) * 100))
    return f"{NOTES[k % 12]}{k // 12 - 1} {'+' if ct >= 0 else '−'}{abs(ct)} ct"


def to_csv(res: Result, smooth: int = 0) -> str:
    lv = res.levels(smooth)
    head = "Frequency (Hz)," + ",".join(f"{lab} (dB)" for lab in res.labels)
    rows = [head]
    for i, f in enumerate(res.freqs):
        rows.append(f"{f:.3f}," + ",".join(f"{lv[c, i]:.2f}" for c in range(lv.shape[0])))
    return "\r\n".join(rows) + "\r\n"
