"""Tempo without changing the pitch and pitch without changing the tempo, through ffmpeg: the rubberband filter
(librubberband: phase vocoder with transient detection; the formants kept on request), or where the build lacks it
atempo (WSOLA) for the tempo and asetrate + aresample + atempo for the pitch. The length of the range changes with the
tempo: n frames at +25 % become n / 1.25."""
from __future__ import annotations

from .. import ff, store
from ..media import Progress
from .base import Effect, Param


def factors(p: dict) -> tuple[float, float]:
    """(tempo, pitch) as ratios: +25 % → 1.25, +12 semitones → 2.0."""
    tempo = 1.0 + p["tempo"] / 100.0
    pitch = 2.0 ** ((p["semitones"] + p["cents"] / 100.0) / 12.0)
    return tempo, pitch


def has_rubberband() -> bool:
    return "rubberband" in ff.caps().get("filters", set())


def engine(p: dict) -> str:
    want = p.get("engine", "auto")
    if want == "atempo" or not has_rubberband():
        return "atempo"
    return "rubberband"


def atempo_chain(f: float) -> list[str]:
    """atempo takes 0.5 … 100 a stage in new builds (0.5 … 2 in old ones): stages of at most ×2 / ÷2."""
    out = []
    while f > 2.0:
        out.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        out.append("atempo=0.5")
        f /= 0.5
    if abs(f - 1.0) > 1e-9 or not out:
        out.append(f"atempo={f:.9g}")
    return out


def filter_string(p: dict, rate: int) -> str:
    tempo, pitch = factors(p)
    if engine(p) == "rubberband":
        opts = [f"tempo={tempo:.9f}", f"pitch={pitch:.9f}", "pitchq=quality", "channels=together",
                "transients=mixed"]
        if p.get("formant"):
            opts.append("formant=preserved")
        return "rubberband=" + ":".join(opts)
    parts = []
    if abs(pitch - 1.0) > 1e-9:
        parts += [f"asetrate={rate * pitch:.6f}", f"aresample={rate}"]
        parts += atempo_chain(tempo / pitch)
    else:
        parts += atempo_chain(tempo)
    return ",".join(parts)


class TempoPitch(Effect):
    key = "tempo"
    name = "fx_tempo"
    realtime = False
    params = [Param("tempo", lo=-50.0, hi=100.0, default=0.0, unit="%", decimals=1, label="tp_tempo"),
              Param("semitones", "int", lo=-24, hi=24, default=0, unit="st", decimals=0, label="tp_semitones"),
              Param("cents", lo=-100.0, hi=100.0, default=0.0, unit="ct", decimals=0, label="tp_cents"),
              Param("formant", "bool", default=False, label="tp_formant"),
              Param("engine", "choice", default="auto",
                    choices=(("auto", "tp_auto"), ("rubberband", "tp_rubberband"), ("atempo", "tp_atempo")),
                    label="tp_engine")]
    presets = {
        "pr_faster": {"tempo": 10.0},
        "pr_slower": {"tempo": -10.0},
        "pr_pal": {"tempo": -4.0},              # 25 → 24 fps: a PAL speed-up back to the film's tempo
        "pr_ntsc": {"tempo": 4.1666667},        # 24 → 25 fps
        "pr_octave_up": {"semitones": 12},
        "pr_octave_down": {"semitones": -12},
        "pr_semi_up": {"semitones": 1},
        "pr_semi_down": {"semitones": -1},
    }

    def render(self, d, a: int, b: int, values: dict, progress: Progress | None = None) -> store.Source:
        from ..proc import pipe_through
        p = self.clean(values)
        reader = (x for _pos, x in d.chunks(a, b))
        tempo, _pitch = factors(p)
        prog = progress or Progress()
        return pipe_through(reader, b - a, d.rate, d.channels, d.layout, d.folder, d.rate, d.channels, d.layout,
                            af=filter_string(p, d.rate), progress=lambda f: prog(f * tempo))

    def length_after(self, values: dict, frames: int) -> int:
        tempo, _p = factors(self.clean(values))
        return int(round(frames / tempo))
