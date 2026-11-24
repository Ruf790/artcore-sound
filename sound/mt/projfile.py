"""Projects on disk (3.0): the .acproj file (JSON — the tracks, clips, buses, effects, envelopes, markers, the media
by path; no sound in it) and the crash journal: the same plus the whole undo history, written into the project's
session folder whenever the project changes (recovery.py finds it after a crash, as it finds documents).

Paths: a medium inside the project's folder is kept relative to it (the folder can be moved or copied whole), one
outside it absolute; at opening a medium not found at its path is looked for next to the project by its name."""
from __future__ import annotations

import json
import os
import time

from .. import VERSION
from ..doc import Marker, new_marker
from . import media as mmedia
from .model import MASTER, Clip, Project, Send, Slot, State, Take, Track, _state_ids, bump_ids

KIND = "artcore-sound-project"
JOURNAL_KIND = "artcore-sound-project-journal"
JOURNAL = "project.json"
EXT = ".acproj"
FORMAT = 1


def audio_dir(p: Project) -> str:
    """Where the program writes the project's own files (recordings, sent clips, frozen tracks)."""
    stem = os.path.splitext(os.path.basename(p.path))[0] if p.path else (p.name or "Project")
    return os.path.join(p.folder or os.path.dirname(p.path), f"{stem} Audio")


# ── to / from data ────────────────────────────────────────────
def clip_dict(c: Clip) -> dict:
    d = {"id": c.id, "start": c.start, "length": c.length,
         "takes": [[k.media, k.offset, k.name] for k in c.takes], "take": c.take}
    for k in ("gain_db", "fade_in", "fade_out", "curve_in", "curve_out", "muted", "name"):
        v = getattr(c, k)
        if v != getattr(Clip, k, None):
            d[k] = v
    return d


def clip_from(d: dict) -> Clip:
    takes = tuple(Take(str(m), int(o), str(n)) for m, o, n in d["takes"]) or (Take("", 0),)
    return Clip(int(d["id"]), int(d["start"]), max(1, int(d["length"])), takes, int(d.get("take", 0)),
                float(d.get("gain_db", 0.0)), int(d.get("fade_in", 0)), int(d.get("fade_out", 0)),
                str(d.get("curve_in", "linear")), str(d.get("curve_out", "linear")), bool(d.get("muted", False)),
                str(d.get("name", "")))


TRACK_KEYS = ("name", "kind", "channels", "gain_db", "pan", "mute", "solo", "phase", "output", "env_on", "color",
              "input", "height")


def track_dict(t: Track, clips=None) -> dict:
    d = {"id": t.id}
    for k in TRACK_KEYS:
        d[k] = getattr(t, k)
    d["input_ch"] = list(t.input_ch)
    d["clips"] = [clip_dict(c) for c in t.clips] if clips is None else clips
    d["sends"] = [[s.bus, s.db, s.pre] for s in t.sends]
    d["fx"] = [{"key": s.key, "values": s.values, "bypass": s.bypass, "id": s.id} for s in t.fx]
    d["vol_env"] = [list(p) for p in t.vol_env]
    d["pan_env"] = [list(p) for p in t.pan_env]
    return d


def track_from(d: dict, clips=None) -> Track:
    kw = {k: d[k] for k in TRACK_KEYS if k in d}
    kw["channels"] = int(kw.get("channels", 2))
    return Track(int(d["id"]), str(kw.pop("name", "")),
                 clips=tuple(clip_from(c) for c in d.get("clips", [])) if clips is None else clips,
                 sends=tuple(Send(int(b), float(db), bool(pre)) for b, db, pre in d.get("sends", [])),
                 fx=tuple(Slot(str(s["key"]), dict(s.get("values") or {}), bool(s.get("bypass")), int(s.get("id", 0)))
                          for s in d.get("fx", [])),
                 vol_env=tuple((int(f), float(v)) for f, v in d.get("vol_env", [])),
                 pan_env=tuple((int(f), float(v)) for f, v in d.get("pan_env", [])),
                 input_ch=tuple(int(x) for x in d.get("input_ch", [])), **kw)


def _rel(path: str, root: str) -> str:
    if not root or not path:
        return path
    try:
        r = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if r.startswith("..") else r


