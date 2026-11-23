"""Opening and saving through ffmpeg. Opening decodes one sound track (of an audio or a video file) into a source
on disk — as raw float32 through a pipe, peaks built on the way; saving pipes the document's frames into ffmpeg,
which encodes WAV 16 / 24 / 32 float, FLAC, MP3, AAC, Opus or AC-3."""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from . import ff, store
from .doc import Document


class Cancelled(Exception):
    pass


class Progress:
    """What a long job reports: fraction done (0…1) and whether the user pressed Cancel."""

    def __init__(self, cb=None):
        self.cb = cb
        self.cancelled = False

    def __call__(self, frac: float) -> None:
        if self.cancelled:
            raise Cancelled()
        if self.cb:
            self.cb(max(0.0, min(1.0, frac)))

    def cancel(self) -> None:
        self.cancelled = True


def _stderr_reader(proc) -> list:
    buf: list = []

    def go():
        try:
            for ln in iter(proc.stderr.readline, b""):
                buf.append(ln.decode("utf-8", "replace").rstrip())
                del buf[:-40]
        except (OSError, ValueError):
            pass
    threading.Thread(target=go, daemon=True).start()
    return buf


def ch_layout_args(channels: int, layout: str) -> list[str]:
    lay = ff.layout_for(channels, layout)
    return ["-ch_layout", lay or f"{channels}c"]


