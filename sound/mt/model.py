"""The multitrack project (3.0). Everything a project is — its tracks with their clips, buses, the master, envelopes,
effect chains, markers and regions — is immutable data (frozen dataclasses and tuples); an edit makes a new state
from the old one, sharing whatever did not change, and the history keeps every state. Undo is therefore unlimited
and costs a few hundred bytes a step; no sound is ever copied (clips point into the project's media, media.py).

Times are frames of the project's rate. A clip plays frames [offset, offset + length) of its active take's medium
at [start, start + length) of the timeline. Clips on one track may overlap: the later one in the track's list lies on
top, and its fades show what is under it (a fade-in over another clip is a crossfade)."""
from __future__ import annotations

import bisect
import itertools
import math
from dataclasses import dataclass, field, replace

from ..doc import Marker, new_marker, remap_markers, sort_markers

MASTER = 0                      # the id of the master (a track's output 0 = the master)
KINDS = ("audio", "bus")
CURVES = ("linear", "log", "exp", "scurve")
ENV_MIN_DB = -60.0              # the bottom of a volume envelope: silence
ENV_MAX_DB = 12.0
GAIN_MIN_DB = -60.0             # a fader at the bottom is silence
GAIN_MAX_DB = 12.0
TRACK_CHANNELS = (1, 2, 6)
AUTO_XFADE = 0.01               # seconds of the fades a new overlap gets at least (no clicks at the seams)
COLORS = 12                     # track colours (style.mt_color)

_ids = itertools.count(1)


def new_id() -> int:
    return next(_ids)


def bump_ids(above: int) -> None:
    """After loading a project: new ids start past the largest one in it."""
    global _ids
    cur = next(_ids)
    _ids = itertools.count(max(cur, int(above) + 1))


# ── the data ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Take:
    """One recording / file a clip can play: frames from `offset` of medium `media`."""
    media: str
    offset: int = 0
    name: str = ""


@dataclass(frozen=True)
class Clip:
    id: int
    start: int
    length: int
    takes: tuple                 # Take, at least one
    take: int = 0                # the active one
    gain_db: float = 0.0
    fade_in: int = 0
    fade_out: int = 0
    curve_in: str = "linear"
    curve_out: str = "linear"
    muted: bool = False
    name: str = ""

    @property
    def end(self) -> int:
        return self.start + self.length

    @property
    def active(self) -> Take:
        return self.takes[min(max(0, self.take), len(self.takes) - 1)]

    @property
    def media(self) -> str:
        return self.active.media

    @property
    def offset(self) -> int:
        return self.active.offset


@dataclass(frozen=True)
class Send:
    bus: int
    db: float = 0.0
    pre: bool = False            # before the fader (and the pan, the envelopes)


@dataclass(frozen=True)
class Slot:
    """An effect in a chain: key (fx.EFFECTS / "vst3"), values, bypass. values is never changed in place."""
    key: str
    values: dict = field(default_factory=dict, hash=False, compare=True)
    bypass: bool = False
    id: int = 0


@dataclass(frozen=True)
class Track:
    id: int
    name: str
    kind: str = "audio"          # audio | bus | master
    channels: int = 2
    clips: tuple = ()            # Clip, the last on top
    gain_db: float = 0.0
    pan: float = 0.0             # −1 (left) … 1 (right)
    mute: bool = False
    solo: bool = False
    phase: bool = False          # the polarity inverted
    output: int = MASTER         # a bus id or MASTER
    sends: tuple = ()            # Send
    fx: tuple = ()               # Slot
    vol_env: tuple = ()          # (frame, dB) sorted; empty — no envelope
    pan_env: tuple = ()          # (frame, −1…1)
    env_on: bool = True          # the envelopes are played
    color: int = 0
    input: str = ""              # the recording input: device name ("" — the default)
    input_ch: tuple = ()         # its channels (indices; () — the first `channels`)
    height: int = 0              # the lane's height in the timeline (0 — the default)

    @property
    def end(self) -> int:
        return max((c.end for c in self.clips), default=0)


