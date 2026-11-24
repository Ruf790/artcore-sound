"""The effects (0.3): equalizers, dynamics, delay and reverb, tempo and pitch, noise reduction, click removal — each
with its live preview; 2.0: restoration (repair.py — hum, clipping, de-esser, crackle; adaptive noise reduction), VST3 plugins (vst3.py). base.py is the frame (parameters, stateful processors, render, the preview chain)."""
from __future__ import annotations

import threading

from .base import Effect, Param, PreviewChain, Processor, render            # noqa: F401
from .dynamics import Compressor, Limiter
from .channels import ChannelMatrix, DialogBoost
from .eq import GraphicEQ, ParametricEQ
from .repair import Declip, Decrackle, Deess, Dehum, detect_hum, scan_clips      # noqa: F401
from .restore import AutoNoiseReduction, Declick, NoiseReduction, noise_profile, scan_clicks   # noqa: F401
from .space import Delay, Reverb
from .tempo import TempoPitch
from .vst3 import VstEffect

ORDER = ("eq", "geq", "comp", "limit", "delay", "reverb", "tempo", "nr", "declick", "chmatrix", "dialog",
         "nr_auto", "decrackle", "declip", "dehum", "deess")
FILM = ("chmatrix", "dialog")          # 2.0: in the Film menu, not Effects
RESTORE = ("nr", "nr_auto", "declick", "decrackle", "declip", "dehum", "deess")   # 2.0: the Restoration menu


_classes = {c.key: c for c in (ParametricEQ, GraphicEQ, Compressor, Limiter, Delay, Reverb, TempoPitch,
                               NoiseReduction, Declick, ChannelMatrix, DialogBoost, AutoNoiseReduction, Decrackle,
                               Declip, Dehum, Deess)}
EFFECTS: dict[str, Effect] = {k: _classes[k]() for k in ORDER}


VST3 = VstEffect()                    # 2.0: every VST3 plugin (which one is in its values) — not in ORDER's menus


def get(key: str) -> Effect:
    if key == "vst3":
        return VST3
    return EFFECTS[key]


def take_noise_profile(d, a: int, b: int) -> dict:
    """«Take a noise sample»: the noise spectrum of [a, b), kept by the noise reduction for the session."""
    prof = noise_profile((x for _p, x in d.chunks(a, b)), d.rate, d.channels)
    prof["seconds"] = (b - a) / d.rate
    EFFECTS["nr"].profile = prof
    return prof


PREVIEW_SECONDS = 12.0


class PreviewRender:
    """The preview of an effect that cannot keep up in real time (tempo / pitch through ffmpeg): a piece of the
    range is rendered ahead in a thread into a small document, which the player then loops."""

    def __init__(self, d, a: int, b: int, effect: Effect, values: dict, folder: str):
        self.d, self.a, self.effect, self.folder = d, a, effect, folder
        self.b = min(b, a + int(PREVIEW_SECONDS * d.rate))
        self.values = dict(values)
        self.doc = None                       # the rendered piece, a Document, once ready
        self.error = ""
        self.frac = 0.0
        self.done = threading.Event()
        self._prog = None

    def start(self) -> None:
        from ..media import Cancelled, Progress

        def cb(f):
            self.frac = f
        self._prog = Progress(cb)

        def work():
            try:
                src = self.effect.render(self.d, self.a, self.b, self.values, self._prog)
                from ..doc import Document
                doc = Document(rate=self.d.rate, channels=self.d.channels, folder=self.folder,
                               labels=tuple(self.d.labels), layout=self.d.layout, name="preview")
                doc.set_initial(src)
                self.doc = doc
            except Cancelled:
                pass
            except Exception as e:                              # noqa: BLE001
                self.error = str(e) or type(e).__name__
            self.done.set()
        threading.Thread(target=work, daemon=True).start()

    def cancel(self) -> None:
        if self._prog is not None:
            self._prog.cancel()