# ── opening ───────────────────────────────────────────────────
def decode(path: str, track: ff.Track, folder: str, progress: Progress | None = None) -> store.Source:
    """Decode the track into a new source. Raises Cancelled, or RuntimeError with ffmpeg's last words."""
    progress = progress or Progress()
    exe = ff.ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    ch = track.channels
    args = [exe, "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", f"0:a:{track.pos}", "-vn", "-sn",
            "-dn", "-ac", str(ch), "-ar", str(track.rate), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = ff.popen(args, stdout=subprocess.PIPE)
    err = _stderr_reader(proc)
    w = store.SourceWriter(folder, ch)
    frame_bytes = 4 * ch
    want = max(1.0, (track.duration or 0) * track.rate)
    rest = b""
    try:
        while True:
            data = proc.stdout.read(store.CHUNK * frame_bytes)
            if not data:
                break
            data = rest + data
            whole = len(data) // frame_bytes * frame_bytes
            rest = data[whole:]
            if whole:
                w.append(np.frombuffer(data[:whole], np.float32).reshape(-1, ch))
            progress(w.frames / want if track.duration else 0.0)
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        w.abort()
        raise
    if proc.returncode != 0 and not w.frames:
        w.abort()
        raise RuntimeError("\n".join(err[-6:]) or f"ffmpeg exit {proc.returncode}")
    src = w.finish()
    progress(1.0)
    return src


def open_file(path: str, folder: str, track_pos: int = 0, progress: Progress | None = None,
              info: ff.FileInfo | None = None) -> Document:
    """A new document with the given sound track of the file."""
    info = info or ff.probe(path)
    track = next((t for t in info.tracks if t.pos == track_pos), info.tracks[0])
    src = decode(path, track, folder, progress)
    lay = ff.layout_for(track.channels, track.layout)
    d = Document(rate=track.rate, channels=track.channels, folder=folder, labels=tuple(ff.labels(track.channels, lay)),
                 layout=lay, name=os.path.basename(path), path=path, info=info, track=track)
    d.markers = read_markers(path, info, track, src.frames)
    d.set_initial(src)
    fmt, opts = format_of(path, track, info)
    d.save_format, d.save_opts = fmt, opts
    return d


def read_markers(path: str, info: ff.FileInfo, track: ff.Track, frames: int) -> tuple:
    """The markers / regions of the file (0.4): a WAV's cue points, else what ffprobe sees (our tag, chapters —
    a film's chapters become regions)."""
    from . import marks
    items: list = []
    try:
        if "wav" in (info.format or "") and track.codec.startswith("pcm_"):
            items = marks.read_wav(path)
        if not items:
            items = marks.from_probe(info, track, track.rate)
    except Exception:                            # noqa: BLE001 — a broken chunk must not stop the opening
        items = []
    return marks.to_markers(items, frames)


def needed_bytes(track: ff.Track) -> int:
    """About how much disk the working file of the track takes (sound + peaks)."""
    frames = int((track.duration or 0) * track.rate)
    return int(frames * track.channels * 4 * 1.02)


# ── saving ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Format:
    key: str
    name: str
    ext: str
    options: tuple            # the choices of its option combo (keys)
    default: str
    max_channels: int = 0     # 0 = any
    rates: tuple = ()         # allowed sample rates (() = any)
    lossy: bool = False


FORMATS = {
    "wav":  Format("wav", "WAV", ".wav", ("16", "24", "32f"), "24"),
    "flac": Format("flac", "FLAC", ".flac", ("16", "24", "16:8", "24:8"), "24"),
    "mp3":  Format("mp3", "MP3", ".mp3", ("320", "256", "192", "160", "128", "v0", "v2"), "320", 2,
                   (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000), True),
    "aac":  Format("aac", "AAC (M4A)", ".m4a", ("640", "512", "384", "320", "256", "192", "160", "128"), "256",
                   8, (), True),
    "opus": Format("opus", "Opus", ".opus", ("512", "448", "320", "256", "192", "160", "128", "96", "64"), "192",
                   8, (), True),
    "ac3":  Format("ac3", "AC-3", ".ac3", ("640", "448", "384", "256", "192"), "640", 6, (32000, 44100, 48000),
                   True),
}
EXT_FORMAT = {".wav": "wav", ".flac": "flac", ".mp3": "mp3", ".m4a": "aac", ".aac": "aac", ".opus": "opus",
              ".ac3": "ac3"}


def default_option(fmt: str, channels: int, bits: int = 0) -> str:
    f = FORMATS[fmt]
    if fmt in ("wav", "flac"):
        if bits == 16:
            return "16"
        if fmt == "wav" and bits == 32:
            return "32f"
        return "24"
    if fmt == "aac" and channels > 2:
        return "640" if channels >= 6 else "384"
    if fmt == "opus" and channels > 2:
        return "448" if channels >= 6 else "256"
    return f.default


def format_of(path: str, track: ff.Track, info: ff.FileInfo) -> tuple[str, dict]:
    """The format and option Save keeps for a file opened from `path` ("" when Save must ask: a video, a DTS …)."""
    if info.has_video:
        return "", {}
    fmt = EXT_FORMAT.get(os.path.splitext(path)[1].lower(), "")
    codec_ok = {"wav": track.codec.startswith("pcm_"), "flac": track.codec == "flac", "mp3": track.codec == "mp3",
                "aac": track.codec == "aac", "opus": track.codec == "opus", "ac3": track.codec == "ac3"}
    if not fmt or not codec_ok.get(fmt):
        return "", {}
    if fmt in ("wav", "flac"):
        opt = default_option(fmt, track.channels, track.bits)
        if fmt == "wav" and track.codec in ("pcm_f32le", "pcm_f64le"):
            opt = "32f"
    else:
        kb = round(track.bitrate / 1000) if track.bitrate else 0
        f = FORMATS[fmt]
        nums = [int(o) for o in f.options if o.isdigit()]
        opt = str(min(nums, key=lambda n: abs(n - kb))) if kb else default_option(fmt, track.channels)
    return fmt, {"option": opt}


def encode_args(fmt: str, option: str, channels: int, rate: int, out_channels: int, out_rate: int) -> list[str]:
    """ffmpeg's output options (codec, quality, channels, rate) for a format."""
    a: list[str] = []
    if out_channels != channels:
        a += ["-ac", str(out_channels)]
    if out_rate != rate:
        a += ["-ar", str(out_rate)]
    dither = ["-af", "aresample=dither_method=triangular"]
    if fmt == "wav":
        codec = {"16": "pcm_s16le", "24": "pcm_s24le", "32f": "pcm_f32le"}[option if option in ("16", "24", "32f")
                                                                          else "24"]
        a += (dither if codec == "pcm_s16le" else []) + ["-c:a", codec, "-rf64", "auto"]
    elif fmt == "flac":
        bits, _, level = option.partition(":")
        a += ["-c:a", "flac", "-compression_level", level or "5"]
        a += dither + ["-sample_fmt", "s16"] if bits == "16" else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    elif fmt == "mp3":
        a += ["-c:a", "libmp3lame"]
        a += ["-q:a", option[1:]] if option.startswith("v") else ["-b:a", f"{option}k"]
    elif fmt == "aac":
        a += ["-c:a", "aac", "-b:a", f"{option}k", "-movflags", "+faststart"]
    elif fmt == "opus":
        a += ["-c:a", "libopus", "-b:a", f"{option}k"]
        if out_channels > 2:                  # Vorbis channel order: side channels go to the back pair
            lay = {3: "3.0", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(out_channels)
            a += ["-mapping_family", "1"] + (["-ch_layout", lay] if lay else [])
    elif fmt == "ac3":
        a += ["-c:a", "ac3", "-b:a", f"{option}k"]
    return a


def out_shape(fmt: str, channels: int, rate: int) -> tuple[int, int]:
    """The channels and rate the format can take (MP3: stereo at most, AC-3: 5.1 and 32 / 44.1 / 48 kHz …)."""
    f = FORMATS[fmt]
    ch = min(channels, f.max_channels) if f.max_channels else channels
    r = rate
    if f.rates and rate not in f.rates:
        r = min((x for x in f.rates if x >= rate), default=max(f.rates))
    if fmt == "opus" and rate not in (8000, 12000, 16000, 24000, 48000):
        r = 48000
    return ch, r


TAG_KEYS = ("title", "artist", "album", "album_artist", "date", "genre", "track", "comment", "composer")


def save(d: Document, path: str, fmt: str, option: str, progress: Progress | None = None,
         a: int = 0, b: int | None = None, tags: dict | None = None, markers: bool = True) -> None:
    """Write frames [a, b) of the document (all by default) to path, with its markers and regions in that range
    (marks.py). Goes through a temporary file next to the target, so the old file stays whole if anything
    fails."""
    from . import marks
    progress = progress or Progress()
    exe = ff.ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    b = d.frames if b is None else b
    out_ch, out_rate = out_shape(fmt, d.channels, d.rate)
    folder = os.path.dirname(os.path.abspath(path)) or "."
    stem, ext = os.path.splitext(os.path.basename(path))
    tmp = os.path.join(folder, f".{stem}.artcore-part{ext or FORMATS[fmt].ext}")
    meta: list[str] = []
    src_tags = dict(tags if tags is not None else (getattr(d.info, "tags", None) or {}))
    for k in TAG_KEYS:
        if src_tags.get(k):
            meta += ["-metadata", f"{k}={src_tags[k]}"]
    if fmt in ("wav", "flac"):
        meta += ["-metadata", "encoded_by=ArtCore Sound"]
    muxer = {"wav": "wav", "flac": "flac", "mp3": "mp3", "aac": "ipod", "opus": "opus", "ac3": "ac3"}[fmt]
    items = marks.items_for_save(d, a, b, out_rate) if markers else []
    meta_file = tmp + ".ffmeta"
    m_in, m_out = marks.save_args(fmt, items, out_rate, meta_file, os.path.basename(path))
    args = [exe, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "f32le", "-ar", str(d.rate),
            *ch_layout_args(d.channels, d.layout), "-i", "pipe:0", *m_in,
            *encode_args(fmt, option, d.channels, d.rate, out_ch, out_rate), *meta, *m_out, "-f", muxer, tmp]
    proc = ff.popen(args, stdin=subprocess.PIPE)
    err = _stderr_reader(proc)
    total = max(1, b - a)
    try:
        for pos, x in d.chunks(a, b):
            try:
                proc.stdin.write(x.tobytes())
            except (BrokenPipeError, OSError):
                break
            progress((pos - a + len(x)) / total * 0.98)
        proc.stdin.close()
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        _rm(tmp)
        _rm(meta_file)
        raise
    _rm(meta_file)
    if proc.returncode != 0 or not os.path.isfile(tmp):
        _rm(tmp)
        raise RuntimeError("\n".join(err[-6:]) or f"ffmpeg exit {proc.returncode}")
    if fmt == "wav" and items:
        try:
            marks.write_wav(tmp, items)
        except (OSError, ValueError):
            _rm(tmp)
            raise
    os.replace(tmp, path)
    progress(1.0)


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass
