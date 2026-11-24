"""The sound files of a project (3.0). A project never copies sound: a WAV at the project's rate (PCM 16 / 24 / 32 bit
or float, RIFF or RF64) is read in place; any other file — FLAC, MP3, a film's track, a WAV at another rate — is
decoded once by ffmpeg into a WAV float file in the cache (<temp>\\ArtCoreSound\\cache), found there again by the
file's path, size and time the next time the project opens. The peaks of every medium (for the timeline) are in the
cache too. What the program writes itself — recordings, «Send to multitrack», frozen tracks, edited clips — goes to
the project's audio folder as WAV float (WavWriter), peaks into the cache as it is written.

A Media whose file is missing stays in the project «offline» (silence, a red clip); relinking points it at another
file."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
import uuid

import numpy as np

from .. import store
from ..store import BASE, CHUNK, FACTOR, MIN_BLOCKS, Source

CACHE_DIR = "cache"
PEAK_STEP = CHUNK * 8             # frames read at a time while the peaks are made
READ_DIRECT = 16 << 10            # reads of at least this many bytes go through the file: the mapping would keep
                                  # every page it touched in the process's memory (RSS) as a project plays on
KSDATAFORMAT_PCM = b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
KSDATAFORMAT_FLOAT = b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
MASKS = {1: 0x4, 2: 0x3, 6: 0x3F, 8: 0x63F}


def cache_root(cfg: dict | None = None) -> str:
    """The folder of the decoded files and the peaks — next to the session folders (store.clean_stale leaves it:
    it removes only s-* folders), kept between runs."""
    from .. import config
    p = os.path.join(config.work_root(cfg), CACHE_DIR)
    os.makedirs(p, exist_ok=True)
    return p


# ── WAV files read in place ───────────────────────────────────
class WavInfo:
    def __init__(self, rate: int, channels: int, kind: str, offset: int, frames: int, mask: int = 0):
        self.rate, self.channels, self.kind, self.offset, self.frames, self.mask = rate, channels, kind, offset, \
            frames, mask

    @property
    def width(self) -> int:
        return {"i2": 2, "i3": 3, "i4": 4, "f4": 4}[self.kind]


def parse_wav(path: str) -> WavInfo | None:
    """The layout of a WAV that can be read in place (PCM 16 / 24 / 32 or float 32, RIFF / RF64), else None."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64"):
                return None
            rf64 = head[:4] == b"RF64"
            ds64_data = None
            fmt = None
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                ck = f.read(8)
                if len(ck) < 8:
                    break
                cid, n = ck[:4], struct.unpack("<I", ck[4:])[0]
                body = pos + 8
                if cid == b"ds64":
                    d = f.read(min(n, 64))
                    if len(d) >= 16:
                        ds64_data = struct.unpack("<Q", d[8:16])[0]
                elif cid == b"fmt ":
                    fmt = f.read(min(n, 64))
                elif cid == b"data":
                    if fmt is None or len(fmt) < 16:
                        return None
                    tag, ch, rate, _bps, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
                    mask = 0
                    if tag == 0xFFFE and len(fmt) >= 40:
                        mask = struct.unpack("<I", fmt[20:24])[0]
                        sub = fmt[24:40]
                        tag = 1 if sub == KSDATAFORMAT_PCM else 3 if sub == KSDATAFORMAT_FLOAT else 0
                    kind = {(1, 16): "i2", (1, 24): "i3", (1, 32): "i4", (3, 32): "f4"}.get((tag, bits))
                    if kind is None or ch <= 0 or rate <= 0:
                        return None
                    if rf64 and n == 0xFFFFFFFF and ds64_data is not None:
                        n = ds64_data
                    n = min(n, size - body)
                    width = {"i2": 2, "i3": 3, "i4": 4, "f4": 4}[kind]
                    return WavInfo(rate, ch, kind, body, n // (width * ch), mask)
                pos = body + n + (n & 1)
    except (OSError, struct.error):
        return None
    return None


class _View:
    """src.data[a:b] of a PcmSource: the frames as float32 (store.source_minmax reads the edges through it)."""

    def __init__(self, src: "PcmSource"):
        self.src = src

    def __getitem__(self, sl):
        a, b, _s = sl.indices(self.src.frames)
        return self.src.read(a, max(0, b - a))


class PcmSource(Source):
    """A WAV file as a source: frames converted to float32 as they are read; the peak levels from the cache."""

    def __init__(self, path: str, info: WavInfo, levels: list | None = None):
        super().__init__("", 0, info.channels, levels, kind="data")
        self.path, self.frames, self.info = path, int(info.frames), info
        self._map = None
        if self.frames:
            try:
                if info.kind != "i3":
                    self._map = np.memmap(path, dtype=np.dtype("<" + info.kind), mode="r", offset=info.offset,
                                          shape=(self.frames, info.channels))
                else:
                    self._map = np.memmap(path, dtype=np.uint8, mode="r", offset=info.offset,
                                          shape=(self.frames, info.channels, 3))
            except (OSError, ValueError):
                self._map = None
        self.data = _View(self) if self.frames else None

    def _convert(self, raw: np.ndarray, out: np.ndarray) -> None:
        k = self.info.kind
        if k == "f4":
            out[...] = raw
        elif k == "i2":
            np.multiply(raw, np.float32(1.0 / 32768.0), out=out, casting="unsafe")
        elif k == "i4":
            out[...] = (raw.astype(np.float64) * (1.0 / 2147483648.0)).astype(np.float32)
        else:
            b = raw.astype(np.int32)
            v = b[..., 0] | (b[..., 1] << 8) | (b[..., 2] << 16)
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            out[...] = v.astype(np.float32) * np.float32(1.0 / 8388608.0)

    def read(self, start: int, n: int, out: np.ndarray | None = None) -> np.ndarray:
        if out is None:
            out = np.empty((n, self.channels), np.float32)
        start, n = int(start), int(n)
        a, b = max(0, start), min(self.frames, start + n)
        if b <= a or self._map is None:
            out[:n] = 0
            return out
        if a > start:
            out[:a - start] = 0
        if b < start + n:
            out[b - start:n] = 0
        raw = self._read_raw(a, b)
        self._convert(raw, out[a - start:b - start])
        return out

    def _read_raw(self, a: int, b: int) -> np.ndarray:
        """Big runs through plain file reads (the system's read-ahead), small ones through the mapping."""
        info = self.info
        nbytes = (b - a) * info.channels * info.width
        if nbytes >= READ_DIRECT:
            f = getattr(self._local, "f", None)
            try:
                if f is None:
                    f = open(self.path, "rb", buffering=0)
                    with self._hlock:
                        self._handles.append(f)
                    self._local.f = f
                f.seek(info.offset + a * info.channels * info.width)
                buf = bytearray(nbytes)
                view = memoryview(buf)
                got = 0
                while got < nbytes:
                    k = f.readinto(view[got:])
                    if not k:
                        break
                    got += k
                if got == nbytes:
                    dt = np.uint8 if info.kind == "i3" else np.dtype("<" + info.kind)
                    arr = np.frombuffer(buf, dt)
                    return arr.reshape((b - a, info.channels, 3) if info.kind == "i3" else (b - a, info.channels))
            except (OSError, ValueError):
                pass
        return np.asarray(self._map[a:b])

    def close(self):
        self._map = None
        self.data = None
        super().close()


# ── peaks ─────────────────────────────────────────────────────
class PeakWriter:
    """The peak levels of a stream of frames into files base.p0, base.p1 … (as store.SourceWriter makes them)."""

    def __init__(self, base: str, channels: int):
        self.base, self.channels = base, int(channels)
        self._pk = open(base + ".p0", "wb")
        self._tail = np.empty((0, self.channels), np.float32)
        self.blocks0 = 0
        self.frames = 0

    def append(self, x: np.ndarray) -> None:
        x = np.asarray(x, np.float32).reshape(-1, self.channels)
        if not len(x):
            return
        self.frames += len(x)
        if len(self._tail):
            x = np.concatenate((self._tail, x))
        full = len(x) // BASE * BASE
        if full:
            pk = store._block_peaks(x[:full])
            self._pk.write(pk.tobytes())
            self.blocks0 += len(pk)
        self._tail = x[full:].copy()

    def finish(self) -> list:
        if len(self._tail):
            pk = store._block_peaks(self._tail)
            self._pk.write(pk.tobytes())
            self.blocks0 += len(pk)
        self._pk.close()
        if not self.blocks0:
            return []
        ch = self.channels
        levels = [np.memmap(self.base + ".p0", dtype=np.float32, mode="r", shape=(self.blocks0, ch, 2))]
        k = 0
        while levels[-1].shape[0] > MIN_BLOCKS * FACTOR:
            prev = levels[-1]
            k += 1
            path = f"{self.base}.p{k}"
            n = 0
            with open(path, "wb") as f:
                for i in range(0, prev.shape[0], FACTOR * 65536):
                    nxt = store._reduce(np.asarray(prev[i:i + FACTOR * 65536]))
                    f.write(nxt.tobytes())
                    n += nxt.shape[0]
            levels.append(np.memmap(path, dtype=np.float32, mode="r", shape=(n, ch, 2)))
        return levels

    def abort(self) -> None:
        try:
            self._pk.close()
        except OSError:
            pass
        remove_peaks(self.base)


def remove_peaks(base: str) -> None:
    for k in range(16):
        try:
            os.remove(f"{base}.p{k}")
        except OSError:
            pass


def load_peaks(base: str, frames: int, channels: int) -> list | None:
    levels = []
    k = 0
    while os.path.isfile(f"{base}.p{k}"):
        n = os.path.getsize(f"{base}.p{k}") // (channels * 8)
        if not n:
            break
        levels.append(np.memmap(f"{base}.p{k}", dtype=np.float32, mode="r", shape=(n, channels, 2)))
        k += 1
    if not levels or levels[0].shape[0] != -(-frames // BASE):
        return None
    return levels


def build_peaks(src: PcmSource, base: str, progress=None) -> list:
    w = PeakWriter(base, src.channels)
    try:
        for a in range(0, src.frames, PEAK_STEP):
            w.append(src.read(a, min(PEAK_STEP, src.frames - a)))
            if progress is not None:
                progress(a / max(1, src.frames))
        return w.finish()
    except BaseException:
        w.abort()
        raise


# ── writing WAV float files ───────────────────────────────────
class WavWriter:
    """A WAV float 32 file written as frames come (a recording, a render): RIFF, RF64 past 4 GB (a JUNK chunk keeps
    the room for ds64). The peaks go to `peaks` (a cache base path) on the way. finish() → PcmSource."""

    def __init__(self, path: str, channels: int, rate: int, peaks: str):
        self.path, self.channels, self.rate = path, int(channels), int(rate)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._f = open(path + ".part", "wb")
        self._f.write(self._header(0))
        self.offset = self._f.tell()
        self.frames = 0
        self.peaks = PeakWriter(peaks, channels)

    def _header(self, frames: int, rf64: bool = False) -> bytes:
        ch, rate = self.channels, self.rate
        data = frames * ch * 4
        if ch <= 2:
            fmt = struct.pack("<HHIIHH", 3, ch, rate, rate * ch * 4, ch * 4, 32)
        else:
            fmt = struct.pack("<HHIIHHHHI", 0xFFFE, ch, rate, rate * ch * 4, ch * 4, 32, 22, 32,
                              MASKS.get(ch, 0)) + KSDATAFORMAT_FLOAT
        fact = struct.pack("<I", min(frames, 0xFFFFFFFF))
        body = 4 + (8 + 28) + (8 + len(fmt)) + (8 + 4) + 8
        if rf64:
            ds = struct.pack("<QQQI", body + data, data, frames, 0)
            return (b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"ds64" + struct.pack("<I", 28) + ds +
                    b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"fact" + struct.pack("<I", 4) + fact +
                    b"data" + struct.pack("<I", 0xFFFFFFFF))
        return (b"RIFF" + struct.pack("<I", body + data) + b"WAVE" + b"JUNK" + struct.pack("<I", 28) + b"\0" * 28 +
                b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"fact" + struct.pack("<I", 4) + fact +
                b"data" + struct.pack("<I", data))

    def append(self, x: np.ndarray) -> None:
        x = np.ascontiguousarray(x, dtype="<f4").reshape(-1, self.channels)
        if not len(x):
            return
        self._f.write(x.tobytes())
        self.frames += len(x)
        self.peaks.append(x)

    def abort(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass
        self.peaks.abort()
        try:
            os.remove(self.path + ".part")
        except OSError:
            pass

    def finish(self) -> PcmSource:
        try:
            levels = self.peaks.finish()
            big = self.offset + self.frames * self.channels * 4 > 0xFFFFFFF0
            self._f.seek(0)
            self._f.write(self._header(self.frames, big))
            self._f.close()
            os.replace(self.path + ".part", self.path)
        except BaseException:
            self.abort()
            raise
        info = parse_wav(self.path)
        if info is None:
            raise OSError(f"bad wav {self.path}")
        return PcmSource(self.path, info, levels or [])


# ── the media of a project ────────────────────────────────────
class Media:
    """A file a project plays. src None: offline (missing) — silence."""

    def __init__(self, mid: str, path: str, track: int = 0, name: str = ""):
        self.id = mid
        self.path = os.path.abspath(path) if path else ""
        self.track = int(track)
        self.name = name or os.path.basename(path)
        self.rate = 0
        self.channels = 2
        self.frames = 0
        self.src: Source | None = None
        self.error = ""
        self.cached = ""                 # the decoded copy in the cache ("" — read in place)
        self.labels: tuple = ()

    @property
    def offline(self) -> bool:
        return self.src is None

    def to_dict(self) -> dict:
        return {"id": self.id, "path": self.path, "track": self.track, "name": self.name, "frames": self.frames,
                "channels": self.channels}

    def close(self) -> None:
        if self.src is not None:
            self.src.close()
        self.src = None


def new_media_id() -> str:
    return uuid.uuid4().hex[:10]


def _key(path: str, track: int, rate: int) -> str:
    st = os.stat(path)
    s = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}|{track}|{rate}|1"
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:24]


_lock = threading.Lock()


def load(m: Media, rate: int, cache: str, progress=None) -> Media:
    """Make the medium's source: read in place (a WAV at `rate`) or from / into the cache. A missing or unreadable
    file leaves it offline with m.error."""
    from .. import ff
    from ..media import Progress
    m.close()
    m.error = ""
    progress = progress or Progress()
    if not m.path or not os.path.isfile(m.path):
        m.error = "missing"
        return m
    try:
        key = _key(m.path, m.track, rate)
        wi = parse_wav(m.path) if m.track == 0 else None
        if wi is not None and wi.rate == rate:
            base = os.path.join(cache, key + ".pk")
            src = PcmSource(m.path, wi)
            levels = load_peaks(base, src.frames, src.channels)
            if levels is None:
                remove_peaks(base)
                levels = build_peaks(src, base, progress)
            src.levels = levels
            m.cached = ""
        else:
            wav = os.path.join(cache, key + ".wav")
            base = os.path.join(cache, key + ".pk")
            src = None
            wi2 = parse_wav(wav) if os.path.isfile(wav) and os.path.isfile(wav + ".ok") else None
            if wi2 is not None:
                src = PcmSource(wav, wi2)
                lv = load_peaks(base, src.frames, src.channels)
                if lv is None:
                    src.close()
                    src = None
                else:
                    src.levels = lv
            if src is None:
                info = ff.probe(m.path)
                track = next((t for t in info.tracks if t.pos == m.track), None)
                if track is None:
                    raise RuntimeError("no sound track")
                src = decode_to(m.path, track, rate, wav, base, progress)
                with open(wav + ".ok", "w") as f:
                    f.write("1")
            _touch(wav)
            m.cached = wav
        m.src = src
        m.frames, m.channels, m.rate = src.frames, src.channels, rate
        from ..ff import labels as _labels
        m.labels = tuple(_labels(src.channels, ""))
    except Exception as e:                       # noqa: BLE001 — the project still opens, this clip is silent
        from ..media import Cancelled
        if isinstance(e, Cancelled):
            raise
        m.close()
        m.error = str(e) or type(e).__name__
    return m


def decode_to(path: str, track, rate: int, wav: str, peaks: str, progress=None) -> PcmSource:
    """ffmpeg → a WAV float at `rate` (soxr when there is one)."""
    import subprocess
    from .. import ff
    from ..media import _stderr_reader, pipe_frames
    from ..proc import _has_soxr
    exe = ff.ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    ch = track.channels
    af = ["-af", f"aresample={rate}:resampler=soxr"] if track.rate != rate and _has_soxr() else \
        (["-ar", str(rate)] if track.rate != rate else [])
    args = [exe, "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", f"0:a:{track.pos}", "-vn", "-sn",
            "-dn", "-ac", str(ch), *af, "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = ff.popen(args, stdout=subprocess.PIPE)
    err = _stderr_reader(proc)
    w = WavWriter(wav, ch, rate, peaks)
    want = max(1.0, (track.duration or 0) * rate)
    try:
        for x in pipe_frames(proc.stdout, ch):
            w.append(x)
            if progress is not None:
                progress(w.frames / want if track.duration else 0.0)
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        w.abort()
        raise
    if proc.returncode != 0 and not w.frames:
        w.abort()
        raise RuntimeError("\n".join(err[-6:]) or f"ffmpeg exit {proc.returncode}")
    return w.finish()


def _touch(path: str) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def clean_cache(cache: str, limit_bytes: int) -> int:
    """Remove the decoded files used longest ago until the cache is under the limit (their peaks with them).
    → how many bytes were removed."""
    try:
        names = os.listdir(cache)
    except OSError:
        return 0
    groups: dict = {}
    for n in names:
        key = n.split(".", 1)[0]
        p = os.path.join(cache, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        g = groups.setdefault(key, [0, 0.0, []])
        g[0] += st.st_size
        g[1] = max(g[1], st.st_mtime)
        g[2].append(p)
    total = sum(g[0] for g in groups.values())
    removed = 0
    for _k, (size, _mt, paths) in sorted(groups.items(), key=lambda kv: kv[1][1]):
        if total <= limit_bytes:
            break
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        total -= size
        removed += size
    return removed


def write_doc(d, a: int, b: int, path: str, cache: str, progress=None) -> PcmSource:
    """Frames [a, b) of an editor document → a WAV float file (Send to multitrack)."""
    base = os.path.join(cache, "w" + hashlib.sha1(f"{path}|{time.time_ns()}".encode()).hexdigest()[:23] + ".pk")
    w = WavWriter(path, d.channels, d.rate, base)
    try:
        total = max(1, b - a)
        for pos, x in d.chunks(a, b):
            w.append(x)
            if progress is not None:
                progress((pos - a + len(x)) / total)
    except BaseException:
        w.abort()
        raise
    return w.finish()


def media_from_source(src: PcmSource, name: str = "") -> Media:
    """A Media for a file the program has just written (its source and peaks are ready)."""
    from ..ff import labels as _labels
    m = Media(new_media_id(), src.path, 0, name or os.path.splitext(os.path.basename(src.path))[0])
    m.src, m.frames, m.channels, m.rate = src, src.frames, src.channels, src.info.rate
    m.labels = tuple(_labels(src.channels, ""))
    return m


def remember_peaks(src: PcmSource, cache: str) -> None:
    """The peaks a WavWriter made go under the file's own cache key, so the next opening finds them."""
    try:
        key = _key(src.path, 0, src.info.rate)
    except OSError:
        return
    base = os.path.join(cache, key + ".pk")
    if os.path.isfile(base + ".p0"):
        return
    for k in range(len(src.levels)):
        try:
            import shutil
            shutil.copyfile(src.levels[k].filename, f"{base}.p{k}")
        except (OSError, AttributeError):
            remove_peaks(base)
            return


def unique_path(folder: str, stem: str, ext: str = ".wav") -> str:
    """folder\\stem.wav, or stem (2).wav … when that exists."""
    os.makedirs(folder, exist_ok=True)
    bad = '<>:"/\\|?*'
    stem = "".join("_" if ch in bad or ord(ch) < 32 else ch for ch in stem).strip(" .") or "audio"
    p = os.path.join(folder, stem + ext)
    k = 2
    while os.path.exists(p) or os.path.exists(p + ".part"):
        p = os.path.join(folder, f"{stem} ({k}){ext}")
        k += 1
    return p


def save_meta(path: str, data: dict) -> None:
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
