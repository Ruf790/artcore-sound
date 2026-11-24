"""Recovery after a crash (1.0). The sound of a document is on disk anyway (store.py), so what a crash loses is only
the piece table: which pieces of which source files, the history of its states, the markers, where it came from.
The window writes that — the journal — into the document's working folder (doc.json, atomically) whenever it
changes. At the next start the folders of a run that is gone (its lock is free) are looked at before they are
removed: documents with unsaved changes are offered back, with their whole undo history; the session that
restores them takes over the old folder (locks it, removes it when it closes)."""
from __future__ import annotations

import json
import os
import time

from . import VERSION, store
from .doc import Document, Marker, State, new_marker

JOURNAL = "doc.json"
KIND = "artcore-sound-journal"


def _rel(path: str, root: str) -> str:
    try:
        r = os.path.relpath(path, root)
    except ValueError:                              # another drive
        return path
    return path if r.startswith("..") else r


def journal_key(d: Document) -> tuple:
    """Changes whenever something the journal holds changes (the window writes only then)."""
    st = d.history[d.pos].id if d.history else 0
    return (st, len(d.history), d.pos, d.mversion, d.saved_id, d.name, d.path, d.save_format)


def to_dict(d: Document, root: str) -> dict:
    sources: dict = {}

    def sid(src) -> str:
        if src.kind == "zero":
            k = f"z{src.channels}"
            sources[k] = {"kind": "zero", "channels": src.channels}
            return k
        k = str(src.id)
        if k not in sources:
            sources[k] = {"kind": "data", "file": _rel(src.path, root) if src.path else "", "frames": src.frames,
                          "channels": src.channels}
        return k

    def marks(ms) -> list:
        return [[m.id, m.name, m.pos, m.end] for m in ms]

    hist = []
    for st in d.history:
        hist.append({"pieces": [[sid(s), int(o), int(n)] for s, o, n in st.pieces], "label": st.label,
                     "rate": st.rate, "channels": st.channels, "labels": list(st.labels), "layout": st.layout,
                     "sel": list(st.sel) if st.sel else None, "cursor": st.cursor,
                     "sel_before": list(st.sel_before) if st.sel_before else None,
                     "cursor_before": st.cursor_before, "markers": marks(st.markers)})
    saved = next((i for i, st in enumerate(d.history) if st.id == d.saved_id), -1)
    tags = dict(getattr(d.info, "tags", None) or {})
    track = getattr(d.track, "pos", 0) if d.track is not None else 0
    return {"kind": KIND, "app_version": VERSION, "time": time.time(), "name": d.name, "path": d.path,
            "track": track, "film": dict(d.film), "tags": tags, "save_format": d.save_format, "save_opts": dict(d.save_opts),
            "dirty": d.dirty, "frames": d.frames, "rate": d.rate, "channels": d.channels, "sources": sources,
            "history": hist, "pos": d.pos, "saved": saved, "sel": list(d.sel) if d.sel else None,
            "cursor": d.cursor, "view_start": d.view_start, "spp": d.spp}


def write(d: Document, root: str) -> None:
    """The document's journal into its folder (temporary file + replace: a crash mid-write keeps the old one)."""
    data = to_dict(d, root)
    path = os.path.join(d.folder, JOURNAL)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove(d: Document) -> None:
    for p in (os.path.join(d.folder, JOURNAL), os.path.join(d.folder, JOURNAL + ".tmp")):
        try:
            os.remove(p)
        except OSError:
            pass


class Entry:
    """A document that can be brought back: the session folder it lived in, its folder, its journal. kind
    "project" (3.0): a multitrack project's journal (mt/projfile.py)."""

    def __init__(self, session: str, folder: str, data: dict, kind: str = "document"):
        self.session, self.folder, self.data, self.kind = session, folder, data, kind

    @property
    def name(self) -> str:
        return self.data.get("name") or "?"

    @property
    def duration(self) -> float:
        rate = self.data.get("rate") or 0
        return (self.data.get("frames") or 0) / rate if rate else 0.0

    @property
    def time(self) -> float:
        return float(self.data.get("time") or 0)

    @property
    def steps(self) -> int:
        return max(0, int(self.data.get("pos") or 0))


