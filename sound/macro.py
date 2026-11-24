"""Macros (2.0): a list of steps recorded from what was done in the window — editing (selections, cut, copy, paste,
delete, silence, trim), processing, effects (VST3 plugins too), saving — with their parameters, kept as a file
(data folder/macros/<name>.acmacro, JSON), given a hotkey, applied to a file or to its selection, or as a step of a
batch.

What a step works on: the selection as the macro has it at that moment (a «select» step sets it, in seconds from
the start or from the end of the file, so «cut the first 2 s» fits every file); none — the whole file. Changing the
channels or the rate always takes the whole file.

run() changes a Document in place; the window runs a macro on a copy of the document in a thread and puts the result
in as one undo step (window.py), the batch on its own document (batch.py)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import batch, config
from .fx.base import Param

FORMAT_VERSION = 1
EXT = ".acmacro"

EDIT_STEPS = ("select", "select_none", "cut", "copy", "paste", "delete", "silence", "insert_silence", "trim",
              "save")
RANGE_STEPS = ("gain", "normalize", "fade_in", "fade_out", "reverse", "invert", "dc", "trim_silence", "fades")
WHOLE_STEPS = ("channels", "rate", "to_mono", "to_stereo")
STEP_KINDS = EDIT_STEPS + RANGE_STEPS + WHOLE_STEPS + batch.FX_STEPS


def _params() -> dict:
    curve = Param("curve", "choice", default="linear",
                  choices=(("linear", "curve_linear"), ("log", "curve_log"), ("exp", "curve_exp"),
                           ("scurve", "curve_scurve")), label="bs_curve")
    where = (("start", "mc_from_start"), ("end", "mc_from_end"))
    return {
        "select": [Param("a", lo=0.0, hi=86400.0, default=0.0, unit="s", decimals=3, label="mc_a"),
                   Param("a_from", "choice", default="start", choices=where, label="mc_a_from"),
                   Param("b", lo=0.0, hi=86400.0, default=0.0, unit="s", decimals=3, label="mc_b"),
                   Param("b_from", "choice", default="start", choices=where, label="mc_b_from")],
        "insert_silence": [Param("seconds", lo=0.0, hi=3600.0, default=1.0, unit="s", decimals=3, label="mc_seconds")],
        "save": [Param("format", "choice", default="wav", choices=_formats(), label="sv_format"),
                 Param("option", "choice", default="", choices=(("", "mc_option_default"),), label="sv_option"),
                 Param("template", "choice", default="{name}", choices=(("{name}", "{name}"),), label="bt_template"),
                 Param("folder", "choice", default="", choices=(("", "mc_folder_same"),), label="mc_folder")],
        "fade_in": [curve], "fade_out": [curve],
        "to_mono": [Param("mode", "choice", default="mix",
                          choices=(("mix", "tm_mix"), ("left", "tm_left"), ("right", "tm_right")), label="tm_how")],
    }


def _formats():
    from . import media
    return tuple((k, k.upper()) for k in media.FORMATS)


_P: dict | None = None


def step_params(kind: str) -> list:
    """The parameters an editor shows (the text ones of «save» are edited apart)."""
    global _P
    if kind in batch.STEP_KINDS:
        return batch.step_params(kind)
    if _P is None:
        _P = _params()
    return _P.get(kind, [])


def clean_values(kind: str, values: dict | None) -> dict:
    values = values or {}
    if kind in batch.STEP_KINDS:
        return batch.clean_values(kind, {**batch.step_defaults(kind), **values})
    if kind == "save":
        from . import media
        fmt = values.get("format") if values.get("format") in media.FORMATS else "wav"
        return {"format": fmt, "option": str(values.get("option") or ""),
                "template": str(values.get("template") or "{name}"), "folder": str(values.get("folder") or "")}
    out = {}
    for p in step_params(kind):
        out[p.key] = p.clean(values.get(p.key, p.default))
    return out


@dataclass
class Step:
    kind: str
    values: dict = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self):
        if self.kind not in STEP_KINDS:
            raise ValueError(f"unknown macro step {self.kind!r}")
        self.values = clean_values(self.kind, self.values)

    def title(self) -> str:
        from .i18n import t
        if self.kind in batch.STEP_KINDS:
            return batch.Step(self.kind, self.values).title()
        return t("mc_" + self.kind if self.kind in EDIT_STEPS else "p_" + self.kind)

    def summary(self) -> str:
        from .i18n import t
        v, k = self.values, self.kind
        if k in batch.STEP_KINDS:
            return batch.Step(k, self.values).summary()
        if k == "select":
            def end(x, frm):
                return t("mc_at_start" if frm == "start" else "mc_at_end", f"{x:.3f}".rstrip("0").rstrip("."))
            if v["a"] == v["b"] and v["a_from"] == v["b_from"]:
                return t("mc_cursor", end(v["a"], v["a_from"]))
            return f"{end(v['a'], v['a_from'])} … {end(v['b'], v['b_from'])}"
        if k == "insert_silence":
            return f"{v['seconds']:.3f} s"
        if k == "save":
            folder = v["folder"] or t("mc_folder_same")
            return f"{v['format'].upper()}{' ' + v['option'] if v['option'] else ''} — {v['template']} — {folder}"
        if k in ("fade_in", "fade_out"):
            return t("curve_" + v["curve"])
        if k == "to_mono":
            return t("tm_" + v["mode"])
        return ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "values": dict(self.values)}

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(str(d.get("kind")), dict(d.get("values") or {}), bool(d.get("enabled", True)))


@dataclass
class Macro:
    name: str
    steps: list = field(default_factory=list)
    hotkey: str = ""                # a QKeySequence's text, "" — none
    path: str = ""                  # its file (once saved)

    def to_dict(self) -> dict:
        from . import VERSION
        return {"format": "artcore-sound-macro", "version": FORMAT_VERSION, "app": VERSION, "name": self.name,
                "hotkey": self.hotkey, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict, path: str = "") -> "Macro":
        if d.get("format") != "artcore-sound-macro":
            raise ValueError("not a macro")
        steps = []
        for s in d.get("steps") or []:
            try:
                steps.append(Step.from_dict(s))
            except (ValueError, TypeError):
                continue                    # a step of a newer version: left out
        return cls(str(d.get("name") or os.path.splitext(os.path.basename(path))[0]), steps,
                   str(d.get("hotkey") or ""), path)


# ── where they are kept ───────────────────────────────────────
def folder() -> str:
    return os.path.join(config.data_dir(), "macros")


def file_name(name: str) -> str:
    return batch.clean_name(name) + EXT


def load(path: str) -> Macro:
    with open(path, encoding="utf-8") as f:
        return Macro.from_dict(json.load(f), path)


def load_all() -> list[Macro]:
    out = []
    try:
        names = os.listdir(folder())
    except OSError:
        return out
    for n in names:
        if n.lower().endswith(EXT):
            try:
                out.append(load(os.path.join(folder(), n)))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda m: m.name.lower())


def save(m: Macro, path: str = "") -> str:
    path = path or m.path or os.path.join(folder(), file_name(m.name))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m.to_dict(), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    if m.path and os.path.normcase(m.path) != os.path.normcase(path) and \
            os.path.dirname(os.path.normcase(m.path)) == os.path.normcase(folder()):
        try:
            os.remove(m.path)                       # renamed: the old file goes
        except OSError:
            pass
    m.path = path
    return path


def delete(m: Macro) -> None:
    if m.path:
        try:
            os.remove(m.path)
        except OSError:
            pass


def find(name: str) -> Macro | None:
    for m in load_all():
        if m.name == name:
            return m
    return None


# ── playing it back ───────────────────────────────────────────
class Context:
    """What a run keeps between its steps: the clipboard, the file the document came from (for «save»), what
    was saved, the notes."""

    def __init__(self, source_path: str = "", clip=None, date: str = ""):
        self.source_path = source_path
        self.clip = clip
        self.saved: list[str] = []
        self.notes: list[str] = []
        self.date = date or time.strftime("%Y-%m-%d")


def seconds_to(d, sec: float, frm: str) -> int:
    x = int(round(sec * d.rate))
    return max(0, min(d.frames, d.frames - x if frm == "end" else x))


def run(d, steps: list, progress=None, ctx: Context | None = None) -> Context:
    """The steps over the document, one after another (Cancel through progress stops it)."""
    from . import proc
    ctx = ctx or Context()
    todo = [s for s in steps if s.enabled]
    for i, s in enumerate(todo):
        sub = proc.SubProgress(progress, i / max(1, len(todo)), (i + 1) / max(1, len(todo)))
        sub(0.0)
        apply(d, s, sub, ctx)
        sub(1.0)
    return ctx


def _range(d) -> tuple[int, int]:
    if d.sel and d.sel[1] > d.sel[0]:
        return d.sel
    return 0, d.frames


def apply(d, s: Step, progress, ctx: Context) -> None:
    """One step (see the module's doc for what it works on)."""
    from . import chans, ff, fx, proc
    from .i18n import t
    k, v = s.kind, s.values
    n = d.frames
    had = d.sel
    a, b = _range(d)

    def put(src, label, end=None):
        end = b if end is None else end
        d.replace(a, end, src, label, sel=(a, a + src.frames) if had else None)

    if k == "select":
        x, y = seconds_to(d, v["a"], v["a_from"]), seconds_to(d, v["b"], v["b_from"])
        x, y = min(x, y), max(x, y)
        if x == y:
            d.sel, d.cursor = None, x
        else:
            d.sel, d.cursor = (x, y), x
    elif k == "select_none":
        d.sel = None
    elif k == "copy":
        if had:
            ctx.clip = d.copy(*had)
    elif k == "cut":
        if had:
            ctx.clip = d.cut(*had)
    elif k == "delete":
        if had:
            d.delete(*had)
    elif k == "paste":
        if ctx.clip is None:
            ctx.notes.append(t("mc_note_no_clip"))
        elif not d.fits(ctx.clip):
            ctx.notes.append(t("mc_note_clip_other"))
        elif had:
            d.paste(ctx.clip, *had)
        else:
            d.paste(ctx.clip, d.cursor)
    elif k == "silence":
        if had:
            d.silence(*had)
    elif k == "insert_silence":
        m = int(round(v["seconds"] * d.rate))
        if m > 0:
            d.insert_silence(had[0] if had else d.cursor, m)
    elif k == "trim":
        if had:
            d.trim(*had)
    elif k == "save":
        save_step(d, v, progress, ctx)
    elif n <= 0:
        return
    elif k == "gain":
        if v["db"]:
            put(proc.apply_op(d, "gain", a, b, progress, db=v["db"]), "gain")
    elif k == "normalize":
        mode = "loudness" if v["mode"] == "loudness" else "peak"
        try:
            src, res = proc.normalize(d, a, b, mode, v["target"], v["ceiling"], v["mode"] == "true_peak", progress)
        except ValueError:
            ctx.notes.append(t("bt_note_silence"))
            return
        put(src, "normalize")
        if res.limited:
            ctx.notes.append(t("bt_note_limited", f"{res.after:.1f}"))
    elif k in ("fade_in", "fade_out"):
        put(proc.apply_op(d, k, a, b, progress, curve=v["curve"]), k)
    elif k in ("reverse", "invert"):
        put(proc.apply_op(d, k, a, b, progress), k)
    elif k == "dc":
        src, _dc = proc.remove_dc(d, a, b, progress)
        put(src, "dc")
    elif k == "trim_silence":
        edges = proc.sound_edges(d, a, b, v["threshold"], int(round(v["margin"] * d.rate / 1000)))
        if edges is None:
            ctx.notes.append(t("bt_note_all_quiet"))
        elif edges != (a, b):
            d.keep(a, b, *edges, label="trim silence")
    elif k == "fades":
        m = b - a
        fi = min(m, int(round(v["fade_in"] * d.rate / 1000)))
        fo = min(m, int(round(v["fade_out"] * d.rate / 1000)))
        if fi > 1:
            d.replace(a, a + fi, proc.apply_op(d, "fade_in", a, a + fi, progress, curve=v["curve"]), "fade in",
                      sel=had or None)
        if fo > 1:
            d.replace(b - fo, b, proc.apply_op(d, "fade_out", b - fo, b, progress, curve=v["curve"]), "fade out",
                      sel=had or None)
    elif k == "channels":
        ch = int(v["channels"])
        if ch != d.channels:
            lay = ff.layout_for(ch)
            d.replace(0, n, chans.convert(d, ch, v.get("method", "auto"), progress), "channels", sel=None,
                      channels=ch, layout=lay, labels=tuple(ff.labels(ch, lay)))
    elif k == "rate":
        rate = int(v["rate"])
        if rate != d.rate:
            d.replace(0, n, proc.convert_document(d, rate, d.channels, d.layout, progress), "rate", sel=None,
                      rate=rate)
    elif k == "to_mono":
        if d.channels == 2:
            d.replace(0, n, proc.stereo_to_mono(d, v["mode"], progress), "to mono", sel=None, channels=1,
                      layout="mono", labels=("M",))
    elif k == "to_stereo":
        if d.channels == 1:
            d.replace(0, n, proc.mono_to_stereo(d, progress), "to stereo", sel=None, channels=2, layout="stereo",
                      labels=("L", "R"))
    elif k in batch.FX_STEPS:
        eff = fx.get(k)
        values = eff.clean(v) if k == "vst3" else {p.key: v[p.key] for p in eff.params}
        if k == "nr":
            import copy
            path = v.get("profile") or ""
            if not path:
                raise ValueError(t("bt_err_no_profile"))
            eff = copy.copy(eff)
            eff.profile = batch.adapt_noise_profile(batch.load_noise_profile(path), d.rate)
        keep_tail = bool(v.get("keep_tail")) and eff.has_tail
        src = fx.render(d, a, b, eff, values, progress, keep_tail)
        put(src, k, a + src.frames if eff.realtime else b)
    else:
        raise ValueError(f"unknown macro step {k!r}")


def save_path(d, v: dict, ctx: Context) -> str:
    """Where a «save» step writes: its folder ("" — the file's own, else the first one there is) and its name
    ({name} — the file's name without the extension; {date})."""
    from . import media
    src = ctx.source_path or getattr(d, "path", "") or ""
    stem = os.path.splitext(os.path.basename(src))[0] if src else (os.path.splitext(d.name)[0] if d.name else "sound")
    where = v["folder"] or (os.path.dirname(src) if src else os.path.expanduser("~"))
    name = batch.clean_name(batch.render_name(v["template"], stem, 1, ctx.date))
    return os.path.join(where, name + media.FORMATS[v["format"]].ext)


def save_step(d, v: dict, progress, ctx: Context) -> None:
    from . import media
    path = save_path(d, v, ctx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    media.save(d, path, v["format"], v["option"], progress)
    ctx.saved.append(path)


# ── recording ─────────────────────────────────────────────────
class Recorder:
    """While «Record macro» is on, the window tells it each command once it is done (note(), with the place —
    the selection or the cursor — taken just before the command): a place other than the one the last step left
    becomes a «select» step first. Undo during the recording takes the last command back (undo())."""

    def __init__(self):
        self.steps: list[Step] = []
        self._where: tuple = ("none",)
        self._marks: list[int] = []                  # len(steps) before each command (for undo)

    @staticmethod
    def place(d, cursor: bool = False) -> tuple:
        """Where a command acts: the selection, else (cursor=True: it works at the cursor) the cursor, else
        the whole file."""
        if d.sel and d.sel[1] > d.sel[0]:
            return ("sel", d.sel[0], d.sel[1], d.frames, d.rate)
        if cursor:
            return ("cur", d.cursor, d.frames, d.rate)
        return ("none",)

    def note(self, d, kind: str, values: dict | None = None, before: tuple | None = None) -> None:
        start = len(self.steps)
        where = before if before is not None else self.place(d)
        if kind == "save" or kind in WHOLE_STEPS:          # the selection does not matter to these
            where = self._where
        if where != self._where:
            if where[0] == "none":
                if self._where[0] != "cur":              # after a cursor step the whole file is still «none»
                    self.steps.append(Step("select_none"))
            elif where[0] == "sel":
                self.steps.append(Step("select", _select_values(where[1], where[2], where[3], where[4])))
            else:
                self.steps.append(Step("select", _select_values(where[1], where[1], where[2], where[3])))
        self.steps.append(Step(kind, values or {}))
        self._marks.append(start)
        self._where = self.place(d)                       # what the command left

    def undo(self) -> None:
        if self._marks:
            del self.steps[self._marks.pop():]
            self._where = ("?",)                        # unknown now: the next command records its place

    def macro(self, name: str) -> Macro:
        return Macro(name, list(self.steps))


def _select_values(a: int, b: int, frames: int, rate: int) -> dict:
    """A selection as seconds — from the end of the file when it is nearer to it (a fade-out at the end fits
    files of any length)."""
    def one(x):
        if x > frames / 2:
            return round((frames - x) / rate, 6), "end"
        return round(x / rate, 6), "start"
    av, af = one(a)
    bv, bf = one(b)
    return {"a": av, "a_from": af, "b": bv, "b_from": bf}

