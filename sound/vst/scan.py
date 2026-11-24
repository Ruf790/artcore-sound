"""Finding the VST3 plugins (2.0): the .vst3 files and bundles in the folders of the settings, each looked into by a
process of its own (`--vst-scan`, host.py) with a time limit, so that a plugin which crashes or hangs while loaded
only lands in the «not loaded» list. What was found is kept (vst3.json in the data folder) by the file's path, size
and time, so the next scan opens only what is new or changed; one not loaded is not retried until asked
(rescan(force=True) or «Retry»)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from .. import config

T_SCAN = 45.0                       # seconds for one file
CACHE_VERSION = 1


def default_folders() -> list[str]:
    if sys.platform == "win32":
        out = [os.path.join(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"), "VST3")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(os.path.join(local, "Programs", "Common", "VST3"))
        return out
    if sys.platform == "darwin":
        return ["/Library/Audio/Plug-Ins/VST3", os.path.expanduser("~/Library/Audio/Plug-Ins/VST3")]
    return [os.path.expanduser("~/.vst3"), "/usr/lib/vst3", "/usr/local/lib/vst3"]


def folders(cfg: dict) -> list[str]:
    got = cfg.get("vst3_folders")
    return list(got) if got else default_folders()


@dataclass
class PluginInfo:
    path: str                       # the .vst3 file or bundle
    name: str                       # the plugin's name (a bundle may hold several)
    maker: str = ""
    category: str = ""
    id: str = ""
    version: str = ""
    channels: list = field(default_factory=list)     # of 1, 2, 6, 8: the counts it takes
    params: int = 0

    @property
    def key(self) -> str:
        return self.id or f"{self.path}|{self.name}"

    def ref(self) -> dict:
        """What an effect's values keep of it."""
        return {"path": self.path, "name": self.name, "id": self.id, "maker": self.maker}


@dataclass
class Failed:
    path: str
    reason: str                     # "crashed" | "hung" | the error's text
    detail: str = ""                # what the process said before it died


def long_path(path: str) -> str:
    """Windows: the full form of a path (C:\\Users\\RUNNER~1 → C:\\Users\\runneradmin) — JUCE does not take the
    8.3 short names."""
    if sys.platform != "win32" or "~" not in path:
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetLongPathNameW(str(path), buf, len(buf))
    return buf.value if 0 < n < len(buf) else path


def find_files(dirs: list[str]) -> list[str]:
    """The .vst3 plugins in the folders (and their subfolders): a bundle (a folder named *.vst3) counts once."""
    out, seen = [], set()
    for root in dirs:
        if not root or not os.path.isdir(root):
            continue
        root = long_path(os.path.abspath(root))
        for here, subdirs, files in os.walk(root):
            keep = []
            for s in subdirs:
                if s.lower().endswith(".vst3"):
                    p = os.path.join(here, s)
                    if os.path.normcase(p) not in seen:
                        seen.add(os.path.normcase(p))
                        out.append(p)
                else:
                    keep.append(s)
            subdirs[:] = keep                            # not inside a bundle
            for f in files:
                if f.lower().endswith(".vst3"):
                    p = os.path.join(here, f)
                    if os.path.normcase(p) not in seen:
                        seen.add(os.path.normcase(p))
                        out.append(p)
    return sorted(out, key=str.lower)


def _stamp(path: str) -> list:
    """Size and time of a file — of a bundle, its binary's (Contents/<arch>/*)."""
    try:
        if os.path.isdir(path):
            best = [0, 0.0]
            for here, _d, files in os.walk(path):
                for f in files:
                    st = os.stat(os.path.join(here, f))
                    best = [best[0] + st.st_size, max(best[1], st.st_mtime)]
            return [best[0], round(best[1], 3)]
        st = os.stat(path)
        return [st.st_size, round(st.st_mtime, 3)]
    except OSError:
        return [0, 0.0]


def cache_path() -> str:
    return os.path.join(config.data_dir(), "vst3.json")


def load_cache() -> dict:
    try:
        with open(cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == CACHE_VERSION:
            return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "files": {}}


