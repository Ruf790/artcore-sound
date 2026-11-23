"""Batch processing (1.0): a chain of steps — everything of the Process and Effects menus — run over a folder or a list
of files, each result saved in a chosen format, name and folder. No window and no Qt here: batchui.py is the window.

A Job (source + steps + output + workers) is plain data and goes to / from JSON (the batch presets, *.acbatch).
plan() turns it into Tasks: one per file and sound track, each with its output path decided up front (so the
existing-file policy, «new name» and two files with the same output name are settled before anything runs).

Runner runs the tasks in worker processes (spawn — real parallelism for the effects that keep Python busy, the
noise reduction, the click removal). A worker opens the file as the editor does — decoded into a source on disk —
runs the steps (each step's frames go to a new source, the one before is deleted at once: no history), measures
the loudness and the peaks before and after, and saves through a temporary file that only becomes the result at the
very end. So memory stays a few chunks per worker whatever the length, and a cancelled or failed task leaves no
half-written file. The runner hands out a task only when the disk has room for it next to those running."""
from __future__ import annotations

import copy
import csv
import datetime as _dt
import json
import math
import multiprocessing as mp
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from . import AUDIO_EXT, VIDEO_EXT, VERSION

FORMAT_VERSION = 1
PRESET_EXT = ".acbatch"
NOISE_EXT = ".acnoise"

# ── the steps ─────────────────────────────────────────────────
PROCESS_STEPS = ("gain", "normalize", "dc", "trim_silence", "fades", "channels", "rate")
FX_STEPS = ("eq", "geq", "comp", "limit", "delay", "reverb", "tempo", "nr", "declick", "chmatrix", "dialog")
STEP_KINDS = PROCESS_STEPS + FX_STEPS
STEP_TITLES = {"gain": "p_gain", "normalize": "p_normalize", "dc": "p_dc", "trim_silence": "p_trim_silence",
               "fades": "bs_fades", "channels": "p_channels", "rate": "p_resample"}


def _params() -> dict:
    """The parameters of the Process steps, described as the effects' (fx.Param) so the same form edits them."""
    from .fx.base import Param
    return {
        "gain": [Param("db", lo=-60.0, hi=40.0, default=0.0, unit="dB", decimals=1, label="bs_db")],
        "normalize": [
            Param("mode", "choice", default="loudness",
                  choices=(("loudness", "bs_mode_lufs"), ("peak", "bs_mode_peak"), ("true_peak", "bs_mode_tp")),
                  label="bs_mode"),
            Param("target", lo=-70.0, hi=0.0, default=-23.0, unit="", decimals=1, label="bs_target"),
            Param("ceiling", lo=-20.0, hi=0.0, default=-1.0, unit="dBTP", decimals=1, label="bs_ceiling")],
        "dc": [],
        "trim_silence": [
            Param("threshold", lo=-120.0, hi=0.0, default=-60.0, unit="dBFS", decimals=1, label="ts_threshold"),
            Param("margin", lo=0.0, hi=10000.0, default=50.0, unit="ms", decimals=0, label="ts_margin")],
        "fades": [
            Param("fade_in", lo=0.0, hi=30000.0, default=10.0, unit="ms", decimals=0, label="bs_fade_in"),
            Param("fade_out", lo=0.0, hi=30000.0, default=10.0, unit="ms", decimals=0, label="bs_fade_out"),
            Param("curve", "choice", default="linear",
                  choices=(("linear", "curve_linear"), ("log", "curve_log"), ("exp", "curve_exp"),
                           ("scurve", "curve_scurve")),
                  label="bs_curve")],
        "channels": [Param("channels", "choice", default=2,
                           choices=((1, "ch_mono"), (2, "ch_stereo"), (6, "bs_ch_51")), label="chn_to"),
                     Param("method", "choice", default="auto",
                           choices=(("auto", "mx_plain"), ("itu", "mx_itu"), ("loro", "mx_loro"), ("pl2", "mx_pl2"),
                                    ("upmix", "mx_upmix")), label="mx_method")],
        "rate": [Param("rate", "choice", default=48000,
                       choices=tuple((r, f"{r} Hz") for r in (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200,
                                                             96000, 176400, 192000)), label="rs_rate")],
    }


_P: dict | None = None


def step_params(kind: str) -> list:
    global _P
    if kind in FX_STEPS:
        from . import fx
        return fx.get(kind).params
    if _P is None:
        _P = _params()
    return _P[kind]


def step_defaults(kind: str) -> dict:
    v = {p.key: p.default for p in step_params(kind)}
    if kind == "nr":
        v["profile"] = ""
    if kind in ("delay", "reverb"):
        v["keep_tail"] = True
    return v


