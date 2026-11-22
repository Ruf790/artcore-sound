"""Processing a range of a document: the frames are read in chunks, changed and written into a new source, which
then takes the range's place (one undo step; only the changed frames are stored). Also the conversion of a clip
to another rate / number of channels through ffmpeg (pasting between files)."""
from __future__ import annotations

import math
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from . import ff, store
from .doc import Clip, Document
from .media import Cancelled, Progress, _stderr_reader, ch_layout_args


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20.0))


def gain_to_db(g: float) -> float:
    return float(20 * np.log10(g)) if g > 0 else float("-inf")


class SubProgress:
    """The part lo … hi of a parent Progress (a job of several passes); Cancel goes through to the parent."""

    def __init__(self, parent: Progress | None, lo: float, hi: float):
        self.parent, self.lo, self.hi = parent or Progress(), lo, hi

    def __call__(self, frac: float) -> None:
        self.parent(self.lo + (self.hi - self.lo) * max(0.0, min(1.0, frac)))


def map_chunks(d: Document, a: int, b: int, fn, progress: Progress | None = None,
               channels: int | None = None) -> store.Source:
    """A new source of fn(frames, position from a) over [a, b) of the document, chunk by chunk (fn may give
    another number of channels: `channels`)."""
    progress = progress or Progress()
    w = store.SourceWriter(d.folder, channels or d.channels)
    total = max(1, b - a)
    try:
        for pos, x in d.chunks(a, b):
            w.append(fn(x, pos - a))
            progress((pos - a + len(x)) / total)
    except BaseException:
        w.abort()
        raise
    return w.finish()


def reversed_source(d: Document, a: int, b: int, progress: Progress | None = None) -> store.Source:
    progress = progress or Progress()
    w = store.SourceWriter(d.folder, d.channels)
    total = max(1, b - a)
    end = b
    try:
        while end > a:
            n = min(store.CHUNK, end - a)
            w.append(d.read(end - n, n)[::-1])
            end -= n
            progress((b - end) / total)
    except BaseException:
        w.abort()
        raise
    return w.finish()


# ── the simple operations (Обработка) ─────────────────────────
def curve_gain(curve: str, t: np.ndarray) -> np.ndarray:
    """Gain for positions t in 0…1 of a fade: linear | log (fast start) | exp (slow start) | scurve."""
    t = np.clip(t, 0.0, 1.0)
    if curve == "log":
        return np.sqrt(t)
    if curve == "exp":
        return t * t
    if curve == "scurve":
        return 0.5 - 0.5 * np.cos(np.pi * t)
    return t


def apply_op(d: Document, op: str, a: int, b: int, progress: Progress | None = None, **kw) -> store.Source:
    """The new frames of [a, b) after op: gain (db) | normalize (db, the target peak) | fade_in | fade_out
    (curve) | invert | reverse."""
    n = b - a
    if op == "reverse":
        return reversed_source(d, a, b, progress)
    if op == "invert":
        return map_chunks(d, a, b, lambda x, _p: -x, progress)
    if op in ("gain", "normalize"):
        if op == "normalize":
            peak = float(d.peak(a, b).max())
            if peak <= 0:
                raise ValueError("silence")
            g = db_to_gain(kw.get("db", -0.1)) / peak
        else:
            g = db_to_gain(kw.get("db", 0.0))
        return map_chunks(d, a, b, lambda x, _p: x * np.float32(g), progress)
    if op in ("fade_in", "fade_out"):
        curve = kw.get("curve", "linear")

        def f(x, p):
            t = np.arange(p, p + len(x), dtype=np.float64) / max(1, n - 1)
            g = curve_gain(curve, 1.0 - t if op == "fade_out" else t)
            return x * g.astype(np.float32)[:, None]
        return map_chunks(d, a, b, f, progress)
    raise ValueError(op)


# ── by the analysis (0.2) ─────────────────────────────────────
LUFS_TARGETS = (-23.0, -16.0, -14.0)      # EBU R128 broadcast; podcasts / Apple; streaming (Spotify, YouTube)


@dataclass
class NormResult:
    """What a normalization did: the gain, whether the true-peak ceiling held it back, the figures before and the
    expected ones after."""
    gain_db: float
    limited: bool
    before: float                  # LUFS, or the peak in dBFS / dBTP
    after: float
    true_peak_after: float         # dBTP (NaN when not measured)
    mode: str