def to_dict(p: Project) -> dict:
    root = os.path.dirname(os.path.abspath(p.path)) if p.path else p.folder
    used = {k.media for t in p.tracks for c in t.clips for k in c.takes}
    media = [{**m.to_dict(), "path": _rel(m.path, root)} for mid, m in p.media.items() if mid in used]
    return {"kind": KIND, "format": FORMAT, "app_version": VERSION, "rate": p.rate, "channels": p.channels,
            "name": p.name, "media": media, "tracks": [track_dict(t) for t in p.tracks],
            "master": track_dict(p.master), "markers": [[m.name, m.pos, m.end] for m in p.markers],
            "video": dict(p.video), "cursor": p.cursor, "sel": list(p.sel) if p.sel else None,
            "view": {"start": p.view_start, "spp": p.spp}, "ui": _ui(p)}


def _ui(p: Project) -> dict:
    return {k: v for k, v in p.ui.items() if isinstance(v, (bool, int, float, str, list, dict))}


def _max_id(tracks, master) -> int:
    n = 0
    for t in tuple(tracks) + (master,):
        n = max(n, t.id)
        for c in t.clips:
            n = max(n, c.id)
        for s in t.fx:
            n = max(n, s.id)
    return n


def from_dict(data: dict, path: str = "") -> Project:
    if data.get("kind") not in (KIND, JOURNAL_KIND):
        raise ValueError("not an ArtCore Sound project")
    if int(data.get("format", 1)) > FORMAT:
        raise ValueError("a project of a newer version")
    p = Project(int(data.get("rate", 48000)), int(data.get("channels", 2)), str(data.get("name", "")), path,
                os.path.dirname(os.path.abspath(path)) if path else str(data.get("folder", "")))
    root = os.path.dirname(os.path.abspath(path)) if path else p.folder
    for md in data.get("media", []):
        mp = md.get("path") or ""
        if mp and not os.path.isabs(mp):
            mp = os.path.join(root, mp)
        m = mmedia.Media(str(md["id"]), mp, int(md.get("track", 0)), str(md.get("name", "")))
        m.frames, m.channels = int(md.get("frames", 0)), int(md.get("channels", 2))
        p.media[m.id] = m
    tracks = tuple(track_from(t) for t in data.get("tracks", []))
    master = track_from(data["master"]) if data.get("master") else p.master
    master = Track(MASTER, master.name or "Master", "master", p.channels, gain_db=master.gain_db, fx=master.fx,
                   vol_env=master.vol_env, env_on=master.env_on, mute=master.mute)
    markers = tuple(new_marker(n, pos, end) for n, pos, end in data.get("markers", []))
    bump_ids(_max_id(tracks, master))
    p.tracks, p.master, p.markers = tracks, master, markers
    p.cursor = int(data.get("cursor") or 0)
    p.sel = p._clip_sel(data.get("sel"))
    v = data.get("view") or {}
    p.view_start, p.spp = float(v.get("start") or 0), float(v.get("spp") or 0)
    p.video = dict(data.get("video") or {})
    p.ui = dict(data.get("ui") or {})
    p.history = []
    p.pos = -1
    p._push("open")
    p.saved_id = p.history[-1].id
    return p


def save(p: Project, path: str | None = None) -> None:
    """Write the .acproj (temporary file + replace). Media paths become relative to the new place."""
    path = os.path.abspath(path or p.path)
    old = p.path
    p.path = path
    p.folder = os.path.dirname(path)
    try:
        data = to_dict(p)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        p.path = old
        raise
    p.name = os.path.splitext(os.path.basename(path))[0]
    p.mark_saved()


def load(path: str) -> Project:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    p = from_dict(data, os.path.abspath(path))
    p.name = os.path.splitext(os.path.basename(path))[0]
    for m in p.media.values():                      # moved with the project: look next to it by name
        if m.path and not os.path.isfile(m.path):
            for cand in (os.path.join(p.folder, os.path.basename(m.path)),
                         os.path.join(audio_dir(p), os.path.basename(m.path))):
                if os.path.isfile(cand):
                    m.path = cand
                    break
    return p


