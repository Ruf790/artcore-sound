"""Sound for films (2.0): File → Return to video. The edited sound replaces a track of the film it came from (or is
added as a new one) without touching the picture: ffmpeg copies every other stream as it is (-c copy) — the video,
the other audio tracks, the subtitles, the attachments — and encodes only the new track (AC-3, E-AC-3, AAC, FLAC,
Opus, PCM; DTS through ffmpeg's experimental DTS core encoder). Language, title, «default» and a delay are set on it;
the chapters are kept, or made from the document's markers and regions.

The result is written to a temporary file next to the target and checked with ffprobe (the number of streams of each
kind, the new track's codec, channels and length) before it takes the target's name, so a failed or cancelled run
never leaves a broken film. No Qt here: filmui.py is the window."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

import numpy as np

from . import ff, store
from .media import Cancelled, Progress, _rm, _stderr_reader, ch_layout_args


@dataclass(frozen=True)
class Codec:
    key: str
    name: str
    encoder: str
    options: tuple                 # bitrates in kbit/s, or bits for the lossless ones
    default: str
    max_channels: int = 8
    rates: tuple = ()              # allowed rates (() = any)
    lossless: bool = False
    mp4: bool = True               # can go into MP4 / MOV
    experimental: bool = False
    probe_names: tuple = ()        # what ffprobe calls the result


CODECS = {
    "ac3":  Codec("ac3", "AC-3 (Dolby Digital)", "ac3", ("640", "448", "384", "256", "192"), "640", 6,
                  (48000, 44100, 32000), probe_names=("ac3",)),
    "eac3": Codec("eac3", "E-AC-3 (Dolby Digital Plus)", "eac3", ("1536", "1024", "768", "640", "448", "384", "256"),
                  "768", 8, (48000, 44100, 32000), probe_names=("eac3",)),
    "aac":  Codec("aac", "AAC", "aac", ("640", "512", "384", "320", "256", "192", "160", "128"), "384", 8,
                  probe_names=("aac",)),
    "flac": Codec("flac", "FLAC", "flac", ("24", "16"), "24", 8, lossless=True, probe_names=("flac",)),
    "opus": Codec("opus", "Opus", "libopus", ("512", "448", "320", "256", "192", "128", "96"), "448", 8, (48000,),
                  probe_names=("opus",)),
    "pcm":  Codec("pcm", "PCM (WAV)", "pcm", ("24", "16"), "24", 8, lossless=True, mp4=False,
                  probe_names=("pcm_s24le", "pcm_s16le")),
    "dts":  Codec("dts", "DTS (core)", "dca", ("1509", "1411", "768"), "1509", 6, (48000, 44100, 32000),
                  experimental=True, probe_names=("dts",)),
}
ORDER = ("ac3", "eac3", "aac", "flac", "opus", "pcm", "dts")
VIDEO_OK = (".mkv", ".mka", ".mp4", ".m4v", ".mov", ".webm")
MP4_LIKE = (".mp4", ".m4v", ".mov")
LANGS = ("rus", "eng", "ukr", "bul", "deu", "fra", "spa", "ita", "jpn", "zho", "und")


def codecs_for(path: str) -> list[str]:
    """The codecs the container can take (WebM: Opus only; MP4 / MOV: no PCM)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".webm":
        return ["opus"]
    return [k for k in ORDER if CODECS[k].mp4 or ext not in MP4_LIKE]


def dts_available() -> bool:
    return "dca" in (ff.caps().get("encoders") or ())


@dataclass
class Options:
    mode: str = "replace"            # replace | add
    replace_index: int = -1          # the stream index of the track to replace
    codec: str = "ac3"
    option: str = ""                 # bitrate (kbit/s) or bits; "" = the codec's default
    lang: str = ""
    title: str = ""
    default: bool = True
    delay_ms: float = 0.0            # > 0: the sound starts later (silence before it), < 0: its start is cut
    chapters: str = "keep"           # keep | markers | none
    out_path: str = ""


@dataclass
class Result:
    path: str
    streams: dict = field(default_factory=dict)       # kind → count in the result
    track: int = 0                                    # the new track's position among the audio tracks
    codec: str = ""
    channels: int = 0
    duration: float = 0.0


def default_out(path: str) -> str:
    """«Film (ArtCore).mkv» next to the film, a free name."""
    stem, ext = os.path.splitext(path)
    p = f"{stem} (ArtCore){ext}"
    n = 2
    while os.path.exists(p):
        p = f"{stem} (ArtCore {n}){ext}"
        n += 1
    return p


def part_path(path: str) -> str:
    folder = os.path.dirname(os.path.abspath(path)) or "."
    stem, ext = os.path.splitext(os.path.basename(path))
    return os.path.join(folder, f".{stem}.artcore-part{ext}")


def out_shape(codec: str, channels: int, rate: int) -> tuple[int, int, str]:
    """(channels, rate, layout) the codec takes: AC-3 / DTS 5.1 at most, 32 / 44.1 / 48 kHz …"""
    c = CODECS[codec]
    ch = min(channels, c.max_channels)
    r = rate
    if c.rates and rate not in c.rates:
        r = min((x for x in c.rates if x >= rate), default=max(c.rates))
    lay = ff.layout_for(ch)
    if codec == "dts" and ch in (5, 6):
        lay = "5.1(side)" if ch == 6 else "5.0(side)"
    return ch, r, lay


