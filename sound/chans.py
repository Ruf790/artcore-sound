"""The 5.1 tools (2.0).

Effects in the effects' frame (live preview, presets, batch steps; the same result whole or in chunks):
- the channel matrix: every output channel a sum of the inputs with gains — swap, copy, mute, boost channels;
- dialog boost: in 5.1 the centre against the rest; in stereo the middle (L + R) in the speech band against the sides.

Channel conversions (Process → Channels, a batch step): 5.1 → stereo by a matrix — ITU-R BS.775 / ATSC A/52 (Lo/Ro
with the centre and the surrounds at −3 dB), LoRo with the surrounds at −6 dB, Dolby Pro Logic II (Lt/Rt: the
surrounds 90° out of phase, through a Hilbert FIR) — and stereo → 5.1 (an upmix: the centre from the middle, the
LFE below 120 Hz, the surrounds from the difference, delayed and darkened). Also a channel taken out into a mono file
and 5.1 put together from six mono files."""
from __future__ import annotations

import os

import numpy as np
from scipy.signal import butter, sosfilt

from . import ff, store
from .fx.base import History
from .fx.channels import MAXC, OFF_DB, ChannelMatrix, DialogBoost, gains, identity   # noqa: F401
from .media import Progress

SQ = 0.7071067811865476


# ── 5.1 → stereo, stereo → 5.1 ────────────────────────────────
DOWNMIX = ("itu", "loro", "pl2")
HILBERT_TAPS = 255


def _idx(labels) -> dict:
    lab = [s.upper() for s in labels]
    out = {}
    for name, alts in (("L", ("L", "FL")), ("R", ("R", "FR")), ("C", ("C", "FC")), ("LFE", ("LFE",)),
                       ("Ls", ("LS", "SL", "BL", "LB")), ("Rs", ("RS", "SR", "BR", "RB"))):
        for a in alts:
            if a in lab:
                out[name] = lab.index(a)
                break
    return out


def hilbert_fir(n: int = HILBERT_TAPS) -> np.ndarray:
    """A 90° phase shifter (type III FIR, Hamming window): its output lags the input by (n − 1) / 2 and a quarter
    period."""
    k = np.arange(n) - (n - 1) // 2
    h = np.zeros(n)
    odd = k % 2 != 0
    h[odd] = 2.0 / (np.pi * k[odd])
    return h * np.hamming(n)


class Downmix:
    """5.1 (or anything with L R C Ls Rs) → stereo, chunk by chunk with its state (the Hilbert's history)."""

    def __init__(self, kind: str, labels, normalize: bool = True):
        self.kind = kind if kind in DOWNMIX else "itu"
        self.ix = _idx(labels)
        self.ch = len(labels)
        surr = 0.5 if self.kind == "loro" else SQ
        if self.kind == "pl2":
            self.h = hilbert_fir()
            self.d = (len(self.h) - 1) // 2
            self.hist = History(len(self.h) - 1, 2)
            self.fhist = History(self.d, 3)
            self.k = 1.0 / (1 + SQ + 0.8718 + 0.4899) if normalize else 1.0
        else:
            m = np.zeros((self.ch, 2))
            for name, (a, b) in (("L", (1, 0)), ("R", (0, 1)), ("C", (SQ, SQ)), ("Ls", (surr, 0)),
                                 ("Rs", (0, surr))):
                if name in self.ix:
                    m[self.ix[name]] = (a, b)
            if not self.ix:                                   # unknown channels: a plain average
                m[:] = 1.0 / self.ch
            if normalize:
                s = m.sum(axis=0).max()
                if s > 1:
                    m /= s
            self.m = m

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64)
        if self.kind != "pl2":
            return (x @ self.m).astype(np.float32)
        z = np.zeros(len(x))
        g = lambda n: x[:, self.ix[n]] if n in self.ix else z           # noqa: E731
        sl, sr = g("Ls"), g("Rs")
        # Lt = L + .707 C − j(.8718 Ls + .4899 Rs),  Rt = R + .707 C + j(.4899 Ls + .8718 Rs)
        sur = np.stack((0.8718 * sl + 0.4899 * sr, 0.4899 * sl + 0.8718 * sr), axis=1)
        cat = self.hist.join(sur)
        from scipy.signal import oaconvolve
        hs = np.stack([oaconvolve(cat[:, j], self.h, mode="valid") for j in range(2)], axis=1)
        front = self.fhist.join(np.stack((g("L"), g("R"), g("C")), axis=1))[:len(x)]
        lt = front[:, 0] + SQ * front[:, 2] - hs[:, 0]
        rt = front[:, 1] + SQ * front[:, 2] + hs[:, 1]
        return (np.stack((lt, rt), axis=1) * self.k).astype(np.float32)

    @property
    def latency(self) -> int:
        return self.d if self.kind == "pl2" else 0


class Upmix:
    """Stereo → 5.1 (L R C LFE Ls Rs): L, R as they are; C = .707 · the middle in 100 Hz … 8 kHz; LFE = the middle
    below 120 Hz; Ls / Rs = ± the difference below 7 kHz, 12 ms later, at −3 dB."""

    def __init__(self, rate: int):
        self.rate = rate
        self.sos_c = butter(2, [100.0, min(8000.0, rate * 0.45)], btype="bandpass", fs=rate, output="sos")
        self.sos_l = butter(4, 120.0, btype="lowpass", fs=rate, output="sos")
        self.sos_s = butter(2, min(7000.0, rate * 0.45), btype="lowpass", fs=rate, output="sos")
        self.zc = np.zeros((self.sos_c.shape[0], 2))
        self.zl = np.zeros((self.sos_l.shape[0], 2))
        self.zs = np.zeros((self.sos_s.shape[0], 2))
        self.delay = History(int(round(0.012 * rate)), 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float64)
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        L, R = x[:, 0], x[:, 1]
        m, s = 0.5 * (L + R), 0.5 * (L - R)
        c, self.zc = sosfilt(self.sos_c, m, zi=self.zc)
        lfe, self.zl = sosfilt(self.sos_l, m, zi=self.zl)
        sd, self.zs = sosfilt(self.sos_s, s, zi=self.zs)
        sd = self.delay.join(sd[:, None])[:len(x), 0]
        out = np.stack((L - 0.5 * SQ * c, R - 0.5 * SQ * c, SQ * c, lfe, SQ * sd, -SQ * sd), axis=1)
        return out.astype(np.float32)


def convert(d, channels: int, method: str = "auto", progress: Progress | None = None) -> store.Source:
    """Process → Channels with a method: 6 → 2 by DOWNMIX, 2 → 6 by «upmix»; anything else by ffmpeg."""
    from .proc import convert_document, map_chunks
    if d.channels >= 5 and channels == 2 and method in DOWNMIX:
        dm = Downmix(method, d.labels)
        lat = dm.latency
        if not lat:
            return map_chunks(d, 0, d.frames, lambda x, _p: dm.process(x), progress, channels=2)
        return _with_latency(d, dm, lat, 2, progress)
    if d.channels in (1, 2) and channels == 6 and method == "upmix":
        um = Upmix(d.rate)
        return map_chunks(d, 0, d.frames, lambda x, _p: um.process(x), progress, channels=6)
    return convert_document(d, d.rate, channels, ff.layout_for(channels), progress)


def _with_latency(d, proc, lat: int, out_ch: int, progress) -> store.Source:
    progress = progress or Progress()
    w = store.SourceWriter(d.folder, out_ch)
    skip = lat
    try:
        for pos, x in d.chunks(0, d.frames):
            y = proc.process(x)
            if skip:
                k = min(skip, len(y))
                y, skip = y[k:], skip - k
            w.append(y)
            progress((pos + len(x)) / max(1, d.frames))
        y = proc.process(np.zeros((lat, d.channels), np.float32))
        w.append(y[skip:][:d.frames - w.frames])
    except BaseException:
        w.abort()
        raise
    return w.finish()


# ── channels out to mono files, six mono files into 5.1 ───────
def extract(d, channel: int, progress: Progress | None = None) -> store.Source:
    from .proc import map_chunks
    return map_chunks(d, 0, d.frames, lambda x, _p: x[:, channel:channel + 1], progress, channels=1)


SUFFIX_ORDER = (("L", ("l", "fl", "left")), ("R", ("r", "fr", "right")), ("C", ("c", "fc", "center", "centre")),
                ("LFE", ("lfe", "sub", "lf")), ("Ls", ("ls", "sl", "bl", "lsurround", "surroundl")),
                ("Rs", ("rs", "sr", "br", "rsurround", "surroundr")))


def guess_slots(paths) -> list[str]:
    """Six files → their order L R C LFE Ls Rs by the names' endings (…_L.wav, …-FC.wav, …LFE.flac); "" where
    nothing fits."""
    import re
    out = [""] * 6
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        tail = re.split(r"[ _\-.()\[\]]+", stem)
        last = tail[-1] if tail else stem
        for k, (_n, names) in enumerate(SUFFIX_ORDER):
            if not out[k] and last in names:
                out[k] = p
                break
    return out


def assemble(paths, folder: str, progress: Progress | None = None) -> tuple[store.Source, int]:
    """Six mono files (L R C LFE Ls Rs) → a 5.1 source and its rate. A file with more channels gives its first; the
    rates follow the first file (ffmpeg resamples the others); shorter files are padded with silence."""
    from . import media
    progress = progress or Progress()
    if len(paths) != 6 or not all(paths):
        raise ValueError("six files are needed")
    rate = 0
    srcs = []
    for k, p in enumerate(paths):
        info = ff.probe(p)
        tr = info.tracks[0]
        rate = rate or tr.rate
        srcs.append(_decode_mono(p, tr, folder, rate, media.Progress(
            lambda f, k=k: progress(0.8 * (k + f) / 6))))
    n = max(s.frames for s in srcs)
    w = store.SourceWriter(folder, 6)
    try:
        pos = 0
        while pos < n:
            k = min(store.CHUNK, n - pos)
            cols = []
            for s in srcs:
                c = np.zeros(k, np.float32)
                m = max(0, min(k, s.frames - pos))
                if m:
                    c[:m] = s.read(pos, m)[:, 0]
                cols.append(c)
            w.append(np.stack(cols, axis=1))
            pos += k
            progress(0.8 + 0.2 * pos / n)
    except BaseException:
        w.abort()
        raise
    finally:
        for s in srcs:
            s.remove() if hasattr(s, "remove") else None
    return w.finish(), rate


def _decode_mono(path: str, track, folder: str, rate: int, progress) -> store.Source:
    import subprocess

    from .media import _stderr_reader, pipe_frames
    args = [ff.ffmpeg(), "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", f"0:a:{track.pos}",
            "-af", "pan=mono|c0=c0", "-ar", str(rate), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = ff.popen(args, stdout=subprocess.PIPE)
    err = _stderr_reader(proc)
    w = store.SourceWriter(folder, 1)
    total = max(1.0, track.duration * rate)
    try:
        for x in pipe_frames(proc.stdout, 1):
            w.append(x)
            progress(min(1.0, w.frames / total))
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        w.abort()
        raise
    if proc.returncode != 0:
        w.abort()
        raise RuntimeError("\n".join(err[-4:]) or "ffmpeg")
    return w.finish()
