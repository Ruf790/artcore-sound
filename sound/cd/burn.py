"""Burning an Audio CD (2.0): the tracks of a document (its regions, or a track from each marker on, or the whole
file), with pauses between them, CD-TEXT (Latin letters) — as a raw Disc-At-Once image (image.py) written by IMAPI2
(IDiscFormat2RawCD, DAO-96: no gap where there should be none, CD-TEXT), or track by track (IDiscFormat2TrackAtOnce:
2 s pauses, no CD-TEXT) when a drive or a disc will not take the raw image. Erasing a CD-RW, ejecting.

IMAPI2 is Windows' own (COM, through comtypes). ARTCORE_FAKE_BURNER (a folder) puts in a recorder of its own that
writes what it is given there — the tests and the self-test."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import cdtext, image
from .toc import FRAMES_PER_SECTOR, MAX_SECTORS, RATE, SECTOR, Toc, Track

MIN_TRACK = 4 * 75                     # Red Book: a track of at least 4 s
RAW_SUBCODE = 3                        # IMAPI_FORMAT2_RAW_CD_SUBCODE_IS_RAW


class BurnError(RuntimeError):
    pass


# ── what goes on the disc ─────────────────────────────────────
@dataclass
class TrackSpec:
    a: int                             # frames of the (44.1 kHz) document
    b: int
    title: str = ""
    performer: str = ""


@dataclass
class Plan:
    tracks: list = field(default_factory=list)          # TrackSpec
    gap: float = 2.0                                     # seconds of silence before tracks 2… (index 0)
    album: str = ""
    performer: str = ""
    cdtext: bool = True
    translit: bool = True

    def text(self) -> cdtext.CdText:
        t = cdtext.CdText({"title": self.album, "performer": self.performer},
                          [{"title": s.title, "performer": s.performer} for s in self.tracks])
        t.album = {k: v for k, v in t.album.items() if v}
        t.tracks = [{k: v for k, v in d.items() if v} for d in t.tracks]
        return t.spelled() if self.translit else t

    def lengths(self) -> list[int]:
        return [-(-(s.b - s.a) // FRAMES_PER_SECTOR) for s in self.tracks]

    def toc(self) -> Toc:
        from .toc import from_lengths
        g = int(round(self.gap * 75))
        return from_lengths(self.lengths(), [g] * max(0, len(self.tracks) - 1))

    def problems(self, capacity: int = MAX_SECTORS) -> list[str]:
        from ..i18n import t
        out = []
        if not self.tracks:
            out.append(t("cd_no_tracks"))
        if len(self.tracks) > 99:
            out.append(t("cd_too_many"))
        short = [i + 1 for i, n in enumerate(self.lengths()) if n < MIN_TRACK]
        if short:
            out.append(t("cd_short", ", ".join(map(str, short))))
        if self.tracks and self.toc().leadout > capacity:
            out.append(t("cd_too_long", f"{self.toc().leadout / 4500:.1f}", f"{capacity / 4500:.1f}"))
        return out


def tracks_of(d, how: str) -> list[TrackSpec]:
    """how: "regions" — each region a track (in time order); "markers" — a track from the start and from each
    marker on, to the next; "whole" — one track."""
    if how == "regions":
        regs = sorted((m for m in d.markers if m.is_region and m.length > 0), key=lambda m: m.pos)
        return [TrackSpec(m.pos, m.end, m.name or "") for m in regs]
    if how == "markers":
        pts = sorted({m.pos for m in d.markers if not m.is_region and 0 < m.pos < d.frames})
        edges = [0] + pts + [d.frames]
        names = {m.pos: m.name for m in d.markers if not m.is_region}
        return [TrackSpec(a, b, names.get(a, "")) for a, b in zip(edges, edges[1:]) if b > a]
    return [TrackSpec(0, d.frames, d.name and os.path.splitext(d.name)[0] or "")]


def prepare(d, plan: Plan, folder: str, progress=None) -> tuple[Toc, str]:
    """The program area as 16-bit 44.1 kHz stereo (TPDF dither — none where the sound is 16-bit already, so a rip
    goes back bit for bit), pauses and the ends of tracks filled with silence, into a file → (the table of
    contents, its path)."""
    from .. import proc
    from ..media import Progress
    pr = progress or Progress()
    src = d
    scale = 1.0
    conv = None
    if d.rate != RATE or d.channels != 2:
        from ..doc import Document
        conv = proc.convert_document(d, RATE, 2, "stereo", proc.SubProgress(pr, 0.0, 0.4))
        src = Document(rate=RATE, channels=2, folder=folder, labels=("L", "R"), layout="stereo", name="cd")
        src.set_initial(conv)
        scale = RATE / d.rate
    toc = plan.toc()
    path = os.path.join(folder, "cd-program.pcm")
    rng = np.random.default_rng(12345)
    gap = int(round(plan.gap * 75))
    total = max(1, toc.leadout)
    done = 0
    try:
        with open(path, "wb") as f:
            for i, (s, t) in enumerate(zip(plan.tracks, toc.audio)):
                if i:
                    f.write(b"\0" * gap * SECTOR)
                a, b = int(round(s.a * scale)), int(round(s.b * scale))
                b = min(b, src.frames)
                written = 0
                for _pos, x in src.chunks(a, b):
                    y = np.asarray(x, np.float64)[:, :2] * 32768.0
                    if not np.array_equal(y, np.round(y)):   # 16-bit already (a rip): bit for bit, else dither
                        y += rng.random(y.shape) - rng.random(y.shape)       # TPDF ±1 LSB
                    f.write(np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes())
                    written += len(x)
                    base = 0.4 if src is not d else 0.0
                    pr(base + (1.0 - base) * (done + written // FRAMES_PER_SECTOR) / total)
                pad = t.length * FRAMES_PER_SECTOR - written
                f.write(b"\0" * pad * 4)
                done += t.length + (gap if i else 0)
    finally:
        if conv is not None:                     # the converted sound's file goes (not the folder: the program's)
            from ..store import delete_source
            delete_source(conv)
    return toc, path


def program_reader(path: str):
    """program(lba, n) over the prepared file."""
    f = open(path, "rb")
    lock = threading.Lock()

    def read(lba: int, n: int) -> bytes:
        with lock:
            f.seek(lba * SECTOR)
            return f.read(n * SECTOR).ljust(n * SECTOR, b"\0")
    read.close = f.close
    return read


def make_image(d, plan: Plan, folder: str, progress=None) -> tuple[Toc, str]:
    """The raw DAO-96 image (with CD-TEXT if wanted) → (the table of contents, its path)."""
    from .. import proc
    from ..media import Progress
    pr = progress or Progress()
    toc, pcm = prepare(d, plan, folder, proc.SubProgress(pr, 0.0, 0.5))
    packs = cdtext.encode(plan.text()) if plan.cdtext else []
    img = os.path.join(folder, "cd-image.raw")
    prog = program_reader(pcm)
    try:
        image.build(img, toc, prog, packs, proc.SubProgress(pr, 0.5, 1.0))
    finally:
        prog.close()
        os.remove(pcm)
    return toc, img


def save_cue(d, plan: Plan, cue_path: str, folder: str, progress=None) -> str:
    from .. import proc
    from ..media import Progress
    pr = progress or Progress()
    toc, pcm = prepare(d, plan, folder, proc.SubProgress(pr, 0.0, 0.6))
    prog = program_reader(pcm)
    try:
        return image.write_cue(cue_path, toc, prog, plan.text(), proc.SubProgress(pr, 0.6, 1.0))
    finally:
        prog.close()
        os.remove(pcm)


# ── the recorders ─────────────────────────────────────────────
@dataclass
class Recorder:
    id: str
    name: str
    path: str = ""                     # "E:\\"


@dataclass
class Media:
    present: bool = False
    blank: bool = False
    rewritable: bool = False
    free: int = 0                      # sectors
    kind: str = ""


def recorders() -> list[Recorder]:
    fake = os.environ.get("ARTCORE_FAKE_BURNER")
    if fake:
        return [Recorder("FAKE", "ArtCore Fake Recorder", fake)]
    if sys.platform != "win32":
        return []
    with _Com():
        dm = _create("IMAPI2.MsftDiscMaster2")
        if not dm.IsSupportedEnvironment:
            return []
        out = []
        for i in range(dm.Count):
            rid = dm.Item(i)
            rec = _create("IMAPI2.MsftDiscRecorder2")
            rec.InitializeDiscRecorder(rid)
            paths = list(rec.VolumePathNames or ())
            name = " ".join(x.strip() for x in (rec.VendorId or "", rec.ProductId or "") if x and x.strip())
            out.append(Recorder(rid, f"{paths[0] if paths else ''} {name}".strip(), paths[0] if paths else ""))
        return out


def media(rec: Recorder) -> Media:
    if rec.id == "FAKE":
        return Media(True, True, True, MAX_SECTORS, "CD-RW")
    with _Com():
        r = _recorder(rec)
        f = _create("IMAPI2.MsftDiscFormat2Data")
        if not f.IsRecorderSupported(r):
            return Media()
        _put(f, "Recorder", r)
        try:
            kind = int(f.CurrentPhysicalMediaType)
        except Exception:                                # noqa: BLE001 — no disc
            return Media()
        names = {1: "CD-ROM", 2: "CD-R", 3: "CD-RW"}
        m = Media(kind != 0, False, kind == 3, 0, names.get(kind, f"type {kind}"))
        try:
            m.blank = bool(f.MediaHeuristicallyBlank)
            m.free = int(f.FreeSectorsOnMedia)
        except Exception:                                # noqa: BLE001
            pass
        return m


def burn(rec: Recorder, img: str, toc: Toc, speed: int = 0, progress=None, eject: bool = True) -> None:
    """The raw image onto the blank disc (Disc-At-Once). speed: ×1 = 75 sectors a second, 0 — the fastest."""
    if rec.id == "FAKE":
        _fake_copy(img, os.path.join(rec.path, "disc.raw"), progress)
        return
    with _Com():
        r = _recorder(rec)
        f = _create("IMAPI2.MsftDiscFormat2RawCD")
        if not f.IsRecorderSupported(r):
            raise BurnError("raw")
        _put(f, "Recorder", r)
        f.ClientName = "ArtCore Sound"
        if not f.IsCurrentMediaSupported(r):
            raise BurnError("media")
        f.PrepareMedia()
        try:
            f.RequestedSectorType = RAW_SUBCODE
            if speed:
                f.SetWriteSpeed(int(speed) * 75, False)
            skip = 0
            try:
                start = int(f.StartOfNextSession)       # where the drive's lead-in begins (a blank disc: < 0)
                if image.LEADIN_START < start < 0:
                    skip = start - image.LEADIN_START
            except Exception:                            # noqa: BLE001
                skip = 0
            with _Ticker(progress, (os.path.getsize(img) // image.RAW - skip) / (75.0 * (speed or 16))):
                try:
                    write2 = f.WriteMedia2                 # the image from MSF 95:00:00, the drive's lead-in later
                except AttributeError:
                    write2 = None
                if write2 is not None:
                    write2(_stream(img), skip)
                else:
                    f.WriteMedia(_stream(img, skip))
        finally:
            try:
                f.ReleaseMedia()
            except Exception:                            # noqa: BLE001
                pass
        if eject:
            try:
                r.EjectMedia()
            except Exception:                            # noqa: BLE001
                pass


def burn_tao(rec: Recorder, pcm_tracks: list[str], speed: int = 0, progress=None, eject: bool = True) -> None:
    """Track by track (2 s pauses, no CD-TEXT): each track a file of 16-bit sound (a multiple of 2352 bytes)."""
    if rec.id == "FAKE":
        for i, p in enumerate(pcm_tracks):
            _fake_copy(p, os.path.join(rec.path, f"track{i + 1:02d}.pcm"), None)
        if progress:
            progress(1.0)
        return
    with _Com():
        r = _recorder(rec)
        f = _create("IMAPI2.MsftDiscFormat2TrackAtOnce")
        if not f.IsRecorderSupported(r):
            raise BurnError("tao")
        _put(f, "Recorder", r)
        f.ClientName = "ArtCore Sound"
        if not f.IsCurrentMediaSupported(r):
            raise BurnError("media")
        f.PrepareMedia()
        try:
            if speed:
                f.SetWriteSpeed(int(speed) * 75, False)
            total = sum(os.path.getsize(p) for p in pcm_tracks) / SECTOR
            done = 0.0
            for p in pcm_tracks:
                n = os.path.getsize(p) / SECTOR
                lo, hi = done / total, (done + n) / total
                sub = (lambda x, lo=lo, hi=hi: progress(lo + (hi - lo) * x)) if progress else None
                with _Ticker(sub, n / (75.0 * (speed or 16))):
                    f.AddAudioTrack(_stream(p))
                done += n
        finally:
            try:
                f.ReleaseMedia()
            except Exception:                            # noqa: BLE001
                pass
        if eject:
            try:
                r.EjectMedia()
            except Exception:                            # noqa: BLE001
                pass


def split_tracks(pcm: str, toc: Toc, folder: str) -> list[str]:
    """The prepared program → a file per track (Track-At-Once; the pauses are the drive's)."""
    out = []
    with open(pcm, "rb") as f:
        for t in toc.audio:
            p = os.path.join(folder, f"cd-track{t.number:02d}.pcm")
            f.seek(t.start * SECTOR)
            with open(p, "wb") as g:
                left = t.length * SECTOR
                while left:
                    b = f.read(min(left, 1 << 22))
                    g.write(b)
                    left -= len(b)
            out.append(p)
    return out


def erase(rec: Recorder, full: bool = False, progress=None) -> None:
    if rec.id == "FAKE":
        for n in os.listdir(rec.path):
            os.remove(os.path.join(rec.path, n))
        return
    with _Com():
        r = _recorder(rec)
        f = _create("IMAPI2.MsftDiscFormat2Erase")
        _put(f, "Recorder", r)
        f.ClientName = "ArtCore Sound"
        f.FullErase = bool(full)
        with _Ticker(progress, 600.0 if full else 60.0):
            f.EraseMedia()


def eject(rec: Recorder) -> None:
    if rec.id == "FAKE":
        return
    with _Com():
        _recorder(rec).EjectMedia()


# ── COM ───────────────────────────────────────────────────────
class _Com:
    """COM for this thread (multithreaded: IMAPI2 works from any thread)."""

    def __enter__(self):
        import comtypes
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self.ok = True
        except OSError:
            self.ok = False                              # already, another way
        return self

    def __exit__(self, *a):
        if self.ok:
            import comtypes
            comtypes.CoUninitialize()


def _create(progid: str):
    import comtypes.client
    return comtypes.client.CreateObject(progid, dynamic=True)


def _recorder(rec: Recorder):
    r = _create("IMAPI2.MsftDiscRecorder2")
    r.InitializeDiscRecorder(rec.id)
    return r


def _put(obj, name: str, value) -> None:
    """A property that takes a COM object (Recorder): by reference when a plain put will not do."""
    try:
        setattr(obj, name, value)
        return
    except Exception:                                    # noqa: BLE001
        pass
    disp = obj._comobj
    dispid = disp.GetIDsOfNames(name)[0]
    disp.Invoke(dispid, value._comobj if hasattr(value, "_comobj") else value, _invkind=8)   # PROPERTYPUTREF


def _stream(path: str, skip_sectors: int = 0):
    """An IStream over a file (SHCreateStreamOnFileEx), moved past skip_sectors of a raw image."""
    import ctypes
    from ctypes import wintypes

    from comtypes import GUID, HRESULT, POINTER, STDMETHOD, IUnknown

    class IStream(IUnknown):
        _iid_ = GUID("{0000000C-0000-0000-C000-000000000046}")
        _methods_ = [STDMETHOD(HRESULT, "Read", [ctypes.c_void_p, ctypes.c_ulong, POINTER(ctypes.c_ulong)]),
                     STDMETHOD(HRESULT, "Write", [ctypes.c_void_p, ctypes.c_ulong, POINTER(ctypes.c_ulong)]),
                     STDMETHOD(HRESULT, "Seek", [ctypes.c_longlong, ctypes.c_ulong, POINTER(ctypes.c_ulonglong)])]
    fn = ctypes.windll.shlwapi.SHCreateStreamOnFileEx
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p,
                   ctypes.POINTER(POINTER(IStream))]
    fn.restype = HRESULT
    p = POINTER(IStream)()
    fn(path, 0x00000020, 0x80, False, None, ctypes.byref(p))      # STGM_READ | STGM_SHARE_DENY_WRITE, normal file
    if skip_sectors:
        p.Seek(skip_sectors * image.RAW, 0, None)
    return p


class _Ticker:
    """Progress while IMAPI2 writes (no events here): the time gone against the time it should take."""

    def __init__(self, progress, seconds: float):
        self.progress, self.seconds = progress, max(1.0, seconds)
        self._stop = threading.Event()

    def __enter__(self):
        if self.progress is not None:
            t0 = time.monotonic()

            def run():
                while not self._stop.wait(0.5):
                    self.progress(min(0.99, (time.monotonic() - t0) / self.seconds))
            threading.Thread(target=run, daemon=True).start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self.progress is not None and a[0] is None:
            self.progress(1.0)


def _fake_copy(src: str, dst: str, progress) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    if progress is not None:
        progress(1.0)


__all__ = ["BurnError", "Media", "Plan", "Recorder", "Track", "TrackSpec", "burn", "burn_tao", "erase", "eject",
           "make_image", "media", "prepare", "recorders", "save_cue", "split_tracks", "tracks_of"]
