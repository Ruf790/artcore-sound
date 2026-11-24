"""The program's side of the plugin host (host.py): starting it, the commands with time limits, the sound through
shared memory. A host that dies (the plugin crashed) or does not answer in time (it hung — the host is then killed)
raises PluginError; nothing of it reaches the program's own process."""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

FRAMES = 1 << 16                    # the largest block in one go (bigger ones are cut)
T_START = 30.0                      # seconds: the host connects (python + pedalboard start)
T_LOAD = 60.0                       # loading a plugin (a big sampler-like one can be slow)
T_PROCESS = 20.0                    # one block
T_CMD = 15.0                        # anything else


class PluginError(RuntimeError):
    """kind: "crashed" (the host died), "hung" (no answer in time), "error" (the plugin said no)."""

    def __init__(self, kind: str, detail: str = ""):
        from ..i18n import t
        self.kind, self.detail = kind, detail
        text = {"crashed": t("vst_crashed"), "hung": t("vst_hung")}.get(kind, detail or t("vst_error"))
        if kind != "error" and detail:
            text += f" ({detail})"
        super().__init__(text)


def host_command(*args: str) -> list[str]:
    """How to start the host: the frozen exe itself, or python with the sources."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "sound.vst.host", *args]


def _source_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def start_process(args: list[str], **kw) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(kw.pop("env", {}) or {})
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = _source_root() + os.pathsep + env.get("PYTHONPATH", "")
    flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
    return subprocess.Popen(host_command(*args), env=env, creationflags=flags, **kw)


def _log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "ArtCoreSound-vsthost.log")


class Host:
    """One host process with one plugin (and its instances over the channels)."""

    def __init__(self):
        from multiprocessing.connection import Listener
        key = secrets.token_bytes(16)
        if sys.platform == "win32":
            address, family = r"\\.\pipe\artcore-vst-" + secrets.token_hex(8), "AF_PIPE"
        else:
            address = os.path.join(tempfile.gettempdir(), f"acvst-{os.getpid()}-{secrets.token_hex(6)}.sock")
            family = "AF_UNIX"
        self._listener = Listener(address, family=family, authkey=key)
        self._log = open(_log_path(), "ab")
        self.proc = start_process(["--vst-host", address], env={"ARTCORE_VST_KEY": key.hex()},
                                  stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log)
        got: list = []

        def accept():
            try:
                got.append(self._listener.accept())
            except Exception as e:                      # noqa: BLE001
                got.append(e)
        th = threading.Thread(target=accept, daemon=True)
        th.start()
        th.join(T_START)
        self.conn = got[0] if got and not isinstance(got[0], Exception) else None
        if self.conn is None:
            died = self.proc.poll() is not None
            self.kill()
            raise PluginError("crashed" if died else "hung", "start")
        self.shm = None
        self.frames = 0
        self.ch = 0
        self.latency = 0
        self.info: dict = {}
        self._lock = threading.Lock()
        self.dead = ""

    # ── the commands ──────────────────────────────────────────
    def call(self, *cmd, timeout: float = T_CMD):
        with self._lock:
            if self.dead:
                raise PluginError(self.dead)
            try:
                self.conn.send(cmd)
                deadline = time.monotonic() + timeout
                while not self.conn.poll(0.05):
                    if self.proc.poll() is not None:
                        self._die("crashed")
                    if time.monotonic() > deadline:
                        self._die("hung")
                kind, payload = self.conn.recv()
            except (EOFError, OSError, BrokenPipeError):
                self._die("crashed")
            if kind == "err":
                raise PluginError("error", payload)
            return payload

    def _die(self, kind: str):
        self.dead = kind
        self.kill()
        raise PluginError(kind)

    def load(self, path: str, name: str, rate: int, channels: int, mode: str = "auto",
             use: list[int] | None = None) -> dict:
        from multiprocessing import shared_memory
        self.ch = int(channels)
        self.frames = FRAMES
        self.shm = shared_memory.SharedMemory(create=True, size=self.frames * self.ch * 8)
        self._in = np.ndarray((self.frames, self.ch), np.float32, self.shm.buf[:self.frames * self.ch * 4])
        self._out = np.ndarray((self.frames, self.ch), np.float32,
                               self.shm.buf[self.frames * self.ch * 4:self.frames * self.ch * 8])
        self.info = self.call("load", {"path": path, "name": name, "rate": int(rate), "channels": self.ch,
                                       "mode": mode, "use": list(use) if use is not None else None,
                                       "frames": self.frames, "shm": self.shm.name}, timeout=T_LOAD)
        self.latency = int(self.info["latency"])
        return self.info

    def process(self, x: np.ndarray) -> np.ndarray:
        """(n, channels) float → the same n frames out, `latency` late."""
        if self.dead:
            raise PluginError(self.dead)
        x = np.asarray(x, np.float32).reshape(-1, self.ch)
        out = np.empty_like(x)
        for i in range(0, len(x), self.frames):
            k = min(self.frames, len(x) - i)
            self._in[:k] = x[i:i + k]
            self.call("process", k, timeout=T_PROCESS)
            out[i:i + k] = self._out[:k]
        return out

    def set(self, state: bytes | None = None, params: dict | None = None) -> None:
        self.call("set", {"state": state, "params": params})

    def param(self, key: str, raw: float) -> str:
        return self.call("param", key, float(raw))

    def reset(self) -> None:
        self.latency = int(self.call("reset"))

    def get(self) -> dict:
        return self.call("get")

    def editor(self, on: bool) -> None:
        self.call("editor", bool(on))

    def preset_load(self, path: str) -> dict:
        return self.call("preset_load", path)

    def preset_data(self) -> bytes:
        return self.call("preset_data")

    # ── the end ───────────────────────────────────────────────
    def close(self) -> None:
        if not self.dead and self.conn is not None:
            try:
                self.call("quit", timeout=3.0)
            except PluginError:
                pass
            self.dead = "closed"
        self.kill()

    def kill(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for c in (getattr(self, "conn", None), self._listener):
            try:
                if c is not None:
                    c.close()
            except OSError:
                pass
        shm = getattr(self, "shm", None)
        if shm is not None:
            self._in = self._out = None
            try:
                shm.close()
                shm.unlink()
            except (OSError, BufferError):
                pass
            self.shm = None
        try:
            self._log.close()
        except OSError:
            pass

    def __del__(self):
        try:
            self.kill()
        except Exception:                               # noqa: BLE001
            pass


class SharedHost:
    """3.0: one host process for every plug-in of a multitrack chain (a track): slot() gives each plug-in its own
    handle (Slot — the same calls as a Host). A plug-in that crashes takes this track's host down, never the
    program or the other tracks; the next slot() starts a new host."""

    def __init__(self):
        self.host: Host | None = None
        self._n = 0
        self._lock = threading.Lock()

    def slot(self) -> "Slot":
        with self._lock:
            if self.host is None or self.host.dead:
                if self.host is not None:
                    self.host.kill()
                self.host = Host()
            self._n += 1
            return Slot(self.host, self._n)

    def close(self) -> None:
        with self._lock:
            if self.host is not None:
                self.host.close()
                self.host = None


class Slot:
    """A plug-in in a shared host: what Host offers one plug-in, through the host's «at» command."""

    def __init__(self, host: Host, sid: int):
        self.h, self.sid = host, sid
        self.shm = None
        self.frames = 0
        self.ch = 0
        self.latency = 0
        self.info: dict = {}
        self._closed = False

    @property
    def dead(self) -> str:
        return "closed" if self._closed else self.h.dead

    def call(self, *cmd, timeout: float = T_CMD):
        if self._closed:
            raise PluginError("crashed", "closed")
        return self.h.call("at", self.sid, cmd, timeout=timeout)

    def load(self, path: str, name: str, rate: int, channels: int, mode: str = "auto",
             use: list[int] | None = None) -> dict:
        from multiprocessing import shared_memory
        self.ch = int(channels)
        self.frames = FRAMES
        self.shm = shared_memory.SharedMemory(create=True, size=self.frames * self.ch * 8)
        self._in = np.ndarray((self.frames, self.ch), np.float32, self.shm.buf[:self.frames * self.ch * 4])
        self._out = np.ndarray((self.frames, self.ch), np.float32,
                               self.shm.buf[self.frames * self.ch * 4:self.frames * self.ch * 8])
        self.info = self.call("load", {"path": path, "name": name, "rate": int(rate), "channels": self.ch,
                                       "mode": mode, "use": list(use) if use is not None else None,
                                       "frames": self.frames, "shm": self.shm.name}, timeout=T_LOAD)
        self.latency = int(self.info["latency"])
        return self.info

    process = Host.process
    set = Host.set
    param = Host.param
    reset = Host.reset
    get = Host.get
    editor = Host.editor
    preset_load = Host.preset_load
    preset_data = Host.preset_data

    def close(self) -> None:
        if not self._closed and not self.h.dead:
            try:
                self.call("unload", timeout=5.0)
            except PluginError:
                pass
        self._closed = True
        self._free()

    def kill(self) -> None:
        self._closed = True
        self._free()

    def _free(self) -> None:
        shm, self.shm = self.shm, None
        if shm is not None:
            self._in = self._out = None
            try:
                shm.close()
                shm.unlink()
            except (OSError, BufferError):
                pass