def _stream_kinds(streams) -> list[tuple[int, str]]:
    """(index, kind) of the streams that are copied: video, audio, subtitles, attachments (data streams — timecodes
    and the like — are left out: most muxers refuse them)."""
    return [(i, k) for i, k, _c in streams if k in ("video", "audio", "subtitle", "attachment")]


def plan(info: ff.FileInfo, d_channels: int, d_rate: int, d_layout: str, opts: Options, meta_path: str = "",
         tmp: str = "out.mkv") -> tuple[list[str], int, dict]:
    """The ffmpeg arguments after «ffmpeg» (input 0 the film, input 1 the f32le pipe, input 2 the chapters' file),
    the new track's position among the output audio tracks, and the expected count of each kind of stream."""
    c = CODECS[opts.codec]
    ch, rate, lay = out_shape(opts.codec, d_channels, d_rate)
    kinds = _stream_kinds(info.streams)
    if opts.mode == "replace" and not any(i == opts.replace_index and k == "audio" for i, k in kinds):
        raise ValueError("no such audio track")
    maps: list[str] = []
    new_pos = -1
    apos = 0
    last_audio = max((n for n, (_i, k) in enumerate(kinds) if k == "audio"), default=-1)
    for n, (i, k) in enumerate(kinds):
        if k == "audio" and opts.mode == "replace" and i == opts.replace_index:
            maps += ["-map", "1:a:0"]
            new_pos = apos
        else:
            maps += ["-map", f"0:{i}"]
        if k == "audio":
            apos += 1
        if opts.mode == "add" and n == last_audio:
            maps += ["-map", "1:a:0"]
            new_pos = apos
            apos += 1
    if new_pos < 0:                                    # adding to a film without sound
        maps += ["-map", "1:a:0"]
        new_pos = apos
        apos += 1
    expect = {"video": 0, "audio": apos, "subtitle": 0, "attachment": 0}
    for _i, k in kinds:
        if k != "audio":
            expect[k] += 1
    args = ["-nostdin", "-hide_banner", "-v", "error", "-y", "-i", info.path,
            "-f", "f32le", "-ar", str(d_rate), *ch_layout_args(d_channels, d_layout), "-i", "pipe:0"]
    if opts.chapters == "markers" and meta_path:
        args += ["-f", "ffmetadata", "-i", meta_path]
    args += maps
    args += ["-map_metadata", "0", "-map_chapters", "2" if opts.chapters == "markers" and meta_path
             else ("0" if opts.chapters == "keep" else "-1")]
    args += ["-c", "copy"]
    k = new_pos
    opt = opts.option or c.default
    if opts.codec == "pcm":
        enc = "pcm_s16le" if opt == "16" else "pcm_s24le"
        args += [f"-c:a:{k}", enc]
    elif opts.codec == "flac":
        args += [f"-c:a:{k}", "flac", f"-sample_fmt:a:{k}", "s16" if opt == "16" else "s32"]
        if opt != "16":
            args += [f"-bits_per_raw_sample:a:{k}", "24"]
    else:
        args += [f"-c:a:{k}", c.encoder, f"-b:a:{k}", f"{opt}k"]
        if opts.codec == "opus" and ch > 2:
            args += [f"-mapping_family:a:{k}", "1"]
    if ch != d_channels or lay != ff.layout_for(d_channels, d_layout):
        args += [f"-filter:a:{k}", f"aresample=ochl={lay}" if lay else f"pan={ch}c"]
    if rate != d_rate:
        args += [f"-ar:a:{k}", str(rate)]
    if c.experimental:
        args += ["-strict", "-2"]
    if opts.lang:
        args += [f"-metadata:s:a:{k}", f"language={opts.lang}"]
    args += [f"-metadata:s:a:{k}", f"title={opts.title}"]
    if opts.default:
        for j in range(apos):
            args += [f"-disposition:a:{j}", "default" if j == k else "0"]
    else:
        args += [f"-disposition:a:{k}", "0"]
    if os.path.splitext(tmp)[1].lower() in MP4_LIKE:
        args += ["-movflags", "+faststart"]
    args += [tmp]
    return args, new_pos, expect


def _chapters_file(d, path: str, lead: int = 0) -> bool:
    from .marks import _ffmeta_esc, items_for_save
    items = items_for_save(d)
    if not items:
        return False
    lines = [";FFMETADATA1"]
    for name, p, e in sorted(items, key=lambda x: x[1]):            # they move with the sound's delay
        p = max(0, p + lead)
        e = p if e is None else max(p, e + lead)
        lines += ["[CHAPTER]", f"TIMEBASE=1/{d.rate}", f"START={p}", f"END={e}", f"title={_ffmeta_esc(name)}"]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return True


