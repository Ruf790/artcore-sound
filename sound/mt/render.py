"""Mixdown (3.0): the project — whole, the time selection or each region — into a file of any format the editor
saves, stems (each track alone through the master, a file each), into a new editor tab, and a track bounced into a
new one. The engine renders straight into ffmpeg's pipe (media.save), a block at a time, so a three-hour project
needs no more memory than a short one and no temporary file. With «effect tails» the mix goes on past the end
while the effects still ring (up to 30 s)."""
from __future__ import annotations

import os

import numpy as np

from .. import media, store
from ..doc import Document
from . import media as mmedia
from .engine import RENDER_BLOCK, Engine, _SoloView

TAIL_MAX = 30.0
QUIET = 1e-5


class MixDoc:
    """What media.save needs of a document — rate, channels, layout, frames, markers, chunks() — with the mix as
    its sound."""

    def __init__(self, project, solo=None, tail: bool = False):
        self.p = project
        self.solo = solo
        self.tail = tail
        self.rate, self.channels, self.layout = project.rate, project.channels, project.layout
        self.labels = project.labels
        self.markers = project.markers
        self.info = None
        self.track = None
        self.name = project.name
        self.frames = project.frames + (int(TAIL_MAX * project.rate) if tail else 0)
        self.written = 0

    def chunks(self, a: int, b: int, size: int = RENDER_BLOCK * 4):
        view = _SoloView(self.p, self.solo) if self.solo is not None else self.p
        eng = Engine(view, live=False)
        end_sound = min(b, self.p.frames) if self.tail else b
        try:
            eng.seek(a)
            pos = a
            quiet = 0
            limit = int(0.25 * self.rate)
            while pos < b:
                n = min(size, b - pos)
                y = eng.read(pos, n)
                if self.tail and pos + n > end_sound:
                    k = max(0, end_sound - pos)
                    pk = float(np.abs(y[k:]).max()) if len(y) > k else 0.0
                    quiet = quiet + (n - k) if pk < QUIET else 0
                yield pos, y
                pos += n
                self.written = pos - a
                if self.tail and pos >= end_sound and quiet >= limit:
                    break
        finally:
            eng.close()


def mixdown(project, path: str, fmt: str, option: str, a: int = 0, b: int | None = None, progress=None,
            tail: bool = False, solo=None, markers: bool = True) -> None:
    d = MixDoc(project, solo, tail)
    b = project.frames if b is None else b
    if tail:
        b = b + int(TAIL_MAX * project.rate)
    media.save(d, path, fmt, option, progress, a, b, tags={}, markers=markers)


def stem_paths(project, folder: str, fmt: str, ids) -> list[tuple[int, str]]:
    from ..marks import safe_name
    ext = media.FORMATS[fmt].ext
    out, used = [], set()
    stem = safe_name(project.name or "Project", "Project")
    for i, tid in enumerate(ids, 1):
        tr = project.track(tid)
        base = f"{stem} - {i:02d} {safe_name(tr.name, str(i))}"
        name, k = base, 2
        while name.lower() in used:
            name = f"{base} ({k})"
            k += 1
        used.add(name.lower())
        out.append((tid, os.path.join(folder, name + ext)))
    return out


def stems(project, folder: str, fmt: str, option: str, ids, a: int = 0, b: int | None = None, progress=None,
          tail: bool = False) -> list[str]:
    """Each track of `ids` alone (as if soloed, through its buses and the master) into a file of its own."""
    from ..proc import SubProgress
    os.makedirs(folder, exist_ok=True)
    items = stem_paths(project, folder, fmt, ids)
    out = []
    for i, (tid, path) in enumerate(items):
        sub = SubProgress(progress, i / len(items), (i + 1) / len(items)) if progress is not None else None
        mixdown(project, path, fmt, option, a, b, sub, tail, solo=[tid], markers=False)
        out.append(path)
    return out


def regions(project, folder: str, fmt: str, option: str, progress=None, numbered: bool = True,
            tail: bool = False) -> list[str]:
    from ..markui import region_paths
    from ..proc import SubProgress
    os.makedirs(folder, exist_ok=True)
    items = region_paths(project, folder, fmt, numbered)
    out = []
    for i, (path, a, b) in enumerate(items):
        sub = SubProgress(progress, i / len(items), (i + 1) / len(items)) if progress is not None else None
        d = MixDoc(project, None, tail)
        media.save(d, path, fmt, option, sub, a, b + (int(TAIL_MAX * project.rate) if tail else 0), tags={},
                   markers=False)
        out.append(path)
    return out


def to_document(project, folder: str, a: int = 0, b: int | None = None, progress=None,
                tail: bool = False) -> Document:
    """The mix as a new editor document (its sound in the session folder; the markers in the range come along)."""
    from ..marks import items_for_save, to_markers
    b = project.frames if b is None else b
    d = MixDoc(project, None, tail)
    w = store.SourceWriter(folder, project.channels)
    total = max(1, b - a)
    try:
        for pos, y in d.chunks(a, b + (int(TAIL_MAX * project.rate) if tail else 0)):
            w.append(y)
            if progress is not None:
                progress(min(1.0, (pos - a + len(y)) / total))
    except BaseException:
        w.abort()
        raise
    src = w.finish()
    doc = Document(rate=project.rate, channels=project.channels, folder=folder, labels=tuple(project.labels),
                   layout=project.layout, name=(project.name or "Mix") + " (mix)")
    doc.markers = to_markers(items_for_save(project, a, b), src.frames)
    doc.set_initial(src, "mixdown")
    doc.saved_id = -1
    return doc


def bounce_track(project, tid: int, path: str, cache: str, progress=None) -> mmedia.PcmSource:
    """The track's clips through its effects (before the fader, the pan and the envelopes) from 0 to its end and
    its effects' tail → a WAV float file (Track → Bounce to a new track)."""
    from dataclasses import replace
    tr = project.track(tid)
    eng = Engine(project, live=False)
    nd_labels = None
    try:
        eng.seek(0)
        nd = eng.nodes[tid]
        nd_labels = nd.labels
        end = tr.end
        lat = nd.chain.latency
        base = os.path.join(cache, "b" + os.path.basename(path).encode("utf-8").hex()[:20] + ".pk")
        w = mmedia.WavWriter(path, len(nd_labels), project.rate, base)
        pos, skip = 0, lat
        quiet = 0
        limit = int(0.25 * project.rate)
        stop = end + int(TAIL_MAX * project.rate)
        bare = replace(tr, mute=False, solo=False)
        try:
            while pos < stop:
                n = min(RENDER_BLOCK, stop - pos)
                y = eng._track(bare, pos, n)
                pos += n
                if skip:
                    k = min(skip, len(y))
                    y = y[k:]
                    skip -= k
                if pos > end:
                    pk = float(np.abs(y).max()) if len(y) else 0.0
                    quiet = quiet + n if pk < QUIET else 0
                w.append(y.astype(np.float32))
                if progress is not None:
                    progress(min(1.0, pos / max(1, end)))
                if pos >= end + lat and quiet >= limit:
                    break
        except BaseException:
            w.abort()
            raise
        return w.finish()
    finally:
        eng.close()
