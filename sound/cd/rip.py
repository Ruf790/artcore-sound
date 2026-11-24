"""Ripping an Audio CD (2.0): tracks read off a drive into the editor (each its own file, or the whole disc as one
file with a region for each track) or saved as files in any format, with tags.

Secure reading: each track read twice; the pieces (CHUNK sectors) whose two reads differ are read again — each time
after reading far away, so the drive's cache does not answer — until two reads agree; a piece that never agrees is
reported (with the most common of its reads kept). The drive's read offset (in frames, as AccurateRip lists them) is
made up for. Each track's CRC32 (of the sound, as EAC's «Copy CRC») is reported."""
from __future__ import annotations

import collections
import os
import zlib
from dataclasses import dataclass, field

import numpy as np

from .. import store
from .drive import CHUNK, Drive
from .toc import SECTOR, Toc

FRAME_BYTES = 4


@dataclass
class Meta:
    album: str = ""
    artist: str = ""
    date: str = ""
    genre: str = ""
    tracks: list = field(default_factory=list)          # [{"title", "artist"}] by track order (all tracks)

    def title(self, i: int) -> str:
        return (self.tracks[i].get("title") if i < len(self.tracks) else "") or ""

    def track_artist(self, i: int) -> str:
        return (self.tracks[i].get("artist") if i < len(self.tracks) else "") or self.artist

    @classmethod
    def from_cdtext(cls, text, n: int) -> "Meta":
        m = cls()
        if text is not None:
            m.album = text.album.get("title", "")
            m.artist = text.album.get("performer", "")
            m.tracks = [{"title": d.get("title", ""), "artist": d.get("performer", "")} for d in text.tracks]
        while len(m.tracks) < n:
            m.tracks.append({"title": "", "artist": ""})
        return m


@dataclass
class Options:
    secure: bool = True
    retries: int = 16                  # re-reads of a piece whose reads disagree
    offset: int = 0                    # the drive's read offset, frames


@dataclass
class TrackResult:
    number: int
    frames: int
    crc: str = ""
    suspicious: int = 0                # pieces (CHUNK sectors) whose reads never agreed
    reread: int = 0                    # pieces read again (and then agreed)
    error: str = ""