def load_media(p: Project, cache: str, progress=None, only=None) -> list[str]:
    """Make the sources of the project's media (the slow part of opening: decoding into the cache the first time).
    → the names of the media that are offline."""
    from ..media import Progress
    from ..proc import SubProgress
    progress = progress or Progress()
    items = [m for m in p.media.values() if only is None or m.id in only]
    for i, m in enumerate(items):
        mmedia.load(m, p.rate, cache, SubProgress(progress, i / max(1, len(items)), (i + 1) / max(1, len(items))))
    progress(1.0)
    return [m.name for m in items if m.offline]


# ── the crash journal ─────────────────────────────────────────
def journal_dict(p: Project) -> dict:
    """The project and its whole history; clips and tracks the states share are written once."""
    clips: dict = {}
    tracks: dict = {}

    def cref(c: Clip) -> int:
        k = id(c)
        if k not in clips:
            clips[k] = (len(clips), clip_dict(c))
        return clips[k][0]

    def tref(t: Track) -> int:
        k = id(t)
        if k not in tracks:
            tracks[k] = (len(tracks), track_dict(t, [cref(c) for c in t.clips]))
        return tracks[k][0]

    hist = []
    for st in p.history:
        hist.append({"tracks": [tref(t) for t in st.tracks], "master": tref(st.master), "label": st.label,
                     "markers": [[m.id, m.name, m.pos, m.end] for m in st.markers],
                     "sel": list(st.sel) if st.sel else None, "cursor": st.cursor,
                     "sel_before": list(st.sel_before) if st.sel_before else None, "cursor_before": st.cursor_before})
    saved = next((i for i, st in enumerate(p.history) if st.id == p.saved_id), -1)
    data = to_dict(p)
    data.update(kind=JOURNAL_KIND, time=time.time(), path=p.path, folder=p.folder, dirty=p.dirty,
                frames=p.frames, clip_table=[v for _i, v in sorted(clips.values(), key=lambda x: x[0])],
                track_table=[v for _i, v in sorted(tracks.values(), key=lambda x: x[0])], history=hist,
                pos=p.pos, saved=saved,
                media=[m.to_dict() for m in p.media.values()])
    return data


def journal_key(p: Project) -> tuple:
    st = p.history[p.pos].id if p.history else 0
    return (st, len(p.history), p.pos, p.saved_id, p.path, p.name, len(p.media))


def write_journal(p: Project) -> None:
    if not p.journal_folder:
        return
    data = journal_dict(p)
    path = os.path.join(p.journal_folder, JOURNAL)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)


def remove_journal(p: Project) -> None:
    if not p.journal_folder:
        return
    for n in (JOURNAL, JOURNAL + ".tmp"):
        try:
            os.remove(os.path.join(p.journal_folder, n))
        except OSError:
            pass


def from_journal(data: dict) -> Project:
    """The project of a journal, as it was: the current state and the whole history."""
    path = data.get("path") or ""
    p = from_dict(data, path)
    if not path:
        p.folder = data.get("folder") or ""
    p.name = data.get("name") or p.name
    ctab = [clip_from(c) for c in data["clip_table"]]
    ttab = []
    for td in data["track_table"]:
        ttab.append(track_from(td, tuple(ctab[i] for i in td["clips"])))
    ids: dict = {}

    def marks(ms) -> tuple:
        out = []
        for mid, name, pos, end in ms:
            if mid not in ids:
                ids[mid] = new_marker(name, pos, end).id
            out.append(Marker(ids[mid], name, int(pos), None if end is None else int(end)))
        return tuple(out)

    states = []
    for st in data["history"]:
        m = ttab[st["master"]]
        master = Track(MASTER, m.name or "Master", "master", p.channels, gain_db=m.gain_db, fx=m.fx,
                       vol_env=m.vol_env, env_on=m.env_on, mute=m.mute)
        states.append(State(tuple(ttab[i] for i in st["tracks"]), master, marks(st["markers"]), st["label"],
                            tuple(st["sel"]) if st["sel"] else None, int(st["cursor"]), next(_state_ids),
                            tuple(st["sel_before"]) if st["sel_before"] else None, int(st["cursor_before"])))
    bump_ids(max([_max_id(s.tracks, s.master) for s in states] or [0]))
    p.history = states
    p.pos = max(0, min(len(states) - 1, int(data.get("pos", len(states) - 1))))
    p._restore(states[p.pos])
    saved = int(data.get("saved", -1))
    p.saved_id = states[saved].id if 0 <= saved < len(states) else 0
    return p
