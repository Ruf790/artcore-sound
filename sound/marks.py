"""Markers and regions in files and lists (0.4).

WAV: the «cue » chunk with a LIST/adtl list after the sound, as Sound Forge writes them — a cue point per marker or
region, «labl» with its name and, for a region, «ltxt» with its length (purpose «rgn »). An RF64 file (over 4 GB)
gets its sizes in the ds64 chunk updated. Reading takes cue points from any software: the sample offset, else the
position; names in UTF-8, else Windows-1251.

Other formats, through ffmpeg: MP3, M4A and MKA real chapters (an ffmetadata input: a region is a chapter, a marker
a chapter of no length); FLAC and Opus the Vorbis comments CHAPTERxxx / CHAPTERxxxNAME (other players read them as
chapters) and in FLAC a CUESHEET tag too. As chapters do not keep everything (M4A and Opus make them follow on one
another), every format but WAV and AC-3 also gets the tag ARTCORE_MARKERS with the exact positions in samples, read
first when the file is opened again. Chapters of a film (MKV, MP4) open as regions.

Lists: CSV (written and read back with samples, also read with times only), .cue sheets (regions — or markers —
as tracks, INDEX 01 at 75 frames a second) and Audacity's label files (start, end and name in seconds)."""
from __future__ import annotations

import csv
import io
import math
import os
import re
import struct

from .doc import Marker, fmt_time, parse_time, sort_markers

Item = tuple          # (name, pos, end or None) in frames
TAG = "ARTCORE_MARKERS"
CUE_FPS = 75


# ── which markers go into a saved file ─────────────────────────
def items_for_save(d, a: int = 0, b: int | None = None, out_rate: int | None = None) -> list[Item]:
    """The markers of [a, b) of the document as (name, pos, end) in the frames of a file starting at a and at
    out_rate (a region that sticks out is cut to the range)."""
    b = d.frames if b is None else b
    k = (out_rate or d.rate) / d.rate
    out = []
    for m in d.markers:
        if m.end is None:
            if a <= m.pos <= b:
                out.append((m.name, int(round((m.pos - a) * k)), None))
        else:
            s, e = max(a, m.pos), min(b, m.end)
            if e > s:
                out.append((m.name, int(round((s - a) * k)), int(round((e - a) * k))))
    return out


def to_markers(items, frames: int | None = None) -> tuple:
    """(name, pos, end) → Marker (new ids); positions past `frames` are dropped / cut."""
    from .doc import new_marker
    out = []
    for name, p, e in items:
        p = int(p)
        if p < 0 or (frames is not None and p > frames):
            continue
        if e is not None:
            e = int(e) if frames is None else min(int(e), frames)
            if e <= p:
                e = None
        out.append(new_marker(name, p, e))
    return sort_markers(out)


# ── WAV: cue + LIST/adtl ──────────────────────────────────────
def _chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def wav_chunks(items: list[Item]) -> bytes:
    """The «cue » and «LIST adtl» chunks for these markers / regions (cue ids from 1)."""
    if not items:
        return b""
    cue = struct.pack("<I", len(items))
    adtl = b"adtl"
    for i, (name, pos, end) in enumerate(items, 1):
        pos = max(0, min(0xFFFFFFFF, int(pos)))
        cue += struct.pack("<II4sIII", i, pos, b"data", 0, 0, pos)
        adtl += _chunk(b"labl", struct.pack("<I", i) + name.encode("utf-8") + b"\0")
        if end is not None:
            ln = max(0, min(0xFFFFFFFF, int(end) - pos))
            adtl += _chunk(b"ltxt", struct.pack("<II4sHHHH", i, ln, b"rgn ", 0, 0, 0, 0))
    return _chunk(b"cue ", cue) + _chunk(b"LIST", adtl)