def probe_streams(path: str) -> list[dict]:
    r = ff.run([ff.ffprobe(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], 60)
    if r.returncode != 0:
        raise ValueError((r.stderr or "ffprobe").strip())
    js = json.loads(r.stdout or "{}")
    fmt = js.get("format") or {}
    out = js.get("streams") or []
    for s in out:
        s["_format_duration"] = float(fmt.get("duration") or 0)
    return out


def return_to_video(d, opts: Options, progress: Progress | None = None, info: ff.FileInfo | None = None) -> Result:
    """Write the document's sound into the film d.film["path"] as opts says → opts.out_path (checked)."""
    progress = progress or Progress()
    src_path = d.film.get("path") or ""
    if not src_path or not os.path.isfile(src_path):
        raise FileNotFoundError(src_path or "film")
    info = info or ff.probe(src_path)
    out = opts.out_path or default_out(src_path)
    tmp = part_path(out)
    meta = tmp + ".ffmeta"
    has_meta = opts.chapters == "markers" and _chapters_file(d, meta, int(round(opts.delay_ms * d.rate / 1000.0)))
    if opts.chapters == "markers" and not has_meta:
        opts = Options(**{**opts.__dict__, "chapters": "none"})
    args, pos, expect = plan(info, d.channels, d.rate, d.layout, opts, meta if has_meta else "", tmp)
    proc = ff.popen([ff.ffmpeg(), *args], stdin=subprocess.PIPE)
    err = _stderr_reader(proc)
    lead = int(round(opts.delay_ms * d.rate / 1000.0))
    start = max(0, -lead)
    total = max(1, d.frames - start + max(0, lead))
    done = 0
    try:
        try:
            if lead > 0:
                z = np.zeros((min(lead, store.CHUNK), d.channels), np.float32)
                left = lead
                while left > 0:
                    k = min(left, len(z))
                    proc.stdin.write(z[:k].tobytes())
                    left -= k
                    done += k
            for _p, x in d.chunks(start, d.frames):
                proc.stdin.write(np.ascontiguousarray(x, np.float32).tobytes())
                done += len(x)
                progress(done / total * 0.97)
        except (BrokenPipeError, OSError):
            pass
        proc.stdin.close()
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        _rm(tmp)
        _rm(meta)
        raise
    _rm(meta)
    if proc.returncode != 0 or not os.path.isfile(tmp):
        _rm(tmp)
        raise RuntimeError("\n".join(err[-6:]) or f"ffmpeg exit {proc.returncode}")
    try:
        res = verify(tmp, expect, pos, opts.codec, out_shape(opts.codec, d.channels, d.rate)[0],
                     total / d.rate)
    except Exception:
        _rm(tmp)
        raise
    os.replace(tmp, out)
    res.path = out
    progress(1.0)
    return res


def verify(path: str, expect: dict, pos: int, codec: str, channels: int, seconds: float) -> Result:
    """ffprobe the result: as many streams of each kind as planned, the new track's codec, channels and length."""
    streams = probe_streams(path)
    got = {"video": 0, "audio": 0, "subtitle": 0, "attachment": 0}
    audio = []
    for s in streams:
        k = s.get("codec_type")
        if k == "video" and (s.get("disposition") or {}).get("attached_pic") and expect.get("video", 0) <= got["video"]:
            got["attachment"] += 1                 # a cover picture: MP4 / MKV may report it as video
            continue
        if k in got:
            got[k] += 1
        if k == "audio":
            audio.append(s)
    for k, n in expect.items():
        if got.get(k, 0) != n:
            raise RuntimeError(f"check: {k} streams {got.get(k, 0)}, expected {n}")
    a = audio[pos]
    names = CODECS[codec].probe_names
    if a.get("codec_name") not in names:
        raise RuntimeError(f"check: the new track is {a.get('codec_name')}, expected {names[0]}")
    if int(a.get("channels") or 0) != channels:
        raise RuntimeError(f"check: the new track has {a.get('channels')} channels, expected {channels}")
    dur = 0.0
    for key in ("duration",):
        try:
            dur = float(a.get(key) or 0)
        except ValueError:
            dur = 0.0
    if not dur:
        tags = {k.lower(): v for k, v in (a.get("tags") or {}).items()}
        dur = ff.parse_hms(tags.get("duration", "")) if tags.get("duration") else a.get("_format_duration", 0.0)
    if dur and abs(dur - seconds) > max(0.6, seconds * 0.002):
        raise RuntimeError(f"check: the new track lasts {dur:.2f} s, expected {seconds:.2f} s")
    return Result(path=path, streams=got, track=pos, codec=a.get("codec_name", ""), channels=channels, duration=dur)


def track_label(t) -> str:
    """«#2 · AC-3 5.1 · rus · Дубляж» for the track combo."""
    from .i18n import t as tr
    parts = [f"#{t.pos + 1}", ff.codec_name(t), f"{t.channels} {tr('ch_short')}"]
    if t.lang:
        parts.append(t.lang)
    if t.title:
        parts.append(t.title)
    return " · ".join(parts)


def guess_lang(text: str) -> str:
    s = (text or "").lower()
    if re.search(r"[а-яё]", s) or "rus" in s:
        return "rus"
    return ""