def clean_values(kind: str, values: dict | None) -> dict:
    values = values or {}
    out = {p.key: p.clean(values.get(p.key, p.default)) for p in step_params(kind)}
    if kind == "nr":
        out["profile"] = str(values.get("profile") or "")
    if kind in ("delay", "reverb"):
        out["keep_tail"] = bool(values.get("keep_tail", True))
    return out


@dataclass
class Step:
    kind: str
    values: dict = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self):
        if self.kind not in STEP_KINDS:
            raise ValueError(f"unknown step {self.kind!r}")
        self.values = clean_values(self.kind, {**step_defaults(self.kind), **(self.values or {})})

    def title(self) -> str:
        from .i18n import t
        if self.kind in FX_STEPS:
            from . import fx
            return t(fx.get(self.kind).name)
        return t(STEP_TITLES[self.kind])

    def summary(self) -> str:
        """A short line of the step's settings for the chain list."""
        from .i18n import t
        v = self.values
        k = self.kind
        if k == "gain":
            return f"{v['db']:+.1f} dB"
        if k == "normalize":
            if v["mode"] == "loudness":
                return t("bs_sum_lufs", f"{v['target']:.1f}", f"{v['ceiling']:.1f}")
            return f"{t('bs_mode_' + ('peak' if v['mode'] == 'peak' else 'tp'))} {v['target']:.1f} dB"
        if k == "trim_silence":
            return f"{v['threshold']:.0f} dBFS, {v['margin']:.0f} ms"
        if k == "fades":
            return t("bs_sum_fades", f"{v['fade_in']:.0f}", f"{v['fade_out']:.0f}")
        if k == "channels":
            base = {1: t("ch_mono"), 2: t("ch_stereo"), 6: "5.1"}[v["channels"]]
            return base if v.get("method", "auto") == "auto" else f"{base}, {t('mx_' + v['method'])}"
        if k == "rate":
            return f"{v['rate']} Hz"
        if k == "nr":
            return os.path.basename(v.get("profile") or "") or t("bs_no_profile")
        if k == "tempo":
            from . import fx
            return fx.get(k).label(v)
        return ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "values": dict(self.values)}

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(str(d.get("kind")), dict(d.get("values") or {}), bool(d.get("enabled", True)))


# ── the job ───────────────────────────────────────────────────
@dataclass
class Source:
    mode: str = "folder"               # folder | list
    folder: str = ""
    recursive: bool = True
    extensions: str = ""               # «wav flac mp3» ("" = every supported format)
    files: list = field(default_factory=list)
    tracks: str = "default"            # default | lang | all
    lang: str = "rus"                  # for tracks = lang: «rus, ru»


@dataclass
class Output:
    format: str = "flac"               # media.FORMATS key, or "source" (as the file, where it can)
    option: str = ""                   # "" = the format's default
    folder_mode: str = "same"          # same | other
    folder: str = ""
    keep_structure: bool = True        # other folder: the subfolders of the source folder too
    template: str = "{name}"
    suffix: str = "_processed"
    exists: str = "rename"             # skip | replace | rename
    keep_tags: bool = True
    keep_markers: bool = True


@dataclass
class Job:
    source: Source = field(default_factory=Source)
    steps: list = field(default_factory=list)
    output: Output = field(default_factory=Output)
    workers: int = 0                   # 0 = cpu count / 2
    measure: bool = True               # loudness and peaks before and after

    def to_dict(self) -> dict:
        return {"app": "ArtCore Sound", "kind": "batch", "version": FORMAT_VERSION, "source": asdict(self.source),
                "steps": [s.to_dict() for s in self.steps], "output": asdict(self.output),
                "workers": self.workers, "measure": self.measure}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        if not isinstance(d, dict) or d.get("kind", "batch") != "batch":
            raise ValueError("not a batch")
        src = Source(**{k: v for k, v in (d.get("source") or {}).items() if k in Source.__dataclass_fields__})
        out = Output(**{k: v for k, v in (d.get("output") or {}).items() if k in Output.__dataclass_fields__})
        steps = []
        for s in d.get("steps") or []:
            try:
                steps.append(Step.from_dict(s))
            except (ValueError, TypeError):
                pass                                 # a step of a newer version: left out
        return cls(src, steps, out, int(d.get("workers") or 0), bool(d.get("measure", True)))

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Job":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def presets_dir() -> str:
    from . import config
    p = os.path.join(config.data_dir(), "batch")
    os.makedirs(p, exist_ok=True)
    return p