def _walk(f, start: int, end: int):
    """(id, data offset, size) of the chunks in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        yield cid, pos + 8, size
        pos += 8 + size + (size & 1)


def write_wav(path: str, items: list[Item]) -> None:
    """Add the chunks of `items` at the end of a finished WAV (RIFF or RF64) and fix its sizes."""
    extra = wav_chunks(items)
    if not extra:
        return
    with open(path, "r+b") as f:
        head = f.read(12)
        if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64"):
            raise ValueError("not a WAV file")
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size & 1:                               # a last chunk of odd size without its pad byte
            f.write(b"\0")
            size += 1
        f.write(extra)
        total = size + len(extra)
        if head[:4] == b"RIFF" and total - 8 <= 0xFFFFFFFF:
            f.seek(4)
            f.write(struct.pack("<I", total - 8))
            return
        if head[:4] == b"RIFF":
            raise ValueError("the file has grown past 4 GB")
        for cid, off, _sz in _walk(f, 12, size):   # RF64: the riff size in ds64 (the first 8 bytes)
            if cid == b"ds64":
                f.seek(off)
                f.write(struct.pack("<Q", total - 8))
                return
        raise ValueError("RF64 without ds64")


def _text(b: bytes) -> str:
    b = b.split(b"\0", 1)[0]
    for enc in ("utf-8", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


def read_wav(path: str) -> list[Item]:
    """The markers / regions of a WAV (Sound Forge, Audacity, Wavosaur, Reaper …): [] when it has none."""
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        head = f.read(12)
        if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64", b"BW64"):
            return []
        f.seek(0, os.SEEK_END)
        end = f.tell()
        cues: dict[int, int] = {}
        order: list[int] = []
        names: dict[int, str] = {}
        lengths: dict[int, int] = {}
        data_size = None
        for cid, off, size in _walk(f, 12, end):
            if cid == b"ds64" and size >= 24:
                f.seek(off)
                data_size = struct.unpack("<QQQ", f.read(24))[1]
            if cid == b"data" and size == 0xFFFFFFFF and data_size is not None:
                # RF64: the data chunk's real size is in ds64 — jump over it by that
                yield_rest = list(_walk(f, off + data_size + (data_size & 1), end))
                for c2, o2, s2 in yield_rest:
                    _read_chunk(f, c2, o2, s2, cues, order, names, lengths)
                break
            _read_chunk(f, cid, off, size, cues, order, names, lengths)
    out = []
    for i in order:
        p = cues[i]
        ln = lengths.get(i, 0)
        out.append((names.get(i, ""), p, p + ln if ln > 0 else None))
    return out


def _read_chunk(f, cid, off, size, cues, order, names, lengths) -> None:
    if cid == b"cue ":
        f.seek(off)
        data = f.read(size)
        if len(data) < 4:
            return
        n = min(struct.unpack("<I", data[:4])[0], (len(data) - 4) // 24)
        for k in range(n):
            ident, position, _fcc, _cs, _bs, offset = struct.unpack("<II4sIII", data[4 + 24 * k:28 + 24 * k])
            if ident not in cues:
                order.append(ident)
            cues[ident] = offset if offset or not position else position
    elif cid == b"LIST":
        f.seek(off)
        data = f.read(size)
        if data[:4] != b"adtl":
            return
        pos = 4
        while pos + 8 <= len(data):
            sid, ss = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
            body = data[pos + 8:pos + 8 + ss]
            if len(body) >= 4:
                ident = struct.unpack("<I", body[:4])[0]
                if sid == b"labl":
                    names[ident] = _text(body[4:])
                elif sid == b"note" and ident not in names:
                    names[ident] = _text(body[4:])
                elif sid == b"ltxt" and len(body) >= 8:
                    lengths[ident] = struct.unpack("<I", body[4:8])[0]
                    if len(body) > 20 and ident not in names:
                        txt = _text(body[20:])
                        if txt:
                            names[ident] = txt
            pos += 8 + ss + (ss & 1)


# ── the tag every other format carries ─────────────────────────
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "")


def _unesc(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n"}.get(m.group(1), m.group(1)), s)


def tag_text(items: list[Item], rate: int) -> str:
    lines = [f"ArtCore markers 1 rate={int(rate)}"]
    for name, p, e in items:
        lines.append(f"R\t{p}\t{e}\t{_esc(name)}" if e is not None else f"M\t{p}\t\t{_esc(name)}")
    return "\n".join(lines)


def parse_tag(text: str, rate: int) -> list[Item] | None:
    lines = (text or "").replace("\r", "").split("\n")
    m = re.match(r"ArtCore markers 1 rate=(\d+)", lines[0] if lines else "")
    if not m:
        return None
    k = rate / max(1, int(m.group(1)))
    out = []
    for ln in lines[1:]:
        parts = ln.split("\t", 3)
        if len(parts) != 4 or parts[0] not in ("M", "R"):
            continue
        try:
            p = int(round(int(parts[1]) * k))
            e = int(round(int(parts[2]) * k)) if parts[0] == "R" else None
        except ValueError:
            continue
        out.append((_unesc(parts[3]), p, e))
    return out


def _hms(sec: float) -> str:
    h, rem = divmod(max(0.0, sec), 3600)
    mi, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(mi):02d}:{s:06.3f}"


def cue_time(sec: float) -> str:
    fr = int(round(max(0.0, sec) * CUE_FPS))
    m, rem = divmod(fr, 60 * CUE_FPS)
    s, f = divmod(rem, CUE_FPS)
    return f"{m:02d}:{s:02d}:{f:02d}"


def _ffmeta_esc(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def save_args(fmt: str, items: list[Item], rate: int, meta_path: str, file_name: str = "") \
        -> tuple[list[str], list[str]]:
    """(extra inputs, output options) that put the markers into a file of this format written by ffmpeg (from
    the pipe as input 0). The ffmetadata file for chapters is written to meta_path when needed."""
    if not items or fmt in ("wav", "ac3"):
        return [], []
    inputs: list[str] = []
    out: list[str] = ["-metadata", f"{TAG}={tag_text(items, rate)}"]
    if fmt in ("mp3", "aac"):
        lines = [";FFMETADATA1"]
        for name, p, e in sorted(items, key=lambda x: x[1]):
            lines += ["[CHAPTER]", f"TIMEBASE=1/{rate}", f"START={p}", f"END={e if e is not None else p}",
                      f"title={_ffmeta_esc(name)}"]
        with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        inputs = ["-f", "ffmetadata", "-i", meta_path]
        out = ["-map", "0:a", "-map_chapters", "1", "-map_metadata", "1"] + out
        if fmt == "aac":
            out += ["-movflags", "+use_metadata_tags"]
    elif fmt in ("flac", "opus"):
        for i, (name, p, _e) in enumerate(sorted(items, key=lambda x: x[1]), 1):
            out += ["-metadata", f"CHAPTER{i:03d}={_hms(p / rate)}", "-metadata", f"CHAPTER{i:03d}NAME={name}"]
        if fmt == "flac":
            out += ["-metadata", "CUESHEET=" + cue_sheet(items, rate, file_name or "audio.flac")]
    return inputs, out


def from_probe(info, track, rate: int) -> list[Item]:
    """The markers ffprobe found in a file: our tag, else its chapters (a chapter of no length is a marker), else
    a CUESHEET tag."""
    tags = {**(getattr(info, "tags", None) or {}), **(getattr(track, "tags", None) or {})}
    txt = tags.get(TAG.lower())
    if txt:
        got = parse_tag(txt, rate)
        if got is not None:
            return got
    chaps = getattr(info, "chapters", None) or []
    if chaps:
        out = []
        for i, (s, e, title) in enumerate(chaps, 1):
            p = int(round(s * rate))
            q = int(round(e * rate)) if e is not None else None
            out.append((title or f"{i}", p, q if q is not None and q > p else None))
        return out
    cs = tags.get("cuesheet")
    if cs:
        return parse_cue(cs, rate, None)
    return []


# ── lists: CSV, .cue, Audacity labels ─────────────────────────
CSV_HEAD = ("Type", "Name", "Start", "End", "Length", "Start sample", "End sample")


def to_csv(markers, rate: int) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_HEAD)
    for m in markers:
        reg = m.end is not None
        w.writerow(["Region" if reg else "Marker", m.name, fmt_time(m.pos, rate),
                    fmt_time(m.end, rate) if reg else "", fmt_time(m.length, rate) if reg else "", m.pos,
                    m.end if reg else ""])
    return buf.getvalue()


def parse_csv(text: str, rate: int) -> list[Item]:
    """Our CSV (samples first, times otherwise); also «name;start;end» with ; or , or tabs and no header."""
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    out = []
    head = [c.strip().lower() for c in rows[0]] if rows else []
    if "name" in head and "start" in head:
        ix = {k: head.index(k) for k in ("type", "name", "start", "end", "start sample", "end sample") if k in head}
        for r in rows[1:]:
            def cell(k):
                i = ix.get(k)
                return r[i].strip() if i is not None and i < len(r) else ""
            p = _num(cell("start sample"))
            if p is None:
                p = parse_time(cell("start"), rate)
            if p is None:
                continue
            e = _num(cell("end sample"))
            if e is None and cell("end"):
                e = parse_time(cell("end"), rate)
            if cell("type").lower().startswith("m"):
                e = None
            out.append((cell("name"), p, e if e is not None and e > p else None))
        return out
    for r in rows:
        if len(r) < 2:
            continue
        p = parse_time(r[1], rate)
        if p is None:
            continue
        e = parse_time(r[2], rate) if len(r) > 2 and r[2].strip() else None
        out.append((r[0].strip(), p, e if e is not None and e > p else None))
    return out


def _num(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def cue_sheet(items, rate: int, file_name: str, tracks_from: str = "auto") -> str:
    """A .cue: the regions as tracks (or, without regions, the markers); INDEX 01 at each start."""
    items = [(m.name, m.pos, m.end) if isinstance(m, Marker) else tuple(m) for m in items]
    regions = [x for x in items if x[2] is not None]
    use = regions if regions and tracks_from in ("auto", "regions") else [x for x in items if x[2] is None] or items
    use = sorted(use, key=lambda x: x[1])
    ext = os.path.splitext(file_name)[1].lower()
    kind = {".mp3": "MP3", ".aif": "AIFF", ".aiff": "AIFF"}.get(ext, "WAVE")
    lines = ['REM COMMENT "ArtCore Sound"', f'FILE "{os.path.basename(file_name) or "audio.wav"}" {kind}']
    for i, (name, p, _e) in enumerate(use, 1):
        title = name.replace('"', "'")
        lines += [f"  TRACK {i:02d} AUDIO", f'    TITLE "{title}"', f"    INDEX 01 {cue_time(p / rate)}"]
    return "\r\n".join(lines) + "\r\n"


def parse_cue(text: str, rate: int, frames: int | None) -> list[Item]:
    """The tracks of a .cue as regions: from INDEX 01 to the next track's start (the last to the end)."""
    tracks: list[list] = []
    for ln in text.replace("\r", "").split("\n"):
        s = ln.strip()
        m = re.match(r"TRACK\s+(\d+)", s, re.I)
        if m:
            tracks.append([f"{int(m.group(1)):02d}", None])
            continue
        if not tracks:
            continue
        m = re.match(r'TITLE\s+"?(.*?)"?\s*$', s, re.I)
        if m:
            tracks[-1][0] = m.group(1)
            continue
        m = re.match(r"INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", s, re.I)
        if m and (int(m.group(1)) == 1 or tracks[-1][1] is None):
            mm, ss, ff = int(m.group(2)), int(m.group(3)), int(m.group(4))
            tracks[-1][1] = int(round((mm * 60 + ss + ff / CUE_FPS) * rate))
    tracks = [x for x in tracks if x[1] is not None]
    out = []
    for i, (name, p) in enumerate(tracks):
        e = tracks[i + 1][1] if i + 1 < len(tracks) else frames
        out.append((name, p, e if e is not None and e > p else None))
    return out