def read_range(drive: Drive, lba: int, frames: int, offset: int) -> bytes:
    """frames of sound from LBA lba on, the drive's offset made up for (what lies off the disc is silence)."""
    byte0 = (lba * SECTOR) + offset * FRAME_BYTES
    s0 = byte0 // SECTOR
    lead = byte0 - s0 * SECTOR
    n = -(-(lead + frames * FRAME_BYTES) // SECTOR)
    data = drive.read(s0, n)
    return data[lead:lead + frames * FRAME_BYTES]


def rip_track(drive: Drive, toc: Toc, a: int, b: int, opts: Options, writer, progress=None, cancel=None) -> TrackResult:
    """Sectors [a, b) of the disc → writer (store.SourceWriter, float). Piece by piece; secure: twice."""
    res = TrackResult(0, (b - a) * (SECTOR // FRAME_BYTES))
    crc = 0
    piece = CHUNK * 4                                    # sectors per piece compared
    pos = a
    total = b - a
    while pos < b:
        if cancel is not None and cancel():
            from ..media import Cancelled
            raise Cancelled()
        n = min(piece, b - pos)
        frames = n * (SECTOR // FRAME_BYTES)
        data = read_range(drive, pos, frames, opts.offset)
        if opts.secure:
            again = read_range(drive, pos, frames, opts.offset)
            if again != data:
                data, agreed, tries = _settle(drive, pos, frames, opts, [data, again])
                res.reread += 1
                if not agreed:
                    res.suspicious += 1
        crc = zlib.crc32(data, crc)
        x = np.frombuffer(data, "<i2").reshape(-1, 2).astype(np.float32) / 32768.0
        writer.append(x)
        pos += n
        if progress is not None:
            progress((pos - a) / max(1, total))
    res.crc = f"{crc & 0xFFFFFFFF:08X}"
    return res


def _settle(drive: Drive, lba: int, frames: int, opts: Options, reads: list) -> tuple[bytes, bool, int]:
    """Read again (after a read far away) until the last two reads agree; else the most common read."""
    far = 0 if lba > 20000 else lba + 20000
    for k in range(opts.retries):
        try:
            drive.read(far, 1)                           # out of the drive's cache
        except Exception:                                # noqa: BLE001 — past the end: fine
            pass
        reads.append(read_range(drive, lba, frames, opts.offset))
        if reads[-1] == reads[-2]:
            return reads[-1], True, k + 1
    best = collections.Counter(reads).most_common(1)[0][0]
    return best, False, opts.retries


def name_for(template: str, meta: Meta, i: int, number: int) -> str:
    from ..batch import clean_name
    vals = {"n": number, "title": meta.title(i) or f"Track {number:02d}", "artist": meta.track_artist(i),
            "album": meta.album, "date": meta.date}

    def sub(m):
        key, _sep, width = m.group(1).partition(":")
        key = key.strip().lower()
        if key == "n":
            w = int(width) if width.strip().isdigit() else 0
            return str(number).zfill(w)
        if key in vals:
            return str(vals[key])
        return m.group(0)
    import re
    return clean_name(re.sub(r"\{([^{}]*)\}", sub, template or "{n:2} - {title}"))


def tags_for(meta: Meta, i: int, number: int, total: int) -> dict:
    t = {"title": meta.title(i), "artist": meta.track_artist(i), "album": meta.album, "album_artist": meta.artist,
         "date": meta.date, "genre": meta.genre, "track": f"{number}/{total}"}
    return {k: v for k, v in t.items() if v}


def track_document(folder: str, name: str):
    from ..doc import Document
    return Document(rate=44100, channels=2, folder=folder, labels=("L", "R"), layout="stereo", name=name)


def rip(drive: Drive, toc: Toc, numbers: list[int], meta: Meta, opts: Options, folder, mode: str,
        progress=None, cancel=None, save=None) -> tuple[list, list[TrackResult]]:
    """mode "tracks": a Document per track; "disc": one Document from the first chosen track to the end of the
    last, a region for each; "files": save(document, index, number) for each track, then the document closed.
    folder: the working folder — or what makes a new one (Session.folder), one for each document.
    → (the documents kept, the results)."""
    from ..doc import Document
    new_folder = folder if callable(folder) else (lambda: folder)
    audio = toc.audio
    chosen = [t for t in audio if t.number in numbers]
    results: list[TrackResult] = []
    docs = []
    total = sum(t.length for t in chosen) or 1
    done = 0

    def sub(t):
        lo = done / total
        hi = (done + t.length) / total
        return (lambda f: progress(lo + (hi - lo) * f)) if progress is not None else None

    if mode == "disc" and chosen:
        a, b = chosen[0].start, chosen[-1].start + chosen[-1].length
        folder = new_folder()
        w = store.SourceWriter(folder, 2)
        try:
            rr = rip_track(drive, toc, a, b, opts, w, (lambda f: progress(f)) if progress else None, cancel)
        except BaseException:
            w.abort()
            raise
        d = Document(rate=44100, channels=2, folder=folder, labels=("L", "R"), layout="stereo",
                     name=meta.album or "Audio CD")
        d.set_initial(w.finish())
        for t in chosen:
            i = audio.index(t)
            title = meta.title(i) or f"Track {t.number:02d}"
            s0 = (t.start - a) * (SECTOR // FRAME_BYTES)
            d.add_region(s0, s0 + t.length * (SECTOR // FRAME_BYTES), title, label="cd")
        d.history = d.history[-1:]
        d.pos = 0
        d.saved_id = -1
        d.cd_meta = meta                                 # «Burn» takes the album and the artist from it
        rr.number = 0
        return [d], [rr]
    for t in chosen:
        i = audio.index(t)
        folder = new_folder()
        w = store.SourceWriter(folder, 2)
        try:
            rr = rip_track(drive, toc, t.start, t.start + t.length, opts, w, sub(t), cancel)
        except BaseException:
            w.abort()
            raise
        rr.number = t.number
        d = track_document(folder, name_for("{n:2} - {title}", meta, i, t.number))
        d.set_initial(w.finish())
        d.saved_id = -1
        results.append(rr)
        if mode == "files" and save is not None:
            try:
                save(d, i, t.number)
            except Exception as e:                       # noqa: BLE001
                rr.error = str(e) or type(e).__name__
            d.close()
        else:
            docs.append(d)
        done += t.length
    return docs, results


def save_track(d, path: str, fmt: str, option: str, tags: dict, progress=None) -> str:
    from .. import media
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    media.save(d, path, fmt, option, progress, tags=tags, markers=False, dither=False)   # bit for bit
    return path