def list_presets() -> list[str]:
    try:
        return sorted(os.path.splitext(n)[0] for n in os.listdir(presets_dir()) if n.endswith(PRESET_EXT))
    except OSError:
        return []


# ── the noise sample in a file ────────────────────────────────
def save_noise_profile(path: str, prof: dict) -> None:
    with open(path, "wb") as f:
        np.savez(f, rate=prof["rate"], channels=prof["channels"], n=prof["n"], power=prof["power"],
                 frames=prof.get("frames", 0), seconds=prof.get("seconds", 0.0))


def load_noise_profile(path: str) -> dict:
    with np.load(path, allow_pickle=False) as z:
        prof = {"rate": int(z["rate"]), "channels": int(z["channels"]), "n": int(z["n"]),
                "power": np.array(z["power"], np.float64), "frames": int(z["frames"]),
                "seconds": float(z["seconds"])}
    if prof["power"].ndim != 2 or prof["power"].shape[0] != prof["n"] // 2 + 1:
        raise ValueError("not a noise sample")
    return prof


def adapt_noise_profile(prof: dict, rate: int) -> dict:
    """The sample for a file of another rate: the power per frequency interpolated onto that rate's bins (and
    scaled to its frame length — the power of a noise frame grows with the frame)."""
    from .fx.restore import frame_size
    n = frame_size(rate)
    if prof["n"] == n and prof["rate"] == rate:
        return prof
    pw = prof["power"]
    f_old = np.arange(pw.shape[0]) * prof["rate"] / prof["n"]
    f_new = np.arange(n // 2 + 1) * rate / n
    out = np.empty((n // 2 + 1, pw.shape[1]))
    for c in range(pw.shape[1]):
        out[:, c] = np.interp(f_new, f_old, pw[:, c], right=pw[-1, c])
    out *= n / prof["n"]
    return {**prof, "rate": rate, "n": n, "power": out}


# ── planning ──────────────────────────────────────────────────
@dataclass
class Task:
    id: int
    n: int                             # the file's number in the list (1-based, {n})
    src: str
    rel: str = ""                      # the subfolder of the source folder it is in
    track: int = 0                     # the audio track (pos)
    track_text: str = ""
    out: str = ""
    fmt: str = ""
    option: str = ""
    status: str = "wait"               # wait | run | ok | error | skip | cancel
    error: str = ""
    notes: list = field(default_factory=list)
    duration: float = 0.0              # seconds of sound
    need: int = 0                      # bytes of working files it needs
    before: dict = field(default_factory=dict)       # {"lufs", "peak", "tp"} in LUFS / dBFS / dBTP
    after: dict = field(default_factory=dict)
    seconds: float = 0.0               # how long it took
    frac: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def wanted_extensions(text: str) -> tuple:
    exts = tuple("." + e.strip().lower().lstrip(".") for e in re.split(r"[\s,;]+", text or "") if e.strip())
    return exts or AUDIO_EXT + VIDEO_EXT


def _natural(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def collect(src: Source) -> list[tuple[str, str]]:
    """The files of the source → [(path, subfolder relative to the source folder)] in a natural order."""
    if src.mode == "list":
        out, seen = [], set()
        for p in src.files:
            p = os.path.abspath(p)
            if p not in seen and os.path.isfile(p):
                seen.add(p)
                out.append((p, ""))
        return out
    root = os.path.abspath(src.folder or ".")
    exts = wanted_extensions(src.extensions)
    found = []
    if src.recursive:
        for dp, dns, fns in os.walk(root):
            dns.sort(key=_natural)
            for fn in fns:
                if fn.lower().endswith(exts) and ".artcore-part" not in fn:
                    found.append((os.path.join(dp, fn), os.path.relpath(dp, root)))
    else:
        try:
            for fn in os.listdir(root):
                p = os.path.join(root, fn)
                if os.path.isfile(p) and fn.lower().endswith(exts) and ".artcore-part" not in fn:
                    found.append((p, "."))
        except OSError:
            pass
    found.sort(key=lambda x: (_natural(x[1]), _natural(os.path.basename(x[0]))))
    return [(p, "" if r == "." else r) for p, r in found]


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(s: str) -> str:
    s = _BAD.sub("_", s).strip().rstrip(". ")
    return s or "_"


def render_name(template: str, name: str, n: int, date: str, track: int = 1, lang: str = "") -> str:
    """{name}, {n} / {n:3} (padded), {date}, {track}, {lang}; anything else in braces stays as it is."""
    def sub(m):
        key, _, width = m.group(1).partition(":")
        key = key.strip().lower()
        if key == "name":
            return name
        if key == "n":
            w = int(width) if width.strip().isdigit() else 0
            return str(n).zfill(w)
        if key == "date":
            return date
        if key == "track":
            return str(track)
        if key == "lang":
            return lang or "und"
        return m.group(0)
    return re.sub(r"\{([^{}]*)\}", sub, template or "{name}")


def pick_tracks(info, src: Source) -> tuple[list, str]:
    """The tracks of a file to process → (tracks, a note when the language was not found)."""
    tracks = info.tracks
    default = next((t for t in tracks if t.default), tracks[0])
    if src.tracks == "all":
        return list(tracks), ""
    if src.tracks == "lang":
        langs = [x.strip().lower() for x in re.split(r"[\s,;]+", src.lang or "") if x.strip()]
        hit = next((t for t in tracks if (t.lang or "").lower() in langs), None)
        if hit is not None:
            return [hit], ""
        return [default], "lang"
    return [default], ""


def output_format(out: Output, path: str, track, info) -> tuple[str, str]:
    from . import media
    fmt, opt = out.format, out.option
    if fmt == "source":
        f, o = media.format_of(path, track, info)
        if not f:
            return "flac", media.default_option("flac", track.channels, track.bits)
        return f, (o or {}).get("option") or media.default_option(f, track.channels, track.bits)
    if fmt not in media.FORMATS:
        fmt = "flac"
    if not opt or opt not in media.FORMATS[fmt].options:
        opt = media.default_option(fmt, track.channels, track.bits)
    return fmt, opt


def plan(job: Job, progress=None, cancelled=lambda: False) -> list[Task]:
    """The tasks of the job, their output paths settled (probes every file; a file ffprobe cannot read becomes a
    task with its error)."""
    from . import ff, media
    from .errors import human
    from .i18n import t
    files = collect(job.source)
    date = _dt.date.today().isoformat()
    out = job.output
    sources = {os.path.normcase(p) for p, _ in files}
    taken: set = set()
    tasks: list[Task] = []
    for i, (path, rel) in enumerate(files, 1):
        if cancelled():
            break
        if progress:
            progress(i / max(1, len(files)))
        try:
            info = ff.probe(path)
        except Exception as e:                                    # noqa: BLE001 — a broken file is a task with an error
            tasks.append(Task(len(tasks), i, path, rel, status="error", error=human(e)))
            continue
        tracks, note = pick_tracks(info, job.source)
        many = len(tracks) > 1
        for tr in tracks:
            fmt, opt = output_format(out, path, tr, info)
            stem = os.path.splitext(os.path.basename(path))[0]
            tmpl = out.template or "{name}"
            if many and "{track" not in tmpl and "{lang" not in tmpl:
                tmpl += "_a{track}"
            name = clean_name(render_name(tmpl, stem, i, date, tr.pos + 1, tr.lang) + (out.suffix or ""))
            if out.folder_mode == "other" and out.folder:
                folder = os.path.abspath(out.folder)
                if out.keep_structure and job.source.mode == "folder" and rel:
                    folder = os.path.join(folder, rel)
            else:
                folder = os.path.dirname(path)
            target = os.path.join(folder, name + media.FORMATS[fmt].ext)
            task = Task(len(tasks), i, path, rel, tr.pos, track_text(tr), target, fmt, opt,
                        duration=tr.duration or info.duration, need=int(media.needed_bytes(tr) * 2.2) + (64 << 20))
            if note:
                task.notes.append(t("bt_note_lang", job.source.lang))
            key = os.path.normcase(os.path.abspath(target))
            own = key == os.path.normcase(os.path.abspath(path))
            if key in taken:                                   # another task of this batch writes that name
                rename = True
            elif os.path.exists(target) or key in sources:
                if out.exists == "skip":
                    task.status = "skip"
                    task.notes.append(t("bt_note_exists"))
                # replacing is fine for the file itself (read before it is written), never for another source
                rename = out.exists == "rename" or (out.exists == "replace" and key in sources and not own)
            else:
                rename = False
            if rename:
                task.out = target = unique_path(target, taken | sources)
                key = os.path.normcase(os.path.abspath(target))
            taken.add(key)
            tasks.append(task)
    return tasks


def unique_path(path: str, taken: set) -> str:
    """«name (2).ext», «name (3).ext» … — the first that neither exists nor is taken by another task."""
    stem, ext = os.path.splitext(path)
    k = 2
    while True:
        p = f"{stem} ({k}){ext}"
        if not os.path.exists(p) and os.path.normcase(os.path.abspath(p)) not in taken:
            return p
        k += 1


def track_text(tr) -> str:
    from . import ff
    parts = [f"#{tr.pos + 1}", ff.codec_name(tr), f"{tr.channels} ch", f"{tr.rate // 1000 if tr.rate % 1000 == 0 else tr.rate / 1000} kHz"]
    if tr.lang:
        parts.append(tr.lang)
    return " · ".join(str(p) for p in parts)


# ── running one task (in a worker) ────────────────────────────
def _drop_history(d) -> None:
    """Forget the undo history and delete the working files of the sources nothing uses any more."""
    keep = {id(s) for s, _o, _n in d.pieces}
    old = [s for s in d.sources() if id(s) not in keep]
    d.history = [d.history[d.pos]]
    d.pos = 0
    d.orphans = []
    from .store import delete_source
    for s in old:
        delete_source(s)


def apply_step(d, step: Step, progress, notes: list | None = None, profiles: dict | None = None) -> None:
    """One step over the whole document (as the menu command with nothing selected)."""
    from . import ff, fx, proc
    from .i18n import t
    k, v = step.kind, step.values
    notes = notes if notes is not None else []
    n = d.frames
    if n <= 0:
        return
    if k == "gain":
        if v["db"]:
            d.replace(0, n, proc.apply_op(d, "gain", 0, n, progress, db=v["db"]), "gain")
    elif k == "normalize":
        mode = "loudness" if v["mode"] == "loudness" else "peak"
        try:
            src, res = proc.normalize(d, 0, n, mode, v["target"], v["ceiling"], v["mode"] == "true_peak", progress)
        except ValueError:
            notes.append(t("bt_note_silence"))
            return
        d.replace(0, n, src, "normalize")
        if res.limited:
            notes.append(t("bt_note_limited", f"{res.after:.1f}"))
    elif k == "dc":
        src, _dc = proc.remove_dc(d, 0, n, progress)
        d.replace(0, n, src, "dc")
    elif k == "trim_silence":
        edges = proc.sound_edges(d, 0, n, v["threshold"], int(round(v["margin"] * d.rate / 1000)))
        if edges is None:
            notes.append(t("bt_note_all_quiet"))
        elif edges != (0, n):
            d.keep(0, n, *edges, label="trim silence")
    elif k == "fades":
        fi = min(n, int(round(v["fade_in"] * d.rate / 1000)))
        fo = min(n, int(round(v["fade_out"] * d.rate / 1000)))
        if fi > 1:
            d.replace(0, fi, proc.apply_op(d, "fade_in", 0, fi, progress, curve=v["curve"]), "fade in", sel=None)
        if fo > 1:
            d.replace(n - fo, n, proc.apply_op(d, "fade_out", n - fo, n, progress, curve=v["curve"]), "fade out",
                      sel=None)
    elif k == "channels":
        ch = int(v["channels"])
        if ch != d.channels:
            lay = ff.layout_for(ch)
            from . import chans
            src = chans.convert(d, ch, v.get("method", "auto"), progress)
            d.replace(0, n, src, "channels", channels=ch, layout=lay, labels=tuple(ff.labels(ch, lay)))
    elif k == "rate":
        rate = int(v["rate"])
        if rate != d.rate:
            d.replace(0, n, proc.convert_document(d, rate, d.channels, d.layout, progress), "rate", rate=rate)
    else:
        eff = fx.get(k)
        values = {p.key: v[p.key] for p in eff.params}
        if k == "nr":
            path = v.get("profile") or ""
            if not path:
                raise ValueError(t("bt_err_no_profile"))
            prof = (profiles or {}).get(path) if profiles is not None else None
            if prof is None:
                prof = load_noise_profile(path)
                if profiles is not None:
                    profiles[path] = prof
            eff = copy.copy(eff)
            eff.profile = adapt_noise_profile(prof, d.rate)
        keep_tail = bool(v.get("keep_tail")) and eff.has_tail
        info: dict = {}
        src = fx.render(d, 0, n, eff, values, progress, keep_tail, info)
        end = src.frames if eff.realtime else n
        d.replace(0, end, src, k, sel=None)
        if k == "declick" and info.get("proc") is not None:
            notes.append(t("bt_note_clicks", info["proc"].found))
    _drop_history(d)


def measure(d, progress) -> dict:
    from . import analysis
    st = analysis.analyze(d, 0, d.frames, progress, loudness=True, true_peak=True)
    peak = max((c.peak for c in st.channels), default=0.0)
    tp = max((c.true_peak for c in st.channels), default=0.0)
    return {"lufs": _num(st.integrated), "peak": _num(analysis.to_db(peak)), "tp": _num(analysis.to_db(tp))}


def _num(v: float):
    return round(float(v), 2) if math.isfinite(v) else None


class _Ctl:
    """A task's Progress inside a worker: reports the fraction, waits while paused, stops on cancel."""

    def __init__(self, tid: int, report, cancel_ev, pause_ev):
        self.tid, self.report, self.cancel_ev, self.pause_ev = tid, report, cancel_ev, pause_ev
        self.cancelled = False
        self._last = 0.0

    def __call__(self, frac: float) -> None:
        from .media import Cancelled
        while self.pause_ev is not None and self.pause_ev.is_set() and not self.cancel_ev.is_set():
            time.sleep(0.1)
        if self.cancel_ev.is_set():
            self.cancelled = True
            raise Cancelled()
        now = time.monotonic()
        if now - self._last > 0.2 or frac >= 1.0:
            self._last = now
            self.report(self.tid, float(frac))

    def cancel(self) -> None:
        self.cancel_ev.set()


def run_task(task: dict, job: dict, session, cancel_ev, pause_ev=None, report=lambda _i, _f: None,
             profiles: dict | None = None) -> dict:
    """Open, process, measure and save one task → the task's dict with status, figures and time filled in."""
    from . import media, proc
    from .errors import human
    from .media import Cancelled
    task = dict(task)
    jb = Job.from_dict(job)
    steps = [s for s in jb.steps if s.enabled]
    t0 = time.monotonic()
    ctl = _Ctl(task["id"], report, cancel_ev, pause_ev)
    # the weights of the passes: open 25 %, measuring 2 × 10 %, the steps 40 %, saving 15 %
    w_open, w_meas, w_save = 0.25, (0.1 if jb.measure else 0.0), 0.15
    w_steps = 1.0 - w_open - 2 * w_meas - w_save
    d = None
    try:
        folder = session.folder()
        d = media.open_file(task["src"], folder, task["track"], proc.SubProgress(ctl, 0.0, w_open))
        task["duration"] = d.duration()
        pos = w_open
        if jb.measure:
            task["before"] = measure(d, proc.SubProgress(ctl, pos, pos + w_meas))
            pos += w_meas
        per = w_steps / max(1, len(steps))
        notes = list(task.get("notes") or [])
        for s in steps:
            apply_step(d, s, proc.SubProgress(ctl, pos, pos + per), notes, profiles)
            pos += per
        if jb.measure:
            task["after"] = measure(d, proc.SubProgress(ctl, pos, pos + w_meas))
            pos += w_meas
        os.makedirs(os.path.dirname(task["out"]) or ".", exist_ok=True)
        tags = None if jb.output.keep_tags else {}
        media.save(d, task["out"], task["fmt"], task["option"], proc.SubProgress(ctl, pos, 1.0), tags=tags,
                   markers=jb.output.keep_markers)
        task["notes"] = notes
        task["status"] = "ok"
    except Cancelled:
        task["status"] = "cancel"
    except Exception as e:                                       # noqa: BLE001 — a failed file is a line of the log
        task["status"] = "error"
        task["error"] = human(e)
    finally:
        if d is not None:
            try:
                d.close()
            except Exception:                                     # noqa: BLE001
                pass
    task["seconds"] = round(time.monotonic() - t0, 2)
    task["frac"] = 1.0
    return task


# ── the worker processes ──────────────────────────────────────
def _worker_main(tq, rq, cancel_ev, pause_ev, work_root: str, env: dict) -> None:
    """A worker process: takes (task, job) from tq until None, puts ("hello" | "prog" | "done", …) to rq."""
    os.environ.update(env)
    from . import store
    from .fx import base as fxbase
    from .i18n import set_lang
    set_lang(env.get("ARTCORE_SOUND_LANG", "ru"))
    fxbase.FFT_WORKERS = 1                     # the files run in parallel already
    try:
        sess = store.Session(work_root, clean=False)
    except Exception as e:                                       # noqa: BLE001
        rq.put(("fatal", os.getpid(), str(e)))
        return
    rq.put(("hello", os.getpid(), sess.path))
    profiles: dict = {}

    def report(tid, frac):
        rq.put(("prog", tid, frac))
    try:
        while True:
            item = tq.get()
            if item is None:
                break
            task, job = item
            rq.put(("start", task["id"], os.getpid()))
            res = run_task(task, job, sess, cancel_ev, pause_ev, report, profiles)
            rq.put(("done", task["id"], res))
    finally:
        sess.close()


def part_files(task: Task) -> list[str]:
    """The temporary files a task's saving may leave if its worker is killed."""
    from . import media
    tmp = media.part_path(task.out, task.fmt)
    return [tmp, tmp + ".ffmeta"]


class Runner:
    """Runs a job in the background: plan, then the worker processes. The window polls snapshot(); pause(),
    cancel() from any thread. events: a list of (kind, task id) the window may read (start / done)."""

    def __init__(self, job: Job, work_root: str | None = None, workers: int | None = None, lang: str = ""):
        from . import config
        from .i18n import lang as cur_lang
        self.job = job
        self.work_root = work_root or config.work_root()
        n = workers if workers is not None else (job.workers or default_workers())
        self.workers = max(1, int(n))
        self.lang = lang or cur_lang()
        self.tasks: list[Task] = []
        self.phase = "idle"              # idle | plan | run | done | cancelled
        self.plan_frac = 0.0
        self.started = self.finished = 0.0
        self.error = ""
        self._lock = threading.Lock()
        self._ctx = mp.get_context("spawn")
        self._cancel = self._ctx.Event()
        self._pause = self._ctx.Event()
        self._thread: threading.Thread | None = None
        self._procs: list = []
        self._sessions: list = []
        self.free_space = None           # tests: a function (path) → bytes

    # control
    def start(self) -> None:
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, name="batch", daemon=True)
        self._thread.start()

    def pause(self, on: bool) -> None:
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict:
        """What the window shows: phase, the overall fraction, counts, the tasks (copies)."""
        with self._lock:
            tasks = [copy.copy(t) for t in self.tasks]
        total = sum(max(1.0, t.duration) for t in tasks) or 1.0
        done = sum(max(1.0, t.duration) * (1.0 if t.status in ("ok", "error", "skip", "cancel") else t.frac)
                   for t in tasks)
        counts = {s: sum(1 for t in tasks if t.status == s) for s in ("wait", "run", "ok", "error", "skip", "cancel")}
        frac = self.plan_frac * 0.02 if self.phase == "plan" else done / total
        return {"phase": self.phase, "frac": frac, "counts": counts, "tasks": tasks, "paused": self.paused,
                "elapsed": (self.finished or time.time()) - self.started if self.started else 0.0}

    # the work
    def _free(self) -> int:
        if self.free_space is not None:
            return self.free_space(self.work_root)
        from .winint import free_space
        return free_space(self.work_root)

    def _run(self) -> None:
        from .i18n import set_lang, t
        set_lang(self.lang)
        try:
            self.phase = "plan"

            def pp(f):
                self.plan_frac = f
            tasks = plan(self.job, pp, lambda: self._cancel.is_set())
            with self._lock:
                self.tasks = tasks
            if self._cancel.is_set():
                self._finish_cancelled()
                return
            self.phase = "run"
            todo = [x for x in tasks if x.status == "wait"]
            if todo:
                self._run_workers(todo, t)
        except Exception as e:                                   # noqa: BLE001
            from .errors import human
            self.error = human(e)
        finally:
            if self._cancel.is_set():
                self._finish_cancelled()
            elif self.phase != "cancelled":
                self.phase = "done"
            self.finished = time.time()

    def _finish_cancelled(self) -> None:
        with self._lock:
            for x in self.tasks:
                if x.status in ("wait", "run"):
                    x.status = "cancel"
        self.phase = "cancelled"

    def _run_workers(self, todo: list[Task], t) -> None:
        import collections
        ctx = self._ctx
        tq, rq = ctx.Queue(), ctx.Queue()
        n = min(self.workers, len(todo))
        env = {k: v for k, v in os.environ.items() if k.startswith(("ARTCORE_", "QT_"))}
        env["ARTCORE_SOUND_LANG"] = self.lang
        for _ in range(n):
            p = ctx.Process(target=_worker_main, args=(tq, rq, self._cancel, self._pause, self.work_root, env),
                            daemon=True)
            p.start()
            self._procs.append(p)
        job = self.job.to_dict()
        pending = collections.deque(todo)
        inflight: dict = {}            # task id → Task
        reserved = 0
        idle = 0
        alive = n
        by_id = {x.id: x for x in todo}
        try:
            while (pending or inflight) and alive:
                if self._cancel.is_set():
                    break
                while idle and pending and not self._pause.is_set():
                    task = pending[0]
                    free = self._free()
                    if free >= 0 and reserved + task.need > free:
                        if inflight:
                            break                       # wait until a running task frees its working files
                        pending.popleft()
                        with self._lock:
                            task.status = "error"
                            task.error = t("bt_err_space", _gb(task.need), _gb(free))
                        continue
                    pending.popleft()
                    with self._lock:
                        task.status = "run"
                    inflight[task.id] = task
                    reserved += task.need
                    idle -= 1
                    tq.put((task.to_dict(), job))
                try:
                    msg = rq.get(timeout=0.2)
                except queue.Empty:
                    alive = sum(1 for p in self._procs if p.is_alive())
                    if not alive:
                        break
                    continue
                kind = msg[0]
                if kind == "hello":
                    self._sessions.append(msg[2])
                    idle += 1
                elif kind == "fatal":
                    alive -= 1
                    self.error = msg[2]
                elif kind == "prog":
                    x = by_id.get(msg[1])
                    if x is not None:
                        x.frac = msg[2]
                elif kind == "done":
                    res = msg[2]
                    x = inflight.pop(msg[1], None)
                    if x is not None:
                        reserved -= x.need
                        with self._lock:
                            for k, v in res.items():
                                if k in Task.__dataclass_fields__:
                                    setattr(x, k, v)
                    idle += 1
        finally:
            for _ in self._procs:
                tq.put(None)
            deadline = time.time() + (20 if self._cancel.is_set() else 60)
            for p in self._procs:
                p.join(max(0.1, deadline - time.time()))
            for p in self._procs:
                if p.is_alive():
                    p.terminate()
                    p.join(5)
            # whatever a killed worker left: its parts of the results, its working folder
            for x in list(inflight.values()):
                for f in part_files(x):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            from .store import remove_folder
            for s in self._sessions:
                if os.path.isdir(s):
                    remove_folder(s)
            for q in (tq, rq):
                try:
                    q.close()
                    q.join_thread()
                except Exception:                                # noqa: BLE001
                    pass
            if inflight and not self._cancel.is_set():
                with self._lock:
                    for x in inflight.values():
                        if x.status == "run":
                            x.status = "error"
                            x.error = t("bt_err_worker")


def _gb(n: int) -> str:
    from .i18n import fmt_bytes
    return fmt_bytes(max(0, n))


# ── the report ────────────────────────────────────────────────
def _fmt(v, dec: int = 1) -> str:
    return "" if v is None else f"{v:.{dec}f}"


def _dur(sec: float) -> str:
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


COLUMNS = ("n", "src", "track", "out", "status", "error", "duration", "lufs_before", "lufs_after", "peak_before",
           "peak_after", "tp_before", "tp_after", "seconds")


def rows(tasks: list[Task]) -> list[list[str]]:
    from .i18n import t
    out = []
    for x in tasks:
        out.append([str(x.n), x.src, x.track_text, x.out if x.status == "ok" else "", t("bst_" + x.status),
                    "; ".join(filter(None, [x.error] + list(x.notes))), _dur(x.duration),
                    _fmt(x.before.get("lufs")), _fmt(x.after.get("lufs")), _fmt(x.before.get("peak")),
                    _fmt(x.after.get("peak")), _fmt(x.before.get("tp")), _fmt(x.after.get("tp")),
                    f"{x.seconds:.1f}"])
    return out


def summary(tasks: list[Task], elapsed: float) -> dict:
    c = {s: sum(1 for x in tasks if x.status == s) for s in ("ok", "error", "skip", "cancel", "wait", "run")}
    c["total"] = len(tasks)
    c["elapsed"] = elapsed
    c["audio"] = sum(x.duration for x in tasks if x.status == "ok")
    return c


def save_csv(tasks: list[Task], path: str) -> None:
    from .i18n import t
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([t("bc_" + c) for c in COLUMNS])
        w.writerows(rows(tasks))


def save_txt(tasks: list[Task], path: str, elapsed: float, job: Job | None = None) -> None:
    from .i18n import t
    s = summary(tasks, elapsed)
    lines = [f"ArtCore Sound {VERSION} — {t('bt_title')}", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
    if job is not None:
        chain = [st.title() + (f" ({st.summary()})" if st.summary() else "") for st in job.steps if st.enabled]
        lines.append(t("bt_rep_chain", " → ".join(chain) or "—"))
        lines.append("")
    lines.append(t("bt_summary", s["ok"], s["error"], s["skip"], s["cancel"], _dur(s["elapsed"]), _dur(s["audio"])))
    lines.append("")
    for r in rows(tasks):
        n, src, track, out, status, err, dur, lb, la, pb, pa, tb, ta, sec = r
        lines.append(f"[{n}] {status} · {src}" + (f" [{track}]" if track else ""))
        if out:
            lines.append(f"     → {out}")
        if lb or la or pb or pa:
            lines.append("     " + t("bt_rep_levels", lb or "—", la or "—", pb or "—", pa or "—", tb or "—", ta or "—"))
        lines.append(f"     {t('bc_duration')}: {dur} · {t('bc_seconds')}: {sec}")
        if err:
            lines.append(f"     {err}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