def parse_labels(text: str, rate: int) -> list[Item]:
    """Audacity's label track export: start \\t end \\t name, in seconds."""
    out = []
    for ln in text.replace("\r", "").split("\n"):
        parts = ln.split("\t")
        if len(parts) < 2 or parts[0].startswith("\\"):
            continue
        try:
            s, e = float(parts[0].replace(",", ".")), float(parts[1].replace(",", "."))
        except ValueError:
            continue
        if not (math.isfinite(s) and math.isfinite(e)):
            continue
        p, q = int(round(s * rate)), int(round(e * rate))
        out.append((parts[2] if len(parts) > 2 else "", p, q if q > p else None))
    return out


def import_file(path: str, rate: int, frames: int) -> list[Item]:
    with open(path, "rb") as f:
        raw = f.read()
    text = None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    text = text if text is not None else raw.decode("latin-1")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".cue":
        return parse_cue(text, rate, frames)
    if ext == ".txt":
        got = parse_labels(text, rate)
        if got:
            return got
    return parse_csv(text, rate)


def export_file(path: str, markers, rate: int, audio_name: str = "") -> None:
    ext = os.path.splitext(path)[1].lower()
    data = cue_sheet(markers, rate, audio_name) if ext == ".cue" else to_csv(markers, rate)
    with open(path, "w", encoding="utf-8-sig" if ext == ".csv" else "utf-8", newline="") as f:
        f.write(data)


def safe_name(name: str, fallback: str) -> str:
    """A region's name as a file name (no \\ / : * ? " < > |, no trailing dots or spaces)."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().rstrip(". ")
    return s[:120] or fallback
