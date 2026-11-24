"""A VST3 plugin as an effect (2.0). One effect, "vst3", for every plugin: which plugin is in its values, with
the plugin's state, so a preset, a batch step and a macro keep all they need:

  {"plugin": {"path", "name", "id", "maker"}, "state": base64 of the plugin's state ("" — as it loads),
   "params": {python name: 0…1} (set over the state), "mode": auto | all | pairs | each,
   "use": the channels it works on ([] — all; the others pass through, lined up)}

The plugin runs in a host process of its own (vst/host.py) — the processor here only passes it the sound; a
plugin that crashes or hangs raises vst.client.PluginError and the program goes on."""
from __future__ import annotations

import base64
import os

import numpy as np

from .base import Effect, Param, Processor

MODES = ("auto", "all", "pairs", "each")
TAIL_SECONDS = 30.0


def b64(state: bytes) -> str:
    return base64.b64encode(state).decode("ascii") if state else ""


def unb64(s: str) -> bytes:
    try:
        return base64.b64decode(s.encode("ascii")) if s else b""
    except (ValueError, UnicodeError):
        return b""


def resolve(ref: dict) -> dict:
    """The plugin's path now: as kept, or — moved, another computer — where the plugin list has it by its id."""
    path = ref.get("path") or ""
    if path and os.path.exists(path):
        return ref
    from .. import config
    from ..vst import scan
    found = scan.library(config.load()).find(ref)
    if found is None:
        from ..i18n import t
        raise FileNotFoundError(t("vst_missing", ref.get("name") or os.path.basename(path) or "?"))
    return {**ref, **found.ref()}


class VstProcessor(Processor):
    def __init__(self, effect, rate, labels, values, shared=None):
        self.shared = shared                    # 3.0: a vst.client.SharedHost (a multitrack chain's host)
        self.host = None
        self._loaded = None                     # (path, name, mode, use) of the plugin loaded
        self._state = None
        self._params: dict = {}
        self.mode_taken = ""
        self.groups: list = []
        super().__init__(effect, rate, labels, values)

    def configure(self, old: dict) -> None:
        from ..vst.client import Host, PluginError
        p = self.p
        ref = p["plugin"]
        if not ref.get("path") and not ref.get("id"):
            raise ValueError("no plugin")
        want = (ref.get("path"), ref.get("name"), p["mode"], tuple(p["use"]))
        if self.host is None or self.host.dead or want != self._loaded or not old:
            self.close()
            ref = resolve(ref)
            self.host = self.shared.slot() if self.shared is not None else Host()
            try:
                info = self.host.load(ref["path"], ref.get("name") or "", self.rate, self.ch, p["mode"],
                                      p["use"] or None)
            except PluginError as e:
                self.close()
                raise _explain(e) from None
            self.mode_taken, self.groups = info["mode"], info["groups"]
            self._loaded = (ref.get("path"), ref.get("name"), p["mode"], tuple(p["use"]))
            self._state, self._params = None, {}
            fresh = True
        else:
            fresh = False
        state = unb64(p["state"])
        params = dict(p["params"])
        if state and state != self._state:
            self.host.set(state=state)
            self._state = state
            self._params = {}
        if params and params != self._params:
            self.host.set(params=params)
            self._params = params
        if fresh:                                # a change while it plays (the preview) keeps its sound going
            self.host.reset()
            self.latency = self.host.latency

    def reset(self) -> None:
        if self.host is not None and not self.host.dead:
            self.host.reset()
            self.latency = self.host.latency

    def _process(self, x: np.ndarray) -> np.ndarray:
        return self.host.process(x).astype(np.float64)

    def tail_frames(self) -> int:
        return int(TAIL_SECONDS * self.rate)

    def close(self) -> None:
        if self.host is not None:
            self.host.close()
            self.host = None

    def __del__(self):
        try:
            self.close()
        except Exception:                               # noqa: BLE001
            pass


def _explain(e):
    """«channels:6» from the host → a sentence."""
    from ..i18n import t
    detail = getattr(e, "detail", "") or ""
    if detail.startswith("channels:"):
        return RuntimeError(t("vst_no_channels", detail.partition(":")[2]))
    if detail == "instrument":
        return RuntimeError(t("vst_instrument"))
    return e


class VstEffect(Effect):
    key = "vst3"
    name = "fx_vst3"
    params = [Param("mode", "choice", default="auto",
                    choices=(("auto", "vst_mode_auto"), ("all", "vst_mode_all"), ("pairs", "vst_mode_pairs"),
                             ("each", "vst_mode_each")), label="vst_mode")]
    has_tail = True
    proc_class = VstProcessor

    def processor(self, rate: int, labels, values: dict, host=None) -> VstProcessor:
        return VstProcessor(self, rate, labels, values, shared=host)

    def defaults(self) -> dict:
        return {"plugin": {}, "state": "", "params": {}, "mode": "auto", "use": []}

    def clean(self, values: dict | None) -> dict:
        v = values or {}
        ref = v.get("plugin") or {}
        ref = {k: str(ref.get(k) or "") for k in ("path", "name", "id", "maker")} if isinstance(ref, dict) else {}
        params = v.get("params") or {}
        params = {str(k): min(1.0, max(0.0, float(x))) for k, x in params.items()
                  if isinstance(x, (int, float))} if isinstance(params, dict) else {}
        use = v.get("use") or []
        use = sorted({int(c) for c in use if isinstance(c, (int, float)) and 0 <= int(c) < 32}) \
            if isinstance(use, (list, tuple)) else []
        state = v.get("state") or ""
        return {"plugin": ref, "state": state if isinstance(state, str) else "", "params": params,
                "mode": v.get("mode") if v.get("mode") in MODES else "auto", "use": use}

    def label(self, values: dict) -> str:
        ref = (values or {}).get("plugin") or {}
        return ref.get("name") or "VST3"
