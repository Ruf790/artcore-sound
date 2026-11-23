"""Where ffmpeg / ffprobe are (ARTCORE_FFMPEG_DIR for tests, ffmpeg\\ next to the exe, PATH) and what ffprobe
says about a file's sound."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from . import config
from .winint import IS_WIN, popen_flags

EXE = ".exe" if IS_WIN else ""


def _dirs() -> list[str]:
    out = []
    env = os.environ.get("ARTCORE_FFMPEG_DIR")
    if env:
        out.append(env)
    base = config.app_dir()
    out += [os.path.join(base, "ffmpeg", "bin"), os.path.join(base, "ffmpeg"), base]
    return out


def find(name: str) -> str:
    for d in _dirs():
        p = os.path.join(d, name + EXE)
        if os.path.isfile(p):
            return p
    return shutil.which(name) or ""


def ffmpeg() -> str:
    return find("ffmpeg")


def ffprobe() -> str:
    return find("ffprobe")


def run(args: list[str], timeout: float = 60, binary: bool = False):
    return subprocess.run(args, capture_output=True, timeout=timeout, text=not binary,
                          encoding=None if binary else "utf-8", errors=None if binary else "replace",
                          stdin=subprocess.DEVNULL, **popen_flags())


def popen(args: list[str], stdin=None, stdout=None) -> subprocess.Popen:
    return subprocess.Popen(args, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                            stdout=stdout if stdout is not None else subprocess.DEVNULL, stderr=subprocess.PIPE,
                            bufsize=0, **popen_flags())


_version: str | None = None


def version() -> str:
    global _version
    if _version is None:
        _version = ""
        exe = ffmpeg()
        if exe:
            try:
                first = run([exe, "-hide_banner", "-version"], 15).stdout.splitlines()[0]
                m = re.match(r"ffmpeg version (\S+)", first)
                _version = m.group(1) if m else first[:40]
            except Exception:
                pass
    return _version


_caps: dict | None = None


def caps() -> dict:
    """{"encoders": set, "filters": set} of this ffmpeg (cached)."""
    global _caps
    if _caps is None:
        enc, flt = set(), set()
        exe = ffmpeg()
        if exe:
            try:
                for ln in run([exe, "-hide_banner", "-encoders"], 30).stdout.splitlines():
                    parts = ln.split()
                    if len(parts) > 1 and len(parts[0]) == 6 and parts[0][0] == "A":
                        enc.add(parts[1])
                for ln in run([exe, "-hide_banner", "-filters"], 30).stdout.splitlines():
                    parts = ln.split()
                    if len(parts) > 2 and ln.startswith(" "):
                        flt.add(parts[1])
            except Exception:
                pass
        _caps = {"encoders": enc, "filters": flt}
    return _caps


# ── channel names ─────────────────────────────────────────────
LAYOUT_LABELS = {
    "mono": ["M"], "stereo": ["L", "R"], "2.1": ["L", "R", "LFE"], "3.0": ["L", "R", "C"],
    "3.0(back)": ["L", "R", "S"], "4.0": ["L", "R", "C", "S"], "quad": ["L", "R", "Ls", "Rs"],
    "quad(side)": ["L", "R", "Ls", "Rs"], "3.1": ["L", "R", "C", "LFE"], "5.0": ["L", "R", "C", "Ls", "Rs"],
    "5.0(side)": ["L", "R", "C", "Ls", "Rs"], "4.1": ["L", "R", "C", "LFE", "S"],
    "5.1": ["L", "R", "C", "LFE", "Ls", "Rs"], "5.1(side)": ["L", "R", "C", "LFE", "Ls", "Rs"],
    "6.0": ["L", "R", "C", "Cs", "Ls", "Rs"], "6.1": ["L", "R", "C", "LFE", "Cs", "Ls", "Rs"],
    "7.0": ["L", "R", "C", "Lb", "Rb", "Ls", "Rs"], "7.1": ["L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"],
    "7.1(wide)": ["L", "R", "C", "LFE", "Lb", "Rb", "Lw", "Rw"],
}
DEFAULT_LAYOUT = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def labels(channels: int, layout: str = "") -> list[str]:
    lab = LAYOUT_LABELS.get(layout or DEFAULT_LAYOUT.get(channels, ""))
    if lab and len(lab) == channels:
        return list(lab)
    return [str(i + 1) for i in range(channels)]


def layout_for(channels: int, layout: str = "") -> str:
    """A layout name ffmpeg knows for this many channels (the file's own if it fits)."""
    if layout and len(LAYOUT_LABELS.get(layout, ())) == channels:
        return layout
    return DEFAULT_LAYOUT.get(channels, "")


# ── ffprobe ───────────────────────────────────────────────────
@dataclass
class Track:
    index: int              # the stream's index in the file
    pos: int                # 0-based among the audio streams (-map 0:a:pos)
    codec: str = ""
    codec_long: str = ""
    profile: str = ""
    rate: int = 0
    channels: int = 0
    layout: str = ""
    bits: int = 0           # bits per sample of the source (0 = lossy / unknown)
    bitrate: int = 0
    duration: float = 0.0
    lang: str = ""
    title: str = ""
    default: bool = False
    tags: dict = field(default_factory=dict)


@dataclass
class FileInfo:
    path: str
    format: str = ""
    format_long: str = ""
    duration: float = 0.0
    size: int = 0
    tracks: list = field(default_factory=list)
    has_video: bool = False
    tags: dict = field(default_factory=dict)
    chapters: list = field(default_factory=list)     # (start s, end s, title)


def _bits(s: dict) -> int:
    for k in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            v = int(s.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    fmt = s.get("sample_fmt", "")
    if s.get("codec_name", "").startswith("pcm_") or s.get("codec_name") in ("flac", "alac", "wavpack", "tta"):
        return {"u8": 8, "s16": 16, "s16p": 16, "s32": 32, "s32p": 32, "flt": 32, "fltp": 32, "dbl": 64}.get(fmt, 0)
    return 0


def probe(path: str) -> FileInfo:
    exe = ffprobe()
    if not exe:
        raise FileNotFoundError("ffprobe")
    r = run([exe, "-v", "error", "-hide_banner", "-print_format", "json", "-show_format", "-show_streams",
             "-show_chapters", path], 60)
    if r.returncode != 0:
        raise ValueError((r.stderr or "ffprobe failed").strip().splitlines()[-1] if r.stderr.strip() else "ffprobe failed")
    js = json.loads(r.stdout or "{}")
    fmt = js.get("format", {})
    info = FileInfo(path=path, format=fmt.get("format_name", ""), format_long=fmt.get("format_long_name", ""))
    try:
        info.duration = float(fmt.get("duration") or 0)
    except ValueError:
        pass
    try:
        info.size = int(fmt.get("size") or os.path.getsize(path))
    except (ValueError, OSError):
        pass
    info.tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for ch in js.get("chapters") or []:
        try:
            s0 = float(ch.get("start_time") or 0)
            s1 = float(ch.get("end_time")) if ch.get("end_time") is not None else None
        except (TypeError, ValueError):
            continue
        info.chapters.append((s0, s1, ((ch.get("tags") or {}).get("title") or "").strip()))
    pos = 0
    for s in js.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            info.has_video = True
        if kind != "audio":
            continue
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        t = Track(index=int(s.get("index", 0)), pos=pos, codec=s.get("codec_name", ""),
                  codec_long=s.get("codec_long_name", ""), profile=s.get("profile", "") or "",
                  rate=int(s.get("sample_rate") or 0), channels=int(s.get("channels") or 0),
                  layout=s.get("channel_layout", "") or "", bits=_bits(s), lang=tags.get("language", ""),
                  title=tags.get("title", ""), default=bool((s.get("disposition") or {}).get("default")),
                  tags=tags)
        try:
            t.bitrate = int(s.get("bit_rate") or 0)
        except ValueError:
            pass
        try:
            t.duration = float(s.get("duration") or 0) or info.duration
        except ValueError:
            t.duration = info.duration
        if not t.duration and "duration" in tags:
            t.duration = parse_hms(tags["duration"])
        if t.channels and t.rate:
            info.tracks.append(t)
        pos += 1
    if not info.tracks:
        raise ValueError("no audio")
    return info


def parse_hms(s: str) -> float:
    """«01:02:03.5» → 3723.5 (0 when it is not a time)."""
    try:
        parts = [float(x) for x in s.strip().split(":")]
    except ValueError:
        return 0.0
    v = 0.0
    for x in parts:
        v = v * 60 + x
    return v


CODEC_NAMES = {"pcm_s16le": "PCM", "pcm_s24le": "PCM", "pcm_s32le": "PCM", "pcm_f32le": "PCM float",
               "pcm_f64le": "PCM float", "pcm_u8": "PCM", "pcm_s16be": "PCM", "pcm_s24be": "PCM",
               "flac": "FLAC", "mp3": "MP3", "aac": "AAC", "ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS",
               "truehd": "TrueHD", "opus": "Opus", "vorbis": "Vorbis", "wmav2": "WMA", "wmav1": "WMA",
               "wmapro": "WMA Pro", "wmalossless": "WMA Lossless", "alac": "ALAC", "mp2": "MP2", "ape": "APE",
               "wavpack": "WavPack", "tta": "TTA"}


def codec_name(t: Track) -> str:
    name = CODEC_NAMES.get(t.codec, t.codec.upper())
    if t.codec == "dts" and t.profile and t.profile != "DTS":
        name = t.profile
    return name