def loudness_gain(integrated: float, true_peak_db: float, target: float, ceiling: float) -> tuple[float, bool]:
    """The gain that brings the integrated loudness to target, held back so the true peak stays at the ceiling.
    → (gain dB, limited)."""
    g = target - integrated
    if true_peak_db + g > ceiling:
        return ceiling - true_peak_db, True
    return g, False


def normalize(d: Document, a: int, b: int, mode: str, target: float, ceiling: float = -1.0,
              true_peak: bool = False, progress: Progress | None = None) -> tuple[store.Source, NormResult]:
    """mode "loudness": the integrated loudness of [a, b) to target LUFS with the true peak at most `ceiling`
    dBTP; mode "peak": the sample peak (true_peak=False) or the true peak to target dB. Two passes: the analysis,
    then the gain. ValueError("silence") when there is nothing to measure."""
    from . import analysis
    st = analysis.analyze(d, a, b, SubProgress(progress, 0.0, 0.5), loudness=mode == "loudness",
                          true_peak=mode == "loudness" or true_peak)
    if mode == "loudness":
        if not math.isfinite(st.integrated):
            raise ValueError("silence")
        tp = analysis.to_db(st.true_peak)
        g, limited = loudness_gain(st.integrated, tp, target, ceiling)
        res = NormResult(g, limited, st.integrated, st.integrated + g, tp + g, mode)
    else:
        pk = st.true_peak if true_peak else st.peak
        if pk <= 0:
            raise ValueError("silence")
        before = analysis.to_db(pk)
        g = target - before
        res = NormResult(g, False, before, target, analysis.to_db(st.true_peak) + g if true_peak else float("nan"),
                         mode)
    k = np.float32(db_to_gain(res.gain_db))
    src = map_chunks(d, a, b, lambda x, _p: x * k, SubProgress(progress, 0.5, 1.0))
    return src, res


def remove_dc(d: Document, a: int, b: int, progress: Progress | None = None) -> tuple[store.Source, np.ndarray]:
    """Subtract each channel's mean over [a, b). → (the new frames, the offsets removed)."""
    from . import analysis
    st = analysis.analyze(d, a, b, SubProgress(progress, 0.0, 0.4), loudness=False, true_peak=False)
    dc = np.array([c.dc for c in st.channels], np.float32)
    return map_chunks(d, a, b, lambda x, _p: x - dc, SubProgress(progress, 0.4, 1.0)), dc


def sound_edges(d: Document, a: int, b: int, threshold_db: float, margin: int = 0) -> tuple[int, int] | None:
    """The part of [a, b) from the first to the last frame louder than threshold_db (any channel), widened by
    `margin` frames each side; None when all of it is quieter. Found from the peak levels a chunk at a time, so
    long silences cost almost nothing."""
    a, b = max(0, a), min(d.frames, b)
    thr = db_to_gain(threshold_db)
    step = store.CHUNK
    first = last = None
    pos = a
    while pos < b:
        e = min(b, pos + step)
        if d.peak(pos, e).max() > thr:
            x = np.abs(d.read(pos, e - pos)).max(axis=1)
            first = pos + int(np.argmax(x > thr))
            break
        pos = e
    if first is None:
        return None
    end = b
    while end > first:
        s = max(first, end - step)
        if d.peak(s, end).max() > thr:
            x = np.abs(d.read(s, end - s)).max(axis=1)
            last = s + int(len(x) - 1 - np.argmax((x > thr)[::-1]))
            break
        end = s
    if last is None:
        last = first
    return max(a, first - margin), min(b, last + 1 + margin)


def stereo_to_mono(d: Document, mode: str = "mix", progress: Progress | None = None) -> store.Source:
    """mix: (L + R) / 2; left / right: one channel."""
    if d.channels != 2:
        raise ValueError("not stereo")
    if mode == "left":
        fn = lambda x, _p: x[:, :1]                                       # noqa: E731
    elif mode == "right":
        fn = lambda x, _p: x[:, 1:2]                                      # noqa: E731
    else:
        fn = lambda x, _p: (x[:, :1] + x[:, 1:2]) * np.float32(0.5)       # noqa: E731
    return map_chunks(d, 0, d.frames, fn, progress, channels=1)