@dataclass
class State:
    tracks: tuple
    master: Track
    markers: tuple
    label: str
    sel: tuple | None
    cursor: int
    id: int = 0
    sel_before: tuple | None = None
    cursor_before: int = 0
    merge: str = ""              # a step of the same merge key right after this one replaces it (a fader drag)


def new_master(channels: int) -> Track:
    return Track(MASTER, "Master", "master", channels)


_state_ids = itertools.count(1)


def clamp_db(v: float, lo: float = GAIN_MIN_DB, hi: float = GAIN_MAX_DB) -> float:
    v = float(v)
    return lo if not math.isfinite(v) else min(hi, max(lo, v))


def db_gain(db: float) -> float:
    return 0.0 if db <= GAIN_MIN_DB else 10.0 ** (db / 20.0)


# ── the project ───────────────────────────────────────────────
class Project:
    """A project: rate, the master's channels, the media, the current state and its history."""

    def __init__(self, rate: int = 48000, channels: int = 2, name: str = "", path: str = "", folder: str = ""):
        from ..ff import labels, layout_for
        self.rate = int(rate)
        self.channels = int(channels)
        self.layout = layout_for(self.channels, "")
        self.labels = tuple(labels(self.channels, self.layout))
        self.name = name
        self.path = path                       # the .acproj
        self.folder = folder                   # the project's folder (audio_dir inside it)
        self.media: dict = {}                  # id → media.Media (the files and their sources)
        self.video: dict = {}                  # the film it scores {"path", "offset"} (frames of the timeline)
        self.tracks: tuple = ()
        self.master = new_master(self.channels)
        self.markers: tuple = ()
        self.sel: tuple | None = None          # the time selection (start, end)
        self.cursor = 0
        self.sel_clips: frozenset = frozenset()  # the selected clips (ids) — not in the history
        self.history: list[State] = []
        self.pos = -1
        self.saved_id = 0
        self.version = 0                       # changes with every state (caches, the engine)
        self.mversion = 0
        self.view_start = 0.0                  # the timeline's view (frames at the left edge, frames per pixel)
        self.spp = 0.0
        self.ui: dict = {}                     # armed / monitored tracks, the mixer's shown state …
        self.journal_folder = ""               # the session folder the crash journal goes to
        self._push("new")
        self.saved_id = self.history[-1].id

    # ── basics ────────────────────────────────────────────────
    @property
    def frames(self) -> int:
        """The end of the last clip (or region / marker)."""
        e = max((t.end for t in self.tracks), default=0)
        for m in self.markers:
            e = max(e, m.end if m.end is not None else m.pos)
        return e

    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    def all_tracks(self):
        """The tracks and the buses, then the master."""
        return self.tracks + (self.master,)

    def track(self, tid: int) -> Track | None:
        if tid == MASTER:
            return self.master
        return next((t for t in self.tracks if t.id == tid), None)

    def index(self, tid: int) -> int:
        return next((i for i, t in enumerate(self.tracks) if t.id == tid), -1)

    def buses(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "bus"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    def clip(self, cid: int) -> tuple[Track, Clip] | tuple[None, None]:
        for t in self.tracks:
            for c in t.clips:
                if c.id == cid:
                    return t, c
        return None, None

    def clips_in(self, a: int, b: int, tracks=None):
        """(track, clip) of the clips that sound in [a, b) (on the given track ids, all by default)."""
        for t in self.tracks:
            if tracks is not None and t.id not in tracks:
                continue
            for c in t.clips:
                if c.start < b and c.end > a:
                    yield t, c

    def used_media(self) -> set:
        """Every medium a state of the history still plays (any take)."""
        out = set()
        for st in self.history:
            for t in st.tracks:
                for c in t.clips:
                    for k in c.takes:
                        out.add(k.media)
        return out

    def snap_points(self, exclude: frozenset = frozenset()) -> list[int]:
        """Clip edges, markers, region edges, 0 and the cursor (the snapping of drags)."""
        pts = {0, self.cursor}
        for t in self.tracks:
            for c in t.clips:
                if c.id not in exclude:
                    pts.add(c.start)
                    pts.add(c.end)
        for m in self.markers:
            pts.add(m.pos)
            if m.end is not None:
                pts.add(m.end)
        return sorted(pts)

    # ── history ───────────────────────────────────────────────
    def _push(self, label: str, sel_before=None, cursor_before: int = 0, merge: str = "") -> None:
        st = State(self.tracks, self.master, self.markers, label, self.sel, self.cursor, next(_state_ids),
                   sel_before, cursor_before, merge)
        if merge and self.history and self.pos == len(self.history) - 1 and self.history[-1].merge == merge \
                and self.history[-1].id != self.saved_id and len(self.history) > 1:
            prev = self.history[-1]
            st.sel_before, st.cursor_before = prev.sel_before, prev.cursor_before
            self.history[-1] = st
        else:
            del self.history[self.pos + 1:]
            self.history.append(st)
        self.pos = len(self.history) - 1
        self.version += 1

    def _restore(self, st: State) -> None:
        if st.markers != self.markers:
            self.mversion += 1
        self.tracks, self.master, self.markers = st.tracks, st.master, st.markers
        self.sel, self.cursor = self._clip_sel(st.sel), st.cursor
        live = {c.id for t in self.tracks for c in t.clips}
        self.sel_clips = frozenset(i for i in self.sel_clips if i in live)
        self.version += 1

    def apply(self, label: str, tracks=None, master=None, markers=None, sel=..., cursor=None, merge: str = "") -> None:
        """A new state: what is given replaces the current; the rest stays."""
        before = (self.sel, self.cursor)
        if tracks is not None:
            self.tracks = tuple(tracks)
        if master is not None:
            self.master = master
        if markers is not None:
            markers = sort_markers(markers)
            if markers != self.markers:
                self.markers = markers
                self.mversion += 1
        if sel is not ...:
            self.sel = self._clip_sel(sel)
        if cursor is not None:
            self.cursor = max(0, int(cursor))
        self._push(label, *before, merge=merge)

    def can_undo(self) -> bool:
        return self.pos > 0

    def can_redo(self) -> bool:
        return self.pos < len(self.history) - 1

    def undo_label(self) -> str:
        return self.history[self.pos].label if self.can_undo() else ""

    def redo_label(self) -> str:
        return self.history[self.pos + 1].label if self.can_redo() else ""

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        cur = self.history[self.pos]
        self.pos -= 1
        self._restore(self.history[self.pos])
        self.sel, self.cursor = self._clip_sel(cur.sel_before), cur.cursor_before
        return True

    def redo(self) -> bool:
        if not self.can_redo():
            return False
        self.pos += 1
        self._restore(self.history[self.pos])
        return True

    def goto_state(self, i: int) -> None:
        if 0 <= i < len(self.history) and i != self.pos:
            self.pos = i
            self._restore(self.history[i])

    @property
    def dirty(self) -> bool:
        return bool(self.history) and self.history[self.pos].id != self.saved_id

    def mark_saved(self) -> None:
        if self.history:
            self.saved_id = self.history[self.pos].id
            self.history[self.pos].merge = ""

    def _clip_sel(self, sel):
        if not sel:
            return None
        a, b = sorted((max(0, int(sel[0])), max(0, int(sel[1]))))
        return (a, b) if b > a else None

    # ── tracks ────────────────────────────────────────────────
    def _put(self, track: Track) -> tuple:
        if track.id == MASTER:
            raise ValueError("the master")
        return tuple(track if t.id == track.id else t for t in self.tracks)

    def set_track(self, tid: int, label: str = "track", merge: str = "", **kw) -> Track | None:
        """Change fields of a track / bus / the master (gain_db, pan, mute, solo, phase, name, output …)."""
        t = self.track(tid)
        if t is None:
            return None
        kw = _clean_track(kw)
        if "output" in kw and kw["output"] != MASTER and not self._can_route(tid, kw["output"]):
            kw.pop("output")
        new = replace(t, **kw)
        if new == t:
            return t
        if tid == MASTER:
            self.apply(label, master=new, merge=merge)
        else:
            self.apply(label, tracks=self._put(new), merge=merge)
        return new

    def add_track(self, name: str, kind: str = "audio", channels: int = 2, at: int | None = None,
                  label: str = "add track", **kw) -> Track:
        if kind not in KINDS:
            raise ValueError(kind)
        if kind == "bus":
            channels = self.channels
        channels = channels if channels in TRACK_CHANNELS else 2
        n = len(self.tracks)
        t = Track(new_id(), name, kind, channels, color=kw.pop("color", n % COLORS), **_clean_track(kw))
        tracks = list(self.tracks)
        tracks.insert(n if at is None else max(0, min(n, at)), t)
        self.apply(label, tracks=tracks)
        return t

    def remove_tracks(self, ids, label: str = "delete track") -> None:
        ids = set(ids) - {MASTER}
        if not ids:
            return
        out = []
        for t in self.tracks:
            if t.id in ids:
                continue
            if t.output in ids or any(s.bus in ids for s in t.sends):
                t = replace(t, output=MASTER if t.output in ids else t.output,
                            sends=tuple(s for s in t.sends if s.bus not in ids))
            out.append(t)
        self.sel_clips = frozenset()
        self.apply(label, tracks=out)

    def move_track(self, tid: int, to: int, label: str = "move track") -> None:
        i = self.index(tid)
        if i < 0:
            return
        tracks = list(self.tracks)
        t = tracks.pop(i)
        tracks.insert(max(0, min(len(tracks), to)), t)
        if tuple(tracks) != self.tracks:
            self.apply(label, tracks=tracks)

    def duplicate_track(self, tid: int, label: str = "duplicate track") -> Track | None:
        t = self.track(tid)
        if t is None or t.id == MASTER:
            return None
        new = replace(t, id=new_id(), name=t.name + " 2", solo=False,
                      clips=tuple(replace(c, id=new_id()) for c in t.clips),
                      fx=tuple(replace(s, id=new_id()) for s in t.fx))
        tracks = list(self.tracks)
        tracks.insert(self.index(tid) + 1, new)
        self.apply(label, tracks=tracks)
        return new

    def _can_route(self, src: int, bus: int) -> bool:
        """A track may go to a bus; a bus to another bus only if that makes no loop."""
        b = self.track(bus)
        if b is None or b.kind != "bus" or bus == src:
            return False
        todo, seen = [bus], set()               # does anything downstream of the bus lead back to src?
        while todo:
            cur = self.track(todo.pop())
            if cur is None or cur.id == MASTER or cur.id in seen:
                continue
            seen.add(cur.id)
            for x in [cur.output] + [s.bus for s in cur.sends]:
                if x == src:
                    return False
                todo.append(x)
        return True

    def can_send(self, src: int, bus: int) -> bool:
        return self._can_route(src, bus)

    def set_send(self, tid: int, bus: int, db: float | None = None, pre: bool | None = None, remove: bool = False,
                 label: str = "send", merge: str = "") -> None:
        t = self.track(tid)
        if t is None or t.id == MASTER:
            return
        sends = [s for s in t.sends if s.bus != bus]
        old = next((s for s in t.sends if s.bus == bus), None)
        if not remove:
            if old is None and not self.can_send(tid, bus):
                return
            s = old or Send(bus)
            s = replace(s, db=clamp_db(s.db if db is None else db), pre=s.pre if pre is None else bool(pre))
            sends.append(s)
            sends.sort(key=lambda s: self.index(s.bus))
        new = replace(t, sends=tuple(sends))
        if new != t:
            self.apply(label, tracks=self._put(new), merge=merge)

    # ── effects ───────────────────────────────────────────────
    def set_fx(self, tid: int, slots, label: str = "effects", merge: str = "") -> None:
        t = self.track(tid)
        if t is None:
            return
        slots = tuple(s if s.id else replace(s, id=new_id()) for s in slots)
        new = replace(t, fx=slots)
        if new == t:
            return
        if tid == MASTER:
            self.apply(label, master=new, merge=merge)
        else:
            self.apply(label, tracks=self._put(new), merge=merge)

    # ── envelopes ─────────────────────────────────────────────
    def set_envelope(self, tid: int, which: str, points, label: str = "envelope", merge: str = "") -> None:
        """which "vol" | "pan"; points (frame, value), any order (one point per frame)."""
        lo, hi = (ENV_MIN_DB, ENV_MAX_DB) if which == "vol" else (-1.0, 1.0)
        pts: dict = {}
        for f, v in points:
            pts[max(0, int(f))] = min(hi, max(lo, float(v)))
        env = tuple(sorted(pts.items()))
        self.set_track(tid, label, merge=merge, **{"vol_env" if which == "vol" else "pan_env": env})

    # ── clips ─────────────────────────────────────────────────
    def _replace_clips(self, label: str, changes: dict, adds: dict | None = None, removes=(), sel=..., merge=""):
        """changes {clip id: Clip or (track id, Clip) to move it to another track}; adds {track id: [Clip]}."""
        removes = set(removes)
        moved: dict = {}
        out = []
        for t in self.tracks:
            clips = []
            for c in t.clips:
                if c.id in removes:
                    continue
                ch = changes.get(c.id)
                if ch is None:
                    clips.append(c)
                elif isinstance(ch, tuple):
                    to, nc = ch
                    if to == t.id:
                        clips.append(nc)
                    else:
                        moved.setdefault(to, []).append(nc)
                else:
                    clips.append(ch)
            out.append([t, clips])
        for t_c in out:
            t, clips = t_c
            for c in (adds or {}).get(t.id, []) + moved.get(t.id, []):
                clips.append(c)
        tracks = tuple(replace(t, clips=tuple(clips)) if tuple(clips) != t.clips else t for t, clips in out)
        if tracks == self.tracks:
            return
        if sel is not ...:
            self.sel_clips = frozenset(sel)
        self.apply(label, tracks=tracks, merge=merge)

    def add_clip(self, tid: int, media_id: str, start: int, length: int, offset: int = 0, name: str = "",
                 label: str = "add clip", xfade: bool = True) -> Clip | None:
        t = self.track(tid)
        if t is None or t.kind != "audio" or length <= 0:
            return None
        c = Clip(new_id(), max(0, int(start)), int(length), (Take(media_id, int(offset), name),), name=name)
        if xfade:
            c = self._overlap_fades(t.clips, c)
        self._replace_clips(label, {}, {tid: [c]}, sel={c.id})
        return c

    def _overlap_fades(self, under, c: Clip, minimum: bool = True) -> Clip:
        """A clip laid over the end of another gets a fade-in as long as the overlap (a crossfade), over the start
        of one a fade-out as long; at least AUTO_XFADE at a seam inside another clip."""
        auto = int(AUTO_XFADE * self.rate) if minimum else 0
        fi, fo = c.fade_in, c.fade_out
        for o in under:
            if o.id == c.id or o.muted:
                continue
            if o.start < c.start < o.end:                       # c starts inside o
                ov = min(o.end, c.end) - c.start
                fi = max(fi, ov if o.end <= c.end else auto)
            if o.start < c.end < o.end:                         # c ends inside o
                ov = c.end - max(o.start, c.start)
                fo = max(fo, ov if o.start >= c.start else auto)
        fi, fo = min(fi, c.length), min(fo, c.length)
        if fi + fo > c.length:
            k = c.length / (fi + fo)
            fi, fo = int(fi * k), c.length - int(fi * k)
        return replace(c, fade_in=fi, fade_out=fo)

    def move_clips(self, ids, delta: int, track_delta: int = 0, label: str = "move clips", copy: bool = False,
                   merge: str = "") -> list:
        """Move (or copy) the clips by delta frames and track_delta audio tracks down. → the new clip ids."""
        ids = set(ids)
        audio = [t for t in self.tracks if t.kind == "audio"]
        pos = {t.id: i for i, t in enumerate(audio)}
        found = [(t, c) for t in audio for c in t.clips if c.id in ids]
        if not found:
            return []
        first = min(c.start for _t, c in found)
        delta = max(-first, int(delta))
        lo = min(pos[t.id] for t, _c in found)
        hi = max(pos[t.id] for t, _c in found)
        track_delta = max(-lo, min(len(audio) - 1 - hi, int(track_delta)))
        changes, adds, new_ids = {}, {}, []
        for t, c in found:
            to = audio[pos[t.id] + track_delta]
            nc = replace(c, start=c.start + delta, id=new_id() if copy else c.id)
            others = [o for o in to.clips if o.id not in ids]
            nc = self._overlap_fades(others, replace(nc, fade_in=c.fade_in, fade_out=c.fade_out))
            new_ids.append(nc.id)
            if copy:
                adds.setdefault(to.id, []).append(nc)
            else:
                changes[c.id] = (to.id, nc)
        self._replace_clips(label, changes, adds, sel=new_ids, merge=merge)
        return new_ids

    def set_clip(self, cid: int, label: str = "clip", merge: str = "", **kw) -> Clip | None:
        _t, c = self.clip(cid)
        if c is None:
            return None
        if "gain_db" in kw:
            kw["gain_db"] = clamp_db(kw["gain_db"], -60.0, 24.0)
        for k in ("fade_in", "fade_out"):
            if k in kw:
                kw[k] = max(0, min(c.length, int(kw[k])))
        if "take" in kw:
            kw["take"] = max(0, min(len(c.takes) - 1, int(kw["take"])))
        for k in ("curve_in", "curve_out"):
            if k in kw and kw[k] not in CURVES:
                kw.pop(k)
        nc = replace(c, **kw)
        if nc.fade_in + nc.fade_out > nc.length:
            if "fade_in" in kw:
                nc = replace(nc, fade_out=nc.length - nc.fade_in)
            else:
                nc = replace(nc, fade_in=nc.length - nc.fade_out)
        if nc != c:
            self._replace_clips(label, {cid: nc}, merge=merge)
        return nc

    def trim_clip(self, cid: int, edge: str, to: int, label: str = "trim clip", merge: str = "",
                  media_frames: int | None = None) -> Clip | None:
        """Move the clip's left or right edge to timeline frame `to` (within its medium: media_frames)."""
        _t, c = self.clip(cid)
        if c is None:
            return None
        to = int(to)
        if edge == "left":
            d = max(-min(k.offset for k in c.takes), min(c.length - 1, to - c.start))
            d = max(d, -c.start)
            takes = tuple(replace(k, offset=k.offset + d) for k in c.takes)
            nc = replace(c, start=c.start + d, length=c.length - d, takes=takes,
                         fade_in=max(0, min(c.fade_in - d, c.length - d)) if d > 0 else c.fade_in)
        else:
            n = max(1, to - c.start)
            if media_frames is not None:
                n = min(n, max(1, media_frames - c.offset))
            nc = replace(c, length=n, fade_out=min(c.fade_out, n))
        if nc.fade_in + nc.fade_out > nc.length:
            nc = replace(nc, fade_out=max(0, nc.length - nc.fade_in))
        if nc != c:
            self._replace_clips(label, {cid: nc}, merge=merge)
        return nc

    def split_clips(self, at: int, ids=None, label: str = "split") -> list:
        """Cut the clips under frame `at` (the selected ones, or all on every track) in two. → the right parts."""
        at = int(at)
        changes, adds, right = {}, {}, []
        for t in self.tracks:
            for c in t.clips:
                if ids is not None and c.id not in ids:
                    continue
                if c.start < at < c.end:
                    d = at - c.start
                    left = replace(c, length=d, fade_out=0, fade_in=min(c.fade_in, d))
                    r = replace(c, id=new_id(), start=at, length=c.length - d, fade_in=0,
                                fade_out=min(c.fade_out, c.length - d),
                                takes=tuple(replace(k, offset=k.offset + d) for k in c.takes))
                    changes[c.id] = left
                    adds.setdefault(t.id, []).append(r)
                    right.append(r.id)
        if changes:
            self._replace_clips(label, changes, adds)
        return right

    def delete_clips(self, ids, label: str = "delete clips") -> None:
        ids = set(ids)
        if ids:
            self._replace_clips(label, {}, removes=ids, sel=())

    def delete_range(self, a: int, b: int, tracks=None, ripple: bool = False, label: str = "delete") -> None:
        """Remove the sound of [a, b) from the clips (of the given track ids, all by default); ripple: what follows
        moves left (and the markers with it)."""
        a, b = sorted((int(a), int(b)))
        if b <= a:
            return
        out = []
        for t in self.tracks:
            if t.kind != "audio" or (tracks is not None and t.id not in tracks):
                out.append(t)
                continue
            clips = []
            for c in t.clips:
                if c.end <= a or c.start >= b:
                    clips.append(replace(c, start=c.start - (b - a)) if ripple and c.start >= b else c)
                    continue
                if c.start < a:
                    clips.append(replace(c, length=a - c.start, fade_out=min(c.fade_out, a - c.start)))
                if c.end > b:
                    d = b - c.start
                    clips.append(replace(c, id=new_id() if c.start < a else c.id,
                                         start=a if ripple else b, length=c.end - b, fade_in=0,
                                         fade_out=min(c.fade_out, c.end - b),
                                         takes=tuple(replace(k, offset=k.offset + d) for k in c.takes)))
            out.append(replace(t, clips=tuple(clips)))
        ms = remap_markers(self.markers, a, b, 0) if ripple else None
        self.apply(label, tracks=out, markers=ms, sel=None, cursor=a)

    def copy_clips(self, ids) -> dict | None:
        """The clipboard: {"clips": [(audio track index from the first, Clip)], "start", "rate"}."""
        ids = set(ids)
        audio = [t for t in self.tracks if t.kind == "audio"]
        found = [(i, c) for i, t in enumerate(audio) for c in t.clips if c.id in ids]
        if not found:
            return None
        top = min(i for i, _c in found)
        start = min(c.start for _i, c in found)
        return {"clips": [(i - top, c) for i, c in found], "start": start, "rate": self.rate,
                "media": {c.takes[k].media for _i, c in found for k in range(len(c.takes))}}

    def copy_range(self, a: int, b: int, tracks=None) -> dict | None:
        """The clipboard of the sound in [a, b) of the tracks (ids; all by default): the clips cut to it."""
        a, b = sorted((int(a), int(b)))
        audio = [t for t in self.tracks if t.kind == "audio" and (tracks is None or t.id in tracks)]
        found = []
        for i, t in enumerate(audio):
            for c in t.clips:
                s, e = max(a, c.start), min(b, c.end)
                if e <= s:
                    continue
                d = s - c.start
                nc = replace(c, start=s, length=e - s, takes=tuple(replace(k, offset=k.offset + d) for k in c.takes),
                             fade_in=max(0, min(e - s, c.fade_in - d)),
                             fade_out=max(0, min(e - s, c.fade_out - (c.end - e))))
                found.append((i, nc))
        if not found:
            return None
        top = min(i for i, _c in found)
        return {"clips": [(i - top, c) for i, c in found], "start": a, "rate": self.rate,
                "media": {k.media for _i, c in found for k in c.takes}}

    def paste_clips(self, clip: dict, at: int, track_id: int, label: str = "paste") -> list:
        audio = [t for t in self.tracks if t.kind == "audio"]
        i0 = next((i for i, t in enumerate(audio) if t.id == track_id), 0)
        adds, new_ids = {}, []
        for di, c in clip["clips"]:
            i = i0 + di
            if i >= len(audio) or c.media not in self.media:
                continue
            to = audio[i]
            nc = replace(c, id=new_id(), start=max(0, int(at) + c.start - clip["start"]))
            nc = self._overlap_fades(to.clips, nc)
            adds.setdefault(to.id, []).append(nc)
            new_ids.append(nc.id)
        if adds:
            self._replace_clips(label, {}, adds, sel=new_ids)
        return new_ids

    def add_take(self, cid: int, take: Take, label: str = "take") -> Clip | None:
        """A new take of a clip (a recording pass over it) — it becomes the active one."""
        _t, c = self.clip(cid)
        if c is None:
            return None
        nc = replace(c, takes=c.takes + (take,), take=len(c.takes))
        self._replace_clips(label, {cid: nc})
        return nc

    def delete_take(self, cid: int, k: int, label: str = "delete take") -> None:
        _t, c = self.clip(cid)
        if c is None or len(c.takes) < 2 or not (0 <= k < len(c.takes)):
            return
        takes = c.takes[:k] + c.takes[k + 1:]
        take = c.take if c.take < k else max(0, c.take - 1)
        self._replace_clips(label, {cid: replace(c, takes=takes, take=take)})

    def replace_media(self, old: str, new: str, offset_map=None, label: str = "replace media") -> None:
        """Every take of medium `old` plays `new` instead (Open clip in editor → saved; a relinked file)."""
        changes = {}
        for t in self.tracks:
            for c in t.clips:
                if any(k.media == old for k in c.takes):
                    changes[c.id] = replace(c, takes=tuple(replace(k, media=new) if k.media == old else k
                                                           for k in c.takes))
        if changes:
            self._replace_clips(label, changes)

    # ── markers and regions ───────────────────────────────────
    def marker(self, mid: int) -> Marker | None:
        return next((m for m in self.markers if m.id == mid), None)

    def add_marker(self, pos: int, name: str, label: str = "add marker") -> Marker:
        m = new_marker(name, max(0, int(pos)))
        self.apply(label, markers=self.markers + (m,))
        return m

    def add_region(self, a: int, b: int, name: str, label: str = "add region") -> Marker | None:
        a, b = sorted((max(0, int(a)), max(0, int(b))))
        if b <= a:
            return None
        m = new_marker(name, a, b)
        self.apply(label, markers=self.markers + (m,))
        return m

    def update_marker(self, mid: int, name=None, pos=None, end=None, label: str = "move marker") -> None:
        m = self.marker(mid)
        if m is None:
            return
        p = m.pos if pos is None else max(0, int(pos))
        e = m.end
        if m.end is not None:
            e = max(0, int(end)) if end is not None else (p + m.length if pos is not None else m.end)
            p, e = sorted((p, e))
            if e <= p:
                return
        new = Marker(m.id, m.name if name is None else str(name), p, e)
        if new != m:
            self.apply(label, markers=tuple(new if x.id == mid else x for x in self.markers))

    def remove_markers(self, ids, label: str = "delete marker") -> None:
        ids = set(ids)
        ms = tuple(m for m in self.markers if m.id not in ids)
        if ms != self.markers:
            self.apply(label, markers=ms)

    def marker_points(self) -> list[int]:
        pts = set()
        for m in self.markers:
            pts.add(m.pos)
            if m.end is not None:
                pts.add(m.end)
        return sorted(pts)

    def regions(self) -> list[Marker]:
        return [m for m in self.markers if m.end is not None]


def _clean_track(kw: dict) -> dict:
    out = dict(kw)
    if "gain_db" in out:
        out["gain_db"] = clamp_db(out["gain_db"])
    if "pan" in out:
        p = float(out["pan"])
        out["pan"] = 0.0 if not math.isfinite(p) else min(1.0, max(-1.0, p))
    for k in ("mute", "solo", "phase", "env_on"):
        if k in out:
            out[k] = bool(out[k])
    if "channels" in out and out["channels"] not in TRACK_CHANNELS:
        out.pop("channels")
    return out


def env_at(env: tuple, frames, default: float):
    """The envelope's values at the frames (numpy array): linear between the points, flat before the first and
    after the last; `default` without points."""
    import numpy as np
    if not env:
        return np.full(len(frames), default, np.float64)
    xs = np.fromiter((p[0] for p in env), np.float64, len(env))
    ys = np.fromiter((p[1] for p in env), np.float64, len(env))
    return np.interp(np.asarray(frames, np.float64), xs, ys)


def env_value(env: tuple, frame: float, default: float) -> float:
    if not env:
        return default
    xs = [p[0] for p in env]
    i = bisect.bisect_right(xs, frame)
    if i == 0:
        return env[0][1]
    if i >= len(env):
        return env[-1][1]
    (x0, y0), (x1, y1) = env[i - 1], env[i]
    return y0 + (y1 - y0) * (frame - x0) / max(1, x1 - x0)