def save_cache(data: dict) -> None:
    path = cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def scan_one(path: str, timeout: float = T_SCAN) -> dict:
    """{"plugins": […]} or {"failed": reason} — the file looked into by a process of its own."""
    from .client import start_process
    try:
        p = start_process(["--vst-scan", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    except OSError as e:
        return {"failed": str(e)}
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return {"failed": "hung"}
    line = (out or b"").decode("utf-8", "replace").strip().splitlines()
    try:
        res = json.loads(line[-1]) if line else None
    except ValueError:
        res = None
    if res is None:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        return {"failed": "crashed", "detail": f"exit {p.returncode}: " + " | ".join(tail)}
    if res.get("error"):
        return {"failed": res["error"]}
    return {"plugins": res.get("plugins", [])}


class Library:
    """The plugins known: what the cache says, scanned anew on asking."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data = load_cache()
        self._lock = threading.Lock()

    # what there is
    def plugins(self) -> list[PluginInfo]:
        out = []
        dirs = [os.path.normcase(os.path.abspath(d)) for d in folders(self.cfg)]
        for path, ent in self.data["files"].items():
            if not _inside(path, dirs):
                continue
            for p in ent.get("plugins", []):
                if p.get("instrument") or p.get("error"):
                    continue
                out.append(PluginInfo(path=path, name=p["name"], maker=p.get("maker", ""),
                                      category=p.get("category", ""), id=p.get("id", ""),
                                      version=p.get("version", ""), channels=list(p.get("channels", [])),
                                      params=int(p.get("params", 0))))
        return sorted(out, key=lambda i: (i.maker.lower(), i.name.lower()))

    def failed(self) -> list[Failed]:
        dirs = [os.path.normcase(os.path.abspath(d)) for d in folders(self.cfg)]
        out = []
        for path, ent in self.data["files"].items():
            if not _inside(path, dirs):
                continue
            if ent.get("failed"):
                out.append(Failed(path, ent["failed"], ent.get("detail", "")))
            for p in ent.get("plugins", []):
                if p.get("error"):
                    out.append(Failed(path + " — " + p.get("name", ""), p["error"]))
        return sorted(out, key=lambda f: f.path.lower())

    def find(self, ref: dict) -> PluginInfo | None:
        """The plugin an effect's values name: by its identifier, else by path and name."""
        if not ref:
            return None
        allp = self.plugins()
        for p in allp:
            if ref.get("id") and p.id == ref.get("id"):
                return p
        for p in allp:
            if os.path.normcase(p.path) == os.path.normcase(ref.get("path", "")) and p.name == ref.get("name"):
                return p
        return None

    # scanning
    def scan(self, force: bool = False, retry: list[str] | None = None, progress=None, cancel=None,
             workers: int = 3) -> tuple[int, int]:
        """Look into the new and changed files (all with force; `retry`: just those). → (plugins, not loaded)."""
        files = find_files(folders(self.cfg))
        old = self.data["files"]
        todo = []
        for f in files:
            ent = old.get(f)
            stamp = _stamp(f)
            if force or (retry and f in retry) or ent is None or ent.get("stamp") != stamp:
                todo.append((f, stamp))
        new = {f: old[f] for f in files if f in old}
        done = [0]

        def one(item):
            f, stamp = item
            if cancel is not None and cancel():
                return f, None
            res = scan_one(f)
            res["stamp"] = stamp
            with self._lock:
                done[0] += 1
                if progress is not None:
                    progress(done[0], len(todo), f)
            return f, res
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for f, res in ex.map(one, todo):
                    if res is not None:
                        new[f] = res
        self.data = {"version": CACHE_VERSION, "files": new}
        save_cache(self.data)
        return len(self.plugins()), len(self.failed())


def _inside(path: str, dirs: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return any(p == d or p.startswith(d.rstrip("\\/") + os.sep) for d in dirs)


_lib: Library | None = None


def library(cfg: dict) -> Library:
    global _lib
    if _lib is None or _lib.cfg is not cfg:
        _lib = Library(cfg)
    return _lib


def to_dict(p: PluginInfo) -> dict:
    return asdict(p)