def mono_to_stereo(d: Document, progress: Progress | None = None) -> store.Source:
    """The one channel into both."""
    if d.channels != 1:
        raise ValueError("not mono")
    return map_chunks(d, 0, d.frames, lambda x, _p: np.repeat(x, 2, axis=1), progress, channels=2)


# ── through ffmpeg ────────────────────────────────────────────
def pipe_through(reader, frames: int, in_rate: int, in_ch: int, in_layout: str, folder: str, out_rate: int,
                 out_ch: int, out_layout: str = "", af: str = "", progress: Progress | None = None) -> store.Source:
    """Frames from reader (an iterable of (n, in_ch) arrays, `frames` in all) through ffmpeg (resampling with
    soxr when the build has it, channel mixing, an -af filter) into a new source."""
    progress = progress or Progress()
    exe = ff.ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    filters = []
    if af:
        filters.append(af)
    if out_rate != in_rate:
        soxr = "soxr" if "soxr" in ff.caps().get("filters", set()) or _has_soxr() else ""
        filters.append(f"aresample={out_rate}" + (":resampler=soxr:precision=28" if soxr else ""))
    lay = ff.layout_for(out_ch, out_layout) or f"{out_ch}c"
    args = [exe, "-nostdin", "-hide_banner", "-v", "error", "-f", "f32le", "-ar", str(in_rate),
            *ch_layout_args(in_ch, in_layout), "-i", "pipe:0"]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-ch_layout", lay, "-ar", str(out_rate), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = ff.popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    err = _stderr_reader(proc)
    failed: list = []

    def feed():
        try:
            for x in reader:
                proc.stdin.write(np.ascontiguousarray(x, np.float32).tobytes())
        except Exception as e:           # a broken pipe when ffmpeg stopped, or Cancelled from the reader
            failed.append(e)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
    th = threading.Thread(target=feed, daemon=True)
    th.start()
    w = store.SourceWriter(folder, out_ch)
    fb = 4 * out_ch
    want = max(1.0, frames * out_rate / in_rate)
    rest = b""
    try:
        while True:
            data = proc.stdout.read(store.CHUNK * fb)
            if not data:
                break
            data = rest + data
            whole = len(data) // fb * fb
            rest = data[whole:]
            if whole:
                w.append(np.frombuffer(data[:whole], np.float32).reshape(-1, out_ch))
            progress(w.frames / want)
        proc.wait()
        th.join(5)
    except BaseException:
        proc.kill()
        proc.wait()
        w.abort()
        raise
    if any(isinstance(e, Cancelled) for e in failed):
        w.abort()
        raise Cancelled()
    if proc.returncode != 0:
        w.abort()
        raise RuntimeError("\n".join(err[-6:]) or f"ffmpeg exit {proc.returncode}")
    return w.finish()


_soxr: bool | None = None


def _has_soxr() -> bool:
    """Is this ffmpeg built with libsoxr (aresample=resampler=soxr)?"""
    global _soxr
    if _soxr is None:
        _soxr = False
        exe = ff.ffmpeg()
        if exe:
            try:
                r = ff.run([exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:d=0.01",
                            "-af", "aresample=48000:resampler=soxr", "-f", "null", "-"], 20)
                _soxr = r.returncode == 0
            except Exception:
                pass
    return _soxr


def clip_frames(clip: Clip, size: int = store.CHUNK):
    """The frames of a clip, chunk by chunk."""
    for src, off, n in clip.pieces:
        pos = 0
        while pos < n:
            k = min(size, n - pos)
            yield src.read(off + pos, k)
            pos += k


def convert_clip(clip: Clip, d: Document, progress: Progress | None = None) -> Clip:
    """The clip in the document's rate and channels (ffmpeg's resampler and channel mixing)."""
    src = pipe_through(clip_frames(clip), clip.frames, clip.rate, clip.channels, clip.layout, d.folder, d.rate,
                       d.channels, d.layout, progress=progress)
    return Clip(((src, 0, src.frames),), src.frames, d.rate, d.channels, tuple(d.labels), d.layout)


def convert_document(d: Document, rate: int, channels: int, layout: str = "",
                     progress: Progress | None = None) -> store.Source:
    """The whole document at another rate / number of channels (Обработка → Частота, Каналы)."""
    reader = (x for _p, x in d.chunks(0, d.frames))
    return pipe_through(reader, d.frames, d.rate, d.channels, d.layout, d.folder, rate, channels, layout,
                        progress=progress)
