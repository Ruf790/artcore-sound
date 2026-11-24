"""The plugin host (2.0): the process a VST3 plugin lives in, so that a plugin which crashes or hangs takes only this
process down, never ArtCore Sound. The program starts it (client.py) as `ArtCoreSound.exe --vst-host` (from the
sources: python -m sound.vst.host), it connects back over multiprocessing.connection (a named pipe on Windows, a Unix
socket elsewhere; the key comes in ARTCORE_VST_KEY) and serves commands; the sound itself goes through shared memory,
not the pipe. The plugin is loaded with Spotify's pedalboard (GPL v3, JUCE inside).

Commands (a tuple, the answer ("ok", payload) or ("err", message)):
  load {path, name, rate, channels, mode, use, frames, shm}   the plugin, one instance per group of channels
  set {state, params}         the plugin's state (bytes, as raw_state) and/or parameters (python name → 0…1)
  param key raw               one parameter (0…1) → its text, as the plugin shows it
  process n                   n frames (≤ frames) of shm's input area → its output area, exactly n, `latency` late
  reset                       forget the sound so far
  get                         {state, params, editor, editor_error}
  editor on|off               the plugin's own window (in this process, on its main thread)
  preset_load path / preset_data      a .vstpreset file in, the current one out (bytes)
  at slot cmd                 3.0: the command for plug-in `slot` of a multitrack chain (one host per track, every
                              plug-in of the chain in it, each with its own shared memory); unload frees a slot
  quit

Channels (mode): "all" — one instance on every channel used (if the plugin takes that many); "pairs" — one per
pair (L R, C LFE, Ls Rs…); "each" — one per channel; "auto" — the first of these the plugin takes. Channels not used
pass through, delayed as the plugin is, so everything stays lined up. pedalboard holds back a plugin's reported
latency at the start; each group's output gets that many zeros in front and every group (and the channels passed
through) is delayed by the largest latency: `latency` of the load answer.

`ArtCoreSound.exe --vst-scan <file>` (scan.py): what the plugins of one .vst3 file are, as JSON on stdout."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback

import numpy as np

TRY_CHANNELS = (1, 2, 6, 8)


def binary(path: str) -> str:
    """What pedalboard opens: on Windows not a bundle (a folder *.vst3) but the plugin inside it
    (Contents\\x86_64-win\\<name>.vst3); elsewhere the bundle itself."""
    if sys.platform != "win32" or not os.path.isdir(path):
        return path
    arch = "arm64-win" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "x86_64-win"
    inside = os.path.join(path, "Contents", arch)
    want = os.path.join(inside, os.path.basename(path))
    if os.path.isfile(want):
        return want
    try:
        found = [f for f in os.listdir(inside) if f.lower().endswith(".vst3")]
    except OSError:
        found = []
    return os.path.join(inside, found[0]) if found else path


def _load(path: str, name: str | None):
    import pedalboard
    path = binary(path)
    p = pedalboard.load_plugin(path, plugin_name=name) if name else pedalboard.load_plugin(path)
    if not getattr(p, "is_effect", True) or getattr(p, "is_instrument", False):
        raise ValueError("instrument")
    return p


def takes(p, ch: int, rate: int = 48000) -> bool:
    """Does the plugin process `ch` channels?"""
    try:
        y = p.process(np.zeros((ch, 256), np.float32), rate, buffer_size=256, reset=True)
        return y.shape[0] == ch
    except Exception:                                   # noqa: BLE001 — «does not support N-channel output»
        return False
    finally:
        try:
            p.reset()
        except Exception:                               # noqa: BLE001
            pass


def param_list(p) -> list[dict]:
    out = []
    for key, q in p.parameters.items():
        kind = "bool" if q.type is bool else ("choice" if q.type is str else "float")
        steps = int(q.num_steps) if q.num_steps and q.num_steps < 0x7FFFFFFF else 0
        d = {"key": key, "name": q.name, "kind": kind, "raw": float(q.raw_value), "text": str(q.string_value),
             "steps": steps, "unit": str(q.label or "")}
        if kind == "choice":
            try:
                d["choices"] = [str(v) for v in q.valid_values]
            except Exception:                           # noqa: BLE001
                d["kind"] = "float"
        out.append(d)
    return out


def groups_for(p, mode: str, use: list[int], rate: int) -> tuple[str, list[list[int]], bool]:
    """(the mode taken, groups of channel indices, mono channels fed as a stereo pair)."""
    n = len(use)
    pairs = [use[i:i + 2] for i in range(0, n, 2)]
    if mode == "auto":
        if takes(p, n, rate):
            mode = "all"
        elif takes(p, 2, rate):
            mode = "pairs"
        else:
            mode = "each"
    if mode == "all":
        if not takes(p, n, rate):
            raise ValueError(f"channels:{n}")
        return mode, [list(use)], False
    if mode == "pairs":
        if not takes(p, 2, rate):
            raise ValueError("channels:2")
        mono_ok = any(len(g) == 1 for g in pairs) and takes(p, 1, rate)
        return mode, pairs, not mono_ok
    if takes(p, 1, rate):
        return "each", [[c] for c in use], False
    if takes(p, 2, rate):
        return "each", [[c] for c in use], True          # a mono-less plugin: the channel twice, the left back
    raise ValueError("channels:1")


class Rack:
    """The instances of one plugin over the channels of a document (see the module's doc)."""

    def __init__(self, cmd: dict):
        self.rate = int(cmd["rate"])
        self.ch = int(cmd["channels"])
        use = [int(c) for c in (cmd.get("use") or range(self.ch)) if 0 <= int(c) < self.ch]
        if not use:
            raise ValueError("no channels")
        first = _load(cmd["path"], cmd.get("name"))
        self.mode, self.groups, self.dup = groups_for(first, cmd.get("mode") or "auto", use, self.rate)
        self.plugins = [first] + [_load(cmd["path"], cmd.get("name")) for _g in self.groups[1:]]
        self.passthru = [c for c in range(self.ch) if c not in use]
        self.frames = int(cmd["frames"])
        from multiprocessing import shared_memory
        try:
            self.shm = shared_memory.SharedMemory(name=cmd["shm"], track=False)
        except TypeError:                                # Python < 3.13
            self.shm = shared_memory.SharedMemory(name=cmd["shm"])
        size = self.frames * self.ch
        self.inp = np.ndarray((self.frames, self.ch), np.float32, self.shm.buf[:size * 4])
        self.out = np.ndarray((self.frames, self.ch), np.float32, self.shm.buf[size * 4:size * 8])
        self.lat = [0] * len(self.plugins)
        self.reset()

    def reset(self) -> None:
        for p in self.plugins:
            p.reset()
        # the latency a plugin reports once it is prepared (pedalboard prepares it at the first process)
        for i, (p, g) in enumerate(zip(self.plugins, self.groups)):
            k = 2 if self.dup and len(g) == 1 else len(g)
            p.process(np.zeros((k, 64), np.float32), self.rate, buffer_size=64, reset=True)
            self.lat[i] = max(0, int(p.reported_latency_samples or 0))
            p.reset()
        self.latency = max(self.lat) if self.lat else 0
        z = np.zeros((self.latency,), np.float32)
        self.fifo = [[np.zeros((len(g), self.latency), np.float32)] for g in self.groups]
        self.fifo_n = [self.latency] * len(self.groups)
        self.pass_fifo = [[z.copy()] for _c in self.passthru]

    def set_state(self, state: bytes) -> None:
        for p in self.plugins:
            p.raw_state = state

    def set_params(self, params: dict) -> None:
        for p in self.plugins:
            qs = p.parameters
            for k, raw in params.items():
                q = qs.get(k)
                if q is not None:
                    q.raw_value = min(1.0, max(0.0, float(raw)))

    def process(self, n: int) -> None:
        x = self.inp[:n]
        y = self.out[:n]
        for i, (p, g) in enumerate(zip(self.plugins, self.groups)):
            xi = np.ascontiguousarray(x[:, g].T)
            if self.dup and len(g) == 1:
                xi = np.vstack((xi, xi))
            yi = p.process(xi, self.rate, buffer_size=min(n, 8192), reset=False)
            if self.dup and len(g) == 1:
                yi = yi[:1]
            self.fifo[i].append(np.asarray(yi, np.float32))
            self.fifo_n[i] += yi.shape[1]
            y[:, g] = self._take(self.fifo[i], n, len(g)).T
            self.fifo_n[i] -= n
        for j, c in enumerate(self.passthru):
            self.pass_fifo[j].append(x[:, c].copy())
            buf = np.concatenate(self.pass_fifo[j])
            y[:, c] = buf[:n]
            self.pass_fifo[j] = [buf[n:]]

    @staticmethod
    def _take(parts: list, n: int, ch: int) -> np.ndarray:
        buf = np.concatenate(parts, axis=1) if len(parts) > 1 else parts[0]
        if buf.shape[1] < n:                             # a plugin late beyond what it reported: silence first
            buf = np.concatenate((np.zeros((ch, n - buf.shape[1]), np.float32), buf), axis=1)
        parts[:] = [buf[:, n:]]
        return buf[:, :n]

    def close(self) -> None:
        self.inp = self.out = None
        try:
            self.shm.close()
        except Exception:                               # noqa: BLE001
            pass


class Host:
    def __init__(self, conn):
        self.conn = conn
        self.rack: Rack | None = None
        self.racks: dict = {}                   # 3.0: slot → Rack (the plug-ins of a track's chain)
        self.editor_q: queue.Queue = queue.Queue()
        self.editor_open = False
        self.editor_error = ""
        self._close_ev: threading.Event | None = None
        self.stop = False

    def answer(self, cmd: tuple):
        op = cmd[0]
        if op == "at":
            slot, inner = cmd[1], tuple(cmd[2])
            if inner[0] == "unload":
                r = self.racks.pop(slot, None)
                if r is not None:
                    r.close()
                return None
            keep = self.rack
            self.rack = self.racks.get(slot)
            try:
                return self.answer(inner)
            finally:
                if self.rack is not None:
                    self.racks[slot] = self.rack
                self.rack = keep
        r = self.rack
        if op == "load":
            if r is not None:
                r.close()
            self.rack = r = Rack(cmd[1])
            p = r.plugins[0]
            return {"latency": r.latency, "mode": r.mode, "groups": r.groups, "params": param_list(p),
                    "state": bytes(p.raw_state), "name": p.name}
        if r is None:
            raise RuntimeError("no plugin")
        if op == "process":
            r.process(int(cmd[1]))
            return None
        if op == "set":
            d = cmd[1]
            if d.get("state"):
                r.set_state(bytes(d["state"]))
            if d.get("params"):
                r.set_params(d["params"])
            return None
        if op == "param":
            r.set_params({cmd[1]: cmd[2]})
            q = r.plugins[0].parameters.get(cmd[1])
            return str(q.string_value) if q is not None else ""
        if op == "reset":
            r.reset()
            return r.latency
        if op == "get":
            p = r.plugins[0]
            state = bytes(p.raw_state)
            if len(r.plugins) > 1:                       # the editor changed the first: the others follow
                for o in r.plugins[1:]:
                    if bytes(o.raw_state) != state:
                        o.raw_state = state
            return {"state": state, "params": param_list(p), "editor": self.editor_open,
                    "editor_error": self.editor_error}
        if op == "editor":
            if cmd[1] and not self.editor_open:
                self.editor_open, self.editor_error = True, ""
                self._close_ev = threading.Event()
                self.editor_q.put((r.plugins[0], self._close_ev))
            elif not cmd[1] and self._close_ev is not None:
                self._close_ev.set()
            return None
        if op == "preset_load":
            r.plugins[0].load_preset(cmd[1])
            state = bytes(r.plugins[0].raw_state)
            r.set_state(state)
            return {"state": state, "params": param_list(r.plugins[0])}
        if op == "preset_data":
            return bytes(r.plugins[0].preset_data)
        raise ValueError(f"unknown command {op!r}")

    def commands(self) -> None:
        try:
            while True:
                try:
                    cmd = self.conn.recv()
                except (EOFError, OSError):
                    break
                if cmd[0] == "quit":
                    self.conn.send(("ok", None))
                    break
                try:
                    self.conn.send(("ok", self.answer(cmd)))
                except Exception as e:                   # noqa: BLE001 — told to the program, the host goes on
                    msg = str(e) or type(e).__name__
                    sys.stderr.write(traceback.format_exc())
                    self.conn.send(("err", msg))
        finally:
            self.stop = True
            if self._close_ev is not None:
                self._close_ev.set()
            self.editor_q.put(None)

    def editors(self) -> None:
        """The main thread: the plugin's own window (JUCE wants it here) — the commands go on meanwhile."""
        while True:
            item = self.editor_q.get()
            if item is None:
                return
            plugin, ev = item
            try:
                plugin.show_editor(ev)
            except Exception as e:                       # noqa: BLE001 — no window (no editor, no display)
                self.editor_error = str(e) or type(e).__name__
            self.editor_open = False


def serve(address: str) -> int:
    from multiprocessing.connection import Client
    key = bytes.fromhex(os.environ.get("ARTCORE_VST_KEY", ""))
    family = "AF_PIPE" if address.startswith("\\\\.\\pipe\\") else "AF_UNIX"
    conn = Client(address, family=family, authkey=key)
    host = Host(conn)
    t = threading.Thread(target=host.commands, name="commands", daemon=True)
    t.start()
    host.editors()
    t.join(2.0)
    try:
        conn.close()
    except OSError:
        pass
    if host.rack is not None:
        host.rack.close()
    for r in host.racks.values():
        r.close()
    os._exit(0)                                         # a plugin's threads must not keep the process alive


def scan_file(path: str) -> dict:
    """The plugins of one .vst3 file: name, maker, category, identifier, version, channels it takes, parameters."""
    import pedalboard
    out = []
    names = pedalboard.VST3Plugin.get_plugin_names_for_file(binary(path))
    for name in names:
        try:
            p = _load(path, name)
        except ValueError as e:
            if str(e) == "instrument":                   # an instrument: not listed among the effects
                out.append({"name": name, "instrument": True})
            else:
                out.append({"name": name, "error": str(e)})
            continue
        except Exception as e:                           # noqa: BLE001
            out.append({"name": name, "error": str(e) or type(e).__name__})
            continue
        if getattr(p, "is_instrument", False) or not getattr(p, "is_effect", True):
            out.append({"name": name, "instrument": True})
            continue
        out.append({"name": p.name or name, "maker": str(p.manufacturer_name or ""),
                    "category": str(p.category or ""), "id": str(p.identifier or ""),
                    "version": str(p.version or ""), "channels": [c for c in TRY_CHANNELS if takes(p, c)],
                    "params": len(p.parameters)})
    return {"path": path, "plugins": out}


def main(argv: list[str]) -> int:
    if "--vst-scan" in argv:
        path = argv[argv.index("--vst-scan") + 1]
        try:
            res = scan_file(path)
        except Exception as e:                           # noqa: BLE001
            res = {"path": path, "error": str(e) or type(e).__name__}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
        os._exit(0)
    if "--vst-host" in argv:
        return serve(argv[argv.index("--vst-host") + 1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