def _stale(path: str) -> bool:
    from PySide6.QtCore import QLockFile
    lock = QLockFile(os.path.join(path, "lock"))
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        lock.unlock()
        return True
    return False


def scan(root: str) -> list[Entry]:
    """The documents with unsaved changes in the folders of runs that are gone, newest first."""
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        sp = os.path.join(root, name)
        if not (name.startswith("s-") and os.path.isdir(sp)) or not _stale(sp):
            continue
        for dn in os.listdir(sp):
            pj = os.path.join(sp, dn, "project.json")            # 3.0: a multitrack project
            if os.path.isfile(pj):
                try:
                    with open(pj, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                if data.get("kind") == "artcore-sound-project-journal" and data.get("dirty") and data.get("history"):
                    out.append(Entry(sp, os.path.join(sp, dn), data, "project"))
            jp = os.path.join(sp, dn, JOURNAL)
            if not os.path.isfile(jp):
                continue
            try:
                with open(jp, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if data.get("kind") == KIND and data.get("dirty") and data.get("history"):
                out.append(Entry(sp, os.path.join(sp, dn), data))
    out.sort(key=lambda e: -e.time)
    return out


def restore(e: Entry, session) -> Document:
    """The document of the entry, as it was: the current state, the whole undo history, the markers, the selection.
    The session takes over the old session folder. ValueError when a working file is missing or damaged."""
    from . import ff
    data = e.data
    session.adopt(e.session)
    srcs: dict = {}
    for k, s in data["sources"].items():
        if s["kind"] == "zero":
            continue
        path = s["file"]
        if path and not os.path.isabs(path):
            path = os.path.join(e.session, path)
        srcs[k] = store.open_source(path, int(s["frames"]), int(s["channels"]))
    zeros: dict = {}

    def src_of(k: str):
        if k.startswith("z"):
            ch = int(k[1:])
            if ch not in zeros:
                zeros[ch] = store.Source.zero(ch)
            return zeros[ch]
        return srcs[k]

    ids: dict = {}

    def marks(ms) -> tuple:
        out = []
        for mid, name, pos, end in ms:
            if mid not in ids:
                ids[mid] = new_marker(name, pos, end).id
            out.append(Marker(ids[mid], name, int(pos), None if end is None else int(end)))
        return tuple(out)

    h = data["history"]
    first = h[0]
    d = Document(rate=first["rate"], channels=first["channels"], folder=e.folder, labels=tuple(first["labels"]),
                 layout=first["layout"], name=data.get("name") or "", path=data.get("path") or "")
    states = []
    from .doc import _state_ids
    for st in h:
        pieces = tuple((src_of(k), int(o), int(n)) for k, o, n in st["pieces"])
        states.append(State(pieces, st["label"], int(st["rate"]), int(st["channels"]), tuple(st["labels"]),
                            st["layout"], tuple(st["sel"]) if st["sel"] else None, int(st["cursor"]),
                            next(_state_ids), tuple(st["sel_before"]) if st["sel_before"] else None,
                            int(st["cursor_before"]), marks(st["markers"])))
    d.history = states
    pos = max(0, min(len(states) - 1, int(data.get("pos", len(states) - 1))))
    d.pos = pos
    d.markers = ()
    d.rate, d.channels = -1, -1                       # _restore() sets everything from the state
    d._restore(states[pos])
    saved = int(data.get("saved", -1))
    d.saved_id = states[saved].id if 0 <= saved < len(states) else 0
    if data.get("sel"):
        d.sel = d._clip_sel(tuple(data["sel"]))
    d.cursor = min(int(data.get("cursor") or 0), d.frames)
    d.view_start, d.spp = float(data.get("view_start") or 0), float(data.get("spp") or 0)
    d.save_format, d.save_opts = data.get("save_format") or "", dict(data.get("save_opts") or {})
    d.film = dict(data.get("film") or {})
    if d.path and os.path.isfile(d.path):
        try:
            d.info = ff.probe(d.path)
            d.track = next((t for t in d.info.tracks if t.pos == data.get("track", 0)), d.info.tracks[0])
        except Exception:                                 # noqa: BLE001 — the tags then come from the journal
            d.info = None
    if d.info is None and data.get("tags"):
        d.info = ff.FileInfo(path=d.path, tags=dict(data["tags"]))
    return d
